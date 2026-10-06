"""Explicit server composition; constructor is inert and cannot mint cutover proof."""
from services.monthly_history_window import calendar_month_start_ts, legacy_week_start_ts
from startup_recovery.readers import ReadAdapters
from startup_recovery.types import RecoveryError
from sqlalchemy import text
from pathlib import Path
import copy
import os
from contextlib import contextmanager
from threading import Lock


def state_paths(api_module):
    """Reviewed server paths only. No client-supplied path and no directory creation."""
    from strategies import shared
    from services import settings_service, news_trading
    from ctrader_connector import CTRADER_ACCOUNTS_PATH
    return {kind: Path(path) for kind, path in {
        'live_backup': api_module.LIVE_BACKUP_FILE,
        'live_monthly_history': api_module.LIVE_MONTHLY_HISTORY_FILE,
        'paper_backup': shared.PAPER_BACKUP_FILE,
        'final_signal_hold': shared.FINAL_SIGNAL_HOLD_FILE,
        'fifteen_m_swing_watch': shared.FIFTEEN_M_SWING_WATCH_FILE,
        'market_data_source': shared.MARKET_DATA_SOURCE_FILE,
        'news_trading_state': news_trading.STATE_FILE,
        'app_settings': settings_service.SETTINGS_PATH,
        'feature_flags': settings_service.FEATURE_FLAGS_PATH,
        'ctrader_accounts': CTRADER_ACCOUNTS_PATH,
        'visits': api_module.VISITS_FILE,
    }.items()}


class ProductionDependencies:
    def __init__(self, *, api_module, engine, session_factory, scope, build):
        self.api, self.engine, self.session_factory = api_module, engine, session_factory
        self.scope, self.build = scope, dict(build)
        # Preserve the existing calendar-month/week windows; recovery never
        # shortens their coverage to make a history query appear complete.
        self.history_from = min(calendar_month_start_ts(), legacy_week_start_ts())
        self.readers = ReadAdapters(engine, history_from=self.history_from)
        self.writer = None
        self.token = None
        self._activation_lock = Lock()
        self._management_prepared = False
        self._entry_started = False
        self._baseline_database = None

    def verify_database(self):
        from services.trade_submission_service import EXECUTION_PROTOCOL_VERSION
        try:
            with self.readers.database.transaction() as connection:
                versions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
                if versions != ['20261001_0030']:
                    raise RecoveryError('RECOVERY_SCHEMA_UNVERIFIED')
                protocol = connection.execute(text(
                    'SELECT protocol_version FROM execution_protocol_state WHERE singleton_id=1'
                )).scalar_one_or_none()
                if protocol != EXECUTION_PROTOCOL_VERSION:
                    raise RecoveryError('RECOVERY_PROTOCOL_UNVERIFIED')
                from services.submission_reservation import reservation_guard_present
                if not reservation_guard_present(connection):
                    raise RecoveryError('RECOVERY_RESERVATION_GUARD_MISSING')
                earliest = connection.execute(text(
                    'SELECT MIN(tp1_requested_at) FROM strategy_setup_lifecycle WHERE account_id=:account'
                ), {'account': self.scope.account_id}).scalar_one()
                if earliest is not None:
                    if earliest.tzinfo is None:
                        raise RecoveryError('RECOVERY_HISTORY_BOUND_UNVERIFIED')
                    self.history_from = min(self.history_from, earliest.timestamp())
                    self.readers.history_from = self.readers.database.history_from = self.history_from
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError('RECOVERY_SCHEMA_UNVERIFIED') from None
        return dict(schema=versions[0], protocol=protocol)

    def reconcile_submissions(self):
        from services.trade_submission_service import reconcile_incomplete_submissions
        def provider(account_id,claimed_at):
            if str(account_id)!=self.scope.account_id:
                raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
            return self.readers.submissions(self.scope,claimed_at)
        return reconcile_incomplete_submissions(record_provider=provider,
            session_factory=self.session_factory,account_id=self.scope.account_id)

    def checkpoints(self, snapshot):
        from startup_recovery.checkpoints import read_candidate, read_committed, CheckpointCandidate
        from startup_recovery.checkpoint_store import ISOLATED_KINDS
        paths = state_paths(self.api)
        heads = snapshot.database.get('checkpoint_heads', [])
        if any(head.get('kind') not in paths for head in heads):
            raise RecoveryError('CHECKPOINT_KIND_INVALID')
        candidates = {}
        for kind, path in paths.items():
            matching = [h for h in heads if h.get('kind') == kind]
            if len(matching) > 1:
                raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
            if matching:
                try:
                    generation = read_committed(path.parent, matching[0]['manifest_hash'])
                    if kind not in generation:
                        raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
                    candidates[kind] = generation[kind]
                except RecoveryError as exc:
                    if kind not in ISOLATED_KINDS:
                        raise
                    candidates[kind] = CheckpointCandidate(kind, b'', False, exc.code)
            else:
                candidates[kind] = read_candidate(path, kind)
        return candidates

    def publish_reconciled(self, result, token, snapshot):
        from startup_recovery.store import require_owner
        from startup_recovery.types import Phase
        from startup_recovery.reconcile import discover, reconcile, checkpoint_dependencies
        from startup_recovery.publication import runtime_projection
        from startup_recovery.checkpoint_store import RuntimeWriter
        from strategies import shared
        from services import settings_service, news_trading
        from ctrader_connector import DEFAULT_CTRADER_ACCOUNT_SETTINGS
        # No state publication merely because a caller supplied an ok result.
        with self.session_factory() as session:
            account, _ = require_owner(session, token)
            if account.phase != Phase.STATE_RECONCILED or account.accepted_manifest_hash != result.manifest_hash:
                raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
        fresh = discover(self.readers, self.scope, token)
        current = reconcile(fresh, self.checkpoints(fresh))
        if not current.ok or current.manifest_hash != result.manifest_hash:
            raise RecoveryError('RECOVERY_SNAPSHOT_CHANGED_BEFORE_PUBLICATION')
        projected = runtime_projection(current, fresh)
        accepted = projected['checkpoints']
        paths = state_paths(self.api)
        paper = None
        quarantined = set(current.quarantined)
        if 'paper_backup' not in quarantined:
            try:
                paper = shared.stage_admitted_paper_payload(
                    accepted.get('paper_backup', shared.snapshot_paper_backup()))
            except RecoveryError:
                quarantined.add('paper_backup')
        holds = copy.deepcopy(accepted.get('final_signal_hold', {}))
        watches = copy.deepcopy(accepted.get('fifteen_m_swing_watch', {}))
        if not isinstance(holds, dict) or not isinstance(watches, dict):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        saved_source = accepted.get('market_data_source')
        if saved_source is not None and (not isinstance(saved_source, dict)
            or saved_source.get('source') not in shared.MARKET_DATA_SOURCES):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        source_boot = dict(source=saved_source['source'] if saved_source else 'ctrader',
            saved_source=saved_source['source'] if saved_source else None,
            loaded_from_file=saved_source is not None)
        source = os.getenv('MARKET_DATA_SOURCE', source_boot['source']).lower()
        # Preserve existing server-env precedence, but do not substitute a feed
        # during recovery when the selected configuration is invalid.
        if source not in shared.MARKET_DATA_SOURCES:
            raise RecoveryError('RECOVERY_MARKET_SOURCE_UNVERIFIED')
        backup = accepted.get('live_backup')
        if backup is not None and (not isinstance(backup, dict)
            or not isinstance(backup.get('account_close_times'), dict)):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        history = projected['closed_history']
        month_start = calendar_month_start_ts()
        month_history = [row for row in history if row['closed_at'] >= month_start]
        from datetime import datetime
        month_key = datetime.fromtimestamp(float(fresh.broker['observed_at']),
                                          self.api.LIVE_MARKET_TIMEZONE).strftime('%Y-%m')
        monthly = dict(history=month_history, updated_at=float(fresh.broker['observed_at']),
            month_key=month_key, account_scope=projected['account']['account_scope'])
        defaults = dict(
            paper_backup=paper,
            final_signal_hold=holds, fifteen_m_swing_watch=watches,
            market_data_source={'source': source_boot['source']},
            news_trading_state=news_trading._empty_state(),
            app_settings={'risk': copy.deepcopy(settings_service.DEFAULT_RISK_SETTINGS)},
            feature_flags=copy.deepcopy(settings_service.DEFAULT_FEATURE_FLAGS),
            ctrader_accounts=copy.deepcopy(DEFAULT_CTRADER_ACCOUNT_SETTINGS), visits=[],
            live_monthly_history=monthly,
        )
        live_backup = dict(account_close_times=copy.deepcopy(backup['account_close_times']) if backup else {},
            live_active_orders=projected['active_orders'], live_trade_history=month_history,
            live_last_execution_time=projected['last_execution_time'],
            live_last_position_closed_at=projected['last_position_closed_at'],
            last_live_reset=backup['last_live_reset'] if backup else legacy_week_start_ts())
        live_backup['account_close_times'][projected['account']['account_scope']] = copy.deepcopy(
            projected['last_position_closed_at'])
        defaults['live_backup'] = live_backup
        identity = dict(account_scope=projected['account']['account_scope'],
            owner_id=projected['owner_id'], symbol=None, strategy_id=None,
            config_hash=None, position_id=None, epoch=token.epoch, boot_id=token.boot_id,
            build_id=self.build['backend_git_sha'], dependencies_hash=current.dependencies_hash,
            generation=1)
        bindings = {kind: (paths[kind], {**identity,
                'dependencies_hash': checkpoint_dependencies(kind, fresh.database, accepted.get(kind, defaults[kind]))},
                accepted.get(kind, defaults[kind]))
            for kind in paths if kind not in quarantined}
        writer = RuntimeWriter(self.session_factory, token, bindings, result.manifest_hash,
            absent_kinds=set(bindings) - set(accepted), dependency_provider=self.produced_dependencies)
        # All parsing/staging precedes shared-state mutation. Hold the same owner
        # boundary through publication; no worker or broker callback runs here.
        with self.session_factory.begin() as session:
            account, _ = require_owner(session, token)
            if account.phase != Phase.STATE_RECONCILED or account.accepted_manifest_hash != result.manifest_hash:
                raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
            self.api.LIVE_ACTIVE_ORDERS = dict(EURUSD=None, XAUUSD=None) | projected['active_orders']
            self.api.LIVE_ACCOUNT_STATE = projected['account'] | {'execution_ready': False}
            self.api.LIVE_TRADE_HISTORY = copy.deepcopy(month_history)
            self.api.LIVE_BROKER_CLOSED_HISTORY = copy.deepcopy(history)
            self.api.LIVE_BROKER_HISTORY_CACHE = dict(history=copy.deepcopy(history),
                updated_at=float(fresh.broker['observed_at']), account_scope=projected['account']['account_scope'])
            self.api.LIVE_MONTHLY_HISTORY_CACHE = monthly
            self.api.LIVE_ACCOUNT_CLOSE_TIMES = live_backup['account_close_times']
            self.api.LIVE_LAST_EXECUTION_TIME = projected['last_execution_time']
            self.api.LIVE_LAST_POSITION_CLOSED_AT = projected['last_position_closed_at']
            shared.LAST_POSITION_CLOSED_AT = {
                projected['account']['account_scope'] + ':' + symbol: stamp
                for symbol, stamp in projected['last_position_closed_at'].items()}
            self.api.LAST_LIVE_RESET = live_backup['last_live_reset']
            for setting, target in [('live_auto_trade_enabled', self.api.LIVE_AUTO_TRADE_ENABLED),
                                    ('paper_auto_trade_enabled', self.api.AUTO_TRADE_ENABLED)]:
                if setting in projected['preferences']:
                    target['enabled'] = projected['preferences'][setting]
            shared.FINAL_SIGNAL_HOLD, shared.FIFTEEN_M_SWING_WATCH = holds, watches
            shared.SOURCE_BOOT_STATE, shared.MARKET_DATA_SOURCE = source_boot, source
            shared.MARKET_DATA_RUNTIME = dict(source=source,
                saved_source=source_boot['saved_source'], loaded_from_file=source_boot['loaded_from_file'])
            if paper is not None:
                shared.restore_admitted_paper_payload(paper)
        self._baseline_database = fresh.database
        self.writer, self.token = writer, token
        from startup_recovery.checkpoint_store import register_worker_producer
        register_worker_producer(writer)

    def produced_dependencies(self, kind, payload):
        from startup_recovery.reconcile import checkpoint_dependencies, checkpoint_execution_version
        from startup_recovery.publication import validate_produced_checkpoint
        baseline = self._baseline_database
        if baseline is None:
            raise RecoveryError('CHECKPOINT_RECOVERY_NOT_ADMITTED')
        current = self.readers.database(self.scope)
        if (current.get('selection') != baseline.get('selection')
            or set(current.get('runtime_owners', [])) != set(baseline.get('runtime_owners', []))):
            raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
        if kind in {'final_signal_hold', 'fifteen_m_swing_watch', 'news_trading_state'}:
            # A current DB lookup may corroborate an operation, not rebind a
            # producer's old signal to a newly edited strategy/generation.
            if checkpoint_execution_version(kind, current, payload) != checkpoint_execution_version(kind, baseline, payload):
                raise RecoveryError('CHECKPOINT_EXECUTION_VERSION_CHANGED')
        validate_produced_checkpoint(kind, payload, current)
        return checkpoint_dependencies(kind, current, payload)

    @contextmanager
    def runtime_context(self, *, entries=False):
        from startup_recovery.store import require_owner
        from startup_recovery.types import Phase
        from startup_recovery.operation_context import RecoveryOperationContext, bound
        from startup_recovery.checkpoint_store import checkpoint_producer
        if self.token is None or self.writer is None:
            raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
        try:
            with self.session_factory() as session:
                account, _ = require_owner(session, self.token)
                allowed = {Phase.NEW_ENTRIES_READY} if entries else {
                    Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY}
                if account.phase not in allowed:
                    raise RecoveryError('RECOVERY_NOT_COMPLETE')
                selection = (self._baseline_database or {}).get('selection') or {}
                revision = selection.get('revision')
                if revision is not None:
                    from datetime import datetime
                    revision = datetime.fromisoformat(revision).isoformat()
                context = RecoveryOperationContext(self.token,
                    'entry' if entries else 'request', account.accepted_manifest_hash,
                    revision)
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError('RECOVERY_DATABASE_UNAVAILABLE') from None
        # Caller keeps this exact boot/epoch even if another process later owns
        # the account. Every broker mutation still performs its own final fence.
        with bound(context), checkpoint_producer(self.writer):
            yield context

    def start_management(self, token):
        if token != self.token:
            raise RecoveryError('RECOVERY_TOKEN_INVALID')
        with self._activation_lock, self.runtime_context():
            if self._management_prepared:
                return
            from startup_recovery.operation_context import capture
            from paths import ensure_runtime_dirs
            from brain import _clear_legacy_consolidation_blocked_watches
            import app_bootstrap
            ensure_runtime_dirs()
            _clear_legacy_consolidation_blocked_watches()
            context = capture('management')
            app_bootstrap._start_forex_background_task(context=context, management_only=True)
            thread = self.api.BACKGROUND_THREAD
            if thread is None or not thread.is_alive() or getattr(thread, 'recovery_context', None) != context:
                raise RecoveryError('RECOVERY_ENGINE_NOT_STARTED')
            from fundamentals.ingestion import start_fundamental_ingestion_scheduler
            start_fundamental_ingestion_scheduler(context=context)
            self._management_prepared = True

    def entry_readiness(self, token):
        if token != self.token or not self._management_prepared:
            raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
        from startup_recovery.operation_context import capture
        import app_bootstrap
        with self.runtime_context():
            return app_bootstrap._start_forex_background_task(context=capture('management'))

    def start_entry_evaluation(self, token):
        if token != self.token or not self._management_prepared:
            raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
        with self._activation_lock, self.runtime_context(entries=True):
            if self._entry_started:
                return
            thread = self.api.BACKGROUND_THREAD
            if thread is None or not thread.is_alive():
                raise RecoveryError('RECOVERY_ENGINE_NOT_STARTED')
            self._entry_started = True
