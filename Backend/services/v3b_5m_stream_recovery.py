"""Purpose-built non-executing recovery for account-scoped V3B streams.

This module is intentionally market-data + database only. It never imports the
live execution adapter, trade submission service, or any cTrader order/position
mutation helpers. Callers must provide authoritative CLOSED candles and this
service validates the full replacement suffix before a single transactional
repair is allowed.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from db import SessionLocal
from indicators.smc import analyze_structure as default_analyzer
from models import (
    IndicatorCandle,
    IndicatorEvent,
    IndicatorEventLifecycle,
    IndicatorStreamState,
    TradeSubmissionAttempt,
)
from services import auto_trade_state_service
from services import indicator_event_stream_service as stream
from services.indicator_stream_account_scope import (
    active_ctrader_stream_scope,
    storage_symbol_for_scope,
)


SUPPORTED_PUBLIC_SYMBOLS = {"EURUSD", "XAUUSD"}
SUPPORTED_TIMEFRAMES = {"5m": 5, "15m": 15, "1h": 60}
RECOVERY_SOURCE = "v3b_5m_admin_recovery"
IRREVERSIBLE_LIFECYCLE_STATUSES = {"SUBMITTING", "CONSUMED"}


class V3B5MRecoveryBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryRequest:
    account_id: str
    symbol: str
    timeframe: str
    storage_key: str
    dry_run: bool = True
    earliest_required_at: object | None = None


def _public_symbol(value):
    return str(value or "").upper().replace("/", "").split("~", 1)[0]


def _timeframe(value):
    text = str(value or "").strip().lower()
    return {
        "5min": "5m",
        "m5": "5m",
        "15min": "15m",
        "m15": "15m",
    }.get(text, text)


def _timeframe_minutes(value):
    timeframe = _timeframe(value)
    minutes = SUPPORTED_TIMEFRAMES.get(timeframe)
    if minutes is None:
        raise V3B5MRecoveryBlocked(
            f"V3B recovery supports {', '.join(SUPPORTED_TIMEFRAMES)} only"
        )
    return timeframe, minutes


def _utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp


def _db(value):
    return _utc(value).to_pydatetime()


def _iso(value):
    return _utc(value).isoformat() if value is not None else None


def resolve_verified_storage_key(account_id, symbol, supplied_storage_key):
    """Resolve the active cTrader scope and verify the supplied storage key."""
    public = _public_symbol(symbol)
    if public not in SUPPORTED_PUBLIC_SYMBOLS:
        raise V3B5MRecoveryBlocked(f"unsupported V3B recovery symbol: {public}")
    requested_account = str(account_id or "").strip()
    scope = active_ctrader_stream_scope()
    parts = str(scope or "").split(":")
    if len(parts) != 3 or parts[0] != "CTRADER" or parts[2] != requested_account:
        raise V3B5MRecoveryBlocked(
            f"active cTrader scope {scope or 'unavailable'} does not match account {requested_account}"
        )
    expected_key = storage_symbol_for_scope(public, scope)
    if str(supplied_storage_key or "").upper() != expected_key:
        raise V3B5MRecoveryBlocked(
            f"storage key mismatch for {public}: expected {expected_key}"
        )
    return public, scope, expected_key


def infer_earliest_rebuild_timestamp(state, events, explicit=None):
    """Find the earliest suffix timestamp needed to reproduce the stale class."""
    if explicit is not None:
        return _utc(explicit)
    reason = str(state.reconciliation_reason or "")
    prefix = "conflicting closed candle correction at "
    if reason.startswith(prefix):
        return _utc(reason[len(prefix):].strip())
    if reason == "late candle changed previously accepted structure events":
        event_times = [_utc(row.candle_timestamp) for row in events]
        if event_times:
            return min(event_times)
        if state.origin_candle is not None:
            return _utc(state.origin_candle)
    raise V3B5MRecoveryBlocked(
        "cannot infer earliest rollback/rebuild timestamp; pass one explicitly"
    )


def normalize_authoritative_closed_frame(frame):
    """Return a sorted CLOSED OHLC frame; duplicate rows are left detectable."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise V3B5MRecoveryBlocked("authoritative CLOSED cTrader history is empty")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        raise V3B5MRecoveryBlocked("authoritative history missing OHLC columns")
    closed = frame.loc[:, ["Open", "High", "Low", "Close"]].copy()
    closed.index = pd.DatetimeIndex([_utc(value) for value in closed.index])
    return closed.sort_index()


def _recognized_ctrader_sparse_gap(public_symbol, previous, following):
    """Allow only broker gaps that are already understood by production policy."""
    return stream._recognized_ctrader_sparse_gap(public_symbol, previous, following)


def validate_closed_history_coverage(
    frame,
    earliest,
    old_watermark,
    public_symbol=None,
    timeframe="5m",
):
    if old_watermark is None:
        raise V3B5MRecoveryBlocked("old durable watermark is unavailable")
    timeframe, interval_minutes = _timeframe_minutes(timeframe)
    earliest = _utc(earliest)
    old_watermark = _utc(old_watermark)
    if earliest > old_watermark:
        raise V3B5MRecoveryBlocked("earliest rebuild timestamp is after old watermark")

    raw_times = [_utc(value) for value in frame.index]
    duplicated = pd.DatetimeIndex(raw_times)
    duplicates = duplicated[duplicated.duplicated(keep=False)]
    if len(duplicates):
        raise V3B5MRecoveryBlocked(
            f"duplicate authoritative CLOSED timestamp {_iso(duplicates[0])}"
        )

    available = sorted({value for value in raw_times if value >= earliest})
    latest = available[-1] if available else None
    if latest is None or latest < old_watermark:
        raise V3B5MRecoveryBlocked(
            "authoritative CLOSED history does not reach old durable watermark "
            f"{_iso(old_watermark)}; latest closed is {_iso(latest)}"
        )
    if earliest not in available:
        raise V3B5MRecoveryBlocked(
            f"authoritative CLOSED history gap: missing {earliest.isoformat()}"
        )

    interval = pd.Timedelta(minutes=interval_minutes)
    sparse_gaps = []
    for timestamp in available:
        if (timestamp - earliest) % interval != pd.Timedelta(0):
            raise V3B5MRecoveryBlocked(
                f"authoritative CLOSED history gap: off-grid timestamp {timestamp.isoformat()}"
            )

    for previous, following in zip(available, available[1:]):
        if following - previous <= interval:
            continue
        missing = []
        cursor = previous + interval
        while cursor < following:
            missing.append(cursor)
            cursor += interval
        if not _recognized_ctrader_sparse_gap(public_symbol, previous, following):
            raise V3B5MRecoveryBlocked(
                f"authoritative CLOSED history gap: missing {missing[0].isoformat()}"
            )
        sparse_gaps.extend(value.isoformat() for value in missing)

    return {
        "earliest": earliest,
        "latest": latest,
        "count": len(available),
        "allowed_sparse_gaps": sparse_gaps,
    }


def _event_ids_for_rebuild(events, lifecycles, earliest):
    direct = {row.event_id for row in events if _utc(row.candle_timestamp) >= earliest}
    affected = set(direct)
    for row in lifecycles:
        if row.event_id in direct:
            affected.add(row.event_id)
            continue
        identity = row.m5_confirmation_identity or {}
        if not row.m5_confirmation_id:
            continue
        found_timestamp = False
        for key in ("candle_open_time", "candle_close_time", "confirmation_timestamp"):
            value = identity.get(key) if isinstance(identity, dict) else None
            if not value:
                continue
            found_timestamp = True
            if _utc(value) >= earliest:
                affected.add(row.event_id)
                break
        if not found_timestamp:
            affected.add(row.event_id)
    return affected


def _build_plan(session, request, closed_frame):
    public, scope, storage_key = resolve_verified_storage_key(
        request.account_id, request.symbol, request.storage_key
    )
    timeframe, _interval_minutes = _timeframe_minutes(request.timeframe)

    stream._database_lock(session, storage_key, timeframe)
    state = session.query(IndicatorStreamState).filter_by(
        symbol=storage_key, timeframe=timeframe
    ).with_for_update().one_or_none()
    if state is None:
        raise V3B5MRecoveryBlocked(
            f"stream {storage_key} {timeframe} is not initialized"
        )
    if state.status != "RECONCILIATION_REQUIRED":
        return {
            "safe": True,
            "idempotent": True,
            "reason": "stream is not reconciliation-required",
            "resolved_scoped_key": storage_key,
            "stream_scope": scope,
            "timeframe": timeframe,
            "old_watermark": _iso(state.last_processed_candle),
            "status": state.status,
        }

    event_rows = session.query(IndicatorEvent).filter_by(
        symbol=storage_key, timeframe=timeframe
    ).with_for_update().all()
    lifecycle_rows = []
    if event_rows:
        lifecycle_rows = session.query(IndicatorEventLifecycle).filter(
            IndicatorEventLifecycle.event_id.in_([row.event_id for row in event_rows])
        ).with_for_update().all()
    earliest = infer_earliest_rebuild_timestamp(
        state, event_rows, request.earliest_required_at
    )
    closed = normalize_authoritative_closed_frame(closed_frame)
    coverage = validate_closed_history_coverage(
        closed,
        earliest,
        state.last_processed_candle,
        public_symbol=public,
        timeframe=timeframe,
    )
    affected_event_ids = _event_ids_for_rebuild(event_rows, lifecycle_rows, earliest)
    affected_lifecycle = [row for row in lifecycle_rows if row.event_id in affected_event_ids]
    unsafe_lifecycle = next((
        row for row in affected_lifecycle
        if str(row.status or "").upper() in IRREVERSIBLE_LIFECYCLE_STATUSES
        or row.consumed_at is not None
    ), None)
    if unsafe_lifecycle is not None:
        raise V3B5MRecoveryBlocked(
            f"event {unsafe_lifecycle.event_id} lifecycle "
            f"{str(unsafe_lifecycle.status or 'UNKNOWN').upper()} is irreversible"
        )
    submission_rows = []
    if affected_event_ids:
        submission_rows = session.query(TradeSubmissionAttempt).filter(
            TradeSubmissionAttempt.event_id.in_(list(affected_event_ids))
        ).with_for_update().all()
    if submission_rows:
        raise V3B5MRecoveryBlocked(
            f"event {submission_rows[0].event_id} has trade_submission_attempt"
        )
    replace_candles = session.query(IndicatorCandle).filter(
        IndicatorCandle.symbol == storage_key,
        IndicatorCandle.timeframe == timeframe,
        IndicatorCandle.candle_timestamp >= _db(earliest),
    ).count()
    rebuilt_event_ids = []
    candle_rows = session.query(IndicatorCandle).filter(
        IndicatorCandle.symbol == storage_key,
        IndicatorCandle.timeframe == timeframe,
        IndicatorCandle.candle_timestamp < _db(earliest),
    ).order_by(
        IndicatorCandle.candle_timestamp.asc(), IndicatorCandle.id.asc()
    ).all()
    prefix = stream._stored_frame(candle_rows)
    replacement = closed[closed.index >= earliest]
    replay_frame = (
        pd.concat([prefix, replacement]).sort_index()
        if not prefix.empty
        else replacement
    )
    point_size = 0.01 if public == "XAUUSD" else 0.00001
    for raw_event in (
        default_analyzer(
            replay_frame,
            timeframe=timeframe,
            point_size=point_size,
        ) or {}
    ).get("events") or []:
        if not isinstance(raw_event, dict) or not raw_event.get("timestamp"):
            continue
        if _utc(raw_event["timestamp"]) >= earliest:
            _identity, event_id = stream.build_event_identity(
                raw_event,
                storage_key,
                timeframe,
                point_size,
            )
            rebuilt_event_ids.append(event_id)

    return {
        "safe": True,
        "idempotent": False,
        "reason": None,
        "resolved_scoped_key": storage_key,
        "public_symbol": public,
        "stream_scope": scope,
        "timeframe": timeframe,
        "earliest_rebuild_timestamp": earliest.isoformat(),
        "old_watermark": _iso(state.last_processed_candle),
        "fetched_closed_history_start": _iso(closed.index[0]),
        "fetched_closed_history_end": _iso(closed.index[-1]),
        "fetched_closed_history_count": int(len(closed)),
        "replacement_suffix_start": coverage["earliest"].isoformat(),
        "replacement_suffix_end": coverage["latest"].isoformat(),
        "replacement_suffix_count": coverage["count"],
        "allowed_sparse_gaps": coverage["allowed_sparse_gaps"],
        "candles_to_replace": replace_candles,
        "events_to_remove": sorted(affected_event_ids),
        "events_to_rebuild": sorted(set(rebuilt_event_ids)),
        "event_count_to_remove": len(affected_event_ids),
        "event_count_to_rebuild": len(set(rebuilt_event_ids)),
        "lifecycle_rows_affected": len(affected_lifecycle),
        "submission_attempts_affected": 0,
        "status_before": state.status,
        "reconciliation_reason_before": state.reconciliation_reason,
    }


def plan_recovery(request, closed_frame, *, session_factory=None):
    factory = session_factory or SessionLocal
    with stream._STREAM_LOCK:
        session = factory()
        try:
            return _build_plan(session, request, closed_frame)
        except V3B5MRecoveryBlocked as exc:
            return {"safe": False, "blocked_reason": str(exc)}
        finally:
            session.rollback()
            session.close()


def _require_live_auto_disabled(session_factory=None):
    state = auto_trade_state_service.load_state(
        session_factory=session_factory,
        force_refresh=True,
    )
    if bool((state or {}).get("live_enabled")):
        raise V3B5MRecoveryBlocked(
            "LIVE Auto must be disabled before recovery apply"
        )


def apply_recovery(request, closed_frame, point_size, *, analyzer=None, session_factory=None):
    """Apply one locked transaction after a successful dry-run plan."""
    if request.dry_run:
        return plan_recovery(request, closed_frame, session_factory=session_factory)

    _require_live_auto_disabled(session_factory=session_factory)
    factory = session_factory or SessionLocal
    effective_analyzer = analyzer or default_analyzer
    with stream._STREAM_LOCK:
        session = factory()
        try:
            now = datetime.now(timezone.utc)
            plan = _build_plan(session, request, closed_frame)
            if plan.get("idempotent"):
                session.rollback()
                return plan
            storage_key = plan["resolved_scoped_key"]
            timeframe = plan["timeframe"]
            earliest = _utc(plan["earliest_rebuild_timestamp"])
            old_watermark = _utc(plan["old_watermark"])
            closed = normalize_authoritative_closed_frame(closed_frame)

            session.query(IndicatorEventLifecycle).filter(
                IndicatorEventLifecycle.event_id.in_(plan["events_to_remove"])
            ).delete(synchronize_session=False)
            if plan["events_to_remove"]:
                session.query(IndicatorEvent).filter(
                    IndicatorEvent.event_id.in_(plan["events_to_remove"])
                ).delete(synchronize_session=False)
            session.query(IndicatorCandle).filter(
                IndicatorCandle.symbol == storage_key,
                IndicatorCandle.timeframe == timeframe,
                IndicatorCandle.candle_timestamp >= _db(earliest),
            ).delete(synchronize_session=False)

            replacement = closed[closed.index >= earliest]
            for timestamp, candle in replacement.iterrows():
                session.add(IndicatorCandle(
                    symbol=storage_key,
                    timeframe=timeframe,
                    candle_timestamp=_db(timestamp),
                    open_price=float(candle["Open"]),
                    high_price=float(candle["High"]),
                    low_price=float(candle["Low"]),
                    close_price=float(candle["Close"]),
                    created_at=now,
                ))
            session.flush()

            candle_rows = session.query(IndicatorCandle).filter_by(
                symbol=storage_key,
                timeframe=timeframe,
            ).order_by(
                IndicatorCandle.candle_timestamp.asc(),
                IndicatorCandle.id.asc(),
            ).all()
            canonical = stream._stored_frame(candle_rows)
            analysis = effective_analyzer(
                canonical,
                timeframe=timeframe,
                point_size=float(point_size),
            )
            persisted_ids = {
                value for (value,) in session.query(IndicatorEvent.event_id).filter_by(
                    symbol=storage_key,
                    timeframe=timeframe,
                ).all()
            }
            rebuilt_event_ids = []
            state = session.query(IndicatorStreamState).filter_by(
                symbol=storage_key,
                timeframe=timeframe,
            ).with_for_update().one()
            activation_watermark = state.activation_watermark
            for raw_event in (analysis or {}).get("events") or []:
                if not isinstance(raw_event, dict) or not raw_event.get("timestamp"):
                    continue
                event_time = _utc(raw_event["timestamp"])
                if event_time < earliest:
                    continue
                identity, event_id = stream.build_event_identity(
                    raw_event,
                    storage_key,
                    timeframe,
                    point_size,
                )
                if event_id in persisted_ids:
                    continue
                payload = stream._json_copy(raw_event)
                payload["event_id"] = event_id
                payload["event_identity"] = copy.deepcopy(identity)
                session.add(IndicatorEvent(
                    event_id=event_id,
                    symbol=storage_key,
                    timeframe=timeframe,
                    candle_timestamp=_db(event_time),
                    classification=identity["classification"],
                    direction=identity["direction"],
                    broken_level=float(raw_event["broken_level"]),
                    opposite_swing=stream._json_copy(identity["opposite_swing"]),
                    identity=stream._json_copy(identity),
                    payload=payload,
                    configuration_version=stream.CONFIGURATION_VERSION,
                    is_historical=bool(
                        activation_watermark
                        and event_time <= _utc(activation_watermark)
                    ),
                    created_at=now,
                ))
                persisted_ids.add(event_id)
                rebuilt_event_ids.append(event_id)

            latest_candle = _utc(canonical.index[-1])
            if latest_candle < old_watermark:
                raise V3B5MRecoveryBlocked("recovery would move watermark backward")

            # Re-check immediately before committing the repair. If LIVE Auto was
            # re-enabled while the recovery was being prepared, fail closed.
            _require_live_auto_disabled(session_factory=session_factory)
            state.last_processed_candle = _db(latest_candle)
            state.status = "READY"
            state.reconciliation_reason = None
            state.updated_at = now
            session.commit()
            output = dict(plan)
            output.update({
                "status_after": "READY",
                "new_watermark": latest_candle.isoformat(),
                "rebuilt_event_ids": rebuilt_event_ids,
                "recovery_source": RECOVERY_SOURCE,
            })
            return output
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def history_start_for_recovery(
    earliest_required_at,
    old_watermark,
    lookback_candles=250,
    timeframe="5m",
):
    _timeframe_name, interval_minutes = _timeframe_minutes(timeframe)
    earliest = _utc(earliest_required_at)
    old = _utc(old_watermark)
    floor = min(earliest, old)
    return floor - timedelta(minutes=interval_minutes * int(lookback_candles))
