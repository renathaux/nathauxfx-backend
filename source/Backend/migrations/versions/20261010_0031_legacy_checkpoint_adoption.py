"""Add evidence-only legacy adoption receipts without backfilling authority."""
from alembic import op
import sqlalchemy as sa

revision = '20261010_0031'
down_revision = '20261001_0030'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recovery_legacy_adoptions',
        sa.Column('scope_key', sa.String(64), sa.ForeignKey('recovery_accounts.scope_key'), primary_key=True),
        sa.Column('cutover_manifest_hash', sa.String(64), primary_key=True),
        sa.Column('attempt_id', sa.String(36), sa.ForeignKey('recovery_attempts.attempt_id'), nullable=False),
        sa.Column('epoch', sa.BigInteger(), nullable=False),
        sa.Column('boot_id', sa.String(128), nullable=False),
        sa.Column('build_id', sa.String(128), nullable=False),
        sa.Column('handoff_evidence_hash', sa.String(64), nullable=False),
        sa.Column('adoption_policy_version', sa.String(64), nullable=False),
        sa.Column('source_hashes', sa.JSON(none_as_null=True), nullable=False),
        sa.Column('dependency_hashes', sa.JSON(none_as_null=True), nullable=False),
        sa.Column('generation_manifest_hash', sa.String(64), nullable=False),
        sa.Column('envelope_hashes', sa.JSON(none_as_null=True), nullable=False),
        sa.Column('receipt_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('epoch > 0', name='ck_legacy_adoption_positive_epoch'),
        sa.CheckConstraint("CAST(source_hashes AS TEXT) <> 'null' AND CAST(dependency_hashes AS TEXT) <> 'null' AND CAST(envelope_hashes AS TEXT) <> 'null'", name='ck_legacy_adoption_hash_maps_not_null'),
    )


def downgrade():
    op.drop_table('recovery_legacy_adoptions')
