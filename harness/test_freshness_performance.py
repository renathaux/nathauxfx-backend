"""Disposable integration benchmark; never imports production configuration.

Real clock/PG/locks/authority/reconcile, synthetic protocol transport.
The first DML is denied before execution; setup/teardown writes are not measured.
Historical elapsed-time floors model unavailable remote latency and workload.
Floors include actual work, rather than adding elapsed times twice.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import time

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import event, text, inspect
from models import RuntimeSetting, StrategyStudioLiveState, CTraderOAuthToken, RecoveryCheckpointHead
from startup_recovery import owner_handoff, store
from startup_recovery.readers import DatabaseReader, BrokerReader
from startup_recovery.reconcile import DiscoverySnapshot
from startup_recovery.types import RecoveryError
from test_legacy_adoption_postgres import prepared, db, migrated_database
from test_owner_handoff_ready import ready_args
from test_owner_handoff_runtime import runtime_args, publish, repin, release, successor


# Retained replay 1: fastest complete observed replay (20.998508s).
# Paired aggregates divided evenly where per-observation values were not retained.
# Safety cost is four-call aggregate / 4; corrected code must call it THREE times.
# Authority includes one safety call. Its floor is inclusive, never additive.
PROFILE = {
    'observation': [5.1757247448, 5.1126127839],
    'db_snapshot': 3.5479743276 / 2,
    'credentials': 1.5379612967 / 2,
    'broker': 4.5169362649 / 2,
    'locked_db': 1.0772148799,
    'safety': 6.4618846569 / 4,
    'unsettled': 0.101255754,
    'authority_inclusive': 3.8132363651,
}

# Whole-path elapsed observations retained in report.md. These are not new
# production observations. Subtract only the established one-call safety saving
# estimate; do not discard unattributed elapsed time or grant unmeasured savings.
HISTORICAL_TOTALS = (20.998508, 22.140957, 21.538407)


class Meter:
    def __init__(self, enabled):
        self.enabled = enabled
        self.rows = []

    @contextmanager
    def stage(self, name, floor=0):
        started = time.perf_counter()
        try:
            yield
        finally:
            work = time.perf_counter() - started
            if self.enabled and floor > work:
                time.sleep(floor - work)
            self.rows.append(dict(stage=name, work_seconds=work,
                elapsed_seconds=time.perf_counter()-started, floor_seconds=floor if self.enabled else 0))


class Wire:
    """Complete fixed-account JSON responses; no socket or broker credentials."""
    def __init__(self, transcript):
        self.transcript = transcript
        self.answer = None

    def send(self, raw):
        request = json.loads(raw)
        kind = request['payloadType']
        assert kind in {2100, 2102, 2121, 2124, 2114, 2187, 2133}
        if kind != 2100:
            assert request['payload']['ctidTraderAccountId'] == 7
        self.transcript.append(kind)
        payload = {'ctidTraderAccountId': 7}
        if kind == 2121:
            payload['trader'] = dict(ctidTraderAccountId=7, moneyDigits=2, balance=989651)
        elif kind == 2124:
            payload.update(position=[], order=[])
        elif kind == 2114:
            payload['symbol'] = [dict(symbolId=1, symbolName='EURUSD'), dict(symbolId=2, symbolName='XAUUSD')]
        elif kind == 2187:
            payload.update(moneyDigits=2, positionUnrealizedPnL=[])
        elif kind == 2133:
            assert request['payload']['fromTimestamp'] == 1000000
            assert request['payload']['toTimestamp'] > 1790000000000
            assert request['payload']['maxRows'] == 1000
            payload.update(hasMore=False, deal=[dict(dealId=101+i, positionId=51+i,
                orderId=201+i, dealStatus=2, executionTimestamp=1790000000000+i,
                symbolId=1, tradeSide=2, filledVolume=10000, executionPrice='1.1',
                closePositionDetail={'closedVolume': 10000}) for i in range(16)])
        self.answer = json.dumps(dict(clientMsgId=request['clientMsgId'],
            payloadType=BrokerReader._responses[kind], payload=payload))

    def recv(self, **kwargs):
        assert self.answer is not None
        result, self.answer = self.answer, None
        return result

    def close(self):
        pass


@pytest.mark.parametrize('trial', [1, 2, 3, 4, 5, 6])
def test_representative_full_path(prepared, monkeypatch, trial):
    assert platform.system() == 'Linux' and os.geteuid() == 501 and os.getegid() == 1000
    assert [x.split(':')[0].strip() for x in Path('/proc/net/dev').read_text().splitlines()[2:]] == ['lo']
    assert os.environ['VERIFIER_TEST_PG_DSN'].startswith('postgresql+psycopg2://cert_user@/verifier_test?host=/tmp/fs-verifier-pg.')
    mode = os.environ.get('FRESHNESS_BENCHMARK_MODE', 'semantic')
    assert mode in {'semantic', 'representative', 'calibrated', 'production_calibrated', 'production_concurrent'}
    if mode in {'production_calibrated', 'production_concurrent'}:
        calibration=Path(__file__).with_name('freshness-calibration.json').read_bytes()
        assert hashlib.sha256(calibration).hexdigest()=='76f9baafe10afacd9119baf3613564c2406c1ac7beb703586b52623b0fbe7f88'
        samples = json.loads(calibration)['trials']
        profiles = []
        residuals = []
        for sample in samples:
            def values(stage):
                return [r['seconds'] for r in sample['timings'] if r['stage'] == stage]
            profile = dict(observation=values('complete_observation'),
                db_snapshot=max(values('database_snapshot')),
                credentials=max(values('credential_read')), broker=max(values('broker_complete')),
                locked_db=values('fresh_database')[0],
                safety=max(values('first_safety') + values('second_safety')),
                unsettled=values('unsettled')[0], authority_inclusive=values('authority')[0])
            # Preserve freeze/thaw, transaction setup and other unattributed work.
            accounted = sum(values('complete_observation')) + sum(values('first_safety')) + sum(values('second_safety')) + profile['locked_db'] + profile['unsettled'] + profile['authority_inclusive']
            residuals.append(max(0, sample['readonly_path_seconds'] - accounted))
            profiles.append(profile)
        if trial <= 3:
            PROFILE.update(profiles[trial-1])
            production_residual = residuals[trial-1]
        else:
            # Observed component maxima together: sensitivity envelope, not a
            # measured production joint sample or a statistical tail estimate.
            PROFILE.update({k: max(p[k] for p in profiles) for k in profiles[0] if k != 'observation'})
            PROFILE['observation'] = [max(p['observation'][i] for p in profiles) for i in range(2)]
            production_residual = max(residuals)
    meter = Meter(False)
    p = prepared
    factory, token = p['session_factory'], p['token']
    engine = factory.kw['bind']
    reader = DatabaseReader(engine, history_from=1000)
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # Deliberately synthetic credentials, incapable of real authentication.
    for name, value in {'CTRADER_CLIENT_ID': 'synthetic-only',
                        'CTRADER_CLIENT_SECRET': 'synthetic-only',
                        'CTRADER_TOKEN_ENCRYPTION_KEY': 'synthetic-only'}.items():
        monkeypatch.setenv(name, value)
    key = base64.urlsafe_b64encode(hashlib.sha256(b'flowsignal:ctrader:synthetic-only').digest())
    with factory.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}', updated_at=stamp, updated_by='synthetic'))
        session.add(StrategyStudioLiveState(owner_id='owner', enabled=True, updated_at=stamp))
        session.add(CTraderOAuthToken(provider='ctrader',
            encrypted_access_token=Fernet(key).encrypt(b'synthetic-only').decode(),
            updated_at=stamp, updated_by='synthetic'))
    if mode == 'production_concurrent':
        from models import IndicatorStreamGeneration
        selected = reader(p['token'].scope)['selection']['scope']
        # Synthetic structured OHLC-shaped JSON, never production state. Three
        # rows approximate the measured 3.34MB stream-generation snapshot.
        bars = [dict(timestamp=1790000000+i*60, open='1234.567', high='1236.789',
                     low='1230.123', close='1235.456', volume=12345) for i in range(10200)]
        with factory.begin() as session:
            for i in range(3):
                session.add(IndicatorStreamGeneration(root_key='synthetic'+str(i),
                    timeframe='M5', generation=1, storage_key='synthetic'+str(i), scope=selected,
                    public_symbol='EURUSD', status='ACTIVE', configuration_version='synthetic',
                    bootstrap_state={'bars': bars}, created_at=stamp))
    transcripts = []

    def connect(*args, **kwargs):
        transcript = []
        transcripts.append(transcript)
        return Wire(transcript)

    import websockets.sync.client
    monkeypatch.setattr(websockets.sync.client, 'connect', connect)

    class Safety:
        count = 0

        def __call__(self, scope):
            index = self.count
            self.count += 1
            with meter.stage('observation', PROFILE['observation'][min(index, 1)]):
                with meter.stage('db_snapshot', PROFILE['db_snapshot']):
                    database = reader(scope)
                with meter.stage('credentials', PROFILE['credentials']):
                    credentials = reader.credentials(scope)
                with meter.stage('broker', PROFILE['broker']):
                    with BrokerReader(scope.account_id, scope.environment, credentials,
                                      time.monotonic()+20) as broker_reader:
                        broker = broker_reader.snapshot(1000)
            return dict(database=database, broker=broker)

        def database(self, session, scope):
            with meter.stage('locked_db', PROFILE['locked_db']):
                return reader.read_connection(session.connection(), scope)

    safety = Safety()
    if mode == 'production_concurrent':
        from startup_recovery import handoff_reader
        class TimedDatabase(DatabaseReader):
            def __call__(self, scope):
                with meter.stage('db_snapshot', PROFILE['db_snapshot']):
                    return super().__call__(scope)
            def credentials(self, scope):
                with meter.stage('credentials', PROFILE['credentials']):
                    return super().credentials(scope)
        class TimedBroker(BrokerReader):
            def __enter__(self):
                self._meter = meter.stage('broker', PROFILE['broker'])
                self._meter.__enter__()
                return super().__enter__()
            def __exit__(self, *args):
                try: return super().__exit__(*args)
                finally: self._meter.__exit__(*args)
        monkeypatch.setattr(handoff_reader, 'DatabaseReader', TimedDatabase)
        monkeypatch.setattr(handoff_reader, 'BrokerReader', TimedBroker)
        class ConcurrentSafety(handoff_reader.ConcurrentHandoffReader):
            count = 0
            def __call__(self, scope):
                self.count += 1
                with meter.stage('observation'):
                    return super().__call__(scope)
            def database(self, session, scope):
                with meter.stage('locked_db', PROFILE['locked_db']):
                    return super().database(session, scope)
        safety = ConcurrentSafety(engine, history_from=1000)
    class AdoptionReader:
        def __call__(self, passed):
            return DiscoverySnapshot.create(passed, **safety(passed.scope))

        def database(self, session, passed):
            return safety.database(session, passed.scope)

    p['snapshot_reader'] = AdoptionReader()
    args = runtime_args.__wrapped__(ready_args.__wrapped__(p))
    args['safety_reader'] = safety
    for _ in range(5):
        successor(args)
    # Five adopted streams, each with five runtime successors, plus seventh-
    # generation monthly history: 6 heads / 37 total referenced lineage visits.
    kinds = ['live_backup', 'final_signal_hold', 'fifteen_m_swing_watch',
             'news_trading_state', 'ctrader_accounts', 'live_monthly_history']
    for kind in kinds:
        for _ in range(7 if kind == 'live_monthly_history' else 5):
            publish(args, kind)
    projection = repin(args)
    fixture_snapshot = reader(args['scope'])
    from startup_recovery.reconcile import _emit, _decimal_values
    fixture_snapshot_bytes = len(_emit(_decimal_values(fixture_snapshot)).encode())
    assert len(projection['authority']['heads']) == 6
    assert sum(len(h['lineage']) for h in projection['authority']['heads']) == 37
    assert args['expected_epoch'] == 6
    safety.count = 0
    transcripts.clear()
    meter.rows.clear()
    meter.enabled = mode in {'representative', 'calibrated', 'production_calibrated', 'production_concurrent'}
    freshness_checks = []
    original_fresh = owner_handoff._runtime_fresh
    historical_total = HISTORICAL_TOTALS[(trial - 1) % 3]
    projected_final_check = historical_total - PROFILE['safety']

    def fresh(broker, invocation_started):
        ordinal = len(freshness_checks) + 1
        if ordinal == 3 and mode in {'production_calibrated', 'production_concurrent'}:
            with meter.stage('observed_unattributed_work', production_residual):
                pass
        if ordinal == 3 and mode == 'calibrated':
            # Preserve previously omitted/unattributed elapsed variation. This
            # is a sensitivity envelope, not attribution to hashing or locks.
            before = time.monotonic() - invocation_started
            with meter.stage('unattributed_historical_elapsed',
                             max(0, projected_final_check - before)):
                pass
        elapsed = time.monotonic() - invocation_started
        freshness_checks.append(dict(ordinal=ordinal, elapsed_seconds=elapsed,
            broker_age_seconds=time.time()-broker['observed_at']))
        return original_fresh(broker, invocation_started)

    monkeypatch.setattr(owner_handoff, '_runtime_fresh', fresh)

    original_request = BrokerReader._request

    def request(self, kind, body):
        with meter.stage('broker_request_' + str(kind)):
            return original_request(self, kind, body)

    monkeypatch.setattr(BrokerReader, '_request', request)

    from startup_recovery import adoption_evidence, reconcile as reconciliation
    for module, name, stage in [
        (adoption_evidence, 'validate_adoption_receipt', 'receipt_validation'),
        (reconciliation, 'reconcile', 'normal_reconciliation'),
        (owner_handoff, 'digest', 'safety_digest_work'),
        (owner_handoff, '_runtime_provenance', 'provenance_work'),
    ]:
        original = getattr(module, name)

        def measured(*a, _original=original, _stage=stage, **kw):
            with meter.stage(_stage):
                return _original(*a, **kw)

        monkeypatch.setattr(module, name, measured)

    for name, stage, floor in [('_runtime_safety', 'canonical_safety', PROFILE['safety']),
                               ('_runtime_authority', 'authority_inclusive', PROFILE['authority_inclusive']),
                               ('_runtime_lineage', 'lineage', 0),
                               ('_runtime_generation', 'generation', 0)]:
        original = getattr(owner_handoff, name)

        def wrap(*a, _original=original, _stage=stage, _floor=floor, **kw):
            with meter.stage(_stage, _floor):
                return _original(*a, **kw)

        monkeypatch.setattr(owner_handoff, name, wrap)
    original_unsettled = store._unsettled

    def unsettled(*a, **kw):
        # Both the adapter and the unchanged mutation primitive check this.
        with meter.stage('unsettled', PROFILE['unsettled']):
            return original_unsettled(*a, **kw)

    monkeypatch.setattr(store, '_unsettled', unsettled)
    original_uow = owner_handoff.reconciliation_uow

    @contextmanager
    def uow(*a, **kw):
        started = time.perf_counter()
        with original_uow(*a, **kw) as value:
            meter.rows.append(dict(stage='account_lock_acquisition',
                elapsed_seconds=time.perf_counter()-started, work_seconds=time.perf_counter()-started,
                floor_seconds=0))
            yield value

    monkeypatch.setattr(owner_handoff, 'reconciliation_uow', uow)
    sql = []
    denied = []

    def database_fingerprint():
        with engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            rows = {}
            for table in sorted(tables):
                quoted = connection.dialect.identifier_preparer.quote(table)
                rows[table] = sorted(connection.execute(text(
                    f'SELECT row_to_json(t)::text FROM {quoted} AS t')).scalars())
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    def filesystem_fingerprint():
        root = Path(args['state_root'])
        result = []
        for path in sorted([root, *root.rglob('*')]):
            info = path.lstat()
            assert not path.is_symlink()
            content = None
            if path.is_file():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
                with os.fdopen(fd, 'rb') as stream:
                    content = hashlib.sha256(stream.read()).hexdigest()
            result.append((str(path.relative_to(root)), info.st_mode, info.st_uid,
                info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns, content))
        return result

    before_db = database_fingerprint()
    before_fs = filesystem_fingerprint()

    def before(conn, cursor, statement, params, context, many):
        operation = statement.lstrip().split(None, 1)[0].upper()
        if operation not in {'SELECT', 'SET', 'SHOW'}:
            denied.append(operation)
            raise RuntimeError('BENCHMARK_DML_DENIED_BEFORE_EXECUTION')
        context._bench_started = time.perf_counter()

    def after(conn, cursor, statement, params, context, many):
        sql.append(dict(seconds=time.perf_counter()-context._bench_started,
                        operation=statement.lstrip().split(None, 1)[0].upper()))

    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    started = time.perf_counter()
    outcome = None
    try:
        outcome = release(args).status
    except RecoveryError as exc:
        outcome = str(exc)
    finally:
        elapsed = time.perf_counter()-started
        event.remove(engine, 'before_cursor_execute', before)
        event.remove(engine, 'after_cursor_execute', after)
    database_unchanged = database_fingerprint() == before_db
    filesystem_unchanged = filesystem_fingerprint() == before_fs
    with factory() as session:
        account = store.account_state(session, args['scope'])
        phase = account.phase
        heads = session.query(RecoveryCheckpointHead).count()
    structure_ok = (transcripts == [[2100, 2102, 2121, 2124, 2114, 2187, 2133]] * 2
        and sum(r['stage'] == 'canonical_safety' for r in meter.rows) == 3
        and sum(r['stage'] == 'generation' for r in meter.rows) == 37
        and heads == 6
        and database_unchanged and filesystem_unchanged
        and phase == 'NEW_ENTRIES_READY')
    reached_write = (denied == ['UPDATE']
        and outcome == 'RECOVERY_HANDOFF_COMMIT_INSPECT_REQUIRED')
    deadline_blocked = (not denied and len(freshness_checks) == 3
        and freshness_checks[-1]['elapsed_seconds'] > 20
        and outcome == 'RECOVERY_TERMINATION_EVIDENCE_INVALID')
    report = dict(mode=mode, environment=dict(os=platform.system(), arch=platform.machine(),
        python=platform.python_version(), uid=os.geteuid(), gid=os.getegid(), network='loopback-only'),
        profile=PROFILE, outcome=outcome, total_seconds=elapsed, margin_to_20=20-elapsed,
        structure_pass=structure_ok,
        freshness_checks=freshness_checks, deadline_blocked=deadline_blocked,
        historical_total_seconds=historical_total,
        projected_final_check_seconds=projected_final_check,
        calibration_kind=('PRODUCTION_MEASURED_COST_FLOORS_NOT_TAIL_PROOF' if mode.startswith('production_')
                          else 'RETAINED_VARIATION_SENSITIVITY_NOT_PRODUCTION_TAIL_PROOF'),
        fixture_snapshot_bytes=fixture_snapshot_bytes,
        trial=trial, validation_reached_write_boundary=denied == ['UPDATE'],
        database_unchanged=database_unchanged, filesystem_unchanged=filesystem_unchanged,
        measured_database_writes=0, setup_writes='DISPOSABLE_FIXTURE_ONLY_BEFORE_MEASUREMENT',
        performance_pass=(structure_ok and reached_write and elapsed < 20)
            if mode in {'representative', 'calibrated', 'production_calibrated', 'production_concurrent'} else None,
        broker_observations=len(transcripts), broker_request_kinds=transcripts,
        checkpoint_heads=heads, referenced_lineage_visits=37, phase_after=phase,
        stages=meter.rows, sql=sql, production_access=False, real_broker_writes=0)
    (Path(os.environ['FRESHNESS_EVIDENCE']) / f'freshness-performance-{mode}-{trial}.json').write_text(json.dumps(report, indent=2)+'\n')
    assert transcripts == [[2100, 2102, 2121, 2124, 2114, 2187, 2133]] * 2
    assert sum(r['stage'] == 'canonical_safety' for r in meter.rows) == 3
    assert sum(r['stage'] == 'generation' for r in meter.rows) == 37
    assert structure_ok and (reached_write or deadline_blocked), report
    if mode in {'representative', 'calibrated', 'production_calibrated', 'production_concurrent'}:
        assert reached_write and elapsed < 20, f'CERTIFIED_DEADLINE_NOT_MET: {elapsed:.6f}s'
