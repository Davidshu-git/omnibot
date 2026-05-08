"""
mhxy_jsonl_adapter — reads mhxy logs/sessions/*.jsonl files.

mhxy JSONL format (one JSON object per line):
  session     {type, id, timestamp, channel}
  message     {type, timestamp, role, content}
  model_call  {type, timestamp, model, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, total_tokens,
               duration_ms, stop_reason, error_message}
  thought     {type, timestamp, content}
  tool_call   {type, timestamp, tool_name, arguments}
  tool_result {type, timestamp, tool_name, output, success, duration_ms, error_message, meta?}

Mapping rules:
  - project_id  = "mhxy"
  - agent_id    = "game-bot"
  - session_id  = record["id"] from the first "session" line in the file
                  (falls back to filename stem if missing)
  - trace_id    = source trace_id/task_run_id when present; otherwise
                  "{session_id}:t{n}" for legacy conversation turns and
                  "{session_id}:scan:{hash}" for diagnostic scans
  - run_id      = "{trace_id}:r{m}" — each model_call within the trace gets one
  - event_id    = "mhxy:{file_path}:{line_number}"
  - external_key = same as event_id (used as raw blob key)
  - mhxy-specific fields not in the unified schema go into extra
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

PROJECT_ID = "mhxy"
AGENT_ID = "game-bot"
SOURCE = "mhxy_jsonl"

_RUNNER_EVENT_TYPES = frozenset({
    "scan_started",
    "task_started", "task_completed", "task_failed", "task_needs_human",
    "task_step_started", "task_step_completed", "task_step_failed",
    "task_denylist_triggered", "instance_status", "reconnect_result",
    "reconnect_step", "executor_perf",
})


def _content_key(session_id: str, record: dict) -> str:
    """
    Stable, content-addressed key for a log record.
    Uses session_id (from log, not file path) + SHA256 of canonical JSON.
    Immune to file rename, rotation, or path changes.
    80-bit hash prefix is collision-safe at mhxy session volumes.
    """
    digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]
    return f"mhxy:{session_id}:{digest}"


def _scan_trace_id(session_id: str, record: dict) -> str:
    """Stable trace boundary for scan_started records that do not carry trace_id."""
    trace_basis = {
        "timestamp": record.get("timestamp"),
        "operation": record.get("operation"),
        "ports": record.get("ports"),
        "port": record.get("port"),
    }
    digest = hashlib.sha256(
        json.dumps(trace_basis, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]
    return f"{session_id}:scan:{digest}"


class MhxyJsonlAdapter:
    source_type = SOURCE

    def __init__(self, log_dir: str):
        self.log_dir = log_dir

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
        session_id = self._extract_session_id(source.path) or source.source_id
        return [SessionRef(session_id=session_id, source_ref=source)]

    def load_events(
        self, session: SessionRef
    ) -> tuple[list[RawEventBlob], list[NormalizedEvent]]:
        path = session.source_ref.path
        session_id = session.session_id
        raw_blobs: list[RawEventBlob] = []
        events: list[NormalizedEvent] = []

        # Trace/run counters — reset per file
        trace_counter = 0
        run_counter = 0
        current_trace_id: str | None = None

        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skip malformed line in %s at line %d: %r", path, lineno, line[:120])
                    continue

                external_key = _content_key(session_id, record)
                collected_at = _now()

                rtype = record.get("type")

                source_trace_id = record.get("trace_id") or record.get("task_run_id")

                # Advance trace for conversation turns
                if source_trace_id:
                    current_trace_id = str(source_trace_id)
                elif rtype == "message" and record.get("role") == "user":
                    trace_counter += 1
                    run_counter = 0
                    current_trace_id = f"{session_id}:t{trace_counter}"
                # Each task run gets its own trace
                elif rtype == "task_started":
                    trace_counter += 1
                    current_trace_id = str(record.get("task_run_id") or f"{session_id}:t{trace_counter}")
                # Diagnostic scans: scan_started is the explicit trace boundary
                elif rtype == "scan_started":
                    current_trace_id = _scan_trace_id(session_id, record)

                # Assign run_id to each model_call
                run_id: str | None = None
                if rtype == "model_call" and current_trace_id:
                    run_counter += 1
                    run_id = f"{current_trace_id}:r{run_counter}"

                blob = RawEventBlob(
                    project_id=PROJECT_ID,
                    source=SOURCE,
                    external_key=external_key,
                    collected_at=collected_at,
                    payload_json=record,
                    payload_hash="",  # filled by ingestion service
                )
                raw_blobs.append(blob)

                event = self._map_record(
                    record=record,
                    session_id=session_id,
                    trace_id=current_trace_id,
                    run_id=run_id,
                    external_key=external_key,
                    collected_at=collected_at,
                )
                if event is not None:
                    events.append(event)

        return raw_blobs, events

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _extract_session_id(self, path: str) -> str | None:
        """Read first line of file to get session id."""
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
                if first:
                    rec = json.loads(first)
                    if rec.get("type") == "session":
                        return rec.get("id")
        except Exception:
            pass
        return None

    def _map_record(
        self,
        record: dict[str, Any],
        session_id: str,
        trace_id: str | None,
        run_id: str | None,
        external_key: str,
        collected_at: datetime,
    ) -> NormalizedEvent | None:
        rtype = record.get("type")
        ts = _parse_ts(record.get("timestamp"))
        event_id = external_key  # already "mhxy:{session_id}:{hash}"

        base = dict(
            event_id=event_id,
            project_id=PROJECT_ID,
            agent_id=AGENT_ID,
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
                    "channel": record.get("channel"),
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
                    "provider": "dashscope",
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

        if rtype == "thought":
            return NormalizedEvent(
                **base,
                event_type=EventType.THOUGHT,
                payload={
                    "kind": "custom_think",
                    "provider": "custom",
                    "content": record.get("content", ""),
                    "summary_level": "unknown",
                },
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
            payload = {
                "tool_name": record.get("tool_name", ""),
                "success": record.get("success", True),
                "result": record.get("output"),
                "duration_ms": record.get("duration_ms"),
            }
            if record.get("meta") is not None:
                payload["meta"] = record["meta"]
            return NormalizedEvent(
                **base,
                event_type=EventType.TOOL_RESULT,
                payload=payload,
                extra={"error_message": record.get("error_message")} if record.get("error_message") else {},
            )

        # Runner / automation events — flat payload, type field preserved inside
        if rtype in _RUNNER_EVENT_TYPES:
            return NormalizedEvent(
                **base,
                event_type=EventType.TASK_EVENT,
                payload=record,
            )

        # Unknown types — store as generic event, preserve all fields in extra
        return NormalizedEvent(
            **base,
            event_type=EventType.EVENT,
            payload={
                "name": f"mhxy_{rtype or 'unknown'}",
                "payload": record,
            },
            extra={"original_type": rtype},
        )
