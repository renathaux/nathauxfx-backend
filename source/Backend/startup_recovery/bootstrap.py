"""Server-only bootstrap. No account/build identity is accepted from HTTP input."""
from uuid import uuid4

from startup_recovery.coordinator import StartupOutcome, recover
from startup_recovery.readers import DatabaseReader
from startup_recovery.types import AccountScope, RecoveryError


def adoption_configuration():
    """Explicit operator pin, independent of the mutable state directory.

    This is not release/build identity and has no directory-discovery fallback.
    Production provisioning remains a separately authorized operator action.
    """
    import os
    from pathlib import Path
    from startup_recovery.checkpoints import _valid_hash
    manifest = os.environ.get('FLOWSIGNAL_ADOPTION_MANIFEST')
    pin = os.environ.get('FLOWSIGNAL_ADOPTION_MANIFEST_SHA256')
    if manifest is None and pin is None:
        return None
    if not manifest or not _valid_hash(pin):
        raise RecoveryError('MANIFEST_PIN_REQUIRED')
    path = Path(manifest)
    if not path.is_absolute() or path.parent.name != '.cutover' or path.name != pin+'.json':
        raise RecoveryError('MANIFEST_PIN_REQUIRED')
    return dict(manifest_path=path,expected_manifest_sha256=pin,state_root=path.parent.parent)


def start(api_module, *, engine=None, session_factory=None, dependencies_factory=None):
    from startup_recovery.observability import attach, observing, note
    with observing(attach(api_module)):
        outcome = _start(api_module, engine=engine, session_factory=session_factory,
                         dependencies_factory=dependencies_factory)
        note('finished', ready=outcome.ready, reason=outcome.reason, phases=outcome.phases)
        return outcome


def _start(api_module, *, engine=None, session_factory=None, dependencies_factory=None):
    """Explicit startup entry point; injected dependencies are for local tests only.

    This function never constructs legacy-cutover or handoff evidence. Missing
    release evidence remains a durable block; liveness is not entry readiness.
    """
    build = None
    from startup_recovery.observability import note
    try:
        from live_integrity.build_identity import capture
        try:
            build = capture()
            note('build', build=build)
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
            dependencies = ProductionDependencies(api_module=api_module, engine=engine,
                session_factory=session_factory, scope=scope, build=build,
                adoption_configuration=adoption_configuration())
        else:
            dependencies = dependencies_factory(api_module=api_module, engine=engine,
                session_factory=session_factory, scope=scope, build=build)
        boot_id = str(uuid4())
        note('boot', boot_id=boot_id)
        outcome = recover(dependencies, scope, boot_id, build['backend_git_sha'])
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
