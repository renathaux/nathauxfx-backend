"""Add account-level Deriv Binary signal claims.

Revision ID: 20260821_0013
Revises: 20260820_0012
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260821_0013"
down_revision: Union[str, None] = "20260820_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "deriv_binary_signal_claims" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "deriv_binary_signal_claims",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("deriv_account_id", sa.String(255), nullable=False),
            sa.Column("strategy_version", sa.String(100), nullable=False),
            sa.Column("signal_id", sa.String(255), nullable=False),
            sa.Column("user_id", sa.String(255), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.UniqueConstraint("deriv_account_id", "strategy_version", "signal_id",
                                name="uq_deriv_binary_account_signal_claim"),
        )


def downgrade() -> None:
    if "deriv_binary_signal_claims" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("deriv_binary_signal_claims")
