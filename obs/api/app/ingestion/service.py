"""
Ingestion service — idempotent write of raw blobs and normalized events.

Rules:
  - raw blobs are deduped by (source, project_id, external_key); content
    changes are detected via payload_hash (UPDATE-in-place).
  - normalized events are deduped by event_id (INSERT OR IGNORE).
  - sessions are upserted by id.
  - All operations are idempotent: re-running the same input is safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Sequence

log = logging.getLogger(__name__)

from sqlalchemy import func as sa_func, select, text as sa_text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, RawEventBlob, Session
from app.schemas.events import NormalizedEvent, RawEventBlob as RawBlobSchema


def _canonical_hash(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def upsert_session(
    db: AsyncSession,
    *,
    session_id: str,
    project_id: str,
    agent_id: str | None = None,
    external_session_id: str | None = None,
    started_at: datetime | None = None,
    status: str = "unknown",
    metadata: dict | None = None,
) -> None:
    # Use __table__ to avoid ORM's 'metadata' attribute shadowing the column
    stmt = pg_insert(Session.__table__).values({
        "id": session_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "external_session_id": external_session_id,
        "started_at": started_at,
        "status": status,
        "metadata": metadata,
    })
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "agent_id": stmt.excluded.agent_id,
            "external_session_id": stmt.excluded.external_session_id,
            # Keep the earliest started_at — never let a later daily file overwrite it
            "started_at": sa_func.coalesce(Session.__table__.c.started_at, stmt.excluded.started_at),
            "status": stmt.excluded.status,
            "metadata": stmt.excluded.metadata,
        },
    )
    await db.execute(stmt)


async def ingest_raw_blob(
    db: AsyncSession,
    blob: RawBlobSchema,
) -> tuple[bool, bool]:
    """
    Insert or update a raw blob.
    Returns (was_inserted, was_updated).

    Uses PostgreSQL xmax to distinguish insert from update:
      xmax = 0  → fresh insert (no prior version in this session)
      xmax != 0 → updated (overwrote an existing row)
      rowcount=0 → no-op (conflict existed but hash unchanged, update skipped)
    """
    payload_hash = _canonical_hash(blob.payload_json)

    insert_stmt = pg_insert(RawEventBlob).values(
        project_id=blob.project_id,
        source=blob.source,
        external_key=blob.external_key,
        collected_at=blob.collected_at,
        payload_json=blob.payload_json,
        payload_hash=payload_hash,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_raw_blob_key",
        set_={
            "payload_json": insert_stmt.excluded.payload_json,
            "payload_hash": insert_stmt.excluded.payload_hash,
            "collected_at": insert_stmt.excluded.collected_at,
        },
        where=RawEventBlob.payload_hash != payload_hash,
    ).returning(sa_text("(xmax = 0) AS was_inserted"))

    result = await db.execute(upsert_stmt)
    row = result.fetchone()
    if row is None:
        return False, False   # skipped: same hash
    was_inserted = bool(row[0])
    return was_inserted, not was_inserted


async def ingest_event(
    db: AsyncSession,
    event: NormalizedEvent,
) -> bool:
    """
    Insert a normalized event. Skips if event_id already exists.
    Returns True if inserted, False if skipped.
    """
    stmt = pg_insert(Event).values(
        event_id=event.event_id,
        project_id=event.project_id,
        agent_id=event.agent_id,
        session_id=event.session_id,
        trace_id=event.trace_id,
        run_id=event.run_id,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        source=event.source,
        payload_json=event.payload,
        extra=event.extra,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
    result = await db.execute(stmt)
    return result.rowcount > 0


async def ingest_batch(
    db: AsyncSession,
    *,
    raw_blobs: Sequence[RawBlobSchema],
    events: Sequence[NormalizedEvent],
) -> dict[str, int]:
    """
    Ingest a batch of raw blobs + normalized events in one transaction.
    Returns counts: {raw_inserted, raw_updated, events_inserted, events_skipped}.
    """
    raw_inserted = raw_updated = events_inserted = events_skipped = 0

    for blob in raw_blobs:
        try:
            inserted, updated = await ingest_raw_blob(db, blob)
            raw_inserted += inserted
            raw_updated += updated
        except Exception as exc:
            log.warning("ingest_raw_blob failed for key=%s: %s", blob.external_key, exc)

    for event in events:
        try:
            ok = await ingest_event(db, event)
            if ok:
                events_inserted += 1
            else:
                events_skipped += 1
        except Exception as exc:
            log.warning("ingest_event failed for event_id=%s: %s", event.event_id, exc)

    await db.commit()
    return {
        "raw_inserted": raw_inserted,
        "raw_updated": raw_updated,
        "events_inserted": events_inserted,
        "events_skipped": events_skipped,
    }
