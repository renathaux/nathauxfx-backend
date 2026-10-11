"""Durable recovery coordination; no legacy authority is backfilled."""
from alembic import op
import sqlalchemy as sa

revision = '20261001_0029'
down_revision = '20261001_0028'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('recovery_accounts',
        sa.Column('scope_key', sa.String(64), primary_key=True),
        sa.Column('broker', sa.String(20), nullable=False),
        sa.Column('environment', sa.String(10), nullable=False),
        sa.Column('account_id', sa.String(100), nullable=False),
        sa.Column('allocated_epoch', sa.BigInteger(), nullable=False),
        sa.Column('owner_attempt_id', sa.String(36)),
        sa.Column('owner_epoch', sa.BigInteger()),
        sa.Column('phase', sa.String(40), nullable=False),
        sa.Column('accepted_manifest_hash', sa.String(64)),
        sa.Column('handoff_evidence', sa.JSON()),
        sa.UniqueConstraint('broker', 'environment', 'account_id', name='uq_recovery_account_scope'))
    op.create_table('recovery_attempts',
        sa.Column('attempt_id', sa.String(36), primary_key=True),
        sa.Column('scope_key', sa.String(64), sa.ForeignKey('recovery_accounts.scope_key'), nullable=False),
        sa.Column('epoch', sa.BigInteger(), nullable=False),
        sa.Column('boot_id', sa.String(128), nullable=False),
        sa.Column('build_id', sa.String(128), nullable=False),
        sa.Column('phase', sa.String(40), nullable=False),
        sa.Column('predecessor_id', sa.String(36)),
        sa.Column('phase_evidence_hash', sa.String(64)),
        sa.Column('conflict_reason', sa.String(100)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('scope_key', 'epoch', name='uq_recovery_scope_epoch'))
    op.create_table('recovery_mutations',
        sa.Column('operation_key', sa.String(220), primary_key=True),
        sa.Column('attempt_id', sa.String(36), sa.ForeignKey('recovery_attempts.attempt_id'), nullable=False),
        sa.Column('scope_key', sa.String(64), sa.ForeignKey('recovery_accounts.scope_key'), nullable=False),
        sa.Column('epoch', sa.BigInteger(), nullable=False),
        sa.Column('operation_kind', sa.String(32), nullable=False),
        sa.Column('intent_hash', sa.String(64), nullable=False),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('trade_submission_attempts.id')),
        sa.Column('setup_id', sa.String(96), sa.ForeignKey('strategy_setup_lifecycle.setup_id')),
        sa.Column('management_intent_id', sa.String(128)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('recovery_checkpoint_heads',
        sa.Column('scope_key', sa.String(64), sa.ForeignKey('recovery_accounts.scope_key'), primary_key=True),
        sa.Column('kind', sa.String(40), primary_key=True),
        sa.Column('manifest_hash', sa.String(64), nullable=False),
        sa.Column('file_hash', sa.String(64), nullable=False),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('identity', sa.JSON(), nullable=False),
        sa.Column('admission_hash', sa.String(64), nullable=False))


def downgrade():
    # Deliberate release action only; application startup never downgrades.
    op.drop_table('recovery_checkpoint_heads')
    op.drop_table('recovery_mutations')
    op.drop_table('recovery_attempts')
    op.drop_table('recovery_accounts')
