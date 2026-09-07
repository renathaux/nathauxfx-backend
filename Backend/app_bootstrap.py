from datetime import datetime, timezone
from email.mime.text import MIMEText
import os
import re
import smtplib
import threading

from fastapi import HTTPException, Request

import api
from strategies import strict_trader
from services.customer_forex_guard import (
    persist_owner_session,
    revoke_owner_session,
    validate_owner_session,
)
from services.fundamental_execution_guard import validate_fundamental_entry
from services.setup_swing_execution_guard import validate_fresh_setup_swing_identity
from services.smc_strategy_authority import (
    AUTHORITY_SOURCE as SMC_AUTHORITY_SOURCE,
    build_chart_structure,
    evaluate_indicator_breakout,
    mark_indicator_breakout_watch,
)


_ORIGINAL_GET_SIGNAL_ALERT_EMAIL_TO = api.get_signal_alert_email_to
_ORIGINAL_PROTECT_LIVE_TRADE_AFTER_TP1 = api.protect_live_trade_after_tp1
_ORIGINAL_VALIDATE_FRESH_EMA_PERMISSION_LOCKED = (
    api.validate_fresh_ema_permission_locked
)
_ORIGINAL_SAVE_REMEMBERED_BREAKOUT = strict_trader.save_remembered_breakout


@api.app.middleware("http")
async def persist_owner_session_immediately_after_login(request, call_next):
    """Persist a new admin token before it can be lost to a Render restart.

    The legacy /login endpoint owns credential verification and writes the new
    token into api.SESSIONS. We only observe successful owner logins here and
    persist the token hash; customer/access-code sessions are never persisted
    as owner sessions.
    """
    is_login = (
        str(request.method or "").upper() == "POST"
        and str(request.url.path or "") == "/login"
    )
    before_tokens = set(api.SESSIONS) if is_login else set()
    response = await call_next(request)
    if is_login and response.status_code < 400:
        for token, session in list(api.SESSIONS.items()):
            if token in before_tokens or not isinstance(session, dict):
                continue
            if str(session.get("role") or "").lower() != "admin":
                continue
            persist_owner_session(token)
    return response


def _owner_bearer_token(request: Request) -> str:
    raw = str(request.headers.get("authorization") or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw.split(" ", 1)[1].strip()


@api.app.get("/owner/session")
def owner_session_status(request: Request):
    token = _owner_bearer_token(request)
    authenticated = validate_owner_session(token, api.SESSIONS)
    return {
        "ok": True,
        "authenticated": bool(authenticated),
        "role": "admin" if authenticated else None,
    }


@api.app.post("/owner/logout")
def owner_logout(request: Request):
    token = _owner_bearer_token(request)
    revoked = revoke_owner_session(token, api.SESSIONS)
    return {
        "ok": bool(revoked),
        "authenticated": False,
    }


def _split_recipients(value):
    recipients = []
    seen = set()
    for item in re.split(r"[,;]", str(value or "")):
        address = item.strip()
        if not address:
            continue
        key = address.casefold()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(address)
    return recipients


def _signal_alert_recipients():
    recipients = []
    seen = set()
    for source in (
        _ORIGINAL_GET_SIGNAL_ALERT_EMAIL_TO(),
        os.getenv("SIGNAL_ALERT_EMAIL_CC", ""),
    ):
        for address in _split_recipients(source):
            key = address.casefold()
            if key in seen:
                continue
            seen.add(key)
            recipients.append(address)
    return recipients


def get_signal_alert_email_to_multi():
    return ", ".join(_signal_alert_recipients())


def _send_tp1_protection_email(trade):
    recipients = _signal_alert_recipients()
    if not recipients:
        return False

    symbol = api.normalize_symbol(trade.get("symbol"))
    side = str(trade.get("side") or trade.get("action") or "").upper()
    protected_sl = trade.get("protected_sl_price") or trade.get("sl")
    generated_at = datetime.now(timezone.utc).isoformat()

    subject = (
        f"FlowSignal TP1 Hit: {symbol} {side} - "
        f"Secure SL {protected_sl}"
    )
    body = f"""
FlowSignal TP1 Protection Alert

Symbol: {symbol}
Direction: {side}

TP1 has been hit.
Move your stop loss now to: {protected_sl}

Entry: {trade.get("entry")}
TP1: {trade.get("tp1")}
TP2: {trade.get("tp2")}
Original SL: {trade.get("original_sl")}
Secured SL: {protected_sl}
Broker protection: CONFIRMED
Time generated: {generated_at}
""".strip()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = api.FEEDBACK_EMAIL
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(api.FEEDBACK_EMAIL, api.FEEDBACK_APP_PASSWORD)
        server.send_message(msg, to_addrs=recipients)

    print("TP1_PROTECTION_EMAIL_SENT =", {
        "symbol": symbol,
        "side": side,
        "protected_sl": protected_sl,
        "to": recipients,
        "subject": subject,
    })
    return True


def protect_live_trade_after_tp1_with_email(trade):
    was_confirmed = (
        api.live_sl_protection_confirmed(trade)
        if isinstance(trade, dict)
        else False
    )

    result = _ORIGINAL_PROTECT_LIVE_TRADE_AFTER_TP1(trade)
    if not isinstance(result, dict):
        return result

    now_confirmed = api.live_sl_protection_confirmed(result)

    # Mark a notification as pending only on the transition from unprotected to
    # broker-verified protection. This avoids stale TP1 emails after a deploy.
    if now_confirmed and not was_confirmed:
        result["tp1_protection_email_pending"] = True
        result["tp1_protection_confirmed_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        api.persist_live_trade_state(result)

    # Persist the pending flag before SMTP. If delivery fails, the next broker
    # sync retries the email without moving the stop loss again.
    if (
        now_confirmed
        and result.get("tp1_protection_email_pending")
        and not result.get("tp1_protection_email_sent")
    ):
        try:
            if _send_tp1_protection_email(result):
                result["tp1_protection_email_sent"] = True
                result["tp1_protection_email_sent_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                result["tp1_protection_email_pending"] = False
                api.persist_live_trade_state(result)
        except Exception as exc:
            print("TP1_PROTECTION_EMAIL_FAILED =", {
                "symbol": api.normalize_symbol(result.get("symbol")),
                "position_id": (
                    result.get("position_id")
                    or result.get("broker_position_id")
                ),
                "error": str(exc),
            })

    return result


def _apply_fundamental_execution_gate(result, symbol, side):
    """Apply macro direction only after every existing technical gate passes."""
    if not isinstance(result, dict) or not result.get("ok"):
        return result

    details = dict(result.get("details") or {})
    try:
        fundamental_gate = validate_fundamental_entry(symbol, side)
    except Exception as exc:
        # A guard implementation error must be visible but must not unexpectedly
        # become a global trading kill switch.
        details.update({
            "fundamental_execution_connected": True,
            "fundamental_gate_state": "BYPASS_GUARD_ERROR",
            "fundamental_error": str(exc),
        })
        print("LIVE_FUNDAMENTAL_FINAL_GATE =", {
            "symbol": api.normalize_symbol(symbol),
            "side": str(side or "").upper(),
            "ok": True,
            "reason": None,
            "gate_state": "BYPASS_GUARD_ERROR",
            "error": str(exc),
        })
        return {
            "ok": True,
            "reason": None,
            "details": details,
        }

    if not isinstance(fundamental_gate, dict):
        details["fundamental_gate_state"] = "BYPASS_INVALID_GUARD_RESPONSE"
        return {
            "ok": True,
            "reason": None,
            "details": details,
        }

    details.update(fundamental_gate.get("details") or {})
    print("LIVE_FUNDAMENTAL_FINAL_GATE =", {
        "symbol": api.normalize_symbol(symbol),
        "side": str(side or "").upper(),
        "ok": bool(fundamental_gate.get("ok")),
        "reason": fundamental_gate.get("reason"),
        "gate_state": details.get("fundamental_gate_state"),
        "direction": details.get("fundamental_direction"),
        "status": details.get("fundamental_status"),
        "score": details.get("fundamental_score"),
        "confidence": details.get("fundamental_confidence"),
    })
    return {
        "ok": bool(fundamental_gate.get("ok")),
        "reason": fundamental_gate.get("reason"),
        "details": details,
    }


def validate_fresh_ema_permission_locked_with_stable_swing_identity(
    symbol,
    side,
    setup_identity=None,
):
    """Recover the stable swing identity and then enforce fundamentals.

    The original final gate still owns EMA, consolidation, and all of its normal
    failure modes. If and only if it reaches the historical swing mismatch,
    verify that the exact strategy-approved pivot still exists in the same
    250-candle SMC authority window used to create the setup. Fundamentals are
    evaluated only after those technical checks pass.
    """
    result = _ORIGINAL_VALIDATE_FRESH_EMA_PERMISSION_LOCKED(
        symbol,
        side,
        setup_identity=setup_identity,
    )
    if not isinstance(result, dict):
        return result
    if result.get("ok"):
        return _apply_fundamental_execution_gate(result, symbol, side)
    if result.get("reason") != "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION":
        return result

    details = dict(result.get("details") or {})
    try:
        latest_15m = api.get_ctrader_market_data(
            api.normalize_symbol(symbol),
            "15m",
            limit=250,
            force_refresh=False,
        )
        closed_15m = strict_trader.closed_frame(latest_15m, 15)
        if closed_15m is not None:
            closed_15m = closed_15m.tail(250).copy()
        swing_check = validate_fresh_setup_swing_identity(
            closed_15m,
            symbol,
            setup_identity,
            strict_trader,
        )
        details.update(swing_check.get("details") or {})

        if swing_check.get("ok"):
            details["legacy_short_window_valid_swing_requalification"] = (
                "false_negative_recovered"
            )
            print("LIVE_SETUP_SWING_IDENTITY_RECOVERED =", {
                "symbol": api.normalize_symbol(symbol),
                "side": str(side or "").upper(),
                "setup_identity": setup_identity,
                "details": swing_check.get("details"),
            })
            return _apply_fundamental_execution_gate(
                {
                    "ok": True,
                    "reason": None,
                    "details": details,
                },
                symbol,
                side,
            )
    except Exception as exc:
        details["stable_setup_swing_recheck_error"] = str(exc)

    return {
        "ok": False,
        "reason": "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION",
        "details": details,
    }


def evaluate_15m_breakout_with_smc_indicator(
    data_15m,
    symbol,
    execution_settings=None,
):
    """Make the backend indicator the single BOS/CHoCH decision source."""
    return evaluate_indicator_breakout(
        data_15m,
        symbol,
        execution_settings=execution_settings,
        strict_trader_module=strict_trader,
    )


def save_remembered_breakout_with_smc_marker(*args, **kwargs):
    return mark_indicator_breakout_watch(
        strict_trader,
        _ORIGINAL_SAVE_REMEMBERED_BREAKOUT,
        *args,
        **kwargs,
    )


@api.app.get("/chart/smc-structure")
def chart_smc_structure(symbol: str = "EURUSD", timeframe: str = "15m", limit: int = 250):
    """Return the same SMC structure that the 15m strategy consumes."""
    normalized_symbol = api.normalize_symbol(symbol)
    normalized_timeframe = str(timeframe or "15m").strip().lower()
    timeframe_minutes = {
        "5m": 5,
        "5min": 5,
        "m5": 5,
        "15m": 15,
        "15min": 15,
        "m15": 15,
        "1h": 60,
        "h1": 60,
    }
    if normalized_symbol not in {"EURUSD", "XAUUSD"}:
        raise HTTPException(status_code=400, detail="Unsupported SMC symbol")
    if normalized_timeframe not in timeframe_minutes:
        raise HTTPException(status_code=400, detail="Unsupported SMC timeframe")
    canonical_timeframe = (
        "5m" if timeframe_minutes[normalized_timeframe] == 5
        else "15m" if timeframe_minutes[normalized_timeframe] == 15
        else "1h"
    )
    requested_limit = max(50, min(int(limit or 250), 500))
    market_data = api.get_ctrader_market_data(
        normalized_symbol,
        canonical_timeframe,
        limit=requested_limit,
        force_refresh=False,
    )
    closed = strict_trader.closed_frame(
        market_data,
        timeframe_minutes[normalized_timeframe],
    )
    if closed is None or closed.empty:
        raise HTTPException(status_code=503, detail="Closed SMC candles unavailable")
    structure = build_chart_structure(
        closed,
        normalized_symbol,
        canonical_timeframe,
        strict_trader_module=strict_trader,
    )
    structure["display_enabled_independent"] = True
    structure["backend_uses_indicator_when_display_off"] = True
    return structure


def _start_forex_background_task():
    print("Startup OK - warming panel cache")
    api.warm_panel_cache_from_persisted_candles()
    try:
        api.start_ctrader_live_price_stream()
    except Exception as exc:
        print("CTRADER_LIVE_STREAM_START_ERROR =", str(exc))
    with api.BACKGROUND_THREAD_LOCK:
        if api.BACKGROUND_THREAD is not None and api.BACKGROUND_THREAD.is_alive():
            print("BACKGROUND_FETCH_ALREADY_RUNNING =", {
                "thread_id": api.BACKGROUND_THREAD.ident,
            })
            return
        api.BACKGROUND_THREAD = threading.Thread(
            target=api.background_fetch,
            name="flowsignal-trading-engine",
            daemon=True,
        )
        api.BACKGROUND_THREAD.start()
        api.ENGINE_RUNTIME_STATE["loop_thread_id"] = api.BACKGROUND_THREAD.ident


# Replace the legacy startup hook with the Forex-only runtime while preserving
# the same panel warmup, cTrader stream, and 24/7 strategy background thread.
api.app.router.on_startup = [
    handler for handler in api.app.router.on_startup
    if handler is not api.start_background_task
]
api.app.router.on_startup.append(_start_forex_background_task)

# The chart toggle is presentation only. The server-side SMC indicator remains
# active because strict_trader evaluates it independently on every 15m cycle.
strict_trader.evaluate_15m_breakout = evaluate_15m_breakout_with_smc_indicator
strict_trader.save_remembered_breakout = save_remembered_breakout_with_smc_marker

# Keep existing alert behavior and the final-entry swing correction. The final
# strategy-generated live entry must now also pass the Fundamental Insight
# direction filter after all existing technical checks succeed.
api.get_signal_alert_email_to = get_signal_alert_email_to_multi
api.protect_live_trade_after_tp1 = protect_live_trade_after_tp1_with_email
api.validate_fresh_ema_permission_locked = (
    validate_fresh_ema_permission_locked_with_stable_swing_identity
)

print("SMC_STRATEGY_AUTHORITY =", {
    "source": SMC_AUTHORITY_SOURCE,
    "bos_choch_authority": True,
    "visual_toggle_controls_strategy": False,
    "fundamental_execution_filter": True,
})

app = api.app
