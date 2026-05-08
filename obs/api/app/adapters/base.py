"""
Adapter contract.

Every adapter must implement discover_sources / scan_sessions / load_events.
The adapter is responsible for:
  - Reading external data
  - Parsing raw format
  - Mapping to NormalizedEvent + RawEventBlob
  - Populating stable IDs

The adapter must NOT perform DB writes directly — it only returns data.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.events import NormalizedEvent, RawEventBlob, SessionRef, SourceRef


@runtime_checkable
class BaseAdapter(Protocol):
    source_type: str

    def discover_sources(self) -> list[SourceRef]: ...

    def scan_sessions(self, source: SourceRef) -> list[SessionRef]: ...

    def load_events(
        self, session: SessionRef
    ) -> tuple[list[RawEventBlob], list[NormalizedEvent]]: ...
