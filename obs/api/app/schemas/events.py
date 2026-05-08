"""
Unified event schema — the single source of truth for all normalized events.

All adapters must map their source data to these types.
Project-specific fields go into `extra`, never into top-level fields.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Event type enum
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    MESSAGE = "message"
    THOUGHT = "thought"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    METRIC = "metric"
    EVENT = "event"
    TASK_EVENT = "task_event"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Payload models — one per EventType
# ---------------------------------------------------------------------------

class SessionStartedPayload(BaseModel):
    channel: Optional[str] = None
    title: Optional[str] = None


class SessionEndedPayload(BaseModel):
    status: str = "unknown"  # success | failed | interrupted | unknown


class MessagePayload(BaseModel):
    role: str  # user | assistant | system | tool
    content: str


class ThoughtKind(str, Enum):
    REASONING_SUMMARY = "reasoning_summary"
    CUSTOM_THINK = "custom_think"
    EXTRACTED = "extracted"


class ThoughtPayload(BaseModel):
    kind: ThoughtKind = ThoughtKind.CUSTOM_THINK
    provider: str = "custom"
    content: str
    summary_level: str = "unknown"  # brief | detailed | unknown


class ModelCallPayload(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    raw_output: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    duration_ms: Optional[float] = None
    success: bool = True


class ToolCallPayload(BaseModel):
    tool_name: str
    arguments: Optional[Any] = None


class ToolResultPayload(BaseModel):
    tool_name: str
    success: bool = True
    result: Optional[Any] = None
    duration_ms: Optional[float] = None


class MetricPayload(BaseModel):
    metric_name: str
    metric_value: float
    metric_unit: Optional[str] = None


class CustomEventPayload(BaseModel):
    name: str
    payload: Optional[Any] = None


class ErrorSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorPayload(BaseModel):
    name: str
    message: str
    stack: Optional[str] = None
    severity: ErrorSeverity = ErrorSeverity.ERROR


# ---------------------------------------------------------------------------
# Normalized event — the envelope every adapter must produce
# ---------------------------------------------------------------------------

class NormalizedEvent(BaseModel):
    """
    The unified event model.  All fields except payload/extra come from
    the standard schema; payload content depends on event_type.
    """
    event_id: str = Field(..., description="Platform-internal unique event ID")
    project_id: str
    agent_id: Optional[str] = None
    session_id: str
    trace_id: Optional[str] = None
    run_id: Optional[str] = None
    event_type: EventType
    timestamp: datetime
    source: str  # e.g. mhxy_jsonl | langsmith
    payload: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Raw blob — the raw source record before normalization
# ---------------------------------------------------------------------------

class RawEventBlob(BaseModel):
    project_id: str
    source: str
    external_key: str     # e.g. "path/to/file.jsonl:42"
    collected_at: datetime
    payload_json: dict[str, Any]
    payload_hash: str     # sha256 of canonical JSON


# ---------------------------------------------------------------------------
# Adapter contract types
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    """Pointer to a discoverable data source (e.g., a JSONL file)."""
    source_id: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRef(BaseModel):
    """Pointer to a session within a source."""
    session_id: str
    source_ref: SourceRef
    metadata: dict[str, Any] = Field(default_factory=dict)
