"""Future first-start sequencing contract only; not an application startup hook."""
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
    from tests.cutover_startup_guard import check_startup, check_evidence
    check_evidence(Path('startup_recovery/cutover_evidence.py').read_text())
    explicit_tool_modules = {'cutover', 'cutover_cli', 'cutover_service', 'cutover_files',
                             'cutover_manifest', 'cutover_db', 'cutover_validation', 'cutover_evidence'}
    for path in paths:
        if path.stem in explicit_tool_modules: continue
        check_startup(path.read_text(), path.stem)


# Exercise the architecture checker itself: synthetic code must not gain a
# command capability merely by using the approved administrative function name.
READY_RO = [
    ('_ready_authority', 'startup_recovery.cutover_manifest', 'decode_manifest'),
    ('_ready_authority', 'startup_recovery.cutover_evidence', '_verify_coverage'),
    ('relinquish_terminated_ready_owner', 'startup_recovery.cutover_evidence', '_VerificationTree'),
]


def guarded_function(name, statements):
    return 'def '+name+'():\n'+'\n'.join('    '+line for line in statements.splitlines())+'\n'


@pytest.mark.parametrize('function,module,symbol', READY_RO)
def test_ready_guard_allows_only_reviewed_readonly_import_and_call(function, module, symbol):
    from tests.cutover_startup_guard import check_startup
    check_startup(guarded_function(function, f'from {module} import {symbol}\n{symbol}()'), 'owner_handoff')


@pytest.mark.parametrize('function,module,symbol', READY_RO)
@pytest.mark.parametrize('caller', ['bootstrap', 'coordinator', 'server_adapter', 'api',
                                  'worker', 'checkpoint_producer', 'trading'])
def test_ready_guard_rejects_same_capability_in_ordinary_modules(function, module, symbol, caller):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup(guarded_function(function, f'from {module} import {symbol}'), caller)


@pytest.mark.parametrize('function,module,symbol', READY_RO)
@pytest.mark.parametrize('other', ['unreviewed_helper', 'relinquish_terminated_owner', 'startup'])
def test_ready_guard_has_no_module_wide_or_blocked_owner_exception(function, module, symbol, other):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup(guarded_function(other, f'from {module} import {symbol}'), 'owner_handoff')


@pytest.mark.parametrize('function', ['_ready_authority', 'relinquish_terminated_ready_owner'])
@pytest.mark.parametrize('statement', [
    'from startup_recovery.cutover_service import inventory_state',
    'from startup_recovery.cutover_service import copy_state',
    'from startup_recovery.cutover_service import verify_state',
    'from startup_recovery.cutover import verify_state as harmless',
    'import startup_recovery.cutover_service',
    'import startup_recovery.cutover_service as harmless',
    'from startup_recovery import cutover_cli as harmless',
    'from startup_recovery.cutover_evidence import *',
    'from startup_recovery.cutover_files import CopyTarget',
    'from startup_recovery.cutover_files import CopyTarget as harmless',
    'from startup_recovery.checkpoints import write_generation',
    'from startup_recovery.producer import produced_envelope',
    'from startup_recovery.store import acquire_owner as harmless',
    'from startup_recovery.store import begin_attempt',
    'from startup_recovery.migrate import main',
    'from ctrader_connector import execute_trade',
    'from os import chmod',
    'from pathlib import Path as Writer',
    'inventory_state()', 'copy_state()', 'verify_state()', 'CopyTarget(root)',
    'write_generation(root, envelopes, None)', 'produced_envelope()',
    'register_producer()', 'store.acquire_owner(session, token)',
    'store.begin_attempt(session, scope, boot, build)', 'coordinator.start()',
    'command.upgrade(config, "head")', 'broker.cancel_order(order)',
    'root.write_bytes(raw)', 'os.chmod(root, 448)', 'open(root, "w")',
    'importlib.import_module("startup_recovery.cutover_service")',
    '__import__("startup_recovery.cutover_cli")',
    'getattr(store, "acquire_owner")(session, token)',
])
def test_ready_guard_rejects_command_writer_recovery_and_broker_capability(function, statement):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup(guarded_function(function, statement), 'owner_handoff')


@pytest.mark.parametrize('function,module,symbol', READY_RO)
def test_ready_guard_rejects_aliases_and_nested_function_impersonation(function, module, symbol):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup(guarded_function(function, f'from {module} import {symbol} as alias'), 'owner_handoff')
    nested = guarded_function('unreviewed', guarded_function(function, f'from {module} import {symbol}'))
    with pytest.raises(AssertionError): check_startup(nested, 'owner_handoff')


@pytest.mark.parametrize('caller', ['api', 'bootstrap', 'coordinator', 'server_adapter'])
@pytest.mark.parametrize('statement', [
    'from startup_recovery.owner_handoff import relinquish_terminated_ready_owner',
    'from startup_recovery.owner_handoff import relinquish_terminated_ready_owner as ready',
    'relinquish_terminated_ready_owner()',
    'owner_handoff.relinquish_terminated_ready_owner()',
    'importlib.import_module("startup_recovery.owner_handoff")',
])
def test_ordinary_startup_cannot_invoke_administrative_ready_adapter(caller, statement):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError): check_startup(guarded_function('startup', statement), caller)


@pytest.mark.parametrize('caller', ['api', 'bootstrap', 'coordinator', 'server_adapter', 'producer', 'runtime'])
@pytest.mark.parametrize('statement', [
    'import startup_recovery.owner_handoff',
    'import startup_recovery.owner_handoff as handoff',
    'from startup_recovery import owner_handoff',
    'from startup_recovery import owner_handoff as handoff',
    'from startup_recovery.owner_handoff import relinquish_terminated_owner',
    'from startup_recovery.owner_handoff import relinquish_terminated_owner as drain',
    'from startup_recovery.owner_handoff import relinquish_terminated_ready_owner',
    'from startup_recovery.owner_handoff import relinquish_terminated_ready_owner as drain',
    'from startup_recovery.owner_handoff import *',
    'from . import owner_handoff as handoff',
    'from .owner_handoff import relinquish_terminated_owner as drain',
    'from startup_recovery import *',
    'importlib.import_module("startup_recovery.owner_handoff")',
    '__import__("startup_recovery.owner_handoff")',
    'importlib.import_module(".owner_handoff", "startup_recovery")',
    '__import__("startup_recovery", fromlist=["owner_handoff"])',
    'target = "startup_recovery.owner_handoff"\nimportlib.import_module(target)',
    'target = ".owner_handoff"\nimportlib.import_module(target, "startup_recovery")',
    'import startup_recovery.owner_handoff as handoff\nready = handoff.relinquish_terminated_ready_owner\nready()',
    'from startup_recovery import owner_handoff as handoff\nready = handoff.relinquish_terminated_ready_owner\nready()',
    'from startup_recovery import owner_handoff as handoff\ngetattr(handoff, "relinquish_terminated_ready_owner")()',
    'def helper():\n    from startup_recovery import owner_handoff as handoff\n    return handoff.relinquish_terminated_ready_owner\nhelper()()',
])
def test_ordinary_startup_cannot_import_administrative_capability(caller, statement):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup(guarded_function('startup', statement), caller)


@pytest.mark.parametrize('statement', [
    'import startup_recovery.runtime as runtime',
    'from startup_recovery import checkpoints',
    'from startup_recovery.types import RecoveryError',
    'importlib.import_module("startup_recovery.runtime")',
])
def test_ordinary_startup_normal_recovery_imports_remain_allowed(statement):
    from tests.cutover_startup_guard import check_startup
    check_startup(guarded_function('startup', statement), 'api')


def test_administrative_adapter_remains_importable_by_external_operator_and_tests():
    # The architecture gate is a source-scan policy, not an import hook. This
    # imports but NEVER invokes either database-mutating administrative adapter.
    from startup_recovery.owner_handoff import relinquish_terminated_owner, relinquish_terminated_ready_owner
    assert callable(relinquish_terminated_owner)
    assert callable(relinquish_terminated_ready_owner)


@pytest.mark.parametrize('expression,expected', [
    ('"abc"', 'abc'), ('"a" + "b"', 'ab'),
    ('"a" + ("b" + "c")', 'abc'), ('("a" + "b") + "c"', 'abc'),
    ('variable + "x"', None), ('call() + "x"', None),
    ('attribute.value', None), ('f"prefix{value}"', None),
    ('42', None), ('"x" * 3', None), ('f"literal"', 'literal'),
    ('f"a" + "b"', 'ab'), ('"" + ""', ''),
])
def test_static_string_resolves_only_literal_syntax(expression, expected):
    import ast
    from tests.cutover_startup_guard import _static_string
    assert _static_string(ast.parse(expression, mode='eval').body) == expected


@pytest.mark.parametrize('caller', ['api', 'bootstrap', 'coordinator', 'server_adapter'])
@pytest.mark.parametrize('source', [
    'importlib.import_module("startup_recovery." + "owner_handoff")',
    '__import__("startup_recovery." + "owner_handoff")',
    'importlib.import_module("startup_" + "recovery." + "owner_" + "handoff")',
    'importlib.import_module(("startup_recovery." + ("owner_" + "handoff")))',
    'getattr(module, "relinquish_" + "terminated_ready_owner")',
    'importlib.import_module("startup_recovery." + "cutover_service")',
    '__import__("startup_recovery", fromlist=["owner_" + "handoff"])',
    'from importlib import import_module as load\nload("startup_recovery." + "owner_" + "handoff")',
    'import importlib as loader\nloader.import_module("startup_recovery." + "owner_" + "handoff")',
    'fetch = getattr\nfetch(module, "relinquish_" + "terminated_ready_owner")',
    'target = "startup_recovery." + "owner_" + "handoff"\nimportlib.import_module(target)',
    'def helper():\n    return "startup_recovery." + "owner_" + "handoff"\nimportlib.import_module(helper())',
    'importlib.import_module(f"startup_recovery." + f"owner_" + "handoff")',
])
def test_static_capability_concatenation_is_blocked(caller, source):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError): check_startup(source, caller)


@pytest.mark.parametrize('source', [
    'importlib.import_module(prefix + suffix)',
    'importlib.import_module(call() + ".runtime")',
    'name = "unrelated" + "text"',
    'importlib.import_module("startup_recovery." + "runtime")',
    'getattr(module, "normal_" + "operation")',
    'importlib.import_module(f"startup_recovery.{name}")',
])
def test_static_folding_does_not_reject_unrelated_or_unresolved_expressions(source):
    from tests.cutover_startup_guard import check_startup
    check_startup(source, 'api')


@pytest.mark.parametrize('function,module,symbol', [
    ('_transport', 'startup_recovery.cutover_evidence', 'read_original_transport'),
    ('_transport', 'startup_recovery.cutover_evidence', '_destination_preflight'),
    ('adopt_verified_cutover', 'startup_recovery.cutover_evidence', '_VerificationTree'),
    ('adopt_verified_cutover', 'startup_recovery.cutover_manifest', 'CutoverError'),
    ('adopt_verified_cutover', 'startup_recovery.cutover_db', 'read_heads'),
])
def test_existing_legacy_adoption_readonly_permissions_preserved(function, module, symbol):
    from tests.cutover_startup_guard import check_startup
    check_startup(guarded_function(function, f'from {module} import {symbol}\n{symbol}()'), 'legacy_adoption')


@pytest.mark.parametrize('source', [
    'from os import remove as _digest\ndef relinquish_terminated_ready_owner():\n    _digest(root)',
    'require = os.remove\ndef relinquish_terminated_ready_owner():\n    require(root)',
    'def relinquish_terminated_ready_owner():\n    def hidden():\n        os.remove(root)\n    hidden()',
    'class _ready_authority:\n    from startup_recovery.cutover_manifest import decode_manifest',
    'def relinquish_terminated_ready_owner():\n    from startup_recovery.cutover_db import read_heads',
    'def _ready_authority():\n    from startup_recovery.cutover_manifest import CutoverError',
    'def relinquish_terminated_ready_owner():\n    callback = store.acquire_owner',
])
def test_ready_guard_rejects_binding_replacement_hidden_writers_and_unused_permissions(source):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError): check_startup(source, 'owner_handoff')
