from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from email.mime.text import MIMEText
import smtplib
import time
import threading
import json
import copy
import os
import hashlib
import hmac
import uuid
import math
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ctrader_connector import (
    CTRADER_PAYLOAD_VOLUME_SCALE,
    build_ctrader_authorization_url,
    clear_ctrader_saved_accounts,
    close_position,
    connect_account,
    convert_ctrader_volume_to_lots,
    convert_lots_to_ctrader_volume,
    disconnect_account,
    exchange_ctrader_authorization_code,
    fetch_ctrader_accounts,
    forget_ctrader_account,
    get_connection_state,
    get_ctrader_connection_snapshot,
    get_ctrader_account_snapshot,
    get_closed_deals_for_current_week,
    get_closed_deals_for_current_month,
    get_ctrader_diagnostics,
    get_ctrader_account_selection_debug,
    get_ctrader_redirect_uri_debug,
    get_ctrader_refresh_token_status,
    get_ctrader_market_data,
    get_live_prices,
    get_ctrader_position_fetch_error,
    get_ctrader_symbol_risk_metadata,
    get_open_positions,
    modify_position_sltp,
    modify_position_stop_loss,
    normalize_symbol,
    normalize_trade_levels,
    parse_ctrader_money,
    place_market_order,
    remove_debug_open_position,
    set_active_ctrader_account,
    set_debug_open_positions,
    start_ctrader_live_price_stream
)
from routes.ctrader import router as ctrader_router
from routes.performance import (
    configure_performance_data_provider,
    router as performance_router,
)
from routes.settings import router as settings_router
from routes.trading import router as trading_router
from routes.diagnostics import router as diagnostics_router
from routes.shadow import router as shadow_router
from services.news_service import (
    fetch_calendar_events,
    get_calendar_data_age_seconds,
    get_news_impact,
    refresh_actual_after_release,
)
from services import news_trading
from services.news_mode_service import (
    InvalidNewsTradingMode,
    get_effective_mode as get_effective_news_mode,
    normalize_mode as normalize_news_mode,
    record_audit as record_news_mode_audit,
    save_mode as save_news_mode,
)
from services.strategy_settings_service import (
    StrategySettingsValidationError,
    get_cached_execution_settings,
    get_configured_cooldown_seconds,
    get_configured_rr_window,
    get_strategy_settings,
    get_strategy_settings_history,
    reset_strategy_settings,
    save_strategy_settings,
)
from services.settings_service import (
    get_tp1_ratio_of_tp2,
    load_feature_flags,
    load_risk_settings,
)
from services.auto_trade_state_service import (
    latest_changes as get_auto_trade_state_changes,
    load_state as load_durable_auto_trade_state,
    save_mode as save_durable_auto_trade_mode,
    save_state as save_durable_auto_trade_state,
)
from services.strategy_diagnostics_service import (
    record_execution_gate_safely,
    update_execution_outcome_safely,
)
from services.forex_observability_service import (
    persist_execution_snapshot_safely,
    record_execution_response_safely,
)
from services.v2_shadow_service import link_v1_execution_safely
from services.execution_risk_service import (
    persist_execution_risk_audit_safely,
    validate_pre_submit as validate_executable_risk,
    validate_sl_amendment as validate_application_sl_amendment,
)
from services.indicator_event_stream_service import (
    get_event_lifecycles,
    update_event_lifecycle,
)
from services.trade_submission_service import (
    claim_submission,
    complete_submission,
    mark_request_started,
    recover_unsent_claim,
    require_reconciliation,
)
from db import database_status as get_database_status, engine as database_engine
from paths import DATA_DIR
from risk_management.account_balance import (
    log_account_balance_verification_failed as risk_log_account_balance_verification_failed,
    validate_verified_account_snapshot,
)
from risk_management.broker_protection import (
    build_live_protection_audit as risk_build_live_protection_audit,
    classify_stop_loss_change as risk_classify_stop_loss_change,
    live_prices_match as risk_live_prices_match,
)
from risk_management.position_sizing import (
    calculate_expected_loss_usd_from_risk_size as risk_calculate_expected_loss_usd_from_risk_size,
    calculate_position_size as risk_calculate_position_size,
    round_volume_down_to_step as risk_round_volume_down_to_step,
)
from risk_management.risk_audit import (
    build_live_risk_calculation_audit as risk_build_live_risk_calculation_audit,
    first_valid_live_price as risk_first_valid_live_price,
    log_live_risk_calculation_audit as risk_log_live_risk_calculation_audit,
)
from risk_management.risk_limits import is_expected_loss_oversized

FINAL_SIGNAL_HOLD_FILE = os.path.join(
    DATA_DIR,
    "final_signal_hold.json"
)

def clear_persisted_final_signal_hold(symbol, reason="trade executed"):
    try:
        if not os.path.exists(FINAL_SIGNAL_HOLD_FILE):
            return

        with open(FINAL_SIGNAL_HOLD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        hold_key = normalize_symbol(symbol)

        if hold_key in data:
            data.pop(hold_key, None)

            with open(FINAL_SIGNAL_HOLD_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)

            try:
                import brain

                brain.FINAL_SIGNAL_HOLD.pop(hold_key, None)
            except Exception as brain_exc:
                print("FINAL_SIGNAL_HOLD_MEMORY_RELEASE_WARNING =", {
                    "symbol": hold_key,
                    "error": str(brain_exc),
                })

            print("FINAL_SIGNAL_HOLD_CONSUMED =", {
                "symbol": hold_key,
                "reason": reason,
            })
    except Exception as exc:
        print("FINAL_SIGNAL_HOLD_RELEASE_ERROR =", {
            "symbol": symbol,
            "error": str(exc),
        })

app = FastAPI()
app.include_router(settings_router)
app.include_router(performance_router)
app.include_router(ctrader_router)
app.include_router(trading_router)
app.include_router(diagnostics_router)
app.include_router(shadow_router)

@app.middleware("http")
async def log_unhandled_api_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        print("UNHANDLED_API_ERROR =", {
            "method": request.method,
            "path": request.url.path,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        traceback.print_exc()
        raise

@app.on_event("startup")
def start_background_task():
    global BACKGROUND_THREAD
    print("Startup OK - warming panel cache")
    try:
        from services.deriv_binary_settlement_recovery import start_settlement_recovery_worker
        start_settlement_recovery_worker()
    except Exception as exc:
        print("DERIV_SETTLEMENT_RECOVERY_START_ERROR =", type(exc).__name__)
    warm_panel_cache_from_persisted_candles()
    try:
        start_ctrader_live_price_stream()
    except Exception as exc:
        print("CTRADER_LIVE_STREAM_START_ERROR =", str(exc))
    with BACKGROUND_THREAD_LOCK:
        if BACKGROUND_THREAD is not None and BACKGROUND_THREAD.is_alive():
            print("BACKGROUND_FETCH_ALREADY_RUNNING =", {
                "thread_id": BACKGROUND_THREAD.ident,
            })
            return
        BACKGROUND_THREAD = threading.Thread(
            target=background_fetch,
            name="flowsignal-trading-engine",
            daemon=True,
        )
        BACKGROUND_THREAD.start()
        ENGINE_RUNTIME_STATE["loop_thread_id"] = BACKGROUND_THREAD.ident
    
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TradeRequest(BaseModel):
    symbol: str
    action: str
    token: str

class FeedbackRequest(BaseModel):
    message: str
    user: str | None = None


class NewsTradingModeRequest(BaseModel):
    mode: str
    time: str | None = None


class StrategySettingsUpdateRequest(BaseModel):
    settings: dict


class StrategySettingsResetRequest(BaseModel):
    confirm: bool = False


class AccessCodeSessionRequest(BaseModel):
    code: str

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class DebugBrokerPositionsRequest(BaseModel):
    positions: list

# =========================
# DEFAULT PANEL DATA
# =========================
def default_panel():
    return {
        "EURUSD": {
            "signal": "WAIT",
            "signal_text": "WAIT ⚪ (startup)",
            "buy_pct": 0,
            "sell_pct": 0,
            "confidence": 0,
            "market_condition": "UNKNOWN",
            "entry_quality": "WEAK"
        },
        "XAUUSD": {
            "signal": "WAIT",
            "signal_text": "WAIT ⚪ (startup)",
            "buy_pct": 0,
            "sell_pct": 0,
            "confidence": 0,
            "market_condition": "UNKNOWN",
            "entry_quality": "WEAK"
        }
    }


# =========================
# CACHE
# =========================
PANEL_CACHE = {
    "data": default_panel(),
    "last_update": 0
}
PANEL_REFRESH_LOCK = threading.Lock()
PANEL_REFRESH_COMPLETE = threading.Event()
PANEL_REFRESH_COMPLETE.set()
PANEL_REFRESH_STATE = {
    "running": False,
    "last_started": None,
    "last_success": None,
    "last_error": None,
    "last_duration_seconds": None,
    "reason": None,
    "last_source": None,
}
LIVE_PANEL_META_CACHE = {
    "live_positions": [],
    "live_price_status": {
        "live_prices": {},
        "live_price_health": "starting",
        "live_price_last_update": None,
    },
        "live_pl_sync": {
        "daily_realized_pl": 0,
        "daily_total_pl": 0,
        "weekly_realized_pl": 0,
        "monthly_realized_pl": 0,
        "floating_live_pl": 0,
        "weekly_total_pl": 0,
        "open_positions_count": 0,
    },
    "live_recent_history": [],
    "live_trade_stats": {},
    "last_execution_time": 0,
    "last_update": None,
    "last_error": None,
}
ENGINE_RUNTIME_STATE = {
    "process_id": os.getpid(),
    "started_at": time.time(),
    "loop_thread_id": None,
    "loop_iterations": 0,
    "last_loop_started": None,
    "last_loop_completed": None,
    "last_loop_error": None,
    "last_strategy_check": None,
    "last_valid_setup": None,
    "last_order_attempt": None,
    "last_broker_response": None,
}
BACKGROUND_THREAD_LOCK = threading.Lock()
BACKGROUND_THREAD = None

LIVE_MONTHLY_HISTORY_CACHE = {
    "history": [],
    "updated_at": 0,
    "month_key": None,
}
LIVE_MONTHLY_HISTORY_FILE = os.path.join(
    DATA_DIR,
    "live_monthly_history.json",
)


def load_live_monthly_history_cache():
    try:
        if not os.path.exists(LIVE_MONTHLY_HISTORY_FILE):
            return

        with open(LIVE_MONTHLY_HISTORY_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        current_month = datetime.now(LIVE_MARKET_TIMEZONE).strftime("%Y-%m")
        if saved.get("month_key") != current_month:
            return

        history = saved.get("history")
        if not isinstance(history, list):
            return

        LIVE_MONTHLY_HISTORY_CACHE.update({
            "history": history,
            "updated_at": float(saved.get("updated_at") or 0),
            "month_key": current_month,
        })
        print("LIVE_MONTHLY_HISTORY_CACHE_LOADED =", {
            "month_key": current_month,
            "closed_trades": len(history),
        })
    except Exception as exc:
        print("LIVE_MONTHLY_HISTORY_CACHE_LOAD_ERROR:", exc)


def save_live_monthly_history_cache():
    try:
        with open(LIVE_MONTHLY_HISTORY_FILE, "w", encoding="utf-8") as file:
            json.dump(LIVE_MONTHLY_HISTORY_CACHE, file, indent=2)
    except Exception as exc:
        print("LIVE_MONTHLY_HISTORY_CACHE_SAVE_ERROR:", exc)

CACHE_SECONDS = 15
PANEL_REFRESH_STUCK_SECONDS = 45
PANEL_INITIAL_DATA_WAIT_SECONDS = 55
ADMIN_TOKEN = "N2415"
FEEDBACK_EMAIL = "flowsignal.contact@gmail.com"
FEEDBACK_APP_PASSWORD = os.getenv("FEEDBACK_APP_PASSWORD", "").strip()
SIGNAL_EMAIL_LAST_SIGNAL = {
    "EURUSD": "WAIT",
    "XAUUSD": "WAIT",
}
SIGNAL_EMAIL_LOCK = threading.Lock()
USERS_FILE = os.path.join(DATA_DIR, "users.json")
VISITS_FILE = os.path.join(DATA_DIR, "visits.json")
SESSIONS = {}
NEWS_MODE_CHANGE_LOCK = threading.RLock()

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
def load_visits():
    if not os.path.exists(VISITS_FILE):
        return []

    try:
        with open(VISITS_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def save_visits(visits):
    with open(VISITS_FILE, "w") as f:
        json.dump(visits, f, indent=2)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()
def _is_valid_panel_payload(data):
    return (
        isinstance(data, dict)
        and isinstance(data.get("EURUSD"), dict)
        and isinstance(data.get("XAUUSD"), dict)
    )

def _panel_candle_counts(data):
    candles = data.get("candles") if isinstance(data, dict) else {}
    counts = {}

    for symbol in ["EURUSD", "XAUUSD"]:
        symbol_candles = (
            candles.get(symbol)
            if isinstance(candles, dict)
            else {}
        )
        counts[symbol] = {
            timeframe: len(symbol_candles.get(timeframe) or [])
            if isinstance(symbol_candles, dict)
            else 0
            for timeframe in ["5m", "15m", "1h"]
        }

    return counts

def _panel_cache_validity(data):
    if not _is_valid_panel_payload(data):
        return {
            "valid": False,
            "reason": "panel payload shape invalid",
            "candle_counts": _panel_candle_counts(data),
        }

    candle_counts = _panel_candle_counts(data)
    problems = []

    for symbol in ["EURUSD", "XAUUSD"]:
        plan = data.get(symbol) or {}
        try:
            price = float(plan.get("price"))
            has_price = math.isfinite(price) and price > 0
        except (TypeError, ValueError):
            has_price = False

        market_condition = str(
            plan.get("market_condition") or "UNKNOWN"
        ).upper()
        has_market_condition = market_condition not in [
            "",
            "UNKNOWN",
            "CTRADER_UNAVAILABLE",
        ]
        scores = [
            plan.get("buy_pct", plan.get("buy_percentage", 0)),
            plan.get("sell_pct", plan.get("sell_percentage", 0)),
            plan.get("confidence", 0),
        ]
        signal = str(plan.get("signal") or "").upper()
        wait_reason = plan.get("blocked_reason") or plan.get("blocked_by")
        try:
            has_scores = (
                signal == "WAIT" and bool(wait_reason)
            ) or any(float(value or 0) > 0 for value in scores)
        except (TypeError, ValueError):
            has_scores = signal == "WAIT" and bool(wait_reason)
        has_candles = any(candle_counts[symbol].values())

        if not has_price:
            problems.append(f"{symbol} price missing")
        if not has_market_condition:
            problems.append(f"{symbol} market condition unavailable")
        if not has_scores:
            problems.append(f"{symbol} scores are all zero")
        if not has_candles:
            problems.append(f"{symbol} candles missing")

    return {
        "valid": not problems,
        "reason": "; ".join(problems) if problems else None,
        "candle_counts": candle_counts,
    }


def _json_safe_panel_value(value, _active_containers=None, _depth=0):
    # Starlette deliberately rejects NaN/Infinity while encoding JSON. Market
    # data frames can legitimately contain either value in an unfinished row,
    # so normalise them at the API boundary instead of taking /panel-data down.
    if _active_containers is None:
        _active_containers = set()
    # A malformed diagnostic/state object can also be an acyclic but
    # effectively unbounded chain of nested dictionaries. It is not useful to
    # the dashboard beyond this point and must never make the whole API fail.
    if _depth >= 16:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dict, list, tuple, set)):
        container_id = id(value)
        if container_id in _active_containers:
            return None
        _active_containers.add(container_id)
        try:
            if isinstance(value, dict):
                return {
                    str(key): _json_safe_panel_value(
                        child,
                        _active_containers,
                        _depth + 1,
                    )
                    for key, child in value.items()
                }
            return [
                _json_safe_panel_value(
                    child,
                    _active_containers,
                    _depth + 1,
                )
                for child in value
            ]
        finally:
            _active_containers.remove(container_id)
    if hasattr(value, "item"):
        try:
            # Re-run conversion because numpy scalar extraction can produce a
            # non-finite Python float or another scalar requiring conversion.
            extracted = value.item()
            if extracted is not value:
                return _json_safe_panel_value(
                    extracted,
                    _active_containers,
                    _depth + 1,
                )
        except (TypeError, ValueError):
            pass
    return value

def _is_startup_panel_payload(data):
    if not _is_valid_panel_payload(data):
        return True

    return all(
        str((data.get(symbol) or {}).get("signal_text") or "").endswith(
            "(startup)"
        )
        for symbol in ["EURUSD", "XAUUSD"]
    )


def signal_email_alerts_enabled():
    return str(
        os.getenv("SIGNAL_EMAIL_ALERTS_ENABLED", "false")
    ).strip().lower() in ["1", "true", "yes", "on"]


def get_signal_alert_email_to():
    return (
        os.getenv("SIGNAL_ALERT_EMAIL_TO", "").strip()
        or FEEDBACK_EMAIL
    )


def get_plan_value(plan, *keys, default="--"):
    if not isinstance(plan, dict):
        return default

    for key in keys:
        value = plan.get(key)
        if value not in [None, "", "--"]:
            return value

    return default


def get_signal_news_status(symbol):
    try:
        news = get_news_impact(symbol)
        if not isinstance(news, dict):
            return "--"

        status = (
            news.get("status")
            or news.get("decision")
            or news.get("news_bias")
            or news.get("bias")
            or "--"
        )
        event = (
            news.get("event_name")
            or news.get("next_event")
            or news.get("event")
            or news.get("news_event")
        )

        if event and status and status != "--":
            return f"{event} - {status}"

        return status or "--"
    except Exception as exc:
        print("SIGNAL_EMAIL_NEWS_STATUS_ERROR =", {
            "symbol": normalize_symbol(symbol),
            "error": str(exc),
        })
        return "--"


def build_signal_alert_email_body(symbol, signal, plan, generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    if isinstance(generated_at, datetime):
        generated_at = generated_at.isoformat()

    return f"""
NathauxFX New Signal Alert

Symbol: {normalize_symbol(symbol)}
Signal direction: {signal}
Entry price: {get_plan_value(plan, "entry_price", "entry", "price")}
Stop Loss: {get_plan_value(plan, "stop_loss", "sl")}
TP1: {get_plan_value(plan, "tp1")}
TP2: {get_plan_value(plan, "tp2")}
Risk %: {get_plan_value(plan, "final_risk_percent", "risk_percent", "allowed_risk_percent")}
Risk dollars: {get_plan_value(plan, "risk_dollars", "risk_amount", "risk_usd")}
Bias strength: {get_plan_value(plan, "bias_strength", "confidence", "directional_bias_strength")}
Main reason: {get_plan_value(plan, "blocked_reason", "plan_reason", "reason", "entry_timing")}
Time generated: {generated_at}
News status: {get_signal_news_status(symbol)}
""".strip()


def send_signal_alert_email(symbol, signal, plan):
    normalized_symbol = normalize_symbol(symbol)
    recipient = get_signal_alert_email_to()
    subject = f"NathauxFX Alert: {normalized_symbol} {signal}"
    body = build_signal_alert_email_body(normalized_symbol, signal, plan)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = FEEDBACK_EMAIL
    msg["To"] = recipient

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(FEEDBACK_EMAIL, FEEDBACK_APP_PASSWORD)
        server.send_message(msg)

    print("SIGNAL_EMAIL_SENT =", {
        "symbol": normalized_symbol,
        "signal": signal,
        "to": recipient,
        "subject": subject,
    })


def send_order_confirmation_email(symbol, signal, plan):
    normalized_symbol = normalize_symbol(symbol)
    normalized_signal = str(signal or "").upper()

    try:
        send_signal_alert_email(normalized_symbol, normalized_signal, plan)
        with SIGNAL_EMAIL_LOCK:
            SIGNAL_EMAIL_LAST_SIGNAL[normalized_symbol] = normalized_signal
        return True
    except Exception as exc:
        print("ORDER_CONFIRMATION_EMAIL_FAILED =", {
            "symbol": normalized_symbol,
            "signal": normalized_signal,
            "error": str(exc),
        })
        return False


def get_email_signal_side(signal):
    normalized = str(signal or "WAIT").upper()
    if normalized in ["BUY", "BUY RUNNING"]:
        return "BUY"
    if normalized in ["SELL", "SELL RUNNING"]:
        return "SELL"
    return "WAIT"


def process_signal_email_alerts(panel_data):
    if not signal_email_alerts_enabled():
        return

    if not isinstance(panel_data, dict):
        return

    with SIGNAL_EMAIL_LOCK:
        for symbol in ["EURUSD", "XAUUSD"]:
            plan = panel_data.get(symbol)
            if not isinstance(plan, dict):
                continue

            signal = get_email_signal_side(plan.get("signal") or "WAIT")
            if signal not in ["BUY", "SELL"]:
                SIGNAL_EMAIL_LAST_SIGNAL[symbol] = "WAIT"
                continue

            previous_signal = SIGNAL_EMAIL_LAST_SIGNAL.get(symbol, "WAIT")
            if previous_signal == signal:
                print("SIGNAL_EMAIL_SKIPPED_DUPLICATE =", {
                    "symbol": symbol,
                    "signal": signal,
                })
                continue

            try:
                send_signal_alert_email(symbol, signal, plan)
            except Exception as exc:
                print("SIGNAL_EMAIL_FAILED =", {
                    "symbol": symbol,
                    "signal": signal,
                    "error": str(exc),
                })
            finally:
                SIGNAL_EMAIL_LAST_SIGNAL[symbol] = signal


def update_panel_cache(data, source):
    validity = _panel_cache_validity(data)
    if not validity["valid"]:
        raise ValueError(
            f"Brain returned unusable panel data: {validity['reason']}"
        )

    updated_at = time.time()
    PANEL_CACHE["data"] = data
    PANEL_CACHE["last_update"] = updated_at
    PANEL_REFRESH_STATE["last_success"] = updated_at
    PANEL_REFRESH_STATE["last_source"] = source
    PANEL_REFRESH_STATE["last_error"] = None
    print("PANEL_CACHE_UPDATE_SUCCESS =", {
        "source": source,
        "updated_at": updated_at,
        "eurusd_signal": (data.get("EURUSD") or {}).get("signal"),
        "xauusd_signal": (data.get("XAUUSD") or {}).get("signal"),
        "candle_counts": validity["candle_counts"],
    })
    process_signal_email_alerts(data)
    return updated_at

def calculate_fresh_panel_data(reason, force_refresh=False):
    from brain import get_panel_data

    print("PANEL_REFRESH_START =", {
        "reason": reason,
        "force_refresh": bool(force_refresh),
    })
    return get_panel_data(force_refresh=force_refresh)


def _actionable_panel_plans(panel_data):
    if not isinstance(panel_data, dict):
        return []
    actionable = []
    for symbol in ("EURUSD", "XAUUSD"):
        plan = get_panel_trade_plan(panel_data, symbol) or {}
        signal = str(plan.get("signal") or "WAIT").upper()
        if signal in {"BUY", "SELL"}:
            actionable.append((symbol, signal, plan))
    return actionable


def record_auto_execution_gate(
    symbol,
    plan,
    gate,
    result,
    reason,
    details=None,
):
    """Emit and best-effort persist one READY-to-order gate decision."""
    plan = plan if isinstance(plan, dict) else {}
    signal = str(plan.get("signal") or "WAIT").upper()
    setup_id = (
        plan.get("signal_setup_id")
        or plan.get("setup_id")
        or (
            get_signal_setup_id(plan, signal)
            if signal in {"BUY", "SELL"}
            else None
        )
    )
    event = {
        "symbol": str(symbol or "").upper(),
        "setup_id": setup_id,
        "source": str(plan.get("execution_source") or "V1").upper(),
        "strategy_decision": str(
            plan.get("strategy_decision") or signal or "WAIT"
        ).upper(),
        "execution_allowed": str(result or "").upper() == "PASS",
        "execution_block_reason": (
            reason if str(result or "").upper() == "BLOCK" else None
        ),
        "active_trade_direction": plan.get("active_trade_direction"),
        "active_trade_id": plan.get("active_trade_id"),
        "gate": str(gate or "UNKNOWN").upper(),
        "result": str(result or "UNKNOWN").upper(),
        "reason": reason,
        "details": details or {},
    }
    print("AUTO_EXECUTION_GATE =", event)
    record_execution_gate_safely(
        (plan.get("audit_diagnostics") or {}).get("cycle_id"),
        event["symbol"],
        setup_id,
        event["gate"],
        event["result"],
        reason,
        details={
            **event["details"],
            "source": event["source"],
            "strategy_decision": event["strategy_decision"],
            "execution_allowed": event["execution_allowed"],
            "execution_block_reason": event["execution_block_reason"],
            "active_trade_direction": event["active_trade_direction"],
            "active_trade_id": event["active_trade_id"],
        },
    )
    return event


def inspect_strategy_panel_handoff(plan):
    """Observe required READY fields without adding a new execution rule."""
    plan = plan if isinstance(plan, dict) else {}
    signal = str(plan.get("signal") or "WAIT").upper()
    missing = [
        key
        for key in ("entry_price", "stop_loss", "tp1", "tp2")
        if is_missing_trade_value(plan.get(key))
    ]
    if signal not in {"BUY", "SELL"}:
        return False, "STRATEGY_NOT_ACTIONABLE", {"missing_fields": missing}
    if missing:
        return False, "STRATEGY_PAYLOAD_INVALID", {"missing_fields": missing}
    return True, "VALIDATED_STRATEGY_PAYLOAD", {"missing_fields": []}

def refresh_panel_cache(reason="background", force_refresh=False):
    if not PANEL_REFRESH_LOCK.acquire(blocking=False):
        print("PANEL_REFRESH_SKIPPED =", {
            "reason": reason,
            "active_reason": PANEL_REFRESH_STATE.get("reason"),
        })
        return False

    started_at = time.time()
    PANEL_REFRESH_COMPLETE.clear()
    PANEL_REFRESH_STATE.update({
        "running": True,
        "last_started": started_at,
        "last_error": None,
        "reason": reason,
    })

    try:
        data = calculate_fresh_panel_data(
            reason,
            force_refresh=force_refresh,
        )
        update_panel_cache(data, reason)
        print("PANEL_REFRESH_SUCCESS =", {
            "reason": reason,
            "seconds": round(time.time() - started_at, 2),
        })
        return True
    except Exception as exc:
        PANEL_REFRESH_STATE["last_error"] = str(exc)
        print("PANEL_REFRESH_ERROR =", {
            "reason": reason,
            "error": str(exc),
        })
        for symbol in ("EURUSD", "XAUUSD"):
            record_auto_execution_gate(
                symbol,
                {},
                "PANEL_HANDOFF",
                "BLOCK",
                "STRATEGY_CALCULATION_FAILED",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "refresh_reason": reason,
                },
            )
        return False
    finally:
        PANEL_REFRESH_STATE["running"] = False
        PANEL_REFRESH_STATE["last_duration_seconds"] = round(
            time.time() - started_at,
            2,
        )
        PANEL_REFRESH_COMPLETE.set()
        PANEL_REFRESH_LOCK.release()

def refresh_panel_cache_direct(reason, force_refresh=True):
    started_at = time.time()
    PANEL_REFRESH_STATE.update({
        "last_started": started_at,
        "last_error": None,
        "reason": reason,
    })

    try:
        data = calculate_fresh_panel_data(
            reason,
            force_refresh=force_refresh,
        )
        refresh_live_panel_meta(data)
        validity = _panel_cache_validity(data)
        if validity["valid"]:
            update_panel_cache(data, reason)
        else:
            PANEL_REFRESH_STATE["last_error"] = (
                f"Fresh brain output has no usable market data: "
                f"{validity['reason']}"
            )
            print("PANEL_CACHE_RETURN_STALE =", {
                "reason": reason,
                "fresh_output_unusable": True,
                "validation_error": validity["reason"],
                "candle_counts": validity["candle_counts"],
            })
        PANEL_REFRESH_STATE["last_duration_seconds"] = round(
            time.time() - started_at,
            2,
        )
        print("PANEL_REFRESH_SUCCESS =", {
            "reason": reason,
            "seconds": PANEL_REFRESH_STATE["last_duration_seconds"],
            "direct": True,
            "cache_updated": validity["valid"],
        })
        return data
    except Exception as exc:
        PANEL_REFRESH_STATE["last_error"] = str(exc)
        PANEL_REFRESH_STATE["last_duration_seconds"] = round(
            time.time() - started_at,
            2,
        )
        print("PANEL_REFRESH_ERROR =", {
            "reason": reason,
            "error": str(exc),
            "direct": True,
        })
        return None


def schedule_panel_cache_refresh(reason, force_refresh=True):
    running_since = PANEL_REFRESH_STATE.get("last_started")
    running_seconds = (
        time.time() - float(running_since)
        if PANEL_REFRESH_STATE.get("running") and running_since
        else 0
    )
    refresh_stuck = running_seconds >= PANEL_REFRESH_STUCK_SECONDS

    if PANEL_REFRESH_STATE.get("running") and not refresh_stuck:
        print("PANEL_REFRESH_BACKGROUND_ALREADY_RUNNING =", {
            "reason": reason,
            "active_reason": PANEL_REFRESH_STATE.get("reason"),
            "running_seconds": round(running_seconds, 1),
        })
        return False

    # Clear before starting the thread so an initial API request cannot race
    # ahead of refresh_panel_cache() and mistake the previously-set event for
    # a completed refresh.
    PANEL_REFRESH_COMPLETE.clear()
    thread = threading.Thread(
        target=refresh_panel_cache,
        kwargs={
            "reason": reason,
            "force_refresh": force_refresh,
        },
        daemon=True,
    )
    thread.start()
    print("PANEL_REFRESH_BACKGROUND_SCHEDULED =", {
        "reason": reason,
        "force_refresh": bool(force_refresh),
        "previous_refresh_stuck": bool(refresh_stuck),
    })
    return True


def wait_for_initial_panel_cache(timeout=PANEL_INITIAL_DATA_WAIT_SECONDS):
    """Wait once for startup market data instead of returning a blank panel."""
    PANEL_REFRESH_COMPLETE.wait(timeout=max(float(timeout or 0), 0))
    cached_data = PANEL_CACHE.get("data")
    validity = _panel_cache_validity(cached_data)
    if not validity["valid"]:
        return None
    return cached_data


def warm_panel_cache_from_persisted_candles():
    try:
        from brain import hydrate_market_data_cache_from_disk

        if not hydrate_market_data_cache_from_disk():
            print("PANEL_STARTUP_WARM_SKIPPED = persisted candles incomplete")
            return False

        return refresh_panel_cache(
            reason="startup_disk_cache",
            force_refresh=False,
        )
    except Exception as exc:
        PANEL_REFRESH_STATE["last_error"] = str(exc)
        print("PANEL_STARTUP_WARM_ERROR =", str(exc))
        return False


def refresh_panel_cache_from_disk(reason):
    try:
        from brain import get_panel_data, hydrate_market_data_cache_from_disk

        if not hydrate_market_data_cache_from_disk():
            return None

        data = get_panel_data(force_refresh=False)
        refresh_live_panel_meta(data)
        validity = _panel_cache_validity(data)

        if not validity["valid"]:
            PANEL_REFRESH_STATE["last_error"] = (
                f"Disk candle fallback unusable: {validity['reason']}"
            )
            print("PANEL_DISK_FALLBACK_UNUSABLE =", {
                "reason": reason,
                "validation_error": validity["reason"],
                "candle_counts": validity["candle_counts"],
            })
            return None

        update_panel_cache(data, reason)
        print("PANEL_DISK_FALLBACK_SUCCESS =", {
            "reason": reason,
            "candle_counts": validity["candle_counts"],
        })
        return data
    except Exception as exc:
        PANEL_REFRESH_STATE["last_error"] = str(exc)
        print("PANEL_DISK_FALLBACK_ERROR =", {
            "reason": reason,
            "error": str(exc),
        })
        return None


def schedule_panel_refresh(reason="api_cache_stale"):
    if PANEL_REFRESH_STATE.get("running"):
        return False

    thread = threading.Thread(
        target=refresh_panel_cache,
        kwargs={"reason": reason},
        daemon=True,
    )
    thread.start()
    return True


def get_signal_setup_id(plan, side=None):
    if not isinstance(plan, dict):
        return None

    signal_side = str(side or plan.get("signal") or "").upper()
    if signal_side not in ["BUY", "SELL"]:
        return None
    if plan.get("news_event_id") and plan.get("signal_setup_id"):
        return str(plan.get("signal_setup_id"))

    breakout = (
        plan.get("fifteen_m_swing_break")
        if isinstance(plan.get("fifteen_m_swing_break"), dict)
        else {}
    )
    confirmation = (
        plan.get("confirmation_5m")
        if isinstance(plan.get("confirmation_5m"), dict)
        else {}
    )
    swing = breakout.get("swing") if isinstance(breakout.get("swing"), dict) else {}
    explicit_identity = (
        plan.get("setup_identity")
        if isinstance(plan.get("setup_identity"), dict)
        else {}
    )
    setup_parts = {
        "symbol": normalize_symbol(
            explicit_identity.get("symbol") or plan.get("symbol")
        ),
        "direction": signal_side,
        "swing_type": explicit_identity.get("swing_type") or swing.get("type"),
        "swing_timestamp": (
            explicit_identity.get("swing_timestamp") or swing.get("time")
        ),
        "swing_price": (
            explicit_identity.get("swing_price")
            if explicit_identity.get("swing_price") is not None
            else swing.get("price")
        ),
        "bos_candle_timestamp": (
            explicit_identity.get("bos_candle_timestamp")
            or plan.get("fifteen_m_break_time")
            or breakout.get("break_time")
        ),
        "bos_level": (
            explicit_identity.get("bos_level")
            if explicit_identity.get("bos_level") is not None
            else plan.get("fifteen_m_swing_level")
            if plan.get("fifteen_m_swing_level") is not None
            else breakout.get("level")
        ),
        "confirmation_timestamp": (
            explicit_identity.get("confirmation_timestamp")
            or plan.get("five_m_closed_candle_time")
            or confirmation.get("confirmation_close_time")
        ),
    }
    indicator_event_id = (
        explicit_identity.get("indicator_event_id")
        or plan.get("source_indicator_event_id")
        or breakout.get("indicator_event_id")
    )
    m5_confirmation_id = (
        explicit_identity.get("m5_confirmation_id")
        or plan.get("m5_confirmation_id")
        or confirmation.get("confirmation_id")
    )
    if indicator_event_id or m5_confirmation_id:
        setup_parts.update({
            "indicator_event_id": indicator_event_id,
            "m5_confirmation_id": m5_confirmation_id,
        })
    if any(value in [None, ""] for value in setup_parts.values()):
        # Legacy plans are still compared for UI lifecycle cleanup only. Live
        # execution separately requires the complete strict setup identity,
        # so this compatibility fingerprint cannot authorize an order.
        legacy_parts = {
            "symbol": normalize_symbol(plan.get("symbol")) or "LEGACY_UNSPECIFIED",
            "direction": signal_side,
            "strategy_setup_type": plan.get("strategy_setup_type"),
            "setup_candle_time": plan.get("setup_candle_time"),
        }
        if any(
            legacy_parts.get(key) in [None, ""]
            for key in ("strategy_setup_type", "setup_candle_time")
        ):
            return None
        encoded = json.dumps(legacy_parts, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    encoded = json.dumps(setup_parts, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_latest_consumed_signal_setup(symbol):
    normalized_symbol = normalize_symbol(symbol)

    for trade in LIVE_TRADE_HISTORY:
        if not isinstance(trade, dict):
            continue
        if normalize_symbol(trade.get("symbol")) != normalized_symbol:
            continue
        if get_live_trade_status(trade) in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]:
            continue

        setup_id = trade.get("signal_setup_id")
        side = str(trade.get("side") or trade.get("action") or "").upper()
        if setup_id and side in ["BUY", "SELL"]:
            return {
                "signal_setup_id": setup_id,
                "side": side,
                "closed_at": trade.get("closed_at"),
            }

    return None


def get_consumed_active_signal_setup(trade, side, setup_id):
    """Return a setup consumed because an existing position blocked execution."""
    if not isinstance(trade, dict) or not setup_id:
        return None
    normalized_side = str(side or "").upper()
    for item in trade.get("consumed_signal_setups") or []:
        if not isinstance(item, dict):
            continue
        if (
            item.get("signal_setup_id") == setup_id
            and str(item.get("side") or "").upper() == normalized_side
        ):
            return item
    return None


def find_consumed_signal_setup(symbol, side, setup_id):
    """Find an exact consumed setup in either the active trade or trade history."""
    normalized_symbol = normalize_symbol(symbol)
    candidates = [LIVE_ACTIVE_ORDERS.get(normalized_symbol), *LIVE_TRADE_HISTORY]
    for trade in candidates:
        if not isinstance(trade, dict):
            continue
        if normalize_symbol(trade.get("symbol")) != normalized_symbol:
            continue
        consumed = get_consumed_active_signal_setup(trade, side, setup_id)
        if consumed:
            return consumed
    return None


def consume_signal_setup_for_active_trade(symbol, plan, side):
    """Persist that a fresh setup was discarded while the symbol was occupied."""
    normalized_symbol = normalize_symbol(symbol)
    normalized_side = str(side or "").upper()
    setup_id = get_signal_setup_id(plan, normalized_side)
    active_trade = LIVE_ACTIVE_ORDERS.get(normalized_symbol)
    if (
        not setup_id
        or normalized_side not in ["BUY", "SELL"]
        or not isinstance(active_trade, dict)
        or get_live_trade_status(active_trade) not in ACTIVE_TRADE_EXECUTION_STATUSES
    ):
        return None

    existing = get_consumed_active_signal_setup(active_trade, normalized_side, setup_id)
    if existing:
        return existing

    consumed = {
        "signal_setup_id": setup_id,
        "side": normalized_side,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
        "reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
    }
    entries = [
        item
        for item in (active_trade.get("consumed_signal_setups") or [])
        if isinstance(item, dict)
    ]
    entries.append(consumed)
    active_trade["consumed_signal_setups"] = entries[-20:]
    persist_live_trade_state(active_trade)
    print("ACTIVE_TRADE_SIGNAL_CONSUMED =", {
        "symbol": normalized_symbol,
        "side": normalized_side,
        "signal_setup_id": setup_id,
        "active_trade_id": get_live_trade_identity(active_trade),
        "reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
    })
    return consumed


def reset_consumed_smc_plan(plan, side, *, active_trade=None):
    """Clear a consumed setup without changing the position that blocked it."""
    if not isinstance(plan, dict):
        return
    normalized_side = str(side or "").upper()
    plan["consumed_market_signal"] = normalized_side
    plan["signal"] = "WAIT"
    plan["final_signal"] = "WAIT"
    plan["signal_display_state"] = "WAIT"
    plan["display_signal"] = "WAIT"
    plan["history_signal"] = "WAIT"
    plan["strategy_decision"] = "WAIT"
    plan["fresh_entry_available"] = False
    plan["strategy_setup_complete"] = False
    plan["smc_progress"] = 0
    plan["smc_status"] = "WAIT"
    plan["next_trigger"] = "Fresh 15m BOS/CHoCH"
    plan["plan_bias"] = "WAIT"
    plan["plan_type"] = "WAIT FOR NEW SIGNAL"
    plan["entry_timing"] = "WAIT FOR FRESH SETUP"
    plan["blocked_by"] = "consumed_during_active_trade"
    plan["blocked_reason"] = (
        f"The fresh {normalized_side} setup passed while a trade was already running "
        "and was discarded. Waiting for a new 15m setup."
    )
    plan["blocker_rule_name"] = "fresh_setup_consumed_during_active_trade"
    plan["trade_setup_consumed"] = True
    plan["execution_allowed"] = False
    plan["execution_status"] = "NOT_APPLICABLE"
    plan["execution_block_reason"] = None
    plan["signal_text"] = "WAIT ⚪ (waiting for a fresh setup)"
    plan.pop("executed_trade_setup_snapshot", None)
    plan.pop("smc_plan_state_source", None)
    plan.pop("current_setup_state", None)
    if isinstance(active_trade, dict):
        plan["trade_already_running"] = True
        plan["active_trade_side"] = str(
            active_trade.get("side") or active_trade.get("action") or ""
        ).upper() or None
        plan["active_trade_status"] = get_live_trade_status(active_trade)


def first_snapshot_value(*values):
    for value in values:
        if value not in [None, "", "--"]:
            return copy.deepcopy(value)
    return None


def build_executed_trade_setup_snapshot(symbol, side, plan, trade_payload, position_id, timestamp=None):
    plan = plan if isinstance(plan, dict) else {}
    trade_payload = trade_payload if isinstance(trade_payload, dict) else {}
    debug = plan.get("strategy_debug") or plan.get("entry_strategy_debug") or {}
    debug = debug if isinstance(debug, dict) else {}
    swing_break = plan.get("fifteen_m_swing_break") or debug.get("fifteen_m_swing_break_detail") or {}
    swing_break = swing_break if isinstance(swing_break, dict) else {}
    execution_time = float(timestamp or time.time())
    if execution_time > 100000000000:
        execution_time /= 1000.0
    normalized_symbol = normalize_symbol(symbol)

    return {
        "snapshot_version": 1,
        "immutable": True,
        "symbol": normalized_symbol,
        "direction": str(side or "").upper(),
        "structure": first_snapshot_value(
            plan.get("structure_trend"),
            plan.get("structure_type"),
            debug.get("htf_structure"),
            debug.get("structure_type"),
        ),
        "bos_choch": first_snapshot_value(
            debug.get("smc_direction"),
            "CHOCH" if debug.get("choch_detected") else None,
            "BOS" if debug.get("bos_detected") else None,
            side,
        ),
        "bos_detected": bool(debug.get("bos_detected") or not debug.get("choch_detected")),
        "choch_detected": bool(debug.get("choch_detected")),
        "break_level": first_snapshot_value(
            debug.get("fifteen_m_break_level"),
            debug.get("fifteen_m_swing_level"),
            debug.get("fifteen_m_bos_level"),
            plan.get("fifteen_m_swing_level"),
            plan.get("fifteen_m_bos_level"),
            swing_break.get("level"),
        ),
        "fifteen_m_close": first_snapshot_value(
            debug.get("fifteen_m_close"),
            debug.get("fifteen_m_close_price"),
            plan.get("fifteen_m_close"),
            True,
        ),
        "fifteen_m_close_confirmed": True,
        "five_m_confirmation": first_snapshot_value(
            debug.get("five_m_confirmation_detail"),
            debug.get("five_m_confirmation_price"),
            plan.get("five_m_confirmation"),
            True,
        ),
        "five_m_confirmation_confirmed": True,
        "swing_sl": first_snapshot_value(
            debug.get("selected_swing_sl"),
            debug.get("final_sl"),
            plan.get("selected_swing_sl"),
            trade_payload.get("sl"),
        ),
        "entry": first_snapshot_value(trade_payload.get("entry"), plan.get("entry_price"), plan.get("entry")),
        "sl": first_snapshot_value(trade_payload.get("sl"), plan.get("stop_loss"), plan.get("sl")),
        "tp1": first_snapshot_value(trade_payload.get("tp1"), plan.get("tp1")),
        "tp2": first_snapshot_value(trade_payload.get("tp2"), plan.get("tp2")),
        "timestamp": execution_time,
        "executed_at": datetime.fromtimestamp(execution_time, tz=timezone.utc).isoformat(),
        "trade_id": f"ctrader-pos-{position_id}" if position_id else None,
        "position_id": position_id,
        "broker_position_id": position_id,
        "status": "EXECUTED",
        "progress": 100,
        "conditions": {
            "bos_choch": True,
            "structure_break": True,
            "fifteen_m_close": True,
            "five_m_confirmation": True,
            "swing_sl": True,
            "tp_rr_validation": True,
        },
    }


def executed_snapshot_matches_trade(snapshot, trade):
    if not isinstance(snapshot, dict) or not isinstance(trade, dict):
        return False
    snapshot_position_id = snapshot.get("broker_position_id") or snapshot.get("position_id")
    trade_position_id = trade.get("broker_position_id") or trade.get("position_id")
    return (
        normalize_symbol(snapshot.get("symbol")) == normalize_symbol(trade.get("symbol"))
        and snapshot_position_id is not None
        and str(snapshot_position_id) == str(trade_position_id)
    )


def ensure_executed_snapshot_for_active_trade(trade, plan=None):
    if not isinstance(trade, dict):
        return trade
    snapshot = trade.get("executed_trade_setup_snapshot")
    if executed_snapshot_matches_trade(snapshot, trade):
        return trade
    position_id = trade.get("broker_position_id") or trade.get("position_id")
    side = trade.get("side") or trade.get("action")
    if position_id is None or str(side or "").upper() not in ["BUY", "SELL"]:
        return trade
    snapshot = build_executed_trade_setup_snapshot(
        trade.get("symbol"),
        side,
        plan or {},
        trade,
        position_id,
        timestamp=trade.get("opened_at") or time.time(),
    )
    snapshot["recovered_from_active_position"] = True
    trade["executed_trade_setup_snapshot"] = snapshot
    return trade


ACTIVE_TRADE_EXECUTION_BLOCK_REASON = "ACTIVE_TRADE_ALREADY_RUNNING"
ACTIVE_TRADE_EXECUTION_STATUSES = {"RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"}


def get_active_trade_execution_block(symbol):
    """Return canonical execution-only block metadata for an open symbol trade."""
    normalized_symbol = normalize_symbol(symbol)
    active_trade = LIVE_ACTIVE_ORDERS.get(normalized_symbol)
    if not isinstance(active_trade, dict):
        return None
    status = get_live_trade_status(active_trade)
    if status not in ACTIVE_TRADE_EXECUTION_STATUSES:
        return None
    return {
        "execution_allowed": False,
        "execution_status": "BLOCKED",
        "execution_block_reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
        "active_trade_direction": str(
            active_trade.get("side") or active_trade.get("action") or ""
        ).upper() or None,
        "active_trade_id": (
            active_trade.get("trade_id")
            or active_trade.get("broker_position_id")
            or active_trade.get("position_id")
            or active_trade.get("broker_order_id")
            or active_trade.get("order_id")
        ),
        "active_trade_status": status,
        "symbol": normalized_symbol,
        "source": "V1",
    }


def apply_trade_signal_lifecycle(panel_data):
    if not isinstance(panel_data, dict):
        return panel_data

    def sync_plan_diagnostics(plan):
        if not isinstance(plan, dict):
            return

        reason = plan.get("blocked_reason") or plan.get("block_reason")
        signal = str(plan.get("signal") or "WAIT").upper()
        final_signal = str(plan.get("final_signal") or signal or "WAIT").upper()
        blocked = final_signal not in ["BUY", "SELL"]

        for key in ["signal_diagnostics", "entry_strategy_debug", "strategy_debug"]:
            debug = plan.get(key)
            if not isinstance(debug, dict):
                debug = {}
            debug.update({
                "final_signal": final_signal if final_signal in ["BUY", "SELL"] else "WAIT",
                "signal_after_filters": final_signal if final_signal in ["BUY", "SELL"] else "WAIT",
                "blocked": blocked,
                "blocked_by": plan.get("blocked_by"),
                "blocked_reason": reason if blocked else None,
                "block_reason": reason if blocked else None,
                "trade_already_running": bool(plan.get("trade_already_running")),
                "active_trade_side": plan.get("active_trade_side"),
                "active_trade_status": plan.get("active_trade_status"),
                "strategy_decision": plan.get("strategy_decision"),
                "execution_allowed": plan.get("execution_allowed"),
                "execution_status": plan.get("execution_status"),
                "execution_block_reason": plan.get("execution_block_reason"),
                "active_trade_direction": plan.get("active_trade_direction"),
                "active_trade_id": plan.get("active_trade_id"),
                "execution_source": plan.get("execution_source"),
            })
            plan[key] = debug

    def apply_active_trade_display(plan, active_trade, side, status):
        strategy_signal = str(plan.get("signal") or "WAIT").upper()
        if strategy_signal not in ["BUY", "SELL"]:
            strategy_signal = "WAIT"
        current_setup_state = copy.deepcopy({
            key: value
            for key, value in plan.items()
            if key not in [
                "current_setup_state",
                "executed_trade_setup_snapshot",
                "smc_plan_state_source",
            ]
        })
        plan["current_setup_state"] = current_setup_state
        plan["strategy_decision"] = strategy_signal
        plan["execution_source"] = "V1"
        plan["active_trade_direction"] = side
        plan["active_trade_id"] = (
            active_trade.get("trade_id")
            or active_trade.get("broker_position_id")
            or active_trade.get("position_id")
            or active_trade.get("broker_order_id")
            or active_trade.get("order_id")
        )
        if strategy_signal in ["BUY", "SELL"]:
            plan["execution_allowed"] = False
            plan["execution_status"] = "BLOCKED"
            plan["execution_block_reason"] = ACTIVE_TRADE_EXECUTION_BLOCK_REASON
        else:
            plan["execution_allowed"] = False
            plan["execution_status"] = "NOT_APPLICABLE"
            plan["execution_block_reason"] = None
        snapshot = active_trade.get("executed_trade_setup_snapshot")
        if executed_snapshot_matches_trade(snapshot, active_trade):
            plan["executed_trade_setup_snapshot"] = copy.deepcopy(snapshot)
            plan["smc_plan_state_source"] = "executed_trade_setup_snapshot"
            plan["plan_type"] = "EXECUTED" if status == "OPEN" else "RUNNING"
            plan["plan_bias"] = snapshot.get("direction") or side
            plan["entry_price"] = snapshot.get("entry")
            plan["stop_loss"] = snapshot.get("sl")
            plan["tp1"] = snapshot.get("tp1")
            plan["tp2"] = snapshot.get("tp2")
            plan["strategy_setup_complete"] = True
            plan["trade_already_running"] = True
            plan["active_trade_side"] = side
            plan["active_trade_status"] = status
            plan["smc_progress"] = 100
            plan["smc_status"] = "EXECUTED" if status == "OPEN" else "RUNNING"
            plan["next_trigger"] = None
            plan["final_signal"] = strategy_signal
            plan["signal_display_state"] = strategy_signal
            plan["display_signal"] = strategy_signal
            plan["history_signal"] = strategy_signal
            plan["strategy_debug"] = {
                **(plan.get("strategy_debug") or {}),
                "smc_direction": snapshot.get("direction") or side,
                "bos_detected": bool(snapshot.get("bos_detected")),
                "choch_detected": bool(snapshot.get("choch_detected")),
                "fifteen_m_swing_break_confirmed": True,
                "fifteen_m_break_level": snapshot.get("break_level"),
                "fifteen_m_close_confirmed": True,
                "five_m_confirmation": True,
                "selected_swing_sl": snapshot.get("swing_sl") or snapshot.get("sl"),
                "sl_valid": True,
                "final_signal": strategy_signal,
                "strategy_decision": strategy_signal,
                "execution_allowed": plan.get("execution_allowed"),
                "execution_block_reason": plan.get("execution_block_reason"),
            }
            sync_plan_diagnostics(plan)
            return strategy_signal

        original_signal = str(plan.get("signal") or "WAIT").upper()
        setup_still_confirmed = original_signal in ["BUY", "SELL"]
        strategy_signal = original_signal if setup_still_confirmed else "WAIT"
        display_signal = strategy_signal

        plan["market_signal"] = original_signal
        plan["signal"] = strategy_signal
        plan["final_signal"] = strategy_signal
        plan["signal_display_state"] = display_signal
        plan["display_signal"] = display_signal
        plan["history_signal"] = display_signal
        plan["setup_still_confirmed"] = setup_still_confirmed
        plan["active_trade_side"] = side
        plan["active_trade_status"] = status
        plan["fresh_entry_available"] = False
        plan["trade_already_running"] = True
        plan["blocked_by"] = "active_trade_running"
        if strategy_signal in ["BUY", "SELL"]:
            plan["blocked_reason"] = ACTIVE_TRADE_EXECUTION_BLOCK_REASON
        plan["blocker_rule_name"] = "active_trade_running"
        plan["signal_text"] = (
            f"{strategy_signal} {'🟢' if strategy_signal == 'BUY' else '🔴'} (trade already running)"
            if strategy_signal in ["BUY", "SELL"]
            else "WAIT ⚪ (trade running; setup no longer fresh)"
        )
        plan["plan_type"] = "TRADE ALREADY RUNNING"
        plan["plan_bias"] = strategy_signal if strategy_signal in ["BUY", "SELL"] else side
        plan["entry_timing"] = "TRADE ALREADY RUNNING"
        plan["strategy_setup_complete"] = False

        level_map = {
            "entry_price": ["entry", "entry_price", "current_price"],
            "stop_loss": ["sl", "stop_loss", "planned_sl"],
            "tp1": ["tp1", "planned_tp1"],
            "tp2": ["tp2", "planned_tp2"],
        }
        for plan_key, trade_keys in level_map.items():
            if plan.get(plan_key) not in [None, "", "--"]:
                continue
            for trade_key in trade_keys:
                value = active_trade.get(trade_key)
                if value not in [None, "", "--"]:
                    plan[plan_key] = value
                    break

        sync_plan_diagnostics(plan)
        return display_signal

    for symbol in ["EURUSD", "XAUUSD"]:
        plan = panel_data.get(symbol)
        active_trade = LIVE_ACTIVE_ORDERS.get(symbol)

        if not isinstance(plan, dict):
            continue

        current_signal = str(plan.get("signal") or "WAIT").upper()
        if current_signal not in ["BUY", "SELL"]:
            current_signal = "WAIT"
        current_setup_id = get_signal_setup_id(plan, current_signal)
        plan["strategy_decision"] = current_signal
        plan.setdefault(
            "execution_allowed",
            None if current_signal in ["BUY", "SELL"] else False,
        )
        plan.setdefault(
            "execution_status",
            "PENDING" if current_signal in ["BUY", "SELL"] else "NOT_APPLICABLE",
        )
        plan.setdefault("execution_block_reason", None)
        plan.setdefault("execution_source", "V1")
        plan.setdefault("fresh_entry_available", current_signal in ["BUY", "SELL"])
        plan.setdefault("trade_already_running", False)
        plan.setdefault("signal_display_state", current_signal)

        if isinstance(active_trade, dict):
            trade_status = get_live_trade_status(active_trade)
            trade_side = str(
                active_trade.get("side")
                or active_trade.get("action")
                or ""
            ).upper()

            if trade_status in ACTIVE_TRADE_EXECUTION_STATUSES:
                consumed_while_active = get_consumed_active_signal_setup(
                    active_trade,
                    current_signal,
                    current_setup_id,
                )
                setup_opened_active_trade = bool(
                    current_setup_id
                    and active_trade.get("signal_setup_id") == current_setup_id
                )
                if consumed_while_active or setup_opened_active_trade:
                    reset_consumed_smc_plan(
                        plan,
                        current_signal,
                        active_trade=active_trade,
                    )
                    sync_plan_diagnostics(plan)
                    print("ACTIVE_TRADE_SIGNAL_RESET_DEBUG =", {
                        "symbol": symbol,
                        "side": current_signal,
                        "signal_setup_id": current_setup_id,
                        "display_signal": "WAIT",
                        "active_trade_status": trade_status,
                        "reason": (
                            "setup opened the active trade"
                            if setup_opened_active_trade
                            else "fresh setup was consumed while trade remained active"
                        ),
                    })
                    continue

                display_signal = apply_active_trade_display(
                    plan,
                    active_trade,
                    trade_side,
                    trade_status,
                )
                plan["active_trade_side"] = trade_side
                plan["active_trade_status"] = trade_status
                print("ACTIVE_TRADE_DISPLAY_DEBUG =", {
                    "symbol": symbol,
                    "side": trade_side,
                    "broker_status": trade_status,
                    "market_signal": current_signal,
                    "display_signal": display_signal,
                    "fresh_entry_available": plan.get("fresh_entry_available"),
                    "trade_already_running": plan.get("trade_already_running"),
                    "reason": "active trade display keeps current strategy signal",
                })
                continue

        consumed = (
            find_consumed_signal_setup(symbol, current_signal, current_setup_id)
            or get_latest_consumed_signal_setup(symbol)
        )
        if (
            current_signal not in ["BUY", "SELL"]
            or not current_setup_id
            or not consumed
            or consumed.get("side") != current_signal
            or consumed.get("signal_setup_id") != current_setup_id
        ):
            continue

        reset_consumed_smc_plan(plan, current_signal)
        plan["trade_already_running"] = False
        plan["signal_text"] = f"WAIT ⚪ ({current_signal} setup consumed; waiting for a fresh setup)"
        plan["blocked_by"] = "consumed_trade_setup"
        plan["blocked_reason"] = (
            f"The {current_signal} setup was already used. "
            "Waiting for a new 15m swing break."
        )
        plan["blocker_rule_name"] = "fresh_setup_required_after_trade_close"
        plan["trade_setup_consumed"] = True
        sync_plan_diagnostics(plan)
        print("CLOSED_TRADE_SIGNAL_RESET_DEBUG =", {
            "symbol": symbol,
            "side": current_signal,
            "signal_setup_id": current_setup_id,
            "display_signal": "WAIT",
            "reason": "closed trade consumed the current setup",
        })

    return panel_data


def overlay_live_forming_candles(panel_data, live_price_status, now=None):
    if not isinstance(panel_data, dict):
        return panel_data

    live_price_status = (
        live_price_status
        if isinstance(live_price_status, dict)
        else {}
    )
    prices = live_price_status.get("live_prices") or {}
    now_timestamp = float(now if now is not None else time.time())
    timeframe_seconds = {
        "5m": 5 * 60,
        "15m": 15 * 60,
        "1h": 60 * 60,
    }

    for symbol in ["EURUSD", "XAUUSD"]:
        tick = prices.get(symbol) or {}
        tick_timestamp = tick.get("timestamp")

        try:
            tick_age = now_timestamp - float(tick_timestamp)
            tick_price = float(
                tick.get("mid")
                or tick.get("bid")
                or tick.get("ask")
            )
        except (TypeError, ValueError):
            continue

        if tick_price <= 0 or tick_age < 0 or tick_age > 10:
            continue

        symbol_candles = (
            (panel_data.get("candles") or {}).get(symbol)
        )
        if not isinstance(symbol_candles, dict):
            continue

        for timeframe, seconds in timeframe_seconds.items():
            candles = symbol_candles.get(timeframe)
            if not isinstance(candles, list) or not candles:
                continue

            bucket_time = int(now_timestamp // seconds) * seconds
            last = candles[-1]

            try:
                last_time = int(float(last.get("time")))
                previous_close = float(last.get("close"))
            except (TypeError, ValueError, AttributeError):
                continue

            if last_time < bucket_time:
                gap_seconds = bucket_time - last_time
                synthetic_open = (
                    tick_price
                    if gap_seconds > seconds * 2
                    else previous_close
                )
                candles.append({
                    "time": bucket_time,
                    "open": synthetic_open,
                    "high": max(synthetic_open, tick_price),
                    "low": min(synthetic_open, tick_price),
                    "close": tick_price,
                })
            elif last_time == bucket_time:
                try:
                    last["high"] = max(
                        float(last.get("high")),
                        tick_price,
                    )
                    last["low"] = min(
                        float(last.get("low")),
                        tick_price,
                    )
                except (TypeError, ValueError):
                    last["high"] = max(previous_close, tick_price)
                    last["low"] = min(previous_close, tick_price)
                last["close"] = tick_price

            if timeframe == "5m":
                plan = panel_data.get(symbol)
                if isinstance(plan, dict):
                    source = plan.setdefault("signal_data_source", {})
                    source.setdefault(
                        "latest_closed_5m_time",
                        source.get("latest_5m_time"),
                    )
                    source["latest_5m_time"] = datetime.fromtimestamp(
                        bucket_time,
                        tz=timezone.utc,
                    ).isoformat()
                    source["candle_source"] = "ctrader_live_tick"
                    source["display_tick_age_seconds"] = round(tick_age, 2)

        print("LIVE_FORMING_CANDLE_DEBUG =", {
            "symbol": symbol,
            "bucket_time": datetime.fromtimestamp(
                int(now_timestamp // 300) * 300,
                tz=timezone.utc,
            ).isoformat(),
            "tick_price": tick_price,
            "tick_age_seconds": round(tick_age, 2),
        })

    return panel_data


def refresh_live_panel_meta(panel_data):
    actionable_plans = _actionable_panel_plans(panel_data)

    try:
        live_positions = sync_live_positions(panel_data)
    except Exception as exc:
        LIVE_PANEL_META_CACHE["last_error"] = str(exc)
        for symbol, _signal, plan in actionable_plans:
            record_auto_execution_gate(
                symbol,
                plan,
                "POSITION_STATE_SYNC",
                "BLOCK",
                "BROKER_POSITION_SYNC_FAILED",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
        print("LIVE_PANEL_META_CRITICAL_ERROR =", {
            "stage": "position_state_sync",
            "error": str(exc),
        })
        return False

    try:
        apply_trade_signal_lifecycle(panel_data)
    except Exception as exc:
        LIVE_PANEL_META_CACHE["last_error"] = str(exc)
        for symbol, _signal, plan in actionable_plans:
            record_auto_execution_gate(
                symbol,
                plan,
                "TRADE_SIGNAL_LIFECYCLE",
                "BLOCK",
                "LIFECYCLE_SAFETY_CHECK_FAILED",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
        print("LIVE_PANEL_META_CRITICAL_ERROR =", {
            "stage": "trade_signal_lifecycle",
            "error": str(exc),
        })
        return False

    try:
        apply_broker_closed_to_panel_signal_history(panel_data)
    except Exception as exc:
        print("LIVE_PANEL_METADATA_WARNING =", {
            "stage": "broker_closed_display_overlay",
            "error": str(exc),
            "execution_blocked": False,
        })

    try:
        run_ctrader_auto_trade_checks(panel_data)
    except Exception as exc:
        LIVE_PANEL_META_CACHE["last_error"] = str(exc)
        for symbol, _signal, plan in actionable_plans:
            record_auto_execution_gate(
                symbol,
                plan,
                "AUTO_EXECUTION",
                "BLOCK",
                "AUTO_EXECUTION_EVALUATION_FAILED",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
        print("LIVE_PANEL_META_CRITICAL_ERROR =", {
            "stage": "auto_execution",
            "error": str(exc),
        })
        return False

    trade_management_error = None
    try:
        update_live_trade_exit_states(panel_data)
    except Exception as exc:
        trade_management_error = str(exc)
        print("LIVE_TRADE_MANAGEMENT_ERROR =", {
            "stage": "open_position_lifecycle",
            "error": str(exc),
        })

    def optional_metadata(stage, getter, fallback):
        try:
            return getter()
        except Exception as exc:
            print("LIVE_PANEL_METADATA_WARNING =", {
                "stage": stage,
                "error": str(exc),
                "execution_blocked": False,
            })
            return fallback

    live_price_status = optional_metadata("live_prices", get_live_prices, {})
    live_pl_sync = optional_metadata("live_pl_sync", calculate_live_pl_sync, {})
    live_recent_history = optional_metadata(
        "live_recent_history",
        get_live_recent_history_for_panel,
        [],
    )
    live_trade_stats = optional_metadata(
        "live_trade_stats",
        calculate_live_trade_stats,
        {},
    )
    last_execution_time = optional_metadata(
        "last_execution_time",
        get_last_execution_time,
        LIVE_PANEL_META_CACHE.get("last_execution_time"),
    )

    LIVE_PANEL_META_CACHE.update({
        "live_positions": live_positions or [],
        "live_price_status": live_price_status or {},
        "live_pl_sync": live_pl_sync or {},
        "live_recent_history": live_recent_history or [],
        "live_trade_stats": live_trade_stats or {},
        "last_execution_time": last_execution_time,
        "last_update": time.time(),
        "last_error": trade_management_error,
    })
    print("LIVE_PANEL_META_REFRESH_SUCCESS =", {
        "positions": len(live_positions or []),
        "history": len(live_recent_history or []),
        "trade_management_error": trade_management_error,
    })
    return trade_management_error is None


def background_fetch():
    while True:
        ENGINE_RUNTIME_STATE["last_loop_started"] = time.time()
        ENGINE_RUNTIME_STATE["loop_iterations"] += 1
        try:
            print("🔄 BACKGROUND FETCH (once for all users)")
            # Server-owned recovery: a browser request is never required to
            # start or restart the cTrader price stream.
            stream_state = start_ctrader_live_price_stream()
            if not stream_state.get("ok"):
                print("CTRADER_LIVE_STREAM_RETRY_WAITING =", {
                    "reason": stream_state.get("reason"),
                })
            if refresh_panel_cache(reason="background"):
                refresh_live_panel_meta(PANEL_CACHE["data"])
                print("✅ Cache updated globally")
            ENGINE_RUNTIME_STATE["last_loop_completed"] = time.time()
            ENGINE_RUNTIME_STATE["last_loop_error"] = None

        except Exception as e:
            ENGINE_RUNTIME_STATE["last_loop_error"] = str(e)
            print("❌ Background fetch error:", e)

        time.sleep(CACHE_SECONDS)

@app.get("/")
def root():
    return {"message": "NathauxFX backend is running"}

@app.get("/news-impact")
def news_impact(symbol: str = "EURUSD"):
    context = get_news_market_context(PANEL_CACHE.get("data"), symbol)
    return get_news_impact(
        symbol,
        candles_5m=context["candles_5m"],
        candles_15m=context["candles_15m"],
        entry_price=context["entry_price"],
        provider_timeout=2,
    )

@app.get("/health-check")
def health_check():
    account_state = sync_ctrader_account_state(force=True)
    candle_status = {}

    try:
        from ctrader_connector import get_ctrader_candle_cache_status
        candle_status = get_ctrader_candle_cache_status()
    except Exception as exc:
        candle_status = {
            "ctrader_last_error": str(exc),
        }

    return {
        "ok": True,
        "backend_online": True,
        "ctrader_auth_status": "connected" if account_state.get("connected") else "disconnected",
        "active_account": account_state.get("account_id"),
        "data_source_status": os.getenv("MARKET_DATA_SOURCE", "ctrader"),
        "last_candle_time": candle_status.get("ctrader_last_success"),
        "last_candle_error": candle_status.get("ctrader_last_error"),
        "auto_trade_status": {
            "paper": AUTO_TRADE_ENABLED.get("enabled"),
            "live": LIVE_AUTO_TRADE_ENABLED.get("enabled"),
        },
        "feature_flags": load_feature_flags(),
        "database": database_runtime_status(),
    }


def database_runtime_status():
    status = get_database_status()
    response = {
        "backend": status.get("backend"),
        "driver": status.get("driver"),
        "configured_by_database_url": status.get("configured_by_database_url"),
        "durable": status.get("durable"),
        "connected": False,
        "migration_revision": None,
    }
    try:
        with database_engine.connect() as connection:
            connection.execute(sql_text("SELECT 1"))
            revision = connection.execute(
                sql_text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar_one_or_none()
        response.update({
            "connected": True,
            "migration_revision": revision,
        })
    except Exception as exc:
        response.update({
            "error": "Database connection or migration check failed.",
            "error_type": type(exc).__name__,
        })
    return response


@app.get("/database-status")
def database_status_endpoint():
    return database_runtime_status()


def auto_trade_state_response(refresh=True):
    if refresh:
        refresh_auto_trade_state_from_persistence("state_response")
    preference_enabled = bool(LIVE_AUTO_TRADE_ENABLED.get("enabled"))
    execution_ready = bool(
        LIVE_ACCOUNT_STATE.get("connected")
        and LIVE_ACCOUNT_STATE.get("execution_ready")
    )
    return {
        "paper_enabled": bool(AUTO_TRADE_ENABLED.get("enabled")),
        "live_enabled": preference_enabled,
        "live_execution_active": preference_enabled and execution_ready,
        "live_execution_paused": preference_enabled and not execution_ready,
        "pause_reason": (
            "broker_not_execution_ready"
            if preference_enabled and not execution_ready
            else None
        ),
        **AUTO_TRADE_STATE_METADATA,
    }


@app.get("/settings/auto-trade-state")
def get_auto_trade_state_setting(request: Request):
    _authenticated_settings_user(request)
    return {
        **auto_trade_state_response(),
        "recent_changes": get_auto_trade_state_changes(limit=10),
    }


@app.get("/system-status")
def system_status():
    now = time.time()
    thread_alive = bool(
        BACKGROUND_THREAD is not None and BACKGROUND_THREAD.is_alive()
    )
    last_loop = ENGINE_RUNTIME_STATE.get("last_loop_completed")
    loop_age = now - float(last_loop) if last_loop else None
    trading_engine_running = bool(
        thread_alive
        and loop_age is not None
        and loop_age <= max(CACHE_SECONDS * 6, 120)
    )
    panel = PANEL_CACHE.get("data") or {}
    feed_status = panel.get("feed_status") if isinstance(panel, dict) else {}
    market_data = {}
    for symbol in ["EURUSD", "XAUUSD"]:
        feed = (feed_status or {}).get(symbol) or {}
        source = feed.get("signal_data_source") or {}
        market_data[symbol] = {
            "available": source.get("available"),
            "reason": source.get("reason"),
            "source": source.get("used_for_signal") or source.get("candle_source"),
            "latest_5m_time": source.get("latest_5m_time"),
            "latest_15m_time": source.get("latest_15m_time"),
            "latest_1h_time": source.get("latest_1h_time"),
            "stale_5m_minutes": source.get("stale_5m_minutes"),
            "stale_15m_minutes": source.get("stale_15m_minutes"),
            "stale_1h_minutes": source.get("stale_1h_minutes"),
            "missed_fetch_count": source.get("missed_fetch_count"),
            "signal": (panel.get(symbol) or {}).get("signal"),
            "last_signal_timestamp": PANEL_CACHE.get("last_update") or None,
        }

    risk_settings = load_risk_settings()
    pl = LIVE_PANEL_META_CACHE.get("live_pl_sync") or {}
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend_online": True,
        "data_transport": "rest_polling",
        "frontend_websocket_expected": False,
        "browser_clients_required": 0,
        "trading_engine": {
            "running": trading_engine_running,
            "thread_alive": thread_alive,
            "process_id": ENGINE_RUNTIME_STATE.get("process_id"),
            "thread_id": ENGINE_RUNTIME_STATE.get("loop_thread_id"),
            "loop_iterations": ENGINE_RUNTIME_STATE.get("loop_iterations"),
            "last_loop_started": ENGINE_RUNTIME_STATE.get("last_loop_started"),
            "last_loop_completed": last_loop,
            "heartbeat_age_seconds": round(loop_age, 1) if loop_age is not None else None,
            "last_error": ENGINE_RUNTIME_STATE.get("last_loop_error"),
            "browser_required": False,
        },
        "auto_trade": auto_trade_state_response(),
        "broker": {
            "connected": bool(LIVE_ACCOUNT_STATE.get("connected")),
            "execution_ready": bool(LIVE_ACCOUNT_STATE.get("execution_ready")),
            "authorized": bool(LIVE_ACCOUNT_STATE.get("auth_ok")),
            "account_id": LIVE_ACCOUNT_STATE.get("account_id"),
            "environment": LIVE_ACCOUNT_STATE.get("mode"),
            "reason": LIVE_ACCOUNT_STATE.get("reason"),
            "last_success_at": LIVE_ACCOUNT_STATE.get("last_success_at"),
            "last_heartbeat": LIVE_ACCOUNT_STATE.get("last_success_at"),
            "connection_owner": "render_backend",
        },
        "market_data": market_data,
        "last_strategy_check": ENGINE_RUNTIME_STATE.get("last_strategy_check"),
        "last_valid_setup": ENGINE_RUNTIME_STATE.get("last_valid_setup"),
        "last_order_attempt": ENGINE_RUNTIME_STATE.get("last_order_attempt"),
        "last_broker_response": ENGINE_RUNTIME_STATE.get("last_broker_response"),
        "block_reasons": {
            symbol: {
                "status": status.get("status"),
                "reason": status.get("reason"),
                "checked_at": status.get("checked_at"),
            }
            for symbol, status in LIVE_AUTO_STATUS_BY_SYMBOL.items()
        },
        "limits": {
            "max_daily_loss": risk_settings.get("maxDailyLoss"),
            "max_weekly_loss": risk_settings.get("maxWeeklyLoss"),
            "daily_total_pl": pl.get("daily_total_pl", 0),
            "weekly_total_pl": pl.get("weekly_total_pl", 0),
            "daily_reset_at": pl.get("daily_reset_ts"),
            "weekly_reset_at": pl.get("weekly_reset_ts"),
            "timezone": "America/New_York",
            "reset_hour": "17:00",
        },
        "recent_auto_trade_changes": get_auto_trade_state_changes(limit=10),
    }

@app.get("/ctrader-diagnostics")
def ctrader_diagnostics():
    return get_ctrader_diagnostics()

def format_status_time(timestamp):
    if not timestamp:
        return None

    try:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(float(timestamp))
        )
    except (TypeError, ValueError):
        return None

LIVE_POSITION_SYNC_STATUS = {
    "last_success": None,
    "last_error": None,
}

@app.get("/ctrader/status")
@app.get("/ctrader-status")
def ctrader_status():
    # Status reads must never block the UI on live broker I/O. The background
    # engine and execution gates perform authoritative live verification.
    account_state = get_ctrader_connection_snapshot()
    refresh_token_status = get_ctrader_refresh_token_status()
    selection_debug = get_ctrader_account_selection_debug()
    connected = bool(account_state.get("connected"))
    last_error = LIVE_POSITION_SYNC_STATUS.get("last_error")
    positions_count = sum(
        1 for trade in LIVE_ACTIVE_ORDERS.values() if isinstance(trade, dict)
    )

    if connected:
        reason = account_state.get("reason") or "authenticated"
    else:
        reason = account_state.get("reason") or "broker disconnected"

    return {
        "connected": connected,
        "auth_ok": bool(account_state.get("auth_ok")),
        "account_found": bool(account_state.get("account_found")),
        "execution_ready": bool(account_state.get("execution_ready")),
        "degraded": bool(account_state.get("degraded")),
        "consecutive_failures": account_state.get("consecutive_failures", 0),
        "has_refresh_token": bool(refresh_token_status.get("has_refresh_token")),
        "refresh_token_loaded": bool(refresh_token_status.get("has_refresh_token")),
        "reason": reason,
        "account_id": account_state.get("account_id"),
        "active_account_id": (
            account_state.get("active_account_id")
            or selection_debug.get("active_account_id")
        ),
        "authorized_account_ids": (
            account_state.get("authorized_account_ids")
            or selection_debug.get("authorized_account_ids")
            or []
        ),
        "selected_account_source": (
            account_state.get("selected_account_source")
            or selection_debug.get("selected_account_source")
        ),
        "is_active_account_authorized": bool(
            account_state.get("is_active_account_authorized")
            or selection_debug.get("is_active_account_authorized")
        ),
        "live_positions_count": positions_count,
        "status_age_seconds": account_state.get("status_age_seconds"),
        "snapshot_source": account_state.get("snapshot_source"),
        "last_success": format_status_time(
            LIVE_POSITION_SYNC_STATUS.get("last_success")
        ),
        "last_error": last_error,
    }

@app.get("/market-data-source")
def market_data_source():
    from brain import get_market_data_source_status

    return get_market_data_source_status()

@app.get("/live-prices")
def live_prices():
    start_ctrader_live_price_stream()
    return get_live_prices()

@app.get("/auto-trade-status")
def auto_trade_status():
    return {
        **AUTO_TRADE_LAST_STATUS,
        "live_auto_status_by_symbol": LIVE_AUTO_STATUS_BY_SYMBOL,
        "auto_trade": auto_trade_state_response(),
    }

@app.post("/market-data-source")
def set_market_data_source_endpoint(payload: dict):
    from brain import get_panel_data, set_market_data_source

    result = set_market_data_source(payload.get("source"))

    if not result.get("ok"):
        return result

    print("MARKET DATA SOURCE SWITCHED TO:", result.get("source"))

    try:
        data = get_panel_data(force_refresh=True)

        if isinstance(data, dict) and "EURUSD" in data and "XAUUSD" in data:
            PANEL_CACHE["data"] = data
            PANEL_CACHE["last_update"] = time.time()
            result["panel_refreshed"] = True
        else:
            result["panel_refreshed"] = False
            result["refresh_error"] = "Forced refresh returned invalid panel data"

    except Exception as e:
        result["panel_refreshed"] = False
        result["refresh_error"] = str(e)
        print("MARKET DATA SOURCE FORCE REFRESH ERROR:", e)

    return result

@app.get("/panel-data")
def panel_data(force: int = 0):
    age = time.time() - PANEL_CACHE["last_update"]

    cached_data = PANEL_CACHE.get("data")

    if not isinstance(cached_data, dict):
        cached_data = default_panel()

    force_requested = str(force).lower() in ["1", "true", "yes"]
    cache_stale = age >= CACHE_SECONDS
    startup_cache = _is_startup_panel_payload(cached_data)
    cache_validity = _panel_cache_validity(cached_data)
    cache_valid = cache_validity["valid"]
    running_since = PANEL_REFRESH_STATE.get("last_started")
    running_seconds = (
        time.time() - float(running_since)
        if PANEL_REFRESH_STATE.get("running") and running_since
        else 0
    )
    refresh_stuck = running_seconds >= PANEL_REFRESH_STUCK_SECONDS
    direct_reason = None

    if force_requested:
        direct_reason = "panel_force"
    elif not cache_valid:
        direct_reason = "api_cache_invalid"
    elif cache_stale:
        direct_reason = (
            "api_cache_stale_startup"
            if startup_cache
            else "api_cache_stale_direct"
        )

    if force_requested:
        fresh_data = refresh_panel_cache_direct(
            direct_reason,
            force_refresh=True,
        )
        if fresh_data is not None:
            cached_data = fresh_data
            age = 0
            fresh_validity = _panel_cache_validity(fresh_data)
            cache_valid = fresh_validity["valid"]
            cache_validity = fresh_validity
        else:
            print("PANEL_CACHE_RETURN_STALE =", {
                "reason": direct_reason,
                "cache_age_seconds": round(age, 1),
                "startup_cache": startup_cache,
                "refresh_running": PANEL_REFRESH_STATE.get("running"),
                "refresh_running_seconds": round(running_seconds, 1),
                "refresh_stuck": refresh_stuck,
                "error": PANEL_REFRESH_STATE.get("last_error"),
            })
    elif direct_reason:
        if not cache_valid:
            schedule_panel_cache_refresh(
                direct_reason,
                force_refresh=False,
            )
            recovered_data = wait_for_initial_panel_cache()
            if recovered_data is not None:
                cached_data = recovered_data
                age = max(
                    time.time() - float(PANEL_CACHE.get("last_update") or 0),
                    0,
                )
                cache_validity = _panel_cache_validity(cached_data)
                cache_valid = cache_validity["valid"]
                startup_cache = _is_startup_panel_payload(cached_data)
                direct_reason = None
                print("PANEL_INITIAL_DATA_READY =", {
                    "cache_age_seconds": round(age, 1),
                    "candle_counts": cache_validity.get("candle_counts"),
                    "refresh_source": PANEL_REFRESH_STATE.get("last_source"),
                })
        elif cache_stale:
            schedule_panel_cache_refresh(
                direct_reason,
                force_refresh=True,
            )
        print("PANEL_CACHE_RETURN_IMMEDIATE =", {
            "reason": direct_reason,
            "cache_age_seconds": round(age, 1),
            "startup_cache": startup_cache,
            "cache_valid": cache_valid,
            "cache_validation_error": cache_validity.get("reason"),
            "refresh_running": PANEL_REFRESH_STATE.get("running"),
            "refresh_running_seconds": round(running_seconds, 1),
            "refresh_stuck": refresh_stuck,
        })

    data = copy.deepcopy(cached_data)
    data = apply_trade_signal_lifecycle(data)
    current_live_price_status = get_live_prices()
    data = overlay_live_forming_candles(
        data,
        current_live_price_status,
    )

    live_positions = LIVE_PANEL_META_CACHE.get("live_positions") or []
    live_price_status = current_live_price_status or (
        LIVE_PANEL_META_CACHE.get("live_price_status") or {}
    )
    paper_trades = data.get("paper_trades") if isinstance(data, dict) else {}
    paper_history = data.get("paper_trade_history") if isinstance(data, dict) else []
    paper_stats = data.get("paper_trade_stats") if isinstance(data, dict) else {}
    live_pl_sync = dict(LIVE_PANEL_META_CACHE.get("live_pl_sync") or {})
    expected_weekly_reset = get_live_week_start_ts()
    expected_daily_reset = get_live_daily_reset_ts()
    expected_month_start = get_live_month_start_ts()

    if live_pl_sync.get("weekly_reset_ts") != expected_weekly_reset:
        if live_pl_sync.get("weekly_realized_pl"):
            print("stale weekly P/L cache ignored")
        live_pl_sync["weekly_realized_pl"] = 0
        live_pl_sync["weekly_total_pl"] = live_pl_sync.get("floating_live_pl", 0)
        live_pl_sync["weekly_reset_ts"] = expected_weekly_reset

    if live_pl_sync.get("daily_reset_ts") != expected_daily_reset:
        if live_pl_sync.get("daily_realized_pl"):
            print("stale daily P/L cache ignored")
        live_pl_sync["daily_realized_pl"] = 0
        live_pl_sync["daily_reset_ts"] = expected_daily_reset

    if live_pl_sync.get("monthly_start_ts") != expected_month_start:
        live_pl_sync["monthly_realized_pl"] = 0
        live_pl_sync["monthly_start_ts"] = expected_month_start

    live_recent_history = LIVE_PANEL_META_CACHE.get("live_recent_history") or []

    if not live_pl_sync.get("pl_calculation_version"):
        live_pl_sync = calculate_live_pl_sync()
        live_recent_history = get_live_recent_history_for_panel()
        LIVE_PANEL_META_CACHE.update({
            "live_pl_sync": live_pl_sync or {},
            "live_recent_history": live_recent_history or [],
            "live_trade_stats": calculate_live_trade_stats() or {},
            "last_update": time.time(),
        })

    print("LIVE_PL_SYNC:", live_pl_sync)

    authoritative_auto_trade_state = auto_trade_state_response(refresh=True)

    print("PAPER_STATE_REFRESH:", {
        "paper_auto_enabled": AUTO_TRADE_ENABLED["enabled"],
        "active_symbols": [
            symbol
            for symbol, trade in (paper_trades or {}).items()
            if trade
        ],
        "history_count": len(paper_history or []),
        "stats": paper_stats or {}
    })
    print("AUTO_PANEL_STATE_REFRESH:", {
        "paper_auto_enabled": AUTO_TRADE_ENABLED["enabled"],
        "live_auto_enabled": LIVE_AUTO_TRADE_ENABLED["enabled"],
        "live_broker_connected": LIVE_ACCOUNT_STATE.get("connected"),
        "live_positions_count": len(live_positions or []),
        "live_active_symbols": [
            symbol
            for symbol, trade in LIVE_ACTIVE_ORDERS.items()
            if trade
        ],
    })

    data["weekly_realized_pl"] = live_pl_sync.get("weekly_realized_pl", 0)
    data["daily_realized_pl"] = live_pl_sync.get("daily_realized_pl", 0)
    data["daily_total_pl"] = live_pl_sync.get("daily_total_pl", 0)
    data["monthly_realized_pl"] = live_pl_sync.get("monthly_realized_pl", 0)
    data["floating_live_pl"] = live_pl_sync.get("floating_live_pl", 0)
    data["weekly_total_pl"] = live_pl_sync.get("weekly_total_pl", 0)

    data["_meta"] = {
        "source": (
            "direct_fresh"
            if direct_reason and age == 0 and cache_valid
            else "direct_fresh_no_data"
            if direct_reason and age == 0 and not cache_valid
            else "shared_cache_refreshing"
            if (
                age >= CACHE_SECONDS
                and PANEL_REFRESH_STATE.get("running")
                and not refresh_stuck
            )
            else "shared_cache"
            if cache_valid
            else "no_valid_cache"
        ),
        "cache_age_seconds": round(age, 1),
        "stale_data": bool(
            PANEL_REFRESH_STATE.get("last_error")
            or not cache_valid
        ),
        "last_successful_refresh": PANEL_REFRESH_STATE.get("last_success"),
        "backend_decision_timestamps": {
            symbol: (
                ((data.get(symbol) or {}).get("strategy_cycle") or {}).get(
                    "evaluation_time"
                )
            )
            for symbol in ["EURUSD", "XAUUSD"]
        },
        "evaluated_m15_candles": {
            symbol: (
                ((data.get(symbol) or {}).get("strategy_cycle") or {}).get(
                    "evaluated_m15_candle"
                )
            )
            for symbol in ["EURUSD", "XAUUSD"]
        },
        "refresh_seconds": CACHE_SECONDS,
        "error": PANEL_REFRESH_STATE.get("last_error"),
        "cache_valid": cache_valid,
        "cache_validation_error": cache_validity.get("reason"),
        "candle_counts": cache_validity.get("candle_counts"),
        "brain_refresh": dict(PANEL_REFRESH_STATE),
        "live_meta_last_update": LIVE_PANEL_META_CACHE.get("last_update"),
        "live_meta_error": LIVE_PANEL_META_CACHE.get("last_error"),

        "paper_auto_enabled":
            authoritative_auto_trade_state["paper_enabled"],

        "live_auto_enabled":
            authoritative_auto_trade_state["live_enabled"],

        "auto_trade_state":
            authoritative_auto_trade_state,

        "live_account":
            LIVE_ACCOUNT_STATE,

        "live_active_orders":
            LIVE_ACTIVE_ORDERS,

        "broker_open_positions_count":
            len(live_positions or []),

        "live_trade_history":
            live_recent_history,

        "live_trade_stats":
            {
                **(LIVE_PANEL_META_CACHE.get("live_trade_stats") or {}),
                "pl_calculation_version":
                    live_pl_sync.get("pl_calculation_version"),
                "daily_realized_pl":
                    live_pl_sync.get("daily_realized_pl", 0),
                "daily_total_pl":
                    live_pl_sync.get("daily_total_pl", 0),
                "weekly_realized_pl":
                    live_pl_sync.get("weekly_realized_pl", 0),
                "monthly_realized_pl":
                    live_pl_sync.get("monthly_realized_pl", 0),
                "floating_live_pl":
                    live_pl_sync.get("floating_live_pl", 0),
                "weekly_total_pl":
                    live_pl_sync.get("weekly_total_pl", 0),
            },

        "weekly_realized_pl":
            live_pl_sync.get("weekly_realized_pl", 0),

        "daily_realized_pl":
            live_pl_sync.get("daily_realized_pl", 0),

        "daily_total_pl":
            live_pl_sync.get("daily_total_pl", 0),

        "monthly_realized_pl":
            live_pl_sync.get("monthly_realized_pl", 0),

        "floating_live_pl":
            live_pl_sync.get("floating_live_pl", 0),

        "weekly_total_pl":
            live_pl_sync.get("weekly_total_pl", 0),

        "pl_calculation_version":
            live_pl_sync.get("pl_calculation_version"),

        "auto_trade_status":
            AUTO_TRADE_LAST_STATUS,

        "live_auto_status_by_symbol":
            LIVE_AUTO_STATUS_BY_SYMBOL,

        "live_prices":
            live_price_status.get("live_prices", {}),

        "live_price_health":
            live_price_status.get("live_price_health"),

        "live_price_last_update":
            live_price_status.get("live_price_last_update"),

        "last_execution_time":
            LIVE_PANEL_META_CACHE.get("last_execution_time", 0)
    }

    return _json_safe_panel_value(data)


@app.get("/brain-status")
def brain_status():
    return {
        "ok": PANEL_REFRESH_STATE.get("last_error") is None,
        "cache_ready": bool(PANEL_CACHE.get("last_update")),
        "cache_age_seconds": (
            round(time.time() - PANEL_CACHE["last_update"], 1)
            if PANEL_CACHE.get("last_update")
            else None
        ),
        **PANEL_REFRESH_STATE,
    }
@app.post("/feedback")
def send_feedback(request: FeedbackRequest):
    try:
        msg_body = f"""
NathauxFX Feedback

User: {request.user or "anonymous"}
Time: {request.time or "unknown"}

Message:
{request.message}
""".strip()

        msg = MIMEText(msg_body)
        msg["Subject"] = "NathauxFX Feedback"
        msg["From"] = FEEDBACK_EMAIL
        msg["To"] = FEEDBACK_EMAIL

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(FEEDBACK_EMAIL, FEEDBACK_APP_PASSWORD)
            server.send_message(msg)

        print("✅ Feedback email sent")
        return {"status": "sent"}

    except Exception as e:
        print("❌ Feedback error:", e)
        return {"status": "error", "message": str(e)}
    
@app.post("/signup")
def signup(request: SignupRequest):
    users = load_users()
    email = request.email.strip().lower()

    if not email or not request.password.strip():
        return {"ok": False, "message": "Email and password required"}

    if email in users:
        return {"ok": False, "message": "Account already exists"}

    role = "user"

    if email == "flowsignal.contact@gmail.com":
        role = "admin"

    users[email] = {
        "password": hash_password(request.password),
        "role": role
    }
    save_users(users)

    return {"ok": True, "message": "Account created"}

@app.post("/login")
def login(request: LoginRequest):
    users = load_users()
    email = request.email.strip().lower()
    hashed = hash_password(request.password)
    
    if email == "flowsignal.contact@gmail.com" and request.password == "@Renathaux509.":
        token = str(uuid.uuid4())
        SESSIONS[token] = {
            "email": email,
            "role": "admin"
        }

        return {
            "ok": True,
            "message": "Admin login success",
            "token": token,
            "email": email,
            "role": "admin"
        }

    if email not in users:
        return {"ok": False, "message": "Account not found"}

    if users[email]["password"] != hashed:
        return {"ok": False, "message": "Wrong password"}

    role = "admin" if email == "flowsignal.contact@gmail.com" else users[email].get("role", "user")

    token = str(uuid.uuid4())
    SESSIONS[token] = {
        "email": email,
        "role": role
    }

    return {
        "ok": True,
        "message": "Login success",
        "token": token,
        "email": email,
        "role": role
    }


@app.post("/session/access-code")
def create_access_code_session(request: AccessCodeSessionRequest):
    """Turn the app's existing access grant into a backend session.

    This does not add a second login. It gives the existing NathauxFX access
    path the same server-side session needed by authenticated settings APIs.
    """
    expected_code = str(os.getenv("FLOWSIGNAL_ACCESS_CODE", "FLOWTEST"))
    supplied_code = str(request.code or "").strip()
    if not supplied_code or not hmac.compare_digest(supplied_code, expected_code):
        raise HTTPException(status_code=401, detail="Invalid NathauxFX access code.")

    token = str(uuid.uuid4())
    SESSIONS[token] = {
        "email": "flowsignal-access-user",
        "role": "user",
        "auth_method": "access_code",
    }
    return {
        "ok": True,
        "token": token,
        "role": "user",
        "authenticated": True,
    }


def _authenticated_settings_user(request: Request):
    authorization = str(request.headers.get("authorization") or "").strip()
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    token = token or str(request.headers.get("x-session-token") or "").strip()
    session = SESSIONS.get(token)
    if not session or session.get("role") not in {"user", "admin", "owner"}:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


def _news_mode_account_context():
    try:
        account = sync_ctrader_account_state(force=False)
    except Exception as exc:
        print("NEWS_MODE_ACCOUNT_SYNC_WARNING =", str(exc))
        account = LIVE_ACCOUNT_STATE
    environment = str(account.get("mode") or "unknown").strip().lower()
    account_id = account.get("active_account_id") or account.get("account_id")
    return account_id, environment


def _allow_live_news_trading():
    return str(os.getenv("ALLOW_LIVE_NEWS_TRADING", "false")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def _strategy_settings_user_id(request):
    user = _authenticated_settings_user(request)
    return user.get("email") or user.get("id") or "authenticated_user"


@app.get("/strategy/settings")
def get_strategy_settings_endpoint(request: Request):
    _strategy_settings_user_id(request)
    return get_strategy_settings()


@app.put("/strategy/settings")
def put_strategy_settings_endpoint(
    payload: StrategySettingsUpdateRequest,
    request: Request,
):
    user_id = _strategy_settings_user_id(request)
    try:
        return save_strategy_settings(payload.settings, updated_by=user_id)
    except StrategySettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategy/settings/reset")
def reset_strategy_settings_endpoint(
    payload: StrategySettingsResetRequest,
    request: Request,
):
    user_id = _strategy_settings_user_id(request)
    try:
        return reset_strategy_settings(
            confirmed=payload.confirm,
            updated_by=user_id,
        )
    except StrategySettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/strategy/settings/history")
def get_strategy_settings_history_endpoint(
    request: Request,
    limit: int = 50,
    offset: int = 0,
):
    _strategy_settings_user_id(request)
    try:
        return get_strategy_settings_history(limit=limit, offset=offset)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid history pagination.") from exc


def _news_mode_response(setting):
    account_id, environment = _news_mode_account_context()
    return {
        **setting,
        "active_account": account_id,
        "broker_environment": environment,
        "allow_live_news_trading": _allow_live_news_trading(),
    }


@app.get("/settings/news-trading-mode")
def get_news_trading_mode_setting(request: Request):
    _authenticated_settings_user(request)
    return _news_mode_response(get_effective_news_mode())


@app.put("/settings/news-trading-mode")
def update_news_trading_mode_setting(payload: NewsTradingModeRequest, request: Request):
    with NEWS_MODE_CHANGE_LOCK:
        return _apply_news_trading_mode_update(payload, request)


def _apply_news_trading_mode_update(payload: NewsTradingModeRequest, request: Request):
    current = get_effective_news_mode()
    account_id, environment = _news_mode_account_context()
    request_source = str(request.headers.get("x-request-source") or "web_app")
    try:
        user = _authenticated_settings_user(request)
    except HTTPException:
        try:
            record_news_mode_audit(
                previous_mode=current["mode"], new_mode=payload.mode,
                user_id="unauthenticated", active_broker_account=account_id,
                broker_environment=environment, request_source=request_source,
                success=False, failure_reason="Authentication required.",
            )
        except Exception as audit_exc:
            print("NEWS_MODE_AUDIT_FAILURE =", str(audit_exc))
        raise

    user_id = user.get("email") or user.get("id") or "authenticated_user"
    failure_reason = None
    try:
        requested_mode = normalize_news_mode(payload.mode)
        if (
            requested_mode == "TRADE_CONFIRMED"
            and environment in {"live", "funded", "real"}
            and not _allow_live_news_trading()
        ):
            failure_reason = (
                "Confirmed news trading is disabled for live accounts. "
                "Use Demo or enable the server safety override."
            )
            raise HTTPException(status_code=403, detail=failure_reason)

        transition = news_trading.apply_mode_transition(
            current["mode"], requested_mode
        )
        saved = save_news_mode(requested_mode, updated_by=user_id)
        try:
            record_news_mode_audit(
                previous_mode=current["mode"], new_mode=saved["mode"],
                user_id=user_id, active_broker_account=account_id,
                broker_environment=environment, request_source=request_source,
                success=True,
            )
        except Exception as audit_exc:
            # The setting is already durable and active. Keep the API truthful;
            # the structured application log remains a secondary audit trail.
            print("NEWS_MODE_AUDIT_FAILURE =", str(audit_exc))
        print("NEWS_TRADING_MODE_APPLIED =", {
            "previous_mode": current["mode"],
            "new_mode": saved["mode"],
            "user": user_id,
            "active_broker_account": account_id,
            "broker_environment": environment,
            "transition": transition,
        })
        return _news_mode_response(saved)
    except InvalidNewsTradingMode as exc:
        failure_reason = str(exc)
        status_code = 422
    except HTTPException as exc:
        failure_reason = str(exc.detail)
        status_code = exc.status_code
    except Exception as exc:
        failure_reason = f"Could not save news trading mode: {exc}"
        status_code = 500

    try:
        record_news_mode_audit(
            previous_mode=current["mode"], new_mode=payload.mode,
            user_id=user_id, active_broker_account=account_id,
            broker_environment=environment, request_source=request_source,
            success=False, failure_reason=failure_reason,
        )
    except Exception as audit_exc:
        print("NEWS_MODE_AUDIT_FAILURE =", str(audit_exc))
    raise HTTPException(status_code=status_code, detail=failure_reason)

@app.post("/track-visit")
def track_visit(data: dict, request: Request):
    try:
        visits = load_visits()

        visitor_id = data.get("visitor_id")
        
        ip = request.headers.get("x-forwarded-for", request.client.host)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        country = "Unknown"

        try:
            if ip not in ["127.0.0.1", "localhost"]:
                geo = requests.get(f"http://ip-api.com/json/{ip}", timeout=2).json()
                country = geo.get("country", "Unknown")
            else:
                country = "Local"
        except Exception:
            country = "Unknown"

        if not visitor_id:
            visitor_id = str(uuid.uuid4())

        visit_data = {
            "time": time.time(),
            "visitor_id": visitor_id,
            "country": country
        }

        visits.append(visit_data)

        thirty_days_ago = time.time() - (30 * 86400)

        visits = [
            v for v in visits
            if v.get("time", 0) >= thirty_days_ago
        ]

        save_visits(visits)

        unique_visitors = len(
            set(v.get("visitor_id") for v in visits if v.get("visitor_id"))
        )

        return {
            "ok": True,
            "visitor_id": visitor_id,
            "total_visits": len(visits),
            "unique_visitors": unique_visitors
        }

    except Exception as e:
        print("TRACK VISIT ERROR:", e)

        return {
            "ok": False,
            "message": str(e)
        }
@app.get("/admin-stats")
def admin_stats():
    visits = load_visits()

    total_visits = len(visits)

    unique_visitors = len(
        set(v.get("visitor_id") for v in visits if v.get("visitor_id"))
    )

    today_start = time.time() - 86400

    today_visits = len([
        v for v in visits
        if v.get("time", 0) >= today_start
    ])

    last_visit = None
    if visits:
        last_visit = max(v["time"] for v in visits)

    countries = list(
    set(
        v.get("country")
        for v in visits
        if v.get("country")
    )
    )

    return {
        "total_visits": total_visits,
        "unique_visitors": unique_visitors,
        "today_visits": today_visits,
        "last_visit": last_visit,
        "countries": countries
    }

@app.post("/execute-trade")
def execute_trade(request: TradeRequest):
    try:
        if request.token != ADMIN_TOKEN:
            print("❌ UNAUTHORIZED TRADE ATTEMPT")
            return {
                "ok": False,
                "message": "Unauthorized"
            }

        print(f"TRADE REQUEST RECEIVED -> {request.symbol} {request.action}")
        return {
            "ok": True,
            "message": f"Trade request received for {request.symbol} {request.action}",
            "symbol": request.symbol,
            "action": request.action
        }
    except Exception as e:
        print("TRADE ERROR:", e)
        return {
            "ok": False,
            "message": str(e)
        }


AUTO_TRADE_ENABLED = {
    "enabled": False
}
LIVE_AUTO_TRADE_ENABLED = {
    "enabled": False
}
AUTO_TRADE_STATE_FILE = os.path.join(
    DATA_DIR,
    "auto_trade_state.json"
)

LAST_EXECUTION_TIME = 0
AUTO_TRADE_STATE_METADATA = {
    "updated_at": None,
    "updated_by": "system",
    "source": "default",
    "request_source": None,
    "reason": None,
    "persistence": {},
}
AUTO_TRADE_STATE_REFRESH_LOCK = threading.RLock()

def load_auto_trade_state():
    try:
        state = load_durable_auto_trade_state(AUTO_TRADE_STATE_FILE)
        AUTO_TRADE_ENABLED["enabled"] = bool(state.get("paper_enabled"))
        LIVE_AUTO_TRADE_ENABLED["enabled"] = bool(state.get("live_enabled"))
        AUTO_TRADE_STATE_METADATA.update({
            key: state.get(key)
            for key in AUTO_TRADE_STATE_METADATA
        })
        print("AUTO TRADE STATE LOADED:", state)
    except Exception as e:
        print("AUTO TRADE STATE LOAD ERROR:", e)


def refresh_auto_trade_state_from_persistence(reason="runtime_refresh"):
    """Synchronize every worker with the backend-authoritative preference."""
    with AUTO_TRADE_STATE_REFRESH_LOCK:
        try:
            state = load_durable_auto_trade_state(AUTO_TRADE_STATE_FILE)
            AUTO_TRADE_ENABLED["enabled"] = bool(state.get("paper_enabled"))
            LIVE_AUTO_TRADE_ENABLED["enabled"] = bool(state.get("live_enabled"))
            AUTO_TRADE_STATE_METADATA.update({
                key: state.get(key)
                for key in AUTO_TRADE_STATE_METADATA
            })
            print("AUTO_TRADE_STATE_REFRESHED =", {
                "reason": reason,
                "paper_enabled": AUTO_TRADE_ENABLED["enabled"],
                "live_enabled": LIVE_AUTO_TRADE_ENABLED["enabled"],
                "updated_at": state.get("updated_at"),
                "source": state.get("source"),
            })
            return state
        except Exception as exc:
            # A transient database failure must not silently force either mode
            # OFF. Keep the last confirmed in-memory preference.
            print("AUTO_TRADE_STATE_REFRESH_ERROR =", {
                "reason": reason,
                "error": str(exc),
                "paper_enabled_kept": AUTO_TRADE_ENABLED["enabled"],
                "live_enabled_kept": LIVE_AUTO_TRADE_ENABLED["enabled"],
            })
            return {
                "paper_enabled": AUTO_TRADE_ENABLED["enabled"],
                "live_enabled": LIVE_AUTO_TRADE_ENABLED["enabled"],
                **AUTO_TRADE_STATE_METADATA,
                "refresh_error": str(exc),
            }

def save_auto_trade_state(
    updated_by="system",
    request_source="backend",
    reason=None,
    changed_mode=None,
):
    try:
        common = {
            "updated_by": updated_by,
            "request_source": request_source,
            "reason": reason,
            "active_broker_account": LIVE_ACCOUNT_STATE.get("account_id")
            if "LIVE_ACCOUNT_STATE" in globals() else None,
            "broker_environment": LIVE_ACCOUNT_STATE.get("mode")
            if "LIVE_ACCOUNT_STATE" in globals() else None,
        }
        if changed_mode:
            normalized_mode = str(changed_mode).strip().lower()
            enabled = (
                AUTO_TRADE_ENABLED["enabled"]
                if normalized_mode == "paper"
                else LIVE_AUTO_TRADE_ENABLED["enabled"]
            )
            state = save_durable_auto_trade_mode(
                mode=normalized_mode,
                enabled=enabled,
                **common,
            )
        else:
            state = save_durable_auto_trade_state(
                paper_enabled=AUTO_TRADE_ENABLED["enabled"],
                live_enabled=LIVE_AUTO_TRADE_ENABLED["enabled"],
                **common,
            )
        AUTO_TRADE_ENABLED["enabled"] = bool(state.get("paper_enabled"))
        LIVE_AUTO_TRADE_ENABLED["enabled"] = bool(state.get("live_enabled"))
        AUTO_TRADE_STATE_METADATA.update({
            key: state.get(key)
            for key in AUTO_TRADE_STATE_METADATA
        })
        print("AUTO_TRADE_STATE_PERSISTED =", state)
        return state
    except Exception as e:
        print("AUTO TRADE STATE SAVE ERROR:", e)
        raise

def get_last_execution_time():
    live_times = [
        timestamp
        for timestamp in LIVE_LAST_EXECUTION_TIME.values()
        if timestamp
    ] if "LIVE_LAST_EXECUTION_TIME" in globals() else []

    return max([LAST_EXECUTION_TIME, *live_times] or [0])

@app.post("/paper-auto-toggle")
def paper_auto_toggle(payload: dict):

    enabled = bool(
        payload.get("enabled", False)
    )

    previous_enabled = AUTO_TRADE_ENABLED["enabled"]
    AUTO_TRADE_ENABLED["enabled"] = enabled
    try:
        state = save_auto_trade_state(
            updated_by="web_user",
            request_source="paper_auto_toggle",
            reason="User changed Paper Auto",
            changed_mode="paper",
        )
    except Exception as exc:
        AUTO_TRADE_ENABLED["enabled"] = previous_enabled
        raise HTTPException(
            status_code=503,
            detail=f"Could not persist Paper Auto state: {exc}",
        ) from exc

    print(
        "AUTO TRADE STATE:",
        AUTO_TRADE_ENABLED["enabled"]
    )

    return {
        "status": "ok",
        "enabled": AUTO_TRADE_ENABLED["enabled"],
        "state": state,
    }

load_auto_trade_state()

LIVE_ACCOUNT_STATE = {
    "connected": False,
    "mode": "demo",   # demo/live
    "broker": "ctrader",
    "account_id": None,
    "execution_ready": False
}

LIVE_ACTIVE_ORDERS = {
    "EURUSD": None,
    "XAUUSD": None
}

LIVE_TRADE_HISTORY = []
LIVE_BROKER_CLOSED_HISTORY = []
LIVE_BROKER_HISTORY_CACHE = {
    "updated_at": 0,
    "history": [],
}
LIVE_RESET_KEY = "last_live_reset"
LIVE_MARKET_TIMEZONE = ZoneInfo("America/New_York")
load_live_monthly_history_cache()
LAST_LIVE_RESET = 0
LIVE_ORDER_LOCK = threading.RLock()
LIVE_ORDER_IN_FLIGHT = set()
MAX_LIVE_TRADE_HISTORY = 50
LIVE_EXECUTION_COOLDOWN_SECONDS = 30
BROKER_SYNC_GRACE_SECONDS = 10
LIVE_RISK_PERCENT = 1.0
MAX_LIVE_RISK_PERCENT = 1.0
RISK_TOLERANCE_PERCENT = 0.0
MIN_LOT = 0.01
LIVE_LAST_EXECUTION_TIME = {
    "EURUSD": 0,
    "XAUUSD": 0
}
LIVE_LAST_POSITION_CLOSED_AT = {
    "EURUSD": 0.0,
    "XAUUSD": 0.0,
}


def get_live_post_close_cooldown_seconds():
    """Shared configured duration; defaults safely to the production 15 minutes."""
    return get_configured_cooldown_seconds()


def invalidate_symbol_setup_state(
    symbol,
    closed_at,
    reason="position closed",
    panel_data=None,
):
    normalized_symbol = normalize_symbol(symbol)
    try:
        closed_timestamp = float(closed_at or time.time())
    except (TypeError, ValueError):
        closed_timestamp = time.time()

    LIVE_LAST_POSITION_CLOSED_AT[normalized_symbol] = max(
        float(LIVE_LAST_POSITION_CLOSED_AT.get(normalized_symbol, 0) or 0),
        closed_timestamp,
    )
    try:
        news_trading.mark_running_trade_closed(
            normalized_symbol,
            closed_at=closed_timestamp,
        )
    except Exception as exc:
        print("NEWS_TRADE_CLOSE_STATE_ERROR =", {
            "symbol": normalized_symbol,
            "error": str(exc),
        })

    try:
        import brain

        brain.clear_symbol_entry_memory(
            normalized_symbol,
            reason,
            closed_at=closed_timestamp,
        )
    except Exception as exc:
        print("SYMBOL_SETUP_MEMORY_INVALIDATION_ERROR =", {
            "symbol": normalized_symbol,
            "closed_at": closed_timestamp,
            "reason": reason,
            "error": str(exc),
        })

    plan_roots = [PANEL_CACHE.get("data")]
    if (
        isinstance(panel_data, dict)
        and all(panel_data is not root for root in plan_roots)
    ):
        plan_roots.append(panel_data)
    for plan_root in plan_roots:
        plan = (
            plan_root.get(normalized_symbol)
            if isinstance(plan_root, dict)
            else None
        )
        if not isinstance(plan, dict):
            continue
        plan.update({
            "signal": "WAIT",
            "final_signal": "WAIT",
            "signal_before_filters": "WAIT",
            "signal_after_filters": "WAIT",
            "signal_display_state": "WAIT",
            "fresh_entry_available": False,
            "strategy_setup_complete": False,
            "fifteen_m_setup": "WAIT",
            "confirmation_5m": "WAIT",
            "confirmation_5m_raw": "WAIT",
            "current_5m_entry_confirmation": False,
            "blocked_by": "position_closed_setup_invalidated",
            "blocked_reason": "Position closed; waiting for a fresh post-close 15m BOS",
            "blocker_rule_name": "post_close_setup_invalidation",
            "previous_position_closed_at": datetime.fromtimestamp(
                closed_timestamp,
                tz=timezone.utc,
            ).isoformat(),
        })

    SIGNAL_EMAIL_LAST_SIGNAL[normalized_symbol] = "WAIT"
    print("POST_CLOSE_SETUP_INVALIDATED =", {
        "symbol": normalized_symbol,
        "closed_at": closed_timestamp,
        "reason": reason,
        "cooldown_seconds": get_live_post_close_cooldown_seconds(),
    })
    return closed_timestamp

def get_configured_live_risk_percent():
    try:
        configured = float(
            load_risk_settings().get("riskPerTradePct", LIVE_RISK_PERCENT)
        )
    except (TypeError, ValueError):
        configured = LIVE_RISK_PERCENT

    return max(0.05, min(1.0, configured))

def get_maximum_allowed_live_risk_percent(risk_percent=None):
    target = (
        get_configured_live_risk_percent()
        if risk_percent is None
        else float(risk_percent)
    )
    return round(target, 4)
AUTO_TRADE_LAST_STATUS = {
    "symbol": None,
    "signal": None,
    "action": None,
    "status": "WAIT",
    "reason": "Waiting for BUY/SELL signal",
    "timestamp": None,
}
LIVE_AUTO_STATUS_BY_SYMBOL = {
    "EURUSD": {
        "symbol": "EURUSD",
        "signal": None,
        "action": None,
        "status": "WAIT",
        "reason": "Waiting for BUY/SELL signal",
        "checked_at": None,
        "active_trade": None,
    },
    "XAUUSD": {
        "symbol": "XAUUSD",
        "signal": None,
        "action": None,
        "status": "WAIT",
        "reason": "Waiting for BUY/SELL signal",
        "checked_at": None,
        "active_trade": None,
    },
}
LIVE_BACKUP_FILE = os.path.join(
    DATA_DIR,
    "live_backup.json"
)

def normalize_live_auto_status(status):
    state = str(status or "WAIT").upper()

    if state == "WAITING":
        return "WAIT"
    if state == "ORDER_SENT":
        return "EXECUTED"
    if state == "ORDER_REJECTED":
        return "BLOCKED"

    return state

def set_auto_trade_status(symbol=None, signal=None, action=None, status="WAITING", reason=None, details=None):
    timestamp = time.time()
    normalized_symbol = normalize_symbol(symbol) if symbol else None
    normalized_status = normalize_live_auto_status(status)
    ENGINE_RUNTIME_STATE["last_strategy_check"] = {
        "symbol": normalized_symbol,
        "signal": signal,
        "status": normalized_status,
        "reason": reason,
        "timestamp": timestamp,
    }
    if str(signal or "").upper() in ["BUY", "SELL"]:
        ENGINE_RUNTIME_STATE["last_valid_setup"] = {
            "symbol": normalized_symbol,
            "signal": str(signal).upper(),
            "timestamp": timestamp,
            "details": details,
        }

    AUTO_TRADE_LAST_STATUS.update({
        "symbol": normalized_symbol,
        "signal": signal,
        "action": action,
        "status": normalized_status,
        "reason": reason,
        "timestamp": timestamp,
        "details": details,
    })

    if normalized_symbol in LIVE_AUTO_STATUS_BY_SYMBOL:
        LIVE_AUTO_STATUS_BY_SYMBOL[normalized_symbol] = {
            "symbol": normalized_symbol,
            "signal": signal,
            "action": action,
            "status": normalized_status,
            "reason": reason,
            "details": details,
            "checked_at": timestamp,
            "active_trade": LIVE_ACTIVE_ORDERS.get(normalized_symbol),
        }
    elif not normalized_symbol:
        for status_symbol in LIVE_AUTO_STATUS_BY_SYMBOL:
            LIVE_AUTO_STATUS_BY_SYMBOL[status_symbol] = {
                **LIVE_AUTO_STATUS_BY_SYMBOL[status_symbol],
                "status": normalized_status,
                "reason": reason,
                "details": details,
                "checked_at": timestamp,
                "active_trade": LIVE_ACTIVE_ORDERS.get(status_symbol),
            }

    print("AUTO TRADE STATUS:", AUTO_TRADE_LAST_STATUS)
    print("LIVE AUTO STATUS BY SYMBOL:", LIVE_AUTO_STATUS_BY_SYMBOL)
    log_live_trade_audit(
        "auto_status",
        LIVE_ACTIVE_ORDERS.get(normalized_symbol) if normalized_symbol else {},
        symbol=normalized_symbol,
        reason=f"{normalized_status}: {reason}"
    )
    return AUTO_TRADE_LAST_STATUS

def get_persistable_live_active_orders():
    return {
        symbol: trade
        for symbol, trade in LIVE_ACTIVE_ORDERS.items()
        if trade and trade.get("source") == "broker"
    }

def get_live_trade_identity(trade):
    if not isinstance(trade, dict):
        return None

    return (
        trade.get("trade_id")
        or trade.get("broker_position_id")
        or trade.get("position_id")
        or trade.get("broker_order_id")
        or trade.get("order_id")
    )

def get_live_trade_match_key(trade):
    if not isinstance(trade, dict):
        return None

    broker_position_id = trade.get("broker_position_id") or trade.get("position_id")

    if broker_position_id:
        return f"position:{broker_position_id}"

    broker_order_id = trade.get("broker_order_id") or trade.get("order_id")

    if broker_order_id:
        return f"order:{broker_order_id}"

    trade_id = trade.get("trade_id")

    if trade_id:
        return f"trade:{trade_id}"

    return None

def ensure_live_trade_identity(trade, symbol=None):
    if not isinstance(trade, dict):
        return trade

    broker_position_id = (
        trade.get("broker_position_id")
        or trade.get("position_id")
    )
    trade_id = get_live_trade_identity(trade)

    if broker_position_id:
        trade_id = f"ctrader-pos-{broker_position_id}"
    elif not trade_id:
        trade_id = f"flowsignal-{normalize_symbol(symbol or trade.get('symbol'))}-{uuid.uuid4()}"

    trade["trade_id"] = str(trade_id)

    if broker_position_id:
        trade["broker_position_id"] = broker_position_id

    return trade

def enrich_broker_closed_trade_levels(trade):
    if not isinstance(trade, dict):
        return trade

    broker_trade = dict(trade)
    raw = broker_trade.get("raw") if isinstance(broker_trade.get("raw"), dict) else {}
    close_detail = (
        raw.get("closePositionDetail")
        if isinstance(raw.get("closePositionDetail"), dict)
        else {}
    )

    raw_tp1 = (
        broker_trade.get("tp1")
        or broker_trade.get("tp_price")
        or raw.get("tp1")
        or raw.get("takeProfit")
        or close_detail.get("tp1")
        or close_detail.get("takeProfit")
    )
    raw_tp2 = (
        broker_trade.get("tp2")
        or broker_trade.get("tp2_price")
        or raw.get("tp2")
        or close_detail.get("tp2")
    )

    if raw_tp1 is not None:
        broker_trade["tp1"] = raw_tp1

    if raw_tp2 is not None:
        broker_trade["tp2"] = raw_tp2

    broker_identifiers = get_trade_identifier_set(broker_trade)
    local_trades = [
        item
        for item in [
            *LIVE_ACTIVE_ORDERS.values(),
            *LIVE_TRADE_HISTORY,
        ]
        if isinstance(item, dict)
    ]
    local_trade = next(
        (
            item
            for item in local_trades
            if (
                broker_identifiers
                and broker_identifiers.intersection(
                    get_trade_identifier_set(item)
                )
            )
        ),
        None,
    )

    if not local_trade:
        return broker_trade

    merged = {
        **broker_trade,
        **local_trade,
    }
    preserved_fields = [
        "sl",
        "original_sl",
        "tp1",
        "tp2",
        "protected_sl_price",
        "hit_tp1",
        "profit_protected",
        "current_price",
        "current_high",
        "current_low",
    ]

    for field in preserved_fields:
        if local_trade.get(field) is not None:
            merged[field] = local_trade.get(field)
        elif broker_trade.get(field) is not None:
            merged[field] = broker_trade.get(field)

    broker_override_fields = [
        "status",
        "result",
        "pnl",
        "profit",
        "broker_pnl",
        "broker_realized_profit",
        "broker_realized_source",
        "broker_pnl_source",
        "closed_at",
        "close_price",
        "deal_id",
        "order_id",
        "broker_order_id",
        "position_id",
        "broker_position_id",
        "trade_id",
        "source",
        "history_source",
        "note",
        "raw",
    ]

    for field in broker_override_fields:
        if field in broker_trade:
            merged[field] = broker_trade.get(field)

    return merged

def get_live_broker_closed_history(force=False):
    run_weekly_live_reset()

    now = time.time()

    if (
        not force
        and LIVE_BROKER_HISTORY_CACHE.get("history")
        and now - LIVE_BROKER_HISTORY_CACHE.get("updated_at", 0) < 20
    ):
        return [
            enrich_broker_closed_trade_levels(trade)
            for trade in LIVE_BROKER_HISTORY_CACHE.get("history") or []
        ]

    try:
        broker_history = [
            enrich_broker_closed_trade_levels(trade)
            for trade in get_closed_deals_for_current_week(max_rows=100)
        ]
    except Exception as e:
        print("LIVE_BROKER_HISTORY_SYNC_ERROR:", e)
        broker_history = []

    LIVE_BROKER_CLOSED_HISTORY[:] = broker_history[:MAX_LIVE_TRADE_HISTORY]
    LIVE_BROKER_HISTORY_CACHE["history"] = list(LIVE_BROKER_CLOSED_HISTORY)
    LIVE_BROKER_HISTORY_CACHE["updated_at"] = now
    print("LIVE_BROKER_HISTORY_SYNC =", {
        "closed_trades": len(LIVE_BROKER_CLOSED_HISTORY),
        "realized_pl": round(sum(item.get("broker_realized_profit") or 0 for item in LIVE_BROKER_CLOSED_HISTORY), 2),
    })

    return list(LIVE_BROKER_CLOSED_HISTORY)

def get_live_broker_monthly_history(force=False):
    now = time.time()
    month_key = datetime.now(LIVE_MARKET_TIMEZONE).strftime("%Y-%m")

    if (
        not force
        and LIVE_MONTHLY_HISTORY_CACHE.get("month_key") == month_key
        and now - LIVE_MONTHLY_HISTORY_CACHE.get("updated_at", 0) < 60
    ):
        return list(LIVE_MONTHLY_HISTORY_CACHE.get("history") or [])

    previous_history = list(LIVE_MONTHLY_HISTORY_CACHE.get("history") or [])
    previous_month_key = LIVE_MONTHLY_HISTORY_CACHE.get("month_key")

    try:
        monthly_history = get_closed_deals_for_current_month(max_rows=500)
    except Exception as exc:
        print("LIVE_MONTHLY_HISTORY_SYNC_ERROR:", exc)
        return previous_history

    if (
        not monthly_history
        and previous_history
        and previous_month_key == month_key
    ):
        print("LIVE_MONTHLY_HISTORY_EMPTY_REFRESH_IGNORED =", {
            "month_key": month_key,
            "preserved_closed_trades": len(previous_history),
        })
        return previous_history

    LIVE_MONTHLY_HISTORY_CACHE["history"] = list(monthly_history or [])
    LIVE_MONTHLY_HISTORY_CACHE["updated_at"] = now
    LIVE_MONTHLY_HISTORY_CACHE["month_key"] = month_key
    save_live_monthly_history_cache()
    return list(LIVE_MONTHLY_HISTORY_CACHE["history"])

def get_live_recent_history_for_panel():
    run_weekly_live_reset()

    broker_history = get_live_broker_closed_history()

    if broker_history:
        return broker_history[:MAX_LIVE_TRADE_HISTORY]

    active_ids = {
        str(get_live_trade_match_key(trade))
        for trade in LIVE_ACTIVE_ORDERS.values()
        if trade and get_live_trade_match_key(trade)
    }
    cleaned = []

    for trade in LIVE_TRADE_HISTORY:
        if str(get_live_trade_match_key(trade)) in active_ids:
            continue

        if not is_usable_local_live_history_trade(trade):
            continue

        if not trade_is_current_week(trade):
            continue

        cleaned.append(trade)

    return cleaned[:MAX_LIVE_TRADE_HISTORY]

def is_usable_local_live_history_trade(trade):
    if not isinstance(trade, dict):
        return False

    status = get_live_trade_status(trade)

    if status in ["RUNNING", "OPEN", "CLOSING", "WAIT", "BLOCKED", "REJECTED", "NO_SIGNAL"]:
        return False

    broker_pl, broker_pl_source = extract_broker_realized_pl(trade)
    stored_pl = trade.get("pnl") if trade.get("pnl") is not None else trade.get("profit")

    try:
        stored_value = float(stored_pl)
    except (TypeError, ValueError):
        stored_value = 0

    if status in ["BROKER_CLOSED", "DISCONNECTED"] and broker_pl_source is None and abs(stored_value) > 500:
        print("LIVE_HISTORY_CORRUPT_ROW_IGNORED =", {
            "symbol": trade.get("symbol"),
            "position_id": trade.get("position_id") or trade.get("broker_position_id"),
            "status": status,
            "stored_profit": stored_value,
        })
        return False

    return True

def log_live_trade_audit(
    event,
    trade=None,
    symbol=None,
    reason=None,
    weekly_realized_pl=None,
    floating_live_pl=None,
    weekly_total_pl=None,
):
    trade = trade if isinstance(trade, dict) else {}
    trade_id = get_live_trade_identity(trade)

    print("LIVE_TRADE_AUDIT_DEBUG =", {
        "event": event,
        "symbol": normalize_symbol(symbol or trade.get("symbol")) if (symbol or trade.get("symbol")) else None,
        "trade_id": str(trade_id) if trade_id else None,
        "broker_position_id": trade.get("broker_position_id") or trade.get("position_id"),
        "side": trade.get("side") or trade.get("action"),
        "entry": trade.get("entry"),
        "current_price": trade.get("current_price") or trade.get("currentPrice"),
        "sl": trade.get("sl"),
        "tp1": trade.get("tp1"),
        "tp2": trade.get("tp2"),
        "status": trade.get("status"),
        "result": trade.get("result"),
        "pips": trade.get("pips"),
        "profit": (
            trade.get("broker_realized_profit")
            if trade.get("broker_realized_profit") is not None
            else trade.get("broker_pnl")
            if trade.get("broker_pnl") is not None
            else trade.get("pnl")
            if trade.get("pnl") is not None
            else trade.get("profit")
        ),
        "weekly_realized_pl": weekly_realized_pl,
        "floating_live_pl": floating_live_pl,
        "weekly_total_pl": weekly_total_pl,
        "source": trade.get("source"),
        "reason": reason or trade.get("note") or trade.get("exit_reason"),
    })

def save_live_backup():
    try:
        with open(LIVE_BACKUP_FILE, "w") as f:
            json.dump({
                "live_active_orders":
                    get_persistable_live_active_orders(),
                "live_trade_history":
                    LIVE_TRADE_HISTORY[:MAX_LIVE_TRADE_HISTORY],
                "live_last_execution_time":
                    LIVE_LAST_EXECUTION_TIME,
                "live_last_position_closed_at":
                    LIVE_LAST_POSITION_CLOSED_AT,
                LIVE_RESET_KEY:
                    LAST_LIVE_RESET,
            }, f, indent=2)
    except Exception as e:
        print("LIVE BACKUP SAVE ERROR:", e)


def persist_live_trade_state(trade):
    if not isinstance(trade, dict):
        return
    symbol = normalize_symbol(trade.get("symbol"))
    active = LIVE_ACTIVE_ORDERS.get(symbol)
    if isinstance(active, dict) and broker_position_matches_trade(active, trade):
        LIVE_ACTIVE_ORDERS[symbol] = copy.deepcopy(trade)
    save_live_backup()

def get_live_week_start_ts(now=None):
    if now is None:
        now = datetime.now(LIVE_MARKET_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=LIVE_MARKET_TIMEZONE)
    else:
        now = now.astimezone(LIVE_MARKET_TIMEZONE)

    reset = now.replace(hour=17, minute=0, second=0, microsecond=0)
    days_since_sunday = (now.weekday() - 6) % 7
    reset = reset - timedelta(days=days_since_sunday)

    if now.weekday() == 6 and now < reset:
        reset = reset - timedelta(days=7)

    return reset.timestamp()

def run_weekly_live_reset(force=False):
    global LAST_LIVE_RESET

    reset_ts = get_live_week_start_ts()

    if not force and reset_ts <= LAST_LIVE_RESET:
        return False

    active_ids = {
        str(get_live_trade_match_key(trade))
        for trade in LIVE_ACTIVE_ORDERS.values()
        if trade and get_live_trade_match_key(trade)
    }
    before_count = len(LIVE_TRADE_HISTORY)
    kept_history = []

    for trade in LIVE_TRADE_HISTORY:
        status = str(
            trade.get("status")
            or trade.get("result")
            or ""
        ).upper()
        match_key = str(get_live_trade_match_key(trade))

        if match_key in active_ids or status in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]:
            kept_history.append(trade)
            continue

        if trade_is_current_week(trade, reset_ts=reset_ts):
            kept_history.append(trade)

    LIVE_TRADE_HISTORY[:] = kept_history[:MAX_LIVE_TRADE_HISTORY]

    for symbol, timestamp in list(LIVE_LAST_EXECUTION_TIME.items()):
        try:
            if float(timestamp or 0) < reset_ts:
                LIVE_LAST_EXECUTION_TIME[symbol] = 0
        except (TypeError, ValueError):
            LIVE_LAST_EXECUTION_TIME[symbol] = 0

    LIVE_BROKER_CLOSED_HISTORY.clear()
    LIVE_BROKER_HISTORY_CACHE["history"] = []
    LIVE_BROKER_HISTORY_CACHE["updated_at"] = 0

    for symbol in LIVE_AUTO_STATUS_BY_SYMBOL:
        if LIVE_ACTIVE_ORDERS.get(symbol):
            continue

        LIVE_AUTO_STATUS_BY_SYMBOL[symbol] = {
            **LIVE_AUTO_STATUS_BY_SYMBOL[symbol],
            "signal": None,
            "action": None,
            "status": "WAIT",
            "reason": "Waiting for BUY/SELL signal",
            "checked_at": time.time(),
            "active_trade": None,
        }

    if not any(LIVE_ACTIVE_ORDERS.values()):
        AUTO_TRADE_LAST_STATUS.update({
            "symbol": None,
            "signal": None,
            "action": None,
            "status": "WAIT",
            "reason": "Waiting for BUY/SELL signal",
            "timestamp": time.time(),
        })

    LAST_LIVE_RESET = reset_ts

    removed_count = before_count - len(LIVE_TRADE_HISTORY)

    print("LIVE WEEKLY RESET:", {
        "reset_time": datetime.fromtimestamp(reset_ts).isoformat(),
        "removed_closed_trades": removed_count,
        "kept_trades": len(LIVE_TRADE_HISTORY),
    })

    save_live_backup()
    return True

def move_live_trade_to_history_once(trade):
    if not isinstance(trade, dict):
        return

    ensure_live_trade_identity(trade)

    identifiers = [
        trade.get("trade_id"),
        trade.get("order_id"),
        trade.get("broker_order_id"),
        trade.get("position_id"),
        trade.get("broker_position_id"),
    ]
    identifiers = [str(item) for item in identifiers if item]

    if identifiers:
        LIVE_TRADE_HISTORY[:] = [
            item for item in LIVE_TRADE_HISTORY
            if not any(
                str(item.get(key)) in identifiers
                for key in [
                    "order_id",
                    "broker_order_id",
                    "position_id",
                    "broker_position_id",
                    "trade_id",
                ]
                if item.get(key)
            )
        ]

    LIVE_TRADE_HISTORY.insert(0, trade)
    del LIVE_TRADE_HISTORY[MAX_LIVE_TRADE_HISTORY:]
    log_live_trade_audit("history_upsert", trade)

def get_trade_timestamp(trade):
    if not isinstance(trade, dict):
        return None

    for key in ["closed_at", "opened_at", "time", "timestamp"]:
        value = trade.get(key)

        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

    return None

def trade_is_today(trade):
    timestamp = get_trade_timestamp(trade)

    if not timestamp:
        return False

    if timestamp > 10000000000:
        timestamp = timestamp / 1000

    trade_time = time.localtime(timestamp)
    current_time = time.localtime()

    return (
        trade_time.tm_year == current_time.tm_year
        and trade_time.tm_yday == current_time.tm_yday
    )

def trade_is_current_week(trade, reset_ts=None):
    timestamp = get_trade_timestamp(trade)

    if not timestamp:
        return False

    if timestamp > 10000000000:
        timestamp = timestamp / 1000

    if reset_ts is None:
        reset_ts = get_live_week_start_ts()

    return timestamp >= reset_ts

def get_live_daily_reset_ts(now=None):
    if now is None:
        now = datetime.now(LIVE_MARKET_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=LIVE_MARKET_TIMEZONE)
    else:
        now = now.astimezone(LIVE_MARKET_TIMEZONE)

    reset = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if now < reset:
        reset -= timedelta(days=1)
    return reset.timestamp()

def get_live_month_start_ts(now=None):
    if now is None:
        now = datetime.now(LIVE_MARKET_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=LIVE_MARKET_TIMEZONE)
    else:
        now = now.astimezone(LIVE_MARKET_TIMEZONE)

    return now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).timestamp()

def get_closed_trade_dedupe_key(trade):
    if not isinstance(trade, dict):
        return None

    deal_id = trade.get("deal_id")
    if deal_id not in [None, ""]:
        return f"deal:{deal_id}"

    trade_id = trade.get("trade_id")
    if trade_id not in [None, ""]:
        return f"trade:{trade_id}"

    timestamp = get_trade_timestamp(trade)
    broker_pl, _source = extract_broker_realized_pl(trade)
    return "|".join([
        str(normalize_symbol(trade.get("symbol")) or ""),
        str(trade.get("order_id") or trade.get("broker_order_id") or ""),
        str(trade.get("position_id") or trade.get("broker_position_id") or ""),
        str(timestamp or ""),
        str(broker_pl if broker_pl is not None else ""),
    ])

def calculate_closed_pl_windows(raw_closed_trades, now=None):
    current = now or datetime.now(LIVE_MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LIVE_MARKET_TIMEZONE)
    else:
        current = current.astimezone(LIVE_MARKET_TIMEZONE)

    weekly_reset_ts = get_live_week_start_ts(current)
    daily_reset_ts = get_live_daily_reset_ts(current)
    monthly_start_ts = get_live_month_start_ts(current)
    unique_trades = []
    seen = set()
    ignored_duplicate_count = 0

    for trade in raw_closed_trades or []:
        if not isinstance(trade, dict):
            continue

        source = str(trade.get("source") or "").lower()
        history_source = str(trade.get("history_source") or "").lower()
        if source not in ["broker", "ctrader"] and "ctrader" not in history_source:
            continue

        key = get_closed_trade_dedupe_key(trade)
        if not key or key in seen:
            ignored_duplicate_count += 1
            continue

        timestamp = get_trade_timestamp(trade)
        if timestamp is None:
            continue
        if timestamp > 10_000_000_000:
            timestamp /= 1000

        broker_pl, broker_pl_source = extract_broker_realized_pl(trade)
        if broker_pl is None:
            continue

        seen.add(key)
        unique_trades.append({
            "trade": trade,
            "timestamp": timestamp,
            "profit": broker_pl,
            "profit_source": broker_pl_source,
        })

    day_trades = [
        item for item in unique_trades
        if item["timestamp"] >= daily_reset_ts
    ]
    week_trades = [
        item for item in unique_trades
        if item["timestamp"] >= weekly_reset_ts
    ]
    month_trades = [
        item for item in unique_trades
        if item["timestamp"] >= monthly_start_ts
    ]
    ignored_old_trades_count = sum(
        item["timestamp"] < weekly_reset_ts
        for item in unique_trades
    )
    weekly_losing_trades = [
        item for item in week_trades
        if item["profit"] < 0
    ]
    daily_losing_trades = [
        item for item in day_trades
        if item["profit"] < 0
    ]

    invalid_week_trades = [
        item for item in week_trades
        if item["timestamp"] < weekly_reset_ts
    ]
    if invalid_week_trades:
        print("WEEKLY_PL_VALIDATION_ERROR =", {
            "weekly_reset_time": datetime.fromtimestamp(
                weekly_reset_ts,
                LIVE_MARKET_TIMEZONE,
            ).isoformat(),
            "invalid_trade_count": len(invalid_week_trades),
        })
        week_trades = [
            item for item in unique_trades
            if item["timestamp"] >= weekly_reset_ts
        ]

    weekly_realized_pl = round(
        sum(item["profit"] for item in week_trades),
        2,
    )
    daily_realized_pl = round(
        sum(item["profit"] for item in day_trades),
        2,
    )

    if weekly_realized_pl < 0 and not weekly_losing_trades:
        print("stale weekly P/L cache ignored")
        weekly_realized_pl = 0

    if daily_realized_pl < 0 and not daily_losing_trades:
        print("stale daily P/L cache ignored")
        daily_realized_pl = 0

    debug = {
        "now_ny": current.isoformat(),
        "current_week_start_ny": datetime.fromtimestamp(
            weekly_reset_ts,
            LIVE_MARKET_TIMEZONE,
        ).isoformat(),
        "daily_start_ny": datetime.fromtimestamp(
            daily_reset_ts,
            LIVE_MARKET_TIMEZONE,
        ).isoformat(),
        "weekly_reset_time": datetime.fromtimestamp(
            weekly_reset_ts,
            LIVE_MARKET_TIMEZONE,
        ).isoformat(),
        "daily_reset_time": datetime.fromtimestamp(
            daily_reset_ts,
            LIVE_MARKET_TIMEZONE,
        ).isoformat(),
        "monthly_start_time": datetime.fromtimestamp(
            monthly_start_ts,
            LIVE_MARKET_TIMEZONE,
        ).isoformat(),
        "closed_trades_count_week": len(week_trades),
        "closed_trades_count_day": len(day_trades),
        "closed_trades_count_month": len(month_trades),
        "closed_trades_after_week_start": len(week_trades),
        "closed_trades_after_daily_start": len(day_trades),
        "ignored_old_trades_count": ignored_old_trades_count,
        "ignored_old_closed_trades": ignored_old_trades_count,
        "ignored_duplicate_count": ignored_duplicate_count,
        "weekly_losing_trades_count": len(weekly_losing_trades),
        "daily_losing_trades_count": len(daily_losing_trades),
        "weekly_pl_source": "raw_ctrader_closed_deals",
        "daily_pl_source": "raw_ctrader_closed_deals",
    }

    return {
        "daily_realized_pl": daily_realized_pl,
        "weekly_realized_pl": weekly_realized_pl,
        "monthly_realized_pl": round(sum(item["profit"] for item in month_trades), 2),
        "day_trades": [item["trade"] for item in day_trades],
        "week_trades": [item["trade"] for item in week_trades],
        "month_trades": [item["trade"] for item in month_trades],
        "daily_reset_ts": daily_reset_ts,
        "weekly_reset_ts": weekly_reset_ts,
        "monthly_start_ts": monthly_start_ts,
        "debug": debug,
    }

def live_trade_has_broker_id(trade):
    if not isinstance(trade, dict):
        return False

    return bool(
        trade.get("position_id")
        or trade.get("broker_position_id")
        or trade.get("broker_order_id")
        or trade.get("order_id")
    )

def normalize_ctrader_money_value(value, trade=None):
    if value is None:
        return None

    raw = trade.get("raw") if isinstance(trade, dict) else None
    money_digits = 2

    if isinstance(raw, dict):
        money_digits = raw.get("moneyDigits", money_digits)

    if isinstance(trade, dict):
        money_digits = trade.get("moneyDigits", money_digits)

    parsed = parse_ctrader_money(value, money_digits)

    if parsed is None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return parsed

def extract_broker_trade_pl(trade):
    if not isinstance(trade, dict):
        return None, None

    raw = trade.get("raw") if isinstance(trade.get("raw"), dict) else {}
    raw_broker_keys = [
        "netUnrealizedPnL",
        "grossUnrealizedPnL",
        "netProfit",
        "unrealizedProfit",
        "netUnrealizedProfit",
        "unrealizedNetProfit",
        "profit",
        "pnl",
        "pl",
        "grossProfit",
        "moneyProfit",
        "grossUnrealizedProfit",
    ]
    trade_broker_keys = [
        "netUnrealizedPnL",
        "grossUnrealizedPnL",
        "netProfit",
        "net_profit",
        "unrealizedProfit",
        "netUnrealizedProfit",
        "unrealizedNetProfit",
        "broker_pnl",
        "pnl",
        "profit",
        "pl",
        "grossProfit",
        "moneyProfit",
        "grossUnrealizedProfit",
    ]

    for source_name, source, broker_keys in [
        ("raw", raw, raw_broker_keys),
        ("trade", trade, trade_broker_keys),
    ]:
        for key in broker_keys:
            if key not in source or source.get(key) is None:
                continue

            if (
                source_name == "trade"
                and key in ["pnl", "profit", "pl", "broker_pnl"]
                and trade.get("broker_pnl_source") == "fallback"
            ):
                continue

            value = normalize_ctrader_money_value(source.get(key), trade)

            if value is not None:
                return value, f"{source_name}.{key}"

    return None, None

def extract_trade_pl(trade):
    if not isinstance(trade, dict):
        return 0

    broker_pl, _source = extract_broker_trade_pl(trade)

    if broker_pl is not None:
        return broker_pl

    result = trade.get("result")

    for key in [
        "pnl",
        "profit",
        "pl",
        "floating_pl",
        "floating_pnl",
        "floatingProfit",
        "floatingPnl",
        "unrealizedProfit",
        "unrealizedNetProfit",
        "grossUnrealizedProfit",
        "netUnrealizedProfit",
        "net_profit",
        "netProfit",
        "grossProfit",
        "moneyProfit",
        "broker_pnl",
    ]:
        value = trade.get(key)

        if value is None and isinstance(result, dict):
            value = result.get(key)

        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    return 0

def extract_broker_realized_pl(trade):
    if not isinstance(trade, dict):
        return None, None

    raw = trade.get("raw") if isinstance(trade.get("raw"), dict) else {}
    raw_realized_keys = [
        "netProfit",
        "profit",
        "grossProfit",
        "moneyProfit",
        "realizedProfit",
        "realizedNetProfit",
        "closedProfit",
    ]
    trade_realized_keys = [
        "broker_realized_profit",
        "realized_profit",
        "realizedProfit",
        "realizedNetProfit",
        "closed_profit",
        "closedProfit",
        "netProfit",
        "grossProfit",
        "moneyProfit",
    ]

    for key in raw_realized_keys:
        if key not in raw or raw.get(key) is None:
            continue

        value = normalize_ctrader_money_value(raw.get(key), trade)

        if value is not None:
            return value, f"raw.{key}"

    for key in trade_realized_keys:
        if key not in trade or trade.get(key) is None:
            continue

        if key in [
            "broker_realized_profit",
            "realized_profit",
            "closed_profit",
        ]:
            try:
                value = float(trade.get(key))
            except (TypeError, ValueError):
                value = None
            if value is not None and math.isfinite(value):
                return value, f"trade.{key}"

        value = normalize_ctrader_money_value(trade.get(key), trade)

        if value is not None:
            return value, f"trade.{key}"

    source = str(trade.get("broker_pnl_source") or "").lower()

    if source and source not in ["fallback", "missing"]:
        for key in ["broker_pnl", "pnl", "profit", "pl"]:
            if key not in trade or trade.get(key) is None:
                continue

            value = normalize_ctrader_money_value(trade.get(key), trade)

            if value is not None:
                return value, f"trade.{key}:{source}"

    return None, None

def get_trade_result_from_pnl(pnl):
    try:
        value = float(pnl)
    except (TypeError, ValueError):
        return "BROKER_CLOSED"

    if value > 0:
        return "WIN"
    if value < 0:
        return "LOSS"

    return "BROKER_CLOSED"

def calculate_tp1_from_tp2(entry, tp2, side):
    entry_value = float(entry)
    tp2_value = float(tp2)
    tp1_ratio = get_tp1_ratio_of_tp2()

    if str(side or "").upper() == "BUY":
        return entry_value + ((tp2_value - entry_value) * tp1_ratio)

    return entry_value - ((entry_value - tp2_value) * tp1_ratio)

def calculate_protected_sl_price(entry, tp2, side):
    entry_value = float(entry)
    tp2_value = float(tp2)

    if str(side or "").upper() == "BUY":
        return entry_value + ((tp2_value - entry_value) * 0.50)

    return entry_value - ((entry_value - tp2_value) * 0.50)


def get_trade_price_increment(trade):
    symbol = normalize_symbol((trade or {}).get("symbol"))
    fallback_digits = 2 if symbol == "XAUUSD" else 5
    fallback_tick = 10 ** -fallback_digits
    metadata = (trade or {}).get("symbol_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    try:
        tick_size = float(
            (trade or {}).get("tick_size")
            or metadata.get("tick_size")
            or fallback_tick
        )
        if not math.isfinite(tick_size) or tick_size <= 0:
            raise ValueError
    except (TypeError, ValueError):
        tick_size = fallback_tick

    try:
        digits = int((trade or {}).get("digits") or metadata.get("digits"))
    except (TypeError, ValueError):
        tick_text = f"{tick_size:.12f}".rstrip("0")
        tick_decimals = len(tick_text.split(".", 1)[1]) if "." in tick_text else 0
        digits = max(fallback_digits, tick_decimals)

    return tick_size, digits


def normalize_price_to_broker_increment(price, trade):
    tick_size, digits = get_trade_price_increment(trade)
    normalized = round(round(float(price) / tick_size) * tick_size, digits)
    return normalized, tick_size, digits


def read_back_broker_stop_loss(trade):
    position_id = (trade or {}).get("position_id") or (trade or {}).get("broker_position_id")
    symbol = normalize_symbol((trade or {}).get("symbol"))

    try:
        positions = get_open_positions() or []
    except Exception as exc:
        return None, {"ok": False, "reason": str(exc), "positions": []}

    for position in positions:
        candidate_id = (
            position.get("position_id")
            or position.get("positionId")
            or position.get("id")
        )
        if str(candidate_id) != str(position_id):
            continue
        if normalize_symbol(position.get("symbol")) != symbol:
            continue
        raw = position.get("raw") if isinstance(position.get("raw"), dict) else {}
        broker_sl = first_valid_live_price(
            position.get("stop_loss"),
            position.get("stopLoss"),
            position.get("sl"),
            raw.get("stopLoss"),
            raw.get("sl"),
        )
        return broker_sl, {"ok": True, "position": position}

    return None, {
        "ok": False,
        "reason": "Matching cTrader position was not returned after amendment",
        "position_id": position_id,
        "symbol": symbol,
    }

def live_sl_protection_confirmed(trade):
    if not isinstance(trade, dict) or not trade.get("protection_confirmed"):
        return False

    broker_result = trade.get("sl_protection_broker_result")

    return isinstance(broker_result, dict) and broker_result.get("ok") is True

def log_trade_visual_levels(trade):
    if not isinstance(trade, dict):
        return

    print("TRADE_VISUAL_LEVELS =", {
        "symbol": normalize_symbol(trade.get("symbol")),
        "entry": trade.get("entry"),
        "original_sl": trade.get("original_sl"),
        "current_sl": trade.get("sl"),
        "planned_sl": trade.get("planned_sl"),
        "broker_stop_loss_confirmed": trade.get("broker_stop_loss_confirmed"),
        "broker_stop_loss_missing": trade.get("broker_stop_loss_missing"),
        "tp1": trade.get("tp1"),
        "tp2": trade.get("tp2"),
        "planned_tp1": trade.get("planned_tp1"),
        "planned_tp2": trade.get("planned_tp2"),
        "broker_take_profit_confirmed": trade.get("broker_take_profit_confirmed"),
        "broker_take_profit_missing": trade.get("broker_take_profit_missing"),
        "hit_tp1": bool(trade.get("hit_tp1")),
        "profit_protected": live_sl_protection_confirmed(trade),
        "protected_sl_price": trade.get("protected_sl_price"),
    })

def protect_live_trade_after_tp1(trade):
    if not isinstance(trade, dict):
        return trade

    if trade.get("hit_tp1") and live_sl_protection_confirmed(trade):
        return trade

    try:
        symbol = normalize_symbol(trade.get("symbol"))
        protected_sl, tick_size, digits = normalize_price_to_broker_increment(
            calculate_protected_sl_price(
                trade.get("entry"),
                trade.get("tp2"),
                trade.get("side") or trade.get("action")
            ),
            trade,
        )
    except (TypeError, ValueError):
        return trade

    position_id = (
        trade.get("position_id")
        or trade.get("broker_position_id")
    )
    original_sl = trade.get("original_sl", trade.get("sl"))

    if not position_id:
        trade["hit_tp1"] = True
        trade["tp1_hit"] = True
        trade["protection_requested"] = False
        trade["protection_confirmed"] = False
        trade["profit_protected"] = False
        trade["protected_sl_price"] = protected_sl
        trade["original_sl"] = original_sl
        trade["result"] = "TP1 HIT"
        trade["sl_protection_failed"] = True
        trade["sl_protection_warning"] = "BROKER SL PROTECTION FAILED"
        trade["sl_protection_error"] = "Missing cTrader position id"

        print("LIVE_SL_PROTECTION_FAILED:", {
            "symbol": symbol,
            "direction": trade.get("side") or trade.get("action"),
            "position_id": position_id,
            "bid": trade.get("bid"),
            "ask": trade.get("ask"),
            "candle_high": trade.get("current_high"),
            "candle_low": trade.get("current_low"),
            "entry": trade.get("entry"),
            "tp1": trade.get("tp1"),
            "tp2": trade.get("tp2"),
            "old_sl": original_sl,
            "calculated_protected_sl": protected_sl,
            "broker_response": None,
            "verification_result": {"ok": False},
            "reason": "Missing cTrader position id",
        })

        persist_live_trade_state(trade)
        return trade

    trade["hit_tp1"] = True
    trade["tp1_hit"] = True
    trade["protection_requested"] = True
    trade["protection_confirmed"] = False
    trade["protected_sl_price"] = protected_sl
    trade["original_sl"] = original_sl
    trade["result"] = "TP1 HIT"
    persist_live_trade_state(trade)

    modify_result = modify_position_stop_loss(
        position_id,
        protected_sl,
        take_profit_price=trade.get("tp2"),
    )

    if not modify_result.get("ok"):
        trade["profit_protected"] = False
        trade["sl_protection_failed"] = True
        trade["sl_protection_warning"] = "BROKER SL PROTECTION FAILED"
        trade["sl_protection_error"] = modify_result.get("reason") or "Unknown cTrader SL modify error"
        trade["sl_protection_broker_result"] = modify_result

        print("LIVE_SL_PROTECTION_FAILED:", {
            "symbol": symbol,
            "direction": trade.get("side") or trade.get("action"),
            "position_id": position_id,
            "bid": trade.get("bid"),
            "ask": trade.get("ask"),
            "candle_high": trade.get("current_high"),
            "candle_low": trade.get("current_low"),
            "entry": trade.get("entry"),
            "tp1": trade.get("tp1"),
            "tp2": trade.get("tp2"),
            "old_sl": original_sl,
            "calculated_protected_sl": protected_sl,
            "reason": trade["sl_protection_error"],
            "broker_response": modify_result,
            "verification_result": {"ok": False},
        })

        persist_live_trade_state(trade)
        return trade

    broker_sl, readback_result = read_back_broker_stop_loss(trade)
    verification_ok = (
        broker_sl is not None
        and abs(float(broker_sl) - protected_sl) <= (tick_size + 1e-12)
    )
    verification = {
        "ok": verification_ok,
        "requested_sl": protected_sl,
        "broker_sl": broker_sl,
        "tick_size": tick_size,
        "digits": digits,
        "within_tick_tolerance": verification_ok,
        "readback": readback_result,
    }
    trade["sl_protection_verification"] = verification

    if not verification_ok:
        trade["profit_protected"] = False
        trade["protection_confirmed"] = False
        trade["sl_protection_failed"] = True
        trade["sl_protection_warning"] = "BROKER SL PROTECTION FAILED"
        trade["sl_protection_error"] = (
            readback_result.get("reason")
            or f"Broker SL {broker_sl} did not match requested SL {protected_sl}"
        )
        trade["sl_protection_broker_result"] = modify_result
        print("BROKER SL PROTECTION FAILED", {
            "symbol": symbol,
            "direction": trade.get("side") or trade.get("action"),
            "position_id": position_id,
            "bid": trade.get("bid"),
            "ask": trade.get("ask"),
            "candle_high": trade.get("current_high"),
            "candle_low": trade.get("current_low"),
            "tp1": trade.get("tp1"),
            "tp2": trade.get("tp2"),
            "old_sl": original_sl,
            "calculated_protected_sl": protected_sl,
            "broker_response": modify_result,
            "verification_result": verification,
        })
        persist_live_trade_state(trade)
        return trade

    trade["profit_protected"] = True
    trade["protection_confirmed"] = True
    trade["sl"] = protected_sl
    trade["sl_protection_failed"] = False
    trade["sl_protection_warning"] = None
    trade["sl_protection_error"] = None
    trade["sl_protection_broker_result"] = modify_result

    print("TP1_HIT_PROTECT_PROFIT:", {
        "symbol": symbol,
        "direction": trade.get("side") or trade.get("action"),
        "bid": trade.get("bid"),
        "ask": trade.get("ask"),
        "candle_high": trade.get("current_high"),
        "candle_low": trade.get("current_low"),
        "entry": trade.get("entry"),
        "tp1": trade.get("tp1"),
        "tp2": trade.get("tp2"),
        "old_sl": original_sl,
        "calculated_protected_sl": protected_sl,
        "profit_protected": True,
        "broker_response": modify_result,
        "verification_result": verification,
    })

    print("LIVE_SL_PROTECTED_ON_BROKER:", {
        "symbol": symbol,
        "side": trade.get("side") or trade.get("action"),
        "position_id": position_id,
        "entry": trade.get("entry"),
        "tp1": trade.get("tp1"),
        "tp2": trade.get("tp2"),
        "original_sl": original_sl,
        "protected_sl_price": protected_sl,
        "broker_result": modify_result,
    })

    persist_live_trade_state(trade)

    return trade

def update_live_trade_tp_protection(trade):
    if not isinstance(trade, dict):
        return trade

    hit_tp1_before = bool(trade.get("tp1_hit") or trade.get("hit_tp1"))
    side = str(trade.get("side") or trade.get("action") or "").upper()

    def optional_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    current_price = optional_float(trade.get("current_price"))
    bid = optional_float(trade.get("bid"))
    ask = optional_float(trade.get("ask"))
    current_high = optional_float(trade.get("current_high"))
    current_low = optional_float(trade.get("current_low"))
    tp1 = optional_float(trade.get("tp1"))
    tp2 = optional_float(trade.get("tp2"))

    if current_high is None:
        current_high = current_price

    if current_low is None:
        current_low = current_price

    trigger_price = current_price
    if side == "BUY" and bid is not None:
        trigger_price = bid
    elif side == "SELL" and ask is not None:
        trigger_price = ask

    wick_touch_enabled = trade.get("wick_touch_enabled", True) is not False
    buy_tick_hit = side == "BUY" and trigger_price is not None and trigger_price >= tp1 if tp1 is not None else False
    sell_tick_hit = side == "SELL" and trigger_price is not None and trigger_price <= tp1 if tp1 is not None else False
    buy_wick_hit = wick_touch_enabled and side == "BUY" and current_high is not None and current_high >= tp1 if tp1 is not None else False
    sell_wick_hit = wick_touch_enabled and side == "SELL" and current_low is not None and current_low <= tp1 if tp1 is not None else False
    buy_trigger_high = current_high if wick_touch_enabled else trigger_price
    sell_trigger_low = current_low if wick_touch_enabled else trigger_price

    tp1_hit_detected = bool(
        tp1 is not None
        and (trigger_price is not None or buy_trigger_high is not None or sell_trigger_low is not None)
        and (
            buy_tick_hit or sell_tick_hit or buy_wick_hit or sell_wick_hit
        )
    )
    tp2_hit_detected = bool(
        tp2 is not None
        and (trigger_price is not None or buy_trigger_high is not None or sell_trigger_low is not None)
        and (
            (side == "BUY" and buy_trigger_high is not None and buy_trigger_high >= tp2)
            or
            (side == "SELL" and sell_trigger_low is not None and sell_trigger_low <= tp2)
        )
    )

    if tp2_hit_detected:
        trade["tp2_hit"] = True

    if tp2_hit_detected and not trade.get("tp2_close_requested"):
        position_id = (
            trade.get("position_id")
            or trade.get("broker_position_id")
        )
        volume_units = (
            trade.get("volume_units")
            or ((trade.get("raw") or {}).get("tradeData") or {}).get("volume")
        )
        close_result = close_position(position_id, volume=volume_units)
        trade["tp2_hit"] = True
        trade["tp2_close_requested"] = close_result.get("ok") is True
        trade["tp2_close_result"] = close_result

        if close_result.get("ok"):
            trade["status"] = "CLOSING"
            trade["result"] = "TP2 HIT"
            trade["exit_status"] = "CLOSING"
            trade["exit_reason"] = "TP2 hit; broker close requested"
            trade["closed_reason"] = "TP2 hit"
        else:
            trade["tp2_close_failed"] = True
            trade["tp2_close_error"] = (
                close_result.get("reason")
                or "Broker close request failed"
            )

        print("LIVE_TP2_CLOSE_DEBUG =", {
            "symbol": normalize_symbol(trade.get("symbol")),
            "side": side,
            "position_id": position_id,
            "volume_units": volume_units,
            "tp2": tp2,
            "bid": bid,
            "ask": ask,
            "trigger_price": trigger_price,
            "buy_trigger_high": buy_trigger_high,
            "sell_trigger_low": sell_trigger_low,
            "current_high": current_high,
            "current_low": current_low,
            "close_result": close_result,
        })
        return trade

    if trade.get("tp2_close_requested"):
        trade["status"] = "CLOSING"
        trade["result"] = "TP2 HIT"
        trade["exit_status"] = "CLOSING"
        trade["exit_reason"] = trade.get("exit_reason") or "TP2 hit; waiting for broker close confirmation"
        print("LIVE_TP2_CLOSE_PENDING_DEBUG =", {
            "symbol": normalize_symbol(trade.get("symbol")),
            "side": side,
            "position_id": trade.get("position_id") or trade.get("broker_position_id"),
            "tp2": tp2,
            "bid": bid,
            "ask": ask,
            "trigger_price": trigger_price,
            "buy_trigger_high": buy_trigger_high,
            "sell_trigger_low": sell_trigger_low,
            "current_high": current_high,
            "current_low": current_low,
        })
        return trade

    if hit_tp1_before and not trade.get("protection_requested"):
        trade = protect_live_trade_after_tp1(trade)

    if hit_tp1_before or tp1 is None or tp2 is None:
        print("LIVE_TP1_PROTECTION_DEBUG", {
            "symbol": normalize_symbol(trade.get("symbol")),
            "side": side,
            "current_price": current_price,
            "bid": bid,
            "ask": ask,
            "trigger_price": trigger_price,
            "buy_trigger_high": buy_trigger_high,
            "sell_trigger_low": sell_trigger_low,
            "current_high": current_high,
            "current_low": current_low,
            "tp1": tp1,
            "tp2": tp2,
            "hit_tp1_before": hit_tp1_before,
            "tp1_hit_detected": tp1_hit_detected,
            "protected_sl_price": trade.get("protected_sl_price"),
        })
        return trade

    if tp1_hit_detected:
        trade = protect_live_trade_after_tp1(trade)

    print("LIVE_TP1_PROTECTION_DEBUG", {
        "symbol": normalize_symbol(trade.get("symbol")),
        "side": side,
        "current_price": current_price,
        "bid": bid,
        "ask": ask,
        "trigger_price": trigger_price,
        "buy_trigger_high": buy_trigger_high,
        "sell_trigger_low": sell_trigger_low,
        "current_high": current_high,
        "current_low": current_low,
        "tp1": tp1,
        "tp2": tp2,
        "hit_tp1_before": hit_tp1_before,
        "tp1_hit_detected": tp1_hit_detected,
        "protected_sl_price": trade.get("protected_sl_price"),
    })

    return trade

def calculate_live_trade_stats():
    run_weekly_live_reset()

    active_ids = {
        str(get_live_trade_match_key(trade))
        for trade in LIVE_ACTIVE_ORDERS.values()
        if trade and get_live_trade_match_key(trade)
    }
    broker_history = get_live_broker_closed_history()
    history_source = broker_history if broker_history else LIVE_TRADE_HISTORY
    today_history = [
        trade for trade in history_source
        if trade_is_today(trade)
        and str(get_live_trade_match_key(trade)) not in active_ids
        and (broker_history or is_usable_local_live_history_trade(trade))
    ]
    today_active = [
        trade for trade in LIVE_ACTIVE_ORDERS.values()
        if trade and trade_is_today(trade)
    ]
    seen = set()
    total_today = 0

    for trade in today_history + today_active:
        trade_key = (
            trade.get("broker_order_id")
            or trade.get("position_id")
            or trade.get("order_id")
            or id(trade)
        )

        if trade_key in seen:
            continue

        seen.add(trade_key)
        total_today += 1

    wins = 0
    losses = 0
    closed = 0
    total_pl = 0

    for trade in today_history:
        status = get_live_trade_status(trade)

        if status in ["RUNNING", "OPEN", "CLOSING", "TP2 HIT"]:
            continue

        closed += 1
        broker_pl, _source = extract_broker_trade_pl(trade)
        pl = broker_pl if broker_pl is not None else 0
        total_pl += pl

        if status in ["WIN", "WON", "PROFIT"] or pl > 0:
            wins += 1
        elif status in ["LOSS", "LOST"] or pl < 0:
            losses += 1

    active_pl = sum(get_stored_live_floating_pl(trade) for trade in today_active)
    total_pl += active_pl

    running = sum(
        1
        for trade in LIVE_ACTIVE_ORDERS.values()
        if trade and get_live_trade_status(trade) in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]
    )

    return {
        "total_today": total_today,
        "wins": wins,
        "losses": losses,
        "running": running,
        "closed": closed,
        "total_pl": round(total_pl, 2),
        "total_pnl": round(total_pl, 2),
    }

def calculate_live_pl_sync():
    run_weekly_live_reset()
    weekly_history = get_live_broker_closed_history(force=True)
    monthly_history = get_live_broker_monthly_history(force=True)
    raw_closed_trades = list(weekly_history or []) + list(monthly_history or [])
    closed_windows = calculate_closed_pl_windows(raw_closed_trades)
    daily_realized_pl = closed_windows["daily_realized_pl"]
    weekly_realized_pl = closed_windows["weekly_realized_pl"]
    monthly_realized_pl = closed_windows["monthly_realized_pl"]

    print("LIVE_PL_WINDOW_DEBUG =", closed_windows["debug"])
    print("WEEKLY_PL_TOTAL =", weekly_realized_pl)

    open_trades = [
        trade
        for trade in LIVE_ACTIVE_ORDERS.values()
        if (
            isinstance(trade, dict)
            and live_trade_has_broker_id(trade)
            and get_live_trade_status(trade) in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]
        )
    ]
    open_position_debug = []
    floating_values = []

    for trade in open_trades:
        floating_pl = get_stored_live_floating_pl(trade)
        floating_values.append(floating_pl)
        open_position_debug.append({
            "position_id": (
                trade.get("position_id")
                or trade.get("broker_position_id")
                or trade.get("broker_order_id")
                or trade.get("order_id")
            ),
            "symbol": normalize_symbol(trade.get("symbol")),
            "entry": trade.get("entry"),
            "current_price": trade.get("current_price")
                or trade.get("currentPrice"),
            "side": trade.get("side") or trade.get("action"),
            "volume": trade.get("volume_units")
                or trade.get("volume")
                or trade.get("lot_size"),
            "floating_pl": round(floating_pl, 2),
        })

    floating_live_pl = sum(floating_values)
    daily_total_pl = daily_realized_pl + floating_live_pl
    weekly_total_pl = weekly_realized_pl + floating_live_pl

    print("LIVE_PL_DEBUG =", {
        **closed_windows["debug"],
        "open_positions": open_position_debug,
        "position_id": (
            open_position_debug[0]["position_id"]
            if open_position_debug else None
        ),
        "symbol": (
            open_position_debug[0]["symbol"]
            if open_position_debug else None
        ),
        "entry": (
            open_position_debug[0]["entry"]
            if open_position_debug else None
        ),
        "current_price": (
            open_position_debug[0]["current_price"]
            if open_position_debug else None
        ),
        "side": (
            open_position_debug[0]["side"]
            if open_position_debug else None
        ),
        "volume": (
            open_position_debug[0]["volume"]
            if open_position_debug else None
        ),
        "floating_pl": (
            open_position_debug[0]["floating_pl"]
            if open_position_debug else 0
        ),
        "floating_live_pl": round(floating_live_pl, 2),
        "daily_realized_pl": round(daily_realized_pl, 2),
        "daily_total_pl": round(daily_total_pl, 2),
        "weekly_realized_pl": round(weekly_realized_pl, 2),
        "monthly_realized_pl": round(monthly_realized_pl, 2),
        "weekly_total_pl": round(weekly_total_pl, 2),
    })
    log_live_trade_audit(
        "pl_sync",
        {"source": "broker", "status": "PL_SYNC", "result": "PL_SYNC"},
        weekly_realized_pl=round(weekly_realized_pl, 2),
        floating_live_pl=round(floating_live_pl, 2),
        weekly_total_pl=round(weekly_total_pl, 2),
        reason=f"open_positions={len(open_trades)}"
    )

    return {
        "pl_calculation_version": "closed-windows-v2",
        "daily_realized_pl": round(daily_realized_pl, 2),
        "daily_total_pl": round(daily_total_pl, 2),
        "weekly_realized_pl": round(weekly_realized_pl, 2),
        "monthly_realized_pl": round(monthly_realized_pl, 2),
        "floating_live_pl": round(floating_live_pl, 2),
        "weekly_total_pl": round(weekly_total_pl, 2),
        "open_positions_count": len(open_trades),
        "daily_reset_ts": closed_windows["daily_reset_ts"],
        "weekly_reset_ts": closed_windows["weekly_reset_ts"],
        "monthly_start_ts": closed_windows["monthly_start_ts"],
        "calculated_at": time.time(),
        "debug": closed_windows["debug"],
    }


def parse_live_loss_limit(value):
    if value in [None, ""]:
        return None

    try:
        limit = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(limit) or limit <= 0:
        return None

    return abs(limit)


def format_live_market_time(ts):
    if ts in [None, ""]:
        return None

    try:
        return datetime.fromtimestamp(
            float(ts),
            LIVE_MARKET_TIMEZONE,
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def get_live_loss_limit_status(now=None):
    current = now or datetime.now(LIVE_MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LIVE_MARKET_TIMEZONE)
    else:
        current = current.astimezone(LIVE_MARKET_TIMEZONE)

    risk_settings = load_risk_settings()
    daily_limit = parse_live_loss_limit(risk_settings.get("maxDailyLoss"))
    weekly_limit = parse_live_loss_limit(risk_settings.get("maxWeeklyLoss"))

    status = {
        "blocked": False,
        "reason": None,
        "maxDailyLoss": daily_limit,
        "maxWeeklyLoss": weekly_limit,
        "daily_limit_enabled": daily_limit is not None,
        "weekly_limit_enabled": weekly_limit is not None,
        "daily_reset_rule": "5:00 PM New York",
        "weekly_reset_rule": "Sunday 5:00 PM New York",
    }

    if daily_limit is None and weekly_limit is None:
        return status

    try:
        live_pl = calculate_live_pl_sync()
    except Exception as exc:
        status.update({
            "blocked": True,
            "reason": "LIVE BLOCKED: risk limit check unavailable.",
            "fetch_error": str(exc),
        })
        return status

    daily_reset_ts = live_pl.get("daily_reset_ts") or get_live_daily_reset_ts(current)
    weekly_reset_ts = live_pl.get("weekly_reset_ts") or get_live_week_start_ts(current)
    daily_reset_dt = datetime.fromtimestamp(
        float(daily_reset_ts),
        LIVE_MARKET_TIMEZONE,
    )
    weekly_reset_dt = datetime.fromtimestamp(
        float(weekly_reset_ts),
        LIVE_MARKET_TIMEZONE,
    )

    daily_total_pl = float(live_pl.get("daily_total_pl") or 0)
    weekly_total_pl = float(live_pl.get("weekly_total_pl") or 0)

    status.update({
        "daily_total_pl": round(daily_total_pl, 2),
        "daily_realized_pl": live_pl.get("daily_realized_pl"),
        "floating_live_pl": live_pl.get("floating_live_pl"),
        "weekly_total_pl": round(weekly_total_pl, 2),
        "weekly_realized_pl": live_pl.get("weekly_realized_pl"),
        "daily_reset_ts": daily_reset_ts,
        "daily_reset_time": daily_reset_dt.isoformat(),
        "next_daily_reset_time": (daily_reset_dt + timedelta(days=1)).isoformat(),
        "weekly_reset_ts": weekly_reset_ts,
        "weekly_reset_time": weekly_reset_dt.isoformat(),
        "next_weekly_reset_time": (weekly_reset_dt + timedelta(days=7)).isoformat(),
    })

    if daily_limit is not None and daily_total_pl <= -daily_limit:
        status.update({
            "blocked": True,
            "limit_type": "daily",
            "reason": "LIVE BLOCKED: max daily loss reached until next 5 PM New York reset.",
            "loss_limit": daily_limit,
            "loss_value": round(abs(daily_total_pl), 2),
            "blocked_until": status["next_daily_reset_time"],
        })
        return status

    if weekly_limit is not None and weekly_total_pl <= -weekly_limit:
        status.update({
            "blocked": True,
            "limit_type": "weekly",
            "reason": "LIVE BLOCKED: max weekly loss reached until Sunday 5 PM New York reset.",
            "loss_limit": weekly_limit,
            "loss_value": round(abs(weekly_total_pl), 2),
            "blocked_until": status["next_weekly_reset_time"],
        })
        return status

    return status


def get_performance_data():
    monthly_trades = get_closed_deals_for_current_month(max_rows=500)
    if monthly_trades:
        closed_trades = [
            trade
            for trade in monthly_trades
            if trade_is_current_week(trade)
        ]
    else:
        closed_trades = get_live_broker_closed_history(force=False)
        monthly_trades = list(closed_trades)
    active_trades = [
        trade
        for trade in LIVE_ACTIVE_ORDERS.values()
        if (
            isinstance(trade, dict)
            and get_live_trade_status(trade)
            in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]
        )
    ]
    floating_pnl = sum(
        get_stored_live_floating_pl(trade)
        for trade in active_trades
    )
    return {
        "closed_trades": closed_trades,
        "monthly_trades": monthly_trades,
        "active_trades": active_trades,
        "floating_pnl": floating_pnl,
    }


configure_performance_data_provider(get_performance_data)

def calculate_live_trade_pips(symbol, side, entry, current_price):
    try:
        entry_value = float(entry)
        current_value = float(current_price)
    except (TypeError, ValueError):
        return None

    pip_size = 0.01 if normalize_symbol(symbol) == "XAUUSD" else 0.0001
    normalized_side = normalize_live_trade_side(side)

    if normalized_side == "BUY":
        price_difference = current_value - entry_value
    elif normalized_side == "SELL":
        price_difference = entry_value - current_value
    else:
        return None

    return round(price_difference / pip_size, 1)

def normalize_live_trade_side(side):
    value = str(side or "").strip().upper()

    if value in ["1", "BUY", "LONG"]:
        return "BUY"
    if value in ["2", "SELL", "SHORT"]:
        return "SELL"

    return value

def calculate_live_floating_pl_from_prices(trade):
    if not isinstance(trade, dict):
        return None

    symbol = normalize_symbol(trade.get("symbol"))
    side = normalize_live_trade_side(trade.get("side") or trade.get("action"))
    position_current_price = trade.get("current_price") or trade.get("currentPrice")
    live_prices = {}

    try:
        live_prices = (get_live_prices() or {}).get("live_prices", {})
    except Exception:
        live_prices = {}

    live_tick = live_prices.get(symbol) or {}
    live_bid = live_tick.get("bid")
    live_ask = live_tick.get("ask")
    used_price = position_current_price

    if side == "BUY" and live_bid is not None:
        used_price = live_bid
    elif side == "SELL" and live_ask is not None:
        used_price = live_ask

    pips = calculate_live_trade_pips(
        symbol,
        side,
        trade.get("entry"),
        used_price
    )

    if pips is None:
        print("LIVE_PL_SOURCE =", {
            "symbol": symbol,
            "side": side,
            "entry": trade.get("entry"),
            "position_current_price": position_current_price,
            "live_bid": live_bid,
            "live_ask": live_ask,
            "used_price": used_price,
            "floating_pl": None,
            "reason": "Cannot calculate pips"
        })
        return None

    lots = get_live_trade_display_lots(trade)

    try:
        lots = float(lots or 0)
    except (TypeError, ValueError):
        lots = 0

    if lots <= 0:
        return None

    try:
        metadata = get_ctrader_symbol_risk_metadata(symbol) or {}
    except Exception:
        metadata = {}

    try:
        pip_value_per_lot = float(
            metadata.get("pip_value_per_lot")
            or (1.0 if symbol == "XAUUSD" else 10.0)
        )
    except (TypeError, ValueError):
        pip_value_per_lot = 1.0 if symbol == "XAUUSD" else 10.0

    try:
        entry_value = float(trade.get("entry"))
        used_price_value = float(used_price)
    except (TypeError, ValueError):
        entry_value = None
        used_price_value = None

    if entry_value is not None and used_price_value is not None:
        if side == "BUY":
            price_difference = used_price_value - entry_value
        elif side == "SELL":
            price_difference = entry_value - used_price_value
        else:
            price_difference = None
    else:
        price_difference = None

    floating_pl = round(pips * lots * pip_value_per_lot, 2)
    broker_net_pl, _broker_net_pl_source = extract_broker_trade_pl(trade)

    print("LIVE_PL_SOURCE =", {
        "symbol": symbol,
        "side": side,
        "entry": trade.get("entry"),
        "position_current_price": position_current_price,
        "live_bid": live_bid,
        "live_ask": live_ask,
        "used_price": used_price,
        "floating_pl": floating_pl
    })
    print("FLOATING_PL_DIRECTION_DEBUG =", {
        "symbol": symbol,
        "side": side,
        "entry": trade.get("entry"),
        "current_price": used_price,
        "price_difference": price_difference,
        "floating_pl": floating_pl,
        "broker_net_pl": broker_net_pl,
    })

    return floating_pl

def get_broker_pl_debug_fields(trade):
    if not isinstance(trade, dict):
        return {}

    raw = trade.get("raw") if isinstance(trade.get("raw"), dict) else {}

    return {
        "broker_profit_field": raw.get("profit", trade.get("profit")),
        "broker_net_profit_field": raw.get("netProfit", trade.get("netProfit")),
        "broker_unrealized_profit_field": (
            raw.get("unrealizedProfit")
            if raw.get("unrealizedProfit") is not None
            else trade.get("unrealizedProfit")
        ),
        "broker_unrealized_net_profit_field": (
            trade.get("netUnrealizedPnL")
            if trade.get("netUnrealizedPnL") is not None
            else raw.get("netUnrealizedPnL")
        ),
        "broker_unrealized_net_profit_legacy_field": (
            raw.get("netUnrealizedProfit")
            if raw.get("netUnrealizedProfit") is not None
            else raw.get("unrealizedNetProfit", trade.get("unrealizedNetProfit"))
        ),
        "commission": raw.get("commission", trade.get("commission")),
        "swap": raw.get("swap", trade.get("swap")),
    }

def get_live_floating_pl(trade):
    broker_pl, broker_pl_source = extract_broker_trade_pl(trade)
    fallback_pl = None
    source = broker_pl_source or "fallback"
    used_pl = broker_pl

    if broker_pl is None:
        fallback_pl = calculate_live_floating_pl_from_prices(trade)
        used_pl = fallback_pl
    elif broker_pl_source in ["trade.netUnrealizedPnL", "raw.netUnrealizedPnL"]:
        print("LIVE_PL_SOURCE =", {
            "position_id": (
                trade.get("position_id")
                or trade.get("broker_position_id")
                or trade.get("id")
            ),
            "source": "netUnrealizedPnL",
            "value": round(broker_pl, 2),
        })

    if used_pl is None:
        used_pl = 0
        source = "missing"

    symbol = normalize_symbol(trade.get("symbol")) if isinstance(trade, dict) else ""
    side = normalize_live_trade_side(
        trade.get("side") or trade.get("action")
    ) if isinstance(trade, dict) else ""
    current_price = (
        trade.get("current_price")
        or trade.get("currentPrice")
        if isinstance(trade, dict)
        else None
    )
    price_difference = None

    try:
        entry_value = float(trade.get("entry"))
        current_price_value = float(current_price)
    except (TypeError, ValueError, AttributeError):
        entry_value = None
        current_price_value = None

    if entry_value is not None and current_price_value is not None:
        if side == "BUY":
            price_difference = current_price_value - entry_value
        elif side == "SELL":
            price_difference = entry_value - current_price_value

    raw_volume = (
        trade.get("volume_units")
        or trade.get("volumeInUnits")
        or (trade.get("tradeData") or {}).get("volume")
        or trade.get("volume")
        if isinstance(trade, dict)
        else None
    )

    print("LIVE_PL_SCALE_DEBUG =", {
        "symbol": symbol,
        "broker_net_pl": None if broker_pl is None else round(broker_pl, 2),
        "fallback_pl": None if fallback_pl is None else round(fallback_pl, 2),
        "raw_volume": raw_volume,
        "displayed_lots": get_live_trade_display_lots(trade),
        "lot_size": trade.get("lot_size") if isinstance(trade, dict) else None,
        "used_pl": round(used_pl, 2),
        "source": source,
    })
    broker_debug_fields = get_broker_pl_debug_fields(trade)
    print("LIVE_PL_FINAL_DEBUG =", {
        "position_id": (
            trade.get("position_id")
            or trade.get("broker_position_id")
            or trade.get("id")
            if isinstance(trade, dict)
            else None
        ),
        "netProfit": broker_debug_fields.get("broker_net_profit_field"),
        "profit": broker_debug_fields.get("broker_profit_field"),
        "unrealizedProfit": broker_debug_fields.get("broker_unrealized_profit_field"),
        "unrealizedNetProfit": broker_debug_fields.get("broker_unrealized_net_profit_field"),
        "commission": broker_debug_fields.get("commission"),
        "swap": broker_debug_fields.get("swap"),
        "final_pl_sent_to_frontend": round(used_pl, 2),
    })
    print("FLOATING_PL_DIRECTION_DEBUG =", {
        "symbol": symbol,
        "side": side,
        "entry": trade.get("entry") if isinstance(trade, dict) else None,
        "current_price": current_price,
        "price_difference": price_difference,
        "floating_pl": round(used_pl, 2),
        "broker_net_pl": broker_pl,
    })

    return round(used_pl, 2)

def get_stored_live_floating_pl(trade):
    if not isinstance(trade, dict):
        return 0

    for key in ["floating_pl", "floating_pnl", "broker_pnl", "pnl", "profit"]:
        try:
            value = float(trade.get(key))
        except (TypeError, ValueError):
            continue

        return round(value, 2)

    return 0

def get_default_broker_lot_size(symbol):
    return 100 if normalize_symbol(symbol) == "XAUUSD" else 100000

def get_ctrader_volume_read_scale(symbol):
    return CTRADER_PAYLOAD_VOLUME_SCALE.get(normalize_symbol(symbol), 1)

def get_live_trade_display_lots(trade):
    if not isinstance(trade, dict):
        return None

    symbol = normalize_symbol(trade.get("symbol"))
    raw_volume = (
        trade.get("volume_units")
        or trade.get("volumeInUnits")
        or (trade.get("tradeData") or {}).get("volume")
    )

    lots = normalize_broker_volume_to_lots(raw_volume, symbol)

    if lots is not None:
        return lots

    lots = normalize_broker_volume_to_lots(trade.get("volume"), symbol)

    if lots is not None:
        return lots

    try:
        lot_size = float(trade.get("lot_size"))
    except (TypeError, ValueError):
        return None

    return round(lot_size, 2) if lot_size > 0 else None

def get_trade_identifier_set(trade):
    if not isinstance(trade, dict):
        return set()

    return {
        str(value)
        for value in [
            trade.get("position_id"),
            trade.get("broker_position_id"),
            trade.get("broker_order_id"),
            trade.get("order_id"),
        ]
        if value
    }

def broker_position_matches_trade(position, trade):
    if not isinstance(position, dict) or not isinstance(trade, dict):
        return False

    position_ids = get_trade_identifier_set(position)
    trade_ids = get_trade_identifier_set(trade)

    if position_ids and trade_ids and position_ids.intersection(trade_ids):
        return True

    position_symbol = normalize_symbol(position.get("symbol"))
    trade_symbol = normalize_symbol(trade.get("symbol"))
    position_side = str(position.get("side") or "").upper()
    trade_side = str(trade.get("side") or trade.get("action") or "").upper()

    return (
        position_symbol == trade_symbol
        and bool(position_side)
        and position_side == trade_side
    )

def find_matching_broker_position(trade, positions):
    for position in positions:
        if broker_position_matches_trade(position, trade):
            return position

    return None

def load_live_backup():
    global LAST_LIVE_RESET

    if not os.path.exists(LIVE_BACKUP_FILE):
        return

    try:
        with open(LIVE_BACKUP_FILE, "r") as f:
            backup = json.load(f)

        active_orders = backup.get("live_active_orders", {})
        history = backup.get("live_trade_history", [])
        last_execution_time = backup.get("live_last_execution_time", {})
        last_position_closed_at = backup.get("live_last_position_closed_at", {})
        LAST_LIVE_RESET = float(backup.get(LIVE_RESET_KEY, 0) or 0)

        if not isinstance(active_orders, dict):
            return

        recovered_snapshot = False
        for symbol, trade in active_orders.items():
            execution_symbol = normalize_symbol(symbol)

            if execution_symbol in LIVE_ACTIVE_ORDERS and trade:
                restored_trade = {
                    **trade,
                    "symbol": execution_symbol
                }
                had_matching_snapshot = executed_snapshot_matches_trade(
                    restored_trade.get("executed_trade_setup_snapshot"),
                    restored_trade,
                )
                LIVE_ACTIVE_ORDERS[execution_symbol] = ensure_executed_snapshot_for_active_trade(restored_trade)
                recovered_snapshot = recovered_snapshot or not had_matching_snapshot

        if isinstance(history, list):
            LIVE_TRADE_HISTORY[:] = [
                {
                    **trade,
                    "symbol": normalize_symbol(trade.get("symbol"))
                }
                for trade in history[:MAX_LIVE_TRADE_HISTORY]
                if isinstance(trade, dict)
            ]

        if isinstance(last_execution_time, dict):
            for symbol, timestamp in last_execution_time.items():
                execution_symbol = normalize_symbol(symbol)

                if execution_symbol in LIVE_LAST_EXECUTION_TIME:
                    try:
                        LIVE_LAST_EXECUTION_TIME[execution_symbol] = float(timestamp or 0)
                    except (TypeError, ValueError):
                        pass

        if isinstance(last_position_closed_at, dict):
            try:
                import brain
            except Exception:
                brain = None
            for symbol, timestamp in last_position_closed_at.items():
                execution_symbol = normalize_symbol(symbol)
                if execution_symbol not in LIVE_LAST_POSITION_CLOSED_AT:
                    continue
                try:
                    close_timestamp = float(timestamp or 0)
                except (TypeError, ValueError):
                    continue
                LIVE_LAST_POSITION_CLOSED_AT[execution_symbol] = close_timestamp
                if brain is not None:
                    brain.set_last_position_closed_at(
                        execution_symbol,
                        close_timestamp,
                    )

        print("LIVE BACKUP LOADED:", get_persistable_live_active_orders())
        if recovered_snapshot:
            save_live_backup()
        run_weekly_live_reset()

    except Exception as e:
        print("LIVE BACKUP LOAD ERROR:", e)

load_live_backup()

def sync_ctrader_account_state(force=False):
    connector_state = get_connection_state(force=force)

    LIVE_ACCOUNT_STATE["connected"] = connector_state["connected"]
    LIVE_ACCOUNT_STATE["mode"] = connector_state["mode"]
    LIVE_ACCOUNT_STATE["broker"] = "ctrader"
    LIVE_ACCOUNT_STATE["account_id"] = connector_state.get("account_id")
    LIVE_ACCOUNT_STATE["execution_ready"] = connector_state.get("execution_ready", False)
    LIVE_ACCOUNT_STATE["auth_ok"] = connector_state.get("auth_ok", False)
    LIVE_ACCOUNT_STATE["account_found"] = connector_state.get("account_found", False)
    LIVE_ACCOUNT_STATE["reason"] = connector_state.get("reason")
    LIVE_ACCOUNT_STATE["degraded"] = connector_state.get("degraded", False)
    LIVE_ACCOUNT_STATE["consecutive_failures"] = connector_state.get(
        "consecutive_failures",
        0,
    )
    LIVE_ACCOUNT_STATE["last_success_at"] = connector_state.get(
        "last_success_at"
    )
    LIVE_ACCOUNT_STATE["active_account_id"] = connector_state.get(
        "active_account_id"
    )
    LIVE_ACCOUNT_STATE["authorized_account_ids"] = connector_state.get(
        "authorized_account_ids",
        [],
    )
    LIVE_ACCOUNT_STATE["selected_account_source"] = connector_state.get(
        "selected_account_source"
    )
    LIVE_ACCOUNT_STATE["is_active_account_authorized"] = connector_state.get(
        "is_active_account_authorized",
        False,
    )

    return LIVE_ACCOUNT_STATE

def get_signal_trade_plan(symbol):
    cached_data = PANEL_CACHE.get("data")
    execution_symbol = normalize_symbol(symbol)

    if not isinstance(cached_data, dict):
        return None

    return cached_data.get(execution_symbol)

def is_missing_trade_value(value):
    return value is None or value == "" or value == "--"

def choose_backend_trade_value(plan, payload, backend_key, *payload_keys):
    backend_value = plan.get(backend_key)

    if not is_missing_trade_value(backend_value):
        return backend_value, "backend"

    for key in payload_keys:
        payload_value = payload.get(key)

        if not is_missing_trade_value(payload_value):
            return payload_value, "payload"

    return backend_value, "missing"

def log_frontend_trade_level_mismatch(symbol, action, level_name, backend_value, payload_value):
    if is_missing_trade_value(backend_value) or is_missing_trade_value(payload_value):
        return

    try:
        backend_float = float(backend_value)
        payload_float = float(payload_value)
    except (TypeError, ValueError):
        if str(backend_value) == str(payload_value):
            return
    else:
        if backend_float == payload_float:
            return

    print(
        "CTRADER FRONTEND LEVEL MISMATCH:",
        symbol,
        action,
        level_name,
        "backend=",
        backend_value,
        "payload=",
        payload_value
    )

def log_rejected_ctrader_trade(symbol, action, entry, sl, tp1, tp2, reason):
    print(
        "CTRADER ORDER REJECTED:",
        symbol,
        action,
        entry,
        sl,
        tp1,
        tp2,
        reason
    )

def reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, reason):
    log_rejected_ctrader_trade(symbol, action, entry, sl, tp1, tp2, reason)

    return {
        "ok": False,
        "broker": "ctrader",
        "mode": LIVE_ACCOUNT_STATE.get("mode", "demo"),
        "symbol": symbol,
        "action": action,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "volume": None,
        "reason": reason,
        "message": reason
    }

def validate_live_trade_risk_reward(symbol, action, entry, sl, tp2):
    minimum_rr, maximum_rr = get_configured_rr_window()
    try:
        entry_value = float(entry)
        sl_value = float(sl)
        tp2_value = float(tp2)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Entry, SL, and TP2 must be valid numbers",
            "risk_reward_ratio": None,
        }

    if not all(math.isfinite(value) for value in [entry_value, sl_value, tp2_value]):
        return {
            "ok": False,
            "reason": "Entry, SL, and TP2 must be finite real numbers",
            "risk_reward_ratio": None,
        }

    side = str(action or "").upper()
    if side == "BUY":
        risk_distance = entry_value - sl_value
        reward_distance = tp2_value - entry_value
    elif side == "SELL":
        risk_distance = sl_value - entry_value
        reward_distance = entry_value - tp2_value
    else:
        return {
            "ok": False,
            "reason": "Action must be BUY or SELL",
            "risk_reward_ratio": None,
        }

    if risk_distance <= 0 or reward_distance <= 0:
        return {
            "ok": False,
            "reason": "LIVE BLOCKED: invalid SL/TP direction.",
            "risk_reward_ratio": None,
        }

    risk_reward = reward_distance / risk_distance
    details = {
        "ok": True,
        "symbol": normalize_symbol(symbol),
        "action": side,
        "risk_distance": risk_distance,
        "reward_distance": reward_distance,
        "risk_reward_ratio": round(risk_reward, 4),
        "minimum_risk_reward": minimum_rr,
        "maximum_risk_reward": maximum_rr,
    }

    if risk_reward < minimum_rr - 1e-9:
        return {
            **details,
            "ok": False,
            "reason": (
                "LIVE BLOCKED: TP2 reward must be at least "
                f"1:{minimum_rr:.2f} before execution."
            ),
        }

    if risk_reward > maximum_rr + 1e-9:
        return {
            **details,
            "ok": False,
            "reason": (
                "LIVE BLOCKED: TP2 reward must stay at or below "
                f"1:{maximum_rr:.2f} before execution."
            ),
        }

    return details

def get_live_trade_status(trade):
    if not isinstance(trade, dict):
        return ""

    status = trade.get("status") or trade.get("result")

    if isinstance(status, dict):
        status = status.get("status") or status.get("result")

    return str(status or "").upper()

def mark_signal_history_broker_closed(symbol):
    try:
        from brain import SIGNAL_HISTORY
    except Exception as e:
        print("LIVE SIGNAL HISTORY BROKER_CLOSE UPDATE ERROR:", e)
        return

    normalized_symbol = normalize_symbol(symbol)
    signal_symbols = {normalized_symbol}

    for trade in SIGNAL_HISTORY:
        if not isinstance(trade, dict):
            continue

        trade_symbol = normalize_symbol(trade.get("symbol"))

        if trade_symbol not in signal_symbols:
            continue

        if str(trade.get("result") or "").upper() == "RUNNING":
            trade["result"] = "BROKER_CLOSED"

def build_broker_closed_trade(trade, closed_at):
    pnl, pnl_source = extract_broker_realized_pl(trade)

    if pnl is None:
        pnl = 0
        pnl_source = "missing"

    realized_result = get_trade_result_from_pnl(pnl)
    if trade.get("tp2_hit") or str(trade.get("result") or "").upper() == "TP2 HIT":
        realized_result = "WIN"
    elif trade.get("hit_tp1") or str(trade.get("result") or "").upper() == "TP1 HIT":
        realized_result = "WIN" if pnl >= 0 else realized_result

    closed_trade = {
        **trade,
        "status": realized_result,
        "result": realized_result,
        "broker_close_status": "BROKER_CLOSED",
        "pnl": pnl,
        "profit": pnl,
        "broker_pnl": pnl,
        "broker_realized_profit": pnl,
        "broker_realized_source": pnl_source,
        "broker_pnl_source": pnl_source,
        "closed_at": closed_at,
        "closed_reason": (
            "TP2 hit"
            if trade.get("tp2_hit") or str(trade.get("result") or "").upper() == "TP2 HIT"
            else trade.get("closed_reason") or "Broker position closed"
        ),
        "note": "Broker position no longer found in cTrader open positions."
    }
    executed_snapshot = closed_trade.pop("executed_trade_setup_snapshot", None)
    if executed_snapshot_matches_trade(executed_snapshot, trade):
        closed_trade["archived_executed_trade_setup_snapshot"] = executed_snapshot
        closed_trade["executed_trade_setup_snapshot_archived_at"] = closed_at
    ensure_live_trade_identity(closed_trade)
    log_live_trade_audit("broker_closed_build", closed_trade, reason=closed_trade.get("note"))
    return closed_trade

def apply_broker_closed_to_panel_signal_history(data):
    if not isinstance(data, dict):
        return

    history = data.get("history")

    if not isinstance(history, list):
        return

    broker_closed_symbols = {
        normalize_symbol(trade.get("symbol"))
        for trade in LIVE_TRADE_HISTORY
        if isinstance(trade, dict)
        and get_live_trade_status(trade) == "BROKER_CLOSED"
        and not LIVE_ACTIVE_ORDERS.get(normalize_symbol(trade.get("symbol")))
    }

    if not broker_closed_symbols:
        return

    for item in history:
        if not isinstance(item, dict):
            continue

        item_symbol = normalize_symbol(item.get("symbol"))

        if item_symbol not in broker_closed_symbols:
            continue

        if str(item.get("result") or "").upper() == "RUNNING":
            item["result"] = "BROKER_CLOSED"

def reject_live_execution_block(symbol, action, trade_payload, reason, log_message, details=None):
    print(log_message)

    rejected = reject_ctrader_order(
        symbol,
        action,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp1"),
        trade_payload.get("tp2"),
        reason
    )
    if isinstance(details, dict):
        rejected["details"] = details
        rejected["live_risk_debug"] = details.get("live_risk_debug") or details
    return rejected

def get_required_market_health_keys(symbol):
    normalized = normalize_symbol(symbol)
    prefix = "XAUUSD" if normalized == "XAUUSD" else "EURUSD"
    return [
        f"{prefix}:1h",
        f"{prefix}:15min",
        f"{prefix}:5min",
    ]

def check_live_market_data_health(symbol):
    try:
        from brain import get_ctrader_data_health

        health = get_ctrader_data_health()
    except Exception as e:
        print("LIVE EXECUTION BLOCKED: market data unhealthy", e)
        return {
            "ok": False,
            "reason": "Market data is stale/unhealthy"
        }

    required_keys = get_required_market_health_keys(symbol)
    stale_keys = health.get("stale_keys") or []
    missing_or_stale_keys = [
        key for key in required_keys
        if key in stale_keys
    ]

    if health.get("data_health") != "OK" or missing_or_stale_keys:
        print(
            "LIVE EXECUTION BLOCKED: market data unhealthy",
            {
                "symbol": normalize_symbol(symbol),
                "data_health": health.get("data_health"),
                "required_keys": required_keys,
                "missing_or_stale_keys": missing_or_stale_keys,
                "stale_keys": stale_keys,
            }
        )

        return {
            "ok": False,
            "reason": "cTrader candles stale" if missing_or_stale_keys else "cTrader candles unavailable",
            "health": health
        }

    return {
        "ok": True,
        "health": health
    }

def log_auto_trade_blocked_reason(
    symbol=None,
    signal=None,
    stage=None,
    reason=None,
    details=None
):
    print("AUTO_TRADE_BLOCKED_REASON =", {
        "symbol": normalize_symbol(symbol) if symbol else None,
        "signal": signal,
        "stage": stage,
        "reason": reason,
        "details": details,
        "live_auto_enabled": LIVE_AUTO_TRADE_ENABLED.get("enabled"),
        "broker_connected": LIVE_ACCOUNT_STATE.get("connected"),
        "active_trade": LIVE_ACTIVE_ORDERS.get(normalize_symbol(symbol)) if symbol else None,
        "timestamp": time.time(),
    })

def log_broker_min_distance_decision(
    symbol,
    distance_details=None,
    actual_blocked=False,
    final_block_reason=None
):
    details = distance_details if isinstance(distance_details, dict) else {}
    failed_fields = details.get("failed_distance_fields")

    if isinstance(failed_fields, list):
        failed_fields = [field for field in failed_fields if field]
    elif failed_fields:
        failed_fields = [failed_fields]
    else:
        failed_fields = []

    min_required = details.get("broker_minimum_distance_pips")
    sl_distance = details.get("sl_distance_pips")
    tp1_distance = details.get("tp1_distance_pips") or details.get("tp_distance_pips")
    tp2_distance = details.get("tp2_distance_pips")
    should_block_for_distance = bool(failed_fields)

    try:
        min_value = float(min_required)
        distances = [
            float(value)
            for value in [sl_distance, tp1_distance, tp2_distance]
            if value is not None
        ]

        if distances and any(distance < min_value for distance in distances):
            should_block_for_distance = True
    except (TypeError, ValueError):
        pass

    print("BROKER_MIN_DISTANCE_DECISION_DEBUG =", {
        "symbol": normalize_symbol(symbol) if symbol else None,
        "min_required_pips": min_required,
        "sl_distance_pips": sl_distance,
        "tp1_distance_pips": tp1_distance,
        "tp2_distance_pips": tp2_distance,
        "failed_distance_fields": failed_fields,
        "should_block_for_distance": should_block_for_distance,
        "actual_blocked": bool(actual_blocked),
        "final_block_reason": final_block_reason,
    })

    return should_block_for_distance

def round_down_to_step(value, step):
    try:
        numeric = float(value)
        numeric_step = float(step)
    except (TypeError, ValueError):
        return 0

    if numeric <= 0 or numeric_step <= 0:
        return 0

    return math.floor(numeric / numeric_step) * numeric_step

def round_volume_down_to_step(volume_units, step_units):
    return risk_round_volume_down_to_step(volume_units, step_units)

def log_volume_safety_debug(details):
    if not isinstance(details, dict):
        return

    pip_size = details.get("pip_size")
    stop_loss_pips = details.get("sl_pips")
    stop_loss_price_distance = details.get("stop_loss_price_distance")

    if stop_loss_price_distance is None:
        try:
            stop_loss_price_distance = float(stop_loss_pips) * float(pip_size)
        except (TypeError, ValueError):
            stop_loss_price_distance = None

    print("VOLUME_SAFETY_DEBUG =", {
        "symbol": details.get("symbol"),
        "risk_percent": details.get("risk_percent"),
        "stop_loss_pips": stop_loss_pips,
        "stop_loss_price_distance": (
            round(stop_loss_price_distance, 8)
            if isinstance(stop_loss_price_distance, (int, float))
            else stop_loss_price_distance
        ),
        "lot_size_before_rounding": (
            details.get("calculated_lots")
            if details.get("calculated_lots") is not None
            else details.get("raw_lots")
        ),
        "lot_size_after_rounding": details.get("lot_size") or details.get("rounded_lots"),
        "broker_min_volume": details.get("min_volume_units"),
        "broker_max_volume": details.get("max_volume_units"),
        "broker_volume_step": details.get("volume_step_units"),
        "final_volume": details.get("volume_units") or details.get("calculated_volume_units"),
        "blocked_reason": None if details.get("ok") else details.get("reason"),
    })

def build_live_block_details(*parts):
    merged = {}

    for part in parts:
        if isinstance(part, dict):
            merged.update(part)

    return merged

def log_live_execution_safety_check(details):
    log_volume_safety_debug(details)
    print(
        "LIVE EXECUTION SAFETY CHECK:",
        {
            "symbol": details.get("symbol"),
            "account_balance": details.get("account_balance"),
            "risk_percent": details.get("risk_percent"),
            "risk_money": details.get("risk_amount"),
            "stop_loss_pips": details.get("sl_pips"),
            "pip_value_per_1_lot": details.get("pip_value_per_lot"),
            "calculated_lot_size": (
                details.get("calculated_lots")
                if details.get("calculated_lots") is not None
                else details.get("raw_lots")
            ),
            "final_lot_size": details.get("lot_size"),
            "broker_lot_size": details.get("lot_contract_size"),
            "final_ctrader_volume_units": details.get("volume_units"),
            "broker_min_volume": details.get("min_volume_units"),
            "broker_max_volume": details.get("max_volume_units"),
            "broker_step_volume": details.get("volume_step_units"),
            "final_risk_money": details.get("final_risk_amount"),
            "final_risk_percent": details.get("final_risk_percent"),
            "requested_units": details.get("requested_units") or details.get("volume_units"),
            "max_lot_cap_used": details.get("max_lot_cap_used"),
            "broker_max_volume": details.get("max_volume_units"),
            "risk_difference": details.get("risk_difference"),
            "allowed_risk_difference": details.get("allowed_risk_difference"),
            "ok": details.get("ok"),
            "reason": details.get("reason"),
        }
    )

def build_live_risk_debug(
    symbol,
    side,
    trade_payload=None,
    risk_size=None,
    broker_reject_reason=None,
):
    trade_payload = trade_payload or {}
    risk_size = risk_size or {}
    metadata = risk_size.get("symbol_metadata") or {}
    min_volume_units = (
        risk_size.get("min_volume_units")
        or metadata.get("min_volume_units")
    )
    lot_contract_size = (
        risk_size.get("lot_contract_size")
        or metadata.get("lot_size")
        or get_default_broker_lot_size(symbol)
    )
    volume_units = risk_size.get("volume_units")
    volume_step_units = (
        risk_size.get("volume_step_units")
        or metadata.get("volume_step_units")
    )
    payload_scale = CTRADER_PAYLOAD_VOLUME_SCALE.get(normalize_symbol(symbol), 1)

    try:
        broker_min_lots = convert_ctrader_volume_to_lots(
            min_volume_units,
            lot_contract_size,
        )
    except Exception:
        broker_min_lots = None

    try:
        payload_volume = int(float(volume_units) * payload_scale)
    except (TypeError, ValueError):
        payload_volume = None

    debug = {
        "symbol": normalize_symbol(symbol),
        "side": str(side or "").upper(),
        "account_id": LIVE_ACCOUNT_STATE.get("account_id"),
        "account_balance": risk_size.get("account_balance"),
        "account_equity": risk_size.get("account_equity"),
        "risk_percent": risk_size.get("risk_percent"),
        "allowed_risk_percent": (
            risk_size.get("allowed_risk_percent")
            or risk_size.get("maximum_allowed_risk_percent")
        ),
        "risk_amount": risk_size.get("risk_amount"),
        "risk_money": risk_size.get("risk_amount"),
        "entry": trade_payload.get("entry"),
        "sl": trade_payload.get("sl"),
        "tp1": trade_payload.get("tp1"),
        "tp2": trade_payload.get("tp2"),
        "sl_distance_pips": risk_size.get("sl_pips"),
        "sl_pips": risk_size.get("sl_pips"),
        "pip_value": risk_size.get("pip_value_per_lot"),
        "pip_value_per_lot": risk_size.get("pip_value_per_lot"),
        "broker_min_lots": broker_min_lots,
        "broker_min_volume": min_volume_units,
        "broker_max_volume": (
            risk_size.get("max_volume_units")
            or metadata.get("max_volume_units")
        ),
        "broker_volume_step": volume_step_units,
        "volume_step": volume_step_units,
        "calculated_volume": (
            risk_size.get("calculated_volume_units")
            if risk_size.get("calculated_volume_units") is not None
            else risk_size.get("raw_volume_units")
        ),
        "calculated_lots": (
            risk_size.get("calculated_lots")
            if risk_size.get("calculated_lots") is not None
            else risk_size.get("raw_lots")
        ),
        "rounded_lots": (
            risk_size.get("rounded_lots")
            if risk_size.get("rounded_lots") is not None
            else risk_size.get("lot_size")
        ),
        "final_volume": volume_units,
        "requested_units": volume_units,
        "payload_volume": payload_volume,
        "final_risk_percent": risk_size.get("final_risk_percent"),
        "max_lot_cap_used": bool(risk_size.get("max_lot_cap_used", False)),
        "broker_reject_reason": broker_reject_reason,
    }
    print("LIVE_RISK_DEBUG =", debug)
    return debug

def calculate_position_size(symbol, account_balance, risk_percent, stop_loss_pips):
    execution_symbol = normalize_symbol(symbol)
    metadata = get_ctrader_symbol_risk_metadata(execution_symbol)
    result = risk_calculate_position_size(
        execution_symbol,
        account_balance,
        risk_percent,
        stop_loss_pips,
        metadata,
        convert_lots_to_volume=convert_lots_to_ctrader_volume,
        convert_volume_to_lots=convert_ctrader_volume_to_lots,
        payload_volume_scale=CTRADER_PAYLOAD_VOLUME_SCALE,
        default_lot_size=get_default_broker_lot_size(execution_symbol),
        risk_tolerance_percent=RISK_TOLERANCE_PERCENT,
        maximum_allowed_risk_percent=get_maximum_allowed_live_risk_percent(risk_percent),
    )

    if result.get("ok"):
        print(
            "LIVE POSITION SIZE:",
            {
                "symbol": result["symbol"],
                "balance": result["account_balance"],
                "risk_money": result["risk_amount"],
                "stop_loss_pips": result["sl_pips"],
                "pip_value": result["pip_value_per_lot"],
                "calculated_lots": result.get("calculated_lots"),
                "ctrader_volume_units": result["volume_units"],
                "raw_volume_units": result["raw_volume_units"],
                "min_volume_units": result["min_volume_units"],
                "max_volume_units": result["max_volume_units"],
                "volume_step_units": result["volume_step_units"],
                "lot_size": result["lot_contract_size"],
                "metadata_source": result["metadata_source"],
            }
        )
    log_live_execution_safety_check(result)
    return result

def log_position_size_example(symbol, account_balance=10000, risk_percent=0.5, stop_loss_pips=10):
    result = calculate_position_size(
        symbol,
        account_balance,
        risk_percent,
        stop_loss_pips
    )

    print(
        "LIVE POSITION SIZE EXAMPLE:",
        {
            "symbol": normalize_symbol(symbol),
            "balance": account_balance,
            "risk_percent": risk_percent,
            "stop_loss_pips": stop_loss_pips,
            "ok": result.get("ok"),
            "reason": result.get("reason"),
            "pip_value_used": result.get("pip_value_per_lot"),
            "calculated_lot": result.get("lot_size"),
            "ctrader_volume_units": result.get("volume_units"),
            "broker_min_volume": (
                result.get("min_volume_units")
                or result.get("symbol_metadata", {}).get("min_volume_units")
            ),
            "broker_max_volume": (
                result.get("max_volume_units")
                or result.get("symbol_metadata", {}).get("max_volume_units")
            ),
            "broker_volume_step": (
                result.get("volume_step_units")
                or result.get("symbol_metadata", {}).get("volume_step_units")
            ),
            "broker_lot_size": (
                result.get("lot_contract_size")
                or result.get("symbol_metadata", {}).get("lot_size")
            ),
        }
    )

    return result

def calculate_live_risk_size(symbol, entry, sl):
    execution_symbol = normalize_symbol(symbol)

    try:
        entry_value = float(entry)
        sl_value = float(sl)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Cannot calculate risk without valid SL"
        }

    sl_distance = abs(entry_value - sl_value)

    if sl_distance <= 0:
        return {
            "ok": False,
            "reason": "Cannot calculate risk without valid SL"
        }

    account = get_ctrader_account_snapshot()
    account_verification = validate_verified_account_snapshot(account)
    if not account_verification.get("ok"):
        risk_log_account_balance_verification_failed(
            execution_symbol,
            account,
            reason=account_verification.get("account_verification_reason"),
        )
        return account_verification

    balance = account_verification.get("balance")
    account_value = account_verification.get("account_equity_used")

    metadata = get_ctrader_symbol_risk_metadata(execution_symbol)

    if not metadata.get("ok"):
        return {
            "ok": False,
            "reason": metadata.get("reason") or "Cannot calculate risk without broker symbol metadata",
            "symbol_metadata": metadata,
        }

    pip_size = metadata.get("pip_size")

    try:
        pip_size = float(pip_size)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Cannot calculate risk without broker symbol metadata",
            "symbol_metadata": metadata,
        }

    if pip_size <= 0:
        return {
            "ok": False,
            "reason": "Cannot calculate risk without broker symbol metadata",
            "symbol_metadata": metadata,
        }

    sl_pips = sl_distance / pip_size

    print("LIVE_SL_PIP_DISTANCE_CHECK:", {
        "symbol": execution_symbol,
        "entry": entry_value,
        "sl": sl_value,
        "sl_distance_price": round(sl_distance, 5),
        "pip_size": pip_size,
        "sl_pips": round(sl_pips, 2),
        "metadata_source": metadata.get("metadata_source"),
        "pip_position": metadata.get("pip_position"),
    })

    configured_risk_percent = get_configured_live_risk_percent()
    position_size = calculate_position_size(
        execution_symbol,
        account_value,
        configured_risk_percent,
        sl_pips
    )
    position_size["account_balance"] = balance
    position_size["account_equity"] = account.get("equity")
    position_size["account_equity_used"] = account_value
    print("ACCOUNT_EQUITY_USED", account_value)
    print("RISK_PERCENT_USED", configured_risk_percent)
    print("SL_DISTANCE_USED", {
        "price": round(sl_distance, 8),
        "pips": round(sl_pips, 2),
    })
    print("CALCULATED_VOLUME", {
        "lots": position_size.get("calculated_lots"),
        "volume_units": position_size.get("raw_volume_units"),
    })
    print("FINAL_VOLUME_SENT", {
        "lots": position_size.get("lot_size"),
        "volume_units": position_size.get("volume_units"),
    })
    print("LIVE_RISK_POSITION_SIZING_AUDIT", {
        "symbol": execution_symbol,
        "account_equity": position_size.get("account_equity"),
        "account_equity_used": position_size.get("account_equity_used"),
        "risk_percent": position_size.get("risk_percent"),
        "max_risk_dollars": (
            position_size.get("max_risk_dollars")
            or position_size.get("risk_amount")
        ),
        "sl_pips": position_size.get("sl_pips"),
        "pip_value_per_1_lot": position_size.get("pip_value_per_lot"),
        "raw_lot": (
            position_size.get("raw_lot")
            if position_size.get("raw_lot") is not None
            else position_size.get("raw_lots")
        ),
        "rounded_lot": (
            position_size.get("rounded_lot")
            if position_size.get("rounded_lot") is not None
            else position_size.get("lot_size")
        ),
        "raw_volume_units": position_size.get("raw_volume_units"),
        "rounded_volume_units": position_size.get("volume_units"),
        "broker_min_volume_units": position_size.get("min_volume_units"),
        "broker_volume_step_units": position_size.get("volume_step_units"),
        "expected_risk_percent": (
            position_size.get("expected_risk_percent")
            if position_size.get("expected_risk_percent") is not None
            else position_size.get("final_risk_percent")
        ),
        "reason_if_blocked": (
            None
            if position_size.get("ok")
            else (
                position_size.get("reason_if_blocked")
                or position_size.get("reason")
            )
        ),
    })
    return position_size

def log_xauusd_live_risk_diagnostics(symbol, trade_payload, risk_size):
    execution_symbol = normalize_symbol(symbol)

    if execution_symbol != "XAUUSD":
        return

    trade_payload = trade_payload or {}
    risk_size = risk_size or {}

    try:
        entry_value = float(trade_payload.get("entry"))
        sl_value = float(trade_payload.get("sl"))
        sl_distance_price = abs(entry_value - sl_value)
    except (TypeError, ValueError):
        entry_value = trade_payload.get("entry")
        sl_value = trade_payload.get("sl")
        sl_distance_price = risk_size.get("stop_loss_price_distance")

    account_balance = risk_size.get("account_balance")
    account_value_used = risk_size.get("account_equity_used")

    if account_value_used is None:
        account_value_used = account_balance

    configured_risk_percent = risk_size.get(
        "risk_percent",
        get_configured_live_risk_percent()
    )

    try:
        expected_dollar_risk = (
            float(account_value_used)
            * (float(configured_risk_percent) / 100)
        )
    except (TypeError, ValueError):
        expected_dollar_risk = risk_size.get("risk_amount")

    final_risk_percent = risk_size.get("final_risk_percent")

    try:
        would_exceed_one_percent = float(final_risk_percent) > 1.0
    except (TypeError, ValueError):
        would_exceed_one_percent = None

    diagnostics = {
        "symbol": execution_symbol,
        "account_balance": account_balance,
        "configured_risk_percent": configured_risk_percent,
        "expected_dollar_risk": (
            round(expected_dollar_risk, 2)
            if isinstance(expected_dollar_risk, (int, float))
            else expected_dollar_risk
        ),
        "entry": entry_value,
        "stop_loss": sl_value,
        "sl_distance_price": (
            round(sl_distance_price, 8)
            if isinstance(sl_distance_price, (int, float))
            else sl_distance_price
        ),
        "pip_size": risk_size.get("pip_size"),
        "sl_pips": risk_size.get("sl_pips"),
        "pip_value_per_lot": risk_size.get("pip_value_per_lot"),
        "raw_lot_size": (
            risk_size.get("raw_lots")
            if risk_size.get("raw_lots") is not None
            else risk_size.get("calculated_lots")
        ),
        "rounded_lot_size": (
            risk_size.get("rounded_lots")
            if risk_size.get("rounded_lots") is not None
            else risk_size.get("lot_size")
        ),
        "min_volume_units": risk_size.get("min_volume_units"),
        "volume_step_units": risk_size.get("volume_step_units"),
        "final_volume_units": risk_size.get("volume_units"),
        "ctrader_payload_volume": risk_size.get("payload_volume"),
        "backend_estimated_dollar_risk": risk_size.get("final_risk_amount"),
        "backend_estimated_risk_percent": final_risk_percent,
        "allowed_max_risk_percent": (
            risk_size.get("maximum_allowed_risk_percent")
            or risk_size.get("allowed_risk_percent")
        ),
        "would_exceed_1_percent": would_exceed_one_percent,
    }

    print("XAUUSD_LIVE_RISK_DIAGNOSTICS_START")
    print(json.dumps(diagnostics, indent=2, default=str))
    print("XAUUSD_LIVE_RISK_DIAGNOSTICS_END")

def build_minimum_live_size(symbol, entry, sl, failed_risk_size=None):
    execution_symbol = normalize_symbol(symbol)
    metadata = get_ctrader_symbol_risk_metadata(execution_symbol)
    failed_risk_size = failed_risk_size or {}
    lot_contract_size = (
        metadata.get("lot_size")
        or get_default_broker_lot_size(execution_symbol)
    )
    min_volume_units = metadata.get("min_volume_units")

    try:
        lot_contract_size = float(lot_contract_size)
    except (TypeError, ValueError):
        lot_contract_size = float(get_default_broker_lot_size(execution_symbol))

    try:
        min_volume_units = int(float(min_volume_units))
    except (TypeError, ValueError):
        min_volume_units = convert_lots_to_ctrader_volume(
            MIN_LOT,
            lot_contract_size
        )

    lot_size = convert_ctrader_volume_to_lots(
        min_volume_units,
        lot_contract_size
    ) or MIN_LOT

    try:
        pip_size = float(metadata.get("pip_size"))
        sl_pips = abs(float(entry) - float(sl)) / pip_size
    except (TypeError, ValueError, ZeroDivisionError):
        pip_size = metadata.get("pip_size")
        sl_pips = None

    account_balance = failed_risk_size.get("account_balance")
    account_equity = failed_risk_size.get("account_equity")

    if account_balance is None:
        account = get_ctrader_account_snapshot()

        if account.get("ok"):
            account_balance = account.get("balance") or account.get("equity")
            account_equity = account.get("equity")

    account_value = (
        account_equity
        if account_equity is not None
        else account_balance
    )

    try:
        account_value = float(account_value)
    except (TypeError, ValueError):
        account_value = 0

    try:
        account_balance_value = float(account_balance)
    except (TypeError, ValueError):
        account_balance_value = account_value

    try:
        pip_value_per_lot = float(metadata.get("pip_value_per_lot"))
    except (TypeError, ValueError):
        pip_value_per_lot = 0

    final_risk_amount = None
    final_risk_percent = None

    if (
        account_value > 0
        and sl_pips is not None
        and sl_pips > 0
        and pip_value_per_lot > 0
    ):
        final_risk_amount = float(lot_size) * sl_pips * pip_value_per_lot
        final_risk_percent = (final_risk_amount / account_value) * 100

    if final_risk_percent is None:
        fallback = {
            "ok": False,
            "symbol": execution_symbol,
            "reason": "Cannot validate broker minimum volume risk",
            "minimum_volume_fallback": True,
            "risk_sizing_failure": failed_risk_size,
            "symbol_metadata": metadata,
        }
        print("LIVE_MINIMUM_VOLUME_FALLBACK_BLOCKED =", fallback)
        return fallback

    configured_risk_percent = get_configured_live_risk_percent()
    maximum_allowed_risk_percent = get_maximum_allowed_live_risk_percent(
        configured_risk_percent
    )
    risk_difference = final_risk_percent - maximum_allowed_risk_percent
    risk_decision = (
        "BLOCK"
        if final_risk_percent > maximum_allowed_risk_percent + RISK_TOLERANCE_PERCENT
        else "ALLOW"
    )
    print("RISK_VALIDATION =", {
        "symbol": execution_symbol,
        "actual_risk": round(final_risk_percent, 4),
        "max_risk": maximum_allowed_risk_percent,
        "tolerance": RISK_TOLERANCE_PERCENT,
        "difference": round(risk_difference, 4),
        "decision": risk_decision,
    })

    if risk_decision == "BLOCK":
        fallback = {
            "ok": False,
            "symbol": execution_symbol,
            "reason": (
                "LIVE BLOCKED: calculated volume below broker minimum and "
                "min volume exceeds allowed risk."
            ),
            "lot_size": round(float(lot_size), 4),
            "volume_units": int(min_volume_units),
            "risk_percent": configured_risk_percent,
            "final_risk_amount": round(final_risk_amount, 2),
            "final_risk_percent": round(final_risk_percent, 4),
            "maximum_allowed_risk_percent": maximum_allowed_risk_percent,
            "allowed_risk_percent": maximum_allowed_risk_percent,
            "risk_tolerance_percent": RISK_TOLERANCE_PERCENT,
            "minimum_volume_fallback": True,
            "risk_sizing_failure": failed_risk_size,
            "symbol_metadata": metadata,
        }
        print("LIVE_MINIMUM_VOLUME_FALLBACK_BLOCKED =", fallback)
        return fallback

    fallback = {
        "ok": True,
        "symbol": execution_symbol,
        "lot_size": round(float(lot_size), 4),
        "volume_units": int(min_volume_units),
        "risk_percent": configured_risk_percent,
        "risk_amount": round(account_value * (configured_risk_percent / 100), 2),
        "account_balance": round(account_balance_value, 2),
        "account_equity": account_equity,
        "account_equity_used": account_value,
        "sl_pips": round(sl_pips, 2) if sl_pips is not None else None,
        "pip_size": pip_size,
        "pip_value_per_lot": pip_value_per_lot,
        "lot_contract_size": lot_contract_size,
        "min_volume_units": int(min_volume_units),
        "max_volume_units": metadata.get("max_volume_units"),
        "volume_step_units": metadata.get("volume_step_units"),
        "final_risk_amount": round(final_risk_amount, 2),
        "final_risk_percent": round(final_risk_percent, 4),
        "maximum_allowed_risk_percent": maximum_allowed_risk_percent,
        "allowed_risk_percent": maximum_allowed_risk_percent,
        "risk_tolerance_percent": RISK_TOLERANCE_PERCENT,
        "symbol_metadata": metadata,
        "minimum_volume_fallback": True,
        "risk_sizing_failure": failed_risk_size,
    }
    print("LIVE_MINIMUM_VOLUME_FALLBACK =", fallback)
    return fallback

def normalize_broker_volume_to_lots(volume, symbol=None):
    try:
        numeric = float(volume)
    except (TypeError, ValueError):
        return None

    if numeric <= 0:
        return None

    if symbol and numeric >= 1:
        lots = convert_ctrader_volume_to_lots(
            numeric,
            get_default_broker_lot_size(symbol)
        )
        return round(lots, 2) if lots is not None else None

    if numeric >= 1000:
        lots = convert_ctrader_volume_to_lots(numeric)
        return round(lots, 2) if lots is not None else None

    return round(numeric, 2)

def is_not_enough_money_result(result):
    text = json.dumps(result, default=str).upper()
    return "NOT_ENOUGH_MONEY" in text or "NOT ENOUGH FUNDS" in text

def calculate_retry_lot_size(current_lot, step):
    reduced = round_down_to_step(float(current_lot) / 2, step or MIN_LOT)

    if reduced < MIN_LOT:
        return None

    return round(reduced, 2)

def get_panel_trade_plan(panel_data, symbol):
    if not isinstance(panel_data, dict):
        return None

    execution_symbol = normalize_symbol(symbol)

    return panel_data.get(execution_symbol)


def get_news_market_context(panel_data, symbol, side=None):
    data = panel_data if isinstance(panel_data, dict) else PANEL_CACHE.get("data")
    symbol = normalize_symbol(symbol)
    candles = ((data or {}).get("candles") or {}).get(symbol) or {}
    try:
        tick = ((get_live_prices() or {}).get("live_prices") or {}).get(symbol) or {}
    except Exception:
        tick = {}
    side = str(side or "").upper()
    entry_price = (
        tick.get("ask") if side == "BUY"
        else tick.get("bid") if side == "SELL"
        else tick.get("mid") or tick.get("bid") or tick.get("ask")
    )
    return {
        "candles_5m": candles.get("5m") or [],
        "candles_15m": candles.get("15m") or [],
        "entry_price": entry_price,
        "tick": tick,
    }


def evaluate_news_entry_state(panel_data, symbol, side=None, audit=False):
    context = get_news_market_context(panel_data, symbol, side=side)
    if news_trading.get_mode() == "OFF":
        decision = news_trading.evaluate([], symbol)
        return {**decision, "market_context": context, "events": []}
    try:
        events = fetch_calendar_events()
        selected = news_trading.select_active_event(
            events,
            symbol,
            datetime.now(timezone.utc),
        )
        if selected:
            refreshed = refresh_actual_after_release(selected, symbol)
            if refreshed is not selected:
                selected_key = news_trading.event_id(selected)
                events = [
                    refreshed
                    if news_trading.event_id(item) == selected_key
                    else item
                    for item in events
                ]
        decision = news_trading.evaluate(
            events,
            symbol,
            candles_5m=context["candles_5m"],
            candles_15m=context["candles_15m"],
            entry_price=context["entry_price"],
            data_age_seconds=get_calendar_data_age_seconds(),
            audit=audit,
        )
    except Exception as exc:
        decision = {
            "mode": news_trading.get_mode(),
            "phase": "RELEASE_LOCK",
            "authoritative_status": "NEWS BLOCK",
            "allow_normal_entry": False,
            "allow_news_entry": False,
            "blocking_reason": "WAIT_NEWS_STATUS_UNAVAILABLE",
            "error": str(exc),
        }
    return {**decision, "market_context": context, "events": events if "events" in locals() else []}


def normal_plan_is_fresh_after_news(plan, decision):
    fresh_after = decision.get("normal_fresh_after")
    if not fresh_after:
        return True
    try:
        watermark = news_trading.parse_time(fresh_after)
        bos_close = news_trading.parse_time(
            plan.get("fifteen_m_break_close_time")
            or ((plan.get("fifteen_m_swing_break") or {}).get("break_close_time"))
        )
        confirmation_close = news_trading.parse_time(
            plan.get("five_m_closed_candle_time")
            or ((plan.get("confirmation_5m") or {}).get("confirmation_close_time"))
        )
    except Exception:
        return False
    return bool(
        watermark
        and bos_close
        and confirmation_close
        and bos_close > watermark
        and confirmation_close > bos_close
    )


def get_plan_execution_metadata(plan, side=None):
    plan = plan if isinstance(plan, dict) else {}
    breakout = (
        plan.get("fifteen_m_swing_break")
        if isinstance(plan.get("fifteen_m_swing_break"), dict)
        else {}
    )
    confirmation = (
        plan.get("confirmation_5m")
        if isinstance(plan.get("confirmation_5m"), dict)
        else {}
    )
    return {
        "signal_setup_id": get_signal_setup_id(plan, side),
        "source_indicator_event_id": (
            plan.get("source_indicator_event_id")
            or breakout.get("indicator_event_id")
        ),
        "indicator_event_identity": copy.deepcopy(
            plan.get("indicator_event_identity")
            or breakout.get("indicator_event_identity")
            or {}
        ),
        "m5_confirmation_id": (
            plan.get("m5_confirmation_id")
            or confirmation.get("confirmation_id")
        ),
        "m5_confirmation_identity": copy.deepcopy(
            plan.get("m5_confirmation_identity")
            or confirmation.get("confirmation_identity")
            or {}
        ),
        "fifteen_m_break_time": (
            plan.get("fifteen_m_break_time")
            or breakout.get("break_time")
        ),
        "fifteen_m_break_close_time": (
            plan.get("fifteen_m_break_close_time")
            or breakout.get("break_close_time")
        ),
        "five_m_confirmation_close_time": (
            plan.get("five_m_closed_candle_time")
            or confirmation.get("confirmation_close_time")
        ),
        "trend_15m": copy.deepcopy(plan.get("trend_15m") or {}),
        "setup_identity": copy.deepcopy(plan.get("setup_identity") or {}),
    }

def get_paper_trade_for_live_symbol(panel_data, symbol):
    if not isinstance(panel_data, dict):
        return None

    execution_symbol = normalize_symbol(symbol)
    paper_trades = panel_data.get("paper_trades") or {}

    return paper_trades.get(execution_symbol)

def paper_would_enter_from_plan(panel_data, symbol, plan):
    if not isinstance(plan, dict):
        return False

    execution_symbol = normalize_symbol(symbol)
    signal = str(plan.get("signal") or "WAIT").upper()

    if signal not in ["BUY", "SELL"]:
        return False

    entry = plan.get("entry_price")
    sl = plan.get("stop_loss")
    tp1 = plan.get("tp1")
    tp2 = plan.get("tp2")

    if any(is_missing_trade_value(value) for value in [entry, sl, tp1, tp2]):
        return False

    try:
        normalized = normalize_trade_levels(
            execution_symbol,
            signal,
            entry,
            sl,
            tp1,
            tp2
        )
    except Exception:
        return False

    return bool(normalized.get("ok"))

def trade_payload_has_required_levels(trade_payload):
    if not isinstance(trade_payload, dict):
        return False, "Trade payload missing"

    side = str(
        trade_payload.get("action")
        or trade_payload.get("side")
        or trade_payload.get("signal")
        or ""
    ).upper()

    required_values = [
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp1"),
        trade_payload.get("tp2"),
    ]

    if any(is_missing_trade_value(value) for value in required_values):
        return False, "Entry, SL, TP1, and TP2 are required before execution"

    normalized = normalize_trade_levels(
        trade_payload.get("symbol"),
        side,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp1"),
        trade_payload.get("tp2"),
    )

    if not normalized.get("ok"):
        return False, "LIVE BLOCKED: invalid SL/TP distance."

    rr_validation = validate_live_trade_risk_reward(
        trade_payload.get("symbol"),
        side,
        normalized.get("entry"),
        normalized.get("sl"),
        normalized.get("tp2"),
    )

    if not rr_validation.get("ok"):
        return False, rr_validation.get("reason")

    return True, None

def log_paper_live_signal_compare(
    symbol,
    plan,
    panel_data,
    live_signal=None,
    live_blocked_by=None,
    live_blocked_reason=None,
    live_would_enter=None
):
    if not isinstance(plan, dict):
        plan = {}

    paper_signal = str(plan.get("signal") or "WAIT").upper()
    live_signal = str(live_signal or paper_signal or "WAIT").upper()
    paper_would_enter = paper_would_enter_from_plan(panel_data, symbol, plan)
    if live_would_enter is None:
        live_would_enter = (
            live_signal in ["BUY", "SELL"]
            and LIVE_AUTO_TRADE_ENABLED.get("enabled")
            and not LIVE_ACTIVE_ORDERS.get(normalize_symbol(symbol))
        )

    debug = {
        "symbol": normalize_symbol(symbol),
        "paper_signal": paper_signal,
        "live_signal": live_signal,
        "same_strategy_signal": paper_signal == live_signal,
        "paper_would_enter": paper_would_enter,
        "live_would_enter": live_would_enter,
        "live_blocked_by": live_blocked_by,
        "live_blocked_reason": live_blocked_reason,
    }

    print("PAPER_LIVE_SIGNAL_COMPARE_DEBUG =", debug)
    return debug

def log_live_xauusd_execution_debug(
    symbol,
    plan=None,
    trade_payload=None,
    risk_size=None,
    stage=None,
    blocked_by=None,
    blocked_reason=None,
    cooldown_active=None,
    existing_position=None,
    payload_valid=None,
    order_sent=False,
    order_accepted=False,
    result=None,
):
    if normalize_symbol(symbol) != "XAUUSD":
        return

    plan = plan if isinstance(plan, dict) else {}
    trade_payload = trade_payload if isinstance(trade_payload, dict) else {}
    risk_size = risk_size if isinstance(risk_size, dict) else {}
    result = result if isinstance(result, dict) else {}

    active_order = LIVE_ACTIVE_ORDERS.get("XAUUSD")

    if existing_position is None:
        existing_position = active_order

    if cooldown_active is None:
        cooldown_active = (
            time.time() - LIVE_LAST_EXECUTION_TIME.get("XAUUSD", 0)
            < LIVE_EXECUTION_COOLDOWN_SECONDS
        )

    if payload_valid is None:
        payload_valid = bool(trade_payload.get("ok")) if trade_payload else None

    strategy_signal = str(plan.get("signal") or trade_payload.get("signal") or "WAIT").upper()
    final_signal = str(trade_payload.get("signal") or plan.get("signal") or "WAIT").upper()
    metadata = risk_size.get("symbol_metadata") if isinstance(risk_size.get("symbol_metadata"), dict) else {}

    debug = {
        "stage": stage,
        "symbol": "XAUUSD",
        "strategy_signal": strategy_signal,
        "final_signal": final_signal,
        "confidence": plan.get("confidence"),
        "blocked_by": blocked_by or plan.get("blocked_by"),
        "blocked_reason": blocked_reason or plan.get("blocked_reason") or result.get("reason") or result.get("message"),
        "entry": trade_payload.get("entry") or plan.get("entry_price"),
        "sl": trade_payload.get("sl") or plan.get("stop_loss"),
        "tp1": trade_payload.get("tp1") or plan.get("tp1"),
        "risk_percent": risk_size.get("risk_percent") or trade_payload.get("risk_percent") or LIVE_RISK_PERCENT,
        "stop_loss_pips": risk_size.get("sl_pips") or trade_payload.get("sl_pips"),
        "lot_size": risk_size.get("lot_size") or trade_payload.get("lot_size") or trade_payload.get("volume"),
        "volume_units": risk_size.get("volume_units") or trade_payload.get("volume_units"),
        "broker_min_volume": risk_size.get("min_volume_units") or metadata.get("min_volume_units"),
        "broker_max_volume": risk_size.get("max_volume_units") or metadata.get("max_volume_units"),
        "volume_step": risk_size.get("volume_step_units") or metadata.get("volume_step_units"),
        "cooldown_active": bool(cooldown_active),
        "existing_position": existing_position,
        "payload_valid": payload_valid,
        "order_sent": bool(order_sent),
        "order_accepted": bool(order_accepted),
    }

    print("LIVE_XAUUSD_EXECUTION_DEBUG =", debug)
    return debug

def run_ctrader_auto_trade_checks(panel_data):
    if not isinstance(panel_data, dict):
        return []

    # The web toggle and the server-owned engine may run in different workers.
    # Re-read the authoritative preference before every execution cycle.
    refresh_auto_trade_state_from_persistence("execution_cycle")
    sync_ctrader_account_state()
    broker_connected = bool(
        LIVE_ACCOUNT_STATE.get("connected")
        and LIVE_ACCOUNT_STATE.get("execution_ready")
    )

    if not LIVE_AUTO_TRADE_ENABLED["enabled"]:
        for symbol in ["EURUSD", "XAUUSD"]:
            plan = get_panel_trade_plan(panel_data, symbol) or {}
            if str(plan.get("signal") or "WAIT").upper() in ["BUY", "SELL"]:
                handoff_ok, handoff_reason, handoff_details = (
                    inspect_strategy_panel_handoff(plan)
                )
                record_auto_execution_gate(
                    symbol,
                    plan,
                    "PANEL_HANDOFF",
                    "PASS" if handoff_ok else "BLOCK",
                    handoff_reason,
                    handoff_details,
                )
                update_execution_outcome_safely(
                    (plan.get("audit_diagnostics") or {}).get("cycle_id"),
                    "BLOCKED",
                    "LIVE_AUTO_OFF",
                    {"gate": "live_auto_disabled", "order_sent": False},
                )
        log_auto_trade_blocked_reason(
            stage="live_auto_disabled",
            reason="Live Auto is off"
        )
        return []

    results = []
    actionable_seen = False

    for symbol in ["EURUSD", "XAUUSD"]:
        plan = get_panel_trade_plan(panel_data, symbol) or {}
        initial_plan = plan
        initial_signal = str(plan.get("signal") or "WAIT").upper()
        if initial_signal in ["BUY", "SELL"]:
            handoff_ok, handoff_reason, handoff_details = (
                inspect_strategy_panel_handoff(plan)
            )
            record_auto_execution_gate(
                symbol,
                plan,
                "PANEL_HANDOFF",
                "PASS" if handoff_ok else "BLOCK",
                handoff_reason,
                handoff_details,
            )
        news_state = evaluate_news_entry_state(
            panel_data,
            symbol,
            audit=True,
        )
        if news_state.get("allow_news_entry"):
            expected_side = news_state.get("expected_symbol_direction")
            news_state = evaluate_news_entry_state(
                panel_data,
                symbol,
                side=expected_side,
                audit=True,
            )
            if news_state.get("allow_news_entry"):
                plan = news_state.get("news_plan") or plan
                if plan is not initial_plan:
                    news_signal = str(plan.get("signal") or "WAIT").upper()
                    if news_signal in ["BUY", "SELL"]:
                        handoff_ok, handoff_reason, handoff_details = (
                            inspect_strategy_panel_handoff(plan)
                        )
                        record_auto_execution_gate(
                            symbol,
                            plan,
                            "PANEL_HANDOFF",
                            "PASS" if handoff_ok else "BLOCK",
                            handoff_reason,
                            handoff_details,
                        )

        if not news_state.get("allow_news_entry") and not news_state.get(
            "allow_normal_entry", True
        ):
            reason = (
                news_state.get("blocking_reason")
                or news_state.get("authoritative_status")
                or "NEWS BLOCK"
            )
            if str(plan.get("signal") or "WAIT").upper() in ["BUY", "SELL"]:
                update_execution_outcome_safely(
                    (plan.get("audit_diagnostics") or {}).get("cycle_id"),
                    "BLOCKED",
                    reason,
                    {"gate": "authoritative_news_gate", "order_sent": False},
                )
            if news_state.get("phase") in {"PRE_NEWS", "RELEASE_LOCK"}:
                try:
                    import brain
                    brain.clear_symbol_entry_memory(
                        symbol,
                        reason,
                        closed_at=None,
                    )
                except Exception as exc:
                    print("NEWS_SETUP_INVALIDATION_ERROR =", {
                        "symbol": symbol,
                        "error": str(exc),
                    })
            set_auto_trade_status(
                symbol=symbol,
                signal="WAIT",
                action=None,
                status="BLOCKED",
                reason=reason,
                details=news_state,
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal="WAIT",
                stage="authoritative_news_gate",
                reason=reason,
                details=news_state,
            )
            results.append({
                "ok": False,
                "symbol": symbol,
                "reason": reason,
                "news_trading": news_state,
            })
            continue

        if (
            not news_state.get("allow_news_entry")
            and not normal_plan_is_fresh_after_news(plan, news_state)
        ):
            reason = "WAIT_FRESH_NORMAL_SETUP_AFTER_NEWS"
            update_execution_outcome_safely(
                (plan.get("audit_diagnostics") or {}).get("cycle_id"),
                "BLOCKED",
                reason,
                {"gate": "post_news_freshness", "order_sent": False},
            )
            set_auto_trade_status(
                symbol=symbol,
                signal="WAIT",
                action=None,
                status="BLOCKED",
                reason=reason,
                details=news_state,
            )
            results.append({"ok": False, "symbol": symbol, "reason": reason})
            continue

        signal = str(plan.get("signal") or "WAIT").upper()
        broker_block_reason = (
            None
            if broker_connected
            else "Live Auto paused — broker disconnected"
        )
        print(f"AUTO TRADE SYMBOL CHECK: {symbol} signal={signal}")
        print("LIVE_AUTO_SIGNAL_DECISION =", {
            "symbol": symbol,
            "signal": signal,
            "strategy_setup_complete": bool(plan.get("strategy_setup_complete")),
            "strategy_setup_type": plan.get("strategy_setup_type"),
            "plan_type": plan.get("plan_type"),
            "entry_timing": plan.get("entry_timing"),
            "blocked_by": plan.get("blocked_by"),
            "blocked_reason": plan.get("blocked_reason"),
            "blocker_rule_name": plan.get("blocker_rule_name"),
            "auto_enabled": LIVE_AUTO_TRADE_ENABLED.get("enabled"),
            "broker_connected": broker_connected,
        })
        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            stage="strategy_signal_seen",
            blocked_by=plan.get("blocked_by"),
            blocked_reason=plan.get("blocked_reason"),
            payload_valid=None,
            order_sent=False,
            order_accepted=False,
        )

        if signal not in ["BUY", "SELL"]:
            print("CTRADER AUTO TRADE SKIPPED: WAIT", symbol)
            signal_before_filters = str(
                plan.get("signal_before_filters")
                or ""
            ).upper()
            blocked_by = str(plan.get("blocked_by") or "").lower()
            blocker_rule_name = str(plan.get("blocker_rule_name") or "").lower()
            held_late_block = (
                signal_before_filters in ["BUY", "SELL"]
                and (
                    blocked_by == "late_entry"
                    or blocker_rule_name == "final_signal_hold_guard"
                )
            )

            display_signal = (
                signal_before_filters
                if held_late_block
                else plan.get("signal") or signal
            )
            wait_reason = (
                plan.get("blocked_reason")
                or plan.get("plan_reason")
                or plan.get("wait_reason")
                or plan.get("entry_timing")
                or "Waiting for BUY/SELL signal"
            )
            set_auto_trade_status(
                symbol=symbol,
                signal=display_signal,
                action=None,
                status="BLOCKED" if held_late_block else "WAIT",
                reason=wait_reason
            )
            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                stage="strategy_wait",
                blocked_by=plan.get("blocked_by"),
                blocked_reason=wait_reason if held_late_block else None,
                payload_valid=False,
                order_sent=False,
                order_accepted=False,
            )
            log_paper_live_signal_compare(
                symbol,
                plan,
                panel_data,
                live_signal=signal,
                live_blocked_by=plan.get("blocked_by"),
                live_blocked_reason=wait_reason if held_late_block else None
            )
            continue

        actionable_seen = True
        if symbol == "XAUUSD":
            print("AUTO TRADE XAUUSD ATTEMPT")

        active_trade_block = get_active_trade_execution_block(symbol)
        if active_trade_block:
            active_side = active_trade_block.get("active_trade_direction") or "trade"
            active_id = active_trade_block.get("active_trade_id")
            block_details = {
                **active_trade_block,
                "strategy_decision": signal,
                "order_sent": False,
            }
            plan.update({
                "strategy_decision": signal,
                "execution_allowed": False,
                "execution_status": "BLOCKED",
                "execution_block_reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                "active_trade_direction": active_side,
                "active_trade_id": active_id,
                "execution_source": "V1",
            })
            record_auto_execution_gate(
                symbol,
                plan,
                "ACTIVE_POSITION",
                "BLOCK",
                ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                block_details,
            )
            update_execution_outcome_safely(
                (plan.get("audit_diagnostics") or {}).get("cycle_id"),
                "BLOCKED",
                ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                {"gate": "active_position", **block_details},
            )
            set_auto_trade_status(
                symbol=symbol,
                signal=signal,
                action=signal,
                status="BLOCKED",
                reason=ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                details=block_details,
            )
            consumed_setup = consume_signal_setup_for_active_trade(
                symbol,
                plan,
                signal,
            )
            if consumed_setup:
                reset_consumed_smc_plan(
                    plan,
                    signal,
                    active_trade=LIVE_ACTIVE_ORDERS.get(symbol),
                )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=signal,
                stage="active_position",
                reason=ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                details=block_details,
            )
            results.append({
                "ok": False,
                "symbol": symbol,
                "signal": signal,
                "strategy_decision": signal,
                "execution_allowed": False,
                "execution_status": "BLOCKED",
                "execution_block_reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
                "active_trade_direction": active_side,
                "active_trade_id": active_id,
                "reason": ACTIVE_TRADE_EXECUTION_BLOCK_REASON,
            })
            continue

        if not broker_connected:
            update_execution_outcome_safely(
                (plan.get("audit_diagnostics") or {}).get("cycle_id"),
                "BLOCKED",
                broker_block_reason,
                {"gate": "broker_connection", "order_sent": False},
            )
            set_auto_trade_status(
                symbol=symbol,
                signal=signal,
                action=signal,
                status="BLOCKED",
                reason=broker_block_reason
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=signal,
                stage="broker_connection",
                reason=broker_block_reason
            )
            log_paper_live_signal_compare(
                symbol,
                plan,
                panel_data,
                live_signal=signal,
                live_blocked_by="broker_connection",
                live_blocked_reason=broker_block_reason
            )
            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                stage="broker_connection",
                blocked_by="broker_connection",
                blocked_reason=broker_block_reason,
                payload_valid=False,
                order_sent=False,
                order_accepted=False,
            )
            print("CTRADER AUTO TRADE PAUSED:", symbol, broker_block_reason)
            continue

        set_auto_trade_status(
            symbol=symbol,
            signal=signal,
            action=signal,
            status="READY",
            reason="Signal ready; running live safety checks"
        )

        log_paper_live_signal_compare(
            symbol,
            plan,
            panel_data,
            live_signal=signal,
            live_blocked_by=None,
            live_blocked_reason=None,
            live_would_enter=True
        )
        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            stage="ready_before_live_execute",
            blocked_by=None,
            blocked_reason=None,
            payload_valid=None,
            order_sent=False,
            order_accepted=False,
        )

        result = execute_live_order_core(
            {
                "symbol": symbol,
                "side": signal,
                "signal": signal,
                "entry": plan.get("entry_price"),
                "entry_price": plan.get("entry_price"),
                "sl": plan.get("stop_loss"),
                "stop_loss": plan.get("stop_loss"),
                "tp1": plan.get("tp1"),
                "tp2": plan.get("tp2"),
                **get_plan_execution_metadata(plan, signal),
                "news_event_id": plan.get("news_event_id"),
                "news_event": copy.deepcopy(plan.get("news_event")),
                "news_confirmation": copy.deepcopy(plan.get("news_confirmation")),
            },
            source="auto"
        )
        results.append(result)
        update_execution_outcome_safely(
            (plan.get("audit_diagnostics") or {}).get("cycle_id"),
            f"{signal}_EXECUTED" if result.get("ok") else "BLOCKED",
            None if result.get("ok") else (
                result.get("reason")
                or result.get("message")
                or result.get("result", {}).get("reason")
                or "ORDER_REJECTED"
            ),
            {
                "gate": "broker_submission",
                "order_sent": bool(result.get("ok")),
                "broker_position_id": (
                    (result.get("active_order") or {}).get("broker_position_id")
                    if result.get("ok") else None
                ),
            },
        )

        if not result.get("ok"):
            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                stage="live_execute_returned_rejected",
                blocked_by=result.get("stage") or result.get("status") or "broker_or_risk",
                blocked_reason=(
                    result.get("reason")
                    or result.get("message")
                    or result.get("result", {}).get("reason")
                    or "unknown"
                ),
                payload_valid=False,
                order_sent=False,
                order_accepted=False,
                result=result,
            )
            log_paper_live_signal_compare(
                symbol,
                plan,
                panel_data,
                live_signal=signal,
                live_blocked_by=result.get("stage") or result.get("status") or "broker_or_risk",
                live_blocked_reason=(
                    result.get("reason")
                    or result.get("message")
                    or result.get("result", {}).get("reason")
                    or "unknown"
                ),
                live_would_enter=True
            )

        if result.get("ok"):
            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                stage="live_execute_returned_accepted",
                blocked_by=None,
                blocked_reason=None,
                existing_position=result.get("active_order"),
                payload_valid=True,
                order_sent=True,
                order_accepted=True,
                result=result,
            )
            print("CTRADER AUTO TRADE SENT", symbol, signal)
        else:
            reason = (
                result.get("reason")
                or result.get("message")
                or result.get("result", {}).get("reason")
                or "unknown"
            )
            if symbol == "XAUUSD":
                print("AUTO TRADE XAUUSD BLOCKED:", reason)
            print(
                "CTRADER AUTO TRADE BLOCKED:",
                reason
            )

    return results

def evaluate_live_trade_exit(symbol, active_trade, current_plan):
    if not active_trade:
        return {
            "exit_status": None,
            "exit_reason": None
        }

    plan = current_plan if isinstance(current_plan, dict) else {}
    trade_side = str(
        active_trade.get("side")
        or active_trade.get("action")
        or ""
    ).upper()
    current_signal = str(plan.get("signal") or "WAIT").upper()
    market_condition = str(plan.get("market_condition") or "").upper()

    try:
        confidence = float(plan.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0

    if trade_side == "BUY" and current_signal == "SELL":
        return {
            "exit_status": "EXIT_SUGGESTED",
            "exit_reason": "Signal flipped bearish"
        }

    if trade_side == "SELL" and current_signal == "BUY":
        return {
            "exit_status": "EXIT_SUGGESTED",
            "exit_reason": "Signal flipped bullish"
        }

    if current_signal == "WAIT":
        return {
            "exit_status": "WATCH",
            "exit_reason": "Signal moved to WAIT"
        }

    if confidence < 35:
        return {
            "exit_status": "WATCH",
            "exit_reason": "Confidence weakened"
        }

    if market_condition in ["CHOPPY", "UNKNOWN"]:
        return {
            "exit_status": "WATCH",
            "exit_reason": "Market condition unsafe"
        }

    return {
        "exit_status": "HOLD",
        "exit_reason": "Trade still aligned"
    }

def update_live_trade_exit_states(panel_data):
    for symbol, active_trade in list(LIVE_ACTIVE_ORDERS.items()):
        if not active_trade:
            continue

        current_plan = get_signal_trade_plan(symbol)

        if isinstance(panel_data, dict):
            current_plan = (
                panel_data.get(symbol)
                or current_plan
            )

        exit_state = evaluate_live_trade_exit(
            symbol,
            active_trade,
            current_plan
        )

        LIVE_ACTIVE_ORDERS[symbol] = {
            **active_trade,
            **exit_state
        }

def prepare_ctrader_trade(payload, volume=0.01):
    sync_ctrader_account_state()

    raw_symbol = str(payload.get("symbol", "")).upper()
    symbol = normalize_symbol(raw_symbol)
    action = str(payload.get("action") or payload.get("side") or "").upper()
    plan = get_signal_trade_plan(symbol) or {}
    # Auto passes the current actionable panel signal. Do not let an older
    # cached WAIT/late-entry plan value override it during execution.
    payload_signal = str(payload.get("signal") or "").upper()
    plan_signal = str(plan.get("signal") or "").upper()
    if payload_signal in ["BUY", "SELL"]:
        signal = payload_signal
    else:
        signal = str(plan_signal or payload_signal or "WAIT").upper()
    value_plan = {} if payload_signal in ["BUY", "SELL"] else plan

    entry, _ = choose_backend_trade_value(
        value_plan,
        payload,
        "entry_price",
        "entry",
        "entry_price"
    )
    sl, _ = choose_backend_trade_value(
        value_plan,
        payload,
        "stop_loss",
        "sl",
        "stop_loss"
    )
    tp1, _ = choose_backend_trade_value(value_plan, payload, "tp1", "tp1")
    tp2, _ = choose_backend_trade_value(value_plan, payload, "tp2", "tp2")

    try:
        decimals = 2 if symbol == "XAUUSD" else 5
        tp1 = round(calculate_tp1_from_tp2(entry, tp2, action), decimals)
    except (TypeError, ValueError):
        pass

    log_frontend_trade_level_mismatch(
        symbol,
        action,
        "entry",
        plan.get("entry_price"),
        payload.get("entry", payload.get("entry_price"))
    )
    log_frontend_trade_level_mismatch(
        symbol,
        action,
        "sl",
        plan.get("stop_loss"),
        payload.get("sl", payload.get("stop_loss"))
    )
    log_frontend_trade_level_mismatch(
        symbol,
        action,
        "tp1",
        plan.get("tp1"),
        payload.get("tp1")
    )
    log_frontend_trade_level_mismatch(
        symbol,
        action,
        "tp2",
        plan.get("tp2"),
        payload.get("tp2")
    )

    if not LIVE_ACCOUNT_STATE.get("connected"):
        return reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, "No cTrader account connected")

    if symbol not in LIVE_ACTIVE_ORDERS:
        return reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, "Unsupported cTrader symbol")

    if action not in ["BUY", "SELL"]:
        return reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, "Action must be BUY or SELL")

    if signal not in ["BUY", "SELL"]:
        return reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, "Signal is WAIT")

    if action != signal:
        return reject_ctrader_order(symbol, action, entry, sl, tp1, tp2, "Order action does not match signal")

    normalized = normalize_trade_levels(
        symbol,
        action,
        entry,
        sl,
        tp1,
        tp2
    )

    normalized["mode"] = LIVE_ACCOUNT_STATE.get("mode", "demo")
    normalized["signal"] = signal
    execution_metadata = get_plan_execution_metadata(plan, action)
    for key in [
        "signal_setup_id",
        "fifteen_m_break_time",
        "fifteen_m_break_close_time",
        "five_m_confirmation_close_time",
        "trend_15m",
        "setup_identity",
        "source_indicator_event_id",
        "indicator_event_identity",
        "m5_confirmation_id",
        "m5_confirmation_identity",
        "news_event_id",
        "news_event",
        "news_confirmation",
    ]:
        payload_value = payload.get(key)
        normalized[key] = (
            copy.deepcopy(payload_value)
            if payload_value not in [None, "", {}]
            else copy.deepcopy(execution_metadata.get(key))
        )

    if not normalized.get("ok"):
        normalized["message"] = normalized.get("reason")
        log_rejected_ctrader_trade(
            symbol,
            action,
            entry,
            sl,
            tp1,
            tp2,
            normalized.get("reason")
        )
        return normalized

    rr_validation = validate_live_trade_risk_reward(
        symbol,
        action,
        normalized.get("entry"),
        normalized.get("sl"),
        normalized.get("tp2"),
    )
    if not rr_validation.get("ok"):
        rejected = reject_ctrader_order(
            symbol,
            action,
            normalized.get("entry"),
            normalized.get("sl"),
            normalized.get("tp1"),
            normalized.get("tp2"),
            rr_validation.get("reason"),
        )
        rejected["details"] = rr_validation
        rejected["risk_reward_ratio"] = rr_validation.get("risk_reward_ratio")
        return rejected

    normalized["volume"] = None

    return normalized

def log_structure_tp_trade_audit(symbol, side, trade_payload, risk_size, plan=None):
    plan = plan or {}
    trade_payload = trade_payload or {}
    risk_size = risk_size or {}
    symbol = normalize_symbol(symbol)
    side = str(side or "").upper()

    try:
        entry = float(trade_payload.get("entry"))
        sl = float(trade_payload.get("sl"))
        tp = float(trade_payload.get("tp2"))
        risk_amount = float(risk_size.get("risk_amount"))
    except (TypeError, ValueError):
        print("STRUCTURE_TP_TRADE_AUDIT:", {
            "symbol": symbol,
            "direction": side,
            "reason": "missing level or risk amount for TP audit",
            "entry": trade_payload.get("entry"),
            "sl": trade_payload.get("sl"),
            "tp": trade_payload.get("tp2"),
            "risk_amount": risk_size.get("risk_amount"),
        })
        return

    price_risk = abs(entry - sl)
    price_reward = abs(tp - entry)
    rr = price_reward / price_risk if price_risk > 0 else 0

    try:
        structure_rr = float(
            plan.get("structure_reward_ratio")
            or plan.get("risk_reward_ratio")
            or rr
        )
    except (TypeError, ValueError):
        structure_rr = rr

    structure_reward_dollars = risk_amount * structure_rr
    minimum_rr, maximum_rr = get_configured_rr_window()
    minimum_reward_dollars = risk_amount * minimum_rr
    maximum_reward_dollars = risk_amount * maximum_rr

    print("STRUCTURE_TP_TRADE_AUDIT:", {
        "symbol": symbol,
        "direction": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk_dollars": round(risk_amount, 2),
        "structure_reward_dollars": round(structure_reward_dollars, 2),
        "reward_risk_ratio": round(rr, 4),
        "structure_reward_risk_ratio": round(structure_rr, 4),
        "minimum_required_reward": round(minimum_reward_dollars, 2),
        "maximum_allowed_reward": round(maximum_reward_dollars, 2),
        "sl_swing_used": (
            plan.get("sl_swing_used")
            or plan.get("sl_source_swing")
            or plan.get("selected_swing_sl")
        ),
        "tp_structure_used": (
            plan.get("tp_structure_used")
            or plan.get("structure_resistance")
            or plan.get("structure_support")
        ),
        "tp_structure_source": plan.get("tp_structure_source"),
        "tp_capped_at_2r": plan.get("tp_capped_at_2r"),
    })

def first_valid_live_price(*values):
    return risk_first_valid_live_price(*values)

def live_prices_match(symbol, left, right):
    return risk_live_prices_match(symbol, left, right, normalize_symbol=normalize_symbol)

def build_live_protection_audit(
    symbol,
    side,
    entry=None,
    saved_sl=None,
    broker_sl=None,
    displayed_sl=None,
    tp2=None,
    broker_tp=None,
    bid=None,
    ask=None,
    lot_size=None,
    expected_loss_usd=None,
    max_risk_usd=None,
    repair_result=None,
    stage=None,
):
    return risk_build_live_protection_audit(
        symbol,
        side,
        normalize_symbol=normalize_symbol,
        entry=entry,
        saved_sl=saved_sl,
        broker_sl=broker_sl,
        displayed_sl=displayed_sl,
        tp2=tp2,
        broker_tp=broker_tp,
        bid=bid,
        ask=ask,
        lot_size=lot_size,
        expected_loss_usd=expected_loss_usd,
        max_risk_usd=max_risk_usd,
        repair_result=repair_result,
        stage=stage,
    )

def build_live_risk_calculation_audit(symbol, side, trade_payload, risk_size, reason=None):
    return risk_build_live_risk_calculation_audit(
        symbol,
        side,
        trade_payload,
        risk_size,
        normalize_symbol=normalize_symbol,
        expected_loss_calculator=calculate_expected_loss_usd_from_risk_size,
        reason=reason,
    )

def log_live_risk_calculation_audit(symbol, side, trade_payload, risk_size, reason=None):
    audit = risk_log_live_risk_calculation_audit(
        symbol,
        side,
        trade_payload,
        risk_size,
        normalize_symbol=normalize_symbol,
        expected_loss_calculator=calculate_expected_loss_usd_from_risk_size,
        reason=reason,
    )
    return audit

def calculate_expected_loss_usd_from_risk_size(risk_size):
    return risk_calculate_expected_loss_usd_from_risk_size(risk_size)

def is_dev_request(request: Request):
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    local_hosts = ["127.0.0.1", "localhost", "::1"]
    local_origins = [
        "http://127.0.0.1:5501",
        "http://localhost:5501",
    ]

    return (
        host in local_hosts
        or forwarded.split(",")[0].strip() in local_hosts
        or origin in local_origins
        or any(origin.startswith(f"http://{local}") for local in local_hosts)
        or any(referer.startswith(f"http://{local}") for local in local_hosts)
    )

def get_panel_candle_extremes(symbol, panel_data=None, timeframe="5m"):
    data = panel_data if isinstance(panel_data, dict) else PANEL_CACHE.get("data")
    candles_root = data.get("candles") if isinstance(data, dict) else None
    symbol_candles = (
        candles_root.get(normalize_symbol(symbol))
        if isinstance(candles_root, dict)
        else None
    )

    if not isinstance(symbol_candles, dict):
        return {}

    candles = symbol_candles.get(timeframe)
    if not isinstance(candles, list) or not candles:
        return {}

    latest = candles[-1]
    if not isinstance(latest, dict):
        return {}

    def as_float(value):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    return {
        "high": as_float(latest.get("high") or latest.get("High")),
        "low": as_float(latest.get("low") or latest.get("Low")),
        "close": as_float(latest.get("close") or latest.get("Close")),
        "time": latest.get("time") or latest.get("datetime") or latest.get("Datetime"),
        "timeframe": timeframe,
    }


def sync_live_positions(panel_data=None):
    sync_ctrader_account_state()

    connected = bool(LIVE_ACCOUNT_STATE.get("connected"))
    print(
        "LIVE_BROKER_STATUS:",
        "connected" if connected else "disconnected"
    )

    if not connected:
        set_auto_trade_status(
            status="BLOCKED",
            reason="Live Auto paused — broker disconnected",
            details={
                "preference_remains_enabled": bool(
                    LIVE_AUTO_TRADE_ENABLED["enabled"]
                ),
                "execution_ready": False,
            },
        )

        print("LIVE_POSITION_SYNC:", {
            "connected": False,
            "positions": [],
            "active_orders": get_persistable_live_active_orders()
        })

        return [
            trade
            for trade in LIVE_ACTIVE_ORDERS.values()
            if trade and get_live_trade_status(trade) in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]
        ]

    if not LIVE_ACCOUNT_STATE.get("connected"):
        return []

    try:
        positions = get_open_positions()
        print("LIVE_POSITION_SYNC:", {
            "connected": True,
            "positions": positions
        })
        position_fetch_error = get_ctrader_position_fetch_error()

        if position_fetch_error:
            print("LIVE_POSITION_SYNC_ERROR:", position_fetch_error)
            LIVE_POSITION_SYNC_STATUS["last_error"] = position_fetch_error
            return [
                trade
                for trade in LIVE_ACTIVE_ORDERS.values()
                if trade and get_live_trade_status(trade) in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]
            ]
        else:
            LIVE_POSITION_SYNC_STATUS["last_success"] = time.time()
            LIVE_POSITION_SYNC_STATUS["last_error"] = None

        previous_active_orders = {
            symbol: trade
            for symbol, trade in LIVE_ACTIVE_ORDERS.items()
            if trade
        }

        rebuilt_active_orders = {
            symbol: None
            for symbol in LIVE_ACTIVE_ORDERS
        }

        for position in positions:
            symbol = normalize_symbol(position.get("symbol"))

            if symbol not in LIVE_ACTIVE_ORDERS:
                continue

            broker_lot_size = normalize_broker_volume_to_lots(
                position.get("volume")
                or position.get("volumeInUnits")
                or (position.get("tradeData") or {}).get("volume"),
                symbol
            )
            current_price = (
                position.get("current_price")
                or position.get("currentPrice")
                or position.get("price")
                or position.get("bid")
                or position.get("ask")
            )
            raw_position_for_levels = (
                position.get("raw")
                if isinstance(position.get("raw"), dict)
                else {}
            )
            nested_raw_position = (
                raw_position_for_levels.get("raw")
                if isinstance(raw_position_for_levels.get("raw"), dict)
                else {}
            )
            broker_current_high = (
                position.get("current_high")
                or position.get("currentHigh")
                or position.get("high")
                or raw_position_for_levels.get("currentHigh")
                or raw_position_for_levels.get("high")
            )
            broker_current_low = (
                position.get("current_low")
                or position.get("currentLow")
                or position.get("low")
                or raw_position_for_levels.get("currentLow")
                or raw_position_for_levels.get("low")
            )
            panel_candle_extremes = get_panel_candle_extremes(
                symbol,
                panel_data=panel_data,
                timeframe="5m",
            )
            position_id = (
                position.get("position_id")
                or position.get("positionId")
                or position.get("id")
                or f"broker-{symbol}"
            )
            side = normalize_live_trade_side(
                position.get("side")
                or position.get("tradeSide")
                or position.get("direction")
                or ""
            )
            entry = (
                position.get("entry")
                or position.get("entry_price")
                or position.get("entryPrice")
                or position.get("openPrice")
            )
            volume_units = (
                position.get("volume")
                or position.get("volumeInUnits")
                or (position.get("tradeData") or {}).get("volume")
            )
            pips = calculate_live_trade_pips(
                symbol,
                side,
                entry,
                current_price
            )
            broker_position_for_pl = {
                **position,
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "current_price": current_price,
                "lot_size": broker_lot_size,
                "volume_units": volume_units,
            }
            broker_pnl = get_live_floating_pl(broker_position_for_pl)
            broker_net_pl, broker_pnl_source = extract_broker_trade_pl(
                broker_position_for_pl
            )

            if broker_net_pl is None:
                broker_pnl_source = "fallback"
            live_prices_for_position = {}

            try:
                live_prices_for_position = (get_live_prices() or {}).get("live_prices", {})
            except Exception:
                live_prices_for_position = {}

            live_tick_for_position = live_prices_for_position.get(symbol) or {}
            live_bid_for_position = live_tick_for_position.get("bid")
            live_ask_for_position = live_tick_for_position.get("ask")
            used_current_price = current_price

            if side == "BUY" and live_bid_for_position is not None:
                used_current_price = live_bid_for_position
            elif side == "SELL" and live_ask_for_position is not None:
                used_current_price = live_ask_for_position

            current_order = previous_active_orders.get(symbol)
            signal_plan = get_signal_trade_plan(symbol) or {}
            broker_synced_sl = (
                position.get("sl")
                or position.get("stop_loss")
                or position.get("stopLoss")
                or raw_position_for_levels.get("sl")
                or raw_position_for_levels.get("stopLoss")
                or nested_raw_position.get("stopLoss")
            )
            planned_sl = (
                (current_order or {}).get("planned_sl")
                or (current_order or {}).get("original_sl")
                or signal_plan.get("stop_loss")
            )
            planned_tp1 = (
                (current_order or {}).get("planned_tp1")
                or (current_order or {}).get("tp1")
                or signal_plan.get("tp1")
            )
            planned_tp2 = (
                (current_order or {}).get("planned_tp2")
                or (current_order or {}).get("tp2")
                or signal_plan.get("tp2")
            )
            broker_synced_tp2 = (
                position.get("take_profit")
                or position.get("takeProfit")
                or raw_position_for_levels.get("takeProfit")
                or nested_raw_position.get("takeProfit")
                or position.get("tp2")
                or raw_position_for_levels.get("tp2")
                or nested_raw_position.get("tp2")
            )
            synced_sl = broker_synced_sl
            synced_tp2 = broker_synced_tp2
            synced_tp1 = (
                position.get("tp1")
                or raw_position_for_levels.get("tp1")
            )
            refresh_overwrite_blocked = False
            modification_age = None
            user_modified_levels = (
                (current_order or {}).get("user_modified_levels")
                if isinstance((current_order or {}).get("user_modified_levels"), dict)
                else {}
            )
            levels_modified_at = (current_order or {}).get("levels_modified_at")

            try:
                modification_age = time.time() - float(levels_modified_at)
            except (TypeError, ValueError):
                modification_age = None

            saved_sl = first_valid_live_price(
                user_modified_levels.get("sl"),
                (current_order or {}).get("sl"),
                (current_order or {}).get("current_sl"),
                (current_order or {}).get("planned_sl"),
            )
            saved_tp1 = first_valid_live_price(
                user_modified_levels.get("tp1"),
                (current_order or {}).get("tp1"),
                (current_order or {}).get("take_profit_1"),
                (current_order or {}).get("planned_tp1"),
            )
            saved_tp2 = first_valid_live_price(
                user_modified_levels.get("tp2"),
                (current_order or {}).get("tp2"),
                (current_order or {}).get("take_profit_2"),
                (current_order or {}).get("take_profit"),
                (current_order or {}).get("planned_tp2"),
            )
            broker_grace_active = modification_age is not None and modification_age <= 120

            if saved_tp1 is not None:
                if synced_tp1 is None or not live_prices_match(symbol, synced_tp1, saved_tp1):
                    refresh_overwrite_blocked = True
                synced_tp1 = saved_tp1
            else:
                try:
                    if entry is not None and synced_tp2 is not None:
                        synced_tp1 = round(
                            calculate_tp1_from_tp2(entry, synced_tp2, side),
                            2 if symbol == "XAUUSD" else 5,
                        )
                except (TypeError, ValueError):
                    pass

            broker_tp_repair_result = None
            broker_tp_missing_or_mismatch = (
                saved_tp2 is not None
                and (
                    broker_synced_tp2 is None
                    or not live_prices_match(symbol, broker_synced_tp2, saved_tp2)
                )
            )

            broker_sl_change = risk_classify_stop_loss_change(
                symbol,
                side,
                broker_synced_sl,
                saved_sl,
                normalize_symbol=normalize_symbol,
            )
            broker_sl_is_valid = False
            try:
                broker_sl_value = float(broker_synced_sl)
                entry_value = float(entry)
                broker_sl_is_valid = (
                    (side == "BUY" and broker_sl_value < entry_value)
                    or (side == "SELL" and broker_sl_value > entry_value)
                )
            except (TypeError, ValueError):
                broker_sl_value = None

            # A non-null broker level is authoritative. This includes manual
            # changes made directly in cTrader; NathauxFX must mirror them
            # instead of repairing them back to an older application value.
            broker_manual_sl_adopted = bool(
                broker_sl_is_valid
                and saved_sl is not None
                and not live_prices_match(symbol, broker_synced_sl, saved_sl)
            )

            if broker_manual_sl_adopted:
                adopted_sl = float(broker_synced_sl)
                saved_sl = adopted_sl
                synced_sl = adopted_sl
                levels_modified_at = time.time()
                user_modified_levels = {
                    **user_modified_levels,
                    "sl": adopted_sl,
                }
                if isinstance(current_order, dict):
                    current_order["sl"] = adopted_sl
                    current_order["current_sl"] = adopted_sl
                    current_order["user_modified_levels"] = copy.deepcopy(
                        user_modified_levels
                    )
                    current_order["levels_modified_at"] = levels_modified_at
                    current_order["manual_broker_sl_adopted"] = True
                    current_order["manual_broker_sl_adopted_at"] = levels_modified_at
                    persist_live_trade_state(current_order)
                print("LIVE_MANUAL_BROKER_SL_ADOPTED =", {
                    "symbol": symbol,
                    "side": side,
                    "position_id": position_id,
                    "broker_sl": adopted_sl,
                    "risk_change": broker_sl_change,
                    "reason": "valid broker SL is authoritative",
                })

            broker_manual_tp_adopted = False
            try:
                broker_tp_value = float(broker_synced_tp2)
                entry_value = float(entry)
                broker_tp_is_valid = (
                    (side == "BUY" and broker_tp_value > entry_value)
                    or (side == "SELL" and broker_tp_value < entry_value)
                )
            except (TypeError, ValueError):
                broker_tp_value = None
                broker_tp_is_valid = False

            if (
                broker_tp_is_valid
                and saved_tp2 is not None
                and not live_prices_match(symbol, broker_synced_tp2, saved_tp2)
            ):
                saved_tp2 = broker_tp_value
                synced_tp2 = broker_tp_value
                try:
                    saved_tp1 = round(
                        calculate_tp1_from_tp2(entry, saved_tp2, side),
                        2 if symbol == "XAUUSD" else 5,
                    )
                    synced_tp1 = saved_tp1
                except (TypeError, ValueError):
                    pass
                levels_modified_at = time.time()
                user_modified_levels = {
                    **user_modified_levels,
                    "tp1": saved_tp1,
                    "tp2": saved_tp2,
                }
                if isinstance(current_order, dict):
                    current_order["tp1"] = saved_tp1
                    current_order["take_profit_1"] = saved_tp1
                    current_order["tp2"] = saved_tp2
                    current_order["take_profit_2"] = saved_tp2
                    current_order["take_profit"] = saved_tp2
                    current_order["user_modified_levels"] = copy.deepcopy(
                        user_modified_levels
                    )
                    current_order["levels_modified_at"] = levels_modified_at
                    current_order["manual_broker_tp_adopted"] = True
                    current_order["manual_broker_tp_adopted_at"] = levels_modified_at
                    persist_live_trade_state(current_order)
                broker_manual_tp_adopted = True
                broker_tp_missing_or_mismatch = False
                print("LIVE_MANUAL_BROKER_TP_ADOPTED =", {
                    "symbol": symbol,
                    "side": side,
                    "position_id": position_id,
                    "broker_tp": broker_tp_value,
                    "derived_tp1": saved_tp1,
                    "reason": "valid broker TP is authoritative",
                })

            broker_sl_missing_or_mismatch = (
                saved_sl is not None
                and (
                    broker_synced_sl is None
                    or not live_prices_match(symbol, broker_synced_sl, saved_sl)
                )
            )
            broker_sl_repair_result = None
            broker_tp_before_repair = broker_synced_tp2
            broker_sl_before_repair = broker_synced_sl

            print("LIVE_ORDER_PROTECTION_AUDIT =", build_live_protection_audit(
                symbol,
                side,
                entry=entry,
                saved_sl=saved_sl,
                broker_sl=broker_synced_sl,
                displayed_sl=broker_synced_sl,
                tp2=saved_tp2,
                broker_tp=broker_synced_tp2,
                bid=live_bid_for_position,
                ask=live_ask_for_position,
                lot_size=broker_lot_size,
                stage="live_sync_compare",
            ))

            if position_id and broker_sl_missing_or_mismatch:
                broker_sl_repair_result = modify_position_sltp(
                    position_id,
                    stop_loss_price=saved_sl,
                    take_profit_price=saved_tp2,
                )
                print("LIVE_BROKER_SL_REPAIR =", build_live_protection_audit(
                    symbol,
                    side,
                    entry=entry,
                    saved_sl=saved_sl,
                    broker_sl=broker_synced_sl,
                    displayed_sl=broker_synced_sl,
                    tp2=saved_tp2,
                    broker_tp=broker_synced_tp2,
                    bid=live_bid_for_position,
                    ask=live_ask_for_position,
                    lot_size=broker_lot_size,
                    repair_result=broker_sl_repair_result,
                    stage="live_sync_sl_repair",
                ))

                if broker_sl_repair_result.get("ok"):
                    broker_synced_sl = saved_sl
                    synced_sl = saved_sl
                    if broker_tp_missing_or_mismatch and saved_tp2 is not None:
                        broker_synced_tp2 = saved_tp2
                        synced_tp2 = saved_tp2
                        print("LIVE_BROKER_TP_REPAIR =", {
                            "symbol": symbol,
                            "side": side,
                            "position_id": position_id,
                            "entry": entry,
                            "saved_sl": saved_sl,
                            "broker_sl_before": broker_sl_before_repair,
                            "displayed_sl": synced_sl,
                            "saved_tp2": saved_tp2,
                            "broker_tp_before": broker_tp_before_repair,
                            "bid": live_bid_for_position,
                            "ask": live_ask_for_position,
                            "lot_size": broker_lot_size,
                            "tp1_managed_by_flowsignal": True,
                            "broker_tp_should_be_tp2": True,
                            "repair_result": broker_sl_repair_result,
                        })

            if position_id and broker_tp_missing_or_mismatch and not (
                broker_sl_repair_result and broker_sl_repair_result.get("ok")
            ):
                broker_tp_repair_result = modify_position_sltp(
                    position_id,
                    stop_loss_price=saved_sl,
                    take_profit_price=saved_tp2,
                )
                print("LIVE_BROKER_TP_REPAIR =", {
                    "symbol": symbol,
                    "side": side,
                    "position_id": position_id,
                    "entry": entry,
                    "saved_sl": saved_sl,
                    "broker_sl_before": broker_synced_sl,
                    "displayed_sl": broker_synced_sl,
                    "saved_tp2": saved_tp2,
                    "broker_tp_before": broker_synced_tp2,
                    "bid": live_bid_for_position,
                    "ask": live_ask_for_position,
                    "lot_size": broker_lot_size,
                    "tp1_managed_by_flowsignal": True,
                    "broker_tp_should_be_tp2": True,
                    "repair_result": broker_tp_repair_result,
                })

                if broker_tp_repair_result.get("ok"):
                    broker_synced_tp2 = saved_tp2
                    synced_tp2 = saved_tp2

            if broker_sl_missing_or_mismatch and not (
                broker_sl_repair_result and broker_sl_repair_result.get("ok")
            ):
                synced_sl = saved_sl
                print("BROKER_SL_MISSING_WARNING =", build_live_protection_audit(
                    symbol,
                    side,
                    entry=entry,
                    saved_sl=saved_sl,
                    broker_sl=broker_synced_sl,
                    displayed_sl=synced_sl,
                    tp2=saved_tp2,
                    broker_tp=broker_synced_tp2,
                    bid=live_bid_for_position,
                    ask=live_ask_for_position,
                    lot_size=broker_lot_size,
                    repair_result=broker_sl_repair_result,
                    stage="live_sync_sl_repair_failed",
                ))

            if broker_tp_missing_or_mismatch and not (
                (broker_tp_repair_result and broker_tp_repair_result.get("ok"))
                or (broker_sl_repair_result and broker_sl_repair_result.get("ok"))
            ):
                synced_tp2 = saved_tp2

            if synced_tp1 is None or synced_tp2 is None:
                print("TRADE_LEVEL_WARNING =", {
                    "symbol": symbol,
                    "position_id": position_id,
                    "message": "Missing broker TP; keeping last known valid trade level",
                    "saved_tp1": saved_tp1,
                    "saved_tp2": saved_tp2,
                })

            print("refreshOverwriteBlocked", refresh_overwrite_blocked, {
                "symbol": symbol,
                "position_id": position_id,
                "modification_age": modification_age,
                "broker_sl": broker_synced_sl,
                "saved_sl": saved_sl,
                "broker_tp2": broker_synced_tp2,
                "saved_tp2": saved_tp2,
                "saved_tp1": saved_tp1,
                "panel_5m_high": panel_candle_extremes.get("high"),
                "panel_5m_low": panel_candle_extremes.get("low"),
                "panel_5m_time": panel_candle_extremes.get("time"),
            })
            observed_prices = [
                value
                for value in [
                    used_current_price,
                    broker_current_high,
                    broker_current_low,
                    panel_candle_extremes.get("high"),
                    panel_candle_extremes.get("low"),
                    current_order.get("current_high") if current_order else None,
                    current_order.get("current_low") if current_order else None,
                ]
                if value is not None
            ]
            numeric_observed_prices = []

            for value in observed_prices:
                try:
                    numeric_observed_prices.append(float(value))
                except (TypeError, ValueError):
                    continue

            current_high = (
                max(numeric_observed_prices)
                if numeric_observed_prices
                else used_current_price
            )
            current_low = (
                min(numeric_observed_prices)
                if numeric_observed_prices
                else used_current_price
            )

            broker_stop_loss_confirmed = not (
                broker_sl_missing_or_mismatch
                and not (
                    broker_sl_repair_result
                    and broker_sl_repair_result.get("ok")
                )
            ) and synced_sl is not None
            broker_take_profit_confirmed = not (
                broker_tp_missing_or_mismatch
                and not (
                    (broker_tp_repair_result and broker_tp_repair_result.get("ok"))
                    or (broker_sl_repair_result and broker_sl_repair_result.get("ok"))
                )
            ) and synced_tp2 is not None

            if symbol == "EURUSD":
                detected_pl_fields = {
                    key: value
                    for key, value in position.items()
                    if any(
                        marker in str(key).lower()
                        for marker in [
                            "pnl",
                            "profit",
                            "pl",
                            "unrealized",
                            "gross",
                            "net",
                            "money",
                            "swap",
                            "commission",
                        ]
                    )
                }
                raw_position = position.get("raw")

                if isinstance(raw_position, dict):
                    detected_pl_fields["raw"] = {
                        key: value
                        for key, value in raw_position.items()
                        if any(
                            marker in str(key).lower()
                            for marker in [
                                "pnl",
                                "profit",
                                "pl",
                                "unrealized",
                                "gross",
                                "net",
                                "money",
                                "swap",
                                "commission",
                            ]
                        )
                    }

                print("RAW_OPEN_POSITION_PL_DEBUG =", {
                    "full_position_object": position,
                    "all_keys": list(position.keys()),
                    "detected_pl_fields": detected_pl_fields,
                    "entry_price": entry,
                    "current_price": current_price,
                    "side": side,
                    "volume": volume_units,
                    "lot_size": broker_lot_size,
                    "calculated_fallback_pl": calculate_live_floating_pl_from_prices({
                        **position,
                        "symbol": symbol,
                        "side": side,
                        "entry": entry,
                        "current_price": current_price,
                        "lot_size": broker_lot_size,
                        "volume_units": volume_units,
                    }),
                    "final_reader_pl": broker_pnl,
                })

            mirrored_order = {
                "order_id": f"broker-{position_id}",
                "trade_id": f"ctrader-pos-{position_id}",
                "position_id": position_id,
                "broker_position_id": position_id,
                "symbol": symbol,
                "side": side,
                "mode": LIVE_ACCOUNT_STATE["mode"],
                "broker": LIVE_ACCOUNT_STATE["broker"],
                "volume": broker_lot_size,
                "lot_size": broker_lot_size,
                "volume_units": volume_units,
                "entry": entry,
                "sl": synced_sl,
                "current_sl": synced_sl,
                "original_sl": (
                    (current_order or {}).get("original_sl")
                    or synced_sl
                    or planned_sl
                ),
                "planned_sl": planned_sl,
                "broker_stop_loss_confirmed": broker_stop_loss_confirmed,
                "broker_stop_loss_missing": not broker_stop_loss_confirmed,
                "broker_sl_warning": (
                    "BROKER SL MISSING"
                    if not broker_stop_loss_confirmed and saved_sl is not None
                    else None
                ),
                "broker_sl_repair_result": broker_sl_repair_result,
                "tp1": synced_tp1,
                "tp2": synced_tp2,
                "planned_tp1": planned_tp1,
                "planned_tp2": planned_tp2,
                "broker_take_profit_confirmed": broker_take_profit_confirmed,
                "broker_take_profit_missing": not broker_take_profit_confirmed,
                "broker_tp_repair_result": broker_tp_repair_result,
                "current_price": used_current_price,
                "bid": live_bid_for_position,
                "ask": live_ask_for_position,
                "current_high": current_high,
                "current_low": current_low,
                "position_current_price": current_price,
                "floating_pl": broker_pnl,
                "floating_pnl": broker_pnl,
                "pnl": broker_pnl,
                "profit": broker_pnl,
                "broker_pnl": broker_pnl,
                "broker_pnl_source": broker_pnl_source,
                "pips": pips,
                "opened_at": position.get("opened_at") or time.time(),
                "source": "broker",
                "result": "RUNNING",
                "signal_setup_id": (
                    (current_order or {}).get("signal_setup_id")
                    or get_signal_setup_id(signal_plan, side)
                ),
                "levels_modified_at": levels_modified_at,
                "user_modified_levels": user_modified_levels,
                "manual_broker_sl_adopted": bool(
                    broker_manual_sl_adopted
                    or (current_order or {}).get("manual_broker_sl_adopted")
                ),
                "manual_broker_sl_adopted_at": (
                    levels_modified_at
                    if broker_manual_sl_adopted
                    else (current_order or {}).get("manual_broker_sl_adopted_at")
                ),
                "manual_broker_tp_adopted": bool(
                    broker_manual_tp_adopted
                    or (current_order or {}).get("manual_broker_tp_adopted")
                ),
                "manual_broker_tp_adopted_at": (
                    levels_modified_at
                    if broker_manual_tp_adopted
                    else (current_order or {}).get("manual_broker_tp_adopted_at")
                ),
                "raw": position.get("raw", position),
            }
            ensure_executed_snapshot_for_active_trade(mirrored_order, signal_plan)
            ensure_live_trade_identity(mirrored_order, symbol)

            if current_order and broker_position_matches_trade(position, current_order):
                rebuilt_active_orders[symbol] = update_live_trade_tp_protection({
                    **current_order,
                    **mirrored_order,
                    "trade_id": get_live_trade_identity(current_order) or get_live_trade_identity(mirrored_order),
                    "opened_at": current_order.get("opened_at") or mirrored_order["opened_at"],
                    "result": (
                        "TP2 HIT"
                        if current_order.get("tp2_hit") or current_order.get("tp2_close_requested")
                        else
                        current_order.get("result")
                        if current_order.get("hit_tp1")
                        else mirrored_order.get("result")
                    ),
                    "status": (
                        "CLOSING"
                        if current_order.get("tp2_hit") or current_order.get("tp2_close_requested")
                        else mirrored_order.get("status")
                    ),
                    "tp2_hit": bool(
                        current_order.get("tp2_hit")
                        or mirrored_order.get("tp2_hit")
                    ),
                    "tp2_close_requested": bool(
                        current_order.get("tp2_close_requested")
                        or mirrored_order.get("tp2_close_requested")
                    ),
                    "tp2_close_result": (
                        current_order.get("tp2_close_result")
                        or mirrored_order.get("tp2_close_result")
                    ),
                    "exit_status": (
                        "CLOSING"
                        if current_order.get("tp2_hit") or current_order.get("tp2_close_requested")
                        else current_order.get("exit_status") or mirrored_order.get("exit_status")
                    ),
                    "exit_reason": (
                        current_order.get("exit_reason")
                        or mirrored_order.get("exit_reason")
                    ),
                })
                ensure_live_trade_identity(rebuilt_active_orders[symbol], symbol)
                log_live_trade_audit("broker_position_synced", rebuilt_active_orders[symbol])
                log_trade_visual_levels(rebuilt_active_orders[symbol])
                continue

            if not current_order:
                rebuilt_active_orders[symbol] = update_live_trade_tp_protection(mirrored_order)
                ensure_live_trade_identity(rebuilt_active_orders[symbol], symbol)
                log_live_trade_audit("broker_position_mirrored", rebuilt_active_orders[symbol])
                log_trade_visual_levels(rebuilt_active_orders[symbol])
                print("BROKER POSITION MIRRORED:", mirrored_order)

            else:
                rebuilt_active_orders[symbol] = mirrored_order
                ensure_live_trade_identity(rebuilt_active_orders[symbol], symbol)
                log_live_trade_audit("broker_position_replaced", rebuilt_active_orders[symbol])
                log_trade_visual_levels(rebuilt_active_orders[symbol])

        closed_at = time.time()
        removed = []

        for symbol, trade in previous_active_orders.items():
            if not trade:
                continue

            if find_matching_broker_position(trade, positions):
                continue

            old_status = get_live_trade_status(trade) or "RUNNING"
            position_id = (
                trade.get("position_id")
                or trade.get("broker_position_id")
                or trade.get("broker_order_id")
                or trade.get("order_id")
            )
            closed_trade = build_broker_closed_trade(trade, closed_at)

            move_live_trade_to_history_once(closed_trade)
            invalidate_symbol_setup_state(
                symbol,
                closed_at,
                reason="broker position closed",
                panel_data=panel_data,
            )
            log_live_trade_audit("broker_position_closed_sync", closed_trade, reason="no broker position")
            mark_signal_history_broker_closed(symbol)
            removed.append({
                "symbol": symbol,
                "order_id": trade.get("order_id"),
                "position_id": position_id,
                "result": "BROKER_CLOSED",
                "reason": "no broker position"
            })

            print("LIVE_BROKER_POSITION_CLOSED_SYNC =", {
                "symbol": symbol,
                "old_status": old_status,
                "new_status": "BROKER_CLOSED",
                "position_id": position_id,
            })
            print("LIVE_TRADE_SYNC_CLOSED_EXTERNALLY", {
                "symbol": symbol,
                "position_id": position_id,
                "closed_at": closed_at,
            })

            print(
                "CTRADER POSITION CLOSED ON BROKER:",
                symbol,
                position_id,
                "BROKER_CLOSED",
                closed_trade.get("pnl")
            )

        LIVE_ACTIVE_ORDERS.update(rebuilt_active_orders)
        active_ids = {
            str(get_live_trade_match_key(trade))
            for trade in LIVE_ACTIVE_ORDERS.values()
            if trade and get_live_trade_match_key(trade)
        }
        cleaned_history = []

        for item in LIVE_TRADE_HISTORY:
            if str(get_live_trade_match_key(item)) in active_ids:
                log_live_trade_audit(
                    "history_removed_because_active",
                    item,
                    reason="same broker position is currently open"
                )
                continue

            result = str(
                item.get("result")
                or item.get("status")
                or ""
            ).upper()

            if result in ["RUNNING", "OPEN", "CLOSING", "TP2 HIT"] and not find_matching_broker_position(item, positions):
                old_status = get_live_trade_status(item) or result
                position_id = (
                    item.get("position_id")
                    or item.get("broker_position_id")
                    or item.get("broker_order_id")
                    or item.get("order_id")
                )
                closed_item = build_broker_closed_trade(item, closed_at)
                invalidate_symbol_setup_state(
                    item.get("symbol"),
                    closed_at,
                    reason="stale running position closed",
                    panel_data=panel_data,
                )
                mark_signal_history_broker_closed(item.get("symbol"))
                log_live_trade_audit(
                    "running_history_converted_closed",
                    closed_item,
                    reason="unconfirmed running history converted to broker closed"
                )
                removed.append({
                    "symbol": item.get("symbol"),
                    "order_id": item.get("order_id"),
                    "position_id": position_id,
                    "result": "BROKER_CLOSED",
                    "reason": "unconfirmed running history converted to broker closed"
                })

                print("LIVE_BROKER_POSITION_CLOSED_SYNC =", {
                    "symbol": normalize_symbol(item.get("symbol")),
                    "old_status": old_status,
                    "new_status": "BROKER_CLOSED",
                    "position_id": position_id,
                })

                cleaned_history.append(closed_item)
                continue

            cleaned_history.append(item)

        LIVE_TRADE_HISTORY[:] = cleaned_history
        del LIVE_TRADE_HISTORY[MAX_LIVE_TRADE_HISTORY:]
        save_live_backup()

        if removed:
            print("LIVE_STALE_RUNNING_REMOVED:", removed)

        return positions
    except Exception as e:
        print("LIVE_POSITION_SYNC_ERROR:", e)
        LIVE_POSITION_SYNC_STATUS["last_error"] = str(e)
        return []

@app.get("/ctrader/connect")
def ctrader_connect():
    return build_ctrader_authorization_url()


@app.get("/ctrader/oauth-debug")
def ctrader_oauth_debug():
    result = build_ctrader_authorization_url()
    redirect_debug = result.get("redirect_uri_debug") or get_ctrader_redirect_uri_debug()
    return {
        "ok": result.get("ok", False),
        "client_id": os.getenv("CTRADER_CLIENT_ID"),
        "redirect_uri": result.get("redirect_uri"),
        "redirect_uri_env_value": redirect_debug.get("env_value"),
        "redirect_uri_file_value": redirect_debug.get("file_value"),
        "redirect_uri_fallback_value": redirect_debug.get("fallback_value"),
        "redirect_uri_final_value": redirect_debug.get("final_value"),
        "redirect_uri_source": redirect_debug.get("source"),
        "auth_url": result.get("authorization_url"),
        "reason": result.get("reason"),
    }


@app.get("/ctrader/callback-debug")
def ctrader_callback_debug(request: Request):
    redirect_debug = get_ctrader_redirect_uri_debug()
    query = dict(request.query_params)
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")
    code_present = bool(request.query_params.get("code"))
    frontend_url = (
        os.getenv("FLOW_SIGNAL_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or "http://127.0.0.1:5501"
    ).rstrip("/")
    reason = None

    if error:
        reason = error_description or error
    elif not code_present:
        reason = "Missing cTrader authorization code"

    return {
        "ok": reason is None,
        "would_redirect_with_ctrader_error": reason is not None,
        "reason": reason,
        "query": query,
        "code_present": code_present,
        "frontend_url": frontend_url,
        "success_redirect_url": f"{frontend_url}/?brokerAccounts=1&ctrader=connected",
        "error_redirect_url": f"{frontend_url}/?brokerAccounts=1&ctrader=error",
        "redirect_uri_env_value": redirect_debug.get("env_value"),
        "redirect_uri_file_value": redirect_debug.get("file_value"),
        "redirect_uri_fallback_value": redirect_debug.get("fallback_value"),
        "redirect_uri_final_value": redirect_debug.get("final_value"),
        "redirect_uri_source": redirect_debug.get("source"),
    }

def get_frontend_redirect_url(status):
    frontend_url = (
        os.getenv("FLOW_SIGNAL_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or "http://127.0.0.1:5501"
    ).rstrip("/")
    redirect_url = f"{frontend_url}/app?brokerAccounts=1&ctrader={status}"

    print("CTRADER_CALLBACK_FRONTEND_REDIRECT =", {
        "frontend_url": frontend_url,
        "status": status,
        "redirect_url": redirect_url,
    })

    return redirect_url


@app.get("/ctrader/callback")
def ctrader_oauth_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")

    if error:
        reason = error_description or error
        redirect_url = get_frontend_redirect_url("error")
        print("CTRADER_CALLBACK_ERROR_DEBUG =", {
            "reason": reason,
            "query": dict(request.query_params),
            "redirect_uri": get_ctrader_redirect_uri_debug(),
            "redirect_url": redirect_url,
        })
        return HTMLResponse(
            f"""
            <!doctype html>
            <html><body>
              <script>
                localStorage.setItem("flowsignalCtraderOAuth", JSON.stringify({{"ok": false, "reason": {json.dumps(reason)}}}));
                window.location.href = {json.dumps(redirect_url)};
              </script>
              cTrader authorization failed. Returning to NathauxFX...
            </body></html>
            """
        )

    if not code:
        reason = "Missing cTrader authorization code"
        redirect_url = get_frontend_redirect_url("error")
        print("CTRADER_CALLBACK_ERROR_DEBUG =", {
            "reason": reason,
            "query": dict(request.query_params),
            "redirect_uri": get_ctrader_redirect_uri_debug(),
            "redirect_url": redirect_url,
        })
        return HTMLResponse(
            f"""
            <!doctype html>
            <html><body>
              <script>
                localStorage.setItem("flowsignalCtraderOAuth", JSON.stringify({{"ok": false, "reason": {json.dumps(reason)}}}));
                window.location.href = {json.dumps(redirect_url)};
              </script>
              Missing authorization code. Returning to NathauxFX...
            </body></html>
            """
        )

    token_result = exchange_ctrader_authorization_code(code)

    if token_result.get("ok"):
        accounts_result = fetch_ctrader_accounts(refresh=True)
        sync_ctrader_account_state(force=True)
        oauth_result = {
            "ok": accounts_result.get("ok", True),
            "reason": accounts_result.get("reason"),
        }
    else:
        oauth_result = token_result
        print("CTRADER_CALLBACK_ERROR_DEBUG =", {
            "reason": token_result.get("reason"),
            "redirect_uri": get_ctrader_redirect_uri_debug(),
        })

    status = "connected" if oauth_result.get("ok") else "error"
    redirect_url = get_frontend_redirect_url(status)

    return HTMLResponse(
        f"""
        <!doctype html>
        <html><body>
          <script>
            localStorage.setItem("flowsignalCtraderOAuth", JSON.stringify({json.dumps(oauth_result)}));
            window.location.href = {json.dumps(redirect_url)};
          </script>
          cTrader authorization finished. Returning to NathauxFX...
        </body></html>
        """
    )


@app.post("/ctrader/disconnect")
def ctrader_disconnect_endpoint():
    return disconnect_ctrader()


@app.get("/ctrader/accounts/refresh")
def ctrader_accounts_refresh_endpoint():
    result = fetch_ctrader_accounts(refresh=True)
    sync_ctrader_account_state(force=True)
    return result


@app.get("/ctrader/accounts")
def ctrader_accounts_endpoint():
    return fetch_ctrader_accounts(refresh=False)


@app.post("/ctrader/accounts/active")
def ctrader_accounts_active_endpoint(payload: dict):
    result = set_active_ctrader_account(
        payload.get("accountId")
        or payload.get("account_id")
    )
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }


@app.post("/ctrader/accounts/forget")
def ctrader_accounts_forget_endpoint(payload: dict):
    result = forget_ctrader_account(
        payload.get("accountId")
        or payload.get("account_id")
    )
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }


@app.post("/ctrader/accounts/clear")
def ctrader_accounts_clear_endpoint():
    result = clear_ctrader_saved_accounts()
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }

@app.post("/debug/set-broker-positions")
async def debug_set_broker_positions(
    payload: DebugBrokerPositionsRequest,
    request: Request
):
    if not is_dev_request(request):
        print(
            "DEBUG BROKER POSITIONS BLOCKED:",
            {
                "client_host": request.client.host if request.client else "",
                "origin": request.headers.get("origin", ""),
            }
        )

        return {
            "ok": False,
            "message": "Debug endpoint is only available locally"
        }

    received_payload = payload.model_dump()
    requested_positions = received_payload.get("positions")

    print("DEBUG BROKER POSITIONS PAYLOAD:", received_payload)

    if not isinstance(requested_positions, list):
        print("DEBUG BROKER POSITIONS VALIDATION ERROR:", received_payload)

        return {
            "ok": False,
            "message": "Expected payload shape: { positions: [] }"
        }

    positions = set_debug_open_positions(requested_positions)

    print("DEBUG BROKER POSITIONS:", positions)

    return {
        "ok": True,
        "positions": positions
    }

@app.post("/connect-ctrader")
def connect_ctrader(payload: dict):
    from ctrader_connector import get_ctrader_config

    config = get_ctrader_config()
    mode = str(payload.get("mode") or (config.get("env") if config else "demo")).lower()
    account_id = payload.get("account_id")

    if mode not in ["demo", "live"]:
        return {
            "ok": False,
            "message": "Invalid mode"
        }

    connector_result = connect_account(account_id, mode)
    accounts_result = fetch_ctrader_accounts(refresh=True)
    sync_ctrader_account_state()

    print("CTRADER CONNECTED:", LIVE_ACCOUNT_STATE)

    return {
        "ok": connector_result.get("ok", True),
        "reason": connector_result.get("reason") or accounts_result.get("reason"),
        "connected": LIVE_ACCOUNT_STATE["connected"],
        "mode": LIVE_ACCOUNT_STATE["mode"],
        "broker": LIVE_ACCOUNT_STATE["broker"],
        "account_id": LIVE_ACCOUNT_STATE["account_id"],
        "execution_ready": LIVE_ACCOUNT_STATE["execution_ready"],
        "accounts": accounts_result.get("accounts", []),
        "active_account_id": accounts_result.get("active_account_id"),
    }

@app.get("/ctrader-accounts")
def ctrader_accounts():
    return fetch_ctrader_accounts(refresh=False)


@app.post("/refresh-ctrader-accounts")
def refresh_ctrader_accounts():
    result = fetch_ctrader_accounts(refresh=True)
    sync_ctrader_account_state(force=True)
    return result


@app.post("/set-active-ctrader-account")
def set_active_ctrader_account_endpoint(payload: dict):
    result = set_active_ctrader_account(payload.get("account_id"))
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }


@app.post("/forget-ctrader-account")
def forget_ctrader_account_endpoint(payload: dict):
    result = forget_ctrader_account(payload.get("account_id"))
    sync_ctrader_account_state(force=True)
    return {
        **result,
        "live_account": LIVE_ACCOUNT_STATE,
    }


@app.post("/disconnect-ctrader")
def disconnect_ctrader():
    # Disconnecting the transport pauses execution; it must not erase the
    # owner's persisted LIVE Auto preference. Reconnection resumes the same
    # preference after the normal broker/final-entry safety checks pass.
    live_preference_kept = bool(LIVE_AUTO_TRADE_ENABLED["enabled"])

    disconnected_at = time.time()

    for symbol, trade in list(LIVE_ACTIVE_ORDERS.items()):
        if not trade:
            continue

        order_id = trade.get("order_id")
        history_trade = None

        for item in LIVE_TRADE_HISTORY:
            if order_id and item.get("order_id") == order_id:
                history_trade = item
                break

        disconnected_trade = {
            **trade,
            "symbol": symbol,
            "result": "DISCONNECTED",
            "closed_at": disconnected_at,
            "note": "NathauxFX tracking stopped; broker positions were not auto-closed."
        }

        move_live_trade_to_history_once(disconnected_trade)

    del LIVE_TRADE_HISTORY[MAX_LIVE_TRADE_HISTORY:]

    disconnect_account()
    sync_ctrader_account_state()
    LIVE_ACTIVE_ORDERS["EURUSD"] = None
    LIVE_ACTIVE_ORDERS["XAUUSD"] = None
    save_live_backup()

    print("CTRADER DISCONNECTED")

    return {
        "ok": True,
        "connected": False,
        "mode": "demo",
        "broker": "ctrader",
        "account_id": None,
        "execution_ready": False,
        "live_auto_enabled": live_preference_kept,
        "live_execution_paused": live_preference_kept,
        "pause_reason": "broker_disconnected" if live_preference_kept else None,
    }

@app.post("/close-live-trade")
def close_live_trade(payload: dict):
    symbol = normalize_symbol(payload.get("symbol"))

    if symbol not in LIVE_ACTIVE_ORDERS or not LIVE_ACTIVE_ORDERS.get(symbol):
        return {
            "ok": False,
            "symbol": symbol,
            "reason": "No active live trade for symbol",
            "message": "No active live trade for symbol"
        }

    active_trade = LIVE_ACTIVE_ORDERS[symbol]
    closed_at = time.time()
    warning = None

    if active_trade.get("source") == "broker":
        remove_debug_open_position(symbol)
        warning = "Broker close is simulated only"

    closed_trade = {
        **active_trade,
        "symbol": symbol,
        "trade_id": get_live_trade_identity(active_trade),
        "status": "MANUAL_CLOSE",
        "result": "MANUAL_CLOSE",
        "closed_at": closed_at,
    }
    ensure_live_trade_identity(closed_trade, symbol)

    if warning:
        closed_trade["warning"] = warning

    move_live_trade_to_history_once(closed_trade)
    invalidate_symbol_setup_state(
        symbol,
        closed_at,
        reason="position manually closed",
    )
    log_live_trade_audit("manual_close", closed_trade, reason=warning)
    LIVE_ACTIVE_ORDERS[symbol] = None
    save_live_backup()

    print("LIVE TRADE MANUAL CLOSE:", closed_trade)

    response = {
        "ok": True,
        "symbol": symbol,
        "closed_trade": closed_trade
    }

    if warning:
        response["warning"] = warning

    return response


@app.post("/modify-live-position-levels")
def modify_live_position_levels(payload: dict):
    symbol = normalize_symbol(payload.get("symbol"))
    trade = LIVE_ACTIVE_ORDERS.get(symbol)

    if not trade:
        return {"ok": False, "reason": "No active live trade for symbol"}

    position_id = trade.get("position_id") or trade.get("broker_position_id")
    requested_position_id = payload.get("position_id")

    if not position_id or (
        requested_position_id is not None
        and str(requested_position_id) != str(position_id)
    ):
        return {"ok": False, "reason": "Live position does not match"}

    try:
        entry = float(trade.get("entry") or trade.get("entry_price"))
        stop_loss = float(payload.get("stop_loss"))
        tp1 = float(payload.get("tp1"))
        tp2 = float(payload.get("tp2"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Entry, SL, TP1, and TP2 must be valid prices"}

    side = str(trade.get("side") or trade.get("action") or "").upper()
    if side == "BUY" and not (stop_loss < entry < tp1 <= tp2):
        return {
            "ok": False,
            "reason": "BUY requires SL below Entry and TP1/TP2 above Entry",
        }
    if side == "SELL" and not (stop_loss > entry > tp1 >= tp2):
        return {
            "ok": False,
            "reason": "SELL requires SL above Entry and TP1/TP2 below Entry",
        }
    if side not in {"BUY", "SELL"}:
        return {"ok": False, "reason": "Unknown live trade side"}

    changed_level = str(payload.get("changed_level") or "").lower()
    if changed_level not in {"sl", "tp1", "tp2"}:
        return {"ok": False, "reason": "Unknown trade level"}

    if changed_level == "sl":
        old_sl = trade.get("current_sl") or trade.get("sl")
        current_units = (
            trade.get("volume_units")
            or (trade.get("risk") or {}).get("volume_units")
        )
        allowed_risk_size = calculate_live_risk_size(symbol, entry, stop_loss)
        sl_risk_check = validate_application_sl_amendment(
            entry,
            old_sl,
            stop_loss,
            current_units,
            allowed_risk_size,
        )
        persist_execution_risk_audit_safely(
            symbol=symbol,
            event_type="APPLICATION_SL_AMENDMENT",
            source="chart_levels",
            broker_position_id=position_id,
            old_entry=entry,
            new_entry=entry,
            old_sl=old_sl,
            new_sl=stop_loss,
            volume_units=current_units,
            approved_risk_amount=(trade.get("risk") or {}).get("risk_amount"),
            approved_risk_percent=(trade.get("risk") or {}).get("risk_percent"),
            status=("PASS" if sl_risk_check.get("ok") else "RISK_EXCEEDED_AFTER_SL_CHANGE"),
            details={
                **sl_risk_check,
                "application_generated": True,
                "interference_with_direct_broker_action": False,
            },
        )
        if not sl_risk_check.get("ok"):
            print("RISK_EXCEEDED_AFTER_SL_CHANGE =", {
                "symbol": symbol,
                "position_id": position_id,
                "old_sl": old_sl,
                "new_sl": stop_loss,
                "details": sl_risk_check,
            })

    broker_result = {"ok": True, "local_only": changed_level == "tp1"}

    if changed_level in {"sl", "tp2"}:
        broker_result = modify_position_sltp(
            position_id,
            stop_loss_price=stop_loss,
            take_profit_price=tp2,
        )
        if not broker_result.get("ok"):
            print("backendUpdate fail", {
                "symbol": symbol,
                "position_id": position_id,
                "changed_level": changed_level,
                "reason": broker_result.get("reason"),
            })
            return {
                "ok": False,
                "reason": broker_result.get("reason") or "cTrader rejected the modification",
                "broker_result": broker_result,
            }

    modified_at = time.time()
    user_modified_levels = {
        **(
            trade.get("user_modified_levels")
            if isinstance(trade.get("user_modified_levels"), dict)
            else {}
        ),
        "sl": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "updated_at": modified_at,
    }
    trade.update({
        "sl": stop_loss,
        "current_sl": stop_loss,
        "tp1": tp1,
        "take_profit_1": tp1,
        "tp2": tp2,
        "take_profit_2": tp2,
        "take_profit": tp2,
        "broker_stop_loss_confirmed": True,
        "broker_stop_loss_missing": False,
        "broker_take_profit_confirmed": True,
        "broker_take_profit_missing": False,
        "levels_modified_at": modified_at,
        "user_modified_levels": user_modified_levels,
    })
    log_live_trade_audit(
        "chart_levels_modified",
        trade,
        reason=f"Changed {changed_level or 'levels'} from chart",
    )
    save_live_backup()
    print("backendUpdate success", {
        "symbol": symbol,
        "position_id": position_id,
        "changed_level": changed_level,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
    })

    return {
        "ok": True,
        "symbol": symbol,
        "changed_level": changed_level,
        "active_order": trade,
        "broker_result": broker_result,
    }

@app.post("/live-auto-toggle")
def live_auto_toggle(payload: dict):
    sync_ctrader_account_state()

    enabled = bool(
        payload.get("enabled", False)
    )

    if enabled and not LIVE_ACCOUNT_STATE["connected"]:
        return {
            "status": "error",
            "enabled": LIVE_AUTO_TRADE_ENABLED["enabled"],
            "message": "Connect cTrader broker before enabling Auto Trade"
        }

    previous_enabled = LIVE_AUTO_TRADE_ENABLED["enabled"]
    LIVE_AUTO_TRADE_ENABLED["enabled"] = enabled
    try:
        state = save_auto_trade_state(
            updated_by=str(payload.get("updated_by") or "web_user"),
            request_source=str(payload.get("source") or "live_auto_toggle"),
            reason="User changed Live Auto",
            changed_mode="live",
        )
    except Exception as exc:
        LIVE_AUTO_TRADE_ENABLED["enabled"] = previous_enabled
        raise HTTPException(
            status_code=503,
            detail=f"Could not persist Live Auto state: {exc}",
        ) from exc

    print(
        "LIVE AUTO TRADE STATE:",
        LIVE_AUTO_TRADE_ENABLED["enabled"]
    )

    return {
        "status": "ok",
        "enabled": LIVE_AUTO_TRADE_ENABLED["enabled"],
        "state": state,
    }


def parse_execution_timestamp(value):
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


def validate_auto_entry_state_locked(
    symbol,
    side,
    trade_payload,
    broker_positions,
    now=None,
):
    normalized_symbol = normalize_symbol(symbol)
    side = str(side or "").upper()
    now = float(now if now is not None else time.time())
    last_closed_at = float(
        LIVE_LAST_POSITION_CLOSED_AT.get(normalized_symbol, 0) or 0
    )
    break_close = parse_execution_timestamp(
        trade_payload.get("fifteen_m_break_close_time")
    )
    confirmation_close = parse_execution_timestamp(
        trade_payload.get("five_m_confirmation_close_time")
    )
    trend = (
        trade_payload.get("trend_15m")
        if isinstance(trade_payload.get("trend_15m"), dict)
        else {}
    )
    setup_identity = (
        trade_payload.get("setup_identity")
        if isinstance(trade_payload.get("setup_identity"), dict)
        else {}
    )
    details = {
        "symbol": normalized_symbol,
        "side": side,
        "signal_setup_id": trade_payload.get("signal_setup_id"),
        "fifteen_m_break_close_time": (
            break_close.isoformat() if break_close else None
        ),
        "five_m_confirmation_close_time": (
            confirmation_close.isoformat() if confirmation_close else None
        ),
        "previous_position_closed_at": (
            datetime.fromtimestamp(last_closed_at, tz=timezone.utc).isoformat()
            if last_closed_at > 0
            else None
        ),
        "ema_trend": trend.get("trend"),
        "buy_allowed": bool(trend.get("buy_allowed")),
        "sell_allowed": bool(trend.get("sell_allowed")),
        "post_close_cooldown_seconds": get_live_post_close_cooldown_seconds(),
        "setup_identity": copy.deepcopy(setup_identity),
    }

    active_order = LIVE_ACTIVE_ORDERS.get(normalized_symbol)
    if active_order and get_live_trade_status(active_order) in [
        "RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"
    ]:
        return {"ok": False, "reason": "active position exists", "details": details}

    if any(
        normalize_symbol(position.get("symbol")) == normalized_symbol
        for position in (broker_positions or [])
    ):
        return {"ok": False, "reason": "broker position exists", "details": details}

    source_event_id = trade_payload.get("source_indicator_event_id")
    if source_event_id:
        lifecycle = (
            get_event_lifecycles([source_event_id]).get(source_event_id, {}).get("LIVE")
            or {}
        )
        details["indicator_event_lifecycle"] = lifecycle
        if lifecycle.get("status") in {
            "CONSUMED", "EXPIRED", "INVALIDATED", "SUBMITTING",
            "RECONCILIATION_REQUIRED",
        }:
            return {
                "ok": False,
                "reason": f"indicator event unavailable: {lifecycle.get('status')}",
                "details": details,
            }

    if not trade_payload.get("signal_setup_id"):
        return {"ok": False, "reason": "missing setup fingerprint", "details": details}

    required_identity_fields = {
        "symbol",
        "direction",
        "swing_type",
        "swing_timestamp",
        "swing_price",
        "bos_candle_timestamp",
        "bos_level",
        "confirmation_timestamp",
    }
    if trade_payload.get("source_indicator_event_id"):
        required_identity_fields.update({"indicator_event_id", "m5_confirmation_id"})
    if any(setup_identity.get(field) in [None, ""] for field in required_identity_fields):
        return {"ok": False, "reason": "missing setup swing identity", "details": details}
    expected_setup_id = get_signal_setup_id(trade_payload, side)
    details["recalculated_signal_setup_id"] = expected_setup_id
    if expected_setup_id != trade_payload.get("signal_setup_id"):
        return {"ok": False, "reason": "setup fingerprint changed", "details": details}

    if break_close is None or confirmation_close is None:
        return {"ok": False, "reason": "missing closed-candle timestamps", "details": details}

    now_datetime = datetime.fromtimestamp(now, tz=timezone.utc)
    if break_close > now_datetime or confirmation_close > now_datetime:
        return {"ok": False, "reason": "setup contains a future candle close", "details": details}

    if confirmation_close <= break_close:
        return {
            "ok": False,
            "reason": "5m confirmation did not close after 15m BOS close",
            "details": details,
        }

    if last_closed_at > 0:
        previous_close = datetime.fromtimestamp(last_closed_at, tz=timezone.utc)
        if break_close <= previous_close:
            return {"ok": False, "reason": "15m BOS predates position close", "details": details}
        if confirmation_close <= previous_close:
            return {"ok": False, "reason": "5m confirmation predates position close", "details": details}
        cooldown_seconds = get_live_post_close_cooldown_seconds()
        if now - last_closed_at < cooldown_seconds:
            details["post_close_cooldown_remaining_seconds"] = round(
                cooldown_seconds - (now - last_closed_at),
                2,
            )
            return {"ok": False, "reason": "post-close cooldown active", "details": details}

    ema_allowed = (
        side == "BUY" and bool(trend.get("buy_allowed"))
    ) or (
        side == "SELL" and bool(trend.get("sell_allowed"))
    )
    if not ema_allowed:
        return {"ok": False, "reason": f"15m EMA does not allow {side}", "details": details}

    return {"ok": True, "reason": None, "details": details}


def validate_fresh_ema_permission_locked(symbol, side, setup_identity=None):
    normalized_symbol = normalize_symbol(symbol)
    side = str(side or "").upper()
    details = {
        "symbol": normalized_symbol,
        "side": side,
        "fresh_ema_recalculated": False,
    }
    try:
        from strategies import strict_trader

        latest_15m = get_ctrader_market_data(
            normalized_symbol,
            "15m",
            limit=100,
            force_refresh=True,
        )
        closed_15m = strict_trader.closed_frame(latest_15m, 15)
        if closed_15m is None or len(closed_15m) < 21:
            details["reason"] = "latest closed 15m candles unavailable"
            return {
                "ok": False,
                "reason": "WAIT_EMA_CHANGED_BEFORE_EXECUTION",
                "details": details,
            }

        trend = strict_trader.trend_filter(closed_15m, normalized_symbol)
        consolidation = strict_trader.classify_consolidation(
            closed_15m,
            normalized_symbol,
        )
        execution_settings = get_cached_execution_settings()
        consolidation_filter_enabled = bool(
            execution_settings.get("consolidation_filter_enabled", True)
        )
        consolidation_blocked = bool(
            consolidation_filter_enabled and consolidation.get("is_consolidation")
        )
        details.update({
            "fresh_ema_recalculated": True,
            "fresh_ema_trend": trend.get("trend"),
            "fresh_ema9": trend.get("ema_fast"),
            "fresh_ema21": trend.get("ema_slow"),
            "fresh_ema_close": trend.get("close"),
            "fresh_buy_allowed": bool(trend.get("buy_allowed")),
            "fresh_sell_allowed": bool(trend.get("sell_allowed")),
            "fresh_last_closed_15m": str(closed_15m.index[-1]),
            "fresh_consolidation": consolidation,
            "consolidation_filter_enabled": consolidation_filter_enabled,
            "consolidation_blocked": consolidation_blocked,
        })
        allowed = (
            side == "BUY" and bool(trend.get("buy_allowed"))
        ) or (
            side == "SELL" and bool(trend.get("sell_allowed"))
        )
        if not allowed:
            return {
                "ok": False,
                "reason": "WAIT_EMA_CHANGED_BEFORE_EXECUTION",
                "details": details,
            }
        if consolidation_blocked:
            return {
                "ok": False,
                "reason": "WAIT_CONSOLIDATION",
                "details": details,
            }

        identity = setup_identity if isinstance(setup_identity, dict) else {}
        if not identity:
            return {
                "ok": False,
                "reason": "WAIT_SETUP_SWING_IDENTITY_MISSING",
                "details": details,
            }
        expected_type = str(identity.get("swing_type") or "").upper()
        expected_time = parse_execution_timestamp(identity.get("swing_timestamp"))
        try:
            expected_price = float(identity.get("swing_price"))
        except (TypeError, ValueError):
            expected_price = None
        valid_swings = strict_trader.detect_valid_swings(
            closed_15m.iloc[:-1].copy(),
            normalized_symbol,
        )
        matching_swing = None
        for swing in valid_swings:
            swing_time = parse_execution_timestamp(swing.get("time"))
            try:
                swing_price = float(swing.get("price"))
            except (TypeError, ValueError):
                continue
            if (
                str(swing.get("type") or "").upper() == expected_type
                and swing_time == expected_time
                and expected_price is not None
                and abs(swing_price - expected_price)
                <= strict_trader.point_size(normalized_symbol) + 1e-12
            ):
                matching_swing = swing
                break
        details["fresh_setup_swing_matched"] = matching_swing is not None
        if matching_swing is None:
            return {
                "ok": False,
                "reason": "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION",
                "details": details,
            }
        return {"ok": True, "reason": None, "details": details}
    except Exception as exc:
        details["reason"] = str(exc)
        return {
            "ok": False,
            "reason": "WAIT_EMA_CHANGED_BEFORE_EXECUTION",
            "details": details,
        }


def place_market_order_with_inflight_cleanup(symbol, **order_kwargs):
    """Release the symbol guard if the broker call raises or times out."""
    broker_call_completed = False
    try:
        result = place_market_order(symbol=symbol, **order_kwargs)
        broker_call_completed = True
        return result
    finally:
        if not broker_call_completed:
            with LIVE_ORDER_LOCK:
                LIVE_ORDER_IN_FLIGHT.discard(symbol)


def _execute_live_order_core_impl(payload: dict, source="manual", _inflight_guard=None):
    global LAST_EXECUTION_TIME

    trade_payload = prepare_ctrader_trade(payload)

    symbol = trade_payload.get("symbol")
    side = trade_payload.get("action")
    plan = get_signal_trade_plan(symbol) or {}
    log_live_xauusd_execution_debug(
        symbol,
        plan=plan,
        trade_payload=trade_payload,
        stage="payload_prepared",
        blocked_by=None if trade_payload.get("ok") else "payload_validation",
        blocked_reason=None if trade_payload.get("ok") else trade_payload.get("reason") or trade_payload.get("message"),
        payload_valid=bool(trade_payload.get("ok")),
        order_sent=False,
        order_accepted=False,
    )

    if not LIVE_AUTO_TRADE_ENABLED["enabled"]:
        log_prefix = (
            "CTRADER AUTO TRADE BLOCKED:"
            if source == "auto"
            else "LIVE EXECUTION BLOCKED:"
        )
        print(log_prefix, "LIVE auto disabled")

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason="Live Auto is off"
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="live_auto_disabled",
                reason="Live Auto is off"
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            stage="live_auto_disabled",
            blocked_by="live_auto_disabled",
            blocked_reason="Live Auto is off",
            payload_valid=bool(trade_payload.get("ok")),
            order_sent=False,
            order_accepted=False,
        )
        return reject_ctrader_order(
            symbol,
            side,
            trade_payload.get("entry"),
            trade_payload.get("sl"),
            trade_payload.get("tp1"),
            trade_payload.get("tp2"),
            "LIVE auto disabled"
        )

    if not trade_payload.get("ok"):
        original_reason = (
            trade_payload.get("reason")
            or trade_payload.get("message")
            or "validation failed"
        )
        reason = original_reason
        if (
            not any(
                is_missing_trade_value(trade_payload.get(key))
                for key in ["entry", "sl", "tp1", "tp2"]
            )
            and any(token in str(original_reason).lower() for token in [
                "sl",
                "tp",
                "distance",
                "too close",
            ])
        ):
            reason = "LIVE BLOCKED: invalid SL/TP distance."
            trade_payload["reason"] = reason
            trade_payload["message"] = reason
            trade_payload["level_validation_reason"] = original_reason

        if any(token in str(reason).lower() for token in [
            "entry",
            "sl",
            "tp",
            "level",
            "number",
            "distance",
        ]):
            print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
                "symbol": symbol,
                "side": side,
                "reason": reason,
                "entry": trade_payload.get("entry"),
                "sl": trade_payload.get("sl"),
                "tp1": trade_payload.get("tp1"),
                "tp2": trade_payload.get("tp2"),
            })
        log_broker_min_distance_decision(
            symbol,
            trade_payload.get("distance_details"),
            actual_blocked=True,
            final_block_reason=reason
        )
        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=reason,
                details=trade_payload.get("distance_details")
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="trade_payload_validation",
                reason=reason,
                details=trade_payload.get("distance_details")
            )
            print("CTRADER AUTO TRADE BLOCKED:", reason)
        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            stage="trade_payload_validation",
            blocked_by="payload_validation",
            blocked_reason=reason,
            payload_valid=False,
            order_sent=False,
            order_accepted=False,
        )
        return trade_payload

    levels_ok, levels_reason = trade_payload_has_required_levels(trade_payload)

    if not levels_ok:
        print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
            "symbol": symbol,
            "side": side,
            "reason": levels_reason,
            "entry": trade_payload.get("entry"),
            "sl": trade_payload.get("sl"),
            "tp1": trade_payload.get("tp1"),
            "tp2": trade_payload.get("tp2"),
        })
        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=levels_reason,
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="missing_tp_levels",
                reason=levels_reason,
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            stage="missing_tp_levels",
            blocked_by="missing_tp_levels",
            blocked_reason=levels_reason,
            payload_valid=False,
            order_sent=False,
            order_accepted=False,
        )
        return reject_ctrader_order(
            symbol,
            side,
            trade_payload.get("entry"),
            trade_payload.get("sl"),
            trade_payload.get("tp1"),
            trade_payload.get("tp2"),
            levels_reason,
        )

    if source == "auto" and trade_payload.get("adjusted_for_broker_distance"):
        log_broker_min_distance_decision(
            symbol,
            trade_payload.get("distance_details"),
            actual_blocked=False,
            final_block_reason=trade_payload.get("adjustment_reason")
        )
        set_auto_trade_status(
            symbol=symbol,
            signal=trade_payload.get("signal"),
            action=side,
            status="READY",
            reason=trade_payload.get("adjustment_reason"),
            details=trade_payload.get("distance_details")
        )

    active_order = LIVE_ACTIVE_ORDERS.get(symbol)
    active_status = get_live_trade_status(active_order)

    if active_order and active_status in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]:
        active_side = str(
            active_order.get("side")
            or active_order.get("action")
            or "trade"
        ).upper()
        duplicate_reason = (
            f"{symbol} blocked — existing {active_side} trade already running"
        )
        log_message = (
            f"CTRADER AUTO TRADE BLOCKED: {duplicate_reason}"
            if source == "auto"
            else "LIVE EXECUTION BLOCKED: active trade already exists"
        )

        if source == "auto":
            log_broker_min_distance_decision(
                symbol,
                trade_payload.get("distance_details"),
                actual_blocked=True,
                final_block_reason=duplicate_reason
            )
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=duplicate_reason
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="duplicate_active_trade",
                reason=duplicate_reason,
                details={
                    "active_status": active_status,
                    "active_side": active_side,
                    "active_position_id": (
                        active_order.get("position_id")
                        or active_order.get("broker_position_id")
                        or active_order.get("broker_order_id")
                        or active_order.get("order_id")
                    )
                }
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            stage="duplicate_active_trade",
            blocked_by="duplicate_active_trade",
            blocked_reason=duplicate_reason,
            existing_position=active_order,
            payload_valid=True,
            order_sent=False,
            order_accepted=False,
        )
        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            duplicate_reason,
            log_message
        )

    loss_limit_status = get_live_loss_limit_status()

    if loss_limit_status.get("blocked"):
        reason = (
            loss_limit_status.get("reason")
            or "LIVE BLOCKED: risk loss limit reached."
        )
        log_message = (
            f"CTRADER AUTO TRADE BLOCKED: {reason}"
            if source == "auto"
            else f"LIVE EXECUTION BLOCKED: {reason}"
        )

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=reason,
                details=loss_limit_status,
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="risk_loss_limit",
                reason=reason,
                details=loss_limit_status,
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            stage="risk_loss_limit",
            blocked_by="risk_loss_limit",
            blocked_reason=reason,
            payload_valid=True,
            order_sent=False,
            order_accepted=False,
        )
        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            reason,
            log_message,
            details=loss_limit_status,
        )

    risk_size = calculate_live_risk_size(
        symbol,
        trade_payload.get("entry"),
        trade_payload.get("sl")
    )

    if not risk_size.get("ok"):
        reason = risk_size.get("reason") or "Live risk sizing failed"
        risk_calculation_audit = log_live_risk_calculation_audit(
            symbol,
            side,
            trade_payload,
            risk_size,
            reason=reason,
        )
        live_risk_debug = build_live_risk_debug(
            symbol,
            side,
            trade_payload,
            risk_size,
            broker_reject_reason=reason,
        )
        risk_block_details = build_live_block_details(
            risk_size,
            live_risk_debug,
            {
                "blocked_reason": reason,
                "broker_rejection_reason": reason,
                "live_risk_debug": live_risk_debug,
                "live_risk_calculation_audit": risk_calculation_audit,
            },
        )

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=reason,
                details=risk_block_details,
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="risk_sizing",
                reason=reason,
                details=risk_block_details,
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            risk_size=risk_size,
            stage="risk_sizing",
            blocked_by="risk_sizing",
            blocked_reason=reason,
            payload_valid=False,
            order_sent=False,
            order_accepted=False,
        )
        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            reason,
            reason,
            details=risk_block_details,
        )

    expected_loss_usd = calculate_expected_loss_usd_from_risk_size(risk_size)
    max_risk_usd = risk_size.get("risk_amount")
    try:
        max_risk_value = float(max_risk_usd)
    except (TypeError, ValueError):
        max_risk_value = None

    if is_expected_loss_oversized(expected_loss_usd, max_risk_value):
        reason = "LIVE_RISK_OVERSIZE_BLOCK: expected SL loss exceeds configured max risk"
        risk_calculation_audit = log_live_risk_calculation_audit(
            symbol,
            side,
            trade_payload,
            {
                **risk_size,
                "expected_loss_usd": expected_loss_usd,
                "max_risk_usd": max_risk_usd,
            },
            reason=reason,
        )
        print("LIVE_RISK_OVERSIZE_BLOCK =", build_live_protection_audit(
            symbol,
            side,
            entry=trade_payload.get("entry"),
            saved_sl=trade_payload.get("sl"),
            broker_sl=None,
            displayed_sl=None,
            tp2=trade_payload.get("tp2"),
            broker_tp=None,
            lot_size=risk_size.get("lot_size"),
            expected_loss_usd=(
                round(expected_loss_usd, 2)
                if isinstance(expected_loss_usd, (int, float))
                else expected_loss_usd
            ),
            max_risk_usd=max_risk_usd,
            repair_result=None,
            stage="pre_order_risk_validation",
        ))

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=reason,
                details={
                    **risk_size,
                    "expected_loss_usd": expected_loss_usd,
                    "max_risk_usd": max_risk_usd,
                    "live_risk_calculation_audit": risk_calculation_audit,
                },
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="risk_oversize",
                reason=reason,
                details={
                    **risk_size,
                    "expected_loss_usd": expected_loss_usd,
                    "max_risk_usd": max_risk_usd,
                    "live_risk_calculation_audit": risk_calculation_audit,
                },
            )

        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            reason,
            reason,
            details={
                **risk_size,
                "expected_loss_usd": expected_loss_usd,
                "max_risk_usd": max_risk_usd,
                "live_risk_calculation_audit": risk_calculation_audit,
            },
        )

    rr_validation = validate_live_trade_risk_reward(
        symbol,
        side,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp2"),
    )

    if not rr_validation.get("ok"):
        reason = rr_validation.get("reason")
        details = {
            **rr_validation,
            "entry": trade_payload.get("entry"),
            "sl": trade_payload.get("sl"),
            "tp2": trade_payload.get("tp2"),
        }

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=reason,
                details=details,
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="risk_reward_validation",
                reason=reason,
                details=details,
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            risk_size=risk_size,
            stage="risk_reward_validation",
            blocked_by="risk_reward_validation",
            blocked_reason=reason,
            payload_valid=False,
            order_sent=False,
            order_accepted=False,
        )
        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            reason,
            reason,
            details=details,
        )

    trade_payload.update({
        "volume": risk_size["lot_size"],
        "lot_size": risk_size["lot_size"],
        "volume_units": risk_size["volume_units"],
        "risk_percent": risk_size["risk_percent"],
        "risk_amount": risk_size["risk_amount"],
        "sl_pips": risk_size["sl_pips"],
        "account_balance_used": risk_size.get("account_balance"),
        "account_equity": risk_size.get("account_equity"),
        "account_equity_used": risk_size.get("account_equity_used"),
        "risk": risk_size,
    })
    log_structure_tp_trade_audit(symbol, side, trade_payload, risk_size, plan)
    log_xauusd_live_risk_diagnostics(symbol, trade_payload, risk_size)
    log_live_xauusd_execution_debug(
        symbol,
        plan=plan,
        trade_payload=trade_payload,
        risk_size=risk_size,
        stage="risk_sizing_ok",
        blocked_by=None,
        blocked_reason=None,
        payload_valid=True,
        order_sent=False,
        order_accepted=False,
    )

    with LIVE_ORDER_LOCK:
        if symbol in LIVE_ORDER_IN_FLIGHT:
            duplicate_reason = (
                f"{symbol} blocked — order already being sent"
            )

            if source == "auto":
                set_auto_trade_status(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    action=side,
                    status="BLOCKED",
                    reason=duplicate_reason
                )
                log_auto_trade_blocked_reason(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    stage="duplicate_order_in_flight",
                    reason=duplicate_reason,
                )

            print("LIVE DUPLICATE ORDER BLOCKED:", {
                "symbol": symbol,
                "reason": duplicate_reason,
                "source": source,
            })

            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                trade_payload=trade_payload,
                risk_size=risk_size,
                stage="duplicate_order_in_flight",
                blocked_by="duplicate_order_in_flight",
                blocked_reason=duplicate_reason,
                existing_position=LIVE_ACTIVE_ORDERS.get(symbol),
                payload_valid=True,
                order_sent=False,
                order_accepted=False,
            )
            return reject_live_execution_block(
                symbol,
                side,
                trade_payload,
                duplicate_reason,
                "LIVE EXECUTION BLOCKED: order already in flight"
            )

        active_order = LIVE_ACTIVE_ORDERS.get(symbol)
        active_status = get_live_trade_status(active_order)

        if active_order and active_status in ["RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"]:
            active_side = str(
                active_order.get("side")
                or active_order.get("action")
                or "trade"
            ).upper()
            duplicate_reason = (
                f"{symbol} blocked — existing {active_side} trade already running"
            )

            if source == "auto":
                set_auto_trade_status(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    action=side,
                    status="BLOCKED",
                    reason=duplicate_reason
                )
                log_auto_trade_blocked_reason(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    stage="duplicate_active_trade_final_gate",
                    reason=duplicate_reason,
                )

            log_live_xauusd_execution_debug(
                symbol,
                plan=plan,
                trade_payload=trade_payload,
                risk_size=risk_size,
                stage="duplicate_active_trade_final_gate",
                blocked_by="duplicate_active_trade",
                blocked_reason=duplicate_reason,
                existing_position=active_order,
                payload_valid=True,
                order_sent=False,
                order_accepted=False,
            )
            return reject_live_execution_block(
                symbol,
                side,
                trade_payload,
                duplicate_reason,
                "LIVE EXECUTION BLOCKED: active trade already exists"
            )

        broker_positions_before_send = get_open_positions()
        # Auto orders and manual clicks carrying a strategy setup fingerprint
        # must use fresh EMA data. A fully user-entered manual market order has
        # no setup fingerprint and remains on the separate discretionary path.
        strategy_generated_order = bool(
            source == "auto" or trade_payload.get("signal_setup_id")
        )
        is_news_order = bool(trade_payload.get("news_event_id"))
        news_runtime = evaluate_news_entry_state(
            PANEL_CACHE.get("data"),
            symbol,
            side=side,
            audit=True,
        )
        if is_news_order:
            news_context = news_runtime.get("market_context") or {}
            tick = news_context.get("tick") or {}
            try:
                spread = abs(float(tick.get("ask")) - float(tick.get("bid")))
                maximum_spread = float(
                    (trade_payload.get("news_confirmation") or {}).get(
                        "confirmation_buffer"
                    )
                )
                spread_ok = spread <= maximum_spread
            except (TypeError, ValueError):
                spread_ok = False
            market_health = check_live_market_data_health(symbol)
            previous_close = float(
                LIVE_LAST_POSITION_CLOSED_AT.get(symbol, 0) or 0
            )
            cooldown_active = bool(
                previous_close
                and time.time() - previous_close
                < get_live_post_close_cooldown_seconds()
            )
            expected_news = {
                "event_id": trade_payload.get("news_event_id"),
                "expected_symbol_direction": side,
                "news_plan": {
                    "signal": side,
                    "entry_price": trade_payload.get("entry"),
                    "stop_loss": trade_payload.get("sl"),
                    "tp1": trade_payload.get("tp1"),
                    "tp2": trade_payload.get("tp2"),
                    "signal_setup_id": trade_payload.get("signal_setup_id"),
                },
            }
            locked_news_gate = news_trading.final_gate(
                expected_news,
                news_runtime.get("events") or [],
                symbol,
                news_context.get("candles_5m") or [],
                news_context.get("entry_price"),
                active_position=any(
                    normalize_symbol(position.get("symbol")) == symbol
                    for position in broker_positions_before_send
                ),
                cooldown_active=cooldown_active,
                feed_fresh=bool(market_health.get("ok")),
                spread_ok=spread_ok,
                data_age_seconds=get_calendar_data_age_seconds(),
                audit=True,
            )
            locked_risk_size = calculate_live_risk_size(
                symbol,
                trade_payload.get("entry"),
                trade_payload.get("sl"),
            )
            locked_rr = validate_live_trade_risk_reward(
                symbol,
                side,
                trade_payload.get("entry"),
                trade_payload.get("sl"),
                trade_payload.get("tp2"),
            )
            risk_changed = bool(
                not locked_risk_size.get("ok")
                or locked_risk_size.get("lot_size") != risk_size.get("lot_size")
                or locked_risk_size.get("volume_units") != risk_size.get("volume_units")
                or locked_risk_size.get("risk_amount") != risk_size.get("risk_amount")
            )
            if risk_changed or not locked_rr.get("ok"):
                locked_news_gate = {
                    **locked_news_gate,
                    "ok": False,
                    "reason": (
                        "WAIT_NEWS_RISK_CHANGED_BEFORE_EXECUTION"
                        if risk_changed
                        else "WAIT_NEWS_RR_CHANGED_BEFORE_EXECUTION"
                    ),
                    "locked_risk_size": locked_risk_size,
                    "initial_risk_size": risk_size,
                    "locked_risk_reward": locked_rr,
                }
            print("LIVE_NEWS_FINAL_ENTRY_GATE =", locked_news_gate)
            if not locked_news_gate.get("ok"):
                reason = locked_news_gate.get("reason") or "NEWS_FINAL_GATE_FAILED"
                return reject_live_execution_block(
                    symbol,
                    side,
                    trade_payload,
                    reason,
                    reason,
                    details=locked_news_gate,
                )
        elif not news_runtime.get("allow_normal_entry", True):
            reason = (
                news_runtime.get("blocking_reason")
                or "AUTHORITATIVE_NEWS_BLOCK"
            )
            return reject_live_execution_block(
                symbol,
                side,
                trade_payload,
                reason,
                reason,
                details=news_runtime,
            )

        if source == "auto" and not is_news_order:
            final_gate = validate_auto_entry_state_locked(
                symbol,
                side,
                trade_payload,
                broker_positions_before_send,
            )
            print("LIVE_AUTO_FINAL_ENTRY_GATE =", {
                **final_gate.get("details", {}),
                "ok": final_gate.get("ok"),
                "reason": final_gate.get("reason"),
            })
            if not final_gate.get("ok"):
                reason = f"LIVE BLOCKED: {final_gate.get('reason')}"
                set_auto_trade_status(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    action=side,
                    status="BLOCKED",
                    reason=reason,
                    details=final_gate.get("details"),
                )
                log_auto_trade_blocked_reason(
                    symbol=symbol,
                    signal=trade_payload.get("signal"),
                    stage="final_entry_state_revalidation",
                    reason=reason,
                    details=final_gate.get("details"),
                )
                return reject_live_execution_block(
                    symbol,
                    side,
                    trade_payload,
                    reason,
                    reason,
                    details=final_gate.get("details"),
                )

        if strategy_generated_order and not is_news_order:
            fresh_ema_gate = validate_fresh_ema_permission_locked(
                symbol,
                side,
                trade_payload.get("setup_identity"),
            )
            print("LIVE_FRESH_EMA_FINAL_GATE =", {
                **fresh_ema_gate.get("details", {}),
                "source": source,
                "strategy_generated_order": True,
                "ok": fresh_ema_gate.get("ok"),
                "reason": fresh_ema_gate.get("reason"),
            })
            if not fresh_ema_gate.get("ok"):
                reason = (
                    fresh_ema_gate.get("reason")
                    or "WAIT_EMA_CHANGED_BEFORE_EXECUTION"
                )
                if source == "auto":
                    set_auto_trade_status(
                        symbol=symbol,
                        signal=trade_payload.get("signal"),
                        action=side,
                        status="BLOCKED",
                        reason=reason,
                        details=fresh_ema_gate.get("details"),
                    )
                    log_auto_trade_blocked_reason(
                        symbol=symbol,
                        signal=trade_payload.get("signal"),
                        stage="fresh_ema_final_gate",
                        reason=reason,
                        details=fresh_ema_gate.get("details"),
                    )
                return reject_live_execution_block(
                    symbol,
                    side,
                    trade_payload,
                    reason,
                    reason,
                    details=fresh_ema_gate.get("details"),
                )

            market_health = check_live_market_data_health(symbol)
            if not market_health.get("ok"):
                return reject_live_execution_block(
                    symbol,
                    side,
                    trade_payload,
                    "WAIT_STALE_MARKET_FEED",
                    "WAIT_STALE_MARKET_FEED",
                    details=market_health,
                )

            locked_risk_size = calculate_live_risk_size(
                symbol,
                trade_payload.get("entry"),
                trade_payload.get("sl"),
            )
            locked_rr = validate_live_trade_risk_reward(
                symbol,
                side,
                trade_payload.get("entry"),
                trade_payload.get("sl"),
                trade_payload.get("tp2"),
            )
            risk_changed = bool(
                not locked_risk_size.get("ok")
                or locked_risk_size.get("lot_size") != risk_size.get("lot_size")
                or locked_risk_size.get("volume_units") != risk_size.get("volume_units")
                or locked_risk_size.get("risk_amount") != risk_size.get("risk_amount")
            )
            if risk_changed or not locked_rr.get("ok"):
                reason = (
                    "WAIT_RISK_CHANGED_BEFORE_EXECUTION"
                    if risk_changed
                    else "WAIT_INVALID_RR"
                )
                return reject_live_execution_block(
                    symbol,
                    side,
                    trade_payload,
                    reason,
                    reason,
                    details={
                        "initial_risk_size": risk_size,
                        "locked_risk_size": locked_risk_size,
                        "locked_risk_reward": locked_rr,
                    },
                )

        LIVE_ORDER_IN_FLIGHT.add(symbol)
        if isinstance(_inflight_guard, dict):
            _inflight_guard["symbol"] = symbol
            _inflight_guard["acquired"] = True

    if any(
        normalize_symbol(position.get("symbol")) == symbol
        for position in broker_positions_before_send
    ):
        duplicate_reason = (
            f"{symbol} blocked — broker already has open position"
        )

        with LIVE_ORDER_LOCK:
            LIVE_ORDER_IN_FLIGHT.discard(symbol)

        if source == "auto":
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="BLOCKED",
                reason=duplicate_reason
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="duplicate_broker_position_final_gate",
                reason=duplicate_reason,
                details={"open_positions_count": len(broker_positions_before_send or [])}
            )

        print("LIVE DUPLICATE BROKER POSITION BLOCKED:", {
            "symbol": symbol,
            "reason": duplicate_reason,
            "open_positions": broker_positions_before_send,
        })

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            risk_size=risk_size,
            stage="duplicate_broker_position_final_gate",
            blocked_by="duplicate_broker_position",
            blocked_reason=duplicate_reason,
            existing_position=[
                position
                for position in broker_positions_before_send
                if normalize_symbol(position.get("symbol")) == symbol
            ],
            payload_valid=True,
            order_sent=False,
            order_accepted=False,
        )
        return reject_live_execution_block(
            symbol,
            side,
            trade_payload,
            duplicate_reason,
            "LIVE EXECUTION BLOCKED: broker position already exists"
        )

    live_risk_debug = build_live_risk_debug(
        symbol,
        side,
        trade_payload,
        risk_size,
        broker_reject_reason=None,
    )
    log_live_xauusd_execution_debug(
        symbol,
        plan=plan,
        trade_payload=trade_payload,
        risk_size=risk_size,
        stage="before_place_market_order",
        blocked_by=None,
        blocked_reason=None,
        existing_position=None,
        payload_valid=True,
        order_sent=True,
        order_accepted=False,
    )
    print("LIVE_ORDER_PROTECTION_AUDIT =", {
        "stage": "before_place_market_order",
        "symbol": symbol,
        "side": side,
        "account_balance": risk_size.get("account_balance"),
        "account_equity": risk_size.get("account_equity"),
        "account_equity_used": risk_size.get("account_equity_used"),
        "configured_risk_percent": risk_size.get("risk_percent"),
        "calculated_lot_size": risk_size.get("lot_size"),
        "volume_units": risk_size.get("volume_units"),
        "entry": trade_payload.get("entry"),
        "SL": trade_payload.get("sl"),
        "saved_sl": trade_payload.get("sl"),
        "broker_sl": None,
        "displayed_sl": None,
        "TP1": trade_payload.get("tp1"),
        "TP2": trade_payload.get("tp2"),
        "tp2": trade_payload.get("tp2"),
        "broker_tp": None,
        "expected_loss_usd": trade_payload.get("expected_loss_usd"),
        "max_risk_usd": trade_payload.get("max_risk_usd"),
        "broker_take_profit_source": "TP2",
        "tp1_managed_by_flowsignal": True,
        "tp2_sent_to_broker": True,
        "expected_loss_usd": (
            round(expected_loss_usd, 2)
            if isinstance(expected_loss_usd, (int, float))
            else expected_loss_usd
        ),
        "max_risk_usd": max_risk_usd,
    })

    ENGINE_RUNTIME_STATE["last_order_attempt"] = {
        "symbol": symbol,
        "side": side,
        "source": source,
        "timestamp": time.time(),
        "entry": trade_payload.get("entry"),
        "sl": trade_payload.get("sl"),
        "tp2": trade_payload.get("tp2"),
        "volume_units": trade_payload.get("volume_units"),
    }
    try:
        pre_submit_tick = (
            ((get_live_prices() or {}).get("live_prices") or {}).get(symbol)
            or {}
        )
    except Exception:
        pre_submit_tick = {}
    pre_submit_risk = validate_executable_risk(
        symbol,
        side,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp2"),
        trade_payload.get("volume_units"),
        pre_submit_tick,
        calculate_live_risk_size,
        validate_live_trade_risk_reward,
    )
    persist_execution_risk_audit_safely(
        symbol=symbol,
        event_type="PRE_SUBMIT_EXECUTABLE_RISK",
        source=source,
        old_entry=trade_payload.get("entry"),
        new_entry=pre_submit_risk.get("executable_entry"),
        old_sl=trade_payload.get("sl"),
        new_sl=trade_payload.get("sl"),
        volume_units=trade_payload.get("volume_units"),
        approved_risk_amount=risk_size.get("risk_amount"),
        approved_risk_percent=risk_size.get("risk_percent"),
        status="PASS" if pre_submit_risk.get("ok") else "RISK_VALIDATION_WARNING",
        details={
            **pre_submit_risk,
            "behavior_changed": False,
            "v1_submission_unchanged": True,
        },
    )
    persist_execution_snapshot_safely(
        symbol=symbol,
        direction=side,
        trade_payload=trade_payload,
        plan=plan,
        quote=pre_submit_tick,
        risk_size=risk_size,
        gate_results={
            "ema_state": locals().get("fresh_ema_gate"),
            "consolidation_result": (
                plan.get("consolidation") if isinstance(plan, dict) else None
            ),
            "news_decision": locals().get("locked_news_gate") or locals().get("news_runtime"),
            "market_data_freshness_result": locals().get("market_health"),
            "cooldown_result": (
                {"active": locals().get("cooldown_active")}
                if "cooldown_active" in locals() else None
            ),
            "active_position_result": {
                "application_active_order": False,
                "broker_position_present": False,
                "final_gate_passed": True,
            },
            "loss_limit_status": loss_limit_status,
            "risk_recalculation_result": pre_submit_risk,
        },
    )
    submission_key = None
    if trade_payload.get("source_indicator_event_id"):
        submission_claim = claim_submission(
            trade_payload.get("source_indicator_event_id"),
            "LIVE",
            LIVE_ACCOUNT_STATE.get("account_id") or LIVE_ACCOUNT_STATE.get("active_account_id"),
            symbol,
            trade_payload.get("signal_setup_id"),
            trade_payload,
        )
        if not submission_claim.get("ok"):
            return reject_live_execution_block(
                symbol, side, trade_payload,
                submission_claim.get("reason") or "durable submission claim failed",
                "LIVE EXECUTION BLOCKED: durable submission claim failed",
                details=submission_claim,
            )
        submission_key = submission_claim["idempotency_key"]
        trade_payload["submission_idempotency_key"] = submission_key
    if submission_key and not mark_request_started(submission_key):
        recover_unsent_claim(submission_key)
        return reject_live_execution_block(
            symbol, side, trade_payload,
            "durable broker request marker failed",
            "LIVE EXECUTION BLOCKED: durable broker request marker failed",
        )
    try:
        result = place_market_order_with_inflight_cleanup(
            symbol,
            action=side,
            entry=trade_payload["entry"],
            sl=trade_payload["sl"],
            tp1=trade_payload["tp1"],
            tp2=trade_payload["tp2"],
            volume=trade_payload["volume"],
            volume_units=trade_payload.get("volume_units"),
            risk=trade_payload.get("risk"),
            mode=trade_payload["mode"],
            client_order_id=submission_key,
        )
    except Exception as exc:
        if submission_key:
            require_reconciliation(submission_key, exc)
        raise
    if submission_key:
        if not complete_submission(submission_key, result):
            require_reconciliation(
                submission_key,
                "broker response received but durable completion failed",
            )
    record_execution_response_safely(symbol, result)
    # Observe the actual fill without changing, retrying, closing, resizing, or
    # widening the V1 order.  Any risk drift is explicit and durable.
    actual_fill = (
        result.get("entry_price")
        or result.get("entry")
        or (result.get("position") or {}).get("entry_price")
    )
    post_fill_status = "FILL_NOT_AVAILABLE"
    post_fill_details = {"broker_ok": bool(result.get("ok"))}
    if result.get("ok") and actual_fill is not None:
        filled_risk = calculate_live_risk_size(
            symbol, actual_fill, trade_payload.get("sl")
        )
        filled_rr = validate_live_trade_risk_reward(
            symbol,
            side,
            actual_fill,
            trade_payload.get("sl"),
            trade_payload.get("tp2"),
        )
        submitted_units = float(trade_payload.get("volume_units") or 0)
        allowed_units = float(filled_risk.get("volume_units") or 0)
        risk_exceeded = bool(
            not filled_risk.get("ok")
            or not filled_rr.get("ok")
            or submitted_units > allowed_units + 1e-9
        )
        post_fill_status = (
            "RISK_EXCEEDED_AFTER_FILL" if risk_exceeded else "PASS"
        )
        post_fill_details = {
            "broker_ok": True,
            "filled_risk": filled_risk,
            "filled_rr": filled_rr,
            "submitted_volume_units": submitted_units,
            "allowed_volume_units": allowed_units,
            "action": "FLAGGED_NO_AUTOMATIC_INTERVENTION" if risk_exceeded else "NONE_REQUIRED",
        }
    persist_execution_risk_audit_safely(
        symbol=symbol,
        event_type="POST_FILL_RISK",
        source=source,
        broker_position_id=result.get("position_id"),
        old_entry=trade_payload.get("entry"),
        new_entry=actual_fill,
        old_sl=trade_payload.get("sl"),
        new_sl=trade_payload.get("sl"),
        volume_units=trade_payload.get("volume_units"),
        approved_risk_amount=risk_size.get("risk_amount"),
        approved_risk_percent=risk_size.get("risk_percent"),
        status=post_fill_status,
        details=post_fill_details,
    )
    # Observability-only association with the independently simulated V2
    # decision. This never changes, retries, or interprets the broker result.
    link_v1_execution_safely(
        symbol,
        trade_payload.get("signal_setup_id"),
        result,
        trade_payload,
    )
    ENGINE_RUNTIME_STATE["last_broker_response"] = {
        "symbol": symbol,
        "side": side,
        "timestamp": time.time(),
        "ok": bool(result.get("ok")),
        "position_id": result.get("position_id"),
        "order_id": result.get("order_id"),
        "reason": result.get("reason") or result.get("message"),
    }

    if trade_payload.get("news_event_id") and isinstance(
        trade_payload.get("news_event"), dict
    ):
        news_trading.record_broker_result(
            trade_payload["news_event"],
            symbol,
            result,
        )

    if not result.get("ok", False):
        reason = result.get("reason") or result.get("message") or "Order rejected"

        if result.get("critical_unprotected_position"):
            emergency_close_result = None
            if result.get("position_id"):
                emergency_close_result = close_position(result.get("position_id"))
            print("LIVE RISK ERROR: trade has no broker SL/TP", {
                "symbol": symbol,
                "position_id": result.get("position_id"),
                "broker_sl_confirmed": result.get("broker_sl_confirmed"),
                "broker_tp_confirmed": result.get("broker_tp_confirmed"),
                "emergency_close_result": emergency_close_result,
            })
            sync_live_positions()

        if is_not_enough_money_result(result):
            reason = (
                f"cTrader says not enough funds for calculated {trade_payload.get('risk_percent', get_configured_live_risk_percent())}% risk size "
                f"({trade_payload.get('lot_size')} lot)"
            )
            result = {
                **result,
                "reason": reason,
                "final_lot_size": trade_payload.get("lot_size"),
            }

        live_risk_debug = build_live_risk_debug(
            symbol,
            side,
            trade_payload,
            risk_size,
            broker_reject_reason=reason,
        )

        if source == "auto":
            rejection_volume_safety = result.get("volume_safety") if isinstance(result, dict) else None
            rejection_details = build_live_block_details(
                trade_payload.get("distance_details"),
                trade_payload.get("risk"),
                rejection_volume_safety,
                {
                    "broker_rejection_reason": reason,
                    "blocked_reason": reason,
                },
                live_risk_debug,
            )
            log_broker_min_distance_decision(
                symbol,
                trade_payload.get("distance_details"),
                actual_blocked=True,
                final_block_reason=reason
            )
            set_auto_trade_status(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                action=side,
                status="ORDER_REJECTED",
                reason=reason,
                details=rejection_details
            )
            log_auto_trade_blocked_reason(
                symbol=symbol,
                signal=trade_payload.get("signal"),
                stage="broker_order_rejected",
                reason=reason,
                details=result
            )

        log_live_xauusd_execution_debug(
            symbol,
            plan=plan,
            trade_payload=trade_payload,
            risk_size=risk_size,
            stage="broker_order_rejected",
            blocked_by="broker_order_rejected",
            blocked_reason=reason,
            existing_position=None,
            payload_valid=True,
            order_sent=True,
            order_accepted=False,
            result=result,
        )
        try:
            return {
                "ok": False,
                "message": reason,
                "reason": reason,
                "broker_rejection_reason": reason,
                "live_risk_debug": live_risk_debug,
                "result": result,
            }
        finally:
            with LIVE_ORDER_LOCK:
                LIVE_ORDER_IN_FLIGHT.discard(symbol)

    LIVE_LAST_EXECUTION_TIME[symbol] = time.time()
    LAST_EXECUTION_TIME = LIVE_LAST_EXECUTION_TIME[symbol]
    order_id = str(uuid.uuid4())
    broker_position_id = result.get("position_id")
    trade_id = (
        f"ctrader-pos-{broker_position_id}"
        if broker_position_id
        else f"flowsignal-{symbol}-{order_id}"
    )

    LIVE_ACTIVE_ORDERS[symbol] = {
        "order_id": order_id,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "action": side,
        "status": "OPEN",
        "mode": LIVE_ACCOUNT_STATE["mode"],
        "broker": LIVE_ACCOUNT_STATE["broker"],
        "source": "broker",
        "volume": trade_payload["volume"],
        "lot_size": trade_payload.get("lot_size", trade_payload["volume"]),
        "volume_units": trade_payload.get("volume_units"),
        "risk_percent": trade_payload.get("risk_percent"),
        "risk_amount": trade_payload.get("risk_amount"),
        "sl_pips": trade_payload.get("sl_pips"),
        "distance_details": trade_payload.get("distance_details"),
        "adjusted_for_broker_distance": trade_payload.get("adjusted_for_broker_distance"),
        "account_balance_used": trade_payload.get("account_balance_used"),
        "account_equity": trade_payload.get("account_equity"),
        "account_equity_used": trade_payload.get("account_equity_used"),
        "entry": trade_payload["entry"],
        "current_price": trade_payload["entry"],
        "current_high": trade_payload["entry"],
        "current_low": trade_payload["entry"],
        "sl": trade_payload["sl"],
        "original_sl": trade_payload["sl"],
        "planned_sl": trade_payload["sl"],
        "broker_stop_loss_confirmed": result.get("broker_sl_confirmed") is True,
        "broker_stop_loss_missing": result.get("broker_sl_confirmed") is not True,
        "tp1": trade_payload["tp1"],
        "tp2": trade_payload["tp2"],
        "planned_tp1": trade_payload["tp1"],
        "planned_tp2": trade_payload["tp2"],
        "broker_take_profit_confirmed": result.get("broker_tp_confirmed") is True,
        "broker_take_profit_missing": result.get("broker_tp_confirmed") is not True,
        "position_id": broker_position_id,
        "broker_order_id": result.get("order_id"),
        "broker_position_id": broker_position_id,
        "exit_status": "HOLD",
        "exit_reason": "Trade still aligned",
        "opened_at": time.time(),
        "result": "RUNNING",
        "signal_setup_id": get_signal_setup_id(plan, side),
        "source_indicator_event_id": trade_payload.get("source_indicator_event_id"),
        "indicator_event_identity": copy.deepcopy(
            trade_payload.get("indicator_event_identity") or {}
        ),
        "m5_confirmation_id": trade_payload.get("m5_confirmation_id"),
        "m5_confirmation_identity": copy.deepcopy(
            trade_payload.get("m5_confirmation_identity") or {}
        ),
        "symbol_metadata": copy.deepcopy(risk_size.get("symbol_metadata") or {}),
        "broker_result": result,
        "hit_tp1": False,
        "tp1_hit": False,
        "protection_requested": False,
        "protection_confirmed": False,
        "profit_protected": False,
        "protected_sl_price": None,
        "sl_protection_failed": False,
        "sl_protection_warning": None,
        "sl_protection_error": None,
        "sl_protection_broker_result": None,
    }
    LIVE_ACTIVE_ORDERS[symbol]["executed_trade_setup_snapshot"] = (
        build_executed_trade_setup_snapshot(
            symbol,
            side,
            plan,
            trade_payload,
            broker_position_id,
            timestamp=LIVE_LAST_EXECUTION_TIME[symbol],
        )
    )
    print("CTRADER_SLTP_CONFIRMATION =", build_live_protection_audit(
        symbol,
        side,
        entry=trade_payload.get("entry"),
        saved_sl=trade_payload.get("sl"),
        broker_sl=result.get("broker_stop_loss_attached"),
        displayed_sl=(
            result.get("broker_stop_loss_attached")
            if result.get("broker_sl_confirmed") is True
            else None
        ),
        tp2=trade_payload.get("tp2"),
        broker_tp=result.get("broker_take_profit_attached"),
        lot_size=trade_payload.get("lot_size"),
        expected_loss_usd=trade_payload.get("expected_loss_usd"),
        max_risk_usd=trade_payload.get("max_risk_usd"),
        repair_result=result.get("tp_amend_result"),
        stage="after_order_success",
    ))
    ensure_live_trade_identity(LIVE_ACTIVE_ORDERS[symbol], symbol)
    update_event_lifecycle(
        trade_payload.get("source_indicator_event_id"),
        "LIVE",
        "CONSUMED",
        m5_confirmation_id=trade_payload.get("m5_confirmation_id"),
        m5_confirmation_identity=trade_payload.get("m5_confirmation_identity"),
        signal_setup_id=trade_payload.get("signal_setup_id"),
    )
    ui_signal_state = f"{side} RUNNING" if side in ["BUY", "SELL"] else "TRADE RUNNING"
    LIVE_ACTIVE_ORDERS[symbol]["ui_signal_state"] = ui_signal_state
    log_live_trade_audit("order_opened", LIVE_ACTIVE_ORDERS[symbol], reason="broker order accepted")
    clear_persisted_final_signal_hold(symbol, reason="broker order accepted")
    log_live_xauusd_execution_debug(
        symbol,
        plan=plan,
        trade_payload=trade_payload,
        risk_size=risk_size,
        stage="broker_order_accepted",
        blocked_by=None,
        blocked_reason=None,
        existing_position=LIVE_ACTIVE_ORDERS[symbol],
        payload_valid=True,
        order_sent=True,
        order_accepted=True,
        result=result,
    )

    log_trade_visual_levels(LIVE_ACTIVE_ORDERS[symbol])
    email_plan = {
        **(plan if isinstance(plan, dict) else {}),
        "signal": side,
        "entry_price": trade_payload["entry"],
        "stop_loss": trade_payload["sl"],
        "tp1": trade_payload["tp1"],
        "tp2": trade_payload["tp2"],
        "risk_percent": trade_payload.get("risk_percent"),
        "risk_dollars": trade_payload.get("risk_amount"),
        "plan_reason": "Live order placed successfully",
    }
    email_sent = send_order_confirmation_email(symbol, side, email_plan)
    active_trade_state = {
        "status": get_live_trade_status(LIVE_ACTIVE_ORDERS[symbol]),
        "result": LIVE_ACTIVE_ORDERS[symbol].get("result"),
        "trade_id": get_live_trade_identity(LIVE_ACTIVE_ORDERS[symbol]),
        "position_id": (
            LIVE_ACTIVE_ORDERS[symbol].get("position_id")
            or LIVE_ACTIVE_ORDERS[symbol].get("broker_position_id")
        ),
    }
    print("ORDER_PLACED_SUCCESS =", {
        "symbol": symbol,
        "side": side,
        "entry": trade_payload["entry"],
        "SL": trade_payload["sl"],
        "TP1": trade_payload["tp1"],
        "TP2": trade_payload["tp2"],
        "account_balance": risk_size.get("account_balance"),
        "account_equity": risk_size.get("account_equity"),
        "configured_risk_percent": risk_size.get("risk_percent"),
        "calculated_lot_size": risk_size.get("lot_size"),
        "volume_units": risk_size.get("volume_units"),
        "payload_tp_field": "relativeTakeProfit",
        "payload_tp_source": "TP2",
        "broker_response_tp_field": result.get("broker_take_profit_attached"),
        "broker_tp_confirmed": result.get("broker_tp_confirmed"),
        "broker_tp_exists": result.get("broker_tp_confirmed") is True,
        "tp1_managed_by_flowsignal": True,
        "tp2_sent_to_broker": True,
        "tp_missing_reason": (
            None
            if result.get("broker_tp_confirmed") is True
            else "Broker TP2 was not confirmed after order placement"
        ),
        "email_sent": bool(email_sent),
        "ui_signal_state": ui_signal_state,
        "active_trade_state": active_trade_state,
    })

    save_live_backup()

    if source == "auto":
        order_sent_reason = "Order sent"

        if trade_payload.get("adjusted_for_broker_distance"):
            order_sent_reason = (
                "Order sent after broker distance adjustment: "
                f"{trade_payload.get('adjustment_reason')}"
            )

        set_auto_trade_status(
            symbol=symbol,
            signal=trade_payload.get("signal"),
            action=side,
            status="ORDER_SENT",
            reason=order_sent_reason,
            details=trade_payload.get("distance_details")
        )

    try:
        return {
            "ok": True,
            "result": result,
            "active_order": LIVE_ACTIVE_ORDERS[symbol],
            "execution_ready": LIVE_ACCOUNT_STATE.get("execution_ready", False),
            "broker_order_sent": result.get("broker_order_sent", True)
        }
    finally:
        with LIVE_ORDER_LOCK:
            LIVE_ORDER_IN_FLIGHT.discard(symbol)

def execute_live_order_core(payload: dict, source="manual"):
    """Execute with guaranteed release of any guard acquired by this call."""
    inflight_guard = {"symbol": None, "acquired": False}
    try:
        return _execute_live_order_core_impl(
            payload,
            source=source,
            _inflight_guard=inflight_guard,
        )
    finally:
        if inflight_guard["acquired"] and inflight_guard["symbol"]:
            with LIVE_ORDER_LOCK:
                LIVE_ORDER_IN_FLIGHT.discard(inflight_guard["symbol"])


@app.post("/execute-live-order")
def execute_live_order(payload: dict):
    return execute_live_order_core(payload, source="manual")
