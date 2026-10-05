"""PostgreSQL-only durable ownership. No sockets, claims or timeout takeovers.

Callers own transactions and commit before scheduling work. NOWAIT prevents a
second worker from queueing for authority. DB disconnect is never relinquishment.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from models import RecoveryAccount, RecoveryAttempt, TradeSubmissionAttempt, StrategySetupLifecycle
from startup_recovery.types import ManagerToken, RecoveryError, Phase, HandoffEvidence

_PHASES = [Phase.BOOTSTRAP, Phase.DB_READY, Phase.BROKER_AUTHENTICATED,
           Phase.STATE_DISCOVERED, Phase.STATE_RECONCILED,
           Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY]
_NEXT = dict(zip(_PHASES, _PHASES[1:]))


def scope_key(scope):
    return hashlib.sha256(json.dumps(asdict(scope), sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise RecoveryError('RECOVERY_EVIDENCE_INVALID')


def account_state(session, scope, *, lock=False):
    query = select(RecoveryAccount).where(RecoveryAccount.scope_key == scope_key(scope))
    if lock:
        from startup_recovery.unit_of_work import ordered
        if ordered(session, 4, scope_key(scope)):
            query = query.with_for_update(nowait=True)
    try:
        # Never trust an ORM identity-map value from an earlier gate.
        return session.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    except DBAPIError as exc:
        code = getattr(exc.orig, 'pgcode', None)
        raise RecoveryError('RECOVERY_OWNER_BUSY' if code == '55P03' else 'RECOVERY_DB_UNAVAILABLE') from None
    except Exception:
        raise RecoveryError('RECOVERY_DB_UNAVAILABLE') from None


def begin_attempt(session, scope, boot_id, build_id):
    if not boot_id or not build_id:
        raise RecoveryError('RECOVERY_BUILD_OR_BOOT_MISSING')
    if session.get_bind().dialect.name != 'postgresql':
        raise RecoveryError('RECOVERY_POSTGRES_REQUIRED')
    key = scope_key(scope)
    session.execute(insert(RecoveryAccount).values(
        scope_key=key, **asdict(scope), allocated_epoch=0,
        phase=Phase.LEGACY_CUTOVER_REQUIRED).on_conflict_do_nothing(index_elements=['scope_key']))
    row = account_state(session, scope, lock=True)
    row.allocated_epoch += 1
    token = ManagerToken(scope, str(uuid4()), row.allocated_epoch, boot_id)
    session.add(RecoveryAttempt(attempt_id=token.attempt_id, scope_key=key,
        epoch=token.epoch, boot_id=boot_id, build_id=build_id, phase=Phase.BOOTSTRAP,
        predecessor_id=row.owner_attempt_id, created_at=datetime.now(timezone.utc)))
    session.flush()
    return token


def _proof(proof):
    if (not isinstance(proof, HandoffEvidence)
        or proof.kind not in {'operator-termination', 'graceful-drain'}
        or proof.prior_process_stopped is not True
        or proof.operations_reconciled is not True or not proof.predecessor_identity):
        raise RecoveryError('RECOVERY_HANDOFF_UNPROVEN')
    _hash(proof.evidence_hash)


def _unsettled(session, scope):
    # Existing records remain the one operation source of truth. Unknown statuses
    # are not safe simply because no recovery annotation exists yet.
    attempts = session.execute(select(TradeSubmissionAttempt).where(
        TradeSubmissionAttempt.account_id == scope.account_id,
        TradeSubmissionAttempt.mode == 'LIVE')).scalars()
    settled = {'ACCEPTED', 'DEFINITELY_REJECTED', 'FAILED_BEFORE_SEND', 'ACCEPTED_PROTECTION_FAILED'}
    if any(a.attempt_status not in settled or a.reconciliation_status == 'RECONCILIATION_REQUIRED'
           or (a.send_intent or {}).get('state') in {'UNRESOLVED','SENT_UNKNOWN','ACCEPTANCE_AMBIGUOUS'}
           or (a.initial_protection or {}).get('state') in {'UNRESOLVED','REQUEST_STARTED','RECONCILIATION_REQUIRED'}
           for a in attempts):
        return True
    rows = session.execute(select(StrategySetupLifecycle).where(
        StrategySetupLifecycle.account_id == scope.account_id)).scalars()
    return any(r.status == 'RECONCILIATION_REQUIRED' or
               (r.tp1_requested_at is not None and r.tp1_completed_at is None) or
               (r.management_state or {}).get('protection_state') in {'PENDING', 'FAILED'}
               for r in rows)


def establish_legacy_cutover(session, scope, proof):
    _proof(proof)
    if proof.kind != 'operator-termination':
        raise RecoveryError('RECOVERY_HANDOFF_UNPROVEN')
    row = account_state(session, scope, lock=True)
    if row is None or row.owner_attempt_id or row.phase != Phase.LEGACY_CUTOVER_REQUIRED:
        raise RecoveryError('RECOVERY_CUTOVER_INVALID')
    if _unsettled(session, scope):
        raise RecoveryError('RECOVERY_OPERATIONS_UNRESOLVED')
    row.handoff_evidence = asdict(proof)
    row.phase = Phase.RELINQUISHED
    session.flush()


def _attempt(session, token):
    attempt = session.execute(select(RecoveryAttempt).where(
        RecoveryAttempt.attempt_id == token.attempt_id).execution_options(populate_existing=True)).scalar_one_or_none()
    if (not attempt or attempt.scope_key != scope_key(token.scope) or
        attempt.epoch != token.epoch or attempt.boot_id != token.boot_id):
        raise RecoveryError('RECOVERY_TOKEN_INVALID')
    return attempt


def acquire_owner(session, token):
    row = account_state(session, token.scope, lock=True)
    attempt = _attempt(session, token)
    if row.phase == Phase.LEGACY_CUTOVER_REQUIRED:
        raise RecoveryError('LEGACY_CUTOVER_REQUIRED')
    if row.owner_attempt_id:
        raise RecoveryError('RECOVERY_OWNER_BUSY')
    if row.owner_epoch is not None and token.epoch <= row.owner_epoch:
        raise RecoveryError('RECOVERY_TOKEN_STALE')
    if row.phase != Phase.RELINQUISHED or not row.handoff_evidence:
        raise RecoveryError('RECOVERY_HANDOFF_UNPROVEN')
    if attempt.phase != Phase.BOOTSTRAP or _unsettled(session, token.scope):
        raise RecoveryError('RECOVERY_OPERATIONS_UNRESOLVED')
    row.owner_attempt_id, row.owner_epoch = token.attempt_id, token.epoch
    row.phase = Phase.BOOTSTRAP
    row.accepted_manifest_hash = None
    session.flush()


def require_owner(session, token, *, lock=True):
    from startup_recovery.runtime import assert_token_usable
    assert_token_usable(token)
    row = account_state(session, token.scope, lock=lock)
    attempt = _attempt(session, token)
    if not row or row.owner_attempt_id != token.attempt_id or row.owner_epoch != token.epoch:
        raise RecoveryError('RECOVERY_TOKEN_STALE')
    if row.phase != attempt.phase:
        raise RecoveryError('RECOVERY_PHASE_CONFLICT')
    return row, attempt


def advance(session, token, expected, target, evidence_hash):
    _hash(evidence_hash)
    row, attempt = require_owner(session, token)
    if row.phase != expected or _NEXT.get(expected) != target:
        raise RecoveryError('RECOVERY_PHASE_INVALID')
    row.phase = attempt.phase = str(target)
    attempt.phase_evidence_hash = evidence_hash
    if target == Phase.STATE_RECONCILED:
        row.accepted_manifest_hash = evidence_hash
    session.flush()


def entries_ready(session, token):
    try:
        row, _ = require_owner(session, token, lock=False)
    except RecoveryError as exc:
        if exc.code in {'RECOVERY_TOKEN_MISSING', 'RECOVERY_TOKEN_STALE', 'RECOVERY_TOKEN_INVALID'}:
            return False
        raise
    return bool(row.phase == Phase.NEW_ENTRIES_READY and row.accepted_manifest_hash)


def relinquish(session, token, proof):
    _proof(proof)
    row, attempt = require_owner(session, token)
    if proof.predecessor_identity != token.boot_id:
        raise RecoveryError('RECOVERY_HANDOFF_UNPROVEN')
    if _unsettled(session, token.scope):
        raise RecoveryError('RECOVERY_OPERATIONS_UNRESOLVED')
    row.phase = attempt.phase = Phase.RELINQUISHED
    row.owner_attempt_id = None
    # Retain the high-water mark: a previously allocated standby cannot revive.
    row.accepted_manifest_hash = None
    row.handoff_evidence = asdict(proof)
    session.flush()
