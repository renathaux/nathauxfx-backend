"""Add FlowSignal multi-user authentication and Deriv ownership persistence.

Revision ID: 20260818_0010
Revises: 20260818_0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260818_0010"
down_revision: Union[str, None] = "20260818_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "flowsignal_users" not in tables:
        op.create_table(
            "flowsignal_users",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("email_verified", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("last_login_at", sa.Float()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email", name="uq_flowsignal_users_email"),
        )
        op.create_index("ix_flowsignal_users_email", "flowsignal_users", ["email"], unique=True)
    if "flowsignal_sessions" not in tables:
        op.create_table(
            "flowsignal_sessions",
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("csrf_token", sa.String(64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("last_seen_at", sa.Float(), nullable=False),
            sa.Column("revoked_at", sa.Float()),
            sa.PrimaryKeyConstraint("token_hash"),
        )
        op.create_index("ix_flowsignal_sessions_user", "flowsignal_sessions", ["user_id"], unique=False)
        op.create_index("ix_flowsignal_sessions_expires", "flowsignal_sessions", ["expires_at"], unique=False)
    if "flowsignal_deriv_connections" not in tables:
        op.create_table(
            "flowsignal_deriv_connections",
            sa.Column("connection_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("encrypted_access_token", sa.Text(), nullable=False),
            sa.Column("accounts_json", sa.Text(), nullable=False),
            sa.Column("selected_account_id", sa.String(255)),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("disconnected", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("connection_id"),
        )
        op.create_index("ix_flowsignal_deriv_connections_user", "flowsignal_deriv_connections", ["user_id"], unique=False)
    if "flowsignal_deriv_oauth_states" not in tables:
        op.create_table(
            "flowsignal_deriv_oauth_states",
            sa.Column("state_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("verifier_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("consumed_at", sa.Float()),
            sa.PrimaryKeyConstraint("state_hash"),
        )
        op.create_index("ix_flowsignal_deriv_oauth_states_user", "flowsignal_deriv_oauth_states", ["user_id"], unique=False)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("flowsignal_deriv_oauth_states", "flowsignal_deriv_connections", "flowsignal_sessions", "flowsignal_users"):
        if table in tables:
            op.drop_table(table)
