"""Position-bound initial protection; original submission ledger is sole authority.

Two short transactions surround at most one amendment. An unresolved intent is
never a retry token. No current strategy lookup or entry capability exists here.
"""
import copy
import json
import time
from dataclasses import dataclass
from uuid import uuid4
from sqlalchemy import select
from models import TradeSubmissionAttempt, StrategySetupLifecycle
from services.accepted_execution import require_accepted_execution
from live_integrity.binding import fingerprint
from live_integrity.metadata import (number,decimal_text,validate_metadata,value,
    distance_price,digest,QUOTE_MAX_AGE)
from startup_recovery.types import RecoveryError
from startup_recovery.admission import _phase
from startup_recovery.unit_of_work import operation_uow,reconciliation_uow,ordered,require_network_boundary
from startup_recovery.runtime import operation_context,assert_token_usable


def _read_authority(session,token):
    from startup_recovery.store import require_owner
    account,_=require_owner(session,token,lock=False)
    if account.phase not in {'POSITION_MANAGEMENT_READY','NEW_ENTRIES_READY'} or not account.accepted_manifest_hash:
        raise RecoveryError('LIVE_RECOVERY_INCOMPLETE')


@dataclass(frozen=True)
class RepairIntent:
    canonical_json: str

    def data(self):
        return json.loads(self.canonical_json)


def _fresh(stamp,now):
    if not number('-.5') <= number(now)-number(stamp) <= QUOTE_MAX_AGE:
        raise RecoveryError('RECOVERY_PROTECTION_OBSERVATION_STALE')


def _position(accepted,position,now):
    if (position.get('source')!='ProtoOAReconcileRes'
        or any(str(position.get(k))!=str(accepted[k]) for k in
               ('account_id','environment','symbol','symbol_id','position_id','order_id','client_order_id','side'))
        or number(position.get('volume_units'))!=number(accepted['volume_units'])
        or number(position.get('entry'))!=number(accepted['entry'])):
        raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
    _fresh(position.get('observed_at'),now)


def prepare_initial_repair(accepted,broker_position,metadata,quote,*,now=None):
    now=time.time() if now is None else now
    _position(accepted,broker_position,now)
    validate_metadata(metadata,account_id=accepted['account_id'],environment=accepted['environment'],symbol=accepted['symbol'],now=now)
    if metadata['symbol_id']!=accepted['symbol_id'] or value(metadata,'account_type')!='0':
        raise RecoveryError('RECOVERY_PROTECTION_METADATA_CONFLICT')
    if (quote.get('source')!='ProtoOASpotEvent' or any(str(quote.get(k))!=str(accepted[k])
        for k in ('account_id','environment','symbol_id')) or quote.get('symbol_name')!=accepted['symbol']):
        raise RecoveryError('RECOVERY_PROTECTION_QUOTE_CONFLICT')
    for k in ('server_timestamp','bid_timestamp','ask_timestamp','received_at'): _fresh(quote.get(k),now)
    bid,ask=number(quote['bid']),number(quote['ask'])
    if not 0<bid<=ask: raise RecoveryError('RECOVERY_PROTECTION_QUOTE_INVALID')
    sl,tp=number(accepted['intended_sl']),number(accepted['intended_tp'])
    current=broker_position.get('sl'); current_tp=broker_position.get('tp2')
    side=accepted['side']
    if side not in ('BUY','SELL'): raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
    if current is not None and ((side=='BUY' and number(current)>sl) or (side=='SELL' and number(current)<sl)):
        raise RecoveryError('RECOVERY_PROTECTION_STRONGER_STOP')
    if current_tp is not None and number(current_tp)!=tp:
        raise RecoveryError('RECOVERY_PROTECTION_TP_CONFLICT')
    satisfied=current is not None and number(current)==sl and current_tp is not None and number(current_tp)==tp
    tick=number(value(metadata,'tick_size'))
    precision=number(10)**-int(value(metadata,'digits'))
    if tick<=0 or any(x<=0 or x%tick or x%precision for x in (sl,tp)):
        raise RecoveryError('RECOVERY_PROTECTION_PRECISION_VIOLATION')
    if not satisfied:
        sl_ref,tp_ref=(bid,ask) if side=='BUY' else (ask,bid)
        sl_gap,tp_gap=(sl_ref-sl,tp-tp_ref) if side=='BUY' else (sl-sl_ref,tp_ref-tp)
        if sl_gap<=0 or tp_gap<=0 or sl_gap<distance_price(metadata,'sl_distance',sl_ref) or tp_gap<distance_price(metadata,'tp_distance',tp_ref):
            raise RecoveryError('RECOVERY_PROTECTION_DISTANCE_VIOLATION')
    body=dict(version=1,accepted_execution_hash=fingerprint(accepted),
        submission_id=accepted['submission_id'],account_id=accepted['account_id'],environment=accepted['environment'],
        position_id=accepted['position_id'],order_id=accepted['order_id'],symbol=accepted['symbol'],symbol_id=accepted['symbol_id'],
        side=side,volume_units=accepted['volume_units'],frozen_plan_hash=accepted['frozen_plan_hash'],
        sl=decimal_text(sl),tp=decimal_text(tp),metadata=copy.deepcopy(metadata),
        metadata_hash=metadata['metadata_hash'],quote=copy.deepcopy(quote),quote_identity=digest(quote),
        observed_at=broker_position['observed_at'],satisfied=satisfied)
    return RepairIntent(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False))


def _accepted(row,token):
    if row is None: raise RecoveryError('RECOVERY_PROTECTION_ATTEMPT_MISSING')
    accepted=require_accepted_execution(row)
    plan=row.execution_snapshot['frozen_plan']
    if (row.attempt_status!='ACCEPTED' or (row.send_intent or {}).get('state')!='ACCEPTED'
        or (token is not None and (accepted['account_id']!=token.scope.account_id or accepted['environment']!=token.scope.environment))
        or accepted['order_id']!=row.broker_order_id or not accepted['order_id']
        or accepted['client_order_id']!=row.broker_client_order_id
        or accepted['frozen_plan_hash']!=row.frozen_plan_hash
        or accepted['strategy_identity']!=row.strategy_identity
        or number(accepted['intended_sl'])!=number(plan['sl'])
        or number(accepted['intended_tp'])!=number(plan['tp2'])
        or accepted['side']!=row.direction or number(accepted['volume_units'])!=number(row.execution_snapshot['volume_units'])):
        raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
    return accepted


def _rows(session,token,attempt_id):
    if token is not None: _phase(session,token,'AMEND_POSITION')
    pointer=session.get(TradeSubmissionAttempt,attempt_id)
    if pointer is None: raise RecoveryError('RECOVERY_PROTECTION_ATTEMPT_MISSING')
    ordered(session,6,pointer.signal_setup_id)
    lifecycle=session.query(StrategySetupLifecycle).filter_by(setup_id=pointer.signal_setup_id).with_for_update().populate_existing().one_or_none()
    ordered(session,7,attempt_id)
    row=session.query(TradeSubmissionAttempt).filter_by(id=attempt_id).with_for_update().populate_existing().one()
    accepted=_accepted(row,token)
    if (lifecycle is None or lifecycle.broker_position_id!=accepted['position_id']
        or lifecycle.account_id!=accepted['account_id'] or lifecycle.owner_id!=accepted['owner_id']):
        raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
    state=lifecycle.management_state or {}
    if lifecycle.tp1_requested_at is not None or any(state.get(k) is not None for k in ('target_protected_sl','protection_state','tp1_state')):
        raise RecoveryError('RECOVERY_PROTECTION_LATER_INTENT')
    return row,accepted


def _intent(material,accepted):
    try:
        intent=material['intent']
        if (material['kind']!='AMEND_INTENT' or material['version']!=1
            or material['accepted_execution_hash']!=fingerprint(accepted)
            or material['intent_hash']!=fingerprint(intent)
            or intent['accepted_execution_hash']!=fingerprint(accepted)
            or any(intent[k]!=accepted[k] for k in ('submission_id','account_id','environment',
                'position_id','order_id','symbol','symbol_id','side','volume_units','frozen_plan_hash'))
            or number(intent['sl'])!=number(accepted['intended_sl'])
            or number(intent['tp'])!=number(accepted['intended_tp'])):
            raise ValueError()
        return intent
    except (KeyError,TypeError,ValueError):
        raise RecoveryError('RECOVERY_PROTECTION_INTENT_CHANGED') from None


class RepairPermit:
    def __init__(self,factory,token,attempt_id,material,reader):
        self.factory,self.token,self.attempt_id=factory,token,attempt_id
        self.material=copy.deepcopy(material);self.reader=reader
        self.active=True;self.sent=False

    def authorize_frame(self,sock,kind,payload):
        require_network_boundary()
        if not self.active or self.sent or kind!=2110:
            raise RecoveryError('RECOVERY_MUTATION_CAPABILITY_INVALID')
        m=self.material; intent=m['intent']
        if (set(payload)!={'ctidTraderAccountId','positionId','stopLoss','takeProfit'}
            or getattr(sock,'_recovery_environment',None)!=intent['environment']
            or str(payload['ctidTraderAccountId'])!=intent['account_id']
            or str(payload['positionId'])!=intent['position_id']
            or number(payload['stopLoss'])!=number(intent['sl']) or number(payload['takeProfit'])!=number(intent['tp'])):
            raise RecoveryError('RECOVERY_PROTECTION_FRAME_MISMATCH')
        # Read broker facts without DB locks, then recheck the epoch/ledger in a
        # short transaction. Durable unresolved state prohibits ownership handoff.
        assert_token_usable(self.token)
        with self.factory() as s: accepted=_accepted(s.get(TradeSubmissionAttempt,self.attempt_id),self.token)
        _intent(m,accepted)
        facts=self.reader(copy.deepcopy(accepted))
        latest=prepare_initial_repair(accepted,facts['position'],facts['metadata'],facts['quote']).data()
        if latest['metadata_hash']!=intent['metadata_hash']:
            raise RecoveryError('RECOVERY_PROTECTION_METADATA_CHANGED')
        with operation_uow(self.factory,self.token) as uow:
            row,_=_rows(uow.session,self.token,self.attempt_id)
            if row.initial_protection!=m: raise RecoveryError('RECOVERY_PROTECTION_INTENT_CHANGED')
        self.sent=True


def prepare_repair(factory,token,attempt_id,reader):
    assert_token_usable(token)
    with factory() as s:
        _read_authority(s,token)
        row=s.get(TradeSubmissionAttempt,attempt_id);accepted=_accepted(row,token)
        state=(row.initial_protection or {}).get('state')
        if state=='CONFIRMED':
            _intent(row.initial_protection,accepted)
            return None
        if state not in {'UNASSESSED',None}: raise RecoveryError('RECOVERY_PROTECTION_UNRESOLVED')
    require_network_boundary()
    facts=reader(copy.deepcopy(accepted))
    intent=prepare_initial_repair(accepted,facts['position'],facts['metadata'],facts['quote']).data()
    with operation_uow(factory,token) as uow:
        row,current=_rows(uow.session,token,attempt_id)
        if current!=accepted: raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
        if (row.initial_protection or {}).get('state') not in {'UNASSESSED',None}:
            raise RecoveryError('RECOVERY_PROTECTION_UNRESOLVED')
        # Repeat freshness at publication, without changing any frozen level.
        prepare_initial_repair(accepted,facts['position'],facts['metadata'],facts['quote'])
        material=dict(version=1,kind='AMEND_INTENT',state='CONFIRMED' if intent['satisfied'] else 'UNRESOLVED',
            operation_id=uuid4().hex,epoch=token.epoch,recovery_attempt_id=token.attempt_id,
            accepted_execution_hash=row.accepted_execution_hash,intent=intent,intent_hash=fingerprint(intent),created_at=time.time())
        row.initial_protection=material
    return None if intent['satisfied'] else RepairPermit(factory,token,attempt_id,material,reader)


def reconcile_initial_repair(factory,token,attempt_id,reader):
    """Observation only. None token is restart reconciliation, not a capability.

    It uses account coordination but never acquires/replaces the manager. A dead
    predecessor's exact outcome can be recorded before safe ownership handoff.
    """
    with factory() as s:
        if token is not None: _read_authority(s,token)
        row=s.get(TradeSubmissionAttempt,attempt_id);accepted=_accepted(row,token)
        material=copy.deepcopy(row.initial_protection)
    if not material or material.get('kind')!='AMEND_INTENT': raise RecoveryError('RECOVERY_PROTECTION_INTENT_MISSING')
    facts=reader(copy.deepcopy(accepted));position=facts['position']
    _position(accepted,position,time.time())
    if number(position['observed_at'])<number(material['created_at']):
        raise RecoveryError('RECOVERY_PROTECTION_OBSERVATION_STALE')
    intent=_intent(material,accepted)
    confirmed=(position.get('sl') is not None and position.get('tp2') is not None
               and number(position['sl'])==number(intent['sl']) and number(position['tp2'])==number(intent['tp']))
    from startup_recovery.types import AccountScope
    transaction=(operation_uow(factory,token) if token is not None else
                 reconciliation_uow(factory,AccountScope('ctrader',accepted['environment'],accepted['account_id'])))
    with transaction as uow:
        row,_=_rows(uow.session,token,attempt_id)
        if row.initial_protection!=material: raise RecoveryError('RECOVERY_PROTECTION_INTENT_CHANGED')
        if confirmed:
            row.initial_protection={**material,'state':'CONFIRMED','confirmation':copy.deepcopy(position)}
        else:
            row.initial_protection={**material,'last_result':'AMBIGUOUS'}
    return {'state':'CONFIRMED' if confirmed else 'UNRESOLVED'}


def repair_accepted_position(factory,context,attempt_id,broker_reader,amend_callback):
    token=getattr(context,'token',context)
    permit=prepare_repair(factory,token,attempt_id,broker_reader)
    if permit is None: return {'state':'CONFIRMED'}
    try:
        with operation_context(permit): amend_callback(RepairIntent(json.dumps(permit.material['intent'],sort_keys=True)))
        if not permit.sent: return {'state':'UNRESOLVED'}
        return reconcile_initial_repair(factory,token,attempt_id,broker_reader)
    except Exception:
        # Transaction A is already committed. No retry, rollback, timeout or
        # response label may erase the only evidence of a possible mutation.
        try:
            with operation_uow(factory,token) as uow:
                row,_=_rows(uow.session,token,attempt_id)
                if row.initial_protection==permit.material:
                    row.initial_protection={**permit.material,'last_result':'AMBIGUOUS'}
        except Exception:
            pass  # durable Transaction A remains the authority during DB failure
        return {'state':'UNRESOLVED'}
    finally:
        permit.active=False


def repair_original_protection(factory,context,attempt_id):
    """Explicit integration boundary; scheduling is the separate Task 5."""
    from ctrader_connector import read_original_position_protection,amend_original_position_protection
    return repair_accepted_position(factory,context,attempt_id,
        read_original_position_protection,amend_original_position_protection)
