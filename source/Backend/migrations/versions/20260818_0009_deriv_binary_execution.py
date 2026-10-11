"""Add account-aware Deriv Binary execution persistence.

Revision ID: 20260818_0009
Revises: 20260813_0008
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260818_0009"
down_revision: Union[str, None] = "20260813_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "deriv_binary_accounts" not in tables:
        op.create_table(
            "deriv_binary_accounts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.String(255), nullable=False),
            sa.Column("deriv_account_id", sa.String(255), nullable=False),
            sa.Column("connection_id", sa.String(255)),
            sa.Column("account_type", sa.String(16), nullable=False),
            sa.Column("currency", sa.String(20), nullable=False),
            sa.Column("balance", sa.Float()),
            sa.Column("auth_state", sa.String(32), nullable=False),
            sa.Column("selected", sa.Boolean(), nullable=False),
            sa.Column("binary_auto_enabled", sa.Boolean(), nullable=False),
            sa.Column("binary_stake", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "deriv_account_id", name="uq_deriv_binary_account"),
        )
    if "deriv_binary_executions" not in tables:
        op.create_table(
            "deriv_binary_executions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.String(255), nullable=False),
            sa.Column("deriv_account_id", sa.String(255), nullable=False),
            sa.Column("account_type", sa.String(16), nullable=False),
            sa.Column("strategy_version", sa.String(100), nullable=False),
            sa.Column("rule_hash", sa.String(64), nullable=False),
            sa.Column("signal_id", sa.String(255), nullable=False),
            sa.Column("direction", sa.String(8), nullable=False),
            sa.Column("contract_type", sa.String(8), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("duration", sa.Integer(), nullable=False),
            sa.Column("duration_unit", sa.String(4), nullable=False),
            sa.Column("stake", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(20), nullable=False),
            sa.Column("proposal_id", sa.String(255)), sa.Column("contract_id", sa.String(255)),
            sa.Column("transaction_id", sa.String(255)), sa.Column("buy_price", sa.Float()),
            sa.Column("potential_payout", sa.Float()), sa.Column("purchase_timestamp", sa.Integer()),
            sa.Column("expiry_timestamp", sa.Integer()), sa.Column("broker_status", sa.String(32), nullable=False),
            sa.Column("outcome", sa.String(8)), sa.Column("profit_loss", sa.Float()),
            sa.Column("settlement_payout", sa.Float()), sa.Column("settlement_timestamp", sa.Integer()),
            sa.Column("settlement_price", sa.Float()), sa.Column("broker_payload_json", sa.Text()),
            sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "deriv_account_id", "strategy_version", "signal_id", name="uq_deriv_binary_execution"),
        )


def downgrade() -> None:
    op.drop_table("deriv_binary_executions")
    op.drop_table("deriv_binary_accounts")
