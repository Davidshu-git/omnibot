"""
omnibot_jsonl_adapter — reads omnibot observability JSONL files.

Format (produced by core/observability.py in omnibot):
  session     {type, id, timestamp, channel, agent_id}
  message     {type, timestamp, role, content, trace_id?}
  thought     {type, timestamp, content, trace_id?}
  model_call  {type, timestamp, model, provider, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, total_tokens,
               duration_ms, stop_reason, error_message, trace_id?, run_id?}
  tool_call   {type, timestamp, tool_name, arguments, trace_id?, run_id?}
  tool_result {type, timestamp, tool_name, output, success, duration_ms,
               error_message, trace_id?, run_id?}

Key differences from mhxy_jsonl:
  - trace_id and run_id are already embedded in each record (no need to derive)
  - agent_id is stored in the session record (not fixed per-adapter)
  - File naming: {session_id}_{YYYYMMDD}.jsonl
"""

from __future__ import annotations

import glob as glob_module
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.adapters.common import now as _now, parse_ts as _parse_ts

log = logging.getLogger(__name__)

from app.schemas.events import (
    EventType,
    NormalizedEvent,
    RawEventBlob,
    SessionRef,
    SourceRef,
)

SOURCE = "omnibot_jsonl"


def _content_key(session_id: str, record: dict) -> str:
    """Content-addressed key: stable across file renames/rotation."""
    digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]
    return f"omnibot:{session_id}:{digest}"


class OmnibotJsonlAdapter:
    source_type = SOURCE

    def __init__(self, log_dir: str, project_id: str = "omnibot") -> None:
        self.log_dir = log_dir
        self.project_id = project_id

    # ------------------------------------------------------------------
    # Adapter interface
    # ------------------------------------------------------------------

    def discover_sources(self) -> list[SourceRef]:
        pattern = os.path.join(self.log_dir, "**", "*.jsonl")
        files = glob_module.glob(pattern, recursive=True)
        return [
            SourceRef(source_id=Path(f).stem, path=f)
            for f in sorted(files)
        ]

    def scan_sessions(self, source: SourceRef) -> list[SessionRef]:
        session_id, agent_id = self._read_session_header(source.path)
        if not session_id:
            session_id = source.source_id  # fallback to filename stem
        if not agent_id:
            agent_id = "unknown"
        return [SessionRef(
            session_id=session_id,
            source_ref=source,
            metadata={"agent_id": agent_id},
        )]

    def load_events(
        self, session: SessionRef
    ) -> tuple[list[RawEventBlob], list[NormalizedEvent]]:
        path = session.source_ref.path
        session_id = session.session_id
        agent_id = (session.metadata or {}).get("agent_id", "unknown")

        raw_blobs: list[RawEventBlob] = []
        events: list[NormalizedEvent] = []

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skip malformed line in %s: %r", path, line[:120])
                    continue

                external_key = _content_key(session_id, record)
                collected_at = _now()

                blob = RawEventBlob(
                    project_id=self.project_id,
                    source=SOURCE,
                    external_key=external_key,
                    collected_at=collected_at,
                    payload_json=record,
                    payload_hash="",
                )
                raw_blobs.append(blob)

                event = self._map_record(record, session_id, agent_id, external_key)
                if event is not None:
                    events.append(event)

        return raw_blobs, events

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_session_header(self, path: str) -> tuple[str | None, str | None]:
        """Read session_id and agent_id from the first session record."""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("type") == "session":
                        return rec.get("id"), rec.get("agent_id")
        except Exception:
            pass
        return None, None

    def _map_record(
        self,
        record: dict[str, Any],
        session_id: str,
        agent_id: str,
        external_key: str,
    ) -> NormalizedEvent | None:
        rtype = record.get("type")
        ts = _parse_ts(record.get("timestamp"))
        event_id = external_key  # already "omnibot:{session_id}:{hash}"

        # trace_id and run_id come directly from the record
        trace_id: str | None = record.get("trace_id")
        run_id: str | None = record.get("run_id")

        base = dict(
            event_id=event_id,
            project_id=self.project_id,
            agent_id=agent_id,
            session_id=session_id,
            trace_id=trace_id,
            run_id=run_id,
            timestamp=ts,
            source=SOURCE,
        )

        if rtype == "session":
            return NormalizedEvent(
                **base,
                event_type=EventType.SESSION_STARTED,
                payload={
                    "channel": record.get("channel", "tg"),
                    "title": None,
                },
            )

        if rtype == "message":
            return NormalizedEvent(
                **base,
                event_type=EventType.MESSAGE,
                payload={
                    "role": record.get("role", "user"),
                    "content": record.get("content", ""),
                },
            )

        if rtype == "thought":
            return NormalizedEvent(
                **base,
                event_type=EventType.THOUGHT,
                payload={
                    "kind": "extracted",
                    "provider": "dashscope",
                    "content": record.get("content", ""),
                    "summary_level": "unknown",
                },
            )

        if rtype == "model_call":
            extra: dict[str, Any] = {}
            if record.get("stop_reason"):
                extra["stop_reason"] = record["stop_reason"]
            if record.get("cache_write_tokens"):
                extra["cache_write_tokens"] = record["cache_write_tokens"]
            return NormalizedEvent(
                **base,
                event_type=EventType.MODEL_CALL,
                payload={
                    "provider": record.get("provider", "dashscope"),
                    "model": record.get("model"),
                    "prompt": record.get("prompt"),
                    "raw_output": record.get("raw_output"),
                    "input_tokens": record.get("input_tokens"),
                    "output_tokens": record.get("output_tokens"),
                    "reasoning_tokens": None,
                    "cache_read_tokens": record.get("cache_read_tokens"),
                    "duration_ms": record.get("duration_ms"),
                    "success": record.get("error_message") is None,
                },
                extra=extra,
            )

        if rtype == "tool_call":
            raw_args = record.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    pass
            return NormalizedEvent(
                **base,
                event_type=EventType.TOOL_CALL,
                payload={
                    "tool_name": record.get("tool_name", ""),
                    "arguments": raw_args,
                },
            )

        if rtype == "tool_result":
            return NormalizedEvent(
                **base,
                event_type=EventType.TOOL_RESULT,
                payload={
                    "tool_name": record.get("tool_name", ""),
                    "success": record.get("success", True),
                    "result": record.get("output"),
                    "duration_ms": record.get("duration_ms"),
                },
                extra={"error_message": record.get("error_message")} if record.get("error_message") else {},
            )

        # Unknown types — preserve as generic event
        return NormalizedEvent(
            **base,
            event_type=EventType.EVENT,
            payload={
                "name": f"omnibot_{rtype or 'unknown'}",
                "payload": record,
            },
            extra={"original_type": rtype},
        )
