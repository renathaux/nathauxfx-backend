"""Future first-start sequencing contract only; not an application startup hook."""
import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import sessionmaker
from tests.cutover_fixture import pg, native_inventory, snapshot_db
from tests.test_cutover_cli import cli, transport


def _verified_transport(n,pg):
    import json
    pin,manifest=transport(n,pg)
    result=cli(['verify','--manifest',manifest,'--manifest-sha256',pin],pg)
    report=json.loads(result.stdout)
    assert result.returncode==2 and report['copy_verified'] and not report['ready_for_cutover']
    return pin,manifest


def test_first_start_transport_never_grants_owner_epoch_or_entries(pg,native_inventory):
    from startup_recovery import store
    from startup_recovery.types import AccountScope, RecoveryError
    from startup_recovery.runtime import manager_context
    from startup_recovery.admission import dispatch_claimed_order
    factory=sessionmaker(pg[0],expire_on_commit=False)
    # Real fixture admission boundary creates an ATTEMPT before cutover; no owner.
    with factory.begin() as s:
        # The shared read-only fixture seeds epoch 1 without exercising its
        # allocator. Align that synthetic counter before calling the real
        # allocating boundary; this does not grant ownership or readiness.
        store.account_state(s,AccountScope('ctrader','demo','synthetic-a')).allocated_epoch=1
        s.flush()
        token=store.begin_attempt(s,AccountScope('ctrader','demo','synthetic-a'),'test-first-start','a'*40)
    before=snapshot_db(pg[0],pg[1])
    _verified_transport(native_inventory,pg)
    assert snapshot_db(pg[0],pg[1])==before  # includes epochs, handoff, accepted snapshots, claims
    with factory.begin() as s:
        state=store.account_state(s,token.scope)
        assert state.phase=='LEGACY_CUTOVER_REQUIRED' and state.owner_attempt_id is None and state.owner_epoch is None
        assert not state.handoff_evidence and not state.accepted_manifest_hash
        assert not store.entries_ready(s,token)
        with pytest.raises(RecoveryError,match='LEGACY_CUTOVER_REQUIRED'): store.acquire_owner(s,token)
    broker=Mock(name='NEW_ORDER')
    with manager_context(token),pytest.raises(RecoveryError,match='RECOVERY_TOKEN_STALE'):
        dispatch_claimed_order(factory,'synthetic-only',broker)
    broker.assert_not_called()
    assert snapshot_db(pg[0],pg[1])==before


def test_first_start_management_fixture_distinct_from_transport_and_entries(pg,native_inventory):
    from startup_recovery import store
    from startup_recovery.types import AccountScope, HandoffEvidence, RecoveryError
    from startup_recovery.admission import entry_gate
    factory=sessionmaker(pg[0],expire_on_commit=False)
    with factory.begin() as s:
        token=store.begin_attempt(s,AccountScope('ctrader','demo','separately-admitted'),'management-boot','a'*40)
        store.establish_legacy_cutover(s,token.scope,HandoffEvidence('operator-termination','b'*64,'synthetic-prior',True,True))
        store.acquire_owner(s,token)
        phases=['BOOTSTRAP','DB_READY','BROKER_AUTHENTICATED','STATE_DISCOVERED','STATE_RECONCILED','POSITION_MANAGEMENT_READY']
        for old,new in zip(phases,phases[1:]): store.advance(s,token,old,new,'c'*64)
    before=snapshot_db(pg[0],pg[1])
    _verified_transport(native_inventory,pg)
    assert snapshot_db(pg[0],pg[1])==before
    with factory.begin() as s:
        account,_=store.require_owner(s,token)
        assert account.phase=='POSITION_MANAGEMENT_READY' and account.accepted_manifest_hash=='c'*64
        assert not store.entries_ready(s,token)
        with pytest.raises(RecoveryError,match='LIVE_RECOVERY_INCOMPLETE'): entry_gate(s,token)
    assert snapshot_db(pg[0],pg[1])==before


def test_first_start_failed_verification_stops_before_next_recovery_step(pg,native_inventory):
    import json
    n=native_inventory; pin,manifest=transport(n,pg)
    (n['state']/'live_backup.json').write_bytes(b'corrupted')
    before=snapshot_db(pg[0],pg[1]); next_recovery=Mock(name='existing_recovery_boundary')
    result=cli(['verify','--manifest',manifest,'--manifest-sha256',pin],pg)
    report=json.loads(result.stdout)
    # Test-only sequencing contract. No production callback/hook is added.
    if report['copy_verified'] and not report['blockers']: next_recovery()
    assert result.returncode==2 and not report['copy_verified'] and report['blockers']
    next_recovery.assert_not_called()
    assert snapshot_db(pg[0],pg[1])==before


def test_ordinary_startup_has_no_cutover_dependency_or_invocation():
    # Architectural dependency gate, not SQL/text formatting: inspect actual
    # import/call ASTs of the existing startup integration, including lazy paths.
    paths=[Path('api.py'),*Path('startup_recovery').glob('*.py')]
    for path in paths:
        if path.stem.startswith('cutover'): continue
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): assert not any('cutover' in x.name for x in node.names)
            if isinstance(node,ast.ImportFrom):
                assert 'cutover' not in (node.module or '')
                assert not any('cutover' in x.name for x in node.names)
            if isinstance(node,ast.Call):
                assert getattr(node.func,'id',getattr(node.func,'attr','')) not in {'inventory_state','copy_state','verify_state'}
