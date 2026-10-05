"""Add isolated Strategy V2 shadow persistence and execution-risk audit.

Revision ID: 20260813_0008
Revises: 20260811_0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_0008"
down_revision: Union[str, None] = "20260811_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "strategy_shadow_runtime" not in tables:
        op.create_table(
            "strategy_shadow_runtime",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("strategy_version", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state_json", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_strategy_shadow_runtime_updated_at", "strategy_shadow_runtime", ["updated_at"])
        op.create_index(
            "uq_strategy_shadow_runtime_symbol_version",
            "strategy_shadow_runtime", ["symbol", "strategy_version"], unique=True,
        )

    if "strategy_shadow_evaluations" not in tables:
        op.create_table(
            "strategy_shadow_evaluations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("evaluation_key", sa.String(64), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("timeframe", sa.String(8), nullable=False),
            sa.Column("strategy_version", sa.String(64), nullable=False),
            sa.Column("setup_fingerprint", sa.String(128), nullable=True),
            sa.Column("direction", sa.String(8), nullable=True),
            sa.Column("structure_type", sa.String(16), nullable=True),
            sa.Column("bos_level", sa.Float(), nullable=True),
            sa.Column("bos_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("bos_buffer", sa.Float(), nullable=True),
            sa.Column("atr14", sa.Float(), nullable=True),
            sa.Column("ema_state", sa.String(32), nullable=True),
            sa.Column("consolidation_state", sa.String(32), nullable=True),
            sa.Column("m5_confirmation_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reference_price", sa.Float(), nullable=True),
            sa.Column("extension_atr", sa.Float(), nullable=True),
            sa.Column("v1_decision", sa.String(32), nullable=False),
            sa.Column("v1_reason", sa.String(255), nullable=True),
            sa.Column("v2_decision", sa.String(32), nullable=False),
            sa.Column("v2_reason", sa.String(255), nullable=True),
            sa.Column("hypothetical_entry", sa.Float(), nullable=True),
            sa.Column("hypothetical_sl", sa.Float(), nullable=True),
            sa.Column("hypothetical_tp1", sa.Float(), nullable=True),
            sa.Column("hypothetical_tp2", sa.Float(), nullable=True),
            sa.Column("hypothetical_rr", sa.Float(), nullable=True),
            sa.Column("hypothetical_risk_percent", sa.Float(), nullable=True),
            sa.Column("retest_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("continuation_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("setup_expiry", sa.DateTime(timezone=True), nullable=True),
            sa.Column("related_previous_trade_id", sa.Integer(), nullable=True),
            sa.Column("post_sl_reset_state", sa.String(64), nullable=True),
            sa.Column("diagnostics_json", sa.JSON(), nullable=False),
            sa.Column("v1_order_id", sa.String(128), nullable=True),
            sa.Column("v1_position_id", sa.String(128), nullable=True),
            sa.Column("v1_outcome_json", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        for name, columns, unique in (
            ("ix_strategy_shadow_evaluations_evaluation_key", ["evaluation_key"], True),
            ("ix_strategy_shadow_evaluations_evaluated_at", ["evaluated_at"], False),
            ("ix_strategy_shadow_evaluations_symbol", ["symbol"], False),
            ("ix_strategy_shadow_evaluations_strategy_version", ["strategy_version"], False),
            ("ix_strategy_shadow_evaluations_setup_fingerprint", ["setup_fingerprint"], False),
            ("ix_strategy_shadow_evaluations_v1_decision", ["v1_decision"], False),
            ("ix_strategy_shadow_evaluations_v2_decision", ["v2_decision"], False),
            ("ix_strategy_shadow_evaluations_v2_reason", ["v2_reason"], False),
            ("ix_shadow_eval_symbol_time", ["symbol", "evaluated_at"], False),
            ("ix_shadow_eval_comparison", ["symbol", "v1_decision", "v2_decision"], False),
        ):
            op.create_index(name, "strategy_shadow_evaluations", columns, unique=unique)

    if "strategy_shadow_trades" not in tables:
        op.create_table(
            "strategy_shadow_trades",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("shadow_trade_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("strategy_version", sa.String(64), nullable=False),
            sa.Column("setup_fingerprint", sa.String(128), nullable=False),
            sa.Column("direction", sa.String(8), nullable=False),
            sa.Column("entry_timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("entry", sa.Float(), nullable=False),
            sa.Column("sl", sa.Float(), nullable=False),
            sa.Column("tp1", sa.Float(), nullable=False),
            sa.Column("tp2", sa.Float(), nullable=False),
            sa.Column("protected_sl", sa.Float(), nullable=False),
            sa.Column("risk_percent", sa.Float(), nullable=True),
            sa.Column("rr", sa.Float(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("exit_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exit_price", sa.Float(), nullable=True),
            sa.Column("r_result", sa.Float(), nullable=True),
            sa.Column("mae_r", sa.Float(), nullable=False),
            sa.Column("mfe_r", sa.Float(), nullable=False),
            sa.Column("tp1_reached", sa.Boolean(), nullable=False),
            sa.Column("tp2_reached", sa.Boolean(), nullable=False),
            sa.Column("sl_reached", sa.Boolean(), nullable=False),
            sa.Column("last_processed_m5", sa.DateTime(timezone=True), nullable=True),
            sa.Column("related_previous_trade_id", sa.Integer(), nullable=True),
            sa.Column("v1_evaluation_id", sa.Integer(), nullable=True),
            sa.Column("v1_order_id", sa.String(128), nullable=True),
            sa.Column("v1_position_id", sa.String(128), nullable=True),
            sa.Column("v1_outcome_json", sa.JSON(), nullable=True),
            sa.Column("diagnostics_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        for name, columns, unique in (
            ("ix_strategy_shadow_trades_shadow_trade_id", ["shadow_trade_id"], True),
            ("ix_strategy_shadow_trades_symbol", ["symbol"], False),
            ("ix_strategy_shadow_trades_strategy_version", ["strategy_version"], False),
            ("ix_strategy_shadow_trades_entry_timestamp", ["entry_timestamp"], False),
            ("ix_strategy_shadow_trades_status", ["status"], False),
            ("uq_shadow_trade_strategy_setup", ["strategy_version", "setup_fingerprint"], True),
            ("ix_shadow_trade_symbol_status", ["symbol", "status"], False),
        ):
            op.create_index(name, "strategy_shadow_trades", columns, unique=unique)

    if "execution_risk_audits" not in tables:
        op.create_table(
            "execution_risk_audits",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("event_type", sa.String(48), nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("broker_position_id", sa.String(128), nullable=True),
            sa.Column("old_entry", sa.Float(), nullable=True),
            sa.Column("new_entry", sa.Float(), nullable=True),
            sa.Column("old_sl", sa.Float(), nullable=True),
            sa.Column("new_sl", sa.Float(), nullable=True),
            sa.Column("volume_units", sa.Float(), nullable=True),
            sa.Column("approved_risk_amount", sa.Float(), nullable=True),
            sa.Column("resulting_risk_amount", sa.Float(), nullable=True),
            sa.Column("approved_risk_percent", sa.Float(), nullable=True),
            sa.Column("resulting_risk_percent", sa.Float(), nullable=True),
            sa.Column("status", sa.String(48), nullable=False),
            sa.Column("details_json", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        for name, columns in (
            ("ix_execution_risk_audits_timestamp", ["timestamp"]),
            ("ix_execution_risk_audits_symbol", ["symbol"]),
            ("ix_execution_risk_audits_event_type", ["event_type"]),
            ("ix_execution_risk_audits_broker_position_id", ["broker_position_id"]),
            ("ix_execution_risk_audits_status", ["status"]),
        ):
            op.create_index(name, "execution_risk_audits", columns)


def downgrade() -> None:
    op.drop_table("execution_risk_audits")
    op.drop_table("strategy_shadow_trades")
    op.drop_table("strategy_shadow_evaluations")
    op.drop_table("strategy_shadow_runtime")
