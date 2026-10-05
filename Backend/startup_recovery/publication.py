"""Pure staging for legacy runtime consumers. Never restores files or sends IO.

Staging is not admission: the coordinator must hold the account owner boundary
and commit reconciliation before a server adapter publishes these values.
"""
from dataclasses import dataclass
import copy
from datetime import datetime, timezone
from decimal import Decimal
from live_integrity.metadata import number, value as metadata_value

from startup_recovery.reconcile import freeze, thaw
from startup_recovery.types import RecoveryError


def execution_checkpoint_identity(payload):
    """Retain dispatch-time evidence, without upgrading unversioned execution."""
    binding = payload.get('studio_binding') or {}
    return dict(strategy_identity=copy.deepcopy(binding.get('strategy_identity')),
                frozen_plan_hash=binding.get('frozen_plan_hash'))


def validate_produced_checkpoint(kind, payload, database):
    """Corroborate new state; never fill its missing identity from current rows."""
    if kind == 'paper_backup':
        from strategies.shared import stage_admitted_paper_payload
        stage_admitted_paper_payload(payload)
    if kind == 'visits':
        if not isinstance(payload, list):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        for visit in payload:
            try:
                if (not isinstance(visit, dict) or set(visit) != {'time', 'visitor_id', 'country'}
                    or not isinstance(visit['visitor_id'], str) or not visit['visitor_id']
                    or not isinstance(visit['country'], str) or isinstance(visit['time'], bool)
                    or not isinstance(visit['time'], (int, float, Decimal))
                    or number(visit['time']) < 0):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise RecoveryError('CHECKPOINT_SCHEMA_INVALID') from None
    if kind == 'live_backup':
        active = payload.get('live_active_orders') if isinstance(payload, dict) else None
        if not isinstance(active, dict):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        for symbol, trade in active.items():
            if not isinstance(trade, dict):
                raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
            binding = trade.get('studio_binding') or {}
            identity = trade.get('strategy_identity') or binding.get('strategy_identity')
            plan_hash = trade.get('frozen_plan_hash') or binding.get('frozen_plan_hash')
            if not identity or not plan_hash:
                raise RecoveryError('CHECKPOINT_EXECUTION_IDENTITY_MISSING')
            matches = [row for row in database.get('lifecycles', [])
                if str(row.get('broker_position_id')) == str(trade.get('broker_position_id') or trade.get('position_id'))]
            if len(matches) != 1:
                raise RecoveryError('CHECKPOINT_EXECUTION_IDENTITY_CONFLICT')
            row = matches[0]
            authoritative = row.get('entry_binding') or {}
            if (row.get('status') != 'CONSUMED' or row.get('symbol') != symbol
                or row.get('account_scope') != trade.get('account_scope')
                or identity != authoritative.get('strategy_identity')
                or plan_hash != authoritative.get('frozen_plan_hash')
                or identity.get('owner_id') != row.get('owner_id')
                or identity.get('strategy_id') != row.get('strategy_id')):
                raise RecoveryError('CHECKPOINT_EXECUTION_IDENTITY_CONFLICT')
    if kind in {'final_signal_hold', 'fifteen_m_swing_watch'}:
        if not isinstance(payload, dict):
            raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
        from startup_recovery.reconcile import scoped_events
        consumed = {row['event_id'] for row in scoped_events(database)
                    if row.get('status') == 'CONSUMED'}
        for held in payload.values():
            if not isinstance(held, dict):
                raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
            event = held.get('source_indicator_event_id') or held.get('indicator_event_id') or held.get('event_id')
            if event in consumed and not held.get('consumed_at'):
                raise RecoveryError('CHECKPOINT_EVENT_ALREADY_CONSUMED')


def project_closed_history(deals, account_scope):
    """Complete broker facts only; no missing amount becomes zero/default profit."""
    rows, seen = [], set()
    try:
        for deal in deals:
            if deal.get('is_close') is not True:
                continue
            key = deal['deal_id']
            if not key or key in seen or deal['symbol'] not in {'EURUSD', 'XAUUSD'}:
                raise ValueError()
            seen.add(key)
            detail = deal['close_detail']
            digits = detail['moneyDigits']
            if type(digits) is not int or not 0 <= digits <= 8:
                raise ValueError()
            net = sum((number(detail[k]) for k in
                       ('grossProfit', 'swap', 'commission', 'pnlConversionFee')), Decimal(0)) / (Decimal(10) ** digits)
            # Legacy runtime summaries consume cent-rounded P/L. Do not introduce
            # a new rounding rule during recovery of a higher-precision account.
            if net != net.quantize(Decimal('.01')):
                raise ValueError()
            stamp = datetime.fromisoformat(deal['execution_timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or deal['side'] not in {'BUY', 'SELL'}:
                raise ValueError()
            delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
            closed_at = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000
            status = 'WIN' if net > 0 else 'LOSS' if net < 0 else 'BROKER_CLOSED'
            rows.append(dict(trade_id='ctrader-deal-' + str(key), deal_id=key,
                order_id=deal['order_id'], position_id=deal['position_id'],
                broker_position_id=deal['position_id'], account_scope=account_scope,
                symbol=deal['symbol'], side=deal['side'], status=status, result=status,
                entry=number(detail['entryPrice']), close_price=number(deal['execution_price']),
                volume_units=number(deal['volume_units']), pnl=net, profit=net,
                broker_pnl=net, broker_realized_profit=net, closed_at=closed_at,
                source='broker', history_source='ctrader_deal_list',
                broker_realized_source='ctrader.closePositionDetail'))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise RecoveryError('RECOVERY_RISK_HISTORY_UNRESOLVED') from None
    return sorted(rows, key=lambda row: (row['closed_at'], str(row['deal_id'])), reverse=True)


@dataclass(frozen=True)
class StagedRuntime:
    _active_orders: object
    _account: object
    _preferences: object
    _checkpoints: object
    manifest_hash: str

    @property
    def active_orders(self): return thaw(self._active_orders)
    @property
    def account(self): return thaw(self._account)
    @property
    def preferences(self): return thaw(self._preferences)
    @property
    def checkpoints(self): return thaw(self._checkpoints)


def stage(result, snapshot):
    if not result.ok or result.epoch != snapshot.token.epoch:
        raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
    scope = snapshot.token.scope
    scope_name = f'CTRADER:{scope.environment.upper()}:{scope.account_id}'
    active = {}
    for position in result.positions:
        symbol = position['symbol']
        # Existing runtime stores a single active position per symbol. Never
        # silently discard exposure or change its supported position semantics.
        if symbol in active or symbol not in {'EURUSD', 'XAUUSD'}:
            raise RecoveryError('RECOVERY_RUNTIME_POSITION_CAPACITY_UNSUPPORTED')
        current, plan = position['current'], position['intended']
        metadata = snapshot.broker['metadata'][symbol]
        active[symbol] = dict(
            account_id=scope.account_id, account_scope=scope_name,
            broker='ctrader', mode=scope.environment, symbol=symbol,
            side=position['side'], action=position['side'], status='OPEN', result='RUNNING',
            source='broker', execution_source='STRATEGY_STUDIO',
            position_id=position['position_id'], broker_position_id=position['position_id'],
            entry=current['entry'], sl=current['sl'], original_sl=plan['sl'], planned_sl=plan['sl'],
            tp1=plan['tp1'], tp2=current['tp2'], planned_tp1=plan['tp1'], planned_tp2=plan['tp2'],
            volume_units=current['volume_units'], risk_amount=position['intended_risk'],
            # Preserve already-reconciled constraints for the existing partial-
            # close consumer. Do not trigger its legacy metadata fallback during
            # the first recovered management pass. This is not entry authority.
            symbol_metadata={key: metadata_value(metadata, key) for key in
                ('min_volume_units', 'max_volume_units', 'volume_step_units')},
            account_balance_used=plan['account_balance'],
            studio_owner_id=position['owner_id'], studio_strategy_id=position['strategy_id'],
            studio_setup_id=position['setup_id'], studio_account_scope=scope_name,
            strategy_identity=position['strategy_identity'], frozen_plan_hash=position['frozen_plan_hash'],
            recovery_execution_snapshot=position['execution_snapshot'],
            trade_management=position['management_state'],
            recovery_epoch=result.epoch, recovery_manifest_hash=result.manifest_hash,
        )
    preferences = {}
    for row in snapshot.database.get('settings', []):
        name, value = row.get('setting_name'), row.get('setting_value')
        if name not in {'live_auto_trade_enabled', 'paper_auto_trade_enabled'}:
            continue
        normalized = str(value).strip().lower()
        if normalized not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}:
            raise RecoveryError('RECOVERY_PREFERENCE_INVALID')
        preferences[name] = normalized in {'true', '1', 'yes', 'on'}
    broker = snapshot.broker
    account = dict(account_id=scope.account_id, account_scope=scope_name,
        broker='ctrader', mode=scope.environment, balance=broker['balance'], equity=broker['equity'],
        auth_ok=True, connected=True, open_positions=result.capacity_used)
    return StagedRuntime(freeze(active), freeze(account), freeze(preferences),
                         freeze(result.checkpoints), result.manifest_hash)


def runtime_projection(result, snapshot):
    """Prepare all authoritative runtime fields before touching shared globals.

    File-only consumption/watch/preference state stays in checkpoints; it is
    never guessed from the absence of a broker position. Watermarks here are
    DB submission/broker close facts, not recreated signal state.
    """
    staged = stage(result, snapshot)
    database, broker = snapshot.database, snapshot.broker
    owners = set(database.get('runtime_owners') or [])
    owners.update(p['owner_id'] for p in result.positions)
    if len(owners) != 1 or not all(isinstance(owner, str) and owner for owner in owners):
        raise RecoveryError('RECOVERY_RUNTIME_OWNER_UNRESOLVED')
    scope_name = staged.account['account_scope']
    history = project_closed_history(broker['deals'], scope_name)
    last_execution, last_closed = dict(EURUSD=0, XAUUSD=0), dict(EURUSD=0, XAUUSD=0)
    for row in database.get('submissions', []):
        if row.get('request_started_at') is None:
            continue
        try:
            stamp = datetime.fromisoformat(row['request_started_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or row['symbol'] not in last_execution:
                raise ValueError()
            delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
            value = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000
            last_execution[row['symbol']] = max(last_execution[row['symbol']], value)
        except (KeyError, ValueError, TypeError):
            raise RecoveryError('RECOVERY_EXECUTION_WATERMARK_UNRESOLVED') from None
    for row in history:
        last_closed[row['symbol']] = max(last_closed[row['symbol']], row['closed_at'])
    # Only an already admitted compatible checkpoint can contribute monotonic
    # evidence. Never use arbitrary process globals or infer a missing deal.
    backup = staged.checkpoints.get('live_backup')
    if backup is not None:
        try:
            close = backup['account_close_times'].get(scope_name, {})
            execution = backup.get('live_last_execution_time', {})
            for source, target in ((close, last_closed), (execution, last_execution)):
                if not isinstance(source, dict):
                    raise ValueError()
                for symbol, stamp in source.items():
                    if symbol not in target or isinstance(stamp, bool) or number(stamp) < 0:
                        raise ValueError()
                    target[symbol] = max(target[symbol], number(stamp))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RecoveryError('RECOVERY_EXECUTION_WATERMARK_UNRESOLVED') from None
    return dict(owner_id=next(iter(owners)), active_orders=staged.active_orders,
        account=staged.account, preferences=staged.preferences, checkpoints=staged.checkpoints,
        closed_history=history,
        # Legacy cooldowns consume time.time() floats; canonical evidence above
        # remains exact. This conversion never touches prices or frozen intent.
        last_execution_time={k: float(v) for k, v in last_execution.items()},
        last_position_closed_at={k: float(v) for k, v in last_closed.items()},
        manifest_hash=staged.manifest_hash)
