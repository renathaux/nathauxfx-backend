"""Server-only bootstrap. No account/build identity is accepted from HTTP input."""
from uuid import uuid4

from startup_recovery.coordinator import StartupOutcome, recover
from startup_recovery.readers import DatabaseReader
from startup_recovery.types import AccountScope, RecoveryError


def start(api_module, *, engine=None, session_factory=None, dependencies_factory=None):
    """Explicit startup entry point; injected dependencies are for local tests only.

    This function never constructs legacy-cutover or handoff evidence. Missing
    release evidence remains a durable block; liveness is not entry readiness.
    """
    build = None
    try:
        from live_integrity.build_identity import capture
        try:
            build = capture()
        except Exception:
            raise RecoveryError('BUILD_IDENTITY_UNVERIFIED') from None
        if engine is None or session_factory is None:
            from db import engine as configured_engine, SessionLocal
            engine = configured_engine if engine is None else engine
            session_factory = SessionLocal if session_factory is None else session_factory
        from live_integrity.snapshots import selected
        try:
            with DatabaseReader(engine, history_from=0).transaction() as connection:
                selection = selected(connection)
        except Exception:
            raise RecoveryError('RECOVERY_SELECTED_ACCOUNT_UNAVAILABLE') from None
        scope = AccountScope('ctrader', selection['environment'], selection['account_id'])
        if dependencies_factory is None:
            from startup_recovery.server_adapter import ProductionDependencies
            dependencies_factory = ProductionDependencies
        dependencies = dependencies_factory(api_module=api_module, engine=engine,
            session_factory=session_factory, scope=scope, build=build)
        outcome = recover(dependencies, scope, str(uuid4()), build['backend_git_sha'])
        if outcome.management_ready:
            api_module._recovery_runtime = dependencies
    except Exception as exc:
        reason = exc.code if isinstance(exc, RecoveryError) else 'RECOVERY_STARTUP_FAILED'
        outcome = StartupOutcome(False, reason, None, ('BOOTSTRAP',))
    api_module.ENGINE_RUNTIME_STATE['recovery'] = dict(
        ready=outcome.ready, reason=outcome.reason, phases=list(outcome.phases),
        management_ready=outcome.management_ready, entries_ready=outcome.entries_ready,
        entry_block_reasons=list(outcome.entry_block_reasons),
        build_identity=build, epoch=outcome.token.epoch if outcome.token else None)
    return outcome
