"""Remove retired Binary/Deriv persistence.

Revision ID: 20260903_0016
Revises: 20260829_0015
"""
from alembic import op
import sqlalchemy as sa

revision = "20260903_0016"
down_revision = "20260829_0015"
branch_labels = None
depends_on = None

_TABLES = (
    "deriv_binary_signal_claims",
    "deriv_binary_executions",
    "deriv_binary_accounts",
    "deriv_v5_relay_signals",
    "flowsignal_deriv_oauth_states",
    "flowsignal_deriv_connections",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in _TABLES:
        if table_name in existing:
            op.drop_table(table_name)


def downgrade() -> None:
    raise RuntimeError("Binary/Deriv removal is intentionally irreversible.")
