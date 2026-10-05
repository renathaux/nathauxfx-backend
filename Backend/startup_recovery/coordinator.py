"""Explicit recovery orchestration. No order projection/dispatch capability.

Build identity comes from the trusted server adapter, never request input. This
coordinator has no cutover-proof constructor: initial release admission must
already be recorded by the separately trusted release/drain procedure.
"""
from dataclasses import dataclass
import re

from startup_recovery import store
from startup_recovery.reconcile import discover, reconcile, digest
from startup_recovery.types import Phase, RecoveryError, ManagerToken


@dataclass(frozen=True)
class StartupOutcome:
    ready: bool
    reason: str | None
    token: ManagerToken | None
    phases: tuple
    result: object = None
    management_ready: bool = False
    entries_ready: bool = False
    entry_block_reasons: tuple = ()


def block(session_factory, token, reason):
    """Revoke this owner only; DB failure never transfers ownership to another."""
    if token is None:
        return
    try:
        with session_factory.begin() as session:
            account, attempt = store.require_owner(session, token)
            account.phase = attempt.phase = Phase.BLOCKED
            account.accepted_manifest_hash = None
            attempt.conflict_reason = reason
    except Exception:
        # An unavailable DB will also fail every final mutation gate. Do not
        # forge relinquishment or clear the previous owner's durable identity.
        return
    finally:
        from startup_recovery.runtime import invalidate_token
        invalidate_token(token)


def recover(dependencies, scope, boot_id, build_id):
    if not isinstance(build_id, str) or not re.fullmatch('[0-9a-f]{40}', build_id):
        raise RecoveryError('BUILD_IDENTITY_UNVERIFIED')
    factory = dependencies.session_factory
    token, result = None, None
    phases = [str(Phase.BOOTSTRAP)]
    def advance(target, evidence):
        with factory.begin() as session:
            store.advance(session, token, phases[-1], target, evidence)
        phases.append(str(target))
    try:
        with factory.begin() as session:
            token = store.begin_attempt(session, scope, boot_id, build_id)
        # Exact broker observation can settle the old ledger without acquiring
        # management authority. Legacy cutover and explicit handoff remain gates.
        # This hook performs broker reads before its short DB write transaction.
        if hasattr(dependencies,'reconcile_submissions'):
            with factory() as session:
                account=store.account_state(session,scope)
                legacy=account.phase==Phase.LEGACY_CUTOVER_REQUIRED
            if not legacy:
                dependencies.verify_database()
                reconciliation=dependencies.reconcile_submissions()
                if not reconciliation.get('ok'):
                    raise RecoveryError('RECOVERY_OPERATIONS_UNRESOLVED')
        with factory.begin() as session:
            store.acquire_owner(session, token)
        database_evidence = dependencies.verify_database()
        if not isinstance(database_evidence, dict) or not database_evidence:
            raise RecoveryError('RECOVERY_DB_UNAVAILABLE')
        advance(Phase.DB_READY, digest(database_evidence))
        authentication = dependencies.readers.authenticate(scope)
        if (not isinstance(authentication, dict) or authentication.get('authenticated') is not True
            or authentication.get('account_id') != scope.account_id
            or authentication.get('environment') != scope.environment):
            raise RecoveryError('RECOVERY_AUTH_REAUTH_REQUIRED')
        advance(Phase.BROKER_AUTHENTICATED, digest(authentication))
        snapshot = discover(dependencies.readers, scope, token)
        advance(Phase.STATE_DISCOVERED, digest({'database': snapshot.database, 'broker': snapshot.broker}))
        result = reconcile(snapshot, dependencies.checkpoints(snapshot))
        if not result.ok:
            raise RecoveryError('RECOVERY_RECONCILIATION_BLOCKED')
        advance(Phase.STATE_RECONCILED, result.manifest_hash)
        dependencies.publish_reconciled(result, token, snapshot)
        advance(Phase.POSITION_MANAGEMENT_READY, result.manifest_hash)
        dependencies.start_management(token)
        if any(position.get('initial_protection_required') for position in result.positions):
            # Preserve proven exposure and start its separately fenced repair,
            # but never treat absent initial protection as entry-ready risk.
            reason = 'RECOVERY_ORIGINAL_PROTECTION_REQUIRED'
            return StartupOutcome(False, reason, token, tuple(phases), result,
                                  True, False, (reason,))
        if hasattr(dependencies, 'entry_readiness'):
            entry = dependencies.entry_readiness(token)
            if not isinstance(entry, dict) or entry.get('ready') is not True:
                reason = entry.get('reason') if isinstance(entry, dict) else None
                # Only this known entry-evaluation dependency can leave
                # management running. Identity/DB/protocol faults revoke both.
                if reason != 'INDICATOR_STREAM_STARTUP_BLOCKED':
                    raise RecoveryError(reason or 'RECOVERY_ENTRY_READINESS_UNVERIFIED')
                with factory() as session:
                    account, _ = store.require_owner(session, token)
                    if account.phase != Phase.POSITION_MANAGEMENT_READY:
                        raise RecoveryError('RECOVERY_NOT_READY')
                return StartupOutcome(False, reason, token, tuple(phases), result,
                                      True, False, (reason,))
        advance(Phase.NEW_ENTRIES_READY, result.manifest_hash)
        dependencies.start_entry_evaluation(token)
        with factory() as session:
            if not store.entries_ready(session, token):
                raise RecoveryError('RECOVERY_NOT_READY')
        return StartupOutcome(True, None, token, tuple(phases), result, True, True)
    except Exception as exc:
        reason = exc.code if isinstance(exc, RecoveryError) else 'RECOVERY_STARTUP_FAILED'
        block(factory, token, reason)
        return StartupOutcome(False, reason, token, tuple(phases), result)
