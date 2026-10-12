"""Durable exposure accounting, not a second capacity ledger."""
import copy
import importlib
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pytest
from models import TradeSubmissionAttempt
from test_strategy_live_version_binding import case
from test_recovery_acceptance_crash import prepare
from test_recovery_accepted_execution import evidence
from startup_recovery.runtime import caller_token
from test_recovery_store import db, store_api


def api():
    try: return importlib.import_module('services.submission_capacity')
    except ModuleNotFoundError: pytest.fail('durable capacity accounting missing')


def reserved(case):
    from services.submission_intent import prepare_entry
    factory,payload,key,token=prepare(case)
    permit=prepare_entry(factory,token,key,payload,'owner-A')
    return factory,payload,key,token,permit.material['snapshot']


def test_unresolved_consumes_symbol_slot_and_survives_new_session(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    # New engine/pool and sessions; no reservation object or manager cache is
    # carried over from the process which committed the intent.
    engine=create_engine(f.kw['bind'].url)
    restarted=sessionmaker(bind=engine)
    for _ in range(2):
        with restarted() as s:
            with pytest.raises(RuntimeError,match='POSITION_CAPACITY'):
                m.enforce_capacity(s,snapshot,broker_positions=[])
    engine.dispose()


def test_committed_intent_contains_exact_capacity_identity(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=k).one()
        assert row.send_intent['capacity_reservation']=={
            'version':1,'account_id':'account-A','environment':'demo','symbol':'EURUSD',
            'submission_id':row.id,'strategy_identity':row.strategy_identity,
            'frozen_plan_hash':row.frozen_plan_hash,'intended_risk':'100','intended_volume':'20000',
            'position_slots':1,'combined_risk_dollars':'100','epoch':t.epoch}


def test_other_symbol_allowed_without_account_risk_cap(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    other=copy.deepcopy(snapshot);other['symbol']='XAUUSD';other['frozen_plan']['symbol']='XAUUSD'
    with f() as s: assert len(m.enforce_capacity(s,other,broker_positions=[]))==1


def test_other_symbol_still_consumes_configured_combined_risk(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    other=copy.deepcopy(snapshot);other['symbol']='XAUUSD';other['frozen_plan']['symbol']='XAUUSD'
    other['frozen_plan']['position_constraints']['max_combined_open_risk_percent']=1.5
    with f() as s:
        with pytest.raises(RuntimeError,match='RISK_CAPACITY'):
            m.enforce_capacity(s,other,broker_positions=[])


@pytest.mark.parametrize('state',['UNRESOLVED','SENT_UNKNOWN','ACCEPTANCE_AMBIGUOUS'])
def test_age_and_changed_epoch_do_not_release(case,state):
    m=api();f,p,k,t,snapshot=reserved(case)
    with f.begin() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=k).one()
        row.send_intent={**row.send_intent,'state':state,'epoch':999}
        row.request_started_at-=timedelta(days=400)
    with f() as s:
        with pytest.raises(RuntimeError,match='POSITION_CAPACITY'):
            m.enforce_capacity(s,snapshot,broker_positions=[])


def test_rejection_releases_but_acceptance_transfers_without_double_count(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    from services.submission_intent import finish_entry
    observed=evidence()
    with f() as s: observed['client_order_id']=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=k).one().broker_client_order_id
    finish_entry(f,t,k,'ACCEPTED',observed)
    broker=[{'position_id':'202','symbol':'EURUSD'}]
    with f() as s:
        exposures=m.exposures(s,snapshot['account_id'],snapshot['environment'],broker_positions=broker)
        assert len(exposures)==1
        assert exposures[0]['risk']==Decimal('100')
        with pytest.raises(RuntimeError,match='POSITION_CAPACITY'):m.enforce_capacity(s,snapshot,broker_positions=broker)
    other=copy.deepcopy(snapshot);other['symbol']='XAUUSD';other['frozen_plan']['symbol']='XAUUSD'
    other['frozen_plan']['position_constraints']['max_combined_open_risk_percent']=2
    with f() as s:
        # Exactly 100 existing + 100 proposed, not 100 ledger + 100 broker + 100.
        assert len(m.enforce_capacity(s,other,broker_positions=broker))==1
        with pytest.raises(RuntimeError,match='EXPOSURE_IDENTITY_CONFLICT'):
            m.enforce_capacity(s,other,broker_positions=[{'position_id':'202','symbol':'XAUUSD'}])


def test_confirmed_rejection_releases_reservation(case):
    m=api();f,p,k,t,snapshot=reserved(case)
    from services.submission_intent import finish_entry
    with f() as s: client=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=k).one().broker_client_order_id
    proof={'mode':'demo','raw':{'ctidTraderAccountId':'account-A','executionType':'ORDER_REJECTED',
        'order':{'clientOrderId':client,'tradeData':{'symbolId':snapshot['symbol_id']}}}}
    finish_entry(f,t,k,'DEFINITELY_REJECTED',proof)
    with f() as s: assert m.enforce_capacity(s,snapshot,broker_positions=[])==[]


@pytest.mark.parametrize('proof',[None,{}, {'mode':'demo','raw':{'errorCode':'TIMEOUT'}}])
def test_unproven_rejection_cannot_release_capacity(case,proof):
    m=api();f,p,k,t,snapshot=reserved(case)
    from services.submission_intent import finish_entry
    with pytest.raises(RuntimeError,match='REJECTION_UNVERIFIED'):
        finish_entry(f,t,k,'DEFINITELY_REJECTED',proof)
    with f() as s:
        with pytest.raises(RuntimeError,match='POSITION_CAPACITY'):
            m.enforce_capacity(s,snapshot,broker_positions=[])


def test_uncommitted_intent_rollback_leaves_no_reservation(case):
    m=api();f,p,k,t=prepare(case)
    from services.accepted_execution import capture_execution_snapshot
    snapshot=capture_execution_snapshot(p)
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=k).one()
        row.execution_snapshot=snapshot;row.send_intent={'state':'UNRESOLVED'}
        s.flush();s.rollback()
    with f() as s: assert m.enforce_capacity(s,snapshot,broker_positions=[])==[]


def test_two_postgres_workers_cannot_reserve_same_symbol(case,db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from models import RecoveryAccount,RecoveryAttempt,StrategySetupLifecycle,ExecutionProtocolState
    from services.strategy_studio_models import SavedStrategy
    from services.submission_reservation import install_reservation_guard
    from services.submission_intent import prepare_entry
    from services.trade_submission_service import claim_strategy_submission
    source,payload=case
    token=caller_token()
    with source() as s,db.begin() as target:
        install_reservation_guard(target.connection())
        for model in (RecoveryAccount,RecoveryAttempt,SavedStrategy,ExecutionProtocolState,StrategySetupLifecycle):
            for row in s.query(model).all():
                fields={c.name:copy.deepcopy(getattr(row,c.name)) for c in model.__table__.columns}
                # SQLite strips timezone information. Restore its documented UTC
                # representation before importing into PostgreSQL timestamptz;
                # otherwise the server's local timezone changes the saved version.
                for column in model.__table__.columns:
                    value=fields[column.name]
                    if isinstance(value,datetime) and value.tzinfo is None and getattr(column.type,'timezone',False):
                        fields[column.name]=value.replace(tzinfo=timezone.utc)
                target.add(model(**fields))
            target.flush()
        row=target.get(StrategySetupLifecycle,'setup-A')
        fields={c.name:copy.deepcopy(getattr(row,c.name)) for c in StrategySetupLifecycle.__table__.columns}
        fields['setup_id']='setup-B';target.add(StrategySetupLifecycle(**fields))
    second=copy.deepcopy(payload);second['studio_setup_id']='setup-B'
    claims=[]
    for p in (payload,second):
        result=claim_strategy_submission(p['studio_setup_id'],'account-A','EURUSD','BUY',p,
            owner_id='owner-A',strategy_id='saved-A',session_factory=db)
        assert result['ok'],result
        claims.append(result)
    barrier=Barrier(2)
    def read(_): barrier.wait(3);return []
    monkeypatch.setattr('services.submission_capacity.read_broker_positions',read)
    def worker(args):
        p,c=args
        try:
            prepare_entry(db,token,c['idempotency_key'],p,'owner-A')
            return 'reserved'
        except RuntimeError as exc:return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(worker,zip((payload,second),claims)))
    assert results.count('reserved')==1,results
    assert any(r in {'RECOVERY_ACCOUNT_BUSY','RECOVERY_POSITION_CAPACITY_RESERVED'} for r in results),results
    with db() as s:
        assert sum(bool(row.send_intent) for row in s.query(TradeSubmissionAttempt))==1
