"""Create the initial FlowSignal PostgreSQL schema.

Revision ID: 20260807_0001
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _validate_existing_table(table_name: str, required_columns: set[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
    missing = required_columns - actual_columns
    if missing:
        raise RuntimeError(
            f"Existing {table_name} table is incompatible with FlowSignal; "
            f"missing columns: {', '.join(sorted(missing))}"
        )


def _existing_indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    tables = _existing_tables()
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("hashed_password", sa.String(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        _validate_existing_table(
            "users", {"id", "email", "hashed_password", "is_active"}
        )
    user_indexes = _existing_indexes("users")
    if "ix_users_email" not in user_indexes:
        op.create_index("ix_users_email", "users", ["email"], unique=True)
    if "ix_users_id" not in user_indexes:
        op.create_index("ix_users_id", "users", ["id"], unique=False)

    if "runtime_settings" not in tables:
        op.create_table(
            "runtime_settings",
            sa.Column("setting_name", sa.String(length=100), nullable=False),
            sa.Column("setting_value", sa.String(length=100), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.String(length=255), nullable=False),
            sa.PrimaryKeyConstraint("setting_name"),
        )
    else:
        _validate_existing_table(
            "runtime_settings",
            {"setting_name", "setting_value", "updated_at", "updated_by"},
        )

    if "news_trading_mode_audit" not in tables:
        op.create_table(
            "news_trading_mode_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("previous_mode", sa.String(length=32), nullable=False),
            sa.Column("new_mode", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=False),
            sa.Column("active_broker_account", sa.String(length=100), nullable=True),
            sa.Column("broker_environment", sa.String(length=32), nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("request_source", sa.String(length=100), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        _validate_existing_table(
            "news_trading_mode_audit",
            {
                "id", "previous_mode", "new_mode", "user_id",
                "active_broker_account", "broker_environment", "timestamp",
                "request_source", "success", "failure_reason",
            },
        )

    if "auto_trade_state_audit" not in tables:
        op.create_table(
            "auto_trade_state_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("trading_mode", sa.String(length=16), nullable=False),
            sa.Column("previous_enabled", sa.Boolean(), nullable=False),
            sa.Column("new_enabled", sa.Boolean(), nullable=False),
            sa.Column("updated_by", sa.String(length=255), nullable=False),
            sa.Column("active_broker_account", sa.String(length=100), nullable=True),
            sa.Column("broker_environment", sa.String(length=32), nullable=True),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("request_source", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        _validate_existing_table(
            "auto_trade_state_audit",
            {
                "id", "trading_mode", "previous_enabled", "new_enabled",
                "updated_by", "active_broker_account", "broker_environment",
                "timestamp", "request_source", "reason",
            },
        )


def downgrade() -> None:
    op.drop_table("auto_trade_state_audit")
    op.drop_table("news_trading_mode_audit")
    op.drop_table("runtime_settings")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
