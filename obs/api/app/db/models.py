"""
SQLAlchemy ORM models.

Tables:
  projects         — registered business projects
  agents           — agent instances / roles within a project
  data_sources     — per-project data source registrations
  sessions         — one user-facing conversation session
  events           — normalized events (the main query table)
  raw_event_blobs  — raw source payloads (audit, replay)
  daily_usage_stats — pre-aggregated token usage (optional, rebuilt from events)
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    source_type: Mapped[str] = mapped_column(String(64))   # mhxy_jsonl | langsmith
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agents: Mapped[list["Agent"]] = relationship(back_populates="project")
    data_sources: Mapped[list["DataSource"]] = relationship(back_populates="project")
    sessions: Mapped[list["Session"]] = relationship(back_populates="project")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str | None] = mapped_column(String(64))   # bot | planner | executor …
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    project: Mapped["Project"] = relationship(back_populates="agents")


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_sync_cursor: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    project: Mapped["Project"] = relationship(back_populates="data_sources")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    external_session_id: Mapped[str | None] = mapped_column(String(256))
    external_trace_id: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    project: Mapped["Project"] = relationship(back_populates="sessions")
    events: Mapped[list["Event"]] = relationship(back_populates="session")

    __table_args__ = (
        Index("ix_sessions_project_started", "project_id", "started_at"),
        Index("ix_sessions_agent_started", "agent_id", "started_at"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(256))
    run_id: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default={})
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default={})

    session: Mapped["Session"] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_events_project_ts", "project_id", "timestamp"),
        Index("ix_events_session_ts", "session_id", "timestamp"),
        Index("ix_events_trace_ts", "trace_id", "timestamp"),
        Index("ix_events_type_ts", "event_type", "timestamp"),
    )


class RawEventBlob(Base):
    __tablename__ = "raw_event_blobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_key: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "project_id", "external_key", name="uq_raw_blob_key"),
        Index("ix_raw_blobs_project_source", "project_id", "source"),
        Index("ix_raw_blobs_hash", "payload_hash"),
    )


class DailyUsageStat(Base):
    __tablename__ = "daily_usage_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("date", "project_id", "agent_id", "model", name="uq_daily_usage"),
        Index("ix_daily_usage_project_date", "project_id", "date"),
    )
