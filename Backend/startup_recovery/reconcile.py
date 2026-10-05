"""Pure field-specific recovery. No DB, transport, manager or checkpoint writes.

Current saved definitions cannot repair missing execution-time identities. A
negative result retains exposure counts and rejects candidates without moving,
rewriting, deleting or versioning their files.
"""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
import copy
import hashlib
import math

from live_integrity.binding import _emit, IDENTITY_FIELDS
from live_integrity.metadata import validate_metadata, number, value, decimal_text
from startup_recovery.checkpoints import Absent, AcceptedCheckpoint, validate_checkpoint
from startup_recovery.types import RecoveryError, ManagerToken


@dataclass(frozen=True)
class FrozenMap:
    items: tuple


def freeze(value):
    if isinstance(value, dict):
        return FrozenMap(tuple((k, freeze(v)) for k, v in sorted(value.items())))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if value is None or isinstance(value, (str, int, bool, Decimal)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise RecoveryError('RECOVERY_SNAPSHOT_INVALID')


def thaw(value):
    if isinstance(value, FrozenMap):
        return {k: thaw(v) for k, v in value.items}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def _decimal_values(value):
    if isinstance(value, dict): return {k: _decimal_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_decimal_values(v) for v in value]
    if isinstance(value, float): return Decimal(str(value))
    return value


def digest(value):
    return hashlib.sha256(_emit(_decimal_values(value)).encode()).hexdigest()


def scoped_events(database):
    """The same account's other owners do not consume this owner's hold."""
    owners = database.get('runtime_owners', [])
    account = (database.get('selection') or {}).get('account_id')
    return [row for row in database.get('event_lifecycles', [])
            if row.get('owner_id') in owners and row.get('account_id') == account
            and row.get('mode') == 'LIVE']


def checkpoint_references(kind, database, payload=None):
    """v3: payload-scoped authority, never polling telemetry or global row sets.

    Account selection (including its epoch/revision) remains a fence. Empty
    signal state has no strategy/stream/event dependency. Populated signal state
    binds selected saved definitions and streams for its symbols, plus only its
    referenced consumption/position records. Accepted execution checkpoints bind
    original execution identity, not the subsequently edited saved strategy.
    Previous v2 envelopes intentionally do not match this version; no rebind.
    """
    from startup_recovery.checkpoints import _kind
    _kind(kind)
    def rows(field):
        result = database.get(field)
        if not isinstance(result, list):
            raise RecoveryError('CHECKPOINT_DEPENDENCIES_UNAVAILABLE')
        return result
    def project(items, keys):
        return sorted(({k: row.get(k) for k in keys} for row in items), key=digest)
    if not isinstance(database.get('selection'), dict):
        raise RecoveryError('CHECKPOINT_DEPENDENCIES_UNAVAILABLE')
    refs = dict(selection=database['selection'], runtime_owners=sorted(rows('runtime_owners')))
    if kind in {'final_signal_hold', 'fifteen_m_swing_watch', 'news_trading_state'}:
        if not isinstance(payload, dict):
            raise RecoveryError('CHECKPOINT_DEPENDENCIES_UNAVAILABLE')
        records = payload.get('opportunities', {}) if kind == 'news_trading_state' else payload
        if not isinstance(records, dict) or any(not isinstance(v, dict) for v in records.values()):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        symbols = set()
        for key, record in records.items():
            symbol = str(record.get('symbol') or key).rsplit(':', 1)[-1]
            if symbol not in {'EURUSD', 'XAUUSD'}:
                raise RecoveryError('CHECKPOINT_DEPENDENCIES_UNAVAILABLE')
            symbols.add(symbol)
        if symbols:
            selections = [r for r in rows('strategy_selections') if r.get('owner_id') in refs['runtime_owners']]
            strategy_ids = {(r.get('owner_id'), r.get('strategy_id')) for r in selections}
            strategies = [r for r in rows('saved_strategies') if (r.get('owner_id'), r.get('strategy_id')) in strategy_ids]
            refs['strategy_selections'] = project(selections, ('owner_id', 'strategy_id'))
            refs['saved_strategies'] = project(strategies,
                ('owner_id', 'strategy_id', 'schema_version', 'updated_at', 'definition_json'))
            heads = rows('stream_heads')
            active = {(r.get('root_key'), r.get('timeframe'), r.get('active_generation')) for r in heads}
            generations = [r for r in rows('stream_generations') if r.get('public_symbol') in symbols
                and (r.get('status') == 'ACTIVE'
                     or (r.get('root_key'), r.get('timeframe'), r.get('generation')) in active)]
            roots = {(r.get('root_key'), r.get('timeframe')) for r in generations}
            refs['stream_generations'] = project(generations, ('root_key', 'timeframe', 'generation',
                'storage_key', 'scope', 'public_symbol', 'status', 'configuration_version'))
            refs['stream_heads'] = project([r for r in heads
                if (r.get('root_key'), r.get('timeframe')) in roots], ('root_key', 'timeframe', 'active_generation'))
            events = {r.get('source_indicator_event_id') or r.get('indicator_event_id') or r.get('event_id')
                      for r in records.values()} - {None, ''}
            rows('event_lifecycles')  # Distinguish an empty complete read from unavailable authority.
            refs['events'] = project([r for r in scoped_events(database) if r.get('event_id') in events],
                ('event_id', 'mode', 'owner_id', 'account_id', 'status', 'consumed_at',
                 'm5_confirmation_id', 'm5_confirmation_identity', 'signal_setup_id'))
            setups = {r.get('setup_id') or r.get('signal_setup_id') for r in records.values()} - {None, ''}
            refs['setups'] = project([r for r in rows('lifecycles') if r.get('setup_id') in setups],
                ('setup_id', 'owner_id', 'account_scope', 'strategy_id', 'status', 'entry_binding',
                 'broker_position_id', 'consumed_at'))
        if kind == 'news_trading_state':
            refs['news_mode'] = project([r for r in database.get('settings', [])
                if r.get('setting_name') == 'news_trading_mode'], ('setting_name', 'setting_value'))
    elif kind == 'live_backup':
        active = (payload or {}).get('live_active_orders', {})
        if not isinstance(active, dict) or any(not isinstance(v, dict) for v in active.values()):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        positions = {str(r.get('broker_position_id') or r.get('position_id')) for r in active.values()}
        refs['positions'] = project([r for r in rows('lifecycles') if str(r.get('broker_position_id')) in positions],
            ('setup_id', 'owner_id', 'account_scope', 'strategy_id', 'broker_position_id',
             'entry_binding', 'execution_snapshot', 'initial_volume_units'))
        # Open exposure/protection is reconciled from DB+broker independently;
        # historical watermarks do not depend on mutable management polling.
    return refs


def checkpoint_dependencies(kind, database, payload=None):
    return digest({'version': 'recovery-checkpoint-dependencies-v3', 'kind': kind,
                   'references': checkpoint_references(kind, database, payload)})


def checkpoint_execution_version(kind, database, payload):
    refs = checkpoint_references(kind, database, payload)
    # Consumption may legitimately advance after admission. It is validated
    # against the new payload separately, not mistaken for a config edit.
    return digest({k: v for k, v in refs.items() if k not in {'events', 'setups'}})


@dataclass(frozen=True)
class DiscoverySnapshot:
    token: ManagerToken
    _database: FrozenMap
    _broker: FrozenMap

    @classmethod
    def create(cls, token, database, broker):
        if not isinstance(token, ManagerToken):
            raise RecoveryError('RECOVERY_TOKEN_MISSING')
        return cls(token, freeze(database), freeze(broker))

    @property
    def database(self): return thaw(self._database)

    @property
    def broker(self): return thaw(self._broker)


@dataclass(frozen=True)
class RecoveryResult:
    epoch: int
    manifest_hash: str
    conflicts: tuple
    capacity_used: int
    _positions: tuple
    quarantined: tuple
    dependencies_hash: str
    _checkpoints: FrozenMap

    @property
    def ok(self): return not self.conflicts

    @property
    def positions(self): return tuple(thaw(p) for p in self._positions)

    @property
    def checkpoints(self): return thaw(self._checkpoints)


def _broker_comparison(broker):
    # Changing quote/equity samples are not identity drift; the second fresh
    # observation supplies the calculation inputs. Position/protection, balance,
    # order/deal and metadata *content* changes require a new complete pass.
    return {k: broker.get(k) for k in ('account_id', 'environment', 'authenticated',
        'complete', 'balance', 'positions', 'orders', 'deals', 'history_from',
        'history_complete')} | {'metadata': {k: v.get('metadata_hash')
        for k, v in broker.get('metadata', {}).items()}}


def _database_comparison(database):
    """Exclude only reviewed polling telemetry from discovery/admission identity.

    Saved-strategy updated_at is a version and account-selection revision is a
    fence: neither is excluded. All operation, protection and consumption facts
    remain evidence. The raw read-only snapshot is retained without alteration.
    """
    result = copy.deepcopy(database)
    for kind in ('lifecycles', 'submissions', 'event_lifecycles', 'strategy_selections', 'settings'):
        for row in result.get(kind, []):
            row.pop('updated_at', None)
            if kind == 'lifecycles' and isinstance(row.get('management_state'), dict):
                row['management_state'].pop('last_management_timestamp', None)
    # Reader's settings_revision hashes the same settings including updated_at;
    # use their semantic values instead of hashing this redundant polling hash.
    if 'settings' in result:
        result.pop('settings_revision', None)
    return result


def discover(readers, scope, token):
    if token.scope != scope:
        raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
    for _ in range(2):
        before = readers.database(scope)
        first = readers.broker(scope)
        second = readers.broker(scope)
        after = readers.database(scope)
        if digest(_database_comparison(before)) == digest(_database_comparison(after)) and digest(_broker_comparison(first)) == digest(_broker_comparison(second)):
            return DiscoverySnapshot.create(token, after, second)
    raise RecoveryError('RECOVERY_SNAPSHOT_UNSTABLE')


def _position(position, row, snapshot):
    scope = snapshot.token.scope
    scope_name = f'CTRADER:{scope.environment.upper()}:{scope.account_id}'
    if row.get('account_scope') != scope_name or row.get('account_id') != scope.account_id:
        raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
    binding = row.get('entry_binding') or {}
    identity = binding.get('strategy_identity') or {}
    if any(identity.get(k) in (None, '') for k in IDENTITY_FIELDS):
        raise RecoveryError('RECOVERY_STRATEGY_IDENTITY_MISSING')
    plan = binding.get('frozen_plan')
    if (not isinstance(plan, dict) or digest(plan) != binding.get('frozen_plan_hash')
        or digest(row.get('definition_snapshot')) != identity['config_hash']
        or identity['owner_id'] != row.get('owner_id') or identity['strategy_id'] != row.get('strategy_id')
        or plan.get('account_scope') != scope_name or plan.get('symbol') != position.get('symbol')
        or plan.get('side') != position.get('side') or row.get('symbol') != position.get('symbol')
        or row.get('direction') != position.get('side')):
        raise RecoveryError('RECOVERY_STRATEGY_IDENTITY_MISSING')
    execution = row.get('execution_snapshot')
    if not isinstance(execution, dict) or any(execution.get(k) is None for k in ('entry', 'initial_sl', 'tp2')):
        raise RecoveryError('RECOVERY_EXECUTION_SNAPSHOT_MISSING')
    durable=[a for a in snapshot.database.get('submissions',[])
             if a.get('signal_setup_id')==row.get('setup_id') and a.get('send_intent') is not None]
    accepted = None
    if durable:
        from services.accepted_execution import require_accepted_execution
        from types import SimpleNamespace
        if len(durable)!=1: raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT')
        accepted=require_accepted_execution(SimpleNamespace(**durable[0]))
        if (accepted['position_id']!=position['position_id'] or accepted['strategy_identity']!=identity
            or accepted['frozen_plan_hash']!=binding['frozen_plan_hash']):
            raise RecoveryError('RECOVERY_ACCEPTANCE_CONFLICT')
        # Derived recovery view only. The original requested-entry snapshot and
        # saved protection intent stay immutable; actual fill comes from ledger.
        execution={**execution,'requested_entry':execution['entry'],'entry':accepted['entry']}
    if (number(execution['entry']) != number(position.get('entry'))
        or number(execution['initial_sl']) != number(plan.get('sl'))
        or number(execution['tp2']) != number(plan.get('tp2'))):
        raise RecoveryError('RECOVERY_EXECUTION_SNAPSHOT_CONFLICT')
    try:
        intended_risk = number(plan.get('intended_risk_dollars'))
        original_balance = number(plan.get('account_balance'))
        if intended_risk <= 0 or original_balance <= 0:
            raise ValueError()
    except ValueError:
        raise RecoveryError('RECOVERY_RISK_UNRESOLVED') from None
    management = row.get('management_state') or {}
    if ((row.get('tp1_requested_at') and not row.get('tp1_completed_at'))
        or management.get('protection_state') in ('PENDING', 'FAILED')
        or row.get('status') != 'CONSUMED' or row.get('management_suspended_at')):
        raise RecoveryError('RECOVERY_OPERATIONS_UNRESOLVED')
    expected_sl = management.get('broker_confirmed_sl') if row.get('protection_applied_at') else execution['initial_sl']
    if row.get('protection_applied_at'):
        protection = plan.get('protection_prices') or {}
        allowed = [protection.get('protected')] + [s.get('protected') for s in protection.get('step_levels', [])]
        allowed = [number(p) for p in allowed if p is not None]
        if (management.get('protection_state') != 'CONFIRMED'
            or number(expected_sl) not in allowed
            or number(management.get('target_protected_sl')) != number(expected_sl)):
            raise RecoveryError('RECOVERY_PROTECTION_CONFLICT')
    protection_matches = (expected_sl is not None and position.get('sl') is not None
        and position.get('tp2') is not None and number(position['sl']) == number(expected_sl)
        and number(position['tp2']) == number(plan['tp2']))
    initial_protection_required = False
    if not protection_matches:
        # A crash after durable acceptance but before the first repair is not
        # missing execution identity. Admit management only, never a new entry.
        # Sent/ambiguous, legacy and later-management protection stay blocked.
        initial = (durable[0].get('initial_protection') or {}) if accepted else {}
        if (accepted is None or initial.get('version') != 1
            or initial.get('state') != 'UNASSESSED'
            or initial.get('accepted_execution_hash') != durable[0].get('accepted_execution_hash')
            or durable[0].get('attempt_status') != 'ACCEPTED'
            or durable[0]['send_intent'].get('state') != 'ACCEPTED'
            or row.get('tp1_requested_at') or row.get('protection_applied_at')
            or any(management.get(k) is not None for k in ('target_protected_sl', 'protection_state', 'tp1_state'))
            or number(position['volume_units']) != number(accepted['volume_units'])
            or position['symbol_id'] != accepted['symbol_id']
            or (position.get('tp2') is not None and number(position['tp2']) != number(accepted['intended_tp']))):
            raise RecoveryError('RECOVERY_PROTECTION_CONFLICT')
        sl = position.get('sl')
        if sl is not None and (number(sl) <= 0 or
            (number(sl) - number(accepted['intended_sl'])) * (1 if position['side'] == 'BUY' else -1) > 0):
            raise RecoveryError('RECOVERY_PROTECTION_CONFLICT')
        initial_protection_required = True
    initial_volume = number(row.get('initial_volume_units'))
    volume = number(position.get('volume_units'))
    if initial_volume <= 0 or volume <= 0:
        raise RecoveryError('RECOVERY_VOLUME_CONFLICT')
    if row.get('tp1_completed_at'):
        # A completion timestamp alone does not prove external partial-close
        # identity. Require the existing correlated deal/volume evidence.
        def stamp(value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None: raise ValueError()
            return parsed
        start, end = stamp(row['tp1_requested_at']), stamp(row['tp1_completed_at'])
        matching = [d for d in snapshot.broker.get('deals', [])
                    if d.get('position_id') == position['position_id'] and d.get('is_close') is True
                    and start <= stamp(d['execution_timestamp']) <= end]
        if (len(matching) != 1 or not matching[0].get('deal_id')
            or number(matching[0].get('volume_units')) != initial_volume - volume
            or number(management.get('tp1_requested_volume')) != initial_volume - volume
            or number(management.get('tp1_volume_before')) != initial_volume
            or management.get('tp1_partial_close_confirmed') is not True):
            raise RecoveryError('RECOVERY_VOLUME_CONFLICT')
    elif volume != initial_volume:
        raise RecoveryError('RECOVERY_VOLUME_CONFLICT')
    record = snapshot.broker.get('metadata', {}).get(position['symbol'])
    try:
        validate_metadata(record, account_id=scope.account_id, environment=scope.environment,
                          symbol=position['symbol'], now=snapshot.broker['observed_at'])
        if record['symbol_id'] != position['symbol_id']:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise RecoveryError('RECOVERY_METADATA_UNRESOLVED') from None
    current_risk = None  # Missing broker SL has unknown risk, never zero/default risk.
    if position.get('sl') is not None:
        distance = (number(position['entry']) - number(position['sl'])) * (1 if position['side'] == 'BUY' else -1)
        current_risk = decimal_text(max(Decimal(0), distance) / number(value(record, 'pip_size')) * volume / number(value(record, 'lot_size')) * number(value(record, 'pip_value_per_lot')))
    return dict(position_id=position['position_id'], owner_id=row['owner_id'], setup_id=row['setup_id'],
        strategy_id=row['strategy_id'], strategy_identity=identity, frozen_plan_hash=binding['frozen_plan_hash'],
        symbol=position['symbol'], side=position['side'], intended=plan, current=position,
        intended_risk=decimal_text(intended_risk), current_risk=current_risk,
        initial_protection_required=initial_protection_required,
        metadata_hash=record['metadata_hash'], execution_snapshot=execution, management_state=management)


def _submission_pending(row):
    if row.get('attempt_status') not in {'ACCEPTED','DEFINITELY_REJECTED','FAILED_BEFORE_SEND','ACCEPTED_PROTECTION_FAILED'}:
        return True
    intent=row.get('send_intent')
    if intent is not None:
        if intent.get('state') not in {'ACCEPTED','DEFINITELY_REJECTED'}: return True
        if intent.get('state')=='ACCEPTED':
            from services.accepted_execution import require_accepted_execution
            from types import SimpleNamespace
            try: require_accepted_execution(SimpleNamespace(**row))
            except (RecoveryError,KeyError,TypeError,ValueError,AttributeError): return True
    if (row.get('initial_protection') or {}).get('state') in {'UNRESOLVED','REQUEST_STARTED','RECONCILIATION_REQUIRED'}:
        return True
    return row.get('reconciliation_status') not in {'NOT_REQUIRED','RECONCILED','MATCHED'}


def reconcile(snapshot, checkpoint_candidates):
    db, broker = snapshot.database, snapshot.broker
    scope = snapshot.token.scope
    conflicts, positions, quarantined, accepted, accepted_hashes = [], [], [], {}, {}
    dependencies_hash = digest({'database': {k: v for k, v in _database_comparison(db).items() if k != 'checkpoint_heads'},
                                'broker': _broker_comparison(broker)})
    selected = db.get('selection') or {}
    if (selected.get('account_id') != scope.account_id or selected.get('environment') != scope.environment
        or not selected.get('revision') or broker.get('account_id') != scope.account_id
        or broker.get('environment') != scope.environment):
        conflicts.append('RECOVERY_ACCOUNT_CONFLICT')
    if broker.get('authenticated') is not True:
        conflicts.append('RECOVERY_AUTH_REAUTH_REQUIRED')
    if broker.get('complete') is not True or any(type(broker.get(k)) is not list for k in ('positions', 'orders', 'deals')):
        conflicts.append('RECOVERY_BROKER_INCOMPLETE')
    try:
        if number(broker.get('balance')) < 0 or number(broker.get('equity')) < 0:
            raise ValueError()
    except ValueError:
        conflicts.append('RECOVERY_RISK_UNRESOLVED')
    try:
        if (broker.get('history_complete') is not True
            or number(broker.get('history_from')) > number(db.get('risk_history_required_from'))
            or number(broker.get('history_to')) < number(broker.get('observed_at'))):
            raise ValueError()
    except ValueError:
        conflicts.append('RECOVERY_RISK_HISTORY_UNRESOLVED')
    pending = [r for r in db.get('submissions', []) if _submission_pending(r)]
    if pending or broker.get('orders'):
        conflicts.append('RECOVERY_OPERATIONS_UNRESOLVED')
    observed = broker.get('positions') if isinstance(broker.get('positions'), list) else []
    seen = set()
    for position in observed:
        pid = position.get('position_id')
        matches = [r for r in db.get('lifecycles', []) if r.get('broker_position_id') == pid]
        if not pid or pid in seen or len(matches) != 1:
            legacy = [r for r in db.get('legacy_executions', []) if r.get('position_id') == pid
                      and r.get('account_id') == scope.account_id and r.get('broker_environment') == scope.environment]
            conflicts.append('RECOVERY_STRATEGY_IDENTITY_MISSING' if len(legacy) == 1 and not matches
                             else 'RECOVERY_POSITION_AMBIGUOUS')
            continue
        seen.add(pid)
        try:
            positions.append(freeze(_position(position, matches[0], snapshot)))
        except RecoveryError as exc:
            conflicts.append(exc.code)
        except (ValueError, KeyError, TypeError):
            conflicts.append('RECOVERY_EXECUTION_EVIDENCE_INVALID')
    for row in db.get('lifecycles', []):
        if row.get('broker_position_id') and row.get('status') in {'CONSUMED', 'SUBMITTING', 'RECONCILIATION_REQUIRED'} and row['broker_position_id'] not in seen:
            conflicts.append('RECOVERY_POSITION_DISAPPEARED')
    for kind, candidate in checkpoint_candidates.items():
        if isinstance(candidate, Absent):
            continue  # Absence stays absence; consumer-specific requirements remain below.
        heads = [h for h in db.get('checkpoint_heads', []) if h.get('kind') == kind]
        if len(heads) == 1:
            head = heads[0]
            identity = head.get('identity') or {}
            if (identity.get('account_scope') == f'CTRADER:{scope.environment.upper()}:{scope.account_id}'
                and type(identity.get('epoch')) is int and identity['epoch'] <= snapshot.token.epoch):
                validated = validate_checkpoint(candidate, identity, accepted_hash=head.get('file_hash'))
                if isinstance(validated, AcceptedCheckpoint):
                    try:
                        payload = validated.payload
                        expected = checkpoint_dependencies(kind, db, payload)
                        if identity.get('dependencies_hash') != expected:
                            raise RecoveryError('CHECKPOINT_DEPENDENCIES_CHANGED')
                        if kind in {'paper_backup', 'visits'}:
                            from startup_recovery.publication import validate_produced_checkpoint
                            validate_produced_checkpoint(kind, payload, db)
                        accepted[kind] = payload
                        accepted_hashes[kind] = validated.checkpoint_hash
                        continue
                    except (RecoveryError, ValueError, TypeError, KeyError):
                        pass  # Retain candidate bytes; isolate non-LIVE corruption below.
        # A checksum alone proves no current compatibility. Reconciler has not
        # silently attached identities to candidates, even if they parse.
        quarantined.append(kind)
        if kind not in {'visits', 'paper_backup', 'feature_flags', 'news_trading_audit'}:
            conflicts.append('RECOVERY_CHECKPOINT_INCOMPATIBLE:' + kind)
    # Unknown missing file-only semantics must be represented explicitly by the
    # DB/source reader. Never infer unconsumed news just from an empty order list.
    for kind in db.get('required_checkpoints', []):
        if kind not in checkpoint_candidates or isinstance(checkpoint_candidates[kind], Absent):
            conflicts.append('RECOVERY_CHECKPOINT_MISSING:' + kind)
    for head in db.get('checkpoint_heads', []):
        kind = head.get('kind')
        if kind not in checkpoint_candidates or isinstance(checkpoint_candidates[kind], Absent):
            if kind not in {'visits', 'paper_backup', 'feature_flags', 'news_trading_audit'}:
                conflicts.append('RECOVERY_CHECKPOINT_MISSING:' + str(kind))
    manifest = digest({'epoch': snapshot.token.epoch, 'attempt_id': snapshot.token.attempt_id,
                       'dependencies_hash': dependencies_hash, 'accepted_checkpoints': accepted_hashes,
                       'conflicts': sorted(set(conflicts))})
    return RecoveryResult(snapshot.token.epoch, manifest, tuple(sorted(set(conflicts))),
        len(observed) + len(broker.get('orders') or []) + len(pending), tuple(positions),
        tuple(sorted(quarantined)), dependencies_hash, freeze(accepted))
