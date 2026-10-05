"""Bounded explicit recovery readers; never call normal sync or selection helpers.

Protocol reference: spotware/openapi-proto-messages OpenApiMessages.proto and
OpenApiModelMessages.proto. Truncated history is blocked, never silently reduced.
No refresh, account-list auto-selection, broker mutation or ORM write capability.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import time
from uuid import uuid4

from sqlalchemy import select, cast, Text, text
from live_integrity.read_store import read_only
from live_integrity.ctrader_reader import Reader
from live_integrity.metadata import number, decimal_text
from live_integrity import snapshots
from startup_recovery.types import RecoveryError
from startup_recovery.reconcile import digest


def _json(value):
    return json.loads(value, parse_float=Decimal) if value is not None else None


class DatabaseReader:
    def __init__(self, engine, *, history_from):
        self.engine, self.history_from = engine, history_from

    @contextmanager
    def transaction(self):
        with read_only(self.engine) as connection:
            # Disposable tests use a private schema. Production uses its reviewed
            # engine schema, not any caller/request-supplied identifier.
            schema = self.engine.get_execution_options().get('schema_translate_map', {}).get(None)
            if schema:
                quoted = self.engine.dialect.identifier_preparer.quote(schema)
                connection.execute(text('SET LOCAL search_path TO ' + quoted))
            yield connection

    def __call__(self, scope):
        from models import StrategySetupLifecycle, TradeSubmissionAttempt, ForexExecutionSnapshot, RecoveryCheckpointHead
        from models import IndicatorStreamGeneration, IndicatorStreamHead, IndicatorEventLifecycle
        from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
        from startup_recovery.store import scope_key
        def rows(connection, model, predicate):
            columns = []
            json_fields = set()
            for column in model.__table__.columns:
                if column.type.__class__.__name__ == 'JSON':
                    columns.append(cast(column, Text).label(column.name))
                    json_fields.add(column.name)
                else:
                    columns.append(column)
            values = connection.execute(select(*columns).where(predicate).order_by(*model.__table__.primary_key.columns).limit(5001)).mappings().all()
            if len(values) > 5000:
                raise RecoveryError('RECOVERY_DB_SNAPSHOT_TOO_LARGE')
            result = []
            for value in values:
                item = dict(value)
                for key in json_fields: item[key] = _json(item[key])
                for key, field in item.items():
                    if isinstance(field, datetime):
                        if field.tzinfo is None:
                            raise RecoveryError('RECOVERY_DB_TIMESTAMP_UNVERIFIED')
                        item[key] = field.astimezone(timezone.utc).isoformat()
                result.append(item)
            return result
        with self.transaction() as connection:
            selection = snapshots.selected(connection)
            if selection['account_id'] != scope.account_id or selection['environment'] != scope.environment:
                raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
            settings = [dict(r) for r in connection.execute(text("SELECT setting_name,setting_value,updated_at FROM runtime_settings WHERE setting_name IN ('live_auto_trade_enabled','news_trading_mode','paper_auto_trade_enabled') ORDER BY setting_name")).mappings()]
            for setting in settings:
                setting['updated_at'] = str(setting['updated_at'])
            owners = connection.execute(text(
                'SELECT owner_id FROM strategy_studio_live_state WHERE enabled=true ORDER BY owner_id LIMIT 2'
            )).scalars().all()
            lifecycles = rows(connection, StrategySetupLifecycle, StrategySetupLifecycle.account_id == scope.account_id)
            execution_owners = {row['owner_id'] for row in lifecycles
                if row.get('broker_position_id') and row.get('status') in {'CONSUMED', 'SUBMITTING', 'RECONCILIATION_REQUIRED'}}
            referenced_owners = sorted(set(owners) | execution_owners)
            generations = rows(connection, IndicatorStreamGeneration, IndicatorStreamGeneration.scope == selection['scope'])
            return dict(selection=selection,
                lifecycles=lifecycles,
                submissions=rows(connection, TradeSubmissionAttempt, (TradeSubmissionAttempt.account_id == scope.account_id) & (TradeSubmissionAttempt.mode == 'LIVE')),
                legacy_executions=rows(connection, ForexExecutionSnapshot, (ForexExecutionSnapshot.account_id == scope.account_id) & (ForexExecutionSnapshot.broker_environment == scope.environment)),
                checkpoint_heads=rows(connection, RecoveryCheckpointHead, RecoveryCheckpointHead.scope_key == scope_key(scope)),
                settings_revision=digest(settings), settings=settings,
                runtime_owners=list(owners),
                stream_generations=generations,
                stream_heads=rows(connection, IndicatorStreamHead, IndicatorStreamHead.root_key.in_([row['root_key'] for row in generations])),
                event_lifecycles=rows(connection, IndicatorEventLifecycle,
                    (IndicatorEventLifecycle.account_id == scope.account_id) & (IndicatorEventLifecycle.mode == 'LIVE')),
                saved_strategies=rows(connection, SavedStrategy, SavedStrategy.owner_id.in_(referenced_owners)),
                strategy_selections=rows(connection, StrategyStudioSelection, StrategyStudioSelection.owner_id.in_(referenced_owners)),
                risk_history_required_from=self.history_from,
                required_checkpoints=['news_trading_state'] if any(s['setting_name']=='news_trading_mode' and s['setting_value'].upper() not in {'OFF','DISABLED','FALSE','0'} for s in settings) else [])

    def credentials(self, scope):
        with self.transaction() as connection:
            selected = snapshots.selected(connection)
            if selected['account_id'] != scope.account_id or selected['environment'] != scope.environment:
                raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
            return snapshots.credentials(connection)


def normalize_position(raw, symbols):
    trade = raw['tradeData']
    side = {1: 'BUY', 2: 'SELL', 'BUY': 'BUY', 'SELL': 'SELL'}.get(trade['tradeSide'])
    if not side or trade['symbolId'] not in symbols:
        raise ValueError('RECOVERY_POSITION_IDENTITY_UNVERIFIED')
    volume = number(trade['volume'])
    if volume <= 0 or volume != volume.to_integral_value():
        raise ValueError('RECOVERY_VOLUME_UNVERIFIED')
    return dict(position_id=str(raw['positionId']), symbol=symbols[trade['symbolId']],
        symbol_id=trade['symbolId'], side=side, volume_units=decimal_text(volume / 100),
        entry=decimal_text(raw['price']),
        sl=decimal_text(raw['stopLoss']) if 'stopLoss' in raw else None,
        tp2=decimal_text(raw['takeProfit']) if 'takeProfit' in raw else None)


class BrokerReader(Reader):
    _responses = {2100:2101, 2102:2103, 2112:2113, 2114:2115, 2116:2117,
                  2121:2122, 2124:2125, 2133:2134, 2137:2138, 2187:2188}

    def _receive(self):
        raw = self._socket.recv(timeout=min(2, self.remaining()))
        if len(raw) > 262144:
            raise ValueError('BROKER_RESPONSE_TOO_LARGE')
        event = json.loads(raw, parse_float=Decimal)
        if event.get('payloadType') in (2142, 2132):
            raise ValueError('BROKER_READ_UNAVAILABLE')
        return event

    def _request(self, kind, body):
        if kind not in self._responses:
            raise ValueError('BROKER_READ_ONLY_VIOLATION')
        self.remaining()
        body = dict(body)
        if kind != 2100:
            if 'ctidTraderAccountId' in body and str(body['ctidTraderAccountId']) != self.account:
                raise ValueError('BROKER_ACCOUNT_MISMATCH')
            body['ctidTraderAccountId'] = int(self.account)
        tag = str(uuid4())
        self._socket.send(json.dumps(dict(clientMsgId=tag, payloadType=kind, payload=body)))
        for _ in range(100):
            event = self._receive()
            if event.get('payloadType') != self._responses[kind] or event.get('clientMsgId') != tag:
                continue
            payload = event.get('payload', {})
            if kind != 2100 and str(payload.get('ctidTraderAccountId')) != self.account:
                raise ValueError('BROKER_ACCOUNT_MISMATCH')
            return payload
        raise ValueError('BROKER_READ_UNAVAILABLE')

    def submission_records(self,start,end):
        """Exact open-position + filled entry-order evidence, never a retry.

        Orders link the broker client ID to a position. A position label alone,
        a partial fill, or a truncated order list is not acceptance proof.
        """
        listed=self._request(2114,{'includeArchivedSymbols':True}).get('symbol',[])
        symbols={s['symbolId']:s['symbolName'] for s in listed}
        response=self._request(2124,{'returnProtectionOrders':False})
        positions=response.get('position',[])
        history=self._request(2137,dict(fromTimestamp=int(number(start)*1000),toTimestamp=int(number(end)*1000)))
        if history.get('hasMore') is not False or not isinstance(history.get('order',[]),list) or not isinstance(positions,list):
            raise ValueError('RECOVERY_HISTORY_INCOMPLETE')
        if len({str(p['positionId']) for p in positions})!=len(positions):
            raise ValueError('RECOVERY_POSITION_AMBIGUOUS')
        by_position={str(p['positionId']):p for p in positions}
        result=[]
        for order in history.get('order',[]):
            if order.get('closingOrder') is True or not order.get('clientOrderId'):
                continue
            if order.get('orderStatus') not in (2,'ORDER_STATUS_FILLED'):
                continue
            raw=by_position.get(str(order.get('positionId')))
            if raw is None: continue  # No inferred open position from historical absence.
            position=normalize_position(raw,symbols)
            trade=order['tradeData']
            side={1:'BUY',2:'SELL','BUY':'BUY','SELL':'SELL'}.get(trade['tradeSide'])
            if (trade['symbolId']!=position['symbol_id'] or side!=position['side']
                or number(order['executedVolume'])!=number(trade['volume'])
                or number(order['executedVolume'])/100!=number(position['volume_units'])
                or number(order['executionPrice'])!=number(position['entry'])):
                raise ValueError('RECOVERY_ACCEPTANCE_CONFLICT')
            result.append(dict(account_id=self.account,environment=self.environment,
                symbol=position['symbol'],symbol_id=position['symbol_id'],side=position['side'],
                position_id=position['position_id'],order_id=str(order['orderId']),
                client_order_id=order['clientOrderId'],entry=position['entry'],volume_units=position['volume_units']))
        return result

    def deals(self, start, end):
        response = self._request(2133, dict(fromTimestamp=int(number(start)*1000),
                                          toTimestamp=int(number(end)*1000), maxRows=1000))
        if response.get('hasMore') is not False:
            raise ValueError('RECOVERY_HISTORY_INCOMPLETE')
        values = response.get('deal', [])
        if not isinstance(values, list): raise ValueError('RECOVERY_HISTORY_INCOMPLETE')
        result = []
        for deal in values:
            status = deal.get('dealStatus')
            if status not in (2, 'FILLED'):
                raise ValueError('RECOVERY_HISTORY_UNSETTLED')
            millis = number(deal['executionTimestamp'])
            if millis != millis.to_integral_value():
                raise ValueError('RECOVERY_HISTORY_TIMESTAMP_INVALID')
            stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(millis))
            result.append(dict(deal_id=str(deal['dealId']), position_id=str(deal['positionId']),
                order_id=str(deal['orderId']), is_close='closePositionDetail' in deal,
                symbol_id=deal.get('symbolId'),
                side={1: 'BUY', 2: 'SELL', 'BUY': 'BUY', 'SELL': 'SELL'}.get(deal.get('tradeSide')),
                execution_price=deal.get('executionPrice'),
                volume_units=decimal_text(number(deal['filledVolume']) / 100),
                execution_timestamp=stamp.isoformat(), close_detail=deal.get('closePositionDetail')))
        return result

    def snapshot(self, history_from):
        account = self.account_state()
        listed = self._request(2114, {'includeArchivedSymbols': False}).get('symbol', [])
        symbols = {s['symbolId']: s['symbolName'] for s in listed}
        positions = [normalize_position(p, symbols) for p in account['positions']]
        pnl = self._request(2187, {})
        digits = pnl.get('moneyDigits')
        if type(digits) is not int or not 0 <= digits <= 8:
            raise ValueError('RECOVERY_EQUITY_UNVERIFIED')
        values = pnl.get('positionUnrealizedPnL', [])
        if {str(v['positionId']) for v in values} != {p['position_id'] for p in positions} or len(values) != len(positions):
            raise ValueError('RECOVERY_EQUITY_UNVERIFIED')
        equity = number(account['balance']) + sum((number(v['netUnrealizedPnL']) / (Decimal(10)**digits) for v in values), Decimal(0))
        metadata = {symbol: self.metadata(symbol) for symbol in sorted({p['symbol'] for p in positions})}
        history_to = time.time()
        deals = self.deals(history_from, history_to)
        for deal in deals:
            deal['symbol'] = symbols.get(deal['symbol_id'])
        return dict(account_id=self.account, environment=self.environment, authenticated=True,
            complete=True, positions=positions, orders=account['orders'], deals=deals,
            balance=account['balance'], equity=decimal_text(equity), metadata=metadata,
            history_from=history_from, history_to=history_to, history_complete=True,
            observed_at=history_to)


class ReadAdapters:
    """One bounded discovery operation, using only selected-account credentials."""
    def __init__(self, engine, *, history_from, broker_factory=BrokerReader):
        self.database = DatabaseReader(engine, history_from=history_from)
        self.history_from = history_from
        self.deadline = time.monotonic() + 20
        self._factory = broker_factory

    def authenticate(self, scope):
        if time.monotonic() >= self.deadline:
            raise RecoveryError('RECOVERY_DISCOVERY_TIMEOUT')
        try:
            credentials = self.database.credentials(scope)
            # Reader.__enter__ authenticates only this fixed selected account.
            # No discovery, selection repair, token refresh or order operation.
            with self._factory(scope.account_id, scope.environment, credentials, self.deadline):
                credentials = None
                return dict(authenticated=True, account_id=scope.account_id,
                            environment=scope.environment)
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError('RECOVERY_BROKER_AUTHENTICATION_FAILED') from None

    def submissions(self,scope,claimed_at):
        if claimed_at is None: raise RecoveryError('RECOVERY_HISTORY_BOUND_UNVERIFIED')
        if claimed_at.tzinfo is None: claimed_at=claimed_at.replace(tzinfo=timezone.utc)
        try:
            credentials=self.database.credentials(scope)
            with self._factory(scope.account_id,scope.environment,credentials,self.deadline) as reader:
                credentials=None
                records=reader.submission_records(claimed_at.timestamp(),time.time())
                return {'ok':True,'complete':True,'records':records}
        except Exception:
            raise RecoveryError('RECOVERY_BROKER_READ_FAILED') from None

    def broker(self, scope):
        if time.monotonic() >= self.deadline:
            raise RecoveryError('RECOVERY_DISCOVERY_TIMEOUT')
        try:
            credentials = self.database.credentials(scope)
            with self._factory(scope.account_id, scope.environment, credentials, self.deadline) as reader:
                credentials = None
                return reader.snapshot(self.history_from)
        except RecoveryError:
            raise
        except Exception:
            # Broker exceptions may include raw frames; never emit them or put
            # credential-bearing causes into the public recovery result.
            raise RecoveryError('RECOVERY_BROKER_READ_FAILED') from None
