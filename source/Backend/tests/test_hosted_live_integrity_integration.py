"""Real owner DB and evaluator; only market/broker reads are controlled fixtures."""
import copy
import hashlib
import json
import time
from datetime import datetime,timezone

import pandas as pd
import pytest
from sqlalchemy import inspect,text

from test_live_integrity_read_store import pg


@pytest.fixture
def current_case(pg,monkeypatch):
    from db import Base
    from models import RuntimeSetting,StrategyStudioLiveState
    from services.strategy_studio_models import SavedStrategy,StrategyStudioSelection
    from sqlalchemy.orm import Session
    from live_integrity.schema import normalize_definition
    from live_integrity.market_facts import MarketFactsTimeline
    from live_integrity import verifier,metadata
    from test_strategy_engine_evaluator import definition,candle,event,T0
    from test_broker_execution_metadata import raw_case
    Base.metadata.create_all(pg)
    owner='owner:fixture@example.invalid'; strategy_id='verifier-strategy'
    config=normalize_definition(definition())
    now=datetime.now(timezone.utc)
    with Session(pg) as session:
        session.merge(SavedStrategy(strategy_id=strategy_id,owner_id=owner,name='Verifier fixture',schema_version=1,definition_json=config,created_at=now,updated_at=now))
        session.flush()
        session.merge(StrategyStudioSelection(owner_id=owner,strategy_id=strategy_id,activated_at=now,updated_at=now))
        session.merge(StrategyStudioLiveState(owner_id=owner,enabled=True,enabled_strategy_id=strategy_id,enabled_at=now,updated_at=now))
        session.merge(RuntimeSetting(setting_name='ctrader_active_account',setting_value=json.dumps({'account_id':'7','env':'demo'}),updated_at=now,updated_by='fixture'))
        session.commit()
    timeline=MarketFactsTimeline(candles={T0:candle(T0,1.099,1.101,1.098,1.1001)},events={T0:event(invalidation=1.0951,trigger_close=1.1001)},trends={},timestamps=[T0],trading_swings=[])
    bundle={'5m':pd.DataFrame({'Close':[1.1001]},index=[T0])}
    monkeypatch.setattr(verifier,'build_market_facts',lambda *args:copy.deepcopy(timeline))
    monkeypatch.setattr(verifier.snapshots,'market',lambda *args:(copy.deepcopy(bundle),[]))
    monkeypatch.setattr(verifier.snapshots,'credentials',lambda *args:{'fixture':True})
    state={'quote_age':0,'changed_metadata':False,'opened':0,'closed':0}
    class Broker:
        def __init__(self,*args):pass
        def __enter__(self):state['opened']+=1;return self
        def __exit__(self,*args):state['closed']+=1
        def account_state(self):return dict(balance='10000',positions=[],orders=[])
        def metadata(self,symbol):
            light,full,trader,assets=raw_case(symbol)
            if state['changed_metadata'] and state.get('metadata_reads',0):full['minVolume']=200000
            state['metadata_reads']=state.get('metadata_reads',0)+1
            return metadata.normalize_symbol_metadata('7','demo',symbol,light,full,trader,assets)
        def quote(self,record):
            stamp=time.time()-state['quote_age']
            return dict(account_id='7',environment='demo',symbol_id=1,symbol_name='EURUSD',bid='1.1000',ask='1.1001',server_timestamp=stamp,bid_timestamp=stamp,ask_timestamp=stamp,received_at=stamp,source='ProtoOASpotEvent')
    return dict(pg=pg,owner=owner,strategy_id=strategy_id,config=config,timeline=timeline,bundle=bundle,Broker=Broker,state=state)


def database_digest(engine):
    result={}
    with engine.connect() as c:
        for name in inspect(engine).get_table_names():
            rows=c.execute(text('SELECT * FROM "'+name+'"')).fetchall()
            result[name]=hashlib.sha256(repr(sorted(repr(tuple(r)) for r in rows)).encode()).hexdigest()
    return result


def test_real_owner_evaluator_intent_no_database_or_shared_mutations(current_case):
    from live_integrity import verifier,evaluator,market_facts
    from live_integrity.read_store import read_only,single_flight
    import api,ctrader_connector
    c=current_case
    before=database_digest(c['pg'])
    originals=copy.deepcopy((c['config'],c['timeline'].__dict__,c['bundle']))
    shared=copy.deepcopy((evaluator.PIP_SIZE,market_facts.POINT_SIZE,api.LIVE_AUTO_TRADE_ENABLED,api.LIVE_ACCOUNT_STATE,api.LIVE_ACTIVE_ORDERS,ctrader_connector.CTRADER_CANDLE_CACHE))
    with read_only(c['pg']) as conn:
        with single_flight(conn):
            result=verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])
    assert result['snapshot_integrity']=='PASS'
    assert result['decision']=='WOULD_BLOCK'  # Full production admission is not inferred.
    assert result['REAL_ORDER_DISPATCH_AVAILABLE'] is False
    assert result['setup_persisted'] is False
    assert result['order_intent']['entry']=='1.1001'
    assert result['order_intent']['tp2']=='1.1101'
    assert result['order_intent']['volume_protocol_cents']==2000000
    assert database_digest(c['pg'])==before
    assert c['config']==originals[0] and c['timeline'].__dict__==originals[1]
    assert c['bundle']['5m'].equals(originals[2]['5m'])
    assert (evaluator.PIP_SIZE,market_facts.POINT_SIZE,api.LIVE_AUTO_TRADE_ENABLED,api.LIVE_ACCOUNT_STATE,api.LIVE_ACTIVE_ORDERS,ctrader_connector.CTRADER_CANDLE_CACHE)==shared
    assert c['state']['opened']==c['state']['closed']==1


@pytest.mark.parametrize('case',['stale_quote','metadata_change','unsupported'])
def test_controlled_blocks_without_strategy_or_lifecycle_mutation(current_case,case):
    from live_integrity import verifier
    from live_integrity.read_store import read_only
    c=current_case
    if case=='stale_quote':c['state']['quote_age']=4
    if case=='metadata_change':c['state']['changed_metadata']=True
    if case=='unsupported':
        changed=copy.deepcopy(c['config']);changed['risk']['max_concurrent_positions']=3
        changed['risk']['max_combined_open_risk_percent']=3
        with c['pg'].begin() as conn:
            conn.execute(text('UPDATE saved_strategies SET definition_json=CAST(:definition AS JSON) WHERE strategy_id=:id'),{'definition':json.dumps(changed),'id':c['strategy_id']})
    before=database_digest(c['pg'])
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match={'stale_quote':'QUOTE_STALE','metadata_change':'METADATA_CHANGED','unsupported':'MULTI_POSITION_NOT_SUPPORTED'}[case]):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])
    assert database_digest(c['pg'])==before
    assert c['state']['opened']==c['state']['closed']


@pytest.mark.parametrize('fault',['owner','config','plan','incomplete_metadata'])
def test_final_projection_blocks_identity_and_plan_substitution(current_case,fault):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        saved=snapshots.saved(conn,c['owner'],c['strategy_id'])
        selection=snapshots.selected(conn)
        broker=c['Broker']()
        record=broker.metadata('EURUSD')
        _,binding=verifier.evaluate_snapshot(saved,selection,'EURUSD',c['bundle'],[],10000,record)
        current=copy.deepcopy(saved)
        owner=c['owner']
        if fault=='owner':owner='owner:foreign@example.invalid'
        if fault=='config':current['identity']['config_hash']='0'*64
        if fault=='plan':binding['frozen_plan']['sl']=1.0952
        if fault=='incomplete_metadata':record['fields']['min_volume_units']['authoritative']=False
        before=database_digest(c['pg'])
        with pytest.raises(ValueError):
            verifier.project_verified(binding,current,record,broker.quote(record),owner=owner)
        assert database_digest(c['pg'])==before


def test_intent_binds_complete_frozen_plan_not_only_prices(current_case):
    from live_integrity import verifier,snapshots
    from live_integrity.binding import fingerprint
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        saved=snapshots.saved(conn,c['owner'],c['strategy_id'])
        broker=c['Broker'](); record=broker.metadata('EURUSD')
        _,binding=verifier.evaluate_snapshot(saved,snapshots.selected(conn),'EURUSD',c['bundle'],[],10000,record)
        intent,_=verifier.project_verified(binding,saved,record,broker.quote(record),owner=c['owner'])
        assert intent.frozen_plan_hash==binding['frozen_plan_hash']
        from live_integrity import order_intent
        from services.broker_execution_metadata import build_order_payload
        from unittest.mock import patch
        # Production serializer must consume precisely the same immutable intent.
        captured=[]; original=order_intent.project_order_intent
        def capture(*args,**kwargs):
            result=original(*args,**kwargs); captured.append(result); return result
        p=binding['frozen_plan']
        exact=dict(symbol=p['symbol'],action=p['side'],entry=p['entry'],sl=p['sl'],tp1=p['tp1'],tp2=p['tp2'],volume=.2,volume_units=20000)
        from live_integrity.metadata import validate_new_order
        validation=validate_new_order(exact,record,record,broker.quote(record),account_id='7',environment='demo')
        with patch.object(order_intent,'project_order_intent',capture):
            build_order_payload(exact,record,validation,client_order_id='fixture',broker_label='fixture',broker_comment='fixture',frozen_binding=binding)
        assert captured[0].canonical_bytes()==intent.canonical_bytes()
        changed=copy.deepcopy(binding)
        changed['frozen_plan']['combined_risk_inputs']['diagnostic_marker']='different-preserved-input'
        changed['frozen_plan_hash']=fingerprint(changed['frozen_plan'])
        other,_=verifier.project_verified(changed,saved,record,broker.quote(record),owner=c['owner'])
        assert other.order_intent_hash!=intent.order_intent_hash


def test_existing_consumed_setup_cannot_be_recreated(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case
    monkeypatch.setattr(snapshots,'existing_setup',lambda *args:{'status':'CONSUMED'},raising=False)
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match='SETUP_NOT_ELIGIBLE'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])


def test_generation_cutover_during_evaluation_blocks(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case; calls=[]
    def generation(*args):
        calls.append(1)
        return [] if len(calls)==1 else [{'generation':2}]
    monkeypatch.setattr(snapshots,'generation_state',generation,raising=False)
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match='GENERATION_CHANGED'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])


def test_missing_production_admission_state_never_claims_final_authorization(current_case):
    from live_integrity import verifier
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        result=verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])
    assert result['decision']=='WOULD_BLOCK'
    assert result['block_reasons']==['PRODUCTION_ADMISSION_STATE_UNAVAILABLE']
    assert result['final_integrity_authorization']=='NOT_PROVEN'
    assert result['order_intent']['entry']=='1.1001'


def test_existing_frozen_metadata_is_not_upgraded(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.binding import fingerprint
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        saved=snapshots.saved(conn,c['owner'],c['strategy_id']); selection=snapshots.selected(conn)
        record=c['Broker']().metadata('EURUSD')
        _,binding=verifier.evaluate_snapshot(saved,selection,'EURUSD',c['bundle'],[],10000,record)
        binding['frozen_plan']['broker_metadata']['retrieved_at']=time.time()-3600
        from live_integrity.metadata import digest
        stale=binding['frozen_plan']['broker_metadata']
        stale['retrieval_id']=digest(dict(metadata_hash=stale['metadata_hash'],retrieved_at=stale['retrieved_at']))
        binding['frozen_plan_hash']=fingerprint(binding['frozen_plan'])
        row=dict(owner_id=c['owner'],strategy_id=c['strategy_id'],account_id='7',account_scope=selection['scope'],symbol='EURUSD',direction='BUY',status='ELIGIBLE',entry_binding=binding,definition_snapshot=c['config'])
        monkeypatch.setattr(snapshots,'existing_setup',lambda *args:copy.deepcopy(row))
        with pytest.raises(ValueError,match='METADATA_STALE'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])


def test_confirmation_requires_persisted_ready_watermark(current_case):
    from live_integrity import snapshots
    from live_integrity.read_store import read_only
    c=current_case
    generations=[dict(root_key='verifier-proof',timeframe='5m',active_generation=2,storage_key='verifier-proof',status='READY')]
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match='CONFIRMATION_NOT_DURABLE'):
            snapshots.durable_confirmation(conn,generations,pd.Timestamp('2026-10-01T10:00Z'))


def test_complete_owner_bound_read_only_admission_reaches_nonexecuting_allow(current_case,tmp_path):
    from live_integrity import verifier
    from live_integrity.read_store import read_only
    from test_live_integrity_admission import evidence
    c=current_case
    with c['pg'].begin() as conn:
        conn.execute(text("INSERT INTO runtime_settings(setting_name,setting_value,updated_at,updated_by) VALUES ('live_auto_trade_enabled','true',NOW(),'fixture') ON CONFLICT(setting_name) DO UPDATE SET setting_value='true'"))
        conn.execute(text("INSERT INTO runtime_settings(setting_name,setting_value,updated_at,updated_by) VALUES ('news_trading_mode','OFF',NOW(),'fixture') ON CONFLICT(setting_name) DO UPDATE SET setting_value='OFF'"))
    before=database_digest(c['pg']); runtime=evidence()
    path=tmp_path/'settings.json'; path.write_text(json.dumps({'risk':runtime.pop('risk_settings')}))
    runtime['risk_settings_path']=str(path)
    original=copy.deepcopy(runtime)
    with read_only(c['pg']) as conn:
        result=verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',runtime,time.monotonic()+10,reader_factory=c['Broker'])
    assert result['decision']=='WOULD_ALLOW'
    assert result['final_integrity_authorization']=='WOULD_ALLOW'
    assert result['REAL_ORDER_DISPATCH_AVAILABLE'] is False
    assert result['order_intent']['frozen_plan_hash']==result['frozen_plan_hash']
    assert runtime==original and database_digest(c['pg'])==before


def test_existing_definition_snapshot_must_match_binding(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        saved=snapshots.saved(conn,c['owner'],c['strategy_id']); selection=snapshots.selected(conn)
        record=c['Broker']().metadata('EURUSD')
        _,binding=verifier.evaluate_snapshot(saved,selection,'EURUSD',c['bundle'],[],10000,record)
        row=dict(owner_id=c['owner'],strategy_id=c['strategy_id'],account_id='7',account_scope=selection['scope'],symbol='EURUSD',direction='BUY',status='ELIGIBLE',entry_binding=binding,definition_snapshot={})
        monkeypatch.setattr(snapshots,'existing_setup',lambda *args:copy.deepcopy(row))
        with pytest.raises(ValueError,match='STRATEGY_VERSION_CHANGED'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])


@pytest.mark.parametrize('generation',[None,1])
def test_existing_generation_links_cannot_be_missing_or_stale(current_case,generation):
    from live_integrity import snapshots
    from live_integrity.read_store import read_only
    assert hasattr(snapshots,'validate_existing_generations'),'Existing lifecycle generation guard missing'
    head=dict(root_key='proof',timeframe='5m',active_generation=2,activation_watermark=pd.Timestamp('2026-09-01T00:00Z'),status='READY')
    links=[] if generation is None else [dict(root_key='proof',timeframe='5m',generation=generation,event_time=pd.Timestamp('2026-09-02T00:00Z'),confirmation_time=pd.Timestamp('2026-09-02T00:05Z'))]
    with read_only(current_case['pg']) as conn:
        with pytest.raises(ValueError,match='SETUP_GENERATION_INVALID'):
            snapshots.validate_existing_generations(conn,[head],links)


def test_risk_settings_change_during_broker_reads_blocks(current_case,tmp_path,monkeypatch):
    from live_integrity import verifier
    from live_integrity.read_store import read_only
    from test_live_integrity_admission import evidence
    c=current_case; runtime=evidence()
    path=tmp_path/'settings.json';path.write_text(json.dumps({'risk':runtime.pop('risk_settings')}))
    runtime['risk_settings_path']=str(path)
    original=c['Broker'].quote
    def quote(self,record):
        path.write_text(json.dumps({'risk':{'maxDailyLoss':100,'maxWeeklyLoss':None}}))
        return original(self,record)
    monkeypatch.setattr(c['Broker'],'quote',quote)
    before=database_digest(c['pg'])
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match='RUNTIME_IDENTITY_CHANGED'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',runtime,time.monotonic()+10,reader_factory=c['Broker'])
    assert database_digest(c['pg'])==before


def test_existing_generation_links_rechecked_after_validation(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case
    with read_only(c['pg']) as conn:
        saved=snapshots.saved(conn,c['owner'],c['strategy_id']);selection=snapshots.selected(conn)
        record=c['Broker']().metadata('EURUSD')
        _,binding=verifier.evaluate_snapshot(saved,selection,'EURUSD',c['bundle'],[],10000,record)
        row=dict(owner_id=c['owner'],strategy_id=c['strategy_id'],account_id='7',account_scope=selection['scope'],symbol='EURUSD',direction='BUY',status='ELIGIBLE',entry_binding=binding,definition_snapshot=c['config'])
        monkeypatch.setattr(snapshots,'existing_setup',lambda *args:copy.deepcopy(row))
        calls=[]
        def links(*args):
            calls.append(1)
            return [] if len(calls)==1 else [{'unexpected':'new link'}]
        monkeypatch.setattr(snapshots,'existing_generations',links)
        with pytest.raises(ValueError,match='SETUP_GENERATION_INVALID'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])


def test_quote_expiry_during_final_database_rechecks_blocks(current_case,monkeypatch):
    from live_integrity import verifier,snapshots
    from live_integrity.read_store import read_only
    c=current_case; original=snapshots.admission_state; clock=time.time; offset=[0]; calls=[]
    monkeypatch.setattr(time,'time',lambda:clock()+offset[0])
    def policy(*args):
        result=original(*args);calls.append(1)
        if len(calls)==2:offset[0]=3
        return result
    monkeypatch.setattr(snapshots,'admission_state',policy)
    with read_only(c['pg']) as conn:
        with pytest.raises(ValueError,match='BROKER_QUOTE_STALE'):
            verifier.verify(conn,c['owner'],c['strategy_id'],'EURUSD',{'live_auto_enabled':True},time.monotonic()+10,reader_factory=c['Broker'])
