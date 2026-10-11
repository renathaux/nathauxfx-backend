"""Version-bound new entries; deliberately do not backfill legacy setups."""
from alembic import op
import sqlalchemy as sa

revision = '20261001_0028'
down_revision = '20260928_0027'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('strategy_setup_lifecycle', sa.Column('entry_binding', sa.JSON(), nullable=True))
    op.add_column('trade_submission_attempts', sa.Column('strategy_identity', sa.JSON(), nullable=True))
    op.add_column('trade_submission_attempts', sa.Column('frozen_plan_hash', sa.String(64), nullable=True))

def downgrade():
    op.drop_column('trade_submission_attempts', 'frozen_plan_hash')
    op.drop_column('trade_submission_attempts', 'strategy_identity')
    op.drop_column('strategy_setup_lifecycle', 'entry_binding')
