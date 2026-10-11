"""Add administrator approval state to FlowSignal accounts.

Revision ID: 20260911_0019
Revises: 20260908_0018
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260911_0019"
down_revision: Union[str, None] = "20260908_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("flowsignal_users")}
    if "full_name" not in columns:
        op.add_column("flowsignal_users", sa.Column("full_name", sa.String(160), nullable=True))
    if "approval_status" not in columns:
        op.add_column("flowsignal_users", sa.Column("approval_status", sa.String(24), nullable=True))
    if "reviewed_at" not in columns:
        op.add_column("flowsignal_users", sa.Column("reviewed_at", sa.Float(), nullable=True))
    if "reviewed_by" not in columns:
        op.add_column("flowsignal_users", sa.Column("reviewed_by", sa.String(320), nullable=True))
    op.execute("UPDATE flowsignal_users SET full_name = email WHERE full_name IS NULL")
    op.execute("UPDATE flowsignal_users SET approval_status = 'APPROVED' WHERE approval_status IS NULL")
    op.alter_column("flowsignal_users", "full_name", existing_type=sa.String(160), nullable=False)
    op.alter_column("flowsignal_users", "approval_status", existing_type=sa.String(24), nullable=False)


def downgrade() -> None:
    for column in ("reviewed_by", "reviewed_at", "approval_status", "full_name"):
        op.drop_column("flowsignal_users", column)
