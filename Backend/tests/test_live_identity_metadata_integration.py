"""Combined production guards, temporary owner DB, mocked broker wire only.

Breaks caught: identity/plan loss at handoff, metadata substitution, skipped
freshness/distance checks, and any new-order send following a blocked guard.
No real strategy/account is read, enabled, edited, or submitted.
"""
import copy
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api
import ctrader_connector as connector
from ctrader_account_context import AccountIdentity, pinned_account
from db import Base
from models import ExecutionProtocolState, StrategySetupLifecycle, StrategyStudioLiveState
from services import ctrader_symbol_metadata as diagnostic
from services import strategy_studio_live_candidate as candidate
from services.strategy_live_binding import dispatch_bound_order
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
from services.strategy_studio_schema import normalize_definition
from services.trade_submission_service import claim_strategy_submission, mark_request_started
from test_broker_execution_metadata import raw_case
from test_strategy_studio_live_candidate import _definition, _pending_state
from test_strategy_engine_evaluator import FakeTimeline, candle


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    yield from build_integrated(tmp_path, monkeypatch)


def build_integrated(tmp_path, monkeypatch, *, factory=None, admission=None, tp1=False):
    """Reuse wire/evaluator fixtures with explicit real admission in Task 7."""
    monkeypatch.setattr('services.submission_capacity.read_broker_positions',lambda token: [])
    own_engine = factory is None
    engine = create_engine(f'sqlite:///{tmp_path / "combined.sqlite"}') if own_engine else factory.kw['bind']
    if own_engine:
        Base.metadata.create_all(engine)
    from services.submission_reservation import install_reservation_guard
    with engine.begin() as connection:
        install_reservation_guard(connection)
    factory = sessionmaker(bind=engine, expire_on_commit=False) if own_engine else factory
    owner, strategy_id = 'verification-owner', 'verification-saved-strategy'
    account = AccountIdentity('7', 'demo')
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    definition = normalize_definition(_definition())
    definition['tp2']['value'] = 2.0
    if tp1:
        definition['tp1'].update(enabled=True, target_r=1.0, close_percent=50.0, protection_r=0.5)
    with factory() as session:
        session.add(SavedStrategy(strategy_id=strategy_id, owner_id=owner,
            name='Combined verification fixture', schema_version=1,
            definition_json=definition, created_at=stamp, updated_at=stamp))
        session.add(StrategyStudioSelection(owner_id=owner,strategy_id=strategy_id,activated_at=stamp,updated_at=stamp))
        session.add(StrategyStudioLiveState(owner_id=owner,enabled=True,enabled_strategy_id=strategy_id,enabled_at=stamp,updated_at=stamp))
        session.add(ExecutionProtocolState(singleton_id=1,protocol_version='indicator-event-execution-v2',updated_at=stamp))
        session.commit()
    light, full, trader, assets = raw_case()
    broker_orders, reads, sockets = [], [], []
    state = dict(quote_age=0, bid=110000, ask=110010, protection=True)
    def open_socket(*args, **kwargs):
        sock = Mock()
        sock.gettimeout.return_value = 8
        sockets.append(sock)
        return sock
    def wire(sock, kind, body, expected):
        if kind == connector.PAYLOAD_NEW_ORDER_REQ:
            broker_orders.append(copy.deepcopy(body))
            return {'payloadType':expected,'payload':dict(ctidTraderAccountId=7,
                executionType='ORDER_FILLED',order={'orderId':101,'clientOrderId':body['clientOrderId']},
                position={'positionId':202,'stopLoss':1.0951 if state['protection'] else None,
                    'takeProfit':1.1101 if state['protection'] else None,
                    'price':'1.1001','tradeData':{'symbolId':1,'tradeSide':1,'volume':2000000}})}
        assert kind in (2100,2102,2112,2114,2116,2121,2127,2149), 'unexpected broker operation'
        reads.append(kind)
        body = dict(ctidTraderAccountId=7)
        if kind == connector.PAYLOAD_SYMBOLS_LIST_REQ: body['symbol'] = [copy.deepcopy(light)]
        elif kind == connector.PAYLOAD_SYMBOL_BY_ID_REQ: body['symbol'] = [copy.deepcopy(full)]
        elif kind == connector.PAYLOAD_TRADER_REQ: body['trader'] = copy.deepcopy(trader)
        elif kind == 2112: body['asset'] = copy.deepcopy(assets)
        elif kind == 2149: body['ctidTraderAccount'] = [{'ctidTraderAccountId':7,'isLive':False}]
        return {'payloadType':expected,'payload':body}
    def quote_frame(sock):
        return json.dumps({'payloadType':connector.PAYLOAD_SPOT_EVENT,'payload':dict(
            ctidTraderAccountId=7,symbolId=1,bid=state['bid'],ask=state['ask'],
            timestamp=int((time.time()-state['quote_age'])*1000))})
    monkeypatch.setattr(connector,'open_ctrader_json_socket',open_socket)
    monkeypatch.setattr(connector,'send_ctrader_request',wire)
    monkeypatch.setattr(connector,'websocket_recv_text',quote_frame)
    monkeypatch.setattr(connector,'get_ctrader_config',lambda: dict(account_id='7',env='demo',client_id='fixture',client_secret='fixture',access_token='fixture'))
    monkeypatch.setattr(diagnostic,'_selected_account',lambda: ('7','demo',stamp))
    monkeypatch.setattr(diagnostic,'_credentials',lambda: dict(client_id='fixture',client_secret='fixture',access_token='fixture'))
    t0, t1 = pd.Timestamp('2026-09-17T13:00:00Z'), pd.Timestamp('2026-09-17T13:05:00Z')
    timeline = FakeTimeline(candles={t0:candle(t0,1.0995,1.1,1.0994,1.0999),
                                    t1:candle(t1,1.0998,1.1002,1.0997,1.1001)})
    timeline.timestamps = lambda: [t0,t1]
    prior = _pending_state()
    prior.pending_setup.update(broken_level=1.0995,invalidation_price=1.0951)
    monkeypatch.setattr(candidate,'build_market_facts',lambda *a, **k: timeline)
    monkeypatch.setattr(api,'sync_ctrader_account_state',lambda: None)
    monkeypatch.setattr(api,'get_signal_trade_plan',lambda symbol: {})
    monkeypatch.setattr(api,'LIVE_ACCOUNT_STATE',dict(connected=True,mode='demo'))
    monkeypatch.setattr(api,'LIVE_ACTIVE_ORDERS',{'EURUSD':None})
    def build():
        return candidate.build_studio_candidate(owner,account,'EURUSD',{'5m':pd.DataFrame()},
            account_balance=10000.,prior_state=prior,session_factory=factory)
    def prepare():
        setup = build()
        assert setup['studio_live_ready'], setup
        plan = api.studio_candidate_execution_plan(setup,account_balance=10000.,owner_id=owner)
        payload = api.prepare_ctrader_trade(dict(plan,action='BUY',entry=plan['entry_price'],sl=plan['stop_loss']))
        assert payload['ok'], payload
        record = payload['studio_binding']['frozen_plan']['broker_metadata']
        # 0.005 USD / authoritative 0.0001 pip = exactly 50 pips.
        risk = api.calculate_position_size('EURUSD',10000.,1.,50.,broker_metadata=record)
        assert risk['ok'], risk
        payload.update(risk=risk,volume=risk['lot_size'],volume_units=risk['volume_units'],
            risk_percent=risk['risk_percent'],risk_amount=risk['risk_amount'],account_balance_used=10000.)
        claimed = claim_strategy_submission(setup['setup_id'],'7','EURUSD','BUY',payload,
            owner_id=owner,strategy_id=strategy_id,session_factory=factory)
        assert claimed['ok'], claimed
        assert mark_request_started(claimed['idempotency_key'],session_factory=factory)
        return setup,payload,claimed
    def dispatch(payload, claimed, runtime_owner=owner):
        def submit():
            with pinned_account(account):
                return connector.place_market_order(symbol='EURUSD',action=payload['action'],
                    entry=payload['entry'],sl=payload['sl'],tp1=payload['tp1'],tp2=payload['tp2'],
                    volume=payload['volume'],volume_units=payload['volume_units'],risk=payload['risk'],
                    mode='demo',client_order_id=claimed['broker_client_order_id'],
                    broker_label=claimed['broker_label'],broker_comment=claimed['broker_comment'])
        return dispatch_bound_order(payload,claimed['idempotency_key'],runtime_owner,submit,session_factory=factory)
    from recovery_fixture import admitted_manager
    from startup_recovery.checkpoint_store import RuntimeWriter, checkpoint_producer
    monkeypatch.setattr(connector, 'CTRADER_ACCOUNTS_PATH', tmp_path / 'ctrader_accounts.json')
    if admission is not None:
        with admission(factory) as recovery_token:
            yield SimpleNamespace(**locals())
        return
    with admitted_manager(factory, '7') as recovery_token:
        # Explicitly admitted preference absence; do not bypass the production
        # reader or let it import a developer's legacy account file.
        checkpoint_identity = dict(account_scope='CTRADER:DEMO:7', owner_id=owner,
            symbol=None, strategy_id=None, config_hash=None, position_id=None,
            epoch=recovery_token.epoch, boot_id=recovery_token.boot_id,
            build_id='a' * 40, dependencies_hash='b' * 64, generation=1)
        writer = RuntimeWriter(factory, recovery_token, {'ctrader_accounts': (
            connector.CTRADER_ACCOUNTS_PATH, checkpoint_identity,
            copy.deepcopy(connector.DEFAULT_CTRADER_ACCOUNT_SETTINGS))},
            'e' * 64, absent_kinds={'ctrader_accounts'})
        with checkpoint_producer(writer):
            yield SimpleNamespace(**locals())
    engine.dispose()


def test_owner_saved_setup_sizing_quote_to_mocked_final_order(integrated):
    c = integrated
    setup, payload, claimed = c.prepare()
    identity = setup['studio_binding']['strategy_identity']
    assert (identity['owner_id'],identity['strategy_id'],identity['schema_version']) == (c.owner,c.strategy_id,1)
    assert identity['updated_at'] == '2026-10-01T00:00:00.000000+00:00'
    assert len(identity['config_hash']) == 64
    assert len(setup['studio_binding']['frozen_plan_hash']) == 64
    with c.factory() as session:
        row = session.get(StrategySetupLifecycle,setup['setup_id'])
        assert row.entry_binding == payload['studio_binding']
        assert row.definition_snapshot == c.definition
        assert row.owner_id == c.owner
    assert payload['volume_units'] == 20000
    assert payload['volume'] == .2
    assert str(payload['tp2']) == '1.1101'  # calculated by the real evaluator, never injected
    result = c.dispatch(payload,claimed)
    assert result['ok'], result
    assert result['broker_result'] == 'ACCEPTED'
    assert len(c.broker_orders) == 1
    sent = c.broker_orders[0]
    assert (sent['ctidTraderAccountId'],sent['symbolId'],sent['volume']) == (7,1,2000000)
    assert (sent['relativeStopLoss'],sent['relativeTakeProfit']) == (500,1000)
    evidence = result['broker_metadata_validation']
    frozen = payload['studio_binding']['frozen_plan']['broker_metadata']
    assert evidence['metadata_hash'] == frozen['metadata_hash']
    assert evidence['retrieval_id'] == frozen['retrieval_id']
    assert evidence['quote']['ask'] == '1.1001'
    assert evidence['quote']['source'] == 'ProtoOASpotEvent'
    assert c.reads.count(connector.PAYLOAD_SYMBOL_BY_ID_REQ) == 2
    assert all(sock.close.call_count == 1 for sock in c.sockets)


def test_recovery_revoked_after_claim_blocks_mocked_final_submission(integrated):
    from models import RecoveryAccount, RecoveryAttempt
    c = integrated
    setup, payload, claimed = c.prepare()
    with c.factory.begin() as s:
        s.query(RecoveryAccount).update({'phase': 'RECOVERY_BLOCKED'})
        s.query(RecoveryAttempt).update({'phase': 'RECOVERY_BLOCKED'})
    result = c.dispatch(payload, claimed)
    assert result['ok'] is False
    assert result['reason'] == 'LIVE_RECOVERY_INCOMPLETE'
    assert c.broker_orders == []


def test_entry_records_acceptance_without_inline_protection_amend(integrated, monkeypatch):
    from models import TradeSubmissionAttempt
    c=integrated
    _, payload, claimed=c.prepare()
    c.state['protection']=False
    amend=Mock(return_value={'ok':False})
    monkeypatch.setattr(connector,'modify_position_sltp',amend)
    monkeypatch.setattr(connector,'fetch_ctrader_open_positions',lambda config: [])
    result=c.dispatch(payload,claimed)
    assert result['broker_result']=='ACCEPTED_PROTECTION_FAILED'
    amend.assert_not_called()
    assert len(c.broker_orders)==1
    with c.factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=claimed['idempotency_key']).one()
        assert row.accepted_execution['position_id']=='202'
        assert row.initial_protection['state']=='UNASSESSED'


def test_unresolved_entry_reserves_capacity_against_another_setup(integrated):
    """A different setup must not bypass a committed unresolved entry intent."""
    from services.submission_intent import prepare_entry
    from startup_recovery.runtime import caller_token
    c=integrated
    setup,payload,first=c.prepare()
    prepare_entry(c.factory,caller_token(),first['idempotency_key'],payload,c.owner)
    second_setup='capacity-check-second-setup'
    with c.factory.begin() as s:
        original=s.get(StrategySetupLifecycle,setup['setup_id'])
        fields={column.name:copy.deepcopy(getattr(original,column.name))
                for column in StrategySetupLifecycle.__table__.columns}
        fields.update(setup_id=second_setup,status='ELIGIBLE',execution_snapshot=None)
        s.add(StrategySetupLifecycle(**fields))
    second_payload=copy.deepcopy(payload)
    second_payload['studio_setup_id']=second_setup
    second=claim_strategy_submission(second_setup,'7','EURUSD','BUY',second_payload,
        owner_id=c.owner,strategy_id=c.strategy_id,session_factory=c.factory)
    if second['ok']:
        result=c.dispatch(second_payload,second)
        assert c.broker_orders==[], 'another setup emitted mocked NEW_ORDER while first intent unresolved'
        assert not result['ok'], 'unresolved entry failed to reserve account/symbol capacity'
    assert c.broker_orders==[], 'another setup emitted NEW_ORDER while first intent unresolved'


def test_post_send_recovery_error_cannot_be_reported_as_unsent(integrated):
    from startup_recovery.types import RecoveryError
    c = integrated
    _, payload, claimed = c.prepare()
    sock = Mock()
    sock._recovery_environment = 'demo'
    def lost_after_send():
        connector.websocket_send_frame(sock, 1, json.dumps({'payloadType': 2106,
            'payload': {'ctidTraderAccountId': 7, 'symbolId': 1,
                'tradeSide': 1, 'orderType': 1, 'volume': 2000000,
                'relativeStopLoss': 500, 'relativeTakeProfit': 1000,
                'clientOrderId': claimed['broker_client_order_id']}}))
        raise RecoveryError('RECOVERY_DB_UNAVAILABLE')
    result = dispatch_bound_order(payload, claimed['idempotency_key'], c.owner,
                                  lost_after_send, session_factory=c.factory)
    assert sock.sendall.call_count == 1
    assert result['broker_result'] == 'AMBIGUOUS'
    assert result['order_sent'] is True


@pytest.mark.parametrize('failure,reason',[
    ('edited','STRATEGY_VERSION_CHANGED'), ('deleted','STRATEGY_SAVED_ROW_MISSING'),
    ('owner','STRATEGY_OWNER_MISMATCH'), ('sl','STRATEGY_PLAN_CHANGED'),
    ('frozen_plan','STRATEGY_PLAN_CHANGED'),
    ('tp','STRATEGY_PLAN_CHANGED'), ('risk','STRATEGY_PLAN_CHANGED'),
    ('metadata','BROKER_METADATA_CHANGED'), ('fallback','BROKER_METADATA_PLAN_CHANGED'),
    ('incomplete','BROKER_METADATA_INVALID_slDistance'), ('stale','BROKER_QUOTE_STALE'),
    ('distance','BROKER_DISTANCE_VIOLATION'),
])
def test_combined_blocked_paths_never_submit(integrated,failure,reason):
    c = integrated
    setup,payload,claimed = c.prepare()
    owner = c.owner
    if failure in ('edited','deleted'):
        with c.factory() as session:
            row = session.get(SavedStrategy,c.strategy_id)
            if failure == 'deleted': session.delete(row)
            else:
                changed = copy.deepcopy(row.definition_json)
                changed['risk']['value'] = 2.0
                row.definition_json = changed
                row.updated_at += timedelta(seconds=1)
            session.commit()
    elif failure == 'owner': owner = 'other-owner'
    elif failure == 'sl': payload['sl'] = 1.09
    elif failure == 'frozen_plan': payload['studio_binding']['frozen_plan']['sl'] = 1.09
    elif failure == 'tp': payload['tp2'] = 1.12
    elif failure == 'risk': payload['risk']['risk_amount'] = 200.
    elif failure == 'metadata': c.full['stepVolume'] = 200000
    elif failure == 'fallback': payload['risk']['broker_metadata'] = {'metadata_source':'fallback'}
    elif failure == 'incomplete': del c.full['slDistance']
    elif failure == 'stale': c.state['quote_age'] = 5
    elif failure == 'distance': c.state['bid'] = 109520
    result = c.dispatch(payload,claimed,owner)
    assert not result['ok'], result
    assert reason in result['reason'], result
    assert c.broker_orders == [], 'blocked final authorization reached NEW_ORDER'


def test_saved_three_positions_stays_blocked_before_eligible_setup(integrated):
    c = integrated
    with c.factory() as session:
        row = session.get(SavedStrategy,c.strategy_id)
        changed = copy.deepcopy(row.definition_json)
        changed['risk'].update(max_concurrent_positions=3,max_combined_open_risk_percent=3.)
        row.definition_json = changed
        session.commit()
    result = c.build()
    assert result['reason'] == 'STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED'
    assert not result['studio_live_ready']
    with c.factory() as session:
        assert session.get(SavedStrategy,c.strategy_id).definition_json['risk']['max_concurrent_positions'] == 3
        assert session.query(StrategySetupLifecycle).count() == 0
    assert c.broker_orders == []


@pytest.mark.parametrize('bad_source',[False,True])
def test_real_evaluator_invalid_price_never_creates_executable_setup(integrated,bad_source):
    c = integrated
    if bad_source:
        c.timeline.candles[c.t1] = replace(c.timeline.candles[c.t1],close=1.1101000000000003)
    else:
        with c.factory() as session:
            row = session.get(SavedStrategy,c.strategy_id)
            changed = copy.deepcopy(row.definition_json)
            changed['tp2']['value'] = 2.0001
            row.definition_json = changed
            session.commit()
    result = c.build()
    assert not result['studio_live_ready']
    assert result['reason'] == ('EXECUTION_PRICE_DECIMAL_LOSS' if bad_source else 'BROKER_PRICE_PRECISION_INVALID')
    with c.factory() as session:
        assert session.query(StrategySetupLifecycle).count() == 0
    assert c.broker_orders == []
