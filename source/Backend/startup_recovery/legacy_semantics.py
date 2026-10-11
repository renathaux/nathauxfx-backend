"""Pure, deliberately bounded validation of transported legacy candidates.

This module has no IO capabilities. It neither repairs state nor supplies missing
execution identity. Its output is evidence, never producer/recovery admission.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from startup_recovery.checkpoints import _parse, _encoded
from startup_recovery.reconcile import checkpoint_dependencies, DiscoverySnapshot, _submission_pending
from startup_recovery.types import RecoveryError

KINDS = frozenset({'live_backup', 'final_signal_hold', 'fifteen_m_swing_watch',
                   'news_trading_state', 'ctrader_accounts'})
SYMBOLS = frozenset({'EURUSD', 'XAUUSD'})
WAITING = frozenset({'WAITING_FOR_5M_CONFIRMATION', 'WAIT_NEWS_ATR_UNAVAILABLE'})


def require(condition):
    if not condition:
        raise RecoveryError('LEGACY_ADOPTION_SEMANTICS_INVALID')


def timestamp(value):
    require(isinstance(value, str))
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None)


def watermark(value):
    require(type(value) in (int, Decimal))
    require(Decimal(value).is_finite() and value >= 0)


def symbol_times(value):
    require(isinstance(value, dict) and set(value) <= SYMBOLS)
    for stamp in value.values():
        watermark(stamp)


def news(payload):
    require(set(payload) == {'version', 'opportunities', 'normal_fresh_after', 'audit', 'mode_enabled_at'})
    require(type(payload['version']) is int and payload['version'] == 1)
    require(isinstance(payload['opportunities'], dict) and payload['audit'] == [])
    require(isinstance(payload['normal_fresh_after'], dict) and set(payload['normal_fresh_after']) <= SYMBOLS)
    for stamp in payload['normal_fresh_after'].values():
        timestamp(stamp)
    if payload['mode_enabled_at'] is not None:
        timestamp(payload['mode_enabled_at'])
    for key, record in payload['opportunities'].items():
        require(isinstance(record, dict) and set(record) == {
            'event_id', 'event_name', 'symbol', 'currency', 'release_time',
            'status', 'reason', 'consumed', 'updated_at', 'details'})
        require(isinstance(record['event_id'], str) and bool(record['event_id']))
        require(record['symbol'] in SYMBOLS)
        require(key == record['event_id'] + '|' + record['symbol'])
        require(isinstance(record['event_name'], str) and bool(record['event_name']))
        require(record['currency'] in ({'EUR', 'USD'} if record['symbol'] == 'EURUSD' else {'USD'}))
        timestamp(record['release_time'])
        timestamp(record['updated_at'])
        require(record['status'] == 'PENDING' and record['consumed'] is False)
        require(record['reason'] in WAITING)
        details = record['details']
        require(isinstance(details, dict) and set(details) == {'ok', 'reason'})
        require(details['ok'] is False and details['reason'] == record['reason'])
        # Deliberately no clock: an elapsed window stays PENDING, unconsumed.


def live(payload):
    require(set(payload) == {'account_close_times', 'live_active_orders', 'live_trade_history',
                            'live_last_execution_time', 'live_last_position_closed_at', 'last_live_reset'})
    require(payload['live_active_orders'] == {})
    require(isinstance(payload['live_trade_history'], list))
    for row in payload['live_trade_history']:
        require(isinstance(row, dict))
        # Historical rows are retained only; they never supply accepted execution.
        for name in ('status', 'result'):
            require(row.get(name) not in {'OPEN', 'RUNNING', 'TP1 HIT', 'TP2 HIT', 'CLOSING'})
    require(isinstance(payload['account_close_times'], dict))
    for scope, times in payload['account_close_times'].items():
        parts = scope.split(':')
        require(len(parts) == 3 and parts[0] == 'CTRADER' and parts[1] in {'DEMO', 'LIVE'} and bool(parts[2]))
        symbol_times(times)
    symbol_times(payload['live_last_execution_time'])
    symbol_times(payload['live_last_position_closed_at'])
    if payload['last_live_reset'] is not None:
        watermark(payload['last_live_reset'])


def accounts(payload, scope):
    require(set(payload) == {'active_account_id', 'active_account_env', 'accounts',
                            'forgotten_account_ids', 'last_refresh', 'active_account_snapshot',
                            'active_account_snapshot_refreshed_at'})
    account = payload['active_account_id']
    require(type(account) in (str, int) and str(account) == scope.account_id)
    require(payload['active_account_env'] == scope.environment)
    require(isinstance(payload['accounts'], list) and all(isinstance(r, dict) for r in payload['accounts']))
    require(isinstance(payload['forgotten_account_ids'], list))
    require(all(type(item) in (str, int) for item in payload['forgotten_account_ids']))
    require(scope.account_id not in {str(item) for item in payload['forgotten_account_ids']})
    require(payload['active_account_snapshot'] is None or isinstance(payload['active_account_snapshot'], dict))
    # Cached observations remain opaque observations, never broker discovery.
    for key in ('last_refresh', 'active_account_snapshot_refreshed_at'):
        if payload[key] is not None:
            timestamp(payload[key])


@dataclass(frozen=True)
class ValidatedLegacySet:
    canonical_payloads: tuple
    _dependencies: tuple

    @property
    def payloads(self):
        return {kind: _parse(raw) for kind, raw in self.canonical_payloads}

    @property
    def dependency_hashes(self):
        return dict(self._dependencies)


def validate_legacy_payloads(payloads, snapshot):
    try:
        require(isinstance(snapshot, DiscoverySnapshot))
        require(isinstance(payloads, dict) and set(payloads) == KINDS)
        require(all(isinstance(raw, bytes) for raw in payloads.values()))
        parsed = {kind: _parse(raw) for kind, raw in payloads.items()}
        require(all(isinstance(value, dict) for value in parsed.values()))
        db, broker, scope = snapshot.database, snapshot.broker, snapshot.token.scope
        selection = db['selection']
        require(selection['account_id'] == scope.account_id and selection['environment'] == scope.environment)
        require(selection['scope'] == f'CTRADER:{scope.environment.upper()}:{scope.account_id}')
        owners = db['runtime_owners']
        require(isinstance(owners, list) and len(owners) == 1 and isinstance(owners[0], str) and bool(owners[0]))
        require(broker['account_id'] == scope.account_id and broker['environment'] == scope.environment)
        require(broker['complete'] is True and broker['authenticated'] is True)
        require(broker['positions'] == [] and broker['orders'] == [])
        # Semantic validation supports only an unexposed, fully settled snapshot.
        for row in db['submissions']:
            # Use the same intent/protection/acceptance checks as reconciliation;
            # a terminal-looking status alone does not prove settlement.
            require(not _submission_pending(row))
        require(db['lifecycles'] == [])
        live_setting = [r for r in db['settings'] if r.get('setting_name') == 'live_auto_trade_enabled']
        require(len(live_setting) == 1 and live_setting[0]['setting_value'] == 'false')
        live(parsed['live_backup'])
        accounts(parsed['ctrader_accounts'], scope)
        news(parsed['news_trading_state'])
        # Populated legacy holds/watches have no reviewed complete execution-
        # identity schema yet; reject rather than rebind to current strategies.
        require(parsed['final_signal_hold'] == {} and parsed['fifteen_m_swing_watch'] == {})
        return ValidatedLegacySet(
            tuple((kind, _encoded(parsed[kind])) for kind in sorted(KINDS)),
            tuple((kind, checkpoint_dependencies(kind, db, parsed[kind])) for kind in sorted(KINDS)))
    except RecoveryError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise RecoveryError('LEGACY_ADOPTION_SEMANTICS_INVALID') from None
