"""Durable, authoritative closed-candle SMC indicator event stream."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError

from db import SessionLocal
from indicators.smc import analyze_structure as legacy_analyze_structure
from models import (
    IndicatorCandle,
    IndicatorEvent,
    IndicatorEventLifecycle,
    IndicatorStreamState,
)


CONFIGURATION_VERSION = "legacy-tradingview-smc-v1"
SUPPORTED_TIMEFRAMES = {"5m": 5, "15m": 15, "1h": 60}
_STREAM_LOCK = threading.RLock()
logger = logging.getLogger(__name__)
TEMPORARY_STATUSES = {"WAITING", "BLOCKED", "ELIGIBLE"}
IN_FLIGHT_STATUSES = {"SUBMITTING", "RECONCILIATION_REQUIRED"}
TERMINAL_STATUSES = {"CONSUMED", "EXPIRED", "INVALIDATED"}
ALL_LIFECYCLE_STATUSES = TEMPORARY_STATUSES | IN_FLIGHT_STATUSES | TERMINAL_STATUSES


class IndicatorStreamUnavailable(RuntimeError):
    pass


class IncomingCandleConflict(IndicatorStreamUnavailable):
    def __init__(self, timestamp):
        self.timestamp = _utc(timestamp)
        super().__init__(f"conflicting incoming closed candles at {self.timestamp.isoformat()}")


def _database_lock(session, symbol, timeframe):
    """Serialize a stream across Render instances when PostgreSQL is used."""
    if session.bind.dialect.name == "postgresql":
        key = int(hashlib.sha256(f"{symbol}:{timeframe}".encode()).hexdigest()[:15], 16)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _event_signature(event, symbol, timeframe, point_size):
    identity, event_id = build_event_identity(event, symbol, timeframe, point_size)
    return event_id, identity


def initialize_indicator_stream(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=legacy_analyze_structure,
    session_factory=None,
    allow_sparse_trendbars=False,
):
    """Deterministically backfill a stream and activate only future candles."""
    return get_authoritative_structure(
        frame,
        symbol,
        timeframe,
        point_size,
        analyzer=analyzer,
        session_factory=session_factory,
        initialize=True,
        allow_sparse_trendbars=allow_sparse_trendbars,
    )


def _normal_symbol(value):
    return str(value or "").upper().replace("/", "")


def _normal_timeframe(value):
    text = str(value or "").strip().lower()
    aliases = {"5min": "5m", "m5": "5m", "15min": "15m", "m15": "15m", "h1": "1h"}
    return aliases.get(text, text)


def _expected_market_candle(symbol, timestamp):
    """Known UTC weekly/daily venue closures; unknown holidays fail closed."""
    value = _utc(timestamp)
    if value.dayofweek == 5:
        return False
    if value.dayofweek == 4 and value.hour >= 21:
        return False
    if value.dayofweek == 6 and value.hour < (22 if symbol == "XAUUSD" else 21):
        return False
    if symbol == "XAUUSD" and value.hour == 21:
        return False
    return True


def _known_market_closure(symbol, previous, following):
    """Return true only for a recognizable UTC venue-close boundary."""
    previous = _utc(previous)
    following = _utc(following)
    # Weekend reopen. Forex resumes around 21:00 UTC; gold around 22:00 UTC.
    reopen_hour = 22 if symbol == "XAUUSD" else 21
    if previous.dayofweek == 4 and following.dayofweek == 6:
        return previous.hour <= 20 and following.hour == reopen_hour
    # Gold's daily maintenance/holiday pause always resumes at 22:00 UTC.
    if symbol == "XAUUSD" and previous.date() == following.date():
        return 18 <= previous.hour <= 20 and following.hour == 22
    return False


def _utc(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def _db_datetime(value):
    return _utc(value).to_pydatetime()


def _json_copy(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _price_text(value, point_size):
    point = float(point_size)
    decimals = max(0, min(10, len(f"{point:.10f}".rstrip("0").split(".")[-1])))
    return f"{float(value):.{decimals}f}"


def build_event_identity(event, symbol, timeframe, point_size):
    opposite = copy.deepcopy((event or {}).get("event_invalidation_swing") or {})
    identity = {
        "symbol": _normal_symbol(symbol),
        "timeframe": _normal_timeframe(timeframe),
        "candle_timestamp": _utc(event.get("timestamp")).isoformat(),
        "classification": str(event.get("event_type") or "").upper(),
        "direction": str(event.get("direction") or "").upper(),
        "broken_level": _price_text(event.get("broken_level"), point_size),
        "opposite_swing": {
            "type": str(opposite.get("type") or "").upper(),
            "price": (
                _price_text(opposite.get("price"), point_size)
                if opposite.get("price") is not None else None
            ),
            "swing_time": (
                _utc(opposite.get("swing_time") or opposite.get("time")).isoformat()
                if opposite.get("swing_time") or opposite.get("time") else None
            ),
        },
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return identity, f"smc1_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _canonical_input(frame):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise IndicatorStreamUnavailable("closed indicator candles unavailable")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        raise IndicatorStreamUnavailable("indicator candles have invalid columns")
    output = frame.loc[:, ["Open", "High", "Low", "Close"]].copy()
    output.index = pd.DatetimeIndex([_utc(value) for value in output.index])
    for timestamp, group in output.groupby(level=0, sort=False):
        first = tuple(float(group.iloc[0][key]) for key in ("Open", "High", "Low", "Close"))
        for row_index in range(1, len(group)):
            current = tuple(float(group.iloc[row_index][key]) for key in ("Open", "High", "Low", "Close"))
            if any(abs(a - b) > 1e-12 for a, b in zip(first, current)):
                raise IncomingCandleConflict(timestamp)
    output = output[~output.index.duplicated(keep="first")].sort_index()
    return output


def _stored_frame(rows):
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    return pd.DataFrame(
        [{
            "Date": _utc(row.candle_timestamp),
            "Open": row.open_price,
            "High": row.high_price,
            "Low": row.low_price,
            "Close": row.close_price,
        } for row in rows]
    ).set_index("Date").sort_index()


def _event_payload(row):
    payload = copy.deepcopy(row.payload or {})
    payload["event_id"] = row.event_id
    payload["symbol"] = row.symbol
    payload["timeframe"] = row.timeframe
    payload["timestamp"] = _utc(row.candle_timestamp).isoformat()
    payload["direction"] = row.direction
    payload["broken_level"] = row.broken_level
    payload["event_identity"] = copy.deepcopy(row.identity or {})
    payload["event_status"] = "CONFIRMED"
    payload["configuration_version"] = row.configuration_version
    payload["is_historical"] = bool(row.is_historical)
    payload["tradable"] = not bool(row.is_historical)
    return payload


def read_authoritative_event(event_id, *, session_factory=None):
    """Read one immutable indicator event without altering stream state.

    Execution-time guards use this to validate the event that created a setup;
    they must not rediscover an old pivot from a later, truncated candle frame.
    """
    if not event_id:
        return None
    factory = session_factory or SessionLocal
    session = factory()
    try:
        row = session.query(IndicatorEvent).filter(
            IndicatorEvent.event_id == str(event_id),
            IndicatorEvent.configuration_version == CONFIGURATION_VERSION,
        ).one_or_none()
        return _event_payload(row) if row is not None else None
    finally:
        session.close()


def get_authoritative_structure(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=legacy_analyze_structure,
    session_factory=None,
    initialize=False,
    allow_sparse_trendbars=False,
):
    """Merge closed candles and return the immutable event stream.

    Existing candles and events are never rewritten. New events are calculated
    by replaying all stored candles from the same durable origin. By default,
    unexplained interval gaps fail closed. ``allow_sparse_trendbars`` is only
    for providers such as cTrader that do not create a time bar when no tick
    arrived during that period; it never synthesizes replacement OHLC bars.
    """
    normalized_symbol = _normal_symbol(symbol)
    normalized_timeframe = _normal_timeframe(timeframe)
    if normalized_timeframe not in SUPPORTED_TIMEFRAMES:
        raise IndicatorStreamUnavailable("unsupported indicator timeframe")
    factory = session_factory or SessionLocal

    with _STREAM_LOCK:
        session = factory()
        try:
            now = datetime.now(timezone.utc)
            _database_lock(session, normalized_symbol, normalized_timeframe)
            state = session.query(IndicatorStreamState).filter(
                IndicatorStreamState.symbol == normalized_symbol,
                IndicatorStreamState.timeframe == normalized_timeframe,
            ).with_for_update().one_or_none()
            creating_stream = state is None
            if state is None:
                if not initialize:
                    raise IndicatorStreamUnavailable("indicator stream is not initialized")
                state = IndicatorStreamState(
                    symbol=normalized_symbol, timeframe=normalized_timeframe,
                    configuration_version=CONFIGURATION_VERSION,
                    status="INITIALIZING", updated_at=now,
                )
                session.add(state)
                session.flush()
            if state.configuration_version != CONFIGURATION_VERSION:
                raise IndicatorStreamUnavailable("indicator stream configuration version changed")
            if state.status == "RECONCILIATION_REQUIRED":
                raise IndicatorStreamUnavailable(state.reconciliation_reason or "indicator stream requires reconciliation")
            if not initialize and state.status not in {"READY", "GAP_BLOCKED"}:
                raise IndicatorStreamUnavailable(f"indicator stream is {state.status}")
            try:
                incoming = _canonical_input(frame)
            except IncomingCandleConflict as exc:
                state.status = "RECONCILIATION_REQUIRED"
                state.reconciliation_reason = str(exc)
                state.updated_at = now
                session.commit()
                raise

            existing_rows = session.query(IndicatorCandle).filter(
                IndicatorCandle.symbol == normalized_symbol,
                IndicatorCandle.timeframe == normalized_timeframe,
            ).all()
            existing = {_utc(row.candle_timestamp): row for row in existing_rows}
            latest_stored = max(existing) if existing else None
            late_insert = False

            new_rows = []
            for timestamp, candle in incoming.iterrows():
                candle_time = _utc(timestamp)
                prior = existing.get(candle_time)
                values = tuple(float(candle[key]) for key in ("Open", "High", "Low", "Close"))
                if prior is not None:
                    stored = (prior.open_price, prior.high_price, prior.low_price, prior.close_price)
                    if any(abs(float(a) - float(b)) > 1e-12 for a, b in zip(values, stored)):
                        state.status = "RECONCILIATION_REQUIRED"
                        state.reconciliation_reason = f"conflicting closed candle correction at {candle_time.isoformat()}"
                        state.updated_at = now
                        session.commit()
                        raise IndicatorStreamUnavailable(state.reconciliation_reason)
                    continue
                if latest_stored is not None and candle_time < latest_stored:
                    late_insert = True
                new_rows.append(IndicatorCandle(
                    symbol=normalized_symbol,
                    timeframe=normalized_timeframe,
                    candle_timestamp=_db_datetime(timestamp),
                    open_price=float(candle["Open"]),
                    high_price=float(candle["High"]),
                    low_price=float(candle["Low"]),
                    close_price=float(candle["Close"]),
                    created_at=now,
                ))
            if new_rows:
                session.bulk_save_objects(new_rows)
            session.flush()

            candle_rows = session.query(IndicatorCandle).filter(
                IndicatorCandle.symbol == normalized_symbol,
                IndicatorCandle.timeframe == normalized_timeframe,
            ).order_by(IndicatorCandle.candle_timestamp.asc(), IndicatorCandle.id.asc()).all()
            canonical = _stored_frame(candle_rows)
            if canonical.empty:
                raise IndicatorStreamUnavailable("authoritative candle stream is empty")

            analysis = analyzer(
                canonical,
                timeframe=normalized_timeframe,
                point_size=float(point_size),
            )
            watermark = _utc(state.last_processed_candle) if state and state.last_processed_candle else None
            persisted_ids = {
                value for (value,) in session.query(IndicatorEvent.event_id).filter(
                    IndicatorEvent.symbol == normalized_symbol,
                    IndicatorEvent.timeframe == normalized_timeframe,
                ).all()
            }
            analyzed_events = [raw for raw in (analysis or {}).get("events") or [] if isinstance(raw, dict) and raw.get("timestamp")]
            if late_insert and persisted_ids:
                rebuilt = {_event_signature(raw, normalized_symbol, normalized_timeframe, point_size)[0] for raw in analyzed_events if _utc(raw["timestamp"]) <= (watermark or _utc(canonical.index[-1]))}
                if rebuilt != persisted_ids:
                    state.status = "RECONCILIATION_REQUIRED"
                    state.reconciliation_reason = "late candle changed previously accepted structure events"
                    state.updated_at = now
                    session.commit()
                    raise IndicatorStreamUnavailable(state.reconciliation_reason)

            # Strict callers still fail closed on unexplained interval gaps.
            # cTrader callers may explicitly allow sparse trendbars because the
            # broker creates a bar only when at least one tick exists. Missing
            # timestamps are left absent rather than filled with synthetic OHLC.
            interval = pd.Timedelta(minutes=SUPPORTED_TIMEFRAMES[normalized_timeframe])
            gap_origin = watermark or (
                _utc(canonical.index[0]) - interval if initialize else None
            )
            if (
                not allow_sparse_trendbars
                and gap_origin is not None
                and _utc(canonical.index[-1]) > gap_origin
            ):
                missing = []
                ordered = [gap_origin] + [
                    _utc(value) for value in canonical.index if _utc(value) > gap_origin
                ]
                for previous, following in zip(ordered, ordered[1:]):
                    distance = following - previous
                    if interval < distance and not _known_market_closure(
                        normalized_symbol, previous, following
                    ):
                        cursor = previous + interval
                        while cursor < following:
                            if _expected_market_candle(normalized_symbol, cursor):
                                missing.append(cursor)
                            cursor += interval
                if missing:
                    state.status = "GAP_BLOCKED"
                    state.reconciliation_reason = "missing closed candles: " + ", ".join(value.isoformat() for value in missing[:5])
                    state.updated_at = now
                    session.commit()
                    raise IndicatorStreamUnavailable(state.reconciliation_reason)

            for raw_event in analyzed_events:
                if not isinstance(raw_event, dict) or not raw_event.get("timestamp"):
                    continue
                event_time = _utc(raw_event["timestamp"])
                if watermark is not None and event_time <= watermark:
                    continue
                identity, event_id = build_event_identity(
                    raw_event, normalized_symbol, normalized_timeframe, point_size
                )
                if event_id in persisted_ids:
                    continue
                payload = _json_copy(raw_event)
                payload["event_id"] = event_id
                payload["event_identity"] = identity
                session.add(IndicatorEvent(
                    event_id=event_id,
                    symbol=normalized_symbol,
                    timeframe=normalized_timeframe,
                    candle_timestamp=_db_datetime(event_time),
                    classification=identity["classification"],
                    direction=identity["direction"],
                    broken_level=float(raw_event["broken_level"]),
                    opposite_swing=_json_copy(identity["opposite_swing"]),
                    identity=_json_copy(identity),
                    payload=payload,
                    configuration_version=CONFIGURATION_VERSION,
                    is_historical=bool(
                        creating_stream
                        or (
                            state.activation_watermark
                            and event_time <= _utc(state.activation_watermark)
                        )
                    ),
                    created_at=now,
                ))
                persisted_ids.add(event_id)

            last_candle = _db_datetime(canonical.index[-1])
            if creating_stream:
                if state.origin_candle is None:
                    state.origin_candle = _db_datetime(canonical.index[0])
                state.activation_watermark = last_candle
            state.last_processed_candle = last_candle
            state.status = "READY"
            state.reconciliation_reason = None
            state.updated_at = now
            session.commit()

            event_rows = session.query(IndicatorEvent).filter(
                IndicatorEvent.symbol == normalized_symbol,
                IndicatorEvent.timeframe == normalized_timeframe,
                IndicatorEvent.configuration_version == CONFIGURATION_VERSION,
            ).order_by(IndicatorEvent.candle_timestamp.asc(), IndicatorEvent.event_id.asc()).all()
            events = [_event_payload(row) for row in event_rows]
            result = copy.deepcopy(analysis or {})
            result["events"] = events
            result["source"] = "authoritative_indicator_event_stream"
            result["configuration_version"] = CONFIGURATION_VERSION
            result["canonical_candle_count"] = len(canonical)
            result["stream_last_candle"] = _utc(canonical.index[-1]).isoformat()
            result["event_count"] = len(events)
            result["stream_status"] = state.status
            result["allow_sparse_trendbars"] = bool(allow_sparse_trendbars)
            result["activation_watermark"] = _utc(state.activation_watermark).isoformat() if state.activation_watermark else None
            return result
        except IndicatorStreamUnavailable:
            session.rollback()
            raise
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            if initialize:
                time.sleep(0.02)
                return get_authoritative_structure(
                    frame,
                    symbol,
                    timeframe,
                    point_size,
                    analyzer=analyzer,
                    session_factory=factory,
                    initialize=False,
                    allow_sparse_trendbars=allow_sparse_trendbars,
                )
            raise IndicatorStreamUnavailable(str(exc)) from exc
        except Exception as exc:
            session.rollback()
            raise IndicatorStreamUnavailable(str(exc)) from exc
        finally:
            session.close()


def read_authoritative_structure(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=legacy_analyze_structure,
    session_factory=None,
):
    """Read persisted events without changing stream state or candle history.

    The supplied frame is used only for the chart's visible swings/current
    structure.  Canonical events always come from the durable event table.
    """
    normalized_symbol = _normal_symbol(symbol)
    normalized_timeframe = _normal_timeframe(timeframe)
    if normalized_timeframe not in SUPPORTED_TIMEFRAMES:
        raise IndicatorStreamUnavailable("unsupported indicator timeframe")

    visible = _canonical_input(frame)
    factory = session_factory or SessionLocal
    session = factory()
    try:
        state = session.query(IndicatorStreamState).filter(
            IndicatorStreamState.symbol == normalized_symbol,
            IndicatorStreamState.timeframe == normalized_timeframe,
        ).one_or_none()
        if state is None:
            raise IndicatorStreamUnavailable("indicator stream is not initialized")
        if state.configuration_version != CONFIGURATION_VERSION:
            raise IndicatorStreamUnavailable(
                "indicator stream configuration version changed"
            )
        if state.status != "READY":
            reason = state.reconciliation_reason or f"indicator stream is {state.status}"
            raise IndicatorStreamUnavailable(reason)

        analysis = analyzer(
            visible,
            timeframe=normalized_timeframe,
            point_size=float(point_size),
        )
        event_rows = session.query(IndicatorEvent).filter(
            IndicatorEvent.symbol == normalized_symbol,
            IndicatorEvent.timeframe == normalized_timeframe,
            IndicatorEvent.configuration_version == CONFIGURATION_VERSION,
        ).order_by(
            IndicatorEvent.candle_timestamp.asc(),
            IndicatorEvent.event_id.asc(),
        ).all()
        events = [_event_payload(row) for row in event_rows]
        result = copy.deepcopy(analysis or {})
        result["events"] = events
        result["source"] = "authoritative_indicator_event_stream"
        result["configuration_version"] = CONFIGURATION_VERSION
        result["canonical_candle_count"] = session.query(IndicatorCandle).filter(
            IndicatorCandle.symbol == normalized_symbol,
            IndicatorCandle.timeframe == normalized_timeframe,
        ).count()
        result["stream_last_candle"] = (
            _utc(state.last_processed_candle).isoformat()
            if state.last_processed_candle else None
        )
        result["event_count"] = len(events)
        result["stream_status"] = state.status
        result["activation_watermark"] = (
            _utc(state.activation_watermark).isoformat()
            if state.activation_watermark else None
        )
        return result
    except IndicatorStreamUnavailable:
        raise
    except Exception as exc:
        raise IndicatorStreamUnavailable(str(exc)) from exc
    finally:
        session.close()


def update_event_lifecycle(
    event_id,
    mode,
    status,
    *,
    blocking_reason=None,
    m5_confirmation_id=None,
    m5_confirmation_identity=None,
    signal_setup_id=None,
    owner_id="SYSTEM",
    account_id="SHARED",
    session_factory=None,
):
    if not event_id:
        return False
    normalized_mode = str(mode or "LIVE").upper()
    normalized_status = str(status or "WAITING").upper()
    normalized_owner = str(owner_id or "SYSTEM")
    normalized_account = str(account_id or "SHARED")
    if normalized_status not in ALL_LIFECYCLE_STATUSES:
        return False
    factory = session_factory or SessionLocal
    with _STREAM_LOCK:
        session = factory()
        try:
            if session.get(IndicatorEvent, str(event_id)) is None:
                return False
            row = session.query(IndicatorEventLifecycle).filter(
                IndicatorEventLifecycle.event_id == str(event_id),
                IndicatorEventLifecycle.mode == normalized_mode,
                IndicatorEventLifecycle.owner_id == normalized_owner,
                IndicatorEventLifecycle.account_id == normalized_account,
            ).with_for_update().one_or_none()
            now = datetime.now(timezone.utc)
            if row is None:
                row = IndicatorEventLifecycle(
                    event_id=str(event_id),
                    mode=normalized_mode,
                    owner_id=normalized_owner,
                    account_id=normalized_account,
                    status=normalized_status,
                    blocking_reason=str(blocking_reason)[:255] if blocking_reason else None,
                    m5_confirmation_id=m5_confirmation_id,
                    m5_confirmation_identity=_json_copy(m5_confirmation_identity or {}),
                    signal_setup_id=signal_setup_id,
                    updated_at=now,
                    consumed_at=now if normalized_status == "CONSUMED" else None,
                )
                session.add(row)
                session.commit()
                return True
            current = str(row.status or "").upper()
            if current in TERMINAL_STATUSES and normalized_status != current:
                logger.warning("INDICATOR_LIFECYCLE_INVALID_TRANSITION event=%s mode=%s from=%s to=%s", event_id, normalized_mode, current, normalized_status)
                session.rollback()
                return False
            if current in IN_FLIGHT_STATUSES and normalized_status != current:
                logger.warning("INDICATOR_LIFECYCLE_INVALID_TRANSITION event=%s mode=%s from=%s to=%s", event_id, normalized_mode, current, normalized_status)
                session.rollback()
                return False
            values = {
                IndicatorEventLifecycle.status: normalized_status,
                IndicatorEventLifecycle.blocking_reason: str(blocking_reason)[:255] if blocking_reason else None,
                IndicatorEventLifecycle.m5_confirmation_id: m5_confirmation_id,
                IndicatorEventLifecycle.m5_confirmation_identity: _json_copy(m5_confirmation_identity or {}),
                IndicatorEventLifecycle.signal_setup_id: signal_setup_id,
                IndicatorEventLifecycle.updated_at: now,
            }
            if normalized_status == "CONSUMED" and row.consumed_at is None:
                values[IndicatorEventLifecycle.consumed_at] = now
            if normalized_status != "CONSUMED" and row.consumed_at is not None:
                logger.warning("INDICATOR_LIFECYCLE_CONSUMED_TIMESTAMP_CONFLICT event=%s mode=%s", event_id, normalized_mode)
                session.rollback()
                return False
            changed = session.query(IndicatorEventLifecycle).filter(
                IndicatorEventLifecycle.id == row.id,
                IndicatorEventLifecycle.status == current,
            ).update(values, synchronize_session=False)
            if changed != 1:
                session.rollback()
                logger.warning("INDICATOR_LIFECYCLE_TRANSITION_LOST event=%s mode=%s from=%s to=%s", event_id, normalized_mode, current, normalized_status)
                return False
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()


def get_event_lifecycles(event_ids, *, owner_id=None, account_id=None, session_factory=None):
    ids = [str(value) for value in event_ids if value]
    if not ids:
        return {}
    factory = session_factory or SessionLocal
    session = factory()
    try:
        query = session.query(IndicatorEventLifecycle).filter(
            IndicatorEventLifecycle.event_id.in_(ids)
        )
        if owner_id is not None:
            query = query.filter(IndicatorEventLifecycle.owner_id == str(owner_id))
        if account_id is not None:
            query = query.filter(IndicatorEventLifecycle.account_id == str(account_id))
        rows = query.order_by(IndicatorEventLifecycle.updated_at.asc()).all()
        output = {event_id: {} for event_id in ids}
        for row in rows:
            output.setdefault(row.event_id, {})[row.mode] = {
                "status": row.status,
                "owner_id": row.owner_id,
                "account_id": row.account_id,
                "blocking_reason": row.blocking_reason,
                "m5_confirmation_id": row.m5_confirmation_id,
                "m5_confirmation_identity": copy.deepcopy(row.m5_confirmation_identity or {}),
                "signal_setup_id": row.signal_setup_id,
                "updated_at": _utc(row.updated_at).isoformat(),
                "consumed_at": _utc(row.consumed_at).isoformat() if row.consumed_at else None,
            }
        return output
    except Exception:
        return {}
    finally:
        session.close()
