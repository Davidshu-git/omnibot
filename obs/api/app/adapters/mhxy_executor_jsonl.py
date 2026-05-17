"""Adapter for Windows MHXY executor structured JSONL events."""
from __future__ import annotations

import glob as glob_module
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.adapters.common import now as _now, parse_ts as _parse_ts
from app.schemas.events import (
    EventType,
    NormalizedEvent,
    RawEventBlob,
    SessionRef,
    SourceRef,
)

log = logging.getLogger(__name__)

PROJECT_ID = "mhxy"
AGENT_ID = "windows-executor"
SOURCE = "mhxy_executor_jsonl"
_EVENT_TYPES = {"executor_request", "executor_internal", "executor_startup"}


class MhxyExecutorJsonlAdapter:
    """Read executor_events_YYYYMMDD.jsonl and map rows to task_event records."""

    source_type = SOURCE

    def __init__(self, log_dir: str) -> None:
        self.log_dir = log_dir

    def discover_sources(self) -> list[SourceRef]:
        """Discover executor event files in the configured directory."""
        pattern = os.path.join(self.log_dir, "executor_events_*.jsonl")
        files = glob_module.glob(pattern)
        return [SourceRef(source_id=Path(f).stem, path=f) for f in sorted(files)]

    def scan_sessions(self, source: SourceRef) -> list[SessionRef]:
        """Return all session buckets present in this executor file."""
        session_ids: set[str] = set()
        with open(source.path, encoding="utf-8") as f:
            if source.start_offset:
                f.seek(source.start_offset)
            for lineno, line in enumerate(f, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("skip malformed executor line in %s at line %d", source.path, lineno)
                    continue
                if isinstance(rec, dict):
                    session_ids.add(self._session_id_for(rec))
        return [SessionRef(session_id=sid, source_ref=source) for sid in sorted(session_ids)]

    def load_events(
        self, session: SessionRef
    ) -> tuple[list[RawEventBlob], list[NormalizedEvent]]:
        """Load raw rows and normalized events from one executor JSONL file."""
        raw_blobs: list[RawEventBlob] = []
        events: list[NormalizedEvent] = []
        path = session.source_ref.path

        with open(path, encoding="utf-8") as f:
            if session.source_ref.start_offset:
                f.seek(session.source_ref.start_offset)
            for lineno, line in enumerate(f, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("skip malformed executor line in %s at line %d", path, lineno)
                    continue
                if not isinstance(rec, dict):
                    continue

                session_id = self._session_id_for(rec)
                if session_id != session.session_id:
                    continue
                external_key = self._content_key(session_id, rec)
                collected_at = _now()
                raw_blobs.append(RawEventBlob(
                    project_id=PROJECT_ID,
                    source=SOURCE,
                    external_key=external_key,
                    collected_at=collected_at,
                    payload_json=rec,
                    payload_hash="",
                ))

                event = self._map_record(rec, session_id, external_key)
                if event is not None:
                    events.append(event)

        return raw_blobs, events

    def _session_id_for(self, rec: dict[str, Any]) -> str:
        """Use NAS session_id when present; otherwise bucket self-health by host/date."""
        session_id = rec.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id

        ts = _parse_ts(rec.get("timestamp")) or datetime.now(timezone.utc)
        host = str(rec.get("host") or "unknown")
        return f"executor_self_{host}_{ts.strftime('%Y%m%d')}"

    def _content_key(self, session_id: str, rec: dict[str, Any]) -> str:
        """Stable content-addressed id for append-only executor rows."""
        digest = hashlib.sha256(
            json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:20]
        return f"mhxy_executor:{session_id}:{digest}"

    def _map_record(
        self,
        rec: dict[str, Any],
        session_id: str,
        external_key: str,
    ) -> NormalizedEvent | None:
        """Map an executor row into the unified task_event schema."""
        rtype = rec.get("type")
        if rtype not in _EVENT_TYPES:
            return None

        ts = _parse_ts(rec.get("timestamp")) or _now()
        trace_id = rec.get("trace_id")
        run_id = rec.get("request_id")
        return NormalizedEvent(
            event_id=external_key,
            project_id=PROJECT_ID,
            agent_id=AGENT_ID,
            session_id=session_id,
            trace_id=str(trace_id) if trace_id else None,
            run_id=str(run_id) if run_id else None,
            timestamp=ts,
            source=SOURCE,
            event_type=EventType.TASK_EVENT,
            payload=rec,
        )
