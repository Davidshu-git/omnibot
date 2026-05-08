"""truncate events and raw_event_blobs for external_key scheme migration

The external_key for raw blobs and event_id for events changed from
path:lineno (fragile) to mhxy:{session_id}:{content_hash} (stable).
Both tables are pure derived data — fully regenerable from source JSONL files.
The startup ingest will repopulate them with the new key scheme.

Revision ID: 003
Revises: 002
Create Date: 2026-04-19
"""
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("TRUNCATE TABLE events RESTART IDENTITY CASCADE")
    op.execute("TRUNCATE TABLE raw_event_blobs RESTART IDENTITY CASCADE")
    # Reset sync cursors so startup ingest re-processes all files with new keys
    op.execute("UPDATE data_sources SET last_sync_cursor = NULL")


def downgrade() -> None:
    # Derived data — cannot restore; downgrade is intentionally a no-op
    pass
