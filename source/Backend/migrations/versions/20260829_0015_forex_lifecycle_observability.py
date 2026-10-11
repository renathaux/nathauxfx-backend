"""Add Forex lifecycle evaluations and immutable execution snapshots.

Revision ID: 20260829_0015
Revises: 20260821_0014
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0015"
down_revision: Union[str, None] = "20260821_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forex_lifecycle_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_evaluation_id", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("deployment_sha", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("account_scope", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(16), nullable=True),
        sa.Column("event_direction", sa.String(8), nullable=True),
        sa.Column("event_close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_broken_level", sa.Float(), nullable=True),
        sa.Column("event_invalidation_swing_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_invalidation_swing_price", sa.Float(), nullable=True),
        sa.Column("event_age_candles", sa.Integer(), nullable=True),
        sa.Column("setup_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("setup_status", sa.String(32), nullable=False),
        sa.Column("setup_invalid_reason", sa.String(255), nullable=True),
        sa.Column("setup_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revival_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("setup_absent_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("setup_last_reappeared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_setup_was_absent_seconds", sa.Float(), nullable=True),
        sa.Column("confirmation_id", sa.String(64), nullable=True),
        sa.Column("confirmation_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_close", sa.Float(), nullable=True),
        sa.Column("confirmation_first_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_age_seconds", sa.Float(), nullable=True),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmation_reused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("new_confirmation_after_revival", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ema_state", sa.String(32), nullable=True),
        sa.Column("entry_candidate_price", sa.Float(), nullable=True),
        sa.Column("displacement_points", sa.Float(), nullable=True),
        sa.Column("rr_at_evaluation", sa.Float(), nullable=True),
        sa.Column("signal_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_block_reason", sa.String(255), nullable=True),
        sa.Column("order_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("order_id", sa.String(128), nullable=True),
        sa.Column("position_id", sa.String(128), nullable=True),
        sa.Column("shadow_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("shadow_policy_results", sa.JSON(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_evaluation_id"),
    )
    op.create_index("ix_forex_lifecycle_evaluations_evaluated_at", "forex_lifecycle_evaluations", ["evaluated_at"])
    op.create_index("ix_forex_lifecycle_evaluations_session_id", "forex_lifecycle_evaluations", ["session_id"])
    op.create_index("ix_forex_lifecycle_evaluations_symbol", "forex_lifecycle_evaluations", ["symbol"])
    op.create_index("ix_forex_lifecycle_evaluations_account_scope", "forex_lifecycle_evaluations", ["account_scope"])
    op.create_index("ix_forex_lifecycle_evaluations_event_id", "forex_lifecycle_evaluations", ["event_id"])
    op.create_index("ix_forex_lifecycle_evaluations_confirmation_id", "forex_lifecycle_evaluations", ["confirmation_id"])
    op.create_index("ix_forex_lifecycle_evaluations_final_block_reason", "forex_lifecycle_evaluations", ["final_block_reason"])
    op.create_index("ix_forex_lifecycle_evaluations_position_id", "forex_lifecycle_evaluations", ["position_id"])
    op.create_index("ix_forex_lifecycle_symbol_event_time", "forex_lifecycle_evaluations", ["symbol", "event_id", "evaluated_at"])
    op.create_index("ix_forex_lifecycle_confirmation_time", "forex_lifecycle_evaluations", ["confirmation_id", "evaluated_at"])
    op.create_index("ix_forex_lifecycle_block_time", "forex_lifecycle_evaluations", ["final_block_reason", "evaluated_at"])

    op.create_table(
        "forex_execution_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("production_sha", sa.String(64), nullable=False),
        sa.Column("backend_session_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=True),
        sa.Column("broker_environment", sa.String(32), nullable=True),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("confirmation_id", sa.String(64), nullable=True),
        sa.Column("order_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("position_id", sa.String(128), nullable=True),
        sa.Column("broker_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id"),
    )
    for name, columns in (
        ("ix_forex_execution_snapshots_snapshot_id", ["snapshot_id"]),
        ("ix_forex_execution_snapshots_backend_session_id", ["backend_session_id"]),
        ("ix_forex_execution_snapshots_symbol", ["symbol"]),
        ("ix_forex_execution_snapshots_account_id", ["account_id"]),
        ("ix_forex_execution_snapshots_event_id", ["event_id"]),
        ("ix_forex_execution_snapshots_confirmation_id", ["confirmation_id"]),
        ("ix_forex_execution_snapshots_order_attempted_at", ["order_attempted_at"]),
        ("ix_forex_execution_snapshots_client_order_id", ["client_order_id"]),
        ("ix_forex_execution_snapshots_broker_order_id", ["broker_order_id"]),
        ("ix_forex_execution_snapshots_position_id", ["position_id"]),
        ("ix_forex_execution_symbol_attempted", ["symbol", "order_attempted_at"]),
    ):
        op.create_index(name, "forex_execution_snapshots", columns)


def downgrade() -> None:
    op.drop_table("forex_execution_snapshots")
    op.drop_table("forex_lifecycle_evaluations")
