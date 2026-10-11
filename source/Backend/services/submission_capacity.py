"""Exposure is a projection of the existing execution ledger, never a new ledger.

Studio LIVE is currently single-position per symbol. Its unsupported multi-
position gate is unchanged. A configured combined-risk cap uses account money.
Unknown risk cannot authorize an entry subject to that cap.
"""
from decimal import Decimal
from sqlalchemy import select
from models import TradeSubmissionAttempt, StrategySetupLifecycle
from services.accepted_execution import _snapshot, require_accepted_execution
from live_integrity.metadata import number, decimal_text
from startup_recovery.types import RecoveryError

UNRESOLVED = {'UNRESOLVED','SENT_UNKNOWN','ACCEPTANCE_AMBIGUOUS'}


def read_broker_positions(token):
    from ctrader_account_context import AccountIdentity, pinned_account
    import ctrader_connector as broker
    with pinned_account(AccountIdentity(token.scope.account_id,token.scope.environment)):
        config=broker.get_ctrader_config()
        if str(config.get('account_id'))!=token.scope.account_id or config.get('env')!=token.scope.environment:
            raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
        positions=broker.fetch_ctrader_open_positions(config)
    if not isinstance(positions,list): raise RecoveryError('RECOVERY_BROKER_EXPOSURE_UNAVAILABLE')
    return positions


def reservation(snapshot, attempt, token):
    plan=snapshot['frozen_plan']
    return dict(version=1,account_id=attempt.account_id,environment=snapshot['environment'],
        symbol=attempt.symbol,submission_id=attempt.id,strategy_identity=attempt.strategy_identity,
        frozen_plan_hash=attempt.frozen_plan_hash,intended_risk=decimal_text(plan['intended_risk_dollars']),
        intended_volume=snapshot['volume_units'],position_slots=1,
        combined_risk_dollars=decimal_text(plan['intended_risk_dollars']),epoch=token.epoch)


def exposures(session,account,environment,*,broker_positions,exclude_attempt_id=None):
    result=[]; by_position={}; by_client={}
    rows=session.scalars(select(TradeSubmissionAttempt).where(
        TradeSubmissionAttempt.account_id==account,TradeSubmissionAttempt.mode=='LIVE'))
    for row in rows:
        if row.id==exclude_attempt_id: continue
        intent=row.send_intent or {}
        unresolved=intent.get('state') in UNRESOLVED
        accepted=row.attempt_status in {'ACCEPTED','ACCEPTED_PROTECTION_FAILED'}
        if not unresolved and not accepted: continue
        snapshot=_snapshot(row)
        if snapshot['environment']!=environment: continue
        lifecycle=session.get(StrategySetupLifecycle,row.signal_setup_id)
        if accepted and not unresolved and lifecycle is not None and lifecycle.status=='CLOSED': continue
        position=None
        if accepted and not unresolved:
            position=require_accepted_execution(row)['position_id']
        item=dict(submission_id=row.id,symbol=row.symbol,
            risk=number(snapshot['frozen_plan']['intended_risk_dollars']),position_id=position)
        result.append(item)
        if position:
            if position in by_position: raise RecoveryError('RECOVERY_EXPOSURE_IDENTITY_CONFLICT')
            by_position[position]=item
        if row.broker_client_order_id: by_client[row.broker_client_order_id]=item
    seen=set()
    for position in broker_positions:
        pid=str(position.get('position_id') or position.get('positionId') or '')
        symbol=position.get('symbol')
        if not pid or not symbol or pid in seen: raise RecoveryError('RECOVERY_BROKER_EXPOSURE_INVALID')
        seen.add(pid)
        if pid in by_position:
            if by_position[pid]['symbol']!=symbol:
                raise RecoveryError('RECOVERY_EXPOSURE_IDENTITY_CONFLICT')
            continue
        client=position.get('client_order_id') or position.get('clientOrderId')
        if client and client in by_client:
            item=by_client[client]
            if item['symbol']!=symbol or item['position_id'] not in (None,pid):
                raise RecoveryError('RECOVERY_EXPOSURE_IDENTITY_CONFLICT')
            item['position_id']=pid
            continue
        # No inferred monetary risk from fallback pip/contract assumptions.
        result.append(dict(submission_id=None,symbol=symbol,risk=None,position_id=pid))
    return result


def enforce_capacity(session,snapshot,*,broker_positions,exclude_attempt_id=None):
    plan=snapshot['frozen_plan']; constraints=plan['position_constraints']
    items=exposures(session,snapshot['account_id'],snapshot['environment'],
        broker_positions=broker_positions,exclude_attempt_id=exclude_attempt_id)
    # Existing LIVE rule, not an invented global position limit.
    if plan['combined_risk_inputs']['requires_no_existing_symbol_position'] and any(
        item['symbol']==snapshot['symbol'] for item in items):
        raise RecoveryError('RECOVERY_POSITION_CAPACITY_RESERVED')
    cap=constraints.get('max_combined_open_risk_percent')
    if cap is not None:
        if any(item['risk'] is None for item in items):
            raise RecoveryError('RECOVERY_RISK_CAPACITY_UNVERIFIED')
        total=sum((item['risk'] for item in items),Decimal(0))+number(plan['intended_risk_dollars'])
        if total>number(plan['account_balance'])*number(cap)/100:
            raise RecoveryError('RECOVERY_RISK_CAPACITY_RESERVED')
    return items
