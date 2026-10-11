"""Add append-only Strategy Settings audit history.

Revision ID: 20260811_0007
Revises: 20260808_0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260811_0007"
down_revision: Union[str, None] = "20260808_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "strategy_setting_audit" in set(inspector.get_table_names()):
        return
    op.create_table(
        "strategy_setting_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("setting_name", sa.String(length=100), nullable=False),
        sa.Column("previous_value", sa.String(length=100), nullable=False),
        sa.Column("new_value", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_strategy_setting_audit_setting_name",
        "strategy_setting_audit",
        ["setting_name"],
    )
    op.create_index(
        "ix_strategy_setting_audit_updated_at",
        "strategy_setting_audit",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_table("strategy_setting_audit")
