"""Read-only integrity composition; ephemeral artifacts never enter execution DB."""
import copy
import hashlib
import time
from datetime import datetime, timezone
import pandas as pd
from live_integrity import snapshots, metadata, risk_snapshot
from live_integrity.binding import freeze_plan, fingerprint, require_binding
from live_integrity.validation import evaluate_current, validate_identity
from live_integrity.market_facts import build_market_facts
from live_integrity.setup_identity import _pending_identity, _setup_id
from live_integrity.sizing import size,stop_distance_pips
from live_integrity.order_intent import project_order_intent
from live_integrity.ctrader_reader import Reader
from live_integrity.admission import validate_admission
from live_integrity.authority import resolve_execution_authority

REAL_ORDER_DISPATCH_AVAILABLE = False


def evaluate_snapshot(saved,selection,symbol,bundle,generations,balance,record,*,include_confirmation=False):
    """Detached request-local evaluator boundary shared with production evaluation."""
    definition = copy.deepcopy(saved['definition'])
    identity = copy.deepcopy(saved['identity'])
    if int(definition['risk'].get('max_concurrent_positions',1))>1:
        raise ValueError('STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED')
    if symbol not in definition['symbols']:
        raise ValueError('WAIT_STUDIO_SYMBOL_DISABLED')
    timeline = build_market_facts(copy.deepcopy(bundle),symbol,definition['trading_timeframe'],definition['trend']['timeframe'],
        definition.get('structure_timeframe',definition['trading_timeframe']))
    stamps = timeline.timestamps()
    if not stamps:
        raise ValueError('WAIT_STUDIO_HISTORY_UNAVAILABLE')
    stamp,before,result = evaluate_current(definition,timeline,stamps,symbol=symbol,balance=balance)
    if result.signal not in ('BUY','SELL'):
        raise ValueError('WAIT_STUDIO_EVALUATOR')
    facts = _pending_identity(before,timeline,stamp)
    if generations and (not facts['structure_event_time'] or any(pd.Timestamp(facts['structure_event_time'])<=pd.Timestamp(g['activation_watermark']) or stamp<=pd.Timestamp(g['activation_watermark']) for g in generations)):
        raise ValueError('WAIT_STUDIO_GENERATION_HISTORICAL')
    setup = _setup_id(owner_id=identity['owner_id'],strategy_id=identity['strategy_id'],schema_version=definition['schema_version'],
        account_scope=selection['scope'],symbol=symbol,direction=result.signal,structure_event_time=facts['structure_event_time'],
        entry_trigger_time=stamp.isoformat(),broken_level=facts['broken_level'],evaluator_setup_id=result.setup_id,
        generation_bindings=generations,strategy_identity=identity)
    frozen = freeze_plan(definition,result,symbol=symbol,account_balance=float(balance),account_scope=selection['scope'],broker_metadata=record)
    binding = dict(strategy_identity=identity,frozen_plan=frozen,frozen_plan_hash=fingerprint(frozen))
    require_binding(binding)
    return (setup,binding,stamp) if include_confirmation else (setup,binding)


def project_verified(binding,current_saved,current_metadata,quote,*,owner):
    validate_identity(binding,current_saved['identity'],current_saved['definition'],owner)
    p = binding['frozen_plan']
    record = p['broker_metadata']
    risk_percent = p['requested_risk_percent']
    sl_pips = stop_distance_pips(p['entry'],p['sl'],metadata.value(record,'pip_size'))
    risk = size(p['symbol'],p['account_balance'],risk_percent,sl_pips,record,
        maximum_allowed_risk_percent=round(float(risk_percent),4),risk_tolerance_percent=0.0,
        payload_volume_scale={p['symbol']:100})
    if not risk.get('ok'):
        raise ValueError('BROKER_EXECUTABLE_SIZING_BLOCKED')
    exact = dict(symbol=p['symbol'],action=p['side'],entry=p['entry'],sl=p['sl'],tp1=p['tp1'],tp2=p['tp2'],
        volume=risk['lot_size'],volume_units=risk['volume_units'])
    validation = metadata.validate_new_order(exact,record,current_metadata,quote,
        account_id=record['account_id'],environment=record['environment'])
    intent = project_order_intent(exact,record,validation,expected_plan_hash=validation['intent_plan_hash'],frozen_binding=binding)
    return intent,validation


def verify(connection,owner,strategy_id,symbol,runtime,deadline,*,reader_factory=Reader):
    saved = snapshots.saved(connection,owner,strategy_id)
    # This gate precedes metadata/socket activity and never edits v11.
    if int(saved['definition']['risk'].get('max_concurrent_positions',1))>1:
        raise ValueError('STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED')
    if runtime.get('live_auto_enabled') is not True:
        raise ValueError('LIVE_AUTO_DISABLED')
    handoff = snapshots.handoff(connection,owner,strategy_id)
    selection = snapshots.selected(connection)
    policy_state=snapshots.admission_state(connection)
    # File IO stays in the killable worker, never in the application snapshot.
    # Missing private server evidence cannot be replaced by caller risk values.
    runtime=copy.deepcopy(runtime)
    runtime.pop('risk_settings',None)
    risk_state=risk_snapshot.read(runtime['risk_settings_path']) if runtime.get('risk_settings_path') else None
    if risk_state is not None:
        runtime['risk_settings']=risk_state['values']
    authority=resolve_execution_authority(symbol,owner_id=owner,account_scope=selection['scope'],profile=dict(
        enabled=handoff['enabled'],strategy_id=strategy_id,enabled_strategy_id=handoff['enabled_strategy_id'],symbols=saved['definition']['symbols']))
    if authority['source']!='STRATEGY_STUDIO' or authority['strategy_id']!=strategy_id:
        raise ValueError('EXECUTION_AUTHORITY_CHANGED')
    generation_snapshot=snapshots.generation_state(connection,selection['scope'],symbol)
    bundle,generations = snapshots.market(connection,selection['scope'],symbol,datetime.now(timezone.utc))
    with reader_factory(selection['account_id'],selection['environment'],snapshots.credentials(connection),deadline) as broker:
        account = broker.account_state()
        # No assumptions about unknown exposure/open risk; no position writes.
        if account['positions'] or account['orders']:
            raise ValueError('EXISTING_EXPOSURE_REQUIRES_EXECUTION_STATE')
        record = broker.metadata(symbol)
        setup,binding,confirmation = evaluate_snapshot(saved,selection,symbol,bundle,generations,float(account['balance']),record,include_confirmation=True)
        existing=snapshots.existing_setup(connection,setup)
        existing_links=snapshots.existing_generations(connection,setup) if existing is not None else []
        if existing is not None:
            if existing['status']!='ELIGIBLE':
                raise ValueError('STUDIO_SETUP_NOT_ELIGIBLE')
            previous=existing['entry_binding']
            validate_identity(previous,saved['identity'],saved['definition'],owner)
            if fingerprint(existing.get('definition_snapshot'))!=previous['strategy_identity']['config_hash']:
                raise ValueError('STRATEGY_VERSION_CHANGED')
            snapshots.validate_existing_generations(connection,generation_snapshot,existing_links)
            if any(existing[key]!=value for key,value in dict(owner_id=owner,strategy_id=strategy_id,
                    account_id=selection['account_id'],account_scope=selection['scope'],symbol=symbol,
                    direction=binding['frozen_plan']['side']).items()):
                raise ValueError('STRATEGY_OWNER_MISMATCH')
            retained=previous['frozen_plan']['broker_metadata']
            metadata.validate_metadata(retained,account_id=selection['account_id'],environment=selection['environment'],symbol=symbol)
            if retained['metadata_hash']!=record['metadata_hash']:
                raise ValueError('BROKER_METADATA_CHANGED')
            binding['frozen_plan']['broker_metadata']=copy.deepcopy(retained)
            binding['frozen_plan_hash']=fingerprint(binding['frozen_plan'])
            if fingerprint(binding)!=fingerprint(previous):
                raise ValueError('STRATEGY_PLAN_CHANGED')
            binding=copy.deepcopy(previous)
        snapshots.durable_confirmation(connection,generation_snapshot,confirmation)
        current = snapshots.saved(connection,owner,strategy_id)
        if selection!=snapshots.selected(connection) or handoff!=snapshots.handoff(connection,owner,strategy_id):
            raise ValueError('RUNTIME_IDENTITY_CHANGED')
        if account!=broker.account_state():
            raise ValueError('BROKER_ACCOUNT_STATE_CHANGED')
        current_metadata = broker.metadata(symbol)
        quote = broker.quote(current_metadata)
        if generation_snapshot!=snapshots.generation_state(connection,selection['scope'],symbol):
            raise ValueError('STUDIO_GENERATION_CHANGED')
        snapshots.durable_confirmation(connection,generation_snapshot,confirmation)
        if existing!=snapshots.existing_setup(connection,setup):
            raise ValueError('STUDIO_SETUP_STATE_CHANGED')
        if existing is not None:
            if existing_links!=snapshots.existing_generations(connection,setup):
                raise ValueError('STUDIO_SETUP_GENERATION_INVALID')
            snapshots.validate_existing_generations(connection,generation_snapshot,existing_links)
        if policy_state!=snapshots.admission_state(connection):
            raise ValueError('RUNTIME_IDENTITY_CHANGED')
        if risk_state is not None and risk_state!=risk_snapshot.read(runtime['risk_settings_path']):
            raise ValueError('RUNTIME_IDENTITY_CHANGED')
        admission=validate_admission(runtime,news_mode=policy_state['news_mode'],symbol=symbol,
            side=binding['frozen_plan']['side'],fundamental_policy=binding['frozen_plan']['fundamental_policy'],
            now=time.time(),monotonic_now=time.monotonic())
        if admission['ok'] and not policy_state['live_enabled']:
            admission={'ok':False,'reason':'LIVE_AUTO_DISABLED'}
        # Validate freshness after all potentially slow DB/file rechecks, not
        # before them. This is the snapshot decision boundary, never dispatch.
        intent,validation = project_verified(binding,current,current_metadata,quote,owner=owner)
        if time.monotonic()>=deadline:
            raise TimeoutError('DIAGNOSTIC_TIMEOUT')
    identity = dict(binding['strategy_identity']); del identity['owner_id']
    return dict(decision='WOULD_ALLOW' if admission['ok'] else 'WOULD_BLOCK',decision_scope='SNAPSHOT_INTEGRITY_ONLY',block_reasons=[] if admission['ok'] else [admission['reason']],
        REAL_ORDER_DISPATCH_AVAILABLE=False,owner_reference=hashlib.sha256(owner.encode()).hexdigest(),
        strategy=dict(name=saved['name'],**identity),setup_id=setup,setup_persisted=False,
        frozen_plan_hash=binding['frozen_plan_hash'],order_intent=intent.safe_projection(),
        intended_risk=str(binding['frozen_plan']['intended_risk_dollars']),metadata_authority='ALL_REQUIRED_FIELDS_AUTHORITATIVE',
        quote_timestamp=quote['server_timestamp'],quote_freshness='PASS',quote_identity=validation['quote_identity'],
        validation_timestamp=validation['validated_at'],
        precision_validation='PASS',distance_validation='PASS',executable_sizing='PASS',
        snapshot_integrity='PASS',final_integrity_authorization='WOULD_ALLOW' if admission['ok'] else 'NOT_PROVEN',
        durable_claim='NOT_EXERCISED_READ_ONLY',atomic_dispatch='NOT_EXERCISED_READ_ONLY',broker_acceptance='NOT_EXERCISED_READ_ONLY')
