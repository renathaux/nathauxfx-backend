"""Pure original-plan capture and original-ledger acceptance recording.

No current strategy, local file, transport or recovery outcome ledger is read.
Transactions are owned by the caller. Missing history is a denial, not a default.
"""
import copy
from datetime import datetime, timezone
from sqlalchemy import select, or_
from models import TradeSubmissionAttempt, StrategySetupLifecycle
from live_integrity.binding import require_binding, fingerprint, BindingError
from live_integrity.metadata import number, decimal_text
from startup_recovery.types import RecoveryError


def capture_execution_snapshot(payload):
    binding = payload.get('studio_binding')
    try:
        identity = require_binding(binding)
        plan = binding['frozen_plan']
        metadata = plan['broker_metadata']
        broker, environment, account = plan['account_scope'].split(':',2)
        units = number(payload['volume_units'])
        if (broker != 'CTRADER' or environment not in {'LIVE','DEMO'} or units <= 0
            or units*100 != (units*100).to_integral_value()
            or metadata['account_id'] != account or metadata['environment'] != environment.lower()
            or metadata['symbol_name'] != plan['symbol'] or not metadata['symbol_id']
            or payload['symbol'] != plan['symbol'] or payload['action'] != plan['side']
            or any(number(payload[k]) != number(plan[k]) for k in ('entry','sl','tp2'))
            or identity['owner_id'] != payload['studio_owner_id']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, BindingError):
        raise RecoveryError('RECOVERY_EXECUTION_IDENTITY_UNVERIFIED') from None
    return copy.deepcopy(dict(version=1, account_id=account, environment=environment.lower(),
        symbol=plan['symbol'], symbol_id=metadata['symbol_id'], side=plan['side'],
        strategy_identity=identity, frozen_plan=plan, frozen_plan_hash=binding['frozen_plan_hash'],
        volume_units=decimal_text(units), volume_protocol_cents=decimal_text(units*100)))


def _snapshot(attempt):
    snapshot = attempt.execution_snapshot
    try:
        if (not isinstance(snapshot,dict) or snapshot['version'] != 1
            or snapshot['strategy_identity'] != attempt.strategy_identity
            or snapshot['frozen_plan_hash'] != attempt.frozen_plan_hash
            or fingerprint(snapshot['frozen_plan']) != attempt.frozen_plan_hash
            or snapshot['account_id'] != attempt.account_id
            or snapshot['symbol'] != attempt.symbol or snapshot['side'] != attempt.direction
            or snapshot['strategy_identity']['owner_id'] != attempt.owner_id):
            raise ValueError()
        require_binding(dict(strategy_identity=snapshot['strategy_identity'],
            frozen_plan=snapshot['frozen_plan'], frozen_plan_hash=snapshot['frozen_plan_hash']))
    except (KeyError, TypeError, ValueError):
        raise RecoveryError('RECOVERY_ACCEPTED_IDENTITY_UNVERIFIED') from None
    return snapshot


def record_accepted_execution(session, attempt_id, evidence):
    from startup_recovery.unit_of_work import ordered
    query=select(TradeSubmissionAttempt).where(TradeSubmissionAttempt.id == attempt_id)
    if ordered(session,7,attempt_id): query=query.with_for_update()
    attempt = session.execute(query
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if attempt is None:
        raise RecoveryError('RECOVERY_ACCEPTED_IDENTITY_UNVERIFIED')
    snapshot = _snapshot(attempt)
    try:
        if (any(evidence[k] != snapshot[k] for k in ('account_id','environment','symbol','symbol_id','side'))
            or evidence['client_order_id'] != attempt.broker_client_order_id
            or not evidence['position_id'] or not evidence['order_id'] or number(evidence['entry']) <= 0
            or number(evidence['volume_units']) != number(snapshot['volume_units'])
            or attempt.request_started_at is None):
            raise ValueError()
        timestamp = evidence.get('accepted_at')
        if timestamp is not None and datetime.fromisoformat(timestamp.replace('Z','+00:00')).tzinfo is None:
            raise ValueError()
        accepted = dict(version=1, submission_id=attempt.id, submission_key=attempt.idempotency_key,
            client_order_id=attempt.broker_client_order_id, owner_id=attempt.owner_id,
            account_id=snapshot['account_id'], environment=snapshot['environment'],
            symbol=snapshot['symbol'], symbol_id=snapshot['symbol_id'], side=snapshot['side'],
            position_id=str(evidence['position_id']), order_id=str(evidence['order_id']) if evidence.get('order_id') else None,
            deal_id=str(evidence['deal_id']) if evidence.get('deal_id') else None,
            entry=decimal_text(evidence['entry']), volume_units=decimal_text(evidence['volume_units']),
            accepted_at=timestamp, strategy_identity=snapshot['strategy_identity'],
            frozen_plan_hash=snapshot['frozen_plan_hash'], execution_snapshot_hash=fingerprint(snapshot),
            intended_sl=decimal_text(snapshot['frozen_plan']['sl']), intended_tp=decimal_text(snapshot['frozen_plan']['tp2']))
    except (KeyError, TypeError, ValueError):
        raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT') from None
    # Account coordination is held by both result and reconciliation callers.
    # A broker identity already owned by another ledger operation is ambiguous,
    # not permission to consume another setup or release its reservation.
    conflict=session.scalar(select(TradeSubmissionAttempt.id).where(
        TradeSubmissionAttempt.account_id==attempt.account_id,
        TradeSubmissionAttempt.id!=attempt.id,
        or_(TradeSubmissionAttempt.broker_position_id==accepted['position_id'],
            TradeSubmissionAttempt.broker_order_id==accepted['order_id'])).limit(1))
    if conflict is not None:
        raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT')
    hashed = fingerprint(accepted)
    if attempt.accepted_execution is not None:
        if attempt.accepted_execution_hash != hashed or attempt.accepted_execution != accepted:
            raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT')
        return copy.deepcopy(accepted)
    if attempt.broker_position_id and attempt.broker_position_id != accepted['position_id']:
        raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT')
    attempt.accepted_execution, attempt.accepted_execution_hash = accepted, hashed
    attempt.broker_position_id, attempt.broker_order_id = accepted['position_id'], accepted['order_id']
    session.flush()
    return copy.deepcopy(accepted)


def acceptance_observation(result, snapshot):
    """Parse actual fill evidence; never substitute requested price or volume."""
    try:
        raw = result['raw']
        position, order = raw['position'], raw['order']
        trade = position['tradeData']
        side = {1: 'BUY', 2: 'SELL', 'BUY': 'BUY', 'SELL': 'SELL'}[trade['tradeSide']]
        if (raw['executionType'] != 'ORDER_FILLED'
            or str(raw['ctidTraderAccountId']) != snapshot['account_id']
            or result['mode'] != snapshot['environment']
            or trade['symbolId'] != snapshot['symbol_id']):
            raise ValueError()
        cents = number(trade['volume'])
        if cents <= 0 or cents != cents.to_integral_value():
            raise ValueError()
        return dict(account_id=str(raw['ctidTraderAccountId']), environment=result['mode'],
            symbol=snapshot['symbol'], symbol_id=trade['symbolId'], side=side,
            client_order_id=order['clientOrderId'], position_id=str(position['positionId']),
            order_id=str(order['orderId']), entry=decimal_text(position['price']),
            volume_units=decimal_text(cents / 100))
    except (KeyError, TypeError, ValueError):
        raise RecoveryError('RECOVERY_ACCEPTANCE_EVIDENCE_MISSING') from None


def reconcile_accepted_execution(session, attempt_id, broker_evidence):
    """Caller-owned reconciliation transaction; no send or ownership acquisition.

    A complete authoritative observation must contain exactly one matching
    original client reference. Absence never authorizes replay or rejection.
    """
    from startup_recovery.unit_of_work import ordered
    pointer=session.get(TradeSubmissionAttempt,attempt_id)
    if pointer is None or not isinstance(broker_evidence,dict) or broker_evidence.get('complete') is not True:
        raise RecoveryError('RECOVERY_RECONCILIATION_INCOMPLETE')
    query=select(StrategySetupLifecycle).where(StrategySetupLifecycle.setup_id==pointer.signal_setup_id)
    if ordered(session,6,pointer.signal_setup_id): query=query.with_for_update()
    lifecycle=session.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    query=select(TradeSubmissionAttempt).where(TradeSubmissionAttempt.id==attempt_id)
    if ordered(session,7,attempt_id): query=query.with_for_update()
    attempt=session.execute(query.execution_options(populate_existing=True)).scalar_one()
    _snapshot(attempt)
    intent=attempt.send_intent
    if (lifecycle is None or not isinstance(intent,dict) or intent.get('state')!='UNRESOLVED'
        or intent.get('submission_id')!=attempt.id
        or intent.get('client_order_id')!=attempt.broker_client_order_id):
        raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
    positions=broker_evidence.get('positions')
    if not isinstance(positions,(list,tuple)) or any(not isinstance(p,dict) for p in positions):
        raise RecoveryError('RECOVERY_RECONCILIATION_INCOMPLETE')
    matching=[p for p in positions if p.get('client_order_id')==attempt.broker_client_order_id]
    if len(matching)!=1:
        raise RecoveryError('RECOVERY_RECONCILIATION_AMBIGUOUS')
    accepted=record_accepted_execution(session,attempt_id,matching[0])
    attempt.attempt_status='ACCEPTED';attempt.reconciliation_status='MATCHED'
    attempt.send_intent={**intent,'state':'ACCEPTED'}
    attempt.initial_protection=dict(version=1,state='UNASSESSED',accepted_execution_hash=attempt.accepted_execution_hash)
    attempt.reconciled_at=attempt.updated_at=datetime.now(timezone.utc)
    lifecycle.status='CONSUMED';lifecycle.broker_position_id=accepted['position_id']
    lifecycle.initial_volume_units=int(number(accepted['volume_units']))
    return copy.deepcopy(accepted)


def require_accepted_execution(attempt):
    snapshot = _snapshot(attempt)
    accepted = attempt.accepted_execution
    if (not isinstance(accepted, dict) or not attempt.accepted_execution_hash
        or fingerprint(accepted) != attempt.accepted_execution_hash
        or accepted.get('execution_snapshot_hash') != fingerprint(snapshot)
        or accepted.get('submission_id') != attempt.id
        or accepted.get('position_id') != attempt.broker_position_id):
        raise RecoveryError('RECOVERY_ACCEPTED_IDENTITY_UNVERIFIED')
    return copy.deepcopy(accepted)
