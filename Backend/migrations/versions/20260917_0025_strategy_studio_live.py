"""Add Strategy Studio LIVE lifecycle state without enabling LIVE handoff.

Revision ID: 20260917_0025
Revises: 20260917_0024
"""
from alembic import op
import sqlalchemy as sa


revision = "20260917_0025"
down_revision = "20260917_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "strategy_setup_lifecycle",
        sa.Column("setup_id", sa.String(length=96), primary_key=True),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("account_scope", sa.String(length=160), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("definition_snapshot", sa.JSON(), nullable=False),
        sa.Column("initial_volume_units", sa.Integer(), nullable=True),
        sa.Column("broker_position_id", sa.String(length=100), nullable=True),
        sa.Column("tp1_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("protection_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("management_suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_strategy_setup_lifecycle_owner_id",
        "strategy_setup_lifecycle",
        ["owner_id"],
    )
    op.create_index(
        "ix_strategy_setup_lifecycle_strategy_id",
        "strategy_setup_lifecycle",
        ["strategy_id"],
    )
    op.create_index(
        "ix_strategy_setup_lifecycle_account_id",
        "strategy_setup_lifecycle",
        ["account_id"],
    )

    op.create_table(
        "strategy_studio_live_state",
        sa.Column("owner_id", sa.String(length=100), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled_strategy_id", sa.String(length=64), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Submission attempts are now allowed to refer either to an immutable
    # indicator event or to a Strategy Studio setup lifecycle. Indicator-event
    # integrity remains enforced by claim_submission() before any attempt row is
    # created; Studio uses its own atomically locked lifecycle table.
    op.drop_constraint(
        "trade_submission_attempts_event_id_fkey",
        "trade_submission_attempts",
        type_="foreignkey",
    )
    op.add_column(
        "trade_submission_attempts",
        sa.Column(
            "lifecycle_kind",
            sa.String(length=32),
            nullable=False,
            server_default="INDICATOR_EVENT",
        ),
    )


def downgrade():
    bind = op.get_bind()
    studio_attempts = bind.execute(
        sa.text(
            "SELECT count(*) FROM trade_submission_attempts "
            "WHERE lifecycle_kind <> 'INDICATOR_EVENT'"
        )
    ).scalar_one()
    studio_lifecycles = bind.execute(
        sa.text("SELECT count(*) FROM strategy_setup_lifecycle")
    ).scalar_one()
    live_states = bind.execute(
        sa.text("SELECT count(*) FROM strategy_studio_live_state")
    ).scalar_one()
    if studio_attempts or studio_lifecycles or live_states:
        raise RuntimeError(
            "Refusing to downgrade Strategy Studio LIVE lifecycle with durable state present"
        )

    op.drop_column("trade_submission_attempts", "lifecycle_kind")
    op.create_foreign_key(
        "trade_submission_attempts_event_id_fkey",
        "trade_submission_attempts",
        "indicator_events",
        ["event_id"],
        ["event_id"],
    )
    op.drop_table("strategy_studio_live_state")
    op.drop_index(
        "ix_strategy_setup_lifecycle_account_id",
        table_name="strategy_setup_lifecycle",
    )
    op.drop_index(
        "ix_strategy_setup_lifecycle_strategy_id",
        table_name="strategy_setup_lifecycle",
    )
    op.drop_index(
        "ix_strategy_setup_lifecycle_owner_id",
        table_name="strategy_setup_lifecycle",
    )
    op.drop_table("strategy_setup_lifecycle")
