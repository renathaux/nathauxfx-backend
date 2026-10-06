"""Add Phase 1 Fundamental Engine persistence.

Revision ID: 20260807_0004
Revises: 20260807_0003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0004"
down_revision: Union[str, None] = "20260807_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "economic_events" not in tables:
        op.create_table(
            "economic_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=128), nullable=False),
            sa.Column("event_name", sa.String(length=255), nullable=False),
            sa.Column("indicator", sa.String(length=128), nullable=False),
            sa.Column("country", sa.String(length=100), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("impact", sa.String(length=16), nullable=False),
            sa.Column("release_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_event_id", sa.String(length=255), nullable=True),
            sa.Column("data_status", sa.String(length=32), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id"),
        )
        op.create_index("ix_economic_events_event_id", "economic_events", ["event_id"])
        op.create_index("ix_economic_events_indicator", "economic_events", ["indicator"])
        op.create_index("ix_economic_events_currency", "economic_events", ["currency"])
        op.create_index("ix_economic_events_release_time", "economic_events", ["release_time"])
        op.create_index("ix_economic_events_provider", "economic_events", ["provider"])
        op.create_index("ix_economic_event_currency_release", "economic_events", ["currency", "release_time"])
        op.create_index("ix_economic_event_indicator_release", "economic_events", ["indicator", "release_time"])

    if "economic_event_observations" not in tables:
        op.create_table(
            "economic_event_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("observation_hash", sa.String(length=64), nullable=False),
            sa.Column("economic_event_id", sa.Integer(), nullable=False),
            sa.Column("actual", sa.String(length=100), nullable=True),
            sa.Column("forecast", sa.String(length=100), nullable=True),
            sa.Column("previous", sa.String(length=100), nullable=True),
            sa.Column("revised_previous", sa.String(length=100), nullable=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("data_status", sa.String(length=32), nullable=False),
            sa.Column("raw_payload", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("observation_hash"),
        )
        op.create_index("ix_economic_event_observations_observation_hash", "economic_event_observations", ["observation_hash"])
        op.create_index("ix_economic_event_observations_economic_event_id", "economic_event_observations", ["economic_event_id"])
        op.create_index("ix_economic_event_observations_provider", "economic_event_observations", ["provider"])
        op.create_index("ix_economic_event_observations_fetched_at", "economic_event_observations", ["fetched_at"])

    if "economic_provider_fetches" not in tables:
        op.create_table(
            "economic_provider_fetches",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("raw_event_count", sa.Integer(), nullable=False),
            sa.Column("normalized_event_count", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_economic_provider_fetches_provider", "economic_provider_fetches", ["provider"])
        op.create_index("ix_economic_provider_fetches_completed_at", "economic_provider_fetches", ["completed_at"])
        op.create_index("ix_economic_provider_fetches_status", "economic_provider_fetches", ["status"])

    if "fundamental_factor_inputs" not in tables:
        op.create_table(
            "fundamental_factor_inputs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("calculation_id", sa.String(length=64), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("factor", sa.String(length=32), nullable=False),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fundamental_factor_inputs_calculation_id", "fundamental_factor_inputs", ["calculation_id"])
        op.create_index("ix_fundamental_factor_inputs_currency", "fundamental_factor_inputs", ["currency"])
        op.create_index("ix_fundamental_factor_inputs_factor", "fundamental_factor_inputs", ["factor"])
        op.create_index("ix_fundamental_factor_inputs_calculated_at", "fundamental_factor_inputs", ["calculated_at"])

    if "currency_strength_snapshots" not in tables:
        op.create_table(
            "currency_strength_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("calculation_id", sa.String(length=64), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("coverage", sa.Float(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("factors", sa.JSON(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("snapshot_id"),
        )
        op.create_index("ix_currency_strength_snapshots_snapshot_id", "currency_strength_snapshots", ["snapshot_id"])
        op.create_index("ix_currency_strength_snapshots_calculation_id", "currency_strength_snapshots", ["calculation_id"])
        op.create_index("ix_currency_strength_snapshots_currency", "currency_strength_snapshots", ["currency"])
        op.create_index("ix_currency_strength_snapshots_calculated_at", "currency_strength_snapshots", ["calculated_at"])

    if "fundamental_insight_snapshots" not in tables:
        op.create_table(
            "fundamental_insight_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("insight_id", sa.String(length=64), nullable=False),
            sa.Column("calculation_id", sa.String(length=64), nullable=False),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("pair_score", sa.Float(), nullable=True),
            sa.Column("direction", sa.String(length=16), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("response_json", sa.JSON(), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("insight_id"),
        )
        op.create_index("ix_fundamental_insight_snapshots_insight_id", "fundamental_insight_snapshots", ["insight_id"])
        op.create_index("ix_fundamental_insight_snapshots_calculation_id", "fundamental_insight_snapshots", ["calculation_id"])
        op.create_index("ix_fundamental_insight_snapshots_symbol", "fundamental_insight_snapshots", ["symbol"])
        op.create_index("ix_fundamental_insight_snapshots_generated_at", "fundamental_insight_snapshots", ["generated_at"])


def downgrade() -> None:
    op.drop_table("fundamental_insight_snapshots")
    op.drop_table("currency_strength_snapshots")
    op.drop_table("fundamental_factor_inputs")
    op.drop_table("economic_provider_fetches")
    op.drop_table("economic_event_observations")
    op.drop_table("economic_events")
