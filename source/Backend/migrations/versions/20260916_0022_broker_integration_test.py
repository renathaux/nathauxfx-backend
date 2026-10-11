"""Dedicated durable broker integration test identity and account fence."""
from alembic import op
import sqlalchemy as sa

revision = '20260916_0022'
down_revision = '20260912_0021'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("broker_integration_test_submissions",
        sa.Column('test_id', sa.String(80), primary_key=True),
        sa.Column('account_id', sa.String(100), nullable=False),
        sa.Column('unresolved_account', sa.String(100), unique=True),
        sa.Column('environment', sa.String(10), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('symbol_id', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(10), nullable=False),
        sa.Column('volume', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(50), unique=True, nullable=False),
        sa.Column('state', sa.String(40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('request_started_at', sa.DateTime(timezone=True)),
        sa.Column('close_started_at', sa.DateTime(timezone=True)),
        sa.Column('reconciled_at', sa.DateTime(timezone=True)),
        sa.Column('broker_order_id', sa.String(100)),
        sa.Column('broker_position_id', sa.String(100)),
        sa.Column('preflight_evidence', sa.JSON(), nullable=False),
        sa.Column('fill_price', sa.Float()),
        sa.Column('broker_opened_at', sa.DateTime(timezone=True)),
        sa.Column('broker_closed_at', sa.DateTime(timezone=True)),
        sa.Column('open_evidence', sa.JSON()),
        sa.Column('duplicate_evidence', sa.JSON()),
        sa.Column('reconciliation_evidence', sa.JSON()),
        sa.Column('last_error', sa.String(100)),
    )


def downgrade():
    count = op.get_bind().execute(sa.text(
        'SELECT count(*) FROM broker_integration_test_submissions WHERE unresolved_account IS NOT NULL')).scalar()
    if count:
        raise RuntimeError('Cannot remove unresolved broker test fence')
    op.drop_table('broker_integration_test_submissions')
