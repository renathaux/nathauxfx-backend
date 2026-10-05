"""Final transport must never accept an unfenced mutation, even via raw frames."""
import importlib
import json
from unittest.mock import Mock

import pytest

from test_recovery_store import store_api, db, begin, cutover


def admission():
    try:
        return importlib.import_module('startup_recovery.admission')
    except ModuleNotFoundError:
        pytest.fail('Final broker mutation admission missing')


@pytest.mark.parametrize('payload_type', [2106, 2107, 2108, 2109, 2110, 2111])
def test_raw_order_frame_without_capability_never_sends(payload_type):
    import ctrader_connector as c
    sock = Mock()
    try:
        c.websocket_send_frame(sock, 1, json.dumps({'payloadType': payload_type,
            'payload': {'ctidTraderAccountId': 123}}))
    except RuntimeError as exc:
        assert 'RECOVERY_' in str(exc)
    else:
        pytest.fail('Unfenced mutation reached raw transport')
    sock.sendall.assert_not_called()


def test_raw_auth_and_heartbeat_remain_nontrading():
    import ctrader_connector as c
    sock = Mock()
    c.websocket_send_frame(sock, 1, json.dumps({'payloadType': 2100, 'payload': {}}))
    c.websocket_send_frame(sock, 10, b'ping')
    assert sock.sendall.call_count == 2


def ready(api, db, t):
    cutover(api, db, t)
    with db.begin() as s:
        api.acquire_owner(s, t)
    phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED', 'STATE_DISCOVERED',
              'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY']
    for old, new in zip(phases, phases[1:]):
        with db.begin() as s:
            api.advance(s, t, old, new, 'e' * 64)


def create_submission(db):
    from datetime import datetime, timezone
    from models import TradeSubmissionAttempt
    with db.begin() as s:
        a = TradeSubmissionAttempt(event_id='evt', mode='LIVE', owner_id='owner',
            account_id='123', symbol='EURUSD', direction='BUY', signal_setup_id='setup',
            idempotency_key='claim', attempt_status='SUBMITTING',
            claimed_at=datetime.now(timezone.utc), request_started_at=datetime.now(timezone.utc),
            broker_client_order_id='client', request_payload_fingerprint='f' * 64,
            reconciliation_status='NOT_REQUIRED', updated_at=datetime.now(timezone.utc))
        s.add(a)
        s.flush()
        return a.id


def test_operation_outcome_always_comes_from_existing_submission(store_api, db):
    from models import TradeSubmissionAttempt
    api = admission()
    t = begin(store_api, db)
    ready(store_api, db, t)
    submission = create_submission(db)
    with api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission) as permit:
        assert permit.operation_key == 'submission:' + str(submission)
    with db.begin() as s:
        assert api.operation_outcome(s, 'submission:' + str(submission)) == 'SUBMITTING'
        s.get(TradeSubmissionAttempt, submission).attempt_status = 'ACCEPTED'
    with db.begin() as s:
        assert api.operation_outcome(s, 'submission:' + str(submission)) == 'ACCEPTED'
    # A parallel recovery outcome cannot be supplied, let alone contradict it.
    with pytest.raises(TypeError):
        api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission, outcome='REJECTED')


def test_delayed_old_epoch_cannot_mutate_after_proven_handoff(store_api, db):
    from startup_recovery.types import HandoffEvidence
    api = admission()
    a = begin(store_api, db)
    ready(store_api, db, a)
    with db.begin() as s:
        store_api.relinquish(s, a, HandoffEvidence('graceful-drain', 'd' * 64, a.boot_id, True, True))
    b = begin(store_api, db, 'boot-b')
    with db.begin() as s:
        store_api.acquire_owner(s, b)
    submit = Mock()
    with pytest.raises(store_api.RecoveryError, match='RECOVERY_TOKEN_STALE'):
        with api.mutation_guard(db, a, 'NEW_ORDER', 'f' * 64, submission_id=999):
            submit()
    submit.assert_not_called()


def test_uncertain_existing_operation_blocks_relinquish(store_api, db):
    from startup_recovery.types import HandoffEvidence
    t = begin(store_api, db)
    ready(store_api, db, t)
    create_submission(db)
    with db.begin() as s:
        with pytest.raises(store_api.RecoveryError, match='RECOVERY_OPERATIONS_UNRESOLVED'):
            store_api.relinquish(s, t, HandoffEvidence('graceful-drain', 'd' * 64, t.boot_id, True, True))


def test_one_admitted_frame_then_replay_is_blocked(store_api, db):
    import ctrader_connector as c
    api = admission()
    t = begin(store_api, db)
    ready(store_api, db, t)
    submission = create_submission(db)
    sock = Mock()
    sock._recovery_environment = 'demo'
    frame = json.dumps({'payloadType': 2106, 'payload': {'ctidTraderAccountId': 123}})
    with api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission):
        with pytest.raises(store_api.RecoveryError, match='RECOVERY_DURABLE_SEND_INTENT_REQUIRED'):
            c.websocket_send_frame(sock, 1, frame)
        with pytest.raises(store_api.RecoveryError, match='RECOVERY_DURABLE_SEND_INTENT_REQUIRED'):
            c.websocket_send_frame(sock, 1, frame)
    assert sock.sendall.call_count == 0
    with pytest.raises(store_api.RecoveryError, match='RECOVERY_OPERATION_ALREADY_FENCED'):
        with api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission):
            c.websocket_send_frame(sock, 1, frame)
    assert sock.sendall.call_count == 0


@pytest.mark.parametrize('environment,account', [('live', 123), ('demo', 456)])
def test_wrong_transport_identity_never_sends(store_api, db, environment, account):
    import ctrader_connector as c
    api = admission()
    t = begin(store_api, db)
    ready(store_api, db, t)
    submission = create_submission(db)
    sock = Mock()
    sock._recovery_environment = environment
    with api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission):
        with pytest.raises(store_api.RecoveryError, match='RECOVERY_ACCOUNT_CONFLICT'):
            c.websocket_send_frame(sock, 1, json.dumps({'payloadType': 2106,
                'payload': {'ctidTraderAccountId': account}}))
    sock.sendall.assert_not_called()


def test_db_connection_loss_after_validation_never_sends_or_transfers(store_api, db):
    import ctrader_connector as c
    from sqlalchemy import text
    api = admission()
    t = begin(store_api, db)
    ready(store_api, db, t)
    submission = create_submission(db)
    sock = Mock()
    sock._recovery_environment = 'demo'
    with pytest.raises(Exception):
        with api.mutation_guard(db, t, 'NEW_ORDER', 'f' * 64, submission_id=submission) as permit:
            pid = permit.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
            with db.begin() as kill:
                assert kill.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': pid}).scalar_one()
            c.websocket_send_frame(sock, 1, json.dumps({'payloadType': 2106,
                'payload': {'ctidTraderAccountId': 123}}))
    sock.sendall.assert_not_called()
    sock.close.assert_called_once()
    b = begin(store_api, db, 'boot-b')
    with db.begin() as s:
        with pytest.raises(store_api.RecoveryError, match='RECOVERY_OWNER_BUSY'):
            store_api.acquire_owner(s, b)


def test_management_without_epoch_does_not_publish_intent_or_call_broker(db, monkeypatch):
    from services import strategy_studio_position_manager as manager
    from test_strategy_studio_position_manager import seed_lifecycle, open_position, prices
    from ctrader_account_context import AccountIdentity
    from models import StrategySetupLifecycle
    seed_lifecycle(db, account_id='123', account_scope='CTRADER:DEMO:123')
    broker = Mock(return_value={'ok': True})
    monkeypatch.setattr(manager, 'close_position', broker)
    result = manager.manage_selected_account_positions('owner-1', AccountIdentity('123', 'demo'),
        [open_position()], prices(), session_factory=db)
    assert result['status'] == 'RECOVERY_BLOCKED'
    assert result['reason'] == 'RECOVERY_TOKEN_MISSING'
    broker.assert_not_called()
    with db() as s:
        row = s.get(StrategySetupLifecycle, 'setup-1')
        assert row.tp1_requested_at is None
        assert row.management_state is None


def test_admitted_manager_references_existing_tp1_intent(store_api, db, monkeypatch):
    from services import strategy_studio_position_manager as manager
    from test_strategy_studio_position_manager import seed_lifecycle, open_position, prices
    from ctrader_account_context import AccountIdentity
    from models import RecoveryMutation, StrategySetupLifecycle
    from startup_recovery.runtime import manager_context, operation_permit
    t = begin(store_api, db)
    ready(store_api, db, t)
    seed_lifecycle(db, account_id='123', account_scope='CTRADER:DEMO:123')
    calls = []
    def close(position_id, volume):
        permit = operation_permit()
        with db() as s:
            row = s.get(StrategySetupLifecycle, 'setup-1')
            assert row.tp1_requested_at is not None
            assert row.management_state['tp1_requested_volume'] == 5000
            annotation = s.get(RecoveryMutation, permit.operation_key)
            assert annotation.setup_id == row.setup_id
            assert annotation.submission_id is None
        calls.append((position_id, volume))
        return {'ok': True}
    monkeypatch.setattr(manager, 'close_position', close)
    with manager_context(t):
        manager.manage_selected_account_positions('owner-1', AccountIdentity('123', 'demo'),
            [open_position()], prices(), session_factory=db)
    assert calls == [('pos-1', 5000)]


def test_admitted_protection_references_existing_intent(store_api, db, monkeypatch):
    from services import strategy_studio_position_manager as manager
    from test_strategy_studio_position_manager import seed_lifecycle, open_position, prices
    from ctrader_account_context import AccountIdentity
    from models import RecoveryMutation
    from startup_recovery.runtime import manager_context, operation_permit
    t = begin(store_api, db)
    ready(store_api, db, t)
    seed_lifecycle(db, account_id='123', account_scope='CTRADER:DEMO:123', tp1_done=True)
    calls = []
    def amend(position_id, sl, take_profit_price):
        permit = operation_permit()
        with db() as s:
            assert s.get(RecoveryMutation, permit.operation_key).setup_id == 'setup-1'
        calls.append((position_id, sl, take_profit_price))
        return {'ok': True}
    monkeypatch.setattr(manager, 'modify_position_stop_loss', amend)
    with manager_context(t):
        manager.manage_selected_account_positions('owner-1', AccountIdentity('123', 'demo'),
            [open_position()], prices(), session_factory=db)
    assert len(calls) == 1
    assert calls[0][0] == 'pos-1'
    assert calls[0][1:] == pytest.approx((1.1025, 1.11))
