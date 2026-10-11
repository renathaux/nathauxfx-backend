"""Add resumable Fundamental Engine backfill jobs.

Revision ID: 20260808_0005
Revises: 20260807_0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260808_0005"
down_revision: Union[str, None] = "20260807_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "economic_backfill_jobs" in set(inspector.get_table_names()):
        return
    op.create_table(
        "economic_backfill_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_cursor", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunk_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("events_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observations_added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicates_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_economic_backfill_jobs_provider", "economic_backfill_jobs", ["provider"]
    )
    op.create_index(
        "ix_economic_backfill_jobs_status", "economic_backfill_jobs", ["status"]
    )
    op.create_index(
        "ix_economic_backfill_jobs_updated_at", "economic_backfill_jobs", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_table("economic_backfill_jobs")

