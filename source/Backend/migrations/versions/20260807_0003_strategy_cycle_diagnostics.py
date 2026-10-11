"""Add durable per-cycle strategy diagnostics.

Revision ID: 20260807_0003
Revises: 20260807_0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0003"
down_revision: Union[str, None] = "20260807_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "strategy_cycle_diagnostics" in inspector.get_table_names():
        return
    op.create_table(
        "strategy_cycle_diagnostics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("evaluation_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_closed_m15_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_closed_m5_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("block_reason", sa.String(length=255), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_id"),
    )
    op.create_index("ix_strategy_cycle_diagnostics_cycle_id", "strategy_cycle_diagnostics", ["cycle_id"])
    op.create_index("ix_strategy_cycle_diagnostics_session_id", "strategy_cycle_diagnostics", ["session_id"])
    op.create_index("ix_strategy_cycle_diagnostics_symbol", "strategy_cycle_diagnostics", ["symbol"])
    op.create_index("ix_strategy_cycle_diagnostics_evaluation_timestamp", "strategy_cycle_diagnostics", ["evaluation_timestamp"])
    op.create_index("ix_strategy_cycle_diagnostics_decision", "strategy_cycle_diagnostics", ["decision"])
    op.create_index("ix_strategy_cycle_diagnostics_block_reason", "strategy_cycle_diagnostics", ["block_reason"])
    op.create_index("ix_strategy_cycle_symbol_evaluated", "strategy_cycle_diagnostics", ["symbol", "evaluation_timestamp"])
    op.create_index("ix_strategy_cycle_decision_evaluated", "strategy_cycle_diagnostics", ["decision", "evaluation_timestamp"])


def downgrade() -> None:
    op.drop_table("strategy_cycle_diagnostics")
