from datetime import datetime, timezone

from indicators.smc import detect_confirmed_swings
from services.indicator_event_stream_service import read_authoritative_event


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


def _durable_event_matches_setup(event, symbol, expected_type, expected_time, expected_price, tolerance):
    """Check that the immutable event is the one that created this setup."""
    if not isinstance(event, dict):
        return False
    normalized_symbol = str(symbol or "").upper().replace("/", "")
    direction = str(event.get("direction") or "").upper()
    expected_direction = "BULLISH" if expected_type == "HIGH" else "BEARISH"
    event_time = _parse_timestamp(event.get("broken_swing_timestamp"))
    try:
        event_price = float(event.get("broken_level"))
    except (TypeError, ValueError):
        return False
    return (
        str(event.get("symbol") or "").upper().replace("/", "") == normalized_symbol
        and str(event.get("timeframe") or "").lower() == "15m"
        and event.get("tradable") is True
        and direction == expected_direction
        and event_time == expected_time
        and abs(event_price - expected_price) <= tolerance
    )
def validate_fresh_setup_swing_identity(
    closed_15m,
    symbol,
    setup_identity,
    strict_trader_module,
):
    """Revalidate the immutable setup pivot without re-qualifying its old leg.

    New setups are created by the authoritative backend SMC indicator, so the
    same confirmed-pivot detector is checked first.  The legacy raw-pivot check
    remains as a compatibility fallback for setups created before the authority
    switch.  EMA/consolidation and the setup fingerprint are validated by the
    existing execution gates separately.
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
        "fresh_setup_swing_match_method": "smc_indicator_then_legacy_raw",
        "fresh_setup_swing_matched": False,
        "fresh_setup_expected_swing": {
            "type": expected_type or None,
            "time": expected_time.isoformat() if expected_time else None,
            "price": expected_price,
        },
    }

    event_id = identity.get("indicator_event_id")
    if event_id:
        # The setup was derived from a durable event.  Its immutable broken
        # swing is the authoritative identity; a rolling market-data request
        # must not invalidate it merely because the old pivot fell out of view.
        if expected_type not in {"HIGH", "LOW"} or expected_time is None or expected_price is None:
            details["fresh_setup_swing_validation_error"] = "durable setup identity unavailable"
            return {"ok": False, "reason": SWING_CHANGED_REASON, "details": details}
        tolerance = strict_trader_module.point_size(normalized_symbol) + 1e-12
        try:
            durable_event = read_authoritative_event(event_id)
        except Exception as exc:
            details["fresh_setup_swing_validation_error"] = (
                f"durable indicator event unavailable: {exc}"
            )
            return {"ok": False, "reason": SWING_CHANGED_REASON, "details": details}
        if _durable_event_matches_setup(
            durable_event,
            normalized_symbol,
            expected_type,
            expected_time,
            expected_price,
            tolerance,
        ):
            details.update({
                "fresh_setup_swing_match_method": "durable_indicator_event_identity",
                "fresh_setup_swing_matched": True,
                "fresh_setup_matched_swing": {
                    "type": expected_type,
                    "time": expected_time.isoformat(),
                    "price": expected_price,
                    "indicator_event_id": event_id,
                },
            })
            return {"ok": True, "reason": None, "details": details}
        details["fresh_setup_swing_validation_error"] = "durable indicator event does not match setup"
        return {"ok": False, "reason": SWING_CHANGED_REASON, "details": details}

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
