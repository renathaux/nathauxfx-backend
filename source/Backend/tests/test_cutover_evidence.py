"""Read-only transport proof never grants adoption or recovery authority."""
import pytest
from tests.cutover_startup_guard import check_startup
from tests.test_legacy_adoption_postgres import db, migrated_database, prepared
from tests.test_cutover_files import snapshot


@pytest.mark.parametrize('source', [
    'from startup_recovery.cutover_files import CopyTarget',
    'from startup_recovery.checkpoints import write_generation',
    'from startup_recovery.store import acquire_owner',
    'import ctrader_connector',
    'tree.install(name, raw)', 'tree._parent(name, create=True)',
    'session.execute(sql)', 'tree.root.unlink()',
])
def test_evidence_boundary_rejects_writer_capabilities(source):
    from tests.cutover_startup_guard import check_evidence
    with pytest.raises(AssertionError):
        check_evidence(source)


@pytest.mark.parametrize('source', [
    'import startup_recovery.cutover',
    'from startup_recovery import cutover',
    'verify_state(path, engine)',
    'from startup_recovery.cutover_service import verify_state as f',
    'inventory_state()', 'copy_state()',
    'run(["python", "-m", "startup_recovery.cutover", "verify"])',
    'from startup_recovery.cutover_evidence import read_original_transport',
])
def test_arbitrary_startup_cannot_access_cutover(source):
    with pytest.raises(AssertionError):
        check_startup(source, 'bootstrap')


def test_only_exact_adoption_call_site_may_consume_original_transport():
    source = ('def _transport():\n'
              '    from startup_recovery.cutover_evidence import read_original_transport\n'
              '    return read_original_transport(tree, path, pin)\n')
    check_startup(source, 'legacy_adoption')
    with pytest.raises(AssertionError):
        check_startup(source.replace('_transport', '_other', 1), 'legacy_adoption')
    with pytest.raises(AssertionError):
        check_startup(source, 'bootstrap')


def test_original_transport_proof_is_readonly_and_grants_nothing(prepared):
    from startup_recovery.cutover_evidence import _VerificationTree, read_original_transport
    from models import RecoveryAccount, RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption, RecoveryMutation
    p = prepared
    models = (RecoveryAccount, RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption, RecoveryMutation)
    def state():
        with p['session_factory']() as session:
            return {m.__tablename__: [{c.name: getattr(row, c.name) for c in m.__table__.columns}
                    for row in session.query(m)] for m in models}
    before_db, before_fs = state(), snapshot(p['state_root'])
    with _VerificationTree(p['state_root']) as tree, tree.shared():
        document, images, expected = read_original_transport(tree, p['manifest_path'], p['expected_manifest_sha256'])
        assert document.digest == p['expected_manifest_sha256']
        assert len(images) == 9  # seven originals, immutable manifest, diagnostic lock
        assert len(expected) == 9
    assert state() == before_db
    assert snapshot(p['state_root']) == before_fs


def test_original_transport_requires_independent_pin(prepared):
    from startup_recovery.cutover_evidence import _VerificationTree, read_original_transport
    from startup_recovery.cutover_manifest import CutoverError
    with _VerificationTree(prepared['state_root']) as tree, tree.shared():
        with pytest.raises(CutoverError, match='MANIFEST_PIN_REQUIRED'):
            read_original_transport(tree, prepared['manifest_path'], None)


def test_ordinary_verify_rejects_orphan_that_only_adoption_can_explain(prepared, monkeypatch):
    from sqlalchemy import event
    from models import RecoveryLegacyAdoption, RecoveryCheckpointHead
    from startup_recovery.legacy_adoption import adopt_verified_cutover
    from startup_recovery.cutover_service import verify_state
    from startup_recovery.cutover_manifest import CutoverError
    from startup_recovery.types import RecoveryError
    p = prepared
    factory = p['session_factory']
    def interrupt(session, context, instances):
        if any(isinstance(row, RecoveryLegacyAdoption) for row in session.new):
            raise RuntimeError('synthetic database failure after durable generation')
    event.listen(factory.class_, 'before_flush', interrupt)
    try:
        with pytest.raises(RecoveryError, match='UNCOMMITTED_ORPHAN'):
            adopt_verified_cutover(**p)
    finally:
        event.remove(factory.class_, 'before_flush', interrupt)
    with factory() as session:
        assert session.query(RecoveryCheckpointHead).count() == 0
        assert session.query(RecoveryLegacyAdoption).count() == 0
    before = snapshot(p['state_root'])
    with pytest.raises(CutoverError, match='DESTINATION_UNKNOWN_STATE'):
        verify_state(p['manifest_path'], factory.kw['bind'], expected_digest=p['expected_manifest_sha256'])
    assert snapshot(p['state_root']) == before
    assert adopt_verified_cutover(**p).status == 'COMMITTED'


def test_original_evidence_never_explains_unrelated_generation(prepared):
    from startup_recovery.legacy_adoption import adopt_verified_cutover
    from startup_recovery.types import RecoveryError
    root = prepared['state_root']/'.recovery-generations'
    root.mkdir(mode=0o700)
    (root/'unexplained').mkdir(mode=0o700)
    before = snapshot(prepared['state_root'])
    with pytest.raises(RecoveryError, match='ORPHAN_CONFLICT'):
        adopt_verified_cutover(**prepared)
    assert snapshot(prepared['state_root']) == before


def test_pristine_adoption_rechecks_reference_snapshot_before_publication(prepared, monkeypatch):
    from dataclasses import replace
    from startup_recovery import cutover_db
    from startup_recovery.legacy_adoption import adopt_verified_cutover
    from startup_recovery.types import RecoveryError
    original = cutover_db.read_heads
    calls = 0
    def drift(engine):
        nonlocal calls
        calls += 1
        value = original(engine)
        return value if calls == 1 else replace(value, digest='f'*64)
    monkeypatch.setattr(cutover_db, 'read_heads', drift)
    before = snapshot(prepared['state_root'])
    with pytest.raises(RecoveryError, match='REFERENCE_CHANGED'):
        adopt_verified_cutover(**prepared)
    assert snapshot(prepared['state_root']) == before
