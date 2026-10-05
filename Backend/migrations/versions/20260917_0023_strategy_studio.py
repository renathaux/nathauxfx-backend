"""Add durable Strategy Studio saved definitions and active selection.

Revision ID: 20260917_0023
Revises: 20260916_0022
"""
from alembic import op
import sqlalchemy as sa

revision = "20260917_0023"
down_revision = "20260916_0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "saved_strategies",
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("strategy_id"),
    )
    op.create_index("ix_saved_strategies_owner_id", "saved_strategies", ["owner_id"], unique=False)
    op.create_index(
        "ix_saved_strategy_owner_updated",
        "saved_strategies",
        ["owner_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "strategy_studio_selection",
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["saved_strategies.strategy_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.create_index(
        "ix_strategy_studio_selection_strategy_id",
        "strategy_studio_selection",
        ["strategy_id"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    row_count = bind.execute(sa.text("SELECT COUNT(*) FROM saved_strategies")).scalar_one()
    if row_count:
        raise RuntimeError(
            "Refusing Strategy Studio downgrade while saved_strategies contains rows"
        )

    op.drop_index(
        "ix_strategy_studio_selection_strategy_id",
        table_name="strategy_studio_selection",
    )
    op.drop_table("strategy_studio_selection")
    op.drop_index("ix_saved_strategy_owner_updated", table_name="saved_strategies")
    op.drop_index("ix_saved_strategies_owner_id", table_name="saved_strategies")
    op.drop_table("saved_strategies")
