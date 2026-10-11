"""Add official-provider links and disagreement records.

Revision ID: 20260808_0006
Revises: 20260808_0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260808_0006"
down_revision: Union[str, None] = "20260808_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "economic_event_provider_links" not in tables:
        op.create_table(
            "economic_event_provider_links",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("economic_event_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_dataset", sa.String(length=64), nullable=False),
            sa.Column("provider_event_id", sa.String(length=255), nullable=True),
            sa.Column("provider_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("reported_event_name", sa.String(length=255), nullable=False),
            sa.Column("reported_indicator", sa.String(length=128), nullable=False),
            sa.Column("reported_release_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reported_impact", sa.String(length=16), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["economic_event_id"], ["economic_events.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_economic_event_provider_links_economic_event_id",
            "economic_event_provider_links", ["economic_event_id"],
        )
        op.create_index(
            "ix_economic_event_provider_links_provider",
            "economic_event_provider_links", ["provider"],
        )
        op.create_index(
            "ix_economic_event_provider_links_provider_fingerprint",
            "economic_event_provider_links", ["provider_fingerprint"], unique=True,
        )
        op.create_index(
            "ix_economic_provider_link_identity",
            "economic_event_provider_links",
            ["provider", "provider_dataset", "provider_event_id"],
        )

    if "economic_event_disagreements" not in tables:
        op.create_table(
            "economic_event_disagreements",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("disagreement_hash", sa.String(length=64), nullable=False),
            sa.Column("economic_event_id", sa.Integer(), nullable=False),
            sa.Column("field_name", sa.String(length=40), nullable=False),
            sa.Column("authoritative_provider", sa.String(length=32), nullable=False),
            sa.Column("authoritative_value", sa.Text(), nullable=False),
            sa.Column("conflicting_provider", sa.String(length=32), nullable=False),
            sa.Column("conflicting_value", sa.Text(), nullable=False),
            sa.Column("rule_version", sa.String(length=24), nullable=False),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["economic_event_id"], ["economic_events.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_economic_event_disagreements_disagreement_hash",
            "economic_event_disagreements", ["disagreement_hash"], unique=True,
        )
        op.create_index(
            "ix_economic_event_disagreements_economic_event_id",
            "economic_event_disagreements", ["economic_event_id"],
        )
        op.create_index(
            "ix_economic_event_disagreements_field_name",
            "economic_event_disagreements", ["field_name"],
        )
        op.create_index(
            "ix_economic_event_disagreements_detected_at",
            "economic_event_disagreements", ["detected_at"],
        )


def downgrade() -> None:
    op.drop_table("economic_event_disagreements")
    op.drop_table("economic_event_provider_links")
