"""Original-ledger accepted execution; NULL legacy identities are never backfilled."""
from alembic import op
import sqlalchemy as sa

revision = '20261001_0030'
down_revision = '20261001_0029'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('execution_snapshot', 'accepted_execution', 'initial_protection', 'send_intent'):
        op.add_column('trade_submission_attempts', sa.Column(name, sa.JSON(), nullable=True))
    op.add_column('trade_submission_attempts', sa.Column('accepted_execution_hash', sa.String(64), nullable=True))
    from services.submission_reservation import install_reservation_guard
    install_reservation_guard(op.get_bind())


def downgrade():
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        connection.execute(sa.text('DROP TRIGGER IF EXISTS saved_execution_reservation ON saved_strategies'))
        connection.execute(sa.text('DROP FUNCTION IF EXISTS guard_saved_execution()'))
    else:
        for action in ('update','delete'):
            connection.execute(sa.text('DROP TRIGGER IF EXISTS saved_execution_'+action))
    for name in ('accepted_execution_hash','send_intent','initial_protection','accepted_execution','execution_snapshot'):
        op.drop_column('trade_submission_attempts', name)
