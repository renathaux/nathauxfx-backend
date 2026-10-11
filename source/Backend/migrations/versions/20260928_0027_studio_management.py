"""Persist Studio execution snapshots and management intents."""
from alembic import op
import sqlalchemy as sa
revision = "20260928_0027"
down_revision = "20260925_0026"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("strategy_setup_lifecycle", sa.Column("execution_snapshot", sa.JSON(), nullable=True))
    op.add_column("strategy_setup_lifecycle", sa.Column("management_state", sa.JSON(), nullable=True))

    op.add_column("strategy_setup_lifecycle", sa.Column("tp1_requested_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    op.drop_column("strategy_setup_lifecycle", "tp1_requested_at")
    op.drop_column("strategy_setup_lifecycle", "management_state")
    op.drop_column("strategy_setup_lifecycle", "execution_snapshot")
