"""Final admission serialized by durable account owner, never broker I/O here.

Lock order: existing account-operation advisory lock (if used), recovery account,
saved strategy, lifecycle. Recovery annotation commits before the send guard.
The annotation has no outcome; the existing execution record is authoritative.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json

from sqlalchemy import select

from models import RecoveryMutation, TradeSubmissionAttempt, StrategySetupLifecycle
from startup_recovery import store
from startup_recovery.types import RecoveryError, Phase
from startup_recovery.runtime import operation_context, operation_permit

# Spotware OpenApiModelMessages.proto ProtoOAPayloadType (2107 is a server event).
_MUTATIONS = {2106: 'NEW_ORDER', 2108: 'CANCEL_ORDER', 2109: 'AMEND_ORDER',
              2110: 'AMEND_POSITION', 2111: 'CLOSE_POSITION'}
# Existing read/auth/subscription protocol only. Unknown protocol operations are
# not silently treated as reads. Control frames have no trading payload.
_READS = {51, 2100, 2102, 2112, 2114, 2116, 2121, 2124, 2127, 2129,
          2133, 2137, 2149, 2175, 2187}


def entry_gate(session, token):
    row, _ = store.require_owner(session, token)
    if row.phase != Phase.NEW_ENTRIES_READY or not row.accepted_manifest_hash:
        raise RecoveryError('LIVE_RECOVERY_INCOMPLETE')


def _phase(session, token, kind):
    row, _ = store.require_owner(session, token)
    allowed = {Phase.NEW_ENTRIES_READY} if kind == 'NEW_ORDER' else {
        Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY}
    if row.phase not in allowed or not row.accepted_manifest_hash:
        raise RecoveryError('LIVE_RECOVERY_INCOMPLETE')


def _reference(session, token, kind, intent_hash, submission_id, setup_id, management_intent_id):
    if kind not in _MUTATIONS.values():
        raise RecoveryError('RECOVERY_OPERATION_UNSUPPORTED')
    if submission_id is not None and setup_id is None and kind == 'NEW_ORDER':
        row = session.execute(select(TradeSubmissionAttempt).where(
            TradeSubmissionAttempt.id == submission_id).execution_options(populate_existing=True)).scalar_one_or_none()
        if (row is None or row.account_id != token.scope.account_id or row.mode != 'LIVE'
            or row.request_payload_fingerprint != intent_hash or row.attempt_status != 'SUBMITTING'
            or row.request_started_at is None):
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
        return 'submission:' + str(submission_id)
    # Management must reference an existing durable intent, not fabricate an
    # operation/result ledger in recovery. The final integration supplies the
    # preserved state fingerprint from that lifecycle.
    if setup_id is not None and submission_id is None and kind in {'AMEND_POSITION', 'CLOSE_POSITION'}:
        row = session.execute(select(StrategySetupLifecycle).where(
            StrategySetupLifecycle.setup_id == setup_id).execution_options(populate_existing=True)).scalar_one_or_none()
        require_initial_protection_settled(session,row)
        if (row is None or row.account_id != token.scope.account_id
            or row.account_scope.lower() != f'ctrader:{token.scope.environment}:{token.scope.account_id}'
            or not management_intent_id or management_intent_id != intent_hash
            or management_intent_hash(row, kind) != intent_hash):
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_INVALID')
        return 'management:' + setup_id + ':' + intent_hash
    raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')


def require_initial_protection_settled(session,lifecycle):
    """Initial repair and later lifecycle management cannot own one position at once.

    Unversioned already-open lifecycle management retains its existing behavior;
    this applies only to positions admitted by the new durable entry protocol.
    """
    if lifecycle is None: return
    rows=session.scalars(select(TradeSubmissionAttempt).where(
        TradeSubmissionAttempt.signal_setup_id==lifecycle.setup_id,
        TradeSubmissionAttempt.account_id==lifecycle.account_id)).all()
    durable=[a for a in rows if a.send_intent is not None]
    if not durable: return
    from services.accepted_execution import require_accepted_execution
    if len(durable)!=1: raise RecoveryError('RECOVERY_PROTECTION_IDENTITY_CONFLICT')
    row=durable[0];accepted=require_accepted_execution(row)
    protection=row.initial_protection or {}
    if (protection.get('state')!='CONFIRMED'
        or protection.get('accepted_execution_hash')!=row.accepted_execution_hash
        or accepted['position_id']!=lifecycle.broker_position_id):
        raise RecoveryError('RECOVERY_INITIAL_PROTECTION_UNRESOLVED')


def management_intent_hash(row, kind):
    """Reference existing durable intent facts, excluding per-poll timestamps."""
    from live_integrity.binding import fingerprint
    state = row.management_state or {}
    identity = {'setup_id': row.setup_id, 'account_scope': row.account_scope,
                'position_id': row.broker_position_id, 'kind': kind}
    if kind == 'CLOSE_POSITION':
        if row.tp1_requested_at is None or state.get('tp1_requested_volume') is None:
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
        stamp = row.tp1_requested_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)  # SQLite UTC fixture storage
        identity.update(requested_at=stamp.astimezone(timezone.utc).isoformat(),
                        requested_volume=state['tp1_requested_volume'])
    elif kind == 'AMEND_POSITION':
        if state.get('target_protected_sl') is None:
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
        identity.update(target=state['target_protected_sl'], step=state.get('protection_step_index'),
                        request_sequence=state.get('protection_request_sequence', 0))
    else:
        raise RecoveryError('RECOVERY_OPERATION_UNSUPPORTED')
    return fingerprint(identity)


class _Permit:
    def __init__(self, session, token, kind, key):
        self.session, self.token, self.kind, self.operation_key = session, token, kind, key
        self.active, self.sent = True, False

    def authorize_frame(self, sock, payload_type, payload):
        if not self.active or self.sent or _MUTATIONS.get(payload_type) != self.kind:
            raise RecoveryError('RECOVERY_MUTATION_CAPABILITY_INVALID')
        if (str(payload.get('ctidTraderAccountId')) != self.token.scope.account_id
            or getattr(sock, '_recovery_environment', None) != self.token.scope.environment):
            raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
        # A fresh DB statement on the held transaction detects a lost connection.
        # No helper replaces this token with the newer durable owner's token.
        _phase(self.session, self.token, self.kind)
        if self.kind=='NEW_ORDER':
            raise RecoveryError('RECOVERY_DURABLE_SEND_INTENT_REQUIRED')
        self.sent = True  # one attempt; even a failed socket send is ambiguous


@contextmanager
def mutation_guard(factory, token, kind, intent_hash, *, submission_id=None,
                   setup_id=None, management_intent_id=None):
    store._hash(intent_hash)
    with factory.begin() as session:
        _phase(session, token, kind)
        key = _reference(session, token, kind, intent_hash, submission_id, setup_id, management_intent_id)
        row = session.get(RecoveryMutation, key)
        if row is None:
            session.add(RecoveryMutation(operation_key=key, attempt_id=token.attempt_id,
                scope_key=store.scope_key(token.scope), epoch=token.epoch,
                operation_kind=kind, intent_hash=intent_hash, submission_id=submission_id,
                setup_id=setup_id, management_intent_id=management_intent_id,
                created_at=datetime.now(timezone.utc)))
        else:
            # Recovery does not authorize replay of an operation from an older
            # epoch, even if a caller presents the same existing execution ID.
            if row.attempt_id != token.attempt_id or row.intent_hash != intent_hash:
                raise RecoveryError('RECOVERY_OPERATION_EPOCH_CHANGED')
            raise RecoveryError('RECOVERY_OPERATION_ALREADY_FENCED')
    with factory.begin() as session:
        _phase(session, token, kind)
        _reference(session, token, kind, intent_hash, submission_id, setup_id, management_intent_id)
        permit = _Permit(session, token, kind, key)
        try:
            with operation_context(permit):
                yield permit
        finally:
            permit.active = False


def operation_outcome(session, operation_key):
    annotation = session.get(RecoveryMutation, operation_key)
    if annotation is None:
        raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
    if annotation.submission_id is not None:
        row = session.execute(select(TradeSubmissionAttempt).where(
            TradeSubmissionAttempt.id == annotation.submission_id).execution_options(populate_existing=True)).scalar_one()
        return row.attempt_status
    row = session.execute(select(StrategySetupLifecycle).where(
        StrategySetupLifecycle.setup_id == annotation.setup_id).execution_options(populate_existing=True)).scalar_one()
    if management_intent_hash(row, annotation.operation_kind) != annotation.management_intent_id:
        return 'REFERENCE_ADVANCED'  # never assign the newer operation's outcome to the old one
    return (row.management_state or {}).get('tp1_state' if annotation.operation_kind == 'CLOSE_POSITION'
                                              else 'protection_state', 'UNRESOLVED')


def dispatch_claimed_order(factory, claim_key, submit):
    """Unversioned entry cannot manufacture an original immutable send intent.

    Existing position management uses its separate preserved lifecycle guard.
    New entries must use the version-bound durable dispatcher, not this legacy
    claim-only callback. Do not backfill from current strategy/defaults.
    """
    from startup_recovery.runtime import caller_token
    token = caller_token()
    with factory() as session:
        row = session.execute(select(TradeSubmissionAttempt).where(
            TradeSubmissionAttempt.idempotency_key == claim_key)).scalar_one_or_none()
        if row is None:
            raise RecoveryError('RECOVERY_OPERATION_IDENTITY_MISSING')
        entry_gate(session,token)
    raise RecoveryError('RECOVERY_EXECUTION_IDENTITY_UNVERIFIED')


def authorize_frame(sock, opcode, payload):
    if opcode in {8, 9, 10}:
        return
    if opcode != 1:
        raise RecoveryError('RECOVERY_PROTOCOL_UNSUPPORTED')
    try:
        message = json.loads(payload)
        kind = message['payloadType']
    except (ValueError, KeyError, TypeError):
        raise RecoveryError('RECOVERY_PROTOCOL_INVALID') from None
    if kind in _READS:
        return
    if kind not in _MUTATIONS:
        raise RecoveryError('RECOVERY_PROTOCOL_UNSUPPORTED')
    try:
        operation_permit().authorize_frame(sock, kind, message.get('payload') or {})
    except RecoveryError:
        try:
            sock.close()
        finally:
            raise
