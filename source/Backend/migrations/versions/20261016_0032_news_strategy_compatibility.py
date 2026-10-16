"""Add explicit news/strategy compatibility evidence, without backfill."""
from alembic import op
import sqlalchemy as sa

revision = '20261016_0032'
down_revision = '20261010_0031'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('recovery_news_strategy_compatibility',
        sa.Column('scope_key', sa.String(64), sa.ForeignKey('recovery_accounts.scope_key'), primary_key=True),
        sa.Column('predecessor_attempt_id', sa.String(36), sa.ForeignKey('recovery_attempts.attempt_id'), nullable=False),
        sa.Column('predecessor_epoch', sa.BigInteger(), nullable=False),
        sa.Column('original_checkpoint_hash', sa.String(64), nullable=False),
        sa.Column('predecessor_strategy_id', sa.String(64), sa.ForeignKey('saved_strategies.strategy_id'), nullable=False),
        sa.Column('successor_strategy_id', sa.String(64), sa.ForeignKey('saved_strategies.strategy_id'), nullable=False),
        sa.Column('receipt_digest', sa.String(64), nullable=False, unique=True),
        sa.Column('document', sa.JSON(none_as_null=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('predecessor_epoch > 0', name='ck_news_compat_positive_epoch'),
        sa.CheckConstraint('predecessor_strategy_id <> successor_strategy_id', name='ck_news_compat_distinct_strategy'),
        sa.CheckConstraint("CAST(document AS TEXT) <> 'null'", name='ck_news_compat_document_not_null'))


def downgrade():
    op.drop_table('recovery_news_strategy_compatibility')
