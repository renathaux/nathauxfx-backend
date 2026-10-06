"""Add durable Deriv settlement recovery lease fields.

Revision ID: 20260820_0012
Revises: 20260820_0011
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "20260820_0012"
down_revision: Union[str, None] = "20260820_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deriv_binary_executions")}
    additions = (
        ("recovery_status", sa.String(32), True, None),
        ("recovery_lease_owner", sa.String(128), True, None),
        ("recovery_lease_expires_at", sa.Float(), True, None),
        ("recovery_attempt_count", sa.Integer(), False, "0"),
        ("recovery_next_retry_at", sa.Float(), True, None),
        ("recovery_error_code", sa.String(64), True, None),
    )
    for name, kind, nullable, default in additions:
        if name not in columns:
            op.add_column("deriv_binary_executions", sa.Column(name, kind, nullable=nullable, server_default=default))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deriv_binary_executions")}
    for name in ("recovery_error_code", "recovery_next_retry_at", "recovery_attempt_count",
                 "recovery_lease_expires_at", "recovery_lease_owner", "recovery_status"):
        if name in columns:
            op.drop_column("deriv_binary_executions", name)
