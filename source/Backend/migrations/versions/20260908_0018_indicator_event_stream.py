"""Persist the authoritative indicator event stream.

Revision ID: 20260908_0018
Revises: 20260903_0017
"""
from alembic import op
import sqlalchemy as sa


revision = "20260908_0018"
down_revision = "20260903_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "indicator_candles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("candle_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Float(), nullable=False),
        sa.Column("high_price", sa.Float(), nullable=False),
        sa.Column("low_price", sa.Float(), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol", "timeframe", "candle_timestamp",
            name="uq_indicator_candle_stream_time",
        ),
    )
    op.create_index("ix_indicator_candles_symbol", "indicator_candles", ["symbol"])
    op.create_index("ix_indicator_candles_timeframe", "indicator_candles", ["timeframe"])
    op.create_index("ix_indicator_candles_candle_timestamp", "indicator_candles", ["candle_timestamp"])

    op.create_table(
        "indicator_events",
        sa.Column("event_id", sa.String(length=80), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("candle_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", sa.String(length=10), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("broken_level", sa.Float(), nullable=False),
        sa.Column("opposite_swing", sa.JSON(), nullable=True),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("configuration_version", sa.String(length=50), nullable=False),
        sa.Column("is_historical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_indicator_events_symbol", "indicator_events", ["symbol"])
    op.create_index("ix_indicator_events_timeframe", "indicator_events", ["timeframe"])
    op.create_index("ix_indicator_events_candle_timestamp", "indicator_events", ["candle_timestamp"])

    op.create_table(
        "indicator_stream_state",
        sa.Column("symbol", sa.String(length=20), primary_key=True),
        sa.Column("timeframe", sa.String(length=10), primary_key=True),
        sa.Column("configuration_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="INITIALIZING"),
        sa.Column("origin_candle", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activation_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_processed_candle", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "indicator_event_lifecycle",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=80), sa.ForeignKey("indicator_events.event_id"), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("blocking_reason", sa.String(length=255), nullable=True),
        sa.Column("m5_confirmation_id", sa.String(length=80), nullable=True),
        sa.Column("m5_confirmation_identity", sa.JSON(), nullable=True),
        sa.Column("signal_setup_id", sa.String(length=80), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", "mode", "owner_id", "account_id", name="uq_indicator_event_lifecycle_scope"),
    )
    op.create_index("ix_indicator_event_lifecycle_event_id", "indicator_event_lifecycle", ["event_id"])

    op.create_table(
        "trade_submission_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=80), sa.ForeignKey("indicator_events.event_id"), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("signal_setup_id", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("attempt_status", sa.String(length=40), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("broker_request_id", sa.String(length=100), nullable=True),
        sa.Column("broker_client_order_id", sa.String(length=50), nullable=False),
        sa.Column("request_payload_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("broker_order_id", sa.String(length=100), nullable=True),
        sa.Column("broker_position_id", sa.String(length=100), nullable=True),
        sa.Column("broker_response", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reconciliation_status", sa.String(length=40), nullable=False, server_default="NOT_REQUIRED"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_trade_submission_idempotency"),
        sa.UniqueConstraint("event_id", "mode", "owner_id", "account_id", "symbol", "signal_setup_id", name="uq_trade_submission_setup_account"),
    )
    op.create_index("ix_trade_submission_attempts_event_id", "trade_submission_attempts", ["event_id"])

    op.create_table(
        "execution_protocol_state",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("protocol_version", sa.String(length=50), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "INSERT INTO execution_protocol_state (singleton_id, protocol_version, updated_at) "
        "VALUES (1, 'indicator-event-execution-v2', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_table("execution_protocol_state")
    op.drop_index("ix_trade_submission_attempts_event_id", table_name="trade_submission_attempts")
    op.drop_table("trade_submission_attempts")
    op.drop_index("ix_indicator_event_lifecycle_event_id", table_name="indicator_event_lifecycle")
    op.drop_table("indicator_event_lifecycle")
    op.drop_table("indicator_stream_state")
    op.drop_index("ix_indicator_events_candle_timestamp", table_name="indicator_events")
    op.drop_index("ix_indicator_events_timeframe", table_name="indicator_events")
    op.drop_index("ix_indicator_events_symbol", table_name="indicator_events")
    op.drop_table("indicator_events")
    op.drop_index("ix_indicator_candles_candle_timestamp", table_name="indicator_candles")
    op.drop_index("ix_indicator_candles_timeframe", table_name="indicator_candles")
    op.drop_index("ix_indicator_candles_symbol", table_name="indicator_candles")
    op.drop_table("indicator_candles")
