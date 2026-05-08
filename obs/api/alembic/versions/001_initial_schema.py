"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # projects
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # agents
    op.create_table(
        "agents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(64)),
        sa.Column("metadata", postgresql.JSONB()),
    )

    # data_sources
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("config_json", postgresql.JSONB()),
        sa.Column("last_sync_cursor", sa.Text()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )

    # sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("external_session_id", sa.String(256)),
        sa.Column("external_trace_id", sa.String(256)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("metadata", postgresql.JSONB()),
    )
    op.create_index("ix_sessions_project_started", "sessions", ["project_id", "started_at"])
    op.create_index("ix_sessions_agent_started", "sessions", ["agent_id", "started_at"])

    # events
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(256), unique=True, nullable=False),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("session_id", sa.String(256), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("trace_id", sa.String(256)),
        sa.Column("run_id", sa.String(256)),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("extra", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_events_project_ts", "events", ["project_id", "timestamp"])
    op.create_index("ix_events_session_ts", "events", ["session_id", "timestamp"])
    op.create_index("ix_events_trace_ts", "events", ["trace_id", "timestamp"])
    op.create_index("ix_events_type_ts", "events", ["event_type", "timestamp"])

    # raw_event_blobs
    op.create_table(
        "raw_event_blobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("source", "project_id", "external_key", name="uq_raw_blob_key"),
    )
    op.create_index("ix_raw_blobs_project_source", "raw_event_blobs", ["project_id", "source"])
    op.create_index("ix_raw_blobs_hash", "raw_event_blobs", ["payload_hash"])

    # daily_usage_stats
    op.create_table(
        "daily_usage_stats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.String(10), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("model", sa.String(128)),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("date", "project_id", "agent_id", "model", name="uq_daily_usage"),
    )
    op.create_index("ix_daily_usage_project_date", "daily_usage_stats", ["project_id", "date"])


def downgrade() -> None:
    op.drop_table("daily_usage_stats")
    op.drop_table("raw_event_blobs")
    op.drop_table("events")
    op.drop_table("sessions")
    op.drop_table("data_sources")
    op.drop_table("agents")
    op.drop_table("projects")
