from datetime import datetime, timezone

from indicators.smc import detect_confirmed_swings
from services.indicator_event_stream_service import (
    IndicatorStreamUnavailable,
    read_authoritative_structure,
)


SWING_CHANGED_REASON = "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION"


def _parse_timestamp(value):
    if value in [None, "", "--"]:
        return None
    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1e12:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _expected_indicator_direction(identity):
    side = str((identity or {}).get("direction") or "").upper()
    if side in {"BUY", "BULLISH"}:
        return "BULLISH"
    if side in {"SELL", "BEARISH"}:
        return "BEARISH"
    return None


def _authoritative_event_swing_match(
    closed_15m,
    normalized_symbol,
    identity,
    expected_type,
    expected_time,
    expected_price,
    tolerance,
    strict_trader_module,
):
    """Verify an old setup pivot against the immutable indicator event stream.

    The final execution window can be shorter than the authority history. When
    the original broken pivot has aged out of the local frame, recomputing
    swings from that short frame cannot prove the pivot changed. The immutable
    event record can: it owns the exact broken level and broken-swing timestamp
    accepted when the BOS/CHoCH was confirmed.
    """
    try:
        authority = read_authoritative_structure(
            closed_15m.copy(),
            normalized_symbol,
            "15m",
            strict_trader_module.point_size(normalized_symbol),
        )
    except (IndicatorStreamUnavailable, Exception) as exc:
        return None, {
            "authoritative_setup_swing_check": "unavailable",
            "authoritative_setup_swing_error": str(exc),
        }

    expected_direction = _expected_indicator_direction(identity)
    expected_broken_type = (
        "HIGH" if expected_direction == "BULLISH"
        else "LOW" if expected_direction == "BEARISH"
        else expected_type
    )
    events = [
        event
        for event in (authority or {}).get("events") or []
        if isinstance(event, dict) and event.get("tradable") is True
    ]
    diagnostic = {
        "authoritative_setup_swing_check": "checked",
        "authoritative_setup_event_count": len(events),
        "authoritative_setup_swing_matched": False,
    }

    for event in reversed(events):
        event_direction = str(event.get("direction") or "").upper()
        if expected_direction and event_direction != expected_direction:
            continue
        if expected_type != expected_broken_type:
            continue
        event_time = _parse_timestamp(event.get("broken_swing_timestamp"))
        try:
            event_price = float(event.get("broken_level"))
        except (TypeError, ValueError):
            continue
        if (
            event_time == expected_time
            and abs(event_price - expected_price) <= tolerance
        ):
            diagnostic.update({
                "authoritative_setup_swing_matched": True,
                "authoritative_setup_event_id": event.get("event_id"),
                "authoritative_setup_event_type": event.get("event_type"),
                "authoritative_setup_event_direction": event_direction or None,
                "authoritative_setup_broken_swing": {
                    "type": expected_broken_type,
                    "time": event.get("broken_swing_timestamp"),
                    "price": event_price,
                },
            })
            return event, diagnostic

    return None, diagnostic


def validate_fresh_setup_swing_identity(
    closed_15m,
    symbol,
    setup_identity,
    strict_trader_module,
):
    """Revalidate the immutable setup pivot without re-qualifying its old leg.

    Authoritative SMC events are checked first because they are the immutable
    source that created new setups. Confirmed-pivot and legacy raw-pivot checks
    remain as compatibility fallbacks for older setups. EMA/consolidation and
    the setup fingerprint are validated by the existing execution gates
    separately.
    """
    identity = setup_identity if isinstance(setup_identity, dict) else {}
    normalized_symbol = strict_trader_module.shared.normalize_symbol(symbol)
    expected_type = str(identity.get("swing_type") or "").upper()
    expected_time = _parse_timestamp(identity.get("swing_timestamp"))
    try:
        expected_price = float(identity.get("swing_price"))
    except (TypeError, ValueError):
        expected_price = None

    details = {
        "fresh_setup_swing_match_method": "authoritative_event_then_smc_then_legacy_raw",
        "fresh_setup_swing_matched": False,
        "fresh_setup_expected_swing": {
            "type": expected_type or None,
            "time": expected_time.isoformat() if expected_time else None,
            "price": expected_price,
        },
    }

    if (
        closed_15m is None
        or len(closed_15m) < 5
        or expected_type not in {"HIGH", "LOW"}
        or expected_time is None
        or expected_price is None
    ):
        details["fresh_setup_swing_validation_error"] = (
            "fresh closed candles or setup identity unavailable"
        )
        return {
            "ok": False,
            "reason": SWING_CHANGED_REASON,
            "details": details,
        }

    tolerance = strict_trader_module.point_size(normalized_symbol) + 1e-12

    authoritative_event, authoritative_details = _authoritative_event_swing_match(
        closed_15m,
        normalized_symbol,
        identity,
        expected_type,
        expected_time,
        expected_price,
        tolerance,
        strict_trader_module,
    )
    details.update(authoritative_details)
    if authoritative_event is not None:
        details.update({
            "fresh_setup_swing_match_method": "authoritative_indicator_event_identity",
            "fresh_setup_swing_matched": True,
            "fresh_setup_matched_swing": {
                "type": expected_type,
                "time": expected_time.isoformat(),
                "price": expected_price,
                "indicator_event_id": authoritative_event.get("event_id"),
            },
        })
        return {"ok": True, "reason": None, "details": details}

    indicator_swings = detect_confirmed_swings(
        closed_15m.copy(),
        left_bars=2,
        right_bars=2,
    )
    details["fresh_indicator_swing_count"] = len(indicator_swings)
    for swing in indicator_swings:
        swing_time = _parse_timestamp(swing.timestamp)
        if (
            str(swing.swing_type).upper() == expected_type
            and swing_time == expected_time
            and abs(float(swing.price) - expected_price) <= tolerance
        ):
            details.update({
                "fresh_setup_swing_match_method": "smc_indicator_confirmed_pivot_identity",
                "fresh_setup_swing_matched": True,
                "fresh_setup_matched_swing": {
                    "type": swing.swing_type,
                    "time": swing.timestamp,
                    "price": float(swing.price),
                    "confirmed_time": swing.confirmed_timestamp,
                },
            })
            return {"ok": True, "reason": None, "details": details}

    raw_swings = strict_trader_module.detect_raw_swings(
        closed_15m.copy(),
        normalized_symbol,
    )
    matching_swing = None

    for swing in raw_swings:
        if str(swing.get("type") or "").upper() != expected_type:
            continue
        swing_time = _parse_timestamp(swing.get("time"))
        try:
            swing_price = float(swing.get("price"))
        except (TypeError, ValueError):
            continue
        if (
            swing_time == expected_time
            and abs(swing_price - expected_price) <= tolerance
        ):
            matching_swing = swing
            break

    details["fresh_raw_swing_count"] = len(raw_swings)
    details["fresh_setup_swing_matched"] = matching_swing is not None
    if matching_swing is not None:
        details.update({
            "fresh_setup_swing_match_method": "legacy_raw_pivot_identity",
            "fresh_setup_matched_swing": {
                "type": matching_swing.get("type"),
                "time": matching_swing.get("time"),
                "price": matching_swing.get("price"),
                "fresh_window_valid_flag": matching_swing.get("valid"),
                "fresh_window_valid_reason": matching_swing.get("valid_reason"),
            },
        })

    return {
        "ok": matching_swing is not None,
        "reason": None if matching_swing is not None else SWING_CHANGED_REASON,
        "details": details,
    }
