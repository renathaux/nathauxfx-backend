"""Local process/DB recovery proofs. Every broker action is a fixture-only spy."""
import copy
import os
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_recovery_store import store_api, db, begin, cutover


def _worker(dsn, schema, channel, boot):
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn
    assert schema.startswith('recovery_') and schema[9:].isalnum()
    from startup_recovery import store, coordinator
    from startup_recovery.admission import mutation_guard
    from startup_recovery.types import AccountScope, HandoffEvidence, RecoveryError
    from test_recovery_reconciliation import empty
    from types import SimpleNamespace
    engine = create_engine(dsn).execution_options(schema_translate_map={None: schema})
    factory = sessionmaker(engine, expire_on_commit=False)
    scope = AccountScope('ctrader', 'demo', '123')
    token, sends, discoveries = None, [], []
    database, broker = empty()
    database['selection'].update(account_id='123')
    broker.update(account_id='123')
    def broker_read(_):
        discoveries.append('broker-read')
        return copy.deepcopy(broker)
    dependencies = SimpleNamespace(session_factory=factory,
        verify_database=lambda: {'schema': 'fixture'},
        readers=SimpleNamespace(database=lambda _: copy.deepcopy(database), broker=broker_read,
            authenticate=lambda _: dict(account_id='123', environment='demo', authenticated=True)),
        checkpoints=lambda _: {}, publish_reconciled=lambda *_: None,
        start_management=lambda _: None, start_entry_evaluation=lambda _: None)
    try:
        while True:
            command = channel.recv()
            try:
                if command == 'stop': return
                if command == 'recover':
                    outcome = coordinator.recover(dependencies, scope, boot, 'a' * 40)
                    token = outcome.token
                    result = dict(ready=outcome.ready, reason=outcome.reason,
                        epoch=token.epoch, discoveries=len(discoveries), phases=outcome.phases)
                elif command == 'disconnect':
                    engine.dispose()
                    result = {'disconnected': True}
                elif command == 'drain':
                    with factory.begin() as session:
                        store.relinquish(session, token, HandoffEvidence(
                            'graceful-drain', 'c' * 64, token.boot_id, True, True))
                    result = {'drained': True}
                elif command == 'delayed-mutation':
                    with mutation_guard(factory, token, 'NEW_ORDER', 'd' * 64, submission_id=1):
                        sends.append('mock-broker-submit')
                    result = {'sent': len(sends)}
                else:
                    raise AssertionError('unknown fixture command')
                channel.send(result)
            except RecoveryError as exc:
                channel.send({'reason': exc.code, 'sent': len(sends)})
    finally:
        engine.dispose()
        channel.close()


def test_two_process_overlap_disconnect_drain_and_delayed_old_mutation(store_api, db):
    seed = begin(store_api, db)
    cutover(store_api, db, seed)
    schema = db.kw['bind'].get_execution_options()['schema_translate_map'][None]
    workers = []
    def exchange(process, message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 15)[0], 'local fixture worker did not respond'
        line = process.stdout.readline()
        assert line, process.stderr.read()
        return json.loads(line)
    try:
        for boot in ('old-process', 'new-process'):
            process = subprocess.Popen([sys.executable, '-B',
                str(Path(__file__).with_name('recovery_process_probe.py')), schema, boot],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            workers.append(process)
        old, new = workers
        first = exchange(old, 'recover')
        assert first['ready'] and first['discoveries'] == 2
        overlap = exchange(new, 'recover')
        assert not overlap['ready'] and overlap['reason'] == 'RECOVERY_OWNER_BUSY'
        assert overlap['discoveries'] == 0
        assert exchange(old, 'disconnect') == {'disconnected': True}
        still_blocked = exchange(new, 'recover')
        assert still_blocked['reason'] == 'RECOVERY_OWNER_BUSY'
        assert exchange(old, 'drain') == {'drained': True}
        successor = exchange(new, 'recover')
        assert successor['ready'] and successor['epoch'] > first['epoch']
        assert successor['discoveries'] == 2
        assert successor['phases'][-1] == 'NEW_ENTRIES_READY'
        assert exchange(old, 'delayed-mutation') == {'reason': 'RECOVERY_TOKEN_STALE', 'sent': 0}
    finally:
        for process in workers:
            if process.poll() is None:
                process.stdin.write('"stop"\n')
                process.stdin.flush()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(5)
            assert process.returncode == 0, process.stderr.read()
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()


@pytest.mark.parametrize('has_position', [False, True])
def test_real_coordinator_publication_to_existing_manager_preserves_intent(
        store_api, db, tmp_path, monkeypatch, has_position):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from sqlalchemy import text
    import api
    import app_bootstrap
    from fundamentals import ingestion
    from strategies import shared
    from models import Base, RuntimeSetting, StrategySetupLifecycle, StrategyStudioLiveState, ExecutionProtocolState
    from services import strategy_studio_position_manager as manager
    from services.strategy_studio_models import SavedStrategy
    from services.trade_submission_service import EXECUTION_PROTOCOL_VERSION
    from live_integrity.binding import fingerprint
    from startup_recovery import server_adapter, coordinator
    from startup_recovery.runtime import caller_token
    from test_recovery_startup import prepare_cutover
    from test_recovery_reconciliation import SCOPE, empty
    from test_recovery_open_position import occupied
    from test_strategy_studio_position_manager import seed_lifecycle, definition, open_position, prices
    from ctrader_account_context import AccountIdentity
    from recovery_fixture import admitted_manager

    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    preserved_definition = definition()
    _, broker = occupied() if has_position else empty()
    broker['history_from'] = 1000
    broker['history_to'] = broker['observed_at']
    execution, _ = occupied()
    binding = copy.deepcopy(execution['lifecycles'][0]['entry_binding'])
    binding['strategy_identity'].update(owner_id='owner-1', strategy_id='strat-live',
        config_hash=fingerprint(preserved_definition))
    def seed(factory):
        seed_lifecycle(factory, account_id='7', account_scope='CTRADER:DEMO:7',
            broker_position_id='42', strategy_definition=copy.deepcopy(preserved_definition))
        with factory.begin() as session:
            row = session.get(StrategySetupLifecycle, 'setup-1')
            row.entry_binding = copy.deepcopy(binding)
            row.execution_snapshot = copy.deepcopy(execution['lifecycles'][0]['execution_snapshot'])
    calls = []
    monkeypatch.setattr(manager, 'close_position', lambda position_id, volume=None:
        calls.append(dict(operation='close', position_id=position_id, volume=volume)) or {'ok': True})
    monkeypatch.setattr(manager, 'modify_position_stop_loss', lambda *a, **kw:
        pytest.fail('this fixture has not yet confirmed the partial close'))
    account = AccountIdentity('7', 'demo')
    if has_position:
        baseline_engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(baseline_engine)
        baseline_factory = sessionmaker(baseline_engine, expire_on_commit=False)
        seed(baseline_factory)
        with admitted_manager(baseline_factory, '7'):
            manager.manage_selected_account_positions('owner-1', account,
                [open_position(position_id='42')], prices(), session_factory=baseline_factory)
        baseline_engine.dispose()
        assert calls == [{'operation': 'close', 'position_id': '42', 'volume': 5000}]
        expected_intent = json.dumps(calls, sort_keys=True, separators=(',', ':')).encode()
        calls.clear()
        seed(db)
    else:
        expected_intent = b'[]'
    with db.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}', updated_at=stamp, updated_by='fixture'))
        session.add(StrategyStudioLiveState(owner_id='owner-1', enabled=True,
            enabled_strategy_id='strat-live', enabled_at=stamp, updated_at=stamp))
        session.add(SavedStrategy(strategy_id='strat-live', owner_id='owner-1', name='Preserved fixture',
            schema_version=1, definition_json=copy.deepcopy(preserved_definition),
            created_at=stamp, updated_at=stamp))
        session.add(ExecutionProtocolState(singleton_id=1, protocol_version=EXECUTION_PROTOCOL_VERSION,
            updated_at=stamp))
        schema = db.kw['bind'].get_execution_options()['schema_translate_map'][None]
        quoted = db.kw['bind'].dialect.identifier_preparer.quote(schema)
        session.execute(text('CREATE TABLE ' + quoted + '.alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
        session.execute(text('INSERT INTO ' + quoted + '.alembic_version VALUES (:v)'), {'v': '20261001_0030'})
        from services.submission_reservation import install_reservation_guard
        install_reservation_guard(session.connection())
    prepare_cutover(store_api, db)
    server = server_adapter.ProductionDependencies(api_module=api, engine=db.kw['bind'],
        session_factory=db, scope=SCOPE, build={'backend_git_sha': 'a' * 40})
    server.history_from = server.readers.history_from = server.readers.database.history_from = 1000
    server.readers.authenticate = lambda scope: dict(account_id='7', environment='demo', authenticated=True)
    server.readers.broker = lambda scope: copy.deepcopy(broker)
    paths = {kind: tmp_path / (kind + '.json') for kind in server_adapter.state_paths(api)}
    monkeypatch.setattr(server_adapter, 'state_paths', lambda _: paths)
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', paths['fifteen_m_swing_watch'])
    import db as database_module
    monkeypatch.setattr(database_module, 'SessionLocal', db)
    for name in ('LIVE_ACTIVE_ORDERS', 'LIVE_ACCOUNT_STATE', 'LIVE_TRADE_HISTORY',
                 'LIVE_BROKER_CLOSED_HISTORY', 'LIVE_BROKER_HISTORY_CACHE', 'LIVE_MONTHLY_HISTORY_CACHE',
                 'LIVE_LAST_EXECUTION_TIME', 'LIVE_LAST_POSITION_CLOSED_AT', 'LIVE_ACCOUNT_CLOSE_TIMES',
                 'LIVE_AUTO_TRADE_ENABLED', 'AUTO_TRADE_ENABLED', 'ENGINE_RUNTIME_STATE', 'LAST_LIVE_RESET',
                 'BACKGROUND_THREAD'):
        monkeypatch.setattr(api, name, copy.deepcopy(getattr(api, name)))
    for name in ('FINAL_SIGNAL_HOLD', 'FIFTEEN_M_SWING_WATCH', 'SOURCE_BOOT_STATE',
                 'MARKET_DATA_RUNTIME', 'MARKET_DATA_SOURCE', 'AUTO_TRADES', 'PAPER_ACTIVE_TRADES',
                 'PAPER_TRADE_HISTORY', 'PAPER_SETUP_LOCKS', 'LAST_PAPER_RESET', 'LAST_POSITION_CLOSED_AT'):
        monkeypatch.setattr(shared, name, copy.deepcopy(getattr(shared, name)))
    started = []
    def existing_engine(context=None, *, management_only=False):
        assert caller_token() == server.token
        assert context.token == server.token
        with db() as session:
            assert store_api.require_owner(session, server.token)[0].phase == 'POSITION_MANAGEMENT_READY'
            assert not store_api.entries_ready(session, server.token)
        if not management_only:
            assert api.BACKGROUND_THREAD.recovery_context == context
            return {'ready': True}  # External indicator-feed readiness, not authority.
        started.append(server.token)
        positions = [value for value in api.LIVE_ACTIVE_ORDERS.values() if value]
        if positions:
            assert positions[0]['risk_amount'] == '50'
            assert positions[0]['strategy_identity'] == binding['strategy_identity']
            assert positions[0]['frozen_plan_hash'] == binding['frozen_plan_hash']
        manager.manage_selected_account_positions('owner-1', account, positions,
            prices(), session_factory=db)
        api.BACKGROUND_THREAD = SimpleNamespace(is_alive=lambda: True, recovery_context=context)
    monkeypatch.setattr(app_bootstrap, '_start_forex_background_task', existing_engine)
    monkeypatch.setattr(ingestion, 'start_fundamental_ingestion_scheduler', lambda **kw: None)
    outcome = coordinator.recover(server, SCOPE, 'whole-path', 'a' * 40)
    assert outcome.ready, outcome.reason
    assert len(started) == 1
    assert json.dumps(calls, sort_keys=True, separators=(',', ':')).encode() == expected_intent
    assert list(tmp_path.iterdir()) == []
    with db() as session:
        assert store_api.entries_ready(session, server.token)
        saved = session.get(SavedStrategy, 'strat-live')
        assert saved.definition_json == preserved_definition and saved.updated_at == stamp
        from models import TradeSubmissionAttempt
        assert session.query(TradeSubmissionAttempt).count() == 0
