"""Use the backend SMC indicator as the single BOS/CHoCH authority.

The visual switch in the browser must never control this module. The strategy
calls this adapter on every normal strategy evaluation, so hiding the overlay
only changes presentation.
"""
from __future__ import annotations

import copy

from indicators.smc import analyze_structure
from services.indicator_event_stream_service import (
    IndicatorStreamUnavailable,
    get_event_lifecycles,
    get_authoritative_structure,
    read_authoritative_structure,
)


AUTHORITY_SOURCE = "backend_smc_indicator"
AUTHORITY_CANDLE_LIMIT = 250
STRATEGY_DIAGNOSTIC_SWING_LIMIT = 20


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_invalidation(event):
    return (
        event.get("event_invalidation_swing")
        if isinstance(event, dict)
        and isinstance(event.get("event_invalidation_swing"), dict)
        else None
    )


def _event_structural_leg_size(event):
    level = _as_float((event or {}).get("broken_level"))
    invalidation_price = _as_float((_event_invalidation(event) or {}).get("price"))
    if level is None or invalidation_price is None:
        return None
    return abs(level - invalidation_price)


def _internal_two_bos_confirmation(analysis, current_event, minimum_swing):
    """Allow the second small internal BOS only after HH/HL or LH/LL confirms.

    This is strategy eligibility only. The SMC indicator itself remains
    untouched and continues to publish every BOS/CHoCH it detects.
    """
    current_direction = str((current_event or {}).get("direction") or "").upper()
    current_type = str((current_event or {}).get("event_type") or "").upper()
    try:
        current_index = int((current_event or {}).get("break_index", -1))
    except (TypeError, ValueError):
        current_index = -1

    details = {
        "qualified": False,
        "rule": "SECOND_SMALL_INTERNAL_BOS_CONFIRMS_STRUCTURE",
        "pattern": None,
        "current_direction": current_direction or None,
        "current_event_type": current_type or None,
        "current_leg_size": _event_structural_leg_size(current_event),
        "minimum_external_leg_size": minimum_swing,
        "previous_event": None,
        "reason": None,
    }

    if current_type != "BOS" or current_direction not in {"BULLISH", "BEARISH"}:
        details["reason"] = "current_event_is_not_internal_bos"
        return details

    prior_events = []
    for event in (analysis or {}).get("events") or []:
        if not isinstance(event, dict) or event is current_event:
            continue
        try:
            event_index = int(event.get("break_index", -1))
        except (TypeError, ValueError):
            continue
        if event_index < current_index:
            prior_events.append(event)

    if not prior_events:
        details["reason"] = "no_previous_structure_event"
        return details

    previous = prior_events[-1]
    previous_direction = str(previous.get("direction") or "").upper()
    previous_type = str(previous.get("event_type") or "").upper()
    previous_leg_size = _event_structural_leg_size(previous)
    previous_level = _as_float(previous.get("broken_level"))
    current_level = _as_float(current_event.get("broken_level"))
    previous_invalidation = _event_invalidation(previous) or {}
    current_invalidation = _event_invalidation(current_event) or {}
    previous_invalidation_price = _as_float(previous_invalidation.get("price"))
    current_invalidation_price = _as_float(current_invalidation.get("price"))

    details["previous_event"] = {
        "event_type": previous_type or None,
        "direction": previous_direction or None,
        "broken_level": previous_level,
        "invalidation_price": previous_invalidation_price,
        "leg_size": previous_leg_size,
        "break_index": previous.get("break_index"),
        "timestamp": previous.get("timestamp"),
    }

    if previous_type != "BOS" or previous_direction != current_direction:
        details["reason"] = "previous_event_is_not_same_direction_bos"
        return details
    if previous_leg_size is None or previous_leg_size >= minimum_swing:
        details["reason"] = "previous_bos_was_not_small_internal_structure"
        return details
    if details["current_leg_size"] is None or details["current_leg_size"] >= minimum_swing:
        details["reason"] = "current_bos_is_not_small_internal_structure"
        return details
    if None in {
        previous_level,
        current_level,
        previous_invalidation_price,
        current_invalidation_price,
    }:
        details["reason"] = "internal_structure_prices_missing"
        return details

    previous_invalidation_type = str(previous_invalidation.get("type") or "").upper()
    current_invalidation_type = str(current_invalidation.get("type") or "").upper()

    if current_direction == "BULLISH":
        pattern_ok = (
            previous_invalidation_type == "LOW"
            and current_invalidation_type == "LOW"
            and current_level > previous_level
            and current_invalidation_price > previous_invalidation_price
        )
        pattern = "HH_HL"
    else:
        pattern_ok = (
            previous_invalidation_type == "HIGH"
            and current_invalidation_type == "HIGH"
            and current_level < previous_level
            and current_invalidation_price < previous_invalidation_price
        )
        pattern = "LH_LL"

    details["pattern"] = pattern
    details["qualified"] = bool(pattern_ok)
    details["reason"] = (
        "second_small_bos_confirms_internal_structure"
        if pattern_ok
        else "second_small_bos_did_not_confirm_internal_structure"
    )
    return details


def _indicator_swings(analysis):
    output = []
    swings = (analysis or {}).get("swings") or []
    for swing in swings[-STRATEGY_DIAGNOSTIC_SWING_LIMIT:]:
        if not isinstance(swing, dict):
            continue
        output.append({
            "type": swing.get("type"),
            "price": swing.get("price"),
            "index": swing.get("index"),
            "time": swing.get("timestamp"),
            "confirmed_index": swing.get("confirmed_index"),
            "confirmed_time": swing.get("confirmed_timestamp"),
            "indicator_source": AUTHORITY_SOURCE,
        })
    return output


def _structure_context(analysis):
    analysis = analysis if isinstance(analysis, dict) else {}
    current = (
        analysis.get("current_structure")
        if isinstance(analysis.get("current_structure"), dict)
        else {}
    )
    bias = str(analysis.get("bias") or current.get("bias") or "NEUTRAL").upper()
    pattern = "HH_HL" if bias == "BULLISH" else "LH_LL" if bias == "BEARISH" else "NEUTRAL"
    return {
        "pattern": pattern,
        "bias": bias,
        "reason": "SMC_INDICATOR_AUTHORITY",
        "indicator_source": AUTHORITY_SOURCE,
        "protected_high": current.get("protected_high"),
        "protected_low": current.get("protected_low"),
        "bullish_continuation_frontier": current.get("bullish_continuation_frontier"),
        "bearish_continuation_frontier": current.get("bearish_continuation_frontier"),
    }


def _analysis_summary(analysis, candle_count):
    analysis = analysis if isinstance(analysis, dict) else {}
    events = analysis.get("events") or []
    swings = analysis.get("swings") or []
    latest_event = events[-1] if events and isinstance(events[-1], dict) else None
    return {
        "source": AUTHORITY_SOURCE,
        "candle_limit": AUTHORITY_CANDLE_LIMIT,
        "candle_count": candle_count,
        "bias": analysis.get("bias"),
        "event_count": len(events),
        "swing_count": len(swings),
        "latest_event": latest_event,
    }


def _base_result(analysis=None, candle_count=0):
    swings = _indicator_swings(analysis)
    return {
        "side": "WAIT",
        "level": None,
        "break_time": None,
        "break_close_time": None,
        "break_close": None,
        "remembered": False,
        "reason": "WAIT_NO_FRESH_15M_SMC_BREAK",
        "swings": swings,
        "raw_swings": swings,
        "structure": _structure_context(analysis),
        "breakouts": [],
        "bos_buffer": None,
        "event_invalidation_swing": None,
        "indicator_authority": True,
        "indicator_source": AUTHORITY_SOURCE,
        "indicator_summary": _analysis_summary(analysis, candle_count),
    }


def _marked_remembered_breakout(strict_trader_module, symbol, side, current_close_time, current_close):
    key = strict_trader_module.get_watch_key(symbol, side)
    watch = strict_trader_module.shared.FIFTEEN_M_SWING_WATCH.get(key)
    if not isinstance(watch, dict) or watch.get("source") != AUTHORITY_SOURCE:
        return None
    remembered = strict_trader_module.remembered_breakout(
        symbol,
        side,
        current_close_time=current_close_time,
        current_close=current_close,
    )
    if not isinstance(remembered, dict):
        return None
    remembered["event_invalidation_swing"] = watch.get("event_invalidation_swing")
    remembered["indicator_authority"] = True
    remembered["indicator_source"] = AUTHORITY_SOURCE
    return remembered


def evaluate_indicator_breakout(
    data_15m,
    symbol,
    execution_settings=None,
    *,
    strict_trader_module,
):
    """Return strict-trader breakout data using SMC indicator structure events.

    The indicator owns event existence, direction and BOS-vs-CHoCH
    classification. The strategy keeps the normal 100-point external leg rule,
    with one internal exception: after one sub-100-point BOS, a second
    same-direction sub-100-point BOS may qualify if it confirms HH/HL or LH/LL.
    Existing buffered 15m close, later 5m confirmation, EMA, consolidation,
    SL/TP, risk, duplicate, position and broker gates remain unchanged.

    A fixed 250-closed-candle authority window is used so chart and strategy see
    the same market-structure history and so strategy payloads remain bounded.
    """
    if data_15m is None or len(data_15m) < 10:
        result = _base_result(candle_count=0 if data_15m is None else len(data_15m))
        result["reason"] = "WAIT_NOT_ENOUGH_15M_DATA"
        return result

    authority_frame = data_15m.tail(AUTHORITY_CANDLE_LIMIT).copy()
    normalized_symbol = strict_trader_module.shared.normalize_symbol(symbol)
    configured = execution_settings or strict_trader_module.get_cached_execution_settings()
    required_buffer = strict_trader_module.bos_buffer(
        authority_frame,
        normalized_symbol,
        configured.get("bos_buffer_points", strict_trader_module.BOS_MIN_BUFFER_POINTS),
    )
    try:
        analysis = get_authoritative_structure(
            data_15m,
            normalized_symbol,
            "15m",
            strict_trader_module.point_size(normalized_symbol),
            analyzer=analyze_structure,
        )
    except IndicatorStreamUnavailable as exc:
        result = _base_result(candle_count=len(authority_frame))
        result["reason"] = "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE"
        result["indicator_stream_error"] = str(exc)
        return result
    result = _base_result(analysis, candle_count=len(authority_frame))
    result["bos_buffer"] = required_buffer

    last_index = len(authority_frame) - 1
    last_close = float(authority_frame.iloc[-1]["Close"])
    last_close_time = strict_trader_module.candle_close_time(authority_frame.index[-1], 15)
    tradable_events = [
        event
        for event in (analysis.get("events") or [])
        if isinstance(event, dict)
        and event.get("tradable") is True
        and str(event.get("direction") or "").upper() in {"BULLISH", "BEARISH"}
    ]
    max_age_minutes = 15 * int(
        getattr(strict_trader_module, "REMEMBERED_BREAKOUT_MAX_15M_CANDLES", 4)
    )
    eligible_events = []
    for event in tradable_events:
        try:
            event_close_time = strict_trader_module.candle_close_time(
                event.get("timestamp"), 15
            )
            age_seconds = (
                strict_trader_module.utc_timestamp(last_close_time)
                - strict_trader_module.utc_timestamp(event_close_time)
            ).total_seconds()
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= age_seconds <= max_age_minutes * 60:
            eligible_events.append(event)

    # A confirmed event remains truth even after its entry window expires.  This
    # prevents later candles from presenting a durable CHoCH/BOS as "no event".
    if tradable_events:
        latest_event = tradable_events[-1]
        result["indicator_event"] = latest_event
        result["indicator_event_id"] = latest_event.get("event_id")
        result["indicator_event_identity"] = latest_event.get("event_identity")
        result["indicator_event_type"] = str(
            latest_event.get("event_type") or "BOS"
        ).upper()
        result["indicator_event_truth"] = "CONFIRMED"
        result["indicator_event_expired"] = latest_event not in eligible_events
        result["entry_lifecycle_eligible"] = False

    # Once a watch exists, it exclusively owns continued entry eligibility.
    # Calling this before considering a new event also lets the existing expiry
    # and structural-invalidation checks clear the watch without the immutable
    # event recreating it later in this evaluation.
    remembered_candidates = []
    for side in ("BUY", "SELL"):
        remembered = _marked_remembered_breakout(
            strict_trader_module,
            normalized_symbol,
            side,
            current_close_time=last_close_time,
            current_close=last_close,
        )
        if remembered:
            remembered_candidates.append(remembered)

    if remembered_candidates:
        remembered = remembered_candidates[-1]
        remembered["structure"] = result["structure"]
        remembered["bos_buffer"] = float(remembered.get("bos_buffer") or required_buffer)
        remembered["indicator_summary"] = result["indicator_summary"]
        remembered["indicator_event_truth"] = "CONFIRMED"
        remembered["entry_lifecycle_eligible"] = True
        result.update(remembered)
        result["breakouts"] = remembered_candidates
        result["reason"] = f"SMC_INDICATOR_REMEMBERED_{str(remembered.get('break_type') or 'BOS').upper()}"
        return result

    latest_candle_timestamp = strict_trader_module.utc_timestamp(
        authority_frame.index[-1]
    )
    newly_persisted_ids = {
        str(value) for value in (analysis.get("new_event_ids") or []) if value
    }
    current_events = [
        event for event in eligible_events
        if strict_trader_module.utc_timestamp(event.get("timestamp"))
        == latest_candle_timestamp
    ]
    new_events = [
        event for event in current_events
        if str(event.get("event_id") or "") in newly_persisted_ids
    ]

    if current_events and not new_events:
        current_event_id = current_events[-1].get("event_id")
        lifecycle = (
            get_event_lifecycles(
                [current_event_id], owner_id="SYSTEM", account_id="SHARED"
            ).get(str(current_event_id), {}).get("LIVE")
            if current_event_id
            else None
        )
        if lifecycle:
            result["indicator_event_lifecycle"] = lifecycle
            result["entry_lifecycle_reason"] = (
                lifecycle.get("blocking_reason")
                or f"indicator event lifecycle is {lifecycle.get('status')}"
            )
            result["reason"] = "WAIT_DURABLE_EVENT_LIFECYCLE_INELIGIBLE"
            return result

    # A cleared current-candle setup must not be reconstructed on the next
    # strategy poll.  The shared lifecycle is written by the existing WAIT /
    # invalidation / consumption paths and survives process restarts.
    if new_events:
        new_event_id = new_events[-1].get("event_id")
        lifecycle = (
            get_event_lifecycles(
                [new_event_id], owner_id="SYSTEM", account_id="SHARED"
            ).get(str(new_event_id), {}).get("LIVE")
            if new_event_id
            else None
        )
        if lifecycle:
            result["indicator_event_lifecycle"] = lifecycle
            result["entry_lifecycle_reason"] = (
                lifecycle.get("blocking_reason")
                or f"indicator event lifecycle is {lifecycle.get('status')}"
            )
            result["reason"] = "WAIT_DURABLE_EVENT_LIFECYCLE_INELIGIBLE"
            return result

    if new_events:
        event = new_events[-1]
        direction = str(event.get("direction") or "").upper()
        side = "BUY" if direction == "BULLISH" else "SELL"
        level = _as_float(event.get("broken_level"))
        break_close = _as_float(event.get("close"))
        invalidation = _event_invalidation(event)
        invalidation_price = _as_float((invalidation or {}).get("price"))
        swing_size = _event_structural_leg_size(event)
        minimum_swing = strict_trader_module.minimum_swing_size(normalized_symbol)

        result["indicator_event"] = event
        result["indicator_event_id"] = event.get("event_id")
        result["indicator_event_identity"] = event.get("event_identity")
        result["indicator_event_type"] = str(event.get("event_type") or "BOS").upper()
        result["entry_lifecycle_eligible"] = True
        result["indicator_structural_leg_size"] = swing_size
        result["minimum_structural_leg_size"] = minimum_swing

        if level is None or break_close is None:
            result["reason"] = "WAIT_INVALID_INDICATOR_SMC_EVENT"
            return result

        internal_confirmation = None
        if swing_size is None:
            result["reason"] = "WAIT_NO_VALID_100_POINT_SWING"
            return result
        if swing_size < minimum_swing:
            internal_confirmation = _internal_two_bos_confirmation(
                analysis,
                event,
                minimum_swing,
            )
            result["internal_structure_confirmation"] = internal_confirmation
            if not internal_confirmation.get("qualified"):
                result["reason"] = "WAIT_NO_VALID_100_POINT_SWING"
                return result

        buffered = (
            break_close > level + required_buffer
            if side == "BUY"
            else break_close < level - required_buffer
        )
        if not buffered:
            result["reason"] = "WAIT_WEAK_15M_BOS"
            return result

        swing_type = "HIGH" if side == "BUY" else "LOW"
        valid_reason = (
            f"indicator_internal_two_bos_{str(internal_confirmation.get('pattern') or '').lower()}"
            if internal_confirmation and internal_confirmation.get("qualified")
            else "indicator_100_point_structure"
        )
        broken_swing = {
            "type": swing_type,
            "price": level,
            "index": event.get("structure_start_index"),
            "time": event.get("broken_swing_timestamp"),
            "swing_size": swing_size,
            "valid": True,
            "valid_reason": valid_reason,
            "indicator_source": AUTHORITY_SOURCE,
            "indicator_event_id": event.get("event_id"),
            "indicator_event_identity": event.get("event_identity"),
            "indicator_event_invalidation_swing": invalidation,
        }
        break_time = event.get("timestamp")
        candidate = {
            "side": side,
            "level": level,
            "break_time": break_time,
            "break_close_time": strict_trader_module.candle_close_time(break_time, 15),
            "break_close": break_close,
            "remembered": False,
            "bos_buffer": required_buffer,
            "swing": broken_swing,
            "break_type": str(event.get("event_type") or "BOS").upper(),
            "invalidation_level": invalidation_price,
            "event_invalidation_swing": invalidation,
            "indicator_authority": True,
            "indicator_source": AUTHORITY_SOURCE,
            "indicator_event": event,
            "indicator_event_id": event.get("event_id"),
            "indicator_event_identity": event.get("event_identity"),
            "setup_status": "WAITING_FOR_FILTERS",
            "strategy_structure_qualification": (
                "INTERNAL_TWO_BOS_CONFIRMATION"
                if internal_confirmation and internal_confirmation.get("qualified")
                else "EXTERNAL_100_POINT_LEG"
            ),
        }
        if internal_confirmation and internal_confirmation.get("qualified"):
            candidate["internal_structure_confirmation"] = internal_confirmation
        strict_trader_module.clear_opposite_watch(
            normalized_symbol,
            side,
            "opposite SMC indicator event",
        )
        result.update(candidate)
        result["breakouts"] = [candidate]
        result["swings"] = [broken_swing]
        result["raw_swings"] = [broken_swing]
        result["reason"] = f"SMC_INDICATOR_{candidate['break_type']}"
        return result

    if tradable_events:
        if result.get("indicator_event_expired"):
            result["reason"] = "WAIT_INDICATOR_EVENT_EXPIRED"
            result["entry_lifecycle_reason"] = "indicator event entry window expired"
        else:
            result["reason"] = "WAIT_DURABLE_EVENT_WATCH_INACTIVE"
            result["entry_lifecycle_reason"] = (
                "confirmed event has no active remembered breakout watch"
            )

    return result


def mark_indicator_breakout_watch(
    strict_trader_module,
    original_save_function,
    *args,
    **kwargs,
):
    """Persist the authority marker and structural invalidation with a watch."""
    result = original_save_function(*args, **kwargs)
    symbol = args[0] if len(args) > 0 else kwargs.get("symbol")
    side = args[1] if len(args) > 1 else kwargs.get("side")
    swing = args[8] if len(args) > 8 else kwargs.get("swing")
    if not isinstance(swing, dict) or swing.get("indicator_source") != AUTHORITY_SOURCE:
        return result
    key = strict_trader_module.get_watch_key(symbol, side)
    watch = strict_trader_module.shared.FIFTEEN_M_SWING_WATCH.get(key)
    if isinstance(watch, dict):
        watch["source"] = AUTHORITY_SOURCE
        watch["event_invalidation_swing"] = swing.get("indicator_event_invalidation_swing")
        strict_trader_module.shared.save_fifteen_m_swing_watch()
    return result


def build_chart_structure(frame, symbol, timeframe, *, strict_trader_module, display_limit=250):
    bounded_frame = (
        frame.tail(AUTHORITY_CANDLE_LIMIT).copy()
        if frame is not None
        else frame
    )
    analysis = read_authoritative_structure(
        bounded_frame,
        symbol,
        timeframe,
        strict_trader_module.point_size(symbol),
        analyzer=analyze_structure,
    )
    analysis["events"] = (analysis.get("events") or [])[-max(10, int(display_limit)):]
    lifecycles = get_event_lifecycles(
        [event.get("event_id") for event in analysis["events"]]
    )
    for event in analysis["events"]:
        event["setup_lifecycle"] = copy.deepcopy(
            lifecycles.get(event.get("event_id")) or {}
        )
    analysis["display_limit"] = int(display_limit)
    return {
        **analysis,
        "symbol": strict_trader_module.shared.normalize_symbol(symbol),
        "timeframe": str(timeframe).lower(),
        "closed_candle_count": len(bounded_frame) if bounded_frame is not None else 0,
        "authority_candle_limit": AUTHORITY_CANDLE_LIMIT,
        "source": AUTHORITY_SOURCE,
        "observation_only": True,
        "affects_strategy": str(timeframe).lower() == "15m",
        "strategy_authority": str(timeframe).lower() == "15m",
    }
