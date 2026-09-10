from datetime import datetime, timezone

from indicators.smc import detect_confirmed_swings
from services.indicator_event_lookup import get_indicator_event


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


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_indicator_event_match(
    event,
    normalized_symbol,
    identity,
    expected_type,
    expected_time,
    expected_price,
    tolerance,
):
    payload = event.get("payload") if isinstance(event, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    event_direction = str(
        (event or {}).get("direction") or payload.get("direction") or ""
    ).upper()
    event_side = (
        "BUY" if event_direction == "BULLISH"
        else "SELL" if event_direction == "BEARISH"
        else None
    )
    canonical_swing_type = (
        "HIGH" if event_side == "BUY"
        else "LOW" if event_side == "SELL"
        else None
    )
    canonical_swing_time = _parse_timestamp(
        payload.get("broken_swing_timestamp")
    )
    canonical_swing_price = _as_float(
        (event or {}).get("broken_level")
        if (event or {}).get("broken_level") is not None
        else payload.get("broken_level")
    )
    canonical_bos_time = _parse_timestamp(
        (event or {}).get("candle_timestamp") or payload.get("timestamp")
    )
    canonical_bos_level = canonical_swing_price

    expected_side = str(identity.get("direction") or "").upper() or None
    expected_bos_time = _parse_timestamp(identity.get("bos_candle_timestamp"))
    expected_bos_level = _as_float(identity.get("bos_level"))

    checks = {
        "symbol": str((event or {}).get("symbol") or "").upper() == normalized_symbol,
        "timeframe": str((event or {}).get("timeframe") or "").lower() == "15m",
        "tradable": not bool((event or {}).get("is_historical")),
        "direction": expected_side in {None, event_side},
        "swing_type": canonical_swing_type == expected_type,
        "swing_time": canonical_swing_time == expected_time,
        "swing_price": (
            canonical_swing_price is not None
            and abs(canonical_swing_price - expected_price) <= tolerance
        ),
        "bos_time": (
            expected_bos_time is None
            or canonical_bos_time == expected_bos_time
        ),
        "bos_level": (
            expected_bos_level is None
            or (
                canonical_bos_level is not None
                and abs(canonical_bos_level - expected_bos_level) <= tolerance
            )
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "canonical_swing": {
            "type": canonical_swing_type,
            "time": (
                canonical_swing_time.isoformat()
                if canonical_swing_time else None
            ),
            "price": canonical_swing_price,
        },
        "canonical_bos": {
            "time": canonical_bos_time.isoformat() if canonical_bos_time else None,
            "level": canonical_bos_level,
            "direction": event_side,
            "classification": (event or {}).get("classification"),
        },
    }


def validate_fresh_setup_swing_identity(
    closed_15m,
    symbol,
    setup_identity,
    strict_trader_module,
):
    """Revalidate an immutable setup pivot without re-qualifying its old leg.

    Indicator-event setups are first checked against their durable immutable
    event. This keeps an already-confirmed BOS/CHoCH valid even when its source
    pivot has naturally fallen outside the shorter execution-time candle
    window. Legacy setups without an indicator event id still use the current
    confirmed/raw pivot checks below.
    """
    identity = setup_identity if isinstance(setup_identity, dict) else {}
    normalized_symbol = strict_trader_module.shared.normalize_symbol(symbol)
    expected_type = str(identity.get("swing_type") or "").upper()
    expected_time = _parse_timestamp(identity.get("swing_timestamp"))
    expected_price = _as_float(identity.get("swing_price"))
    event_id = identity.get("indicator_event_id")

    details = {
        "fresh_setup_swing_match_method": "authoritative_event_then_smc_then_legacy_raw",
        "fresh_setup_swing_matched": False,
        "fresh_setup_expected_swing": {
            "type": expected_type or None,
            "time": expected_time.isoformat() if expected_time else None,
            "price": expected_price,
        },
        "source_indicator_event_id": event_id,
    }

    if (
        expected_type not in {"HIGH", "LOW"}
        or expected_time is None
        or expected_price is None
    ):
        details["fresh_setup_swing_validation_error"] = (
            "setup identity unavailable"
        )
        return {
            "ok": False,
            "reason": SWING_CHANGED_REASON,
            "details": details,
        }

    tolerance = strict_trader_module.point_size(normalized_symbol) + 1e-12

    if event_id:
        event = get_indicator_event(event_id)
        details["authoritative_indicator_event_found"] = event is not None
        if event is None:
            details["fresh_setup_swing_validation_error"] = (
                "immutable source indicator event unavailable"
            )
            return {
                "ok": False,
                "reason": SWING_CHANGED_REASON,
                "details": details,
            }

        canonical = _canonical_indicator_event_match(
            event,
            normalized_symbol,
            identity,
            expected_type,
            expected_time,
            expected_price,
            tolerance,
        )
        details["authoritative_indicator_event_checks"] = canonical["checks"]
        details["authoritative_indicator_event_swing"] = canonical[
            "canonical_swing"
        ]
        details["authoritative_indicator_event_bos"] = canonical[
            "canonical_bos"
        ]
        if canonical["ok"]:
            details.update({
                "fresh_setup_swing_match_method": "authoritative_indicator_event_identity",
                "fresh_setup_swing_matched": True,
                "fresh_setup_matched_swing": canonical["canonical_swing"],
            })
            return {"ok": True, "reason": None, "details": details}

        details["fresh_setup_swing_validation_error"] = (
            "immutable source indicator event does not match setup identity"
        )
        return {
            "ok": False,
            "reason": SWING_CHANGED_REASON,
            "details": details,
        }

    if closed_15m is None or len(closed_15m) < 5:
        details["fresh_setup_swing_validation_error"] = (
            "fresh closed candles unavailable for legacy setup"
        )
        return {
            "ok": False,
            "reason": SWING_CHANGED_REASON,
            "details": details,
        }

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
