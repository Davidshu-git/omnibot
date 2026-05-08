"""add unique constraint on data_sources(project_id, source_type)

Revision ID: 002
Revises: 001
Create Date: 2026-04-19
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Clean up any duplicate rows first, keeping the latest id
    op.execute("""
        DELETE FROM data_sources
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM data_sources
            GROUP BY project_id, source_type
        )
    """)
    op.create_unique_constraint(
        "uq_datasource_project_source",
        "data_sources",
        ["project_id", "source_type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_datasource_project_source", "data_sources", type_="unique")
