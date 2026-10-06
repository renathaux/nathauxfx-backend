"""Durable pre-send entry intent, followed by an independent result transaction.

No callback is executed inside a transaction. An issued permit cannot be loaded
from the database/reissued: unresolved intent requires reconciliation, not replay.
"""
import copy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from models import TradeSubmissionAttempt, StrategySetupLifecycle, BrokerIntegrationTestSubmission
from startup_recovery.unit_of_work import operation_uow, ordered, require_network_boundary
from startup_recovery.types import RecoveryError
from startup_recovery.store import require_owner
from startup_recovery.admission import entry_gate
from live_integrity.binding import fingerprint
from live_integrity.metadata import number
from services.accepted_execution import capture_execution_snapshot, record_accepted_execution
from services.submission_reservation import install_reservation_guard, reservation_guard_present


def _attempt(session,key):
    query=select(TradeSubmissionAttempt).where(TradeSubmissionAttempt.idempotency_key==key)
    pointer=session.scalar(select(TradeSubmissionAttempt.id).where(TradeSubmissionAttempt.idempotency_key==key))
    if pointer is None: raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
    if ordered(session,7,pointer): query=query.with_for_update()
    row=session.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None: raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
    return row


class EntryPermit:
    def __init__(self,factory,token,key,material):
        self.factory,self.token,self.operation_key=factory,token,key
        self.material=copy.deepcopy(material)
        self.sent=False
        self.active=True

    def authorize_frame(self,sock,kind,payload):
        require_network_boundary()
        if not self.active or self.sent or kind!=2106:
            raise RecoveryError('RECOVERY_MUTATION_CAPABILITY_INVALID')
        m=self.material; plan=m['snapshot']['frozen_plan']
        try:
            expected={
                'ctidTraderAccountId':m['account_id'],'symbolId':m['symbol_id'],
                'volume':m['snapshot']['volume_protocol_cents'],
                'relativeStopLoss':abs(number(plan['entry'])-number(plan['sl']))*100000,
                'relativeTakeProfit':abs(number(plan['tp2'])-number(plan['entry']))*100000}
            if (getattr(sock,'_recovery_environment',None)!=m['environment']
                or any(number(payload[k])!=number(v) for k,v in expected.items())
                or payload.get('tradeSide') not in (m['side'],1 if m['side']=='BUY' else 2)
                or payload.get('orderType') not in ('MARKET',1)
                or payload.get('clientOrderId')!=m['client_order_id']):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise RecoveryError('RECOVERY_ORDER_INTENT_MISMATCH') from None
        # A short final read transaction ends BEFORE raw socket send. The durable
        # unresolved record prevents handoff; connection loss is not a drain.
        with self.factory() as session:
            account,_=require_owner(session,self.token,lock=False)
            row=session.scalar(select(TradeSubmissionAttempt).where(TradeSubmissionAttempt.idempotency_key==self.operation_key))
            if (account.phase!='NEW_ENTRIES_READY' or row is None
                or row.send_intent!=m or row.attempt_status!='SUBMITTING'):
                raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
        self.sent=True


def prepare_entry(factory,token,key,payload,owner):
    from services.strategy_live_binding import lock_saved,validate_bound_payload,dispatch_fingerprint
    from stream_generations import root_key,lock,studio_claim_allowed
    from services.submission_capacity import read_broker_positions,enforce_capacity,reservation
    broker_positions=read_broker_positions(token)
    with operation_uow(factory,token) as uow:
        s=uow.session
        if s.scalar(select(BrokerIntegrationTestSubmission.test_id).where(
            BrokerIntegrationTestSubmission.unresolved_account == token.scope.account_id)):
            raise RecoveryError('RECOVERY_ACCOUNT_OPERATION_UNRESOLVED')
        # Generation locks precede owner/saved/lifecycle locks.
        root=root_key(payload['symbol'],payload['studio_account_scope'])
        lock(s,root,'5m')
        ordered(s,3,'execution_protocol')
        if not reservation_guard_present(s.connection()):
            raise RecoveryError('RECOVERY_RESERVATION_GUARD_MISSING')
        entry_gate(s,token)
        saved=lock_saved(s,owner,payload['studio_strategy_id'])
        ordered(s,6,payload['studio_setup_id'])
        lifecycle=s.query(StrategySetupLifecycle).filter_by(setup_id=payload['studio_setup_id']).with_for_update().populate_existing().one_or_none()
        row=_attempt(s,key)
        if row.send_intent is not None:
            raise RecoveryError('RECOVERY_OPERATION_UNRESOLVED')
        validate_bound_payload(s,lifecycle,payload,owner,saved=saved)
        if not studio_claim_allowed(s,lifecycle):
            raise RecoveryError('RECOVERY_GENERATION_CHANGED')
        if (row.owner_id!=owner or row.account_id!=token.scope.account_id
            or row.signal_setup_id!=lifecycle.setup_id or row.attempt_status!='SUBMITTING'
            or lifecycle.status!='SUBMITTING'
            or row.request_payload_fingerprint!=dispatch_fingerprint(payload)):
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
        snapshot=capture_execution_snapshot(payload)
        if snapshot['environment']!=token.scope.environment:
            raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
        if row.execution_snapshot is not None and row.execution_snapshot!=snapshot:
            raise RecoveryError('RECOVERY_EXECUTION_IDENTITY_UNVERIFIED')
        enforce_capacity(s,snapshot,broker_positions=broker_positions,exclude_attempt_id=row.id)
        row.execution_snapshot=snapshot
        now=datetime.now(timezone.utc)
        material=dict(version=1,kind='SEND_INTENT',state='UNRESOLVED',operation_id=uuid4().hex,
            account_id=row.account_id,environment=token.scope.environment,symbol=row.symbol,
            symbol_id=snapshot['symbol_id'],side=row.direction,submission_id=row.id,
            client_order_id=row.broker_client_order_id,strategy_identity=row.strategy_identity,
            frozen_plan_hash=row.frozen_plan_hash,epoch=token.epoch,boot_id=token.boot_id,
            recovery_attempt_id=token.attempt_id,created_at=now.isoformat(),snapshot=snapshot,
            capacity_reservation=reservation(snapshot,row,token))
        row.send_intent=material
        row.request_started_at=now
        row.reconciliation_status='PENDING'
        row.updated_at=now
    # Only reached after successful COMMIT, never from a rollback/finally block.
    return EntryPermit(factory,token,key,material)


def finish_entry(factory,token,key,outcome,evidence):
    with operation_uow(factory,token) as uow:
        s=uow.session
        require_owner(s,token)
        pointer=s.scalar(select(TradeSubmissionAttempt).where(TradeSubmissionAttempt.idempotency_key==key))
        if pointer is None: raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
        ordered(s,6,pointer.signal_setup_id)
        lifecycle=s.query(StrategySetupLifecycle).filter_by(setup_id=pointer.signal_setup_id).with_for_update().one_or_none()
        row=_attempt(s,key)
        intent=row.send_intent
        if (not intent or intent.get('epoch')!=token.epoch or intent.get('recovery_attempt_id')!=token.attempt_id
            or intent.get('state') not in {'UNRESOLVED','ACCEPTED'}):
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
        if intent.get('state')=='ACCEPTED':
            if outcome!='ACCEPTED': raise RecoveryError('RECOVERY_OUTCOME_UNVERIFIED')
            # A late/repeated exact result acknowledges the same ledger identity;
            # it cannot reset protection state or cause another broker operation.
            return record_accepted_execution(s,row.id,evidence)
        if outcome=='ACCEPTED':
            accepted=record_accepted_execution(s,row.id,evidence)
            row.attempt_status='ACCEPTED'
            row.reconciliation_status='MATCHED'
            row.initial_protection=dict(version=1,state='UNASSESSED',accepted_execution_hash=row.accepted_execution_hash)
            if lifecycle is None: raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
            lifecycle.status='CONSUMED'; lifecycle.broker_position_id=accepted['position_id']
            lifecycle.initial_volume_units=int(number(accepted['volume_units']))
        elif outcome=='DEFINITELY_REJECTED':
            # A label, elapsed timeout, or empty broker scan cannot release the
            # committed reservation. Require an exact broker rejection event
            # for the original account/client operation, without fill evidence.
            try:
                raw=evidence['raw']; order=raw['order']
                if (evidence['mode']!=intent['environment']
                    or str(raw['ctidTraderAccountId'])!=intent['account_id']
                    or raw['executionType']!='ORDER_REJECTED'
                    or order['clientOrderId']!=intent['client_order_id']
                    or order['tradeData']['symbolId']!=intent['symbol_id']
                    or raw.get('position') or raw.get('deal') or raw.get('positionId')
                    or row.accepted_execution is not None or row.broker_position_id):
                    raise ValueError()
            except (KeyError,TypeError,ValueError):
                raise RecoveryError('RECOVERY_REJECTION_UNVERIFIED') from None
            intent={**intent,'rejection_evidence':dict(
                environment=evidence['mode'],account_id=str(raw['ctidTraderAccountId']),
                client_order_id=order['clientOrderId'],symbol_id=order['tradeData']['symbolId'],
                execution_type=raw['executionType'])}
            row.attempt_status='DEFINITELY_REJECTED';row.reconciliation_status='NOT_REQUIRED'
            if lifecycle: lifecycle.status='BLOCKED'
        elif outcome=='AMBIGUOUS':
            row.attempt_status='RECONCILIATION_REQUIRED';row.reconciliation_status='PENDING'
            # Preserve unresolved intent even when an exception description exists.
        else: raise RecoveryError('RECOVERY_OUTCOME_UNVERIFIED')
        row.send_intent={**intent,'state':'UNRESOLVED' if outcome=='AMBIGUOUS' else outcome}
        row.updated_at=datetime.now(timezone.utc)
