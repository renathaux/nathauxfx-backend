"""Add encrypted durable cTrader OAuth token storage.

Revision ID: 20260807_0002
Revises: 20260807_0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0002"
down_revision: Union[str, None] = "20260807_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ctrader_oauth_tokens" not in inspector.get_table_names():
        op.create_table(
            "ctrader_oauth_tokens",
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("encrypted_access_token", sa.Text(), nullable=False),
            sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.String(length=255), nullable=False),
            sa.PrimaryKeyConstraint("provider"),
        )
        return

    columns = {
        column["name"] for column in inspector.get_columns("ctrader_oauth_tokens")
    }
    required = {
        "provider",
        "encrypted_access_token",
        "encrypted_refresh_token",
        "updated_at",
        "updated_by",
    }
    missing = required - columns
    if missing:
        raise RuntimeError(
            "Existing ctrader_oauth_tokens table is incompatible; missing: "
            + ", ".join(sorted(missing))
        )


def downgrade() -> None:
    op.drop_table("ctrader_oauth_tokens")
