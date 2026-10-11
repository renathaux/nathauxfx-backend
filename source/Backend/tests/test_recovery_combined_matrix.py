"""Task 7 combined proof: real coordinator, PostgreSQL ledger and wire guards.

Each test catches a lost production boundary: admission, durable uncertainty,
identity, handoff, or original protection. Only external broker observations /
transport and continuous worker launch are test doubles. No production data.
"""
import copy
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from models import TradeSubmissionAttempt, StrategySetupLifecycle, RecoveryMutation
from test_recovery_correction_integration import production_chain
from test_recovery_store import db, store_api
from recovery_integration_fixture import recovery_server, admit
from startup_recovery.types import HandoffEvidence, RecoveryError


def accepted(c):
    setup, payload, claim = c.prepare()
    assert c.dispatch(payload, claim)['broker_result'] == 'ACCEPTED'
    with c.factory() as session:
        row = session.query(TradeSubmissionAttempt).one()
        return SimpleNamespace(setup=setup, payload=payload, claim=claim,
            id=row.id, evidence=copy.deepcopy(row.accepted_execution))


@pytest.mark.parametrize('sl,tp', [(None, None), (None, '1.1101'),
    ('1.0951', None), ('1.0901', '1.1101')])
def test_restart_admits_never_sent_original_repair_before_protection_exists(
        production_chain, monkeypatch, tmp_path, sl, tp):
    """Acceptance survives a crash before repair: admission must not require repair first."""
    import api
    from startup_recovery import coordinator
    c = production_chain
    a = accepted(c)
    drain(c, c.outcome.token)
    broker = broker_view(c, a.payload)
    broker['positions'][0].update(sl=sl, tp2=tp)
    h = recovery_server(c.factory, monkeypatch, tmp_path, broker=broker)
    # Even an available entry feed cannot grant entry while protection is missing.
    h.server.entry_readiness = lambda token: {'ready': True}
    outcome = coordinator.recover(h.server, h.scope, 'pre-repair-restart', 'a' * 40)
    assert outcome.management_ready, (outcome.reason, outcome.result)
    assert not outcome.entries_ready
    assert outcome.entry_block_reasons == ('RECOVERY_ORIGINAL_PROTECTION_REQUIRED',)
    assert outcome.result.capacity_used == 1
    recovered, = outcome.result.positions
    assert recovered['initial_protection_required'] is True
    assert recovered['current']['sl'] == sl and recovered['current']['tp2'] == tp
    assert recovered['current_risk'] == (None if sl is None else ('200' if sl == '1.0901' else '100'))
    assert [kind for kind, _ in h.trace] == ['management']
    position, frames = repair_wire(c, a, monkeypatch, sl=sl, tp=tp)
    with h.server.runtime_context():
        assert not c.dispatch(a.payload, a.claim)['ok']
        assert api.repair_pending_original_protection() == [{'state': 'CONFIRMED'}]
    assert frames == [dict(ctidTraderAccountId=7, positionId=202,
        stopLoss=1.0951, takeProfit=1.1101)]
    assert len(c.frames) == 1
    with c.factory() as session:
        row = session.get(TradeSubmissionAttempt, a.id)
        assert row.accepted_execution == a.evidence
        assert row.initial_protection['state'] == 'CONFIRMED'
        assert session.query(RecoveryMutation).count() == 0
    # A later admitted manager observes the confirmed original levels, never resends.
    third, recovered = handoff(c, a.payload, monkeypatch, tmp_path,
        predecessor=outcome.token, boot='post-repair-restart')
    with third.server.runtime_context():
        assert api.repair_pending_original_protection() == []
    assert len(frames) == 1 and len(c.frames) == 1


@pytest.mark.parametrize('defect', ['stronger_stop', 'different_tp', 'missing_acceptance',
    'ambiguous_amendment', 'confirmed_but_missing', 'later_management'])
def test_restart_does_not_launder_conflicting_or_sent_protection_as_initial_repair(
        production_chain, monkeypatch, tmp_path, defect):
    from startup_recovery import coordinator
    c = production_chain
    a = accepted(c)
    drain(c, c.outcome.token)
    broker = broker_view(c, a.payload)
    broker['positions'][0].update(sl=None, tp2=None)
    with c.factory.begin() as session:
        row = session.get(TradeSubmissionAttempt, a.id)
        if defect == 'stronger_stop': broker['positions'][0]['sl'] = '1.1002'
        if defect == 'different_tp': broker['positions'][0]['tp2'] = '1.1201'
        if defect == 'missing_acceptance': row.accepted_execution = None
        if defect == 'ambiguous_amendment': row.initial_protection = {'state': 'UNRESOLVED'}
        if defect == 'confirmed_but_missing': row.initial_protection = {'state': 'CONFIRMED'}
        if defect == 'later_management':
            session.query(StrategySetupLifecycle).one().management_state = {'target_protected_sl': '1.1026'}
    h = recovery_server(c.factory, monkeypatch, tmp_path, broker=broker)
    h.server.entry_readiness = lambda token: {'ready': True}
    outcome = coordinator.recover(h.server, h.scope, 'unsafe-pre-repair', 'a' * 40)
    assert not outcome.management_ready and not outcome.entries_ready
    assert h.trace == [] and len(c.frames) == 1


def broker_view(c, payload):
    broker = copy.deepcopy(c.harness.broker)
    broker['positions'] = [dict(position_id='202', symbol='EURUSD', symbol_id=1,
        side='BUY', volume_units='20000', entry='1.1001', sl='1.0951', tp2='1.1101')]
    broker['metadata'] = {'EURUSD': copy.deepcopy(payload['studio_binding']['frozen_plan']['broker_metadata'])}
    broker['observed_at'] = broker['history_to'] = time.time()
    return broker


def drain(c, token):
    from startup_recovery import store
    from startup_recovery.unit_of_work import operation_uow
    with operation_uow(c.factory, token) as uow:
        store.relinquish(uow.session, token,
            HandoffEvidence('graceful-drain', 'c' * 64, token.boot_id, True, True))


def handoff(c, payload, monkeypatch, tmp_path, *, predecessor=None, boot='matrix-b'):
    drain(c, predecessor or c.outcome.token)
    h = recovery_server(c.factory, monkeypatch, tmp_path, broker=broker_view(c, payload))
    outcome = admit(h, boot=boot, entries=False, cutover=False)
    assert outcome.management_ready and not outcome.entries_ready
    assert outcome.result.capacity_used == 1
    return h, outcome


def repair_wire(c, a, monkeypatch, *, sl=None, tp=None, lost=False):
    """Keep the real read/projection/amend/permit paths; replace protocol I/O."""
    import ctrader_connector as connector
    position = dict(positionId=202, price='1.1001',
        tradeData=dict(symbolId=1, tradeSide=1, volume=2000000))
    if sl is not None:
        position['stopLoss'] = sl
    if tp is not None:
        position['takeProfit'] = tp
    frames = []
    previous = connector.send_ctrader_request
    def wire(sock, kind, body, expected):
        assert kind != 2106, 'repair/management emitted NEW_ORDER'
        if kind == 2124:
            return {'payloadType':2125, 'payload':dict(ctidTraderAccountId=7,
                position=[copy.deepcopy(position)])}
        if kind == 2137:
            return {'payloadType':2138, 'payload':dict(ctidTraderAccountId=7, hasMore=False,
                order=[dict(orderId=101, positionId=202,
                    clientOrderId=a.evidence['client_order_id'], orderStatus=2)])}
        if kind == 2110:
            assert c.factory.kw['bind'].pool.checkedout() == 0
            with c.factory() as session:
                row = session.get(TradeSubmissionAttempt, a.id)
                assert row.initial_protection['state'] == 'UNRESOLVED'
                assert row.accepted_execution == a.evidence
            connector.websocket_send_frame(sock, 1,
                json.dumps(dict(payloadType=kind, payload=body)))
            frames.append(copy.deepcopy(body))
            if lost:
                raise TimeoutError('isolated broker response lost')
            position.update(stopLoss=body['stopLoss'], takeProfit=body['takeProfit'])
            return {'payloadType':2126, 'payload':dict(ctidTraderAccountId=7)}
        return previous(sock, kind, body, expected)
    monkeypatch.setattr(connector, 'send_ctrader_request', wire)
    return position, frames


def test_management_only_successor_rejects_entry_and_all_delayed_a_authority(
        production_chain, monkeypatch, tmp_path):
    from startup_recovery.operation_context import capture, run, publication
    from services.accepted_position_repair import repair_original_protection
    from services.trade_submission_service import claim_strategy_submission
    from startup_recovery import store
    c = production_chain
    a = accepted(c)
    delayed = capture('management')
    h, outcome = handoff(c, a.payload, monkeypatch, tmp_path)
    position, frames = repair_wire(c, a, monkeypatch)
    # A's concrete entry and repair services cannot reacquire B's identity.
    assert not c.dispatch(a.payload, a.claim)['ok']
    with pytest.raises(RecoveryError):
        repair_original_protection(c.factory, c.outcome.token, a.id)
    with pytest.raises(RecoveryError):
        with c.harness.server.runtime_context():
            pytest.fail('A rediscovered itself as current')
    with pytest.raises(RecoveryError):
        run(delayed, lambda: pytest.fail('delayed A callback executed'))
    with pytest.raises(RecoveryError):
        with publication():
            pytest.fail('A published into B state')
    with pytest.raises(RecoveryError):
        c.harness.server.writer.write(c.harness.paths['app_settings'], 'app_settings', {'bad': True})
    # Failed A publication must not revoke B.
    with c.factory() as session:
        assert store.require_owner(session, outcome.token)[0].phase == 'POSITION_MANAGEMENT_READY'
    with h.server.runtime_context():
        denied = claim_strategy_submission(a.setup['setup_id'], '7', 'EURUSD', 'BUY', a.payload,
            owner_id=c.owner, strategy_id=c.strategy_id, session_factory=c.factory)
        assert not denied['ok'] and denied['reason'] == 'LIVE_RECOVERY_INCOMPLETE'
        denied = c.dispatch(a.payload, a.claim)
        assert not denied['ok'] and denied['reason'] == 'LIVE_RECOVERY_INCOMPLETE'
        assert repair_original_protection(c.factory, outcome.token, a.id)['state'] == 'CONFIRMED'
    assert frames == [dict(ctidTraderAccountId=7, positionId=202, stopLoss=1.0951, takeProfit=1.1101)]
    assert len(c.frames) == 1
    # Restart C recovers the same accepted/protected state, with no extra mutation.
    third, recovered = handoff(c, a.payload, monkeypatch, tmp_path,
        predecessor=outcome.token, boot='matrix-c')
    with third.server.runtime_context():
        assert repair_original_protection(c.factory, recovered.token, a.id)['state'] == 'CONFIRMED'
    assert len(frames) == 1
    with c.factory() as session:
        row = session.get(TradeSubmissionAttempt, a.id)
        assert row.accepted_execution == a.evidence
        assert row.initial_protection['state'] == 'CONFIRMED'
        assert session.query(RecoveryMutation).count() == 0


@pytest.mark.parametrize('history,failure', [
    ('exact','lost'), ('duplicate','lost'), ('truncated','lost'), ('empty','lost'),
    ('missing_original','lost'), ('exact','process_death'), ('exact','db_disconnect')])
def test_lost_entry_reserves_then_reconciles_without_resending(
        production_chain, monkeypatch, tmp_path, history, failure):
    import ctrader_connector as connector
    from services.submission_capacity import exposures, enforce_capacity
    c = production_chain
    _, payload, claim = c.prepare()
    old_wire = connector.send_ctrader_request
    def lost(sock, kind, body, expected):
        result = old_wire(sock, kind, body, expected)
        if kind == 2106:
            raise TimeoutError('isolated accepted response lost')
        return result
    with monkeypatch.context() as fault:
        if failure == 'lost':
            fault.setattr(connector, 'send_ctrader_request', lost)
        else:
            from services import submission_intent
            def failed_commit(*args, **kwargs):
                if failure == 'process_death':
                    raise SystemExit('isolated process stopped after broker acceptance')
                raise OperationalError('isolated Transaction B', None, RuntimeError('disconnected'))
            fault.setattr(submission_intent, 'finish_entry', failed_commit)
        try:
            c.dispatch(payload, claim)
        except (SystemExit, OperationalError):
            assert failure != 'lost'
    c.factory.kw['bind'].dispose()
    assert not c.dispatch(payload, claim)['ok']
    with c.factory() as session:
        row = session.query(TradeSubmissionAttempt).one()
        assert row.send_intent['state'] == 'UNRESOLVED' and row.accepted_execution is None
        snapshot = copy.deepcopy(row.execution_snapshot)
        assert len(exposures(session, '7', 'demo', broker_positions=[])) == 1
        with pytest.raises(RecoveryError, match='POSITION_CAPACITY'):
            enforce_capacity(session, snapshot, broker_positions=[])
        proposed = copy.deepcopy(snapshot)
        proposed['symbol'] = proposed['frozen_plan']['symbol'] = 'XAUUSD'
        proposed['frozen_plan']['position_constraints']['max_combined_open_risk_percent'] = 1.5
        with pytest.raises(RecoveryError, match='RISK_CAPACITY'):
            enforce_capacity(session, proposed, broker_positions=[])
    with pytest.raises(RecoveryError, match='UNRESOLVED'):
        drain(c, c.outcome.token)
    evidence = dict(account_id='7', environment='demo', symbol='EURUSD', symbol_id=1,
        side='BUY', client_order_id=claim['broker_client_order_id'], position_id='202',
        order_id='101', entry='1.1001', volume_units='20000')
    items = [evidence]
    if history == 'duplicate':
        items.append(dict(evidence, position_id='303'))
    if history == 'empty':
        items = []
    if history == 'missing_original':
        with c.factory.begin() as session:
            session.query(TradeSubmissionAttempt).one().execution_snapshot = None
    monkeypatch.setattr(c.harness.server.readers, 'submissions',
        lambda scope, claimed: dict(ok=True, complete=history != 'truncated', records=items))
    result = c.harness.server.reconcile_submissions()
    assert result['unresolved'] == ([] if history == 'exact' else [claim['idempotency_key']])
    with c.factory() as session:
        row = session.query(TradeSubmissionAttempt).one()
        assert row.send_intent['state'] == ('ACCEPTED' if history == 'exact' else 'UNRESOLVED')
        assert session.query(RecoveryMutation).count() == 0
        if history == 'exact':
            assert row.reconciliation_status == 'MATCHED'
            exposure = exposures(session, '7', 'demo',
                broker_positions=[dict(position_id='202', symbol='EURUSD')])
            assert len(exposure) == 1 and exposure[0]['risk'] == Decimal('100')
            assert row.accepted_execution['frozen_plan_hash'] == snapshot['frozen_plan_hash']
    if history == 'exact':
        handoff(c, payload, monkeypatch, tmp_path)
    else:
        with pytest.raises(RecoveryError, match='UNRESOLVED'):
            drain(c, c.outcome.token)
    assert len(c.frames) == len(c.broker_orders) == 1


def next_setup(c):
    """A second real evaluator result, not a copied/forged lifecycle row."""
    import pandas as pd
    from test_strategy_engine_evaluator import candle
    first, last = c.t0 + pd.Timedelta(hours=1), c.t1 + pd.Timedelta(hours=1)
    c.timeline.candles = {first:candle(first,1.0995,1.1,1.0994,1.0999),
                         last:candle(last,1.0998,1.1002,1.0997,1.1001)}
    c.timeline._times = [first,last]
    c.timeline.timestamps = lambda: [first,last]
    c.prior.pending_setup['event_timestamp'] = first.isoformat()
    return c.prepare()


def test_distinct_real_setup_cannot_send_over_unresolved_entry(production_chain, monkeypatch):
    import ctrader_connector as connector
    c = production_chain
    first, payload, claim = c.prepare()
    second, other_payload, other_claim = next_setup(c)
    assert first['setup_id'] != second['setup_id']
    old_wire = connector.send_ctrader_request
    def lost(sock, kind, body, expected):
        result = old_wire(sock, kind, body, expected)
        if kind == 2106:
            raise TimeoutError('lost acceptance')
        return result
    monkeypatch.setattr(connector, 'send_ctrader_request', lost)
    c.dispatch(payload, claim)
    result = c.dispatch(other_payload, other_claim)
    assert not result['ok'] and result['reason'] == 'RECOVERY_POSITION_CAPACITY_RESERVED'
    assert len(c.frames) == len(c.broker_orders) == 1
    with c.factory() as session:
        rows = session.query(TradeSubmissionAttempt).all()
        assert len(rows) == 2
        assert sum(row.send_intent is not None for row in rows) == 1


def test_two_admitted_postgres_workers_reserve_one_remaining_slot(production_chain, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from startup_recovery.operation_context import capture, run
    from services.submission_intent import prepare_entry
    c = production_chain
    _, p1, claim1 = c.prepare()
    _, p2, claim2 = next_setup(c)
    context = capture('entry')
    barrier = Barrier(2)
    def broker_read(token):
        assert token == c.outcome.token
        barrier.wait(5)
        return []
    monkeypatch.setattr('services.submission_capacity.read_broker_positions', broker_read)
    def reserve(payload, claim):
        try:
            prepare_entry(c.factory, context.token, claim['idempotency_key'], payload, c.owner)
            return 'reserved'
        except RecoveryError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, context, reserve, p, claim)
            for p,claim in ((p1,claim1),(p2,claim2))]
        results = [future.result(timeout=10) for future in futures]
    assert results.count('reserved') == 1, results
    assert any(value in {'RECOVERY_ACCOUNT_BUSY','RECOVERY_POSITION_CAPACITY_RESERVED'} for value in results)
    with c.factory() as session:
        rows = session.query(TradeSubmissionAttempt).all()
        assert sum(row.send_intent is not None for row in rows) == 1
        assert session.query(RecoveryMutation).count() == 0
    assert c.frames == []


def test_ambiguous_amendment_blocks_replay_and_handoff_until_observed_confirmation(
        production_chain, monkeypatch, tmp_path):
    from services.accepted_position_repair import repair_original_protection, reconcile_initial_repair
    from ctrader_connector import read_original_position_protection
    c = production_chain
    a = accepted(c)
    h, outcome = handoff(c, a.payload, monkeypatch, tmp_path)
    position, frames = repair_wire(c, a, monkeypatch, lost=True)
    with h.server.runtime_context():
        assert repair_original_protection(c.factory, outcome.token, a.id)['state'] == 'UNRESOLVED'
        c.factory.kw['bind'].dispose()
        with pytest.raises(RecoveryError, match='UNRESOLVED'):
            repair_original_protection(c.factory, outcome.token, a.id)
        assert reconcile_initial_repair(c.factory, None, a.id, read_original_position_protection)['state'] == 'UNRESOLVED'
        with pytest.raises(RecoveryError, match='UNRESOLVED'):
            drain(c, outcome.token)
        position.update(stopLoss=1.0951, takeProfit=1.1101)
        assert reconcile_initial_repair(c.factory, None, a.id, read_original_position_protection)['state'] == 'CONFIRMED'
    third, current = handoff(c, a.payload, monkeypatch, tmp_path,
        predecessor=outcome.token, boot='matrix-c')
    with third.server.runtime_context():
        assert repair_original_protection(c.factory, current.token, a.id)['state'] == 'CONFIRMED'
    with c.factory() as session:
        row = session.get(TradeSubmissionAttempt, a.id)
        assert row.initial_protection['state'] == 'CONFIRMED'
        assert row.accepted_execution == a.evidence
        assert session.query(RecoveryMutation).count() == 0
    assert len(frames) == 1 and len(c.frames) == 1


@pytest.mark.parametrize('violation', ['stronger_stop', 'wrong_position', 'wrong_side', 'wrong_volume',
    'missing_snapshot', 'changed_plan', 'distance', 'precision', 'missing_metadata', 'stale_quote'])
def test_real_repair_rejects_unsafe_identity_or_protection(
        production_chain, monkeypatch, tmp_path, violation):
    from services.accepted_position_repair import repair_original_protection
    c = production_chain
    a = accepted(c)
    h, outcome = handoff(c, a.payload, monkeypatch, tmp_path)
    position, frames = repair_wire(c, a, monkeypatch)
    if violation == 'stronger_stop':
        position['stopLoss'] = 1.0999
    elif violation == 'wrong_position':
        position['positionId'] = 999
    elif violation == 'wrong_side':
        position['tradeData']['tradeSide'] = 2
    elif violation == 'wrong_volume':
        position['tradeData']['volume'] = 1000000
    elif violation == 'distance':
        c.full['slDistance'] = 100000
    elif violation == 'precision':
        c.full['digits'] = 2
    elif violation == 'missing_metadata':
        del c.full['slDistance']
    elif violation == 'stale_quote':
        c.state['quote_age'] = 60
    else:
        with c.factory.begin() as session:
            row = session.get(TradeSubmissionAttempt, a.id)
            snapshot = copy.deepcopy(row.execution_snapshot)
            if violation == 'changed_plan':
                snapshot['frozen_plan']['sl'] = '1.09'
            row.execution_snapshot = None if violation == 'missing_snapshot' else snapshot
    before = copy.deepcopy(position)
    with h.server.runtime_context(), pytest.raises((RecoveryError, ValueError, RuntimeError)):
        repair_original_protection(c.factory, outcome.token, a.id)
    assert frames == [] and position == before and len(c.frames) == 1


def test_current_saved_strategy_cannot_rewrite_original_accepted_protection(
        production_chain, monkeypatch, tmp_path):
    from services.strategy_studio_models import SavedStrategy
    from services.accepted_position_repair import repair_original_protection
    c = production_chain
    a = accepted(c)
    h, outcome = handoff(c, a.payload, monkeypatch, tmp_path)
    _, frames = repair_wire(c, a, monkeypatch)
    # Only this disposable DB fixture is edited. Invalid current definition is
    # deliberately unusable; it must never be a source for accepted protection.
    with c.factory.begin() as session:
        session.get(SavedStrategy, c.strategy_id).definition_json = {'new': 'unrelated'}
    with h.server.runtime_context():
        assert repair_original_protection(c.factory, outcome.token, a.id)['state'] == 'CONFIRMED'
    assert frames == [dict(ctidTraderAccountId=7, positionId=202, stopLoss=1.0951, takeProfit=1.1101)]
    with c.factory() as session:
        assert session.get(TradeSubmissionAttempt, a.id).accepted_execution == a.evidence


@pytest.mark.parametrize('production_chain', [True], indirect=True)
def test_q_existing_tp1_protection_close_semantics_through_real_management(
        production_chain, monkeypatch, tmp_path):
    from services.accepted_position_repair import repair_original_protection
    from services import strategy_studio_position_manager as manager
    from ctrader_account_context import AccountIdentity
    from test_strategy_studio_position_manager import open_position
    c = production_chain
    a = accepted(c)
    h, outcome = handoff(c, a.payload, monkeypatch, tmp_path)
    _, initial = repair_wire(c, a, monkeypatch)
    operations = []
    monkeypatch.setattr(manager, 'close_position',
        lambda position_id, volume=None: operations.append(('close', position_id, volume)) or {'ok':True})
    monkeypatch.setattr(manager, 'modify_position_stop_loss',
        lambda position_id, stop, take_profit_price=None:
            operations.append(('protect', position_id, stop, take_profit_price)) or {'ok':True})
    position = open_position(position_id='202', entry=1.1001, sl=1.0951,
        tp2=1.1101, volume=20000, price=1.106)
    def poll(positions):
        return manager.manage_selected_account_positions(c.owner, AccountIdentity('7','demo'),
            positions, {'EURUSD':dict(bid=1.106, ask=1.1061)}, session_factory=c.factory)
    with h.server.runtime_context():
        assert repair_original_protection(c.factory, outcome.token, a.id)['state'] == 'CONFIRMED'
        first = poll([position])
        assert first['actions'][0]['action'] == 'TP1_PARTIAL_CLOSE'
        assert operations == [('close','202',10000)]
        poll([position])  # Same observation cannot close twice.
        assert len(operations) == 1
        position.update(volume=10000, volume_units=10000)
        poll([position])
        assert operations == [('close','202',10000), ('protect','202',1.1026,1.1101)]
        position.update(sl=1.1026, stop_loss=1.1026)
        assert poll([position])['actions'] == []
        with c.factory() as session:
            row = session.get(StrategySetupLifecycle, a.setup['setup_id'])
            assert row.tp1_completed_at is not None and row.protection_applied_at is not None
            assert row.management_state['protection_state'] == 'CONFIRMED'
            assert session.get(TradeSubmissionAttempt,a.id).accepted_execution == a.evidence
        assert poll([])['terminalized'] == 1
    assert len(initial) == 1 and len(c.frames) == 1
    with c.factory() as session:
        assert session.get(StrategySetupLifecycle,a.setup['setup_id']).status == 'CLOSED'
        # Later management references its existing lifecycle; never independent outcomes.
        references = session.query(RecoveryMutation).all()
        assert len(references) == 2
        assert {reference.operation_kind for reference in references} == {'CLOSE_POSITION', 'AMEND_POSITION'}
        for reference in references:
            assert reference.setup_id == a.setup['setup_id']
            assert reference.submission_id is None and reference.management_intent_id
            assert reference.intent_hash == reference.management_intent_id
            assert reference.epoch == outcome.token.epoch
            assert not hasattr(reference, 'status')
