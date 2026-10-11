"""Persist account-scoped V3B signal state transitions.

Revision ID: 20260917_0024
Revises: 20260917_0023
"""
from alembic import op
import sqlalchemy as sa


revision = "20260917_0024"
down_revision = "20260917_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v3b_signal_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_scope", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(10), nullable=False),
        sa.Column("signal_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_profile", sa.String(50), nullable=False),
        sa.Column("event_id", sa.String(80)),
        sa.Column("confirmation_id", sa.String(80)),
        sa.Column("setup_id", sa.String(80)),
        sa.Column("signal_creation_state", sa.String(30), nullable=False),
        sa.Column("execution_status", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("confidence", sa.Float()),
        sa.Column("entry", sa.Float()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_scope", "symbol", "ordinal", name="uq_v3b_signal_stream_ordinal"),
    )
    op.create_index("ix_v3b_signal_scope_time", "v3b_signal_transitions", ["account_scope", "signal_timestamp"])


def downgrade():
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM v3b_signal_transitions")).scalar_one()
    if count:
        raise RuntimeError("Refusing to remove persisted V3B signal history")
    op.drop_index("ix_v3b_signal_scope_time", table_name="v3b_signal_transitions")
    op.drop_table("v3b_signal_transitions")
