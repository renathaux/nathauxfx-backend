"""Persist short-lived owner Forex sessions across backend deploys.

Revision ID: 20260903_0017
Revises: 20260903_0016
"""
from alembic import op
import sqlalchemy as sa

revision = "20260903_0017"
down_revision = "20260903_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flowsignal_owner_sessions",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_flowsignal_owner_sessions_expires_at",
        "flowsignal_owner_sessions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_flowsignal_owner_sessions_expires_at",
        table_name="flowsignal_owner_sessions",
    )
    op.drop_table("flowsignal_owner_sessions")
