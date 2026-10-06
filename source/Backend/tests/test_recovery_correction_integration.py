"""Task 7: real admission and ledger integration; no real broker traffic."""
import pytest
import copy
import json
import time
from contextlib import contextmanager
from test_recovery_store import db, store_api
from recovery_integration_fixture import prepare_database, recovery_server, admit


@pytest.fixture
def coordinated(db, monkeypatch, tmp_path):
    prepare_database(db)
    h = recovery_server(db, monkeypatch, tmp_path)
    outcome = admit(h)
    return h, outcome


def test_coordinator_is_the_only_source_of_fixture_admission(coordinated):
    from startup_recovery.runtime import require_worker_admission
    from startup_recovery.types import RecoveryError
    h, outcome = coordinated
    assert outcome.phases == ('BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED',
        'STATE_DISCOVERED', 'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY')
    with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_MISSING'):
        require_worker_admission(None, 'entry')
    with h.server.runtime_context(entries=True):
        require_worker_admission(None, 'entry')
    assert [kind for kind, _ in h.trace] == ['management', 'entries']


@pytest.fixture
def production_chain(db, monkeypatch, tmp_path, request):
    from test_live_identity_metadata_integration import build_integrated
    from models import TradeSubmissionAttempt
    import ctrader_connector as connector
    holder = {}
    @contextmanager
    def admission(factory):
        prepare_database(factory)
        h = recovery_server(factory, monkeypatch, tmp_path)
        outcome = admit(h)
        holder.update(h=h, outcome=outcome)
        with h.server.runtime_context(entries=True):
            yield outcome.token
    generator = build_integrated(tmp_path, monkeypatch, factory=db, admission=admission,
        tp1=getattr(request, 'param', False))
    c = next(generator)
    c.harness = holder['h']
    c.outcome = holder['outcome']
    c.frames = []
    old_wire, old_open = connector.send_ctrader_request, connector.open_ctrader_json_socket
    def opened(*args, **kwargs):
        sock = old_open(*args, **kwargs)
        sock._recovery_environment = 'demo'
        return sock
    def wire(sock, kind, body, expected):
        if kind == 2106:
            # Independent session observes Transaction A before the real socket
            # frame guard. No connection remains checked out during mutation.
            assert db.kw['bind'].pool.checkedout() == 0
            with db() as s:
                row = s.query(TradeSubmissionAttempt).filter_by(broker_client_order_id=body['clientOrderId']).one()
                assert row.send_intent['state'] == 'UNRESOLVED'
                assert row.accepted_execution is None
            connector.websocket_send_frame(sock, 1, json.dumps(dict(payloadType=kind, payload=body)))
            c.frames.append((kind, copy.deepcopy(body)))
        return old_wire(sock, kind, body, expected)
    monkeypatch.setattr(connector, 'open_ctrader_json_socket', opened)
    monkeypatch.setattr(connector, 'send_ctrader_request', wire)
    try:
        yield c
    finally:
        generator.close()


def test_coordinated_claim_commit_wire_acceptance_and_duplicate_entry(production_chain):
    from models import TradeSubmissionAttempt, RecoveryMutation
    c = production_chain
    setup, payload, claim = c.prepare()
    result = c.dispatch(payload, claim)
    assert result['broker_result'] == 'ACCEPTED', result
    assert len(c.frames) == 1 and c.frames[0][0] == 2106
    with c.factory() as session:
        attempt = session.query(TradeSubmissionAttempt).one()
        assert attempt.accepted_execution['position_id'] == '202'
        assert attempt.accepted_execution['order_id'] == '101'
        assert attempt.accepted_execution['frozen_plan_hash'] == setup['studio_binding']['frozen_plan_hash']
        assert attempt.send_intent['state'] != 'UNRESOLVED'
        assert session.query(RecoveryMutation).count() == 0
    again = c.dispatch(payload, claim)
    assert not again['ok']
    assert len(c.frames) == 1


def test_unadmitted_worker_cannot_send_even_with_an_existing_claim(production_chain):
    from contextvars import Context
    from models import TradeSubmissionAttempt
    c = production_chain
    _, payload, claim = c.prepare()
    result = Context().run(c.dispatch, payload, claim)
    assert not result['ok']
    assert result['reason'] == 'RECOVERY_TOKEN_MISSING'
    assert c.frames == [] and c.broker_orders == []
    with c.factory() as session:
        assert session.query(TradeSubmissionAttempt).one().send_intent is None


def test_accepted_ledger_identity_survives_exact_decimal_database_discovery(production_chain):
    """The read-only production reader must not invalidate untouched acceptance."""
    from decimal import Decimal
    from types import SimpleNamespace
    from models import TradeSubmissionAttempt
    from services.accepted_execution import require_accepted_execution
    c = production_chain
    _, payload, claim = c.prepare()
    assert c.dispatch(payload, claim)['broker_result'] == 'ACCEPTED'
    with c.factory() as session:
        original = require_accepted_execution(session.query(TradeSubmissionAttempt).one())
    discovered = c.harness.server.readers.database(c.harness.scope)['submissions']
    assert len(discovered) == 1
    row = discovered[0]
    assert isinstance(row['execution_snapshot']['frozen_plan']['account_balance'], Decimal)
    # No row or identity mutation occurred. The exact-decimal read path must
    # validate the same accepted evidence as the ORM path, without float coercion.
    assert require_accepted_execution(SimpleNamespace(**row)) == original


def test_decimal_discovery_rejects_changed_numeric_identity(production_chain):
    from decimal import Decimal
    from types import SimpleNamespace
    from services.accepted_execution import require_accepted_execution
    from startup_recovery.types import RecoveryError
    c = production_chain
    _, payload, claim = c.prepare()
    assert c.dispatch(payload, claim)['broker_result'] == 'ACCEPTED'
    row, = c.harness.server.readers.database(c.harness.scope)['submissions']
    require_accepted_execution(SimpleNamespace(**row))
    changed = copy.deepcopy(row)
    plan = changed['execution_snapshot']['frozen_plan']
    assert isinstance(plan['account_balance'], Decimal)
    plan['account_balance'] += Decimal('0.000000000000000001')
    assert plan['account_balance'] != row['execution_snapshot']['frozen_plan']['account_balance']
    with pytest.raises(RecoveryError):
        require_accepted_execution(SimpleNamespace(**changed))


def test_accepted_position_handoff_to_management_only_and_original_repair(production_chain, monkeypatch, tmp_path):
    from models import TradeSubmissionAttempt, RecoveryMutation
    from startup_recovery import store
    from startup_recovery.types import HandoffEvidence, RecoveryError
    from startup_recovery.unit_of_work import operation_uow
    from startup_recovery.operation_context import capture, run
    from services.accepted_position_repair import repair_original_protection
    import ctrader_connector as connector
    c = production_chain
    _, payload, claim = c.prepare()
    assert c.dispatch(payload, claim)['broker_result'] == 'ACCEPTED'
    delayed = capture('management')
    with c.factory() as session:
        attempt = session.query(TradeSubmissionAttempt).one()
        attempt_id = attempt.id
        accepted = copy.deepcopy(attempt.accepted_execution)
    broker = copy.deepcopy(c.harness.broker)
    broker['positions'] = [dict(position_id='202', symbol='EURUSD', symbol_id=1,
        side='BUY', volume_units='20000', entry='1.1001', sl='1.0951', tp2='1.1101')]
    broker['metadata'] = {'EURUSD': copy.deepcopy(payload['studio_binding']['frozen_plan']['broker_metadata'])}
    broker['observed_at'] = broker['history_to'] = time.time()
    proof = HandoffEvidence('graceful-drain', 'c' * 64, c.outcome.token.boot_id, True, True)
    with operation_uow(c.factory, c.outcome.token) as uow:
        store.relinquish(uow.session, c.outcome.token, proof)
    successor = recovery_server(c.factory, monkeypatch, tmp_path, broker=broker)
    outcome = admit(successor, boot='integration-b', entries=False, cutover=False)
    assert outcome.management_ready and not outcome.entries_ready
    assert outcome.result.capacity_used == 1
    with pytest.raises(RecoveryError):
        run(delayed, lambda: pytest.fail('stale worker reached callback'))
    # The already accepted broker position now lacks its original protection.
    # Only the test broker observation changes; no DB acceptance is fabricated.
    position = dict(positionId=202, price='1.1001', tradeData=dict(symbolId=1, tradeSide=1, volume=2000000))
    amendments = []
    old_wire = connector.send_ctrader_request
    def wire(sock, kind, body, expected):
        assert kind != 2106, 'management-only repair attempted a new order'
        if kind == 2124:
            return {'payloadType':2125, 'payload':dict(ctidTraderAccountId=7, position=[copy.deepcopy(position)])}
        if kind == 2137:
            return {'payloadType':2138, 'payload':dict(ctidTraderAccountId=7, hasMore=False,
                order=[dict(orderId=101, positionId=202, clientOrderId=accepted['client_order_id'], orderStatus=2)])}
        if kind == 2110:
            assert c.factory.kw['bind'].pool.checkedout() == 0
            with c.factory() as session:
                assert session.get(TradeSubmissionAttempt, attempt_id).initial_protection['state'] == 'UNRESOLVED'
            connector.websocket_send_frame(sock, 1, json.dumps(dict(payloadType=kind, payload=body)))
            amendments.append(copy.deepcopy(body))
            position.update(stopLoss=body['stopLoss'], takeProfit=body['takeProfit'])
            return {'payloadType':2126, 'payload':dict(ctidTraderAccountId=7)}
        return old_wire(sock, kind, body, expected)
    monkeypatch.setattr(connector, 'send_ctrader_request', wire)
    with successor.server.runtime_context():
        result = repair_original_protection(c.factory, outcome.token, attempt_id)
    assert result['state'] == 'CONFIRMED', result
    assert amendments == [dict(ctidTraderAccountId=7, positionId=202, stopLoss=1.0951, takeProfit=1.1101)]
    with c.factory() as session:
        assert session.get(TradeSubmissionAttempt, attempt_id).accepted_execution == accepted
        assert session.get(TradeSubmissionAttempt, attempt_id).initial_protection['state'] == 'CONFIRMED'
        assert session.query(RecoveryMutation).count() == 0
    assert len(c.frames) == 1
