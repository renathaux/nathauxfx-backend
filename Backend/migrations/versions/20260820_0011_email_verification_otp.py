"""Add customer email verification OTP storage.

Revision ID: 20260820_0011
Revises: 20260818_0010
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260820_0011"
down_revision: Union[str, None] = "20260818_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "flowsignal_email_verification_codes" not in tables:
        op.create_table(
            "flowsignal_email_verification_codes",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("code_hash", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("consumed_at", sa.Float()),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_flowsignal_email_verification_codes_user",
            "flowsignal_email_verification_codes",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_flowsignal_email_verification_codes_expires",
            "flowsignal_email_verification_codes",
            ["expires_at"],
            unique=False,
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "flowsignal_email_verification_codes" in tables:
        op.drop_table("flowsignal_email_verification_codes")
