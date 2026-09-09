# =========================
# 📦 IMPORTS
# =========================
import json
import math
import os
import time
import copy
import uuid
import pandas as pd
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from ctrader_connector import (
    append_current_forming_candle,
    get_ctrader_candle_cache_status,
    get_ctrader_candle_health,
    get_ctrader_live_price_status,
    get_ctrader_market_data,
    hydrate_persisted_ctrader_candle_cache,
    normalize_symbol,
    normalize_trade_levels
)
from paths import DATA_DIR
from services.settings_service import get_tp1_ratio_of_tp2
from services.strategy_settings_service import get_configured_cooldown_seconds
from services.strategy_diagnostics_service import persist_cycle_safely
from services.v2_shadow_service import evaluate_cycle_safely as evaluate_v2_shadow_cycle_safely

MARKET_DATA_SOURCES = ["ctrader"]
MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_DATA_SOURCE_FILE = os.path.join(
    DATA_DIR,
    "market_data_source.json"
)
FINAL_SIGNAL_HOLD_FILE = os.path.join(
    DATA_DIR,
    "final_signal_hold.json"
)
FIFTEEN_M_SWING_WATCH_FILE = os.path.join(
    DATA_DIR,
    "fifteen_m_swing_watch.json"
)
FINAL_SIGNAL_HOLD_EXPIRATION_SECONDS = 4 * 60 * 60
FINAL_SIGNAL_HOLD_MAX_SETUP_CANDLES = 3
FINAL_SIGNAL_HOLD_STALE_5M_CANDLES = 2
FINAL_SIGNAL_HOLD_MAX_TP1_PROGRESS = 0.60
FIFTEEN_M_PENDING_MAX_5M_CANDLES = 3
XAUUSD_FIFTEEN_M_PENDING_MAX_5M_CANDLES = 5
MIN_LIVE_FRESHNESS_SCORE = 70

PANEL_SWING_REFERENCE_FIELDS = (
    "type",
    "time",
    "price",
    "index",
    "swing_size",
    "valid",
    "valid_reason",
)
PANEL_CACHE_CYCLE_MARKER = "CYCLE_DETECTED"


def _panel_reference_identity(reference):
    """Keep one swing anchor without following its historical linked list."""
    if not isinstance(reference, dict):
        return None
    return {
        field: bounded_panel_snapshot(reference.get(field))
        for field in PANEL_SWING_REFERENCE_FIELDS
    }


def bounded_panel_snapshot(value, _active_ids=None):
    """Create a finite JSON-safe copy for UI/cache use only.

    Strict strategy results may contain a linked ``reference_swing`` chain.
    That graph is useful while calculating structure, but the last-open panel
    cache only needs the immediate anchor identity.  This function never
    mutates the strategy result used by execution.
    """
    if _active_ids is None:
        _active_ids = set()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, pd.Timestamp)):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.isoformat()
    if isinstance(value, dict):
        identity = id(value)
        if identity in _active_ids:
            return PANEL_CACHE_CYCLE_MARKER
        _active_ids.add(identity)
        try:
            snapshot = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text == "reference_swing":
                    snapshot[key_text] = _panel_reference_identity(item)
                else:
                    snapshot[key_text] = bounded_panel_snapshot(
                        item,
                        _active_ids,
                    )
            return snapshot
        finally:
            _active_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in _active_ids:
            return PANEL_CACHE_CYCLE_MARKER
        _active_ids.add(identity)
        try:
            return [bounded_panel_snapshot(item, _active_ids) for item in value]
        finally:
            _active_ids.remove(identity)
    if hasattr(value, "item"):
        try:
            converted = value.item()
            if converted is value:
                return str(value)
            return bounded_panel_snapshot(converted, _active_ids)
        except Exception:
            pass
    return str(value)


def remember_last_open_payload(payload):
    """Best-effort optional cache update; never suppress strategy execution."""
    try:
        get_panel_data._last_open_payload = bounded_panel_snapshot(payload)
        return True
    except Exception as exc:
        print("PANEL_CACHE_SNAPSHOT_WARNING =", {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "execution_blocked": False,
        })
        return False

# =========================
# SIMPLE MOMENTUM MODE
# =========================
# Goal: FlowSignal should take clean 15m momentum trades without waiting
# for a perfect SMC checklist. 1H/HTF becomes a confidence warning, not
# a hard blocker. This keeps protection against obvious fake breakouts
# while avoiding the constant WAIT problem.
SIMPLE_MOMENTUM_MODE = True
SIMPLE_MOMENTUM_MIN_SCORE = 68
SIMPLE_MOMENTUM_MIN_CONFIDENCE = 52
SIMPLE_MOMENTUM_MIN_GAP = 10
# When a clean 15m momentum breakout appears, do not let 5m timing
# erase the trade. The 5m confirmation can still improve the entry, but
# it is not allowed to turn a completed 15m BUY/SELL back into WAIT.
SIMPLE_MOMENTUM_DIRECT_15M_ENTRY = False
SCORE_PERSISTENCE_MEMORY = {}
PAIR_STRATEGY_RULES = {}


def register_pair_strategy_rules(symbol, rules):
    PAIR_STRATEGY_RULES[normalize_symbol(symbol)] = dict(rules or {})


def get_pair_strategy_rule(symbol, name, default=None):
    return PAIR_STRATEGY_RULES.get(normalize_symbol(symbol), {}).get(
        name,
        default,
    )


def get_15m_actionable_swing_atr_limit(symbol):
    normalized_symbol = normalize_symbol(symbol)
    if normalized_symbol == "XAUUSD":
        return 3.0
    return 2.5


def get_15m_pending_max_5m_candles(symbol):
    normalized_symbol = normalize_symbol(symbol)
    if normalized_symbol == "XAUUSD":
        return XAUUSD_FIFTEEN_M_PENDING_MAX_5M_CANDLES
    return FIFTEEN_M_PENDING_MAX_5M_CANDLES


def call_pair_strategy_hook(symbol, name, *args, **kwargs):
    hook = get_pair_strategy_rule(symbol, name)

    if callable(hook):
        return hook(*args, **kwargs)

    return None

def load_saved_market_data_source():
    if not os.path.exists(MARKET_DATA_SOURCE_FILE):
        return {
            "source": "ctrader",
            "saved_source": None,
            "loaded_from_file": False,
        }

    try:
        with open(MARKET_DATA_SOURCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        saved_source = str(data.get("source", "")).lower()

        if saved_source in MARKET_DATA_SOURCES:
            return {
                "source": saved_source,
                "saved_source": saved_source,
                "loaded_from_file": True,
            }

    except Exception as e:
        print("MARKET DATA SOURCE LOAD ERROR:", e)

    return {
        "source": "ctrader",
        "saved_source": None,
        "loaded_from_file": False,
    }

def save_market_data_source(source):
    with open(MARKET_DATA_SOURCE_FILE, "w", encoding="utf-8") as f:
        json.dump({"source": source}, f, indent=2)

SOURCE_BOOT_STATE = load_saved_market_data_source()
MARKET_DATA_SOURCE = os.getenv(
    "MARKET_DATA_SOURCE",
    SOURCE_BOOT_STATE["source"]
).lower()

if MARKET_DATA_SOURCE not in MARKET_DATA_SOURCES:
    print("MARKET DATA SOURCE INVALID, using ctrader:", MARKET_DATA_SOURCE)
    MARKET_DATA_SOURCE = "ctrader"

MARKET_DATA_RUNTIME = {
    "source": MARKET_DATA_SOURCE,
    "saved_source": SOURCE_BOOT_STATE["saved_source"],
    "loaded_from_file": SOURCE_BOOT_STATE["loaded_from_file"],
}

MARKET_DATA_STATUS = {
    "last_fetch_source": None,
    "fallback_used": False,
    "error": None,
}

CTRADER_HEALTH_TIMEFRAMES = {
    "5m": {
        "label": "5min",
        # cTrader returns only completed trendbars and can publish the newest
        # closed bar one refresh late. Keep the frame usable for three 5m
        # slots while the provider catches up.
        "stale_after": 15 * 60,
    },
    "15m": {
        "label": "15min",
        "stale_after": 45 * 60,
    },
    "1h": {
        "label": "1h",
        "stale_after": 150 * 60,
    },
}

CTRADER_HEALTH_SYMBOLS = ["EURUSD", "XAUUSD"]
CTRADER_LAST_SUCCESS_TIMES = {
    f"{symbol}:{settings['label']}": None
    for symbol in CTRADER_HEALTH_SYMBOLS
    for settings in CTRADER_HEALTH_TIMEFRAMES.values()
}

def set_market_data_source(source, persist=True):
    global MARKET_DATA_SOURCE

    normalized = "ctrader"

    if normalized not in MARKET_DATA_SOURCES:
        return {
            "ok": False,
            "source": MARKET_DATA_RUNTIME["source"],
            "available_sources": MARKET_DATA_SOURCES,
            "error": "Invalid market data source",
        }

    MARKET_DATA_SOURCE = normalized
    MARKET_DATA_RUNTIME["source"] = normalized
    MARKET_DATA_RUNTIME["saved_source"] = normalized
    MARKET_DATA_RUNTIME["loaded_from_file"] = True
    MARKET_DATA_STATUS["fallback_used"] = False
    MARKET_DATA_STATUS["error"] = None

    if persist:
        save_market_data_source(normalized)

    print("MARKET DATA SOURCE:", MARKET_DATA_RUNTIME["source"])

    return {
        "ok": True,
        **get_market_data_source_status()
    }

def get_market_data_source():
    return "ctrader"

def get_market_data_source_status():
    ctrader_status = get_ctrader_candle_cache_status()
    ctrader_health = get_ctrader_data_health()
    live_price_status = get_ctrader_live_price_status()

    return {
        "source": get_market_data_source(),
        "saved_source": MARKET_DATA_RUNTIME.get("saved_source"),
        "loaded_from_file": MARKET_DATA_RUNTIME.get("loaded_from_file", False),
        "available_sources": MARKET_DATA_SOURCES,
        "last_fetch_source": MARKET_DATA_STATUS.get("last_fetch_source"),
        "fallback_used": MARKET_DATA_STATUS.get("fallback_used", False),
        "error": MARKET_DATA_STATUS.get("error"),
        "live_price_health": live_price_status.get("live_price_health"),
        "live_price_last_update": live_price_status.get("live_price_last_update"),
        **ctrader_health,
        **ctrader_status,
    }

def _ctrader_health_key(symbol, timeframe):
    normalized_symbol = normalize_symbol(symbol)
    settings = CTRADER_HEALTH_TIMEFRAMES.get(str(timeframe or "").lower())
    label = settings["label"] if settings else str(timeframe or "").lower()

    return f"{normalized_symbol}:{label}"

def _mark_ctrader_success(symbol, timeframe, timestamp=None):
    key = _ctrader_health_key(symbol, timeframe)
    if timestamp is not None:
        timestamp = pd.Timestamp(timestamp)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        timestamp = timestamp.to_pydatetime()

    CTRADER_LAST_SUCCESS_TIMES[key] = timestamp or datetime.now(timezone.utc)

def _is_ctrader_data_fresh(symbol, timeframe, now=None):
    settings = CTRADER_HEALTH_TIMEFRAMES.get(str(timeframe or "").lower())

    if not settings:
        return False

    timestamp = CTRADER_LAST_SUCCESS_TIMES.get(_ctrader_health_key(symbol, timeframe))

    if not timestamp:
        return False

    current_time = now or datetime.now(timezone.utc)

    return (current_time - timestamp).total_seconds() <= settings["stale_after"]

def get_ctrader_data_health(now=None):
    current_time = now or datetime.now(timezone.utc)
    active_source = get_market_data_source()
    stale_keys = []
    last_success_times = {}

    for key, timestamp in CTRADER_LAST_SUCCESS_TIMES.items():
        last_success_times[key] = timestamp.isoformat() if timestamp else None

        if active_source != "ctrader":
            continue

        _, timeframe_label = key.split(":", 1)
        timeframe = "5m" if timeframe_label == "5min" else "15m" if timeframe_label == "15min" else "1h"
        settings = CTRADER_HEALTH_TIMEFRAMES[timeframe]
        symbol = key.split(":", 1)[0]
        candle_health = get_ctrader_candle_health(symbol, timeframe)

        if (
            not candle_health.get("usable")
            and (
                not timestamp
                or (current_time - timestamp).total_seconds() > settings["stale_after"]
            )
        ):
            stale_keys.append(key)

    return {
        "data_health": "STALE" if stale_keys else "OK",
        "stale_keys": stale_keys,
        "last_success_times": last_success_times,
    }

def is_market_calendar_closed(now=None):
    current_time = now or datetime.now(MARKET_TIMEZONE)

    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current_time = current_time.astimezone(MARKET_TIMEZONE)

    day = current_time.weekday()  # Monday=0, Sunday=6
    hour = current_time.hour

    # Forex week: Sunday 5 PM New York through Friday 5 PM New York.
    if day == 4 and hour >= 17:
        return True
    if day == 5:
        return True
    if day == 6 and hour < 17:
        return True

    return False

# =========================
# 🧠 V5 HELPERS
# =========================
def detect_recent_pressure(data, symbol):
    close = data["Close"]
    open_ = data["Open"]
    high = data["High"]
    low = data["Low"]

    if len(data) < 8:
        return "NEUTRAL", 0

    recent = data.tail(8)

    bull_count = 0
    bear_count = 0

    for _, candle in recent.iterrows():
        if candle["Close"] > candle["Open"]:
            bull_count += 1
        elif candle["Close"] < candle["Open"]:
            bear_count += 1

    c1 = close.iloc[-1].item()
    o1 = open_.iloc[-1].item()
    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    body1 = abs(c1 - o1)
    candle_range = h1 - l1

    strong_body = get_pair_strategy_rule(symbol, "recent_pressure_strong_body")

    pressure_score = 0

    if bull_count >= 5:
        pressure_score += 3
    elif bull_count >= 4:
        pressure_score += 2

    if bear_count >= 5:
        pressure_score -= 3
    elif bear_count >= 4:
        pressure_score -= 2

    if c1 > o1:
        pressure_score += 1
    elif c1 < o1:
        pressure_score -= 1

    if body1 > strong_body:
        if c1 > o1:
            pressure_score += 2
        elif c1 < o1:
            pressure_score -= 2

    if candle_range > 0:
        close_position = (c1 - l1) / candle_range

        if close_position >= 0.7:
            pressure_score += 1
        elif close_position <= 0.3:
            pressure_score -= 1

    if pressure_score >= 3:
        return "BULLISH", pressure_score
    elif pressure_score <= -3:
        return "BEARISH", pressure_score
    else:
        return "NEUTRAL", pressure_score
    
def detect_momentum_strength(data, symbol):
    close = data["Close"]
    open_ = data["Open"]
    high = data["High"]
    low = data["Low"]

    if len(data) < 6:
        return "WEAK", 0

    c1 = close.iloc[-1].item()
    c2 = close.iloc[-2].item()
    c3 = close.iloc[-3].item()

    o1 = open_.iloc[-1].item()
    o2 = open_.iloc[-2].item()
    o3 = open_.iloc[-3].item()

    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)
    body3 = abs(c3 - o3)

    avg_range = (high.iloc[-6:] - low.iloc[-6:]).mean().item()
    candle_range = h1 - l1
    body_ratio = body1 / candle_range if candle_range > 0 else 0

    strong_body = get_pair_strategy_rule(symbol, "momentum_strong_body")
    min_body = get_pair_strategy_rule(symbol, "momentum_min_body")

    bull_1 = c1 > o1
    bear_1 = c1 < o1
    bull_2 = c2 > o2
    bear_2 = c2 < o2
    bull_3 = c3 > o3
    bear_3 = c3 < o3

    bull_count = sum([bull_1, bull_2, bull_3])
    bear_count = sum([bear_1, bear_2, bear_3])

    momentum_score = 0

    if bull_1 or bear_1:
        momentum_score += 2

    if body1 > min_body:
        momentum_score += 1

    if body1 > strong_body:
        momentum_score += 2

    if body_ratio >= 0.65:
        momentum_score += 2
    elif body_ratio >= 0.5:
        momentum_score += 1

    if bull_count >= 2 or bear_count >= 2:
        momentum_score += 1

    if bull_count == 3 or bear_count == 3:
        momentum_score += 1

    if avg_range > 0 and candle_range > avg_range * 1.2:
        momentum_score += 1

    if momentum_score >= 7:
        return "STRONG", momentum_score
    elif momentum_score >= 4:
        return "MEDIUM", momentum_score
    else:
        return "WEAK", momentum_score   

def detect_market_condition(data, ema9, ema21, ema50):
    close = data["Close"]
    high = data["High"]
    low = data["Low"]

    if len(close) < 10:
        return "UNKNOWN"

    c1 = close.iloc[-1].item()
    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    candle_range = h1 - l1
    avg_range = (high.iloc[-8:] - low.iloc[-8:]).mean().item()

    ema_bull_stack = ema9 > ema21 > ema50
    ema_bear_stack = ema9 < ema21 < ema50

    close_above_ema9 = c1 > ema9
    close_below_ema9 = c1 < ema9

    small_candle = candle_range < avg_range * 0.65
    big_candle = candle_range > avg_range * 1.15

    if ema_bull_stack and close_above_ema9 and big_candle:
        return "TRENDING_BULL"
    if ema_bear_stack and close_below_ema9 and big_candle:
        return "TRENDING_BEAR"

    if ema_bull_stack and c1 < ema9:
        return "PULLBACK_BULL"
    if ema_bear_stack and c1 > ema9:
        return "PULLBACK_BEAR"

    if small_candle:
        return "CHOPPY"

    return "MIXED"

def detect_liquidity_grab(data):
    if len(data) < 2:
        return None

    last = data.iloc[-1]

    body = abs(last["Close"] - last["Open"])
    lower_wick = min(last["Open"], last["Close"]) - last["Low"]
    upper_wick = last["High"] - max(last["Open"], last["Close"])

        # stronger trap conditions
    bullish_trap = (
        lower_wick > body * 2.5
        and last["Close"] > last["Open"]  # must close bullish
    )

    bearish_trap = (
        upper_wick > body * 2.5
       and last["Close"] < last["Open"]  # must close bearish
    )

    if bullish_trap:
        return "BULLISH"

    if bearish_trap:
        return "BEARISH"

    return None

def get_entry_quality(
    buy_score,
    sell_score,
    score_gap,
    market_condition,
    zone_position,
    htf_structure,
    choch_signal
):
    top_score = max(buy_score, sell_score)

    confluence_score = 0

    if htf_structure == "BULLISH" and choch_signal == "BULLISH":
        confluence_score += 1

    if htf_structure == "BEARISH" and choch_signal == "BEARISH":
        confluence_score += 1

    if zone_position == "DISCOUNT" and htf_structure == "BULLISH":
        confluence_score += 1

    if zone_position == "PREMIUM" and htf_structure == "BEARISH":
        confluence_score += 1

    if market_condition in ["CHOPPY", "MIXED", "UNKNOWN"]:
        if top_score >= 85 and score_gap >= 25 and confluence_score >= 2:
            return "MEDIUM"
        elif top_score >= 80 and score_gap >= 40:
            return "MEDIUM"
        elif top_score >= 70 and score_gap >= 50:
            return "MEDIUM"
        elif top_score >= 75 and confluence_score >= 1:
            return "MEDIUM"
        return "WEAK"

    if top_score >= 95 and score_gap >= 25 and confluence_score >= 2:
        return "STRONG"
    elif top_score >= 85 and score_gap >= 20 and confluence_score >= 1:
        return "MEDIUM"
    else:
        return "WEAK"


def apply_no_trade_logic(
    buy_score,
    sell_score,
    market_condition,
    low_volatility,
    zone_position,
    htf_structure,
    choch_signal,
    momentum_strength,
    recent_pressure
):
    reasons_nt = []

    score_gap = abs(buy_score - sell_score)
    top_score = max(buy_score, sell_score)
    no_trade = False

    bullish_override = (
        htf_structure == "BULLISH"
        and choch_signal == "BULLISH"
        and zone_position == "DISCOUNT"
        and top_score >= 85
        and score_gap >= 20
    )

    bearish_override = (
        htf_structure == "BEARISH"
        and choch_signal == "BEARISH"
        and zone_position == "PREMIUM"
        and top_score >= 85
        and score_gap >= 20
    )

    momentum_override = (
        momentum_strength == "STRONG"
        and (
            (recent_pressure == "BULLISH" and buy_score >= 75 and score_gap >= 15)
            or
            (recent_pressure == "BEARISH" and sell_score >= 75 and score_gap >= 15)
        )
    )

    strong_override = bullish_override or bearish_override or momentum_override

    if market_condition in ["CHOPPY", "MIXED", "UNKNOWN"] and not strong_override:
        if top_score < 75 or score_gap < 15:
            no_trade = True
            reasons_nt.append("Messy market -> WAIT")

    if low_volatility and score_gap < 25 and momentum_strength != "STRONG":
        no_trade = True
        reasons_nt.append("Low volatility + weak edge")

    if buy_score >= 50 and sell_score >= 50 and score_gap < 20:
        no_trade = True
        reasons_nt.append("Mixed direction")

    return no_trade, reasons_nt

def apply_fake_signal_killer(
    symbol,
    buy_score,
    sell_score,
    score_gap,
    market_condition,
    market_state,
    entry_timing,
    htf_structure,
    choch_signal,
    recent_pressure,
    momentum_strength,
    zone_position,
    c1,
    ema9,
    ema21,
    recent_high,
    recent_low
):
    reasons_fk = []
    kill_trade = False

    dist_ema9 = abs(c1 - ema9)
    dist_ema21 = abs(c1 - ema21)

    stretch_ema9 = get_pair_strategy_rule(symbol, "fake_killer_stretch_ema9")
    stretch_ema21 = get_pair_strategy_rule(symbol, "fake_killer_stretch_ema21")
    breakout_buffer = get_pair_strategy_rule(symbol, "fake_killer_breakout_buffer")

    top_score = max(buy_score, sell_score)
    direction = "BUY" if buy_score > sell_score else "SELL"

    if top_score < 72 or score_gap < 10:
        kill_trade = True
        reasons_fk.append("Fake killer: weak edge")

    if market_condition in ["CHOPPY", "MIXED", "UNKNOWN"] and momentum_strength == "WEAK":
        kill_trade = True
        reasons_fk.append("Fake killer: messy market")

    if market_state == "RANGE" and score_gap < 26:
        kill_trade = True
        reasons_fk.append("Fake killer: range trap")

    if dist_ema9 > stretch_ema9 or dist_ema21 > stretch_ema21:
        if (
            SIMPLE_MOMENTUM_MODE
            and momentum_strength in ["MEDIUM", "STRONG"]
            and top_score >= SIMPLE_MOMENTUM_MIN_SCORE
            and score_gap >= SIMPLE_MOMENTUM_MIN_GAP
        ):
            reasons_fk.append("Soft warning: momentum is stretched, not blocked")
        else:
            kill_trade = True
            reasons_fk.append("Fake killer: too stretched")

    if direction == "BUY":
        if htf_structure == "BEARISH" and not (
            choch_signal == "BULLISH"
            and momentum_strength in ["MEDIUM", "STRONG"]
            and recent_pressure == "BULLISH"
            and zone_position == "DISCOUNT"
            and buy_score >= 85
            and score_gap >= 20
        ):
            if (
                SIMPLE_MOMENTUM_MODE
                and momentum_strength in ["MEDIUM", "STRONG"]
                and recent_pressure == "BULLISH"
                and buy_score >= SIMPLE_MOMENTUM_MIN_SCORE
                and score_gap >= SIMPLE_MOMENTUM_MIN_GAP
            ):
                reasons_fk.append("Soft HTF warning: buy against 1H/HTF, not blocked")
            else:
                kill_trade = True
                reasons_fk.append("Fake killer: buy against HTF")

        if c1 > recent_high + breakout_buffer and entry_timing in ["LATE_BUY", "WAIT_PULLBACK_BUY"]:
            kill_trade = True
            reasons_fk.append("Fake killer: breakout buy too late")

    elif direction == "SELL":
        if htf_structure == "BULLISH" and not (
            choch_signal == "BEARISH"
            and momentum_strength in ["MEDIUM", "STRONG"]
            and recent_pressure == "BEARISH"
            and zone_position == "PREMIUM"
            and sell_score >= 85
            and score_gap >= 20
        ):
            if (
                SIMPLE_MOMENTUM_MODE
                and momentum_strength in ["MEDIUM", "STRONG"]
                and recent_pressure == "BEARISH"
                and sell_score >= SIMPLE_MOMENTUM_MIN_SCORE
                and score_gap >= SIMPLE_MOMENTUM_MIN_GAP
            ):
                reasons_fk.append("Soft HTF warning: sell against 1H/HTF, not blocked")
            else:
                kill_trade = True
                reasons_fk.append("Fake killer: sell against HTF")

        if c1 < recent_low - breakout_buffer and entry_timing in ["LATE_SELL", "WAIT_PULLBACK_SELL"]:
            kill_trade = True
            reasons_fk.append("Fake killer: breakout sell too late")

    return kill_trade, reasons_fk

def detect_htf_structure(htf_data):
    high = htf_data["High"]
    low = htf_data["Low"]
    close = htf_data["Close"]

    if len(htf_data) < 6:
        return "NEUTRAL"

    h1 = high.iloc[-1].item()
    h2 = high.iloc[-2].item()
    h3 = high.iloc[-3].item()

    l1 = low.iloc[-1].item()
    l2 = low.iloc[-2].item()
    l3 = low.iloc[-3].item()

    c1 = close.iloc[-1].item()
    c2 = close.iloc[-2].item()

    if h1 > h2 > h3 and l1 > l2 > l3:
        return "BULLISH"

    if h1 < h2 < h3 and l1 < l2 < l3:
        return "BEARISH"

    if h1 > h2 and l1 > l2 and c1 > c2:
        return "BULLISH"

    if h1 < h2 and l1 < l2 and c1 < c2:
        return "BEARISH"

    return "NEUTRAL"

def detect_hh_hl_lh_ll_structure(data, lookback=40):
    if data is None or data.empty or len(data) < 10:
        return {
            "structure": "NEUTRAL",
            "last_high": None,
            "last_low": None,
            "prev_high": None,
            "prev_low": None,
        }

    window = data.tail(lookback)
    highs = window["High"]
    lows = window["Low"]
    swing_highs = []
    swing_lows = []

    for index in range(2, len(window) - 2):
        high_value = float(highs.iloc[index])
        low_value = float(lows.iloc[index])

        if high_value >= float(highs.iloc[index - 1]) and high_value >= float(highs.iloc[index + 1]):
            swing_highs.append(high_value)

        if low_value <= float(lows.iloc[index - 1]) and low_value <= float(lows.iloc[index + 1]):
            swing_lows.append(low_value)

    if len(swing_highs) < 2:
        swing_highs = [float(value) for value in highs.tail(8).nlargest(2).sort_index()]

    if len(swing_lows) < 2:
        swing_lows = [float(value) for value in lows.tail(8).nsmallest(2).sort_index()]

    last_high = swing_highs[-1] if swing_highs else None
    prev_high = swing_highs[-2] if len(swing_highs) >= 2 else None
    last_low = swing_lows[-1] if swing_lows else None
    prev_low = swing_lows[-2] if len(swing_lows) >= 2 else None
    structure = "NEUTRAL"

    if last_high is not None and prev_high is not None and last_low is not None and prev_low is not None:
        if last_high > prev_high and last_low > prev_low:
            structure = "HH_HL"
        elif last_high < prev_high and last_low < prev_low:
            structure = "LH_LL"

    return {
        "structure": structure,
        "last_high": last_high,
        "last_low": last_low,
        "prev_high": prev_high,
        "prev_low": prev_low,
    }

# =========================
# 🧭 MULTI-TIMEFRAME CONFLUENCE
# =========================
def detect_mtf_confluence(data_5m, data_15m, symbol):
    if data_5m.empty or data_15m.empty or len(data_15m) < 30:
        return "NEUTRAL", 0

    close_15 = data_15m["Close"]

    ema9_15 = close_15.ewm(span=9).mean().iloc[-1].item()
    ema21_15 = close_15.ewm(span=21).mean().iloc[-1].item()
    ema50_15 = close_15.ewm(span=50).mean().iloc[-1].item()

    htf_structure = detect_htf_structure(data_15m)

    score = 0

    if ema9_15 > ema21_15 > ema50_15:
        score += 2
        bias = "BULLISH"
    elif ema9_15 < ema21_15 < ema50_15:
        score -= 2
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    if htf_structure == "BULLISH":
        score += 2
    elif htf_structure == "BEARISH":
        score -= 2

    if score >= 3:
        return "BULLISH", abs(score)

    if score <= -3:
        return "BEARISH", abs(score)

    return bias, abs(score)

# =========================
# 🧠 SMART CONFIDENCE ENGINE
# =========================
def calculate_smart_confidence(
    session_active,
    bullish_fvg,
    bearish_fvg,
    bullish_displacement,
    bearish_displacement,
    fake_breakout,
    mtf_bias,
    signal_side
):
    confidence = 50

    # Session
    if session_active:
        confidence += 10
    else:
        confidence -= 8

    # FVG
    if signal_side == "BUY" and bullish_fvg:
        confidence += 8

    if signal_side == "SELL" and bearish_fvg:
        confidence += 8

    # Displacement
    if signal_side == "BUY" and bullish_displacement:
        confidence += 10

    if signal_side == "SELL" and bearish_displacement:
        confidence += 10

    # Fake breakout penalty
    if fake_breakout != "NONE":
        confidence -= 18

    # MTF alignment
    if signal_side == "BUY":
        if mtf_bias == "BULLISH":
            confidence += 12
        elif mtf_bias == "BEARISH":
            confidence -= 12

    if signal_side == "SELL":
        if mtf_bias == "BEARISH":
            confidence += 12
        elif mtf_bias == "BULLISH":
            confidence -= 12

    return max(1, min(99, confidence))

def clamp_score(value, low=1, high=99):
    return max(low, min(high, int(round(value))))

def calculate_dynamic_smc_retest_scores(
    side,
    symbol,
    htf_structure,
    choch_signal,
    bullish_bos,
    bearish_bos,
    displacement_score,
    bullish_displacement,
    bearish_displacement,
    recent_pressure,
    momentum_strength,
    zone_position,
    session_active,
    retest_distance,
    retest_buffer,
    candle_body,
    candle_range,
    fake_breakout,
):
    side = str(side or "").upper()
    bullish_side = side == "BUY"
    side_score = 58
    confidence = 48
    components = {}

    aligned_htf = "BULLISH" if bullish_side else "BEARISH"
    opposite_htf = "BEARISH" if bullish_side else "BULLISH"
    aligned_choch = "BULLISH" if bullish_side else "BEARISH"
    aligned_pressure = "BULLISH" if bullish_side else "BEARISH"
    opposite_pressure = "BEARISH" if bullish_side else "BULLISH"
    preferred_zone = "DISCOUNT" if bullish_side else "PREMIUM"
    bad_zone = "PREMIUM" if bullish_side else "DISCOUNT"
    has_aligned_bos = bullish_bos if bullish_side else bearish_bos
    has_aligned_displacement = bullish_displacement if bullish_side else bearish_displacement

    if htf_structure == aligned_htf:
        components["htf_structure"] = 8
    elif htf_structure == "NEUTRAL":
        components["htf_structure"] = 3
    elif htf_structure == opposite_htf:
        components["htf_structure"] = -8
    else:
        components["htf_structure"] = 0

    components["choch"] = 7 if choch_signal == aligned_choch else 0
    components["bos"] = 6 if has_aligned_bos else 0

    displacement_component = min(10, max(0, float(displacement_score or 0) * 2))
    components["displacement"] = displacement_component if has_aligned_displacement else -3

    if recent_pressure == aligned_pressure:
        components["recent_pressure"] = 6
    elif recent_pressure == opposite_pressure:
        components["recent_pressure"] = -6
    else:
        components["recent_pressure"] = 1

    if momentum_strength == "STRONG":
        components["momentum"] = 6
    elif momentum_strength == "MEDIUM":
        components["momentum"] = 3
    else:
        components["momentum"] = -2

    if zone_position == preferred_zone:
        components["zone"] = 5
    elif zone_position == bad_zone:
        components["zone"] = -5
    else:
        components["zone"] = 0

    components["session"] = 3 if session_active else -4

    try:
        retest_ratio = abs(float(retest_distance)) / max(float(retest_buffer), 1e-12)
    except (TypeError, ValueError):
        retest_ratio = 1

    retest_quality = max(0, min(1, 1 - retest_ratio))
    components["retest_quality"] = round(retest_quality * 10, 2)

    try:
        body_ratio = abs(float(candle_body)) / max(float(candle_range), 1e-12)
    except (TypeError, ValueError):
        body_ratio = 0

    if body_ratio >= 0.65:
        components["body_strength"] = 6
    elif body_ratio >= 0.45:
        components["body_strength"] = 3
    else:
        components["body_strength"] = -2

    components["fake_breakout"] = -10 if fake_breakout != "NONE" else 0

    component_total = sum(float(value or 0) for value in components.values())
    side_score = clamp_score(side_score + component_total, 55, 94)
    opposite_score = clamp_score(100 - side_score + max(0, -components.get("recent_pressure", 0)), 5, 42)
    confidence = clamp_score(confidence + component_total + (6 if has_aligned_bos or choch_signal == aligned_choch else 0), 35, 92)

    return {
        "buy_score": side_score if bullish_side else opposite_score,
        "sell_score": opposite_score if bullish_side else side_score,
        "confidence": confidence,
        "components": {
            **components,
            "component_total": round(component_total, 2),
            "retest_distance": retest_distance,
            "retest_buffer": retest_buffer,
            "retest_quality": round(retest_quality, 3),
            "body_ratio": round(body_ratio, 3),
            "symbol": symbol,
            "side": side,
        },
    }


def detect_choch(data):
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    open_ = data["Open"]

    if len(data) < 7:
        return "NONE"

    c1 = close.iloc[-1].item()
    c2 = close.iloc[-2].item()

    h1 = high.iloc[-1].item()
    h2 = high.iloc[-2].item()
    h3 = high.iloc[-3].item()
    h4 = high.iloc[-4].item()
    h5 = high.iloc[-5].item()

    l1 = low.iloc[-1].item()
    l2 = low.iloc[-2].item()
    l3 = low.iloc[-3].item()
    l4 = low.iloc[-4].item()
    l5 = low.iloc[-5].item()

    o1 = open_.iloc[-1].item()

    body1 = abs(c1 - o1)
    recent_range = (high.iloc[-6:] - low.iloc[-6:]).mean().item()
    strong_shift = body1 > recent_range * 0.35

    recent_micro_high = max(h3, h4, h5)
    recent_micro_low = min(l3, l4, l5)

    lower_highs_before = h3 < h4 and h4 < h5
    higher_lows_before = l3 > l4 and l4 > l5

    bullish_break = c1 > recent_micro_high and c1 > c2 and h1 > h2
    bearish_break = c1 < recent_micro_low and c1 < c2 and l1 < l2

    if lower_highs_before and bullish_break and strong_shift:
        return "BULLISH"

    if higher_lows_before and bearish_break and strong_shift:
        return "BEARISH"

    return "NONE"


def detect_pd_zone(data):
    high = data["High"]
    low = data["Low"]
    close = data["Close"]

    if len(data) < 12:
        return "NEUTRAL", 0.5, 0, 0, 0

    range_high = high.iloc[-12:].max().item()
    range_low = low.iloc[-12:].min().item()
    current_price = close.iloc[-1].item()

    dealing_range = range_high - range_low
    if dealing_range <= 0:
        return "NEUTRAL", 0.5, range_high, range_low, current_price

    eq = (range_high + range_low) / 2
    position = (current_price - range_low) / dealing_range

    if current_price < eq:
        zone = "DISCOUNT"
    elif current_price > eq:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    return zone, position, range_high, range_low, current_price


def detect_swing_liquidity(data):
    high = data["High"]
    low = data["Low"]

    if len(data) < 10:
        return None, None

    swing_points = []
    swing_highs = []
    swing_lows = []

    for i in range(2, len(data) - 2):
        h = high.iloc[i].item()
        l = low.iloc[i].item()

        h1 = high.iloc[i - 1].item()
        h2 = high.iloc[i - 2].item()
        h3 = high.iloc[i + 1].item()
        h4 = high.iloc[i + 2].item()

        l1 = low.iloc[i - 1].item()
        l2 = low.iloc[i - 2].item()
        l3 = low.iloc[i + 1].item()
        l4 = low.iloc[i + 2].item()

        if h > h1 and h > h2 and h > h3 and h > h4:
            swing_highs.append(h)

            swing_points.append({
                "type": "HIGH",
                "price": float(h),
                "index": int(i),
                "time": int(pd.Timestamp(data.index[i]).timestamp())
            })
        if l < l1 and l < l2 and l < l3 and l < l4:
            swing_lows.append(l)
            swing_points.append({
                "type": "LOW",
                "price": float(l),
                "index": int(i),
                "time": int(pd.Timestamp(data.index[i]).timestamp())
            })
    last_swing_high = swing_highs[-1] if len(swing_highs) >= 1 else None
    prev_swing_high = swing_highs[-2] if len(swing_highs) >= 2 else None

    last_swing_low = swing_lows[-1] if len(swing_lows) >= 1 else None
    prev_swing_low = swing_lows[-2] if len(swing_lows) >= 2 else None

    market_structure = "RANGE"

    if (
        last_swing_high is not None
        and prev_swing_high is not None
        and last_swing_low is not None
        and prev_swing_low is not None
    ):
        if last_swing_high > prev_swing_high and last_swing_low > prev_swing_low:
            market_structure = "BULLISH"

        elif last_swing_high < prev_swing_high and last_swing_low < prev_swing_low:
            market_structure = "BEARISH"

    return (
        last_swing_high,
        last_swing_low,
        prev_swing_high,
        prev_swing_low,
        market_structure,
        swing_points[-12:]
    )

def calculate_atr_values(data, period=14):
    if data is None or len(data) == 0:
        return pd.Series(dtype=float)

    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    close = data["Close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat([
        (high - low).abs(),
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()

def get_actionable_15m_swing_level(
    closed_data_15m,
    side,
    symbol=None,
    reference_price=None,
):
    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol) if symbol else "UNKNOWN"
    debug = {
        "symbol": normalized_symbol,
        "side": side,
        "reference_price": reference_price,
        "atr": None,
        "selected": None,
        "candidates": [],
        "rejected": [],
        "reason": None,
    }

    if side not in ["BUY", "SELL"]:
        debug["reason"] = "No actionable 15m side"
        return None, debug

    if closed_data_15m is None or len(closed_data_15m) < 10:
        debug["reason"] = "Not enough closed 15m candles"
        return None, debug

    try:
        data = closed_data_15m.copy()
        if reference_price is None:
            reference_price = float(data["Close"].iloc[-1])
        else:
            reference_price = float(reference_price)
        debug["reference_price"] = reference_price

        atr_values = calculate_atr_values(data)
        current_atr = float(atr_values.iloc[-1]) if len(atr_values) else 0.0
        debug["atr"] = current_atr
        if current_atr <= 0:
            debug["reason"] = "15m ATR unavailable"
            return None, debug

        recent_limit = 50
        atr_limit = get_15m_actionable_swing_atr_limit(normalized_symbol)
        max_distance = current_atr * atr_limit
        close_tie_distance = current_atr * 0.25
        point_tolerance = max(
            get_strategy_pip_size(normalized_symbol) or 0.0001,
            1e-10,
        )
        debug["atr_limit"] = atr_limit

        candidates = []

        for i in range(2, len(data) - 2):
            high_value = float(data["High"].iloc[i])
            low_value = float(data["Low"].iloc[i])
            swing_type = None
            level = None

            if side == "BUY":
                if (
                    high_value > float(data["High"].iloc[i - 1])
                    and high_value > float(data["High"].iloc[i - 2])
                    and high_value > float(data["High"].iloc[i + 1])
                    and high_value > float(data["High"].iloc[i + 2])
                ):
                    swing_type = "HIGH"
                    level = high_value
            else:
                if (
                    low_value < float(data["Low"].iloc[i - 1])
                    and low_value < float(data["Low"].iloc[i - 2])
                    and low_value < float(data["Low"].iloc[i + 1])
                    and low_value < float(data["Low"].iloc[i + 2])
                ):
                    swing_type = "LOW"
                    level = low_value

            if level is None:
                continue

            swing_atr = float(atr_values.iloc[i]) if len(atr_values) > i else current_atr
            candle_range = high_value - low_value
            following = data.iloc[i + 1:min(len(data), i + 7)]
            if side == "BUY":
                pullback = (
                    max(0.0, level - float(following["Low"].min()))
                    if len(following)
                    else 0.0
                )
                correct_side = level > reference_price
            else:
                pullback = (
                    max(0.0, float(following["High"].max()) - level)
                    if len(following)
                    else 0.0
                )
                correct_side = level < reference_price

            distance = abs(level - reference_price)
            age_candles = len(data) - 1 - i
            meaningful_by_range = swing_atr > 0 and candle_range >= swing_atr * 0.8
            meaningful_by_rejection = swing_atr > 0 and pullback >= swing_atr * 0.5
            recent = age_candles <= recent_limit
            reasons = []

            if not correct_side:
                reasons.append("wrong side of reference price")
            if distance > max_distance:
                reasons.append(f"distance > {atr_limit:g}x 15m ATR")
            if not (meaningful_by_range or meaningful_by_rejection):
                reasons.append("noise: range <0.8x ATR and rejection <0.5x ATR")

            range_score = min(2.0, candle_range / swing_atr) if swing_atr > 0 else 0
            rejection_score = min(2.0, pullback / swing_atr) if swing_atr > 0 else 0
            recency_score = max(0.0, 1.0 - (age_candles / recent_limit))
            actionability_score = (
                max(0.0, atr_limit - (distance / current_atr)) / atr_limit
            )
            strength_score = (
                range_score * 35
                + rejection_score * 35
                + recency_score * 20
                + actionability_score * 10
            )
            candidate = {
                "type": swing_type,
                "price": level,
                "time": pd.Timestamp(data.index[i]).isoformat(),
                "index": int(i),
                "age_candles": int(age_candles),
                "recent": bool(recent),
                "distance": distance,
                "distance_pips": (
                    distance / point_tolerance if point_tolerance else None
                ),
                "atr_multiple": distance / current_atr if current_atr else None,
                "candle_range": candle_range,
                "range_atr": candle_range / swing_atr if swing_atr else None,
                "post_swing_rejection": pullback,
                "rejection_atr": pullback / swing_atr if swing_atr else None,
                "strength_score": round(strength_score, 2),
                "rejected_reasons": list(reasons),
            }
            candidates.append(candidate)

        valid_recent = [
            candidate for candidate in candidates
            if not candidate["rejected_reasons"] and candidate["recent"]
        ]
        valid_older = [
            candidate for candidate in candidates
            if not candidate["rejected_reasons"] and not candidate["recent"]
        ]
        selection_pool = valid_recent or valid_older

        def candidate_summary(candidate):
            return {
                **candidate,
                "price": round(candidate["price"], get_strategy_decimals(normalized_symbol) or 5),
                "distance": round(candidate["distance"], get_strategy_decimals(normalized_symbol) or 5),
                "distance_pips": (
                    round(candidate["distance_pips"], 2)
                    if candidate.get("distance_pips") is not None
                    else None
                ),
                "atr_multiple": (
                    round(candidate["atr_multiple"], 4)
                    if candidate.get("atr_multiple") is not None
                    else None
                ),
                "candle_range": round(candidate["candle_range"], get_strategy_decimals(normalized_symbol) or 5),
                "range_atr": (
                    round(candidate["range_atr"], 4)
                    if candidate.get("range_atr") is not None
                    else None
                ),
                "post_swing_rejection": round(candidate["post_swing_rejection"], get_strategy_decimals(normalized_symbol) or 5),
                "rejection_atr": (
                    round(candidate["rejection_atr"], 4)
                    if candidate.get("rejection_atr") is not None
                    else None
                ),
            }

        debug["candidates"] = [
            candidate_summary(candidate)
            for candidate in sorted(candidates, key=lambda item: item["distance"])[:24]
        ]
        debug["rejected"] = [
            candidate_summary(candidate)
            for candidate in candidates
            if candidate["rejected_reasons"]
        ][-24:]

        if not selection_pool:
            debug["reason"] = "No actionable 15m swing level within ATR range"
            print("ACTIONABLE_15M_SWING_SELECTION_DEBUG =", debug)
            return None, debug

        nearest_distance = min(candidate["distance"] for candidate in selection_pool)
        close_candidates = [
            candidate for candidate in selection_pool
            if candidate["distance"] <= nearest_distance + close_tie_distance
        ]
        selected = sorted(
            close_candidates,
            key=lambda item: (
                item["age_candles"],
                -item["strength_score"],
                item["distance"],
            ),
        )[0]
        debug["selected"] = candidate_summary(selected)
        debug["reason"] = "selected actionable 15m swing"
        print("ACTIONABLE_15M_SWING_SELECTION_DEBUG =", debug)
        return float(selected["price"]), debug
    except Exception as exc:
        debug["reason"] = f"actionable 15m swing selection unavailable: {exc}"
        print("ACTIONABLE_15M_SWING_SELECTION_DEBUG =", debug)
        return None, debug

def detect_equal_levels(data, tolerance):

    highs = data["High"].tolist()
    lows = data["Low"].tolist()

    equal_highs = []
    equal_lows = []

    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) <= tolerance:
                equal_highs.append(round((highs[i] + highs[j]) / 2, 5))

    for i in range(len(lows) - 1):
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) <= tolerance:
                equal_lows.append(round((lows[i] + lows[j]) / 2, 5))

    return {
        "equal_highs": equal_highs[-5:],
        "equal_lows": equal_lows[-5:]
    }


def detect_liquidity_trap(c1, o1, h1, l1, swing_high, swing_low, strong_body):
    body1 = abs(c1 - o1)

    bullish_trap = False
    bearish_trap = False

    if swing_low is not None:
        bullish_trap = l1 < swing_low and c1 > swing_low and c1 > o1 and body1 >= strong_body * 0.8

    if swing_high is not None:
        bearish_trap = h1 > swing_high and c1 < swing_high and c1 < o1 and body1 >= strong_body * 0.8

    if bullish_trap:
        return "BULLISH"
    if bearish_trap:
        return "BEARISH"
    return "NONE"


def detect_entry_timing(
    c1,
    o1,
    h1,
    l1,
    ema9,
    ema21,
    zone_position,
    pd_high,
    pd_low,
    symbol,
    bullish_bos,
    bearish_bos,
    htf_structure,
    choch_signal,
    recent_pressure,
    momentum_strength,
    market_state,
    trap_signal
):
    body1 = abs(c1 - o1)
    candle_range = h1 - l1
    dist_from_ema9 = abs(c1 - ema9)

    pullback_limit = get_pair_strategy_rule(symbol, "no_trade_pullback_limit")
    stretch_limit = get_pair_strategy_rule(symbol, "no_trade_stretch_limit")
    huge_body = get_pair_strategy_rule(symbol, "no_trade_huge_body")
    reentry_buffer = get_pair_strategy_rule(symbol, "no_trade_reentry_buffer")

    if candle_range <= 0:
        return "NEUTRAL"

    body_ratio = body1 / candle_range

    bullish_context = (
        (
            htf_structure == "BULLISH"
            and market_state in ["BULL_PULLBACK", "BULL_REVERSAL", "BULL_EXPANSION"]
        )
        or
        (
            choch_signal == "BULLISH"
            and recent_pressure == "BULLISH"
            and momentum_strength in ["MEDIUM", "STRONG"]
        )
        or
        (
            trap_signal == "BULLISH"
            and c1 > o1
            and recent_pressure == "BULLISH"
        )
    )

    bearish_context = (
        (
            htf_structure == "BEARISH"
            and market_state in ["BEAR_PULLBACK", "BEAR_REVERSAL", "BEAR_EXPANSION"]
        )
        or
        (
            choch_signal == "BEARISH"
            and recent_pressure == "BEARISH"
            and momentum_strength in ["MEDIUM", "STRONG"]
        )
        or
        (
            trap_signal == "BEARISH"
            and c1 < o1
            and recent_pressure == "BEARISH"
        )
    )

    bullish_rejection = (
        c1 > o1
        and c1 > ema9
        and l1 <= ema9 + reentry_buffer
        and body_ratio >= 0.45
    )

    bearish_rejection = (
        c1 < o1
        and c1 < ema9
        and h1 >= ema9 - reentry_buffer
        and body_ratio >= 0.45
    )

    too_stretched_buy = dist_from_ema9 > stretch_limit or (bullish_bos and body1 > huge_body)
    too_stretched_sell = dist_from_ema9 > stretch_limit or (bearish_bos and body1 > huge_body)

    # 🔥 fast trap-based reversal timing
    bullish_trap_reclaim = (
        trap_signal == "BULLISH"
        and c1 > o1
        and c1 > ema9
    )
    bearish_trap_reject = (
        trap_signal == "BEARISH"
        and c1 < o1
        and c1 < ema9
    )

    if bullish_context:
        if bullish_trap_reclaim:
            if zone_position == "DISCOUNT":
                return "GOOD_BUY"
            return "WAIT_PULLBACK_BUY"

        if zone_position == "PREMIUM":
            return "WAIT_PULLBACK_BUY"

        if too_stretched_buy and c1 > ema9:
            return "LATE_BUY"

        if bullish_rejection and dist_from_ema9 <= pullback_limit:
            return "GOOD_BUY"

        if c1 > ema9 and dist_from_ema9 <= pullback_limit and body_ratio >= 0.35:
            return "GOOD_BUY"

        if c1 > ema9 and not too_stretched_buy:
            return "WAIT_PULLBACK_BUY"

        return "NEUTRAL"

    if bearish_context:
        if bearish_trap_reject:
            if zone_position == "PREMIUM":
                return "GOOD_SELL"
            return "WAIT_PULLBACK_SELL"

        if zone_position == "DISCOUNT":
            return "WAIT_PULLBACK_SELL"

        if too_stretched_sell and c1 < ema9:
            return "LATE_SELL"

        if bearish_rejection and dist_from_ema9 <= pullback_limit:
            return "GOOD_SELL"

        if c1 < ema9 and dist_from_ema9 <= pullback_limit and body_ratio >= 0.35:
            return "GOOD_SELL"

        if c1 < ema9 and not too_stretched_sell:
            return "WAIT_PULLBACK_SELL"

        return "NEUTRAL"

    return "NEUTRAL"


def detect_market_state(data, ema9, ema21, ema50, choch_signal):
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    open_ = data["Open"]

    if len(data) < 8:
        return "RANGE"

    c1 = close.iloc[-1].item()
    c2 = close.iloc[-2].item()
    c3 = close.iloc[-3].item()

    o1 = open_.iloc[-1].item()
    o2 = open_.iloc[-2].item()
    o3 = open_.iloc[-3].item()

    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    body1 = abs(c1 - o1)
    avg_range = (high.iloc[-6:] - low.iloc[-6:]).mean().item()

    bullish_stack = ema9 > ema21 > ema50
    bearish_stack = ema9 < ema21 < ema50

    above_ema9 = c1 > ema9
    below_ema9 = c1 < ema9

    expansion_body = body1 > avg_range * 0.45

    if bullish_stack and above_ema9 and expansion_body and c1 > c2 > c3:
        return "BULL_EXPANSION"

    if bearish_stack and below_ema9 and expansion_body and c1 < c2 < c3:
        return "BEAR_EXPANSION"

    if bullish_stack and c1 < ema9 and choch_signal != "BEARISH":
        return "BULL_PULLBACK"

    if bearish_stack and c1 > ema9 and choch_signal != "BULLISH":
        return "BEAR_PULLBACK"

    if choch_signal == "BULLISH" and c1 > ema9:
        return "BULL_REVERSAL"

    if choch_signal == "BEARISH" and c1 < ema9:
        return "BEAR_REVERSAL"

    return "RANGE"


def calculate_confidence(
    buy_score,
    sell_score,
    htf_structure,
    choch_signal,
    zone_position,
    trap_signal,
    market_state,
    entry_timing,
    market_condition
):
    top_score = max(buy_score, sell_score)
    score_gap = abs(buy_score - sell_score)
    direction = "BUY" if buy_score > sell_score else "SELL" if sell_score > buy_score else "NONE"

    confidence = 5

    confidence += top_score * 0.38
    confidence += score_gap * 0.22

    if direction == "BUY":
        if htf_structure == "BULLISH":
            confidence += 7
        if choch_signal == "BULLISH":
            confidence += 5
        if zone_position == "DISCOUNT":
            confidence += 4
        if trap_signal == "BULLISH":
            confidence += 3
        if market_state in ["BULL_PULLBACK", "BULL_REVERSAL", "BULL_EXPANSION"]:
            confidence += 5
        if entry_timing == "GOOD_BUY":
            confidence += 7
        elif entry_timing == "WAIT_PULLBACK_BUY":
            confidence -= 5
        elif entry_timing == "LATE_BUY":
            confidence -= 9

    elif direction == "SELL":
        if htf_structure == "BEARISH":
            confidence += 7
        if choch_signal == "BEARISH":
            confidence += 5
        if zone_position == "PREMIUM":
            confidence += 4
        if trap_signal == "BEARISH":
            confidence += 3
        if market_state in ["BEAR_PULLBACK", "BEAR_REVERSAL", "BEAR_EXPANSION"]:
            confidence += 5
        if entry_timing == "GOOD_SELL":
            confidence += 7
        elif entry_timing == "WAIT_PULLBACK_SELL":
            confidence -= 5
        elif entry_timing == "LATE_SELL":
            confidence -= 9

    if market_condition == "CHOPPY":
        confidence -= 10
    elif market_condition == "MIXED":
        confidence -= 6
    elif market_condition == "UNKNOWN":
        confidence -= 4

    if top_score < 25:
        confidence *= 0.60
    elif top_score < 40:
        confidence *= 0.78

    if score_gap < 10:
        confidence *= 0.65
    elif score_gap < 20:
        confidence *= 0.82

    confidence = max(0, min(int(confidence), 95))
    return confidence


# =========================
# 📊 DATA FETCH (TWELVE DATA)
# =========================
import os
TWELVE_DATA_API_KEY = "6cdda0e63fd34eb586552edf157a188b"

def _normalize_td_values(values):
    if not values:
        return pd.DataFrame()

    df = pd.DataFrame(values).copy()

    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
        "datetime": "Datetime"
    }
    df = df.rename(columns=rename_map)

    needed = ["Open", "High", "Low", "Close"]
    for col in needed:
        if col not in df.columns:
            return pd.DataFrame()

    if "Volume" not in df.columns:
        df["Volume"] = 0

    numeric_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Datetime" in df.columns:
        df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
        df = df.dropna(subset=["Datetime"])
        df = df.sort_values("Datetime").set_index("Datetime")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df[["Open", "High", "Low", "Close", "Volume"]]


def safe_download(symbol, interval, outputsize=80, tries=1, pause=1):
    last_error = None

    if not TWELVE_DATA_API_KEY or TWELVE_DATA_API_KEY == "PASTE_YOUR_KEY_HERE":
        print("TWELVE DATA ERROR: missing API key")
        return pd.DataFrame()

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }

    for attempt in range(1, tries + 1):
        try:
            print(f"{symbol} {interval}: attempt {attempt}/{tries}")

            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()

            if payload.get("status") == "error":
                message = payload.get("message", "Unknown Twelve Data error")
                code = payload.get("code", "")
                raise ValueError(f"{code} {message}".strip())

            df = _normalize_td_values(payload.get("values", []))

            if not df.empty:
                print(f"{symbol} {interval}: OK ({len(df)} rows)")
                return df

            print(f"{symbol} {interval}: empty data")

        except Exception as e:
            last_error = e
            print(f"{symbol} {interval}: failed -> {e}")

        time.sleep(pause)

    print(f"{symbol} {interval}: FAILED after {tries} tries. Last error: {last_error}")
    return pd.DataFrame()

_FETCH_LOCK = False
def _update_market_cache(cache, key, fetched_df, label):
    if fetched_df is not None and not fetched_df.empty:
        cache[key] = fetched_df
        return True

    if cache[key].empty:
        print(f"{label}: no new data and no cached candles available")
        return False

    print(f"{label}: keeping last good cached candles")
    return False

def _fetch_twelvedata_symbol(symbol, interval, outputsize):
    td_symbol = "EUR/USD" if symbol == "EURUSD" else "XAU/USD"
    return safe_download(td_symbol, interval, outputsize=outputsize)

def _fetch_ctrader_symbol(symbol, timeframe, limit, force_refresh=False):
    print(f"CTRADER DATA TRY: {symbol} {timeframe}")

    try:
        df = get_ctrader_market_data(
            symbol,
            timeframe,
            limit=limit,
            force_refresh=force_refresh
        )
    except Exception as e:
        print(f"CTRADER DATA ERROR: {symbol} {timeframe} {e}")
        return pd.DataFrame()

    if df is not None and not df.empty:
        print(f"CTRADER DATA OK: {symbol} {timeframe} rows={len(df)}")
        return df

    print(f"CTRADER DATA EMPTY: {symbol} {timeframe}")
    return pd.DataFrame()

def _get_last_candle_timestamp(df):
    if df is None or df.empty:
        return None

    try:
        last_idx = pd.Timestamp(df.index[-1])
        if last_idx.tzinfo is None:
            last_idx = last_idx.tz_localize("UTC")
        else:
            last_idx = last_idx.tz_convert("UTC")
        return last_idx
    except Exception:
        return None

def log_candle_freshness(symbol, timeframe, df):
    now_utc = datetime.now(timezone.utc)
    last_timestamp = _get_last_candle_timestamp(df)
    age_minutes = None
    last_open = None
    last_high = None
    last_low = None
    last_close = None

    if last_timestamp is not None:
        age_minutes = round(
            (now_utc - last_timestamp.to_pydatetime()).total_seconds() / 60,
            2
        )

    if df is not None and not df.empty:
        last = df.iloc[-1]
        last_open = float(last.get("Open")) if pd.notna(last.get("Open")) else None
        last_high = float(last.get("High")) if pd.notna(last.get("High")) else None
        last_low = float(last.get("Low")) if pd.notna(last.get("Low")) else None
        last_close = float(last.get("Close")) if pd.notna(last.get("Close")) else None

    print("CANDLE_FRESHNESS_DEBUG =", {
        "symbol": normalize_symbol(symbol),
        "timeframe": timeframe,
        "last_candle_time": last_timestamp.isoformat() if last_timestamp is not None else None,
        "now_utc": now_utc.isoformat(),
        "age_minutes": age_minutes,
        "rows": len(df) if df is not None else 0,
        "last_open": last_open,
        "last_high": last_high,
        "last_low": last_low,
        "last_close": last_close,
        "source": "ctrader",
    })

    return {
        "last_candle_time": last_timestamp,
        "age_minutes": age_minutes,
    }

def _refresh_market_symbol(cache, source, symbol, timeframe, interval, outputsize, cache_key, force_refresh=False):
    log_candle_freshness(symbol, timeframe, cache.get(cache_key))

    if (
        not force_refresh
        and not cache[cache_key].empty
        and _is_ctrader_data_fresh(symbol, timeframe)
    ):
        print(f"CTRADER DATA HEALTH OK: {_ctrader_health_key(symbol, timeframe)}")
        MARKET_DATA_STATUS["last_fetch_source"] = "ctrader"
        MARKET_DATA_STATUS["fallback_used"] = False
        return True

    ctrader_df = _fetch_ctrader_symbol(
        symbol,
        timeframe,
        outputsize,
        force_refresh=force_refresh
    )
    candle_health = get_ctrader_candle_health(symbol, timeframe)

    if ctrader_df is not None and not ctrader_df.empty and candle_health.get("usable"):
        freshness = log_candle_freshness(symbol, timeframe, ctrader_df)
        last_candle_time = freshness.get("last_candle_time")
        _update_market_cache(
            cache,
            cache_key,
            ctrader_df,
            f"ctrader {symbol} {timeframe}"
        )
        if candle_health.get("missed_fetch_count", 0) == 0:
            _mark_ctrader_success(symbol, timeframe, timestamp=last_candle_time)
        MARKET_DATA_STATUS["last_fetch_source"] = "ctrader"
        MARKET_DATA_STATUS["fallback_used"] = candle_health.get("source") == "ctrader_cache"
        print("BRAIN_CANDLE_DEBUG =", {
            **candle_health,
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "rows": len(ctrader_df),
            "brain_accepted": True,
        })
        return True

    if ctrader_df is not None and not ctrader_df.empty and cache[cache_key].empty:
        cache[cache_key] = ctrader_df

    print(f"CTRADER DATA HEALTH STALE: {_ctrader_health_key(symbol, timeframe)}")
    print("BRAIN_CANDLE_DEBUG =", {
        **candle_health,
        "symbol": normalize_symbol(symbol),
        "timeframe": timeframe,
        "rows": len(ctrader_df) if ctrader_df is not None else 0,
        "brain_accepted": False,
    })

    if not MARKET_DATA_STATUS.get("error"):
        MARKET_DATA_STATUS["error"] = "cTrader candles unavailable"

    MARKET_DATA_STATUS["fallback_used"] = False
    return False

def _refresh_market_pair(cache, source, timeframe, interval, outputsize, eurusd_key, gold_key, force_refresh=False):
    eurusd_updated = _refresh_market_symbol(
        cache,
        source,
        "EURUSD",
        timeframe,
        interval,
        outputsize,
        eurusd_key,
        force_refresh=force_refresh
    )
    xauusd_updated = _refresh_market_symbol(
        cache,
        source,
        "XAUUSD",
        timeframe,
        interval,
        outputsize,
        gold_key,
        force_refresh=force_refresh
    )

    return eurusd_updated and xauusd_updated

def _advance_cached_market_frames(cache):
    frame_map = [
        ("eurusd_5m", "EURUSD", "5m"),
        ("gold_5m", "XAUUSD", "5m"),
        ("eurusd_15m", "EURUSD", "15m"),
        ("gold_15m", "XAUUSD", "15m"),
        ("eurusd_1h", "EURUSD", "1h"),
        ("gold_1h", "XAUUSD", "1h"),
    ]

    for cache_key, symbol, timeframe in frame_map:
        current = cache.get(cache_key)

        if current is None or current.empty:
            continue

        advanced = append_current_forming_candle(
            current,
            symbol,
            timeframe,
        )
        if advanced is not None and not advanced.empty:
            cache[cache_key] = advanced

def _empty_market_data_cache():
    return {
        "eurusd_5m": pd.DataFrame(),
        "gold_5m": pd.DataFrame(),
        "eurusd_15m": pd.DataFrame(),
        "gold_15m": pd.DataFrame(),
        "eurusd_1h": pd.DataFrame(),
        "gold_1h": pd.DataFrame(),
        "last_5m_update": None,
        "last_15m_update": None,
        "last_1h_update": None,
    }


def hydrate_market_data_cache_from_disk():
    frame_map = {
        "eurusd_5m": ("EURUSD", "5m"),
        "gold_5m": ("XAUUSD", "5m"),
        "eurusd_15m": ("EURUSD", "15m"),
        "gold_15m": ("XAUUSD", "15m"),
        "eurusd_1h": ("EURUSD", "1h"),
        "gold_1h": ("XAUUSD", "1h"),
    }
    restored = {}

    for cache_key, (symbol, timeframe) in frame_map.items():
        persisted = hydrate_persisted_ctrader_candle_cache(symbol, timeframe)
        frame = (
            persisted.get("data")
            if isinstance(persisted, dict)
            else persisted
        )
        if frame is None or frame.empty:
            print("MARKET_CACHE_HYDRATION_SKIPPED =", {
                "missing_symbol": symbol,
                "missing_timeframe": timeframe,
            })
            return False
        restored[cache_key] = frame

    cache = _empty_market_data_cache()
    cache.update(restored)
    hydrated_at = datetime.now(timezone.utc)
    cache.update({
        "last_5m_update": hydrated_at,
        "last_15m_update": hydrated_at,
        "last_1h_update": hydrated_at,
    })
    fetch_market_data._cache = cache
    MARKET_DATA_STATUS["last_fetch_source"] = "ctrader_cache"
    MARKET_DATA_STATUS["fallback_used"] = True
    MARKET_DATA_STATUS["error"] = None
    print("MARKET_CACHE_HYDRATED =", {
        cache_key: len(frame)
        for cache_key, frame in restored.items()
    })
    return True


def fetch_market_data(force_refresh=False):
    global _FETCH_LOCK

    started_at = time.time()
    now = datetime.now(timezone.utc)
    calendar_closed = is_market_calendar_closed()
    active_source = get_market_data_source()

    print("MARKET DATA SOURCE ACTIVE:", active_source)

    print("CTRADER DATA TRY START")

    MARKET_DATA_STATUS["fallback_used"] = False
    MARKET_DATA_STATUS["error"] = None

    if not hasattr(fetch_market_data, "_cache"):
        fetch_market_data._cache = _empty_market_data_cache()

    cache = fetch_market_data._cache

    if force_refresh:
        print("FORCE MARKET DATA REFRESH")

    if _FETCH_LOCK and not force_refresh:
        print("===== FETCH LOCK ACTIVE: USING CACHE =====")
        _advance_cached_market_frames(cache)
        return (
            cache["eurusd_5m"],
            cache["gold_5m"],
            cache["eurusd_15m"],
            cache["gold_15m"],
            cache["eurusd_1h"],
            cache["gold_1h"],
        )

    last_5m = cache["last_5m_update"]
    last_15m = cache["last_15m_update"]
    last_1h = cache["last_1h_update"]

    _FETCH_LOCK = True

    need_5m = (
        force_refresh
        or cache["eurusd_5m"].empty
        or cache["gold_5m"].empty
        or last_5m is None
        or (now - last_5m).total_seconds() >= 300
    )

    need_15m = (
        force_refresh
        or cache["eurusd_15m"].empty
        or cache["gold_15m"].empty
        or last_15m is None
        or (now - last_15m).total_seconds() >= 900
    )
    need_1h = (
        force_refresh
        or cache["eurusd_1h"].empty
        or cache["gold_1h"].empty
        or last_1h is None
        or (now - last_1h).total_seconds() >= 3600
    )
    try:
        if need_5m and (force_refresh or not calendar_closed or cache["eurusd_5m"].empty or cache["gold_5m"].empty):
            print("===== REFRESHING 5M DATA =====")
            _refresh_market_pair(
                cache,
                active_source,
                "5m",
                "5min",
                5000,
                "eurusd_5m",
                "gold_5m",
                force_refresh=force_refresh
            )
            cache["last_5m_update"] = now
        else:
            print("===== USING CACHED 5M DATA =====")

        if need_15m and (force_refresh or not calendar_closed or cache["eurusd_15m"].empty or cache["gold_15m"].empty):
            print("===== REFRESHING 15M DATA =====")
            _refresh_market_pair(
                cache,
                active_source,
                "15m",
                "15min",
                5000,
                "eurusd_15m",
                "gold_15m",
                force_refresh=force_refresh
            )
            cache["last_15m_update"] = now
        else:
            print("===== USING CACHED 15M DATA =====")

        if need_1h and (force_refresh or not calendar_closed or cache["eurusd_1h"].empty or cache["gold_1h"].empty):
            print("===== REFRESHING 1H DATA =====")
            _refresh_market_pair(
                cache,
                active_source,
                "1h",
                "1h",
                5000,
                "eurusd_1h",
                "gold_1h",
                force_refresh=force_refresh
            )
            cache["last_1h_update"] = now
        else:
            print("===== USING CACHED 1H DATA =====")

    finally:
        _FETCH_LOCK = False

    # Historical trendbars are refreshed on their normal timeframe cadence,
    # but the active candle must advance on every panel cycle using live ticks.
    _advance_cached_market_frames(cache)

    eurusd = cache["eurusd_5m"]
    gold = cache["gold_5m"]
    eurusd_htf = cache["eurusd_15m"]
    gold_htf = cache["gold_15m"]
    eurusd_1h = cache["eurusd_1h"]
    gold_1h = cache["gold_1h"]
    print("===== FETCH DEBUG =====")
    print("Time now:", now.strftime("%Y-%m-%d %H:%M:%S UTC"))
    print(
        "Last 5m update:",
        cache["last_5m_update"].strftime("%Y-%m-%d %H:%M:%S UTC")
        if cache["last_5m_update"] else None
    )
    print(
        "Last 15m update:",
        cache["last_15m_update"].strftime("%Y-%m-%d %H:%M:%S UTC")
        if cache["last_15m_update"] else None
    )
    print("EURUSD 5m rows:", len(eurusd))
    print("XAUUSD 5m rows:", len(gold))
    print("EURUSD 15m rows:", len(eurusd_htf))
    print("XAUUSD 15m rows:", len(gold_htf))

    print("EURUSD 5m columns:", eurusd.columns.tolist() if not eurusd.empty else "EMPTY")
    print("XAUUSD 5m columns:", gold.columns.tolist() if not gold.empty else "EMPTY")
    print("EURUSD 15m columns:", eurusd_htf.columns.tolist() if not eurusd_htf.empty else "EMPTY")
    print("XAUUSD 15m columns:", gold_htf.columns.tolist() if not gold_htf.empty else "EMPTY")
    print("MARKET_DATA_FETCH_DURATION_DEBUG =", {
        "seconds": round(time.time() - started_at, 2),
        "force_refresh": force_refresh,
        "source": active_source,
        "refreshed": {
            "5m": need_5m,
            "15m": need_15m,
            "1h": need_1h,
        },
    })

    return eurusd, gold, eurusd_htf, gold_htf, eurusd_1h, gold_1h

def detect_market_mode(
    data,
    htf_data,
    ema9,
    ema21,
    ema50,
    htf_structure,
    recent_pressure,
    momentum_strength,
    symbol
):
    close = data["Close"]
    high = data["High"]
    low = data["Low"]

    if len(data) < 12 or len(htf_data) < 8:
        return "UNKNOWN"

    c1 = close.iloc[-1].item()
    c2 = close.iloc[-2].item()

    recent_high = high.iloc[-8:-1].max().item()
    recent_low = low.iloc[-8:-1].min().item()

    bullish_ema = ema9 > ema21 > ema50
    bearish_ema = ema9 < ema21 < ema50

    bullish_breakout = c1 > recent_high and c1 > c2
    bearish_breakout = c1 < recent_low and c1 < c2

    ema_spread = abs(ema9 - ema21)

    flat_limit = get_pair_strategy_rule(symbol, "market_mode_flat_limit")

    if ema_spread < flat_limit and momentum_strength == "WEAK":
        return "RANGE"

    if htf_structure == "BULLISH" and bullish_ema:
        if bullish_breakout and momentum_strength in ["MEDIUM", "STRONG"]:
            return "BREAKOUT_BULL"
        if recent_pressure in ["BULLISH", "NEUTRAL"]:
            return "TREND_CONTINUATION_BULL"

    if htf_structure == "BEARISH" and bearish_ema:
        if bearish_breakout and momentum_strength in ["MEDIUM", "STRONG"]:
            return "BREAKOUT_BEAR"
        if recent_pressure in ["BEARISH", "NEUTRAL"]:
            return "TREND_CONTINUATION_BEAR"

    if htf_structure == "BULLISH" and recent_pressure == "BEARISH" and c1 < ema21:
        return "SHIFT_BEAR"

    if htf_structure == "BEARISH" and recent_pressure == "BULLISH" and c1 > ema21:
        return "SHIFT_BULL"

    return "MIXED"

SMC_STATE = {
    "EURUSD": {
       "pending": None,
        "stage": "WAIT",
        "bos_level": None,
        "sweep_level": None,
        "retest_seen": False,
        "entry_confirm_level": None,
        "structure_pattern": "NEUTRAL",
        "last_idea": None
        },
    "XAUUSD": {
        "pending": None,
        "stage": "WAIT",
        "bos_level": None,
        "sweep_level": None,
        "retest_seen": False,
        "entry_confirm_level": None,
        "structure_pattern": "NEUTRAL",
        "last_idea": None
            }
        }

def load_final_signal_hold():
    if not os.path.exists(FINAL_SIGNAL_HOLD_FILE):
        return {}

    try:
        with open(FINAL_SIGNAL_HOLD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        return {
            normalize_symbol(symbol): value
            for symbol, value in data.items()
            if isinstance(value, dict)
            and not value.get("consumed_at")
            and str(value.get("setup_freshness") or "").upper() != "EXPIRED"
        }
    except Exception as exc:
        print("FINAL_SIGNAL_HOLD_LOAD_ERROR =", str(exc))
        return {}

def save_final_signal_hold():
    try:
        with open(FINAL_SIGNAL_HOLD_FILE, "w", encoding="utf-8") as f:
            json.dump(FINAL_SIGNAL_HOLD, f, indent=2, default=str)
    except Exception as exc:
        print("FINAL_SIGNAL_HOLD_SAVE_ERROR =", str(exc))

FINAL_SIGNAL_HOLD = load_final_signal_hold()

def load_fifteen_m_swing_watch():
    if not os.path.exists(FIFTEEN_M_SWING_WATCH_FILE):
        return {}

    try:
        with open(FIFTEEN_M_SWING_WATCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print("FIFTEEN_M_SWING_WATCH_LOAD_ERROR =", str(exc))
        return {}

def save_fifteen_m_swing_watch():
    temp_file = None
    try:
        temp_file = f"{FIFTEEN_M_SWING_WATCH_FILE}.{uuid.uuid4().hex}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(FIFTEEN_M_SWING_WATCH, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, FIFTEEN_M_SWING_WATCH_FILE)
    except Exception as exc:
        print("FIFTEEN_M_SWING_WATCH_SAVE_ERROR =", str(exc))
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

FIFTEEN_M_SWING_WATCH = load_fifteen_m_swing_watch()

# The API restores this per-symbol close watermark from live_backup.json.  The
# strict strategy uses it to reject any BOS/confirmation that belonged to a
# position which has already finished.
LAST_POSITION_CLOSED_AT = {
    "EURUSD": 0.0,
    "XAUUSD": 0.0,
}


def set_last_position_closed_at(symbol, closed_at):
    normalized_symbol = normalize_symbol(symbol)
    try:
        timestamp = float(closed_at or 0)
    except (TypeError, ValueError):
        return 0.0
    LAST_POSITION_CLOSED_AT[normalized_symbol] = max(
        float(LAST_POSITION_CLOSED_AT.get(normalized_symbol, 0) or 0),
        timestamp,
    )
    return LAST_POSITION_CLOSED_AT[normalized_symbol]


def clear_symbol_entry_memory(symbol, reason, closed_at=None):
    normalized_symbol = normalize_symbol(symbol)
    removed_watches = []

    for side in ["BUY", "SELL"]:
        key = get_15m_swing_watch_key(normalized_symbol, side)
        if key in FIFTEEN_M_SWING_WATCH:
            FIFTEEN_M_SWING_WATCH.pop(key, None)
            removed_watches.append(key)

    hold_removed = clear_final_signal_hold_for_symbol(normalized_symbol, reason)
    if removed_watches:
        save_fifteen_m_swing_watch()
    if closed_at is not None:
        set_last_position_closed_at(normalized_symbol, closed_at)

    print("SYMBOL_ENTRY_MEMORY_INVALIDATED =", {
        "symbol": normalized_symbol,
        "reason": reason,
        "closed_at": LAST_POSITION_CLOSED_AT.get(normalized_symbol),
        "removed_watches": removed_watches,
        "final_signal_hold_removed": hold_removed,
    })
    return bool(removed_watches or hold_removed)

def get_final_signal_hold_key(symbol):
    return normalize_symbol(symbol)

def is_final_signal_hold_expired(held):
    if not isinstance(held, dict):
        return True

    if held.get("consumed_at"):
        return True

    if str(held.get("setup_freshness") or "").upper() == "EXPIRED":
        return True

    try:
        saved_at = pd.Timestamp(held.get("saved_at"))

        if saved_at.tzinfo is None:
            saved_at = saved_at.tz_localize("UTC")
        else:
            saved_at = saved_at.tz_convert("UTC")

        age = (datetime.now(timezone.utc) - saved_at.to_pydatetime()).total_seconds()
        return age > FINAL_SIGNAL_HOLD_EXPIRATION_SECONDS
    except Exception:
        return True

def parse_final_signal_hold_timestamp(value):
    try:
        ts = pd.Timestamp(value)

        if pd.isna(ts):
            return None

        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        return ts
    except Exception:
        return None

def get_final_signal_hold_candle_age(saved_candle_time, current_candle_time):
    saved = parse_final_signal_hold_timestamp(saved_candle_time)
    current = parse_final_signal_hold_timestamp(current_candle_time)

    if saved is None or current is None:
        return None

    age_seconds = (current - saved).total_seconds()

    if pd.isna(age_seconds) or not math.isfinite(float(age_seconds)):
        return None

    return max(0, int(age_seconds // 300))

def calculate_final_signal_hold_tp1_progress(held, current_price):
    side = str(held.get("signal") or "").upper()
    held_fields = held.get("fields") or {}

    try:
        entry = float(held.get("entry") or held_fields.get("entry_price"))
        tp1 = float(held.get("tp1") or held_fields.get("tp1"))
        price = float(current_price)
    except (TypeError, ValueError):
        return None

    full_distance = abs(tp1 - entry)

    if full_distance <= 0:
        return None

    if side == "BUY":
        moved = price - entry
    elif side == "SELL":
        moved = entry - price
    else:
        return None

    return max(0, moved / full_distance)

def get_five_minute_candle_age(anchor_time, current_time):
    anchor = parse_final_signal_hold_timestamp(anchor_time)
    current = parse_final_signal_hold_timestamp(current_time)

    if anchor is None or current is None:
        return None

    age_seconds = (current - anchor).total_seconds()

    if pd.isna(age_seconds) or not math.isfinite(float(age_seconds)):
        return None

    return max(0, int(age_seconds // 300))

def evaluate_entry_freshness(
    side,
    bos_time,
    bos_price,
    broken_swing_price,
    confirmation_time,
    current_candle_time,
    entry_price,
    tp1,
    symbol,
):
    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    pip_size = get_strategy_pip_size(normalized_symbol)
    max_distance_pips = get_pair_strategy_rule(
        normalized_symbol,
        "freshness_max_distance_pips",
    )
    setup_age_candles = get_five_minute_candle_age(bos_time, current_candle_time)
    max_setup_age_candles = get_15m_pending_max_5m_candles(normalized_symbol)
    confirmation_age_candles = get_five_minute_candle_age(
        confirmation_time,
        current_candle_time,
    )
    score = 100
    reasons = []
    hard_expired = False

    try:
        entry_value = float(entry_price)
        broken_swing_value = float(broken_swing_price)
        distance_from_bos_pips = abs(entry_value - broken_swing_value) / pip_size
    except (TypeError, ValueError):
        entry_value = None
        broken_swing_value = None
        distance_from_bos_pips = None
        score -= 30
        reasons.append("broken swing or entry price unavailable")

    tp1_progress = None

    try:
        tp1_value = float(tp1)
        if entry_value is not None and broken_swing_value is not None:
            total_to_tp1 = abs(tp1_value - broken_swing_value)
            moved = abs(entry_value - broken_swing_value)
            tp1_progress = moved / total_to_tp1 if total_to_tp1 > 0 else 1
    except (TypeError, ValueError):
        tp1_progress = None

    if side not in ["BUY", "SELL"]:
        score -= 100
        hard_expired = True
        reasons.append("no actionable setup direction")

    if setup_age_candles is None:
        score -= 20
        hard_expired = True
        reasons.append("15m BOS/CHOCH time unavailable")
    elif setup_age_candles > max_setup_age_candles:
        score -= 45
        hard_expired = True
        reasons.append("15m close confirmation is stale")

    if confirmation_age_candles is None:
        score -= 20
        hard_expired = True
        reasons.append("5m confirmation time unavailable")
    elif confirmation_age_candles > FINAL_SIGNAL_HOLD_MAX_SETUP_CANDLES:
        score -= 45
        hard_expired = True
        reasons.append("more than 3 x 5m candles passed after confirmation")
    elif confirmation_age_candles > 0:
        score -= confirmation_age_candles * 10
        reasons.append("5m confirmation is not the latest closed candle")

    if distance_from_bos_pips is not None and distance_from_bos_pips > max_distance_pips:
        score -= 35
        hard_expired = True
        reasons.append("price moved too far from broken swing")

    if tp1_progress is not None and tp1_progress >= FINAL_SIGNAL_HOLD_MAX_TP1_PROGRESS:
        score -= 45
        hard_expired = True
        reasons.append("price already traveled more than 60% toward TP1")

    score = max(0, min(100, int(round(score))))
    fresh = score >= MIN_LIVE_FRESHNESS_SCORE and not hard_expired

    return {
        "fresh": fresh,
        "expired": not fresh,
        "setup_age_candles": setup_age_candles,
        "max_setup_age_candles": max_setup_age_candles,
        "confirmation_age_candles": confirmation_age_candles,
        "distance_from_bos_pips": (
            round(distance_from_bos_pips, 1)
            if distance_from_bos_pips is not None
            else None
        ),
        "freshness_score": score,
        "freshness_reason": (
            "Fresh BOS/CHOCH and current 5m confirmation"
            if fresh and not reasons
            else f"Freshness score ok: {'; '.join(reasons)}"
            if fresh
            else "; ".join(reasons) or "Setup expired / entry too late"
        ),
        "tp1_progress": round(tp1_progress, 3) if tp1_progress is not None else None,
        "max_confirmation_age_candles": FINAL_SIGNAL_HOLD_MAX_SETUP_CANDLES,
        "max_tp1_progress": FINAL_SIGNAL_HOLD_MAX_TP1_PROGRESS,
    }

def evaluate_setup_hold_expiration(held, current_candle_time, current_price):
    candle_age = get_final_signal_hold_candle_age(
        held.get("five_m_candle_time"),
        current_candle_time,
    )
    tp1_progress = calculate_final_signal_hold_tp1_progress(
        held,
        current_price,
    )

    if candle_age is not None and candle_age > FINAL_SIGNAL_HOLD_MAX_SETUP_CANDLES:
        return {
            "expired": True,
            "reason": f"setup older than {FINAL_SIGNAL_HOLD_MAX_SETUP_CANDLES} candles",
            "candle_age": candle_age,
            "tp1_progress": tp1_progress,
        }

    if (
        tp1_progress is not None
        and tp1_progress >= FINAL_SIGNAL_HOLD_MAX_TP1_PROGRESS
    ):
        return {
            "expired": True,
            "reason": "price reached 60% of TP1 without entry",
            "candle_age": candle_age,
            "tp1_progress": tp1_progress,
        }

    return {
        "expired": False,
        "reason": None,
        "candle_age": candle_age,
        "tp1_progress": tp1_progress,
    }

def evaluate_setup_freshness(
    previous_setup,
    side,
    bos_level,
    setup_candle_time,
    closed_data_5m,
    setup_result,
):
    side = str(side or "").upper()

    if side not in ["BUY", "SELL"]:
        return {
            "setup_freshness": "EXPIRED",
            "fresh": False,
            "reason": "No actionable setup",
        }

    previous_side = str((previous_setup or {}).get("signal") or "").upper()

    if previous_side != side:
        return {
            "setup_freshness": "FRESH",
            "fresh": True,
            "reason": f"Fresh {side} direction",
        }

    try:
        previous_bos = float((previous_setup or {}).get("bos_level"))
        current_bos = float(bos_level)
        new_bos = current_bos > previous_bos if side == "BUY" else current_bos < previous_bos
    except (TypeError, ValueError):
        new_bos = False

    if new_bos:
        return {
            "setup_freshness": "FRESH",
            "fresh": True,
            "reason": "New BOS beyond previous BOS",
        }

    score_debug = setup_result.get("score_contribution_debug") or {}
    fresh_choch = bool(
        score_debug.get("bullish_choch")
        if side == "BUY"
        else score_debug.get("bearish_choch")
    )
    previous_candle_time = (previous_setup or {}).get("setup_candle_time")

    if fresh_choch and setup_candle_time and setup_candle_time != previous_candle_time:
        return {
            "setup_freshness": "FRESH",
            "fresh": True,
            "reason": "Fresh CHOCH after retracement",
        }

    return {
        "setup_freshness": "ACTIVE",
        "fresh": False,
        "reason": f"{side} trend remains active without a fresh entry setup",
    }

def create_smc_state():
    return {
        "pending": None,
        "stage": "WAIT",
        "bos_level": None,
        "sweep_level": None,
        "retest_seen": False,
        "entry_confirm_level": None,
        "setup_candle_time": None,
        "structure_pattern": "NEUTRAL",
        "last_idea": None,
    }

# =========================
# 🤖 PAPER AUTO-TRADER
# =========================
AUTO_TRADES = {
    "EURUSD": None,
    "XAUUSD": None,
}
PAPER_ACTIVE_TRADES = []

PAPER_TRADE_HISTORY = []
PAPER_BACKUP_FILE = os.path.join(DATA_DIR, "paper_backup.json")
PAPER_RESET_KEY = "last_paper_reset"
PAPER_MAX_OPEN_SECONDS = 24 * 60 * 60
LAST_PAPER_RESET = 0
PAPER_SETUP_LOCKS = {}
LIVE_TRADES = {
    "EURUSD": None,
    "XAUUSD": None,
}

LIVE_TRADE_HISTORY = []

def create_test_live_trade():
    global LIVE_TRADES, LIVE_TRADE_HISTORY

    if LIVE_TRADES["EURUSD"] is not None:
        return

    trade = {
        "symbol": "EURUSD",
        "side": "BUY",
        "source": "live",
        "entry": 1.16000,
        "sl": 1.15800,
        "tp1": 1.16200,
        "tp2": 1.16400,
        "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "OPEN",
        "result": "RUNNING",
        "pips": 0
    }

    LIVE_TRADES["EURUSD"] = trade
    LIVE_TRADE_HISTORY.append(trade.copy())

if os.path.exists(PAPER_BACKUP_FILE):
    try:
        with open(PAPER_BACKUP_FILE, "r") as f:
            backup = json.load(f)

        loaded_paper_trades = backup.get("paper_trades", AUTO_TRADES)
        loaded_active_trades = backup.get("paper_active_trades")
        if isinstance(loaded_active_trades, list):
            PAPER_ACTIVE_TRADES = [
                trade for trade in loaded_active_trades
                if isinstance(trade, dict)
                and str(trade.get("status", "")).upper() == "OPEN"
            ]
        elif isinstance(loaded_paper_trades, dict):
            PAPER_ACTIVE_TRADES = [
                trade for trade in loaded_paper_trades.values()
                if isinstance(trade, dict)
                and str(trade.get("status", "")).upper() == "OPEN"
            ]

        PAPER_TRADE_HISTORY = backup.get(
            "paper_trade_history",
            PAPER_TRADE_HISTORY
        )
        LAST_PAPER_RESET = float(backup.get(PAPER_RESET_KEY, 0) or 0)
        PAPER_SETUP_LOCKS = backup.get("paper_setup_locks", {})
        if not isinstance(PAPER_SETUP_LOCKS, dict):
            PAPER_SETUP_LOCKS = {}

        for trade in PAPER_ACTIVE_TRADES:
            trade["symbol"] = normalize_symbol(trade.get("symbol"))

        for history_trade in PAPER_TRADE_HISTORY:
            if isinstance(history_trade, dict):
                history_trade["symbol"] = normalize_symbol(
                    history_trade.get("symbol")
                )

        for trade in PAPER_ACTIVE_TRADES:
            trade.setdefault("trade_id", f"paper-{uuid.uuid4()}")

            for history_trade in reversed(PAPER_TRADE_HISTORY):
                if (
                    history_trade.get("symbol") == trade.get("symbol")
                    and history_trade.get("opened_at") == trade.get("opened_at")
                    and str(history_trade.get("status", "")).upper() == "OPEN"
                ):
                    history_trade["trade_id"] = trade["trade_id"]
                    break

        AUTO_TRADES = {
            "EURUSD": next(
                (
                    trade for trade in reversed(PAPER_ACTIVE_TRADES)
                    if trade.get("symbol") == "EURUSD"
                ),
                None
            ),
            "XAUUSD": next(
                (
                    trade for trade in reversed(PAPER_ACTIVE_TRADES)
                    if trade.get("symbol") == "XAUUSD"
                ),
                None
            ),
        }

        print("✅ Loaded paper backup")

    except Exception as e:
        print("❌ Failed loading paper backup:", e)

def save_paper_backup():
    try:
        with open(PAPER_BACKUP_FILE, "w") as f:
            json.dump({
                "paper_trades": AUTO_TRADES,
                "paper_active_trades": PAPER_ACTIVE_TRADES,
                "paper_trade_history": PAPER_TRADE_HISTORY,
                "paper_setup_locks": PAPER_SETUP_LOCKS,
                PAPER_RESET_KEY: LAST_PAPER_RESET,
            }, f, indent=2)
    except Exception as e:
        print("❌ Failed saving paper backup:", e)

def get_last_sunday_5pm_local_ts(now=None):
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    reset = now.replace(hour=17, minute=0, second=0, microsecond=0)

    days_since_sunday = (now.weekday() - 6) % 7
    reset = reset - timedelta(days=days_since_sunday)

    if now.weekday() == 6 and now < reset:
        reset = reset - timedelta(days=7)

    return reset.timestamp()

def get_paper_trade_stats():
    wins = 0
    losses = 0
    running = 0
    cleanup = 0

    for trade in PAPER_TRADE_HISTORY:
        result = str(trade.get("result", "")).upper()
        status = str(trade.get("status", "")).upper()

        if "STALE" in result or "RESET" in result:
            cleanup += 1
        elif status == "CLOSED" and (
            result == "WIN"
            or "TP" in result
            or "PROFIT" in result
        ):
            wins += 1
        elif status == "CLOSED" and (
            result == "LOSS"
            or "SL" in result
            or "STOP" in result
        ):
            losses += 1
        elif status == "OPEN" or result in ["RUNNING", "TP1 HIT"]:
            running += 1

    return {
        "wins": wins,
        "losses": losses,
        "running": running,
        "cleanup": cleanup,
        "total": wins + losses + running,
    }

def run_weekly_paper_reset():
    global PAPER_TRADE_HISTORY, LAST_PAPER_RESET

    reset_ts = get_last_sunday_5pm_local_ts()

    if reset_ts <= LAST_PAPER_RESET:
        return

    before_count = len(PAPER_TRADE_HISTORY)

    PAPER_TRADE_HISTORY = [
        trade for trade in PAPER_TRADE_HISTORY
        if str(trade.get("status", "")).upper() == "OPEN"
        or str(trade.get("result", "")).upper() in ["RUNNING", "TP1 HIT"]
    ]

    LAST_PAPER_RESET = reset_ts
    removed_count = before_count - len(PAPER_TRADE_HISTORY)

    print(
        "PAPER WEEKLY RESET:",
        {
            "reset_time": datetime.fromtimestamp(reset_ts).isoformat(),
            "removed_closed_trades": removed_count,
            "kept_open_trades": len(PAPER_TRADE_HISTORY),
        }
    )

    save_paper_backup()

# =========================
# 🧱 FVG + SESSION HELPERS
# =========================
def detect_fvg(data, symbol):
    if len(data) < 5:
        return None

    candles = data.tail(8)

    min_gap = get_pair_strategy_rule(symbol, "fvg_min_gap")

    fvgs = []

    for i in range(2, len(candles)):
        c0 = candles.iloc[i - 2]
        c2 = candles.iloc[i]

        # Bullish FVG: candle 3 low is above candle 1 high
        if c2["Low"] > c0["High"]:
            gap = c2["Low"] - c0["High"]
            if gap >= min_gap:
                fvgs.append({
                    "type": "BULLISH",
                    "low": float(c0["High"]),
                    "high": float(c2["Low"]),
                    "mid": float((c0["High"] + c2["Low"]) / 2)
                })

        # Bearish FVG: candle 3 high is below candle 1 low
        if c2["High"] < c0["Low"]:
            gap = c0["Low"] - c2["High"]
            if gap >= min_gap:
                fvgs.append({
                    "type": "BEARISH",
                    "low": float(c2["High"]),
                    "high": float(c0["Low"]),
                    "mid": float((c2["High"] + c0["Low"]) / 2)
                })

    return fvgs[-1] if fvgs else None


def is_in_fvg_retest(price, fvg):
    if not fvg:
        return False

    return fvg["low"] <= price <= fvg["high"]


def get_session_state():
    now = datetime.now(timezone.utc)
    hour = now.hour

    # Main forex activity windows UTC
    london = 7 <= hour < 11
    new_york = 13 <= hour < 17
    overlap = 13 <= hour < 16

    if overlap:
        return "NY_LONDON_OVERLAP", True

    if london:
        return "LONDON", True

    if new_york:
        return "NEW_YORK", True

    return "LOW_ACTIVITY", False

# =========================
# ⚡ DISPLACEMENT ENGINE
# =========================
def detect_displacement(data, symbol):
    if len(data) < 8:
        return "WEAK", 0

    last = data.iloc[-1]
    recent = data.tail(8)

    body = abs(last["Close"] - last["Open"])
    candle_range = last["High"] - last["Low"]

    if candle_range <= 0:
        return "WEAK", 0

    avg_range = (recent["High"] - recent["Low"]).mean()
    body_ratio = body / candle_range

    min_body = get_pair_strategy_rule(symbol, "displacement_min_body")

    score = 0

    if body >= min_body:
        score += 2

    if candle_range >= avg_range * 1.25:
        score += 2

    if body_ratio >= 0.65:
        score += 3
    elif body_ratio >= 0.50:
        score += 2

    if last["Close"] > last["Open"]:
        direction = "BULLISH"
    elif last["Close"] < last["Open"]:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    if score >= 6:
        return f"STRONG_{direction}", score
    elif score >= 4:
        return f"MEDIUM_{direction}", score
    else:
        return f"WEAK_{direction}", score

# =========================
# 🧨 FAKE BREAKOUT KILLER
# =========================
def detect_fake_breakout(c1, o1, h1, l1, recent_high, recent_low, symbol):
    body = abs(c1 - o1)
    candle_range = h1 - l1

    if candle_range <= 0:
        return "NONE"

    upper_wick = h1 - max(c1, o1)
    lower_wick = min(c1, o1) - l1

    reclaim_buffer = get_pair_strategy_rule(symbol, "fake_breakout_reclaim_buffer")

    fake_bull_breakout = (
        h1 > recent_high
        and c1 < recent_high - reclaim_buffer
        and upper_wick > body * 1.8
    )

    fake_bear_breakout = (
        l1 < recent_low
        and c1 > recent_low + reclaim_buffer
        and lower_wick > body * 1.8
    )

    if fake_bull_breakout:
        return "FAKE_BULL_BREAKOUT"

    if fake_bear_breakout:
        return "FAKE_BEAR_BREAKOUT"

    return "NONE"

def parse_paper_trade_time(value):
    if not value:
        return None

    try:
        cleaned = str(value).replace(" UTC", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None

def update_open_paper_history(symbol, trade):
    trade_id = trade.get("trade_id")

    for i in range(len(PAPER_TRADE_HISTORY) - 1, -1, -1):
        if (
            PAPER_TRADE_HISTORY[i].get("symbol") == symbol
            and PAPER_TRADE_HISTORY[i].get("status") == "OPEN"
            and (
                (trade_id and PAPER_TRADE_HISTORY[i].get("trade_id") == trade_id)
                or (
                    not PAPER_TRADE_HISTORY[i].get("trade_id")
                    and PAPER_TRADE_HISTORY[i].get("opened_at") == trade.get("opened_at")
                )
            )
        ):
            PAPER_TRADE_HISTORY[i] = trade.copy()
            return True

    return False

def backfill_paper_trade_keys(trade):
    if not isinstance(trade, dict):
        return

    symbol = normalize_symbol(trade.get("symbol"))
    side = str(trade.get("side") or "").upper()
    trade["symbol"] = symbol

    if not trade.get("level_lock_key"):
        level_result = {
            "entry_price": trade.get("entry"),
            "stop_loss": trade.get("sl") or trade.get("original_sl"),
            "tp2": trade.get("tp2"),
        }
        trade["level_lock_key"] = get_paper_level_lock_key(symbol, side, level_result)

    if not trade.get("setup_lock_key"):
        trade["setup_lock_key"] = (
            trade.get("signal_key")
            or trade.get("level_lock_key")
        )

def dedupe_paper_trade_history():
    seen_closed_keys = set()
    deduped = []
    removed = 0

    for trade in PAPER_TRADE_HISTORY:
        if not isinstance(trade, dict):
            continue

        backfill_paper_trade_keys(trade)
        status = str(trade.get("status", "")).upper()
        keys = [
            trade.get("setup_lock_key"),
            trade.get("level_lock_key"),
            trade.get("signal_key"),
        ]
        keys = [key for key in keys if key]

        if status == "OPEN":
            deduped.append(trade)
            remember_paper_setup_lock(trade)
            continue

        duplicate_key = next(
            (key for key in keys if key in seen_closed_keys),
            None,
        )

        if duplicate_key:
            removed += 1
            continue

        for key in keys:
            seen_closed_keys.add(key)
        deduped.append(trade)
        remember_paper_setup_lock(trade)

    if removed:
        PAPER_TRADE_HISTORY[:] = deduped
        print("PAPER HISTORY DUPLICATES REMOVED:", {
            "removed": removed,
            "remaining": len(PAPER_TRADE_HISTORY),
        })

    return removed

def refresh_paper_trade_summary(symbol):
    AUTO_TRADES[symbol] = next(
        (
            trade for trade in reversed(PAPER_ACTIVE_TRADES)
            if trade.get("symbol") == symbol
            and str(trade.get("status", "")).upper() == "OPEN"
        ),
        None
    )

def dedupe_open_paper_trades():
    seen_symbols = set()
    deduped_active = []
    duplicate_ids = set()
    history_duplicates_removed = dedupe_paper_trade_history()

    for trade in reversed(PAPER_ACTIVE_TRADES):
        symbol = normalize_symbol(trade.get("symbol"))
        trade["symbol"] = symbol
        backfill_paper_trade_keys(trade)

        if (
            symbol in seen_symbols or
            str(trade.get("status", "")).upper() != "OPEN"
        ):
            duplicate_id = trade.get("trade_id")
            if duplicate_id:
                duplicate_ids.add(duplicate_id)
            trade["status"] = "CLOSED"
            trade["result"] = "DUPLICATE_CLOSED"
            trade["closed_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            continue

        seen_symbols.add(symbol)
        remember_paper_setup_lock(trade)
        deduped_active.append(trade)

    PAPER_ACTIVE_TRADES[:] = list(reversed(deduped_active))

    if duplicate_ids or history_duplicates_removed:
        for history_trade in PAPER_TRADE_HISTORY:
            if history_trade.get("trade_id") in duplicate_ids:
                history_trade["status"] = "CLOSED"
                history_trade["result"] = "DUPLICATE_CLOSED"
                history_trade["closed_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )

        print("PAPER DUPLICATES CLEARED:", {
            "duplicates": len(duplicate_ids),
            "history_duplicates_removed": history_duplicates_removed,
            "active_remaining": len(PAPER_ACTIVE_TRADES),
        })

        save_paper_backup()

    refresh_paper_trade_summary("EURUSD")
    refresh_paper_trade_summary("XAUUSD")

def remove_active_paper_trade(trade):
    trade_id = trade.get("trade_id")
    PAPER_ACTIVE_TRADES[:] = [
        active_trade
        for active_trade in PAPER_ACTIVE_TRADES
        if active_trade is not trade
        and (
            not trade_id
            or active_trade.get("trade_id") != trade_id
        )
    ]
    refresh_paper_trade_summary(trade.get("symbol"))

def get_paper_signal_key(symbol, side, result):
    setup_id = result.get("signal_setup_id")
    event_id = result.get("source_indicator_event_id")
    confirmation_id = result.get("m5_confirmation_id")
    if setup_id:
        return f"{symbol}:{side}:setup:{setup_id}"
    if event_id and confirmation_id:
        return f"{symbol}:{side}:event:{event_id}:confirmation:{confirmation_id}"
    signal_time = (
        result.get("five_m_closed_candle_time")
        or result.get("setup_candle_time")
    )

    if signal_time:
        return f"{symbol}:{side}:{signal_time}"

    return ":".join(str(value) for value in [
        symbol,
        side,
        result.get("entry_price"),
        result.get("stop_loss"),
        result.get("tp2"),
    ])

def _paper_round(symbol, value):
    try:
        decimals = 5 if normalize_symbol(symbol) == "EURUSD" else 2
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return None

def get_paper_level_lock_key(symbol, side, result):
    symbol = normalize_symbol(symbol)
    entry = _paper_round(symbol, result.get("entry_price"))
    sl = _paper_round(symbol, result.get("stop_loss"))
    tp2 = _paper_round(symbol, result.get("tp2"))

    if entry is None or sl is None or tp2 is None:
        return None

    return f"{symbol}:{side}:levels:{entry}:{sl}:{tp2}"

def get_paper_setup_lock_key(symbol, side, result):
    symbol = normalize_symbol(symbol)
    swing_break = result.get("fifteen_m_swing_break") or {}
    anchor_time = (
        result.get("fifteen_m_breakout_candle_time")
        or swing_break.get("closed_candle_time")
        or result.get("setup_candle_time")
        or result.get("five_m_closed_candle_time")
    )
    setup_level = (
        result.get("fifteen_m_swing_level")
        or result.get("fifteen_m_bos_level")
        or swing_break.get("swing_level")
        or result.get("structure_resistance")
        or result.get("structure_support")
    )
    rounded_level = _paper_round(symbol, setup_level)
    setup_type = str(
        result.get("strategy_setup_type")
        or result.get("plan_type")
        or "SETUP"
    ).replace(" ", "_").upper()

    if anchor_time and rounded_level is not None:
        return f"{symbol}:{side}:{setup_type}:{rounded_level}:{anchor_time}"

    return get_paper_level_lock_key(symbol, side, result)

def get_existing_paper_setup_lock(symbol, side, *keys):
    active_keys = {key for key in keys if key}

    if not active_keys:
        return None

    for key in active_keys:
        lock = PAPER_SETUP_LOCKS.get(key)
        if lock and lock.get("symbol") == symbol and lock.get("side") == side:
            return lock

    for trade in reversed(PAPER_TRADE_HISTORY):
        if (
            trade.get("symbol") == symbol
            and str(trade.get("side", "")).upper() == side
            and (
                trade.get("setup_lock_key") in active_keys
                or trade.get("level_lock_key") in active_keys
                or trade.get("signal_key") in active_keys
            )
        ):
            return trade

    return None

def remember_paper_setup_lock(trade):
    for key_name in ["setup_lock_key", "level_lock_key", "signal_key"]:
        key = trade.get(key_name)
        if not key:
            continue

        PAPER_SETUP_LOCKS[key] = {
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "trade_id": trade.get("trade_id"),
            "opened_at": trade.get("opened_at"),
            "entry": trade.get("entry"),
            "sl": trade.get("sl"),
            "tp2": trade.get("tp2"),
        }

dedupe_open_paper_trades()

def paper_trade_age_seconds(trade):
    timestamps = [
        trade.get("closed_at"),
        trade.get("opened_at"),
        trade.get("updated_at"),
    ]

    for timestamp in timestamps:
        parsed = parse_paper_trade_time(timestamp)
        if parsed:
            return (
                datetime.now(timezone.utc) -
                parsed.astimezone(timezone.utc)
            ).total_seconds()

    return None

def has_open_paper_trade_for_symbol(symbol):
    return any(
        trade for trade in PAPER_ACTIVE_TRADES
        if trade.get("symbol") == symbol
        and str(trade.get("status", "")).upper() == "OPEN"
    )

def has_seen_paper_signal(symbol, side, signal_key):
    if not signal_key:
        return False

    return any(
        trade for trade in PAPER_TRADE_HISTORY
        if trade.get("symbol") == symbol
        and str(trade.get("side", "")).upper() == side
        and trade.get("signal_key") == signal_key
    )

def is_paper_reentry_cooling_down(symbol, side):
    recent_trades = [
        trade for trade in reversed(PAPER_TRADE_HISTORY)
        if trade.get("symbol") == symbol
        and str(trade.get("side", "")).upper() == side
    ]

    if not recent_trades:
        return False, None

    age_seconds = paper_trade_age_seconds(recent_trades[0])

    if age_seconds is None:
        return False, None

    cooldown_seconds = get_configured_cooldown_seconds()
    return age_seconds < cooldown_seconds, age_seconds

def trim_paper_trade_history(max_trades=50):
    while len(PAPER_TRADE_HISTORY) > max_trades:
        closed_index = next(
            (
                index for index, trade in enumerate(PAPER_TRADE_HISTORY)
                if str(trade.get("status", "")).upper() == "CLOSED"
            ),
            None
        )

        if closed_index is None:
            return

        PAPER_TRADE_HISTORY.pop(closed_index)

def close_stale_paper_trade(symbol, trade, current_price, calc_pips):
    opened_at = parse_paper_trade_time(trade.get("opened_at"))
    if not opened_at:
        return False

    age_seconds = (
        datetime.now(timezone.utc) - opened_at.astimezone(timezone.utc)
    ).total_seconds()

    if age_seconds < PAPER_MAX_OPEN_SECONDS:
        return False

    trade["status"] = "CLOSED"
    trade["result"] = "STALE_CLOSED"
    trade["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    trade["closed_price"] = current_price
    trade["pips"] = calc_pips(
        symbol,
        trade["side"],
        trade["entry"],
        current_price
    )

    update_open_paper_history(symbol, trade)
    remove_active_paper_trade(trade)
    save_paper_backup()

    print("PAPER STALE CLEARED:", trade)
    return True

def update_paper_trade(
    symbol,
    result,
    current_price,
    current_low=None,
    current_high=None
):
    global AUTO_TRADES, PAPER_TRADE_HISTORY

    symbol = normalize_symbol(symbol)
    run_weekly_paper_reset()
    dedupe_open_paper_trades()

    def calc_pips(symbol, side, entry, close_price):
        pip_value = get_pair_strategy_rule(symbol, "paper_pip_value")

        if side == "BUY":
            return round((close_price - entry) / pip_value, 1)
        else:
            return round((entry - close_price) / pip_value, 1)

    def calculate_tp1(entry, tp2, side):
        tp1_ratio = get_tp1_ratio_of_tp2()

        if side == "BUY":
            return entry + ((tp2 - entry) * tp1_ratio)

        return entry - ((entry - tp2) * tp1_ratio)

    def calculate_protected_sl(entry, tp2, side):
        if side == "BUY":
            return entry + ((tp2 - entry) * 0.50)

        return entry - ((entry - tp2) * 0.50)

    def protect_trade_after_tp1(trade):
        if trade.get("hit_tp1"):
            return

        protected_sl = calculate_protected_sl(
            trade["entry"],
            trade["tp2"],
            trade["side"]
        )
        decimals = 5 if trade["symbol"] == "EURUSD" else 2

        trade["hit_tp1"] = True
        trade["profit_protected"] = True
        trade["protected_sl_price"] = round(protected_sl, decimals)
        trade["original_sl"] = trade.get("original_sl", trade["sl"])
        trade["sl"] = trade["protected_sl_price"]
        trade["result"] = "TP1 HIT"

        print("TP1_HIT_PROTECT_PROFIT:", {
            "symbol": trade["symbol"],
            "side": trade["side"],
            "entry": trade["entry"],
            "tp1": trade["tp1"],
            "tp2": trade["tp2"],
            "original_sl": trade["original_sl"],
            "protected_sl_price": trade["protected_sl_price"],
            "profit_protected": True,
        })

    signal = result.get("signal")

    is_buy_trade = signal == "BUY"
    is_sell_trade = signal == "SELL"

    active_trades = [
        trade for trade in list(PAPER_ACTIVE_TRADES)
        if trade.get("symbol") == symbol
        and str(trade.get("status", "")).upper() == "OPEN"
    ]

    for trade in active_trades:
        side = trade["side"]
        entry = trade["entry"]
        sl = trade["sl"]
        tp1 = trade["tp1"]
        tp2 = trade["tp2"]

        numeric_current_price = float(current_price)

        if close_stale_paper_trade(
            symbol,
            trade,
            numeric_current_price,
            calc_pips
        ):
            continue

        trade["pips"] = calc_pips(
            symbol,
            side,
            entry,
            numeric_current_price
        )

        numeric_current_low = float(
            current_low
            if current_low is not None
            else result.get("current_low", numeric_current_price)
        )
        numeric_current_high = float(
            current_high
            if current_high is not None
            else result.get("current_high", numeric_current_price)
        )

        if side == "BUY":
            if numeric_current_high >= tp2:
                trade["status"] = "CLOSED"
                trade["result"] = "WIN"
            elif numeric_current_high >= tp1:
                protect_trade_after_tp1(trade)

            if trade["status"] != "CLOSED" and numeric_current_low <= trade["sl"]:
                trade["status"] = "CLOSED"
                trade["result"] = (
                    "WIN" if trade.get("profit_protected") else "LOSS"
                )

        if side == "SELL":
            if numeric_current_low <= tp2:
                trade["status"] = "CLOSED"
                trade["result"] = "WIN"
            elif numeric_current_low <= tp1:
                protect_trade_after_tp1(trade)

            if trade["status"] != "CLOSED" and numeric_current_high >= trade["sl"]:
                trade["status"] = "CLOSED"
                trade["result"] = (
                    "WIN" if trade.get("profit_protected") else "LOSS"
                )

        update_open_paper_history(symbol, trade)

        if trade["status"] == "CLOSED":
            trade["closed_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            trade["closed_price"] = numeric_current_price
            trade["pips"] = calc_pips(
                symbol,
                side,
                entry,
                numeric_current_price
            )

            update_open_paper_history(symbol, trade)
            remove_active_paper_trade(trade)
            save_paper_backup()
            print("PAPER CLOSED:", trade)

    # OPEN NEW TRADE
    if is_buy_trade or is_sell_trade:
        entry = result.get("entry_price")
        sl = result.get("stop_loss")
        tp1 = result.get("tp1")
        tp2 = result.get("tp2")

        if entry == "--" or sl == "--" or tp1 == "--" or tp2 == "--":
            print("PAPER BLOCKED:", symbol, signal, entry, sl, tp1, tp2)
            return

        side = "BUY" if is_buy_trade else "SELL"
        signal_key = get_paper_signal_key(symbol, side, result)
        setup_lock_key = get_paper_setup_lock_key(symbol, side, result)
        level_lock_key = get_paper_level_lock_key(symbol, side, result)

        if has_open_paper_trade_for_symbol(symbol):
            print("PAPER BLOCKED:", {
                "symbol": symbol,
                "side": side,
                "reason": "active paper trade already running for symbol",
                "signal_key": signal_key,
            })
            return

        existing_setup_lock = get_existing_paper_setup_lock(
            symbol,
            side,
            setup_lock_key,
            level_lock_key,
            signal_key,
        )

        if existing_setup_lock or has_seen_paper_signal(symbol, side, signal_key):
            print("PAPER BLOCKED:", {
                "symbol": symbol,
                "side": side,
                "reason": "paper setup already handled",
                "setup_lock_key": setup_lock_key,
                "level_lock_key": level_lock_key,
                "signal_key": signal_key,
                "previous_trade_id": (
                    existing_setup_lock.get("trade_id")
                    if isinstance(existing_setup_lock, dict)
                    else None
                ),
            })
            return

        cooling_down, cooldown_age = is_paper_reentry_cooling_down(symbol, side)

        if cooling_down:
            cooldown_seconds = get_configured_cooldown_seconds()
            print("PAPER BLOCKED:", {
                "symbol": symbol,
                "side": side,
                "reason": "paper re-entry cooldown active",
                "cooldown_seconds": cooldown_seconds,
                "age_seconds": round(cooldown_age or 0, 1),
                "signal_key": signal_key,
            })
            return

        decimals = get_pair_strategy_rule(symbol, "paper_decimals")

        try:
            entry_value = float(entry)
            tp2_value = float(tp2)
            tp1 = round(calculate_tp1(entry_value, tp2_value, side), decimals)
        except (TypeError, ValueError):
            print("PAPER BLOCKED:", symbol, signal, entry, sl, tp1, tp2)
            return

        normalized = normalize_trade_levels(
            symbol,
            side,
            entry,
            sl,
            tp1,
            tp2
        )

        if not normalized.get("ok"):
            print(
                "PAPER BLOCKED:",
                symbol,
                side,
                entry,
                sl,
                tp1,
                tp2,
                normalized.get("reason")
            )
            return

        new_trade = {
            "trade_id": f"paper-{uuid.uuid4()}",
            "signal_key": signal_key,
            "setup_lock_key": setup_lock_key,
            "level_lock_key": level_lock_key,
            "symbol": symbol,
            "side": side,
            "source": "paper",
            "entry": normalized["entry"],
            "sl": normalized["sl"],
            "original_sl": normalized["sl"],
            "tp1": normalized["tp1"],
            "tp2": normalized["tp2"],
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "closed_at": None,
            "closed_price": None,
            "status": "OPEN",
            "result": "RUNNING",
            "pips": 0,
            "hit_tp1": False,
            "profit_protected": False,
            "protected_sl_price": None,
            "signal_setup_id": result.get("signal_setup_id"),
            "source_indicator_event_id": result.get("source_indicator_event_id"),
            "indicator_event_identity": copy.deepcopy(
                result.get("indicator_event_identity") or {}
            ),
            "m5_confirmation_id": result.get("m5_confirmation_id"),
            "m5_confirmation_identity": copy.deepcopy(
                result.get("m5_confirmation_identity") or {}
            ),
        }

        PAPER_ACTIVE_TRADES.append(new_trade)
        remember_paper_setup_lock(new_trade)
        refresh_paper_trade_summary(symbol)
        PAPER_TRADE_HISTORY.append(new_trade.copy())
        trim_paper_trade_history()

        save_paper_backup()

        print("PAPER OPEN GUARD:", {
            "symbol": symbol,
            "side": side,
            "trade_id": new_trade["trade_id"],
            "strategy_setup_complete": result.get("strategy_setup_complete"),
            "duplicate_signal_allowed": False,
            "active_symbol_trade_count": sum(
                1 for active_trade in PAPER_ACTIVE_TRADES
                if active_trade.get("symbol") == symbol
            ),
        })
        print("PAPER OPENED:", new_trade)
        return

def get_signal(data, htf_data, symbol, state_key=None):
    if data.empty or len(data) < 30:
        return {
            "signal": "WAIT",
            "signal_text": "WAIT ⚪ (not enough data)",
            "buy_pct": 0,
            "sell_pct": 0,
            "confidence": 0,
            "market_condition": "STRUCTURE",
            "entry_quality": "WAIT",
            "entry_timing": "WAIT",
            "fake_kill": False,
            "debug_reasons": ["Not enough data"]
        }

    if htf_data.empty or len(htf_data) < 10:
        htf_data = data.copy()

    close = data["Close"]
    open_ = data["Open"]
    high = data["High"]
    low = data["Low"]
    setup_candle_time = None

    try:
        setup_candle_time = pd.Timestamp(data.index[-1]).isoformat()
    except Exception:
        setup_candle_time = None

    c1 = close.iloc[-1].item()
    o1 = open_.iloc[-1].item()
    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    decimals = get_pair_strategy_rule(symbol, "signal_decimals")
    buffer = get_pair_strategy_rule(symbol, "signal_buffer")
    retest_buffer = get_pair_strategy_rule(symbol, "signal_retest_buffer")
    late_entry_distance = get_pair_strategy_rule(symbol, "signal_late_entry_distance")
    no_retest_max_distance = get_pair_strategy_rule(symbol, "signal_no_retest_max_distance")
    equal_tolerance = get_pair_strategy_rule(symbol, "equal_tolerance")

    reasons = []
    liquidity_levels = detect_equal_levels(
    data.tail(30),
    equal_tolerance
)

    fvg = detect_fvg(data, symbol)
    session_name, session_active = get_session_state()

    displacement, displacement_score = detect_displacement(data, symbol)

    bullish_displacement = displacement in ["STRONG_BULLISH", "MEDIUM_BULLISH"]
    bearish_displacement = displacement in ["STRONG_BEARISH", "MEDIUM_BEARISH"]

    bullish_fvg = fvg is not None and fvg["type"] == "BULLISH"
    bearish_fvg = fvg is not None and fvg["type"] == "BEARISH"

    price_in_fvg = is_in_fvg_retest(c1, fvg)

    buy_side_liquidity = liquidity_levels["equal_highs"]
    sell_side_liquidity = liquidity_levels["equal_lows"]

    nearest_buy_liquidity = (
        min([lvl for lvl in buy_side_liquidity if lvl > c1], default=None)
    )

    nearest_sell_liquidity = (
        max([lvl for lvl in sell_side_liquidity if lvl < c1], default=None)
    )

    (
        swing_high,
        swing_low,
        prev_swing_high,
        prev_swing_low,
        swing_structure,
        smc_swings
    ) = detect_swing_liquidity(data)
    fifteen_m_structure = detect_hh_hl_lh_ll_structure(data)
    fifteen_m_pattern = fifteen_m_structure.get("structure")

    recent_high = (
        prev_swing_high
        if prev_swing_high is not None
        else high.iloc[-20:-1].max().item()
    )

    recent_low = (
        prev_swing_low
        if prev_swing_low is not None
        else low.iloc[-20:-1].min().item()
    )

    fake_breakout = detect_fake_breakout(
        c1, o1, h1, l1, recent_high, recent_low, symbol
    )

    htf_structure = detect_htf_structure(htf_data)
    mtf_bias, mtf_score = detect_mtf_confluence(data, htf_data, symbol)
        # =========================
    # 15M TREND FILTER
    # =========================
    htf_close = htf_data["Close"]

    htf_ema9 = htf_close.ewm(span=9).mean().iloc[-1].item()
    htf_ema21 = htf_close.ewm(span=21).mean().iloc[-1].item()
    htf_ema50 = htf_close.ewm(span=50).mean().iloc[-1].item()

    htf_bullish = (
        htf_ema9 > htf_ema21 > htf_ema50
    )

    htf_bearish = (
        htf_ema9 < htf_ema21 < htf_ema50
    )

    bullish_sweep = (
    (
        swing_low is not None
        and l1 < swing_low
        and c1 > swing_low
        and c1 > o1
    )
    or
    (
        nearest_sell_liquidity is not None
        and l1 < nearest_sell_liquidity
        and c1 > nearest_sell_liquidity
        and c1 > o1
    )
)
    bearish_sweep = (
    (
        swing_high is not None
        and h1 > swing_high
        and c1 < swing_high
        and c1 < o1
    )
    or
    (
        nearest_buy_liquidity is not None
        and h1 > nearest_buy_liquidity
        and c1 < nearest_buy_liquidity
        and c1 < o1
    )
)

    bullish_choch = (
        htf_structure in ["BEARISH", "NEUTRAL"]
        and c1 > recent_high
        and c1 > o1
    )

    bearish_choch = (
        htf_structure in ["BULLISH", "NEUTRAL"]
        and c1 < recent_low
        and c1 < o1
    )

    bullish_bos = (
        htf_structure == "BULLISH"
        and c1 > recent_high
        and c1 > o1
    )

    bearish_bos = (
        htf_structure == "BEARISH"
        and c1 < recent_low
        and c1 < o1
    )

    ema9 = close.ewm(span=9).mean().iloc[-1].item()
    ema21 = close.ewm(span=21).mean().iloc[-1].item()
    ema50 = close.ewm(span=50).mean().iloc[-1].item()
    recent_pressure, recent_pressure_score = detect_recent_pressure(data, symbol)
    momentum_strength, momentum_score = detect_momentum_strength(data, symbol)
    zone_position, zone_score, pd_high, pd_low, _pd_price = detect_pd_zone(data)
    choch_signal = (
        "BULLISH" if bullish_choch
        else "BEARISH" if bearish_choch
        else "NONE"
    )
    market_condition = detect_market_condition(data, ema9, ema21, ema50)
    market_state = detect_market_state(data, ema9, ema21, ema50, choch_signal)
    eurusd_recovery_setup = bool(call_pair_strategy_hook(
        symbol,
        "is_recovery_setup",
        htf_structure,
        choch_signal,
        recent_pressure,
        c1,
        ema21,
    ))
    score_contribution_debug = {
        "symbol": symbol,
        "htf_structure": htf_structure,
        "fifteen_m_pattern": fifteen_m_pattern,
        "fifteen_m_last_high": fifteen_m_structure.get("last_high"),
        "fifteen_m_last_low": fifteen_m_structure.get("last_low"),
        "choch_signal": choch_signal,
        "recent_pressure": recent_pressure,
        "momentum_strength": momentum_strength,
        "zone_position": zone_position,
        "market_state": market_state,
        "mtf_bias": mtf_bias,
        "mtf_score": mtf_score,
        "bullish_fvg": bullish_fvg,
        "bearish_fvg": bearish_fvg,
        "bullish_bos": bullish_bos,
        "bearish_bos": bearish_bos,
        "bullish_choch": bullish_choch,
        "bearish_choch": bearish_choch,
        "bullish_displacement": bullish_displacement,
        "bearish_displacement": bearish_displacement,
        "displacement_score": displacement_score,
        "ema21_reclaimed": c1 > ema21,
        "eurusd_recovery_setup": eurusd_recovery_setup,
        "penalties": {
            "htf_bearish_penalty": 0,
            "sell_holding_penalty": 0,
            "bearish_fvg_penalty": 0,
            "structure_weighting_penalty": 0,
        },
        "branch": None,
    }

        # =========================
    # RECENT BOS MEMORY AFTER RESTART
    # =========================
    recent_lookback = data.tail(8)

    recent_bull_break = False
    recent_bear_break = False

    for _, candle in recent_lookback.iterrows():
        if candle["Close"] > recent_high and candle["Close"] > candle["Open"]:
            recent_bull_break = True

        if candle["Close"] < recent_low and candle["Close"] < candle["Open"]:
            recent_bear_break = True

    # Do NOT force BOS from tiny recent candles.
# BOS must come from real protected swing break only.
    if recent_bull_break and h1 > recent_high and c1 > recent_high:
        bullish_bos = True
        bullish_choch = True

    if recent_bear_break and l1 < recent_low and c1 < recent_low:
        bearish_bos = True
        bearish_choch = True

    buy_retest = (
        swing_low is not None
        and l1 <= swing_low + retest_buffer
        and c1 > swing_low
        and c1 > o1
    )

    sell_retest = (
        swing_high is not None
        and h1 >= swing_high - retest_buffer
        and c1 < swing_high
        and c1 < o1
    )

    final_signal = "WAIT"
    buy_score = 0
    sell_score = 0
    confidence = 0
    entry_quality = "WAIT"
    entry_timing = "WAIT"
    plan_type = "WAIT FOR BOS"
    plan_side = "WAIT"
    smc_branch = None
    dynamic_score_components = None
    hardcoded_score_removed = False
    breakout_entry_allowed = False
    breakout_plan_reason = None
    smc_breakout_debug = []
    momentum_breakout_debug = []
    strategy_setup_complete = False
    strategy_setup_type = None

    if bullish_sweep:
        reasons.append("Bullish liquidity sweep detected")

    if bearish_sweep:
        reasons.append("Bearish liquidity sweep detected")

    if fake_breakout != "NONE":
      reasons.append(fake_breakout)

    state = SMC_STATE.setdefault(state_key or symbol, create_smc_state())
        # =========================
    # 🔄 STRUCTURE FLIP RESET
    # =========================
    if state["pending"] == "SELL" and (bullish_choch or bullish_bos):
        state["pending"] = "BUY"
        state["bos_level"] = recent_high
        state["sweep_level"] = swing_low
        state["retest_seen"] = False
        state["entry_confirm_level"] = recent_high
        state["setup_candle_time"] = setup_candle_time
        state["structure_pattern"] = fifteen_m_pattern
        state["stage"] = "BUY_READY"
        reasons.append("State flipped from SELL to BUY")

    if state["pending"] == "BUY" and (bearish_choch or bearish_bos):
        state["pending"] = "SELL"
        state["bos_level"] = recent_low
        state["sweep_level"] = swing_high
        state["retest_seen"] = False
        state["entry_confirm_level"] = recent_low
        state["setup_candle_time"] = setup_candle_time
        state["structure_pattern"] = fifteen_m_pattern
        state["stage"] = "SELL_READY"
        reasons.append("State flipped from BUY to SELL")

    # SAVE BOS / CHOCH FIRST
    # SAVE BOS / CHOCH FIRST
    if state["pending"] is None:

        if bullish_choch or bullish_bos:
            state["pending"] = "BUY"
            state["bos_level"] = recent_high
            state["sweep_level"] = swing_low
            state["retest_seen"] = False
            state["entry_confirm_level"] = recent_high
            state["setup_candle_time"] = setup_candle_time
            state["structure_pattern"] = fifteen_m_pattern
            reasons.append("Bullish CHOCH/BOS saved")

        elif bearish_choch or bearish_bos:
            state["pending"] = "SELL"
            state["bos_level"] = recent_low
            state["sweep_level"] = swing_high
            state["retest_seen"] = False
            state["entry_confirm_level"] = recent_low
            state["setup_candle_time"] = setup_candle_time
            state["structure_pattern"] = fifteen_m_pattern
            reasons.append("Bearish CHOCH/BOS saved")

        # =========================
    # ENTRY TIMING ENGINE
    # =========================

    if state["pending"] == "BUY" and state["bos_level"] is not None:
        in_buy_retest_zone = (
    c1 > state["bos_level"] - retest_buffer
)
        buy_retest_touched = (
            l1 <= state["bos_level"] + retest_buffer
            and c1 >= state["bos_level"] - retest_buffer
        )
        if buy_retest_touched:
            state["retest_seen"] = True

        buy_close_confirmed = (
            state.get("retest_seen")
            and c1 >= (state.get("entry_confirm_level") or state["bos_level"])
            and c1 > o1
            and state.get("structure_pattern") in ["HH_HL", "NEUTRAL"]
        )
        buy_displacement = (
                c1 > o1
                and c1 > state["bos_level"] - (retest_buffer * 0.3)
            )

        buy_no_retest_momentum = (
            c1 > o1
            and c1 > state["bos_level"]
            and (c1 - state["bos_level"]) <= no_retest_max_distance
            and (h1 - l1) > 0
            and ((c1 - o1) / (h1 - l1)) >= 0.60
        )

        if in_buy_retest_zone and buy_close_confirmed:

            if (
                bullish_choch
                or bullish_bos
                or buy_displacement
                or recent_pressure == "BULLISH"
            ):

                final_signal = "BUY"

                dynamic_scores = calculate_dynamic_smc_retest_scores(
                    "BUY",
                    symbol,
                    htf_structure,
                    choch_signal,
                    bullish_bos,
                    bearish_bos,
                    displacement_score,
                    bullish_displacement,
                    bearish_displacement,
                    recent_pressure,
                    momentum_strength,
                    zone_position,
                    session_active,
                    abs(c1 - state["bos_level"]),
                    retest_buffer,
                    abs(c1 - o1),
                    h1 - l1,
                    fake_breakout,
                )
                buy_score = dynamic_scores["buy_score"]
                sell_score = dynamic_scores["sell_score"]
                confidence = dynamic_scores["confidence"]
                smc_branch = "SMC_BUY_RETEST"
                dynamic_score_components = dynamic_scores["components"]
                hardcoded_score_removed = True

                entry_quality = "SMC"
                entry_timing = "CHOCH/BOS RETEST"

                plan_type = "SMC BUY"
                plan_side = "BUY"

                state["pending"] = None
                state["stage"] = "BUY_ACTIVE"
                state["retest_seen"] = False
                strategy_setup_complete = True
                strategy_setup_type = "BUY_RETEST"

            else:
                state["stage"] = "BUY_READY"

                plan_type = "BUY READY"
                plan_side = "WAIT"
                entry_timing = "BUY READY"

                buy_score = 65
                sell_score = 10
                confidence = 55

        elif buy_no_retest_momentum:
            plan_type = "WAIT BUY RETEST OR STRONG BREAKOUT"
            plan_side = "WAIT"
            entry_timing = "WAIT BREAKOUT QUALITY"
            buy_score = 65
            sell_score = 12
            confidence = 52
            reasons.append("Bullish break exists, waiting for retest or stronger breakout confirmation")
        else:
            if state["stage"] in ["BUY_READY", "BUY_ACTIVE"] and state.get("retest_seen"):
                plan_type = "BUY HOLDING"
                plan_side = "WAIT"
                entry_timing = "WAIT 15M CLOSE ABOVE LAST HIGH"
                buy_score = 75
                sell_score = 10
                confidence = 70
                reasons.append("Retest seen; waiting 15m close above last high")
            else:
                plan_type = "WAIT BUY RETEST"
                plan_side = "WAIT"
                entry_timing = "WAIT BUY RETEST"
                buy_score = 45
                sell_score = 15
                confidence = 35

    elif state["pending"] == "SELL" and state["bos_level"] is not None:
        in_sell_retest_zone = (
    c1 < state["bos_level"] + retest_buffer
)
        sell_retest_touched = (
            h1 >= state["bos_level"] - retest_buffer
            and c1 <= state["bos_level"] + retest_buffer
        )
        if sell_retest_touched:
            state["retest_seen"] = True

        sell_close_confirmed = (
            state.get("retest_seen")
            and c1 <= (state.get("entry_confirm_level") or state["bos_level"])
            and c1 < o1
            and state.get("structure_pattern") in ["LH_LL", "NEUTRAL"]
        )
        sell_displacement = (
            c1 < o1
            and c1 < state["bos_level"] + (retest_buffer * 0.3)
        )

        sell_no_retest_momentum = (
            c1 < o1
            and c1 < state["bos_level"]
            and (state["bos_level"] - c1) <= no_retest_max_distance
            and (h1 - l1) > 0
            and ((o1 - c1) / (h1 - l1)) >= 0.60
        )

        if in_sell_retest_zone and sell_close_confirmed:

            if (
                bearish_choch
                or bearish_bos
                or sell_displacement
                or recent_pressure == "BEARISH"
            ):

                final_signal = "SELL"

                dynamic_scores = calculate_dynamic_smc_retest_scores(
                    "SELL",
                    symbol,
                    htf_structure,
                    choch_signal,
                    bullish_bos,
                    bearish_bos,
                    displacement_score,
                    bullish_displacement,
                    bearish_displacement,
                    recent_pressure,
                    momentum_strength,
                    zone_position,
                    session_active,
                    abs(c1 - state["bos_level"]),
                    retest_buffer,
                    abs(c1 - o1),
                    h1 - l1,
                    fake_breakout,
                )
                buy_score = dynamic_scores["buy_score"]
                sell_score = dynamic_scores["sell_score"]
                confidence = dynamic_scores["confidence"]
                smc_branch = "SMC_SELL_RETEST"
                dynamic_score_components = dynamic_scores["components"]
                hardcoded_score_removed = True

                entry_quality = "SMC"
                entry_timing = "CHOCH/BOS RETEST"

                plan_type = "SMC SELL"
                plan_side = "SELL"

                state["pending"] = None
                state["stage"] = "SELL_ACTIVE"
                state["retest_seen"] = False
                strategy_setup_complete = True
                strategy_setup_type = "SELL_RETEST"

            else:
                state["stage"] = "SELL_READY"

                plan_type = "SELL READY"
                plan_side = "WAIT"
                entry_timing = "SELL READY"

                if eurusd_recovery_setup:
                    recovery_state = call_pair_strategy_hook(
                        symbol,
                        "get_recovery_sell_state",
                        "sell_ready",
                    )
                    buy_score = recovery_state["buy_score"]
                    sell_score = recovery_state["sell_score"]
                    confidence = recovery_state["confidence"]
                    score_contribution_debug["branch"] = recovery_state["branch"]
                    score_contribution_debug["penalties"]["sell_holding_penalty"] = recovery_state["penalty"]
                    reasons.append(recovery_state["reason"])
                else:
                    buy_score = 10
                    sell_score = 65
                    confidence = 55
                    score_contribution_debug["branch"] = "SELL_READY"
                    score_contribution_debug["penalties"]["sell_holding_penalty"] = -55
        elif sell_no_retest_momentum:
            plan_type = "WAIT SELL RETEST OR STRONG BREAKOUT"
            plan_side = "WAIT"
            entry_timing = "WAIT BREAKOUT QUALITY"
            buy_score = 12
            sell_score = 65
            confidence = 52
            reasons.append("Bearish break exists, waiting for retest or stronger breakout confirmation")
        else:
            if state["stage"] in ["SELL_READY", "SELL_ACTIVE"] and state.get("retest_seen"):
                plan_type = "SELL HOLDING"
                plan_side = "WAIT"
                entry_timing = "WAIT 15M CLOSE BELOW LAST LOW"
                if eurusd_recovery_setup:
                    recovery_state = call_pair_strategy_hook(
                        symbol,
                        "get_recovery_sell_state",
                        "sell_holding_retest",
                    )
                    buy_score = recovery_state["buy_score"]
                    sell_score = recovery_state["sell_score"]
                    confidence = recovery_state["confidence"]
                    score_contribution_debug["branch"] = recovery_state["branch"]
                    score_contribution_debug["penalties"]["sell_holding_penalty"] = recovery_state["penalty"]
                    reasons.append(recovery_state["reason"])
                else:
                    buy_score = 10
                    sell_score = 75
                    confidence = 70
                    score_contribution_debug["branch"] = "SELL_HOLDING_RETEST"
                    score_contribution_debug["penalties"]["sell_holding_penalty"] = -65
                reasons.append("Retest seen; waiting 15m close below last low")
            else:
                plan_type = "WAIT SELL RETEST"
                plan_side = "WAIT"
                entry_timing = "WAIT SELL RETEST"
                buy_score = 15
                sell_score = 45
                confidence = 35

    else:
            # =========================
        # KEEP ACTIVE TREND MEMORY
        # =========================

        if (
            state["stage"] in ["BUY_READY", "BUY_ACTIVE"]
            and state["bos_level"] is not None
            and c1 > state["bos_level"] - retest_buffer
        ):

            final_signal = "WAIT"

            buy_score = 75
            sell_score = 10
            confidence = 70

            entry_quality = "HOLDING"
            entry_timing = "BUY HOLDING"

            plan_type = "BUY HOLDING"
            plan_side = "WAIT"
            reasons.append("Bullish trend still active")

        elif (
            state["stage"] in ["SELL_READY", "SELL_ACTIVE"]
            and state["bos_level"] is not None
            and c1 < state["bos_level"] + retest_buffer
        ):
            final_signal = "WAIT"

            if eurusd_recovery_setup:
                recovery_state = call_pair_strategy_hook(
                    symbol,
                    "get_recovery_sell_state",
                    "sell_holding_memory",
                )
                buy_score = recovery_state["buy_score"]
                sell_score = recovery_state["sell_score"]
                confidence = recovery_state["confidence"]
                score_contribution_debug["branch"] = recovery_state["branch"]
                score_contribution_debug["penalties"]["sell_holding_penalty"] = recovery_state["penalty"]
                reasons.append(recovery_state["reason"])
            else:
                buy_score = 10
                sell_score = 75
                confidence = 70
                score_contribution_debug["branch"] = "SELL_HOLDING_MEMORY"
                score_contribution_debug["penalties"]["sell_holding_penalty"] = -65

            entry_quality = "HOLDING"
            entry_timing = "WAIT SELL COMPLETION"

            plan_type = "SELL HOLDING"
            plan_side = "WAIT"
            reasons.append("Bearish trend active; waiting for completed 15m setup")

        else:

            if c1 > recent_low and c1 < recent_high:

                if c1 < ((recent_high + recent_low) / 2):

                    plan_type = "WAIT SELL BREAK"
                    entry_timing = "WAIT SELL BREAK"

                    sell_score = 45
                    buy_score = 15
                    confidence = 35

                    entry_quality = "BUY"

                    reasons.append("Price near support, waiting bearish BOS")

                else:

                    plan_type = "WAIT BUY BREAK"
                    entry_timing = "WAIT BUY BREAK"

                    buy_score = 45
                    sell_score = 15
                    confidence = 35

                    entry_quality = "WAIT"

                    reasons.append("Price near resistance, waiting bullish BOS")

            else:

                plan_type = "WAIT FOR BOS"
                entry_timing = "WAIT"

            plan_side = "WAIT"

    # =========================
    # CONTROLLED SMC BREAKOUT ENTRY
    # Retest remains preferred. This only opens a clean displacement BOS
    # when price does not give a retest.
    # =========================
    stretch_ema9 = get_pair_strategy_rule(symbol, "breakout_stretch_ema9")
    stretch_ema21 = get_pair_strategy_rule(symbol, "breakout_stretch_ema21")

    provisional_buy_score = max(
        buy_score,
        62
        + (10 if bullish_bos or bullish_choch else 0)
        + (8 if bullish_displacement else 0)
        + (6 if recent_pressure == "BULLISH" else 0)
        + (4 if bullish_fvg else 0)
    )
    provisional_sell_score = max(
        sell_score,
        62
        + (10 if bearish_bos or bearish_choch else 0)
        + (8 if bearish_displacement else 0)
        + (6 if recent_pressure == "BEARISH" else 0)
        + (4 if bearish_fvg else 0)
    )
    provisional_buy_confidence = max(
        confidence,
        calculate_smart_confidence(
            session_active,
            bullish_fvg,
            bearish_fvg,
            bullish_displacement,
            bearish_displacement,
            fake_breakout,
            mtf_bias,
            "BUY",
        ) if bullish_bos or bullish_choch else confidence
    )
    provisional_sell_confidence = max(
        confidence,
        calculate_smart_confidence(
            session_active,
            bullish_fvg,
            bearish_fvg,
            bullish_displacement,
            bearish_displacement,
            fake_breakout,
            mtf_bias,
            "SELL",
        ) if bearish_bos or bearish_choch else confidence
    )

    # =========================
    # MOMENTUM BREAKOUT ENTRY
    # Retest remains preferred. This second path handles clean, high-score
    # breaks that continue without returning to the setup level.
    # =========================
    momentum_stretched_ok = (
        abs(c1 - ema9) <= stretch_ema9
        and abs(c1 - ema21) <= stretch_ema21
    )
    momentum_buy_close_break_ok = c1 > recent_high
    momentum_sell_close_break_ok = c1 < recent_low
    momentum_buy_allowed = (
        final_signal == "WAIT"
        and momentum_buy_close_break_ok
        and recent_pressure == "BULLISH"
        and momentum_strength in ["MEDIUM", "STRONG"]
        and provisional_buy_score >= SIMPLE_MOMENTUM_MIN_SCORE
        and provisional_buy_confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
        and fake_breakout == "NONE"
    )
    momentum_sell_allowed = (
        final_signal == "WAIT"
        and momentum_sell_close_break_ok
        and recent_pressure == "BEARISH"
        and momentum_strength in ["MEDIUM", "STRONG"]
        and provisional_sell_score >= SIMPLE_MOMENTUM_MIN_SCORE
        and provisional_sell_confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
        and fake_breakout == "NONE"
    )

    if momentum_buy_allowed:
        final_signal = "BUY"
        buy_score = min(95, provisional_buy_score)
        sell_score = max(5, min(sell_score, 25))
        confidence = min(95, provisional_buy_confidence)
        strategy_setup_complete = True
        strategy_setup_type = "BUY_MOMENTUM_BREAKOUT"
        plan_type = "SMC BUY MOMENTUM BREAKOUT"
        entry_timing = "MOMENTUM BREAKOUT BUY"
        plan_side = "BUY"
        entry_quality = "SMC"
        breakout_entry_allowed = True
        smc_branch = "SMC_BUY_MOMENTUM_BREAKOUT"
        breakout_plan_reason = "BUY momentum breakout accepted: retest not required"
        state["pending"] = None
        state["stage"] = "BUY_ACTIVE"
        state["last_idea"] = "BUY"
        reasons.append(breakout_plan_reason)

    elif momentum_sell_allowed:
        final_signal = "SELL"
        buy_score = max(5, min(buy_score, 25))
        sell_score = min(95, provisional_sell_score)
        confidence = min(95, provisional_sell_confidence)
        strategy_setup_complete = True
        strategy_setup_type = "SELL_MOMENTUM_BREAKOUT"
        plan_type = "SMC SELL MOMENTUM BREAKOUT"
        entry_timing = "MOMENTUM BREAKOUT SELL"
        plan_side = "SELL"
        entry_quality = "SMC"
        breakout_entry_allowed = True
        smc_branch = "SMC_SELL_MOMENTUM_BREAKOUT"
        breakout_plan_reason = "SELL momentum breakout accepted: retest not required"
        state["pending"] = None
        state["stage"] = "SELL_ACTIVE"
        state["last_idea"] = "SELL"
        reasons.append(breakout_plan_reason)

    momentum_breakout_debug = [
        {
            "symbol": symbol,
            "side": "BUY",
            "bullish_bos": bullish_bos,
            "bearish_bos": bearish_bos,
            "bullish_choch": bullish_choch,
            "bearish_choch": bearish_choch,
            "recent_pressure": recent_pressure,
            "momentum_strength": momentum_strength,
            "buy_score": provisional_buy_score,
            "sell_score": provisional_sell_score,
            "confidence": provisional_buy_confidence,
            "displacement_score": displacement_score,
            "fake_breakout": fake_breakout,
            "close_break_ok": momentum_buy_close_break_ok,
            "stretched_ok": momentum_stretched_ok,
            "allowed": momentum_buy_allowed,
            "final_signal": final_signal,
        },
        {
            "symbol": symbol,
            "side": "SELL",
            "bullish_bos": bullish_bos,
            "bearish_bos": bearish_bos,
            "bullish_choch": bullish_choch,
            "bearish_choch": bearish_choch,
            "recent_pressure": recent_pressure,
            "momentum_strength": momentum_strength,
            "buy_score": provisional_buy_score,
            "sell_score": provisional_sell_score,
            "confidence": provisional_sell_confidence,
            "displacement_score": displacement_score,
            "fake_breakout": fake_breakout,
            "close_break_ok": momentum_sell_close_break_ok,
            "stretched_ok": momentum_stretched_ok,
            "allowed": momentum_sell_allowed,
            "final_signal": final_signal,
        },
    ]

    for momentum_debug in momentum_breakout_debug:
        print("MOMENTUM_BREAKOUT_ENTRY_DEBUG", momentum_debug)

    buy_bos_level = state.get("bos_level") if state.get("pending") == "BUY" else recent_high
    sell_bos_level = state.get("bos_level") if state.get("pending") == "SELL" else recent_low

    buy_score_ok = provisional_buy_score >= SIMPLE_MOMENTUM_MIN_SCORE
    sell_score_ok = provisional_sell_score >= SIMPLE_MOMENTUM_MIN_SCORE
    buy_confidence_ok = provisional_buy_confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
    sell_confidence_ok = provisional_sell_confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
    buy_displacement_ok = bullish_displacement or displacement_score >= 4
    sell_displacement_ok = bearish_displacement or displacement_score >= 4
    buy_pressure_ok = recent_pressure == "BULLISH"
    sell_pressure_ok = recent_pressure == "BEARISH"
    fake_breakout_ok = fake_breakout == "NONE"
    buy_close_break_ok = (
        bullish_bos
        and c1 > recent_high
        and (buy_bos_level is None or c1 > buy_bos_level)
    )
    sell_close_break_ok = (
        bearish_bos
        and c1 < recent_low
        and (sell_bos_level is None or c1 < sell_bos_level)
    )
    stretched_ok = (
        abs(c1 - ema9) <= stretch_ema9
        and abs(c1 - ema21) <= stretch_ema21
    )

    buy_breakout_allowed = (
        final_signal == "WAIT"
        and bullish_bos
        and buy_score_ok
        and buy_confidence_ok
        and buy_displacement_ok
        and buy_pressure_ok
        and fake_breakout_ok
        and buy_close_break_ok
    )
    sell_breakout_allowed = (
        final_signal == "WAIT"
        and bearish_bos
        and sell_score_ok
        and sell_confidence_ok
        and sell_displacement_ok
        and sell_pressure_ok
        and fake_breakout_ok
        and sell_close_break_ok
    )

    if buy_breakout_allowed:
        final_signal = "BUY"
        buy_score = min(92, provisional_buy_score)
        sell_score = max(5, min(sell_score, 25))
        confidence = min(88, provisional_buy_confidence)
        plan_type = "SMC BUY BREAKOUT"
        entry_timing = "BOS BUY BREAKOUT"
        plan_side = "BUY"
        entry_quality = "SMC"
        breakout_entry_allowed = True
        breakout_plan_reason = "BUY after BOS breakout with strong displacement; retest not required"
        smc_branch = "SMC_BUY_BREAKOUT"
        state["pending"] = None
        state["stage"] = "BUY_ACTIVE"
        state["last_idea"] = "BUY"
        strategy_setup_complete = True
        strategy_setup_type = "BUY_BREAKOUT"
        reasons.append(breakout_plan_reason)

    elif sell_breakout_allowed:
        final_signal = "SELL"
        buy_score = max(5, min(buy_score, 25))
        sell_score = min(92, provisional_sell_score)
        confidence = min(88, provisional_sell_confidence)
        plan_type = "SMC SELL BREAKOUT"
        entry_timing = "BOS SELL BREAKOUT"
        plan_side = "SELL"
        entry_quality = "SMC"
        breakout_entry_allowed = True
        breakout_plan_reason = "SELL after BOS breakout with strong displacement; retest not required"
        smc_branch = "SMC_SELL_BREAKOUT"
        state["pending"] = None
        state["stage"] = "SELL_ACTIVE"
        state["last_idea"] = "SELL"
        strategy_setup_complete = True
        strategy_setup_type = "SELL_BREAKOUT"
        reasons.append(breakout_plan_reason)

    smc_breakout_debug = [
        {
            "symbol": symbol,
            "side": "BUY",
            "bos_confirmed": bullish_bos,
            "score_ok": buy_score_ok,
            "confidence_ok": buy_confidence_ok,
            "displacement_ok": buy_displacement_ok,
            "pressure_ok": buy_pressure_ok,
            "fake_breakout": fake_breakout,
            "close_break_ok": buy_close_break_ok,
            "stretched_ok": stretched_ok,
            "breakout_entry_allowed": buy_breakout_allowed,
            "final_signal": final_signal,
        },
        {
            "symbol": symbol,
            "side": "SELL",
            "bos_confirmed": bearish_bos,
            "score_ok": sell_score_ok,
            "confidence_ok": sell_confidence_ok,
            "displacement_ok": sell_displacement_ok,
            "pressure_ok": sell_pressure_ok,
            "fake_breakout": fake_breakout,
            "close_break_ok": sell_close_break_ok,
            "stretched_ok": stretched_ok,
            "breakout_entry_allowed": sell_breakout_allowed,
            "final_signal": final_signal,
        },
    ]

    for breakout_debug in smc_breakout_debug:
        print("SMC_BREAKOUT_ENTRY_DEBUG =", breakout_debug)

    # =========================
    # EXIT IDEA LOGIC
    # =========================
    if final_signal == "WAIT" and state.get("last_idea") == "SELL" and (bullish_choch or bullish_bos):
        final_signal = "EXIT SELL"
        buy_score = 70
        sell_score = 20
        confidence = 85
        entry_quality = "EXIT"
        entry_timing = "EXIT SELL"
        plan_type = "EXIT SELL"
        plan_side = "EXIT"
        state["last_idea"] = None

    elif final_signal == "WAIT" and state.get("last_idea") == "BUY" and (bearish_choch or bearish_bos):
        final_signal = "EXIT BUY"
        buy_score = 20
        sell_score = 70
        confidence = 85
        entry_quality = "EXIT"
        entry_timing = "EXIT BUY"
        plan_type = "EXIT BUY"
        plan_side = "EXIT"
        state["last_idea"] = None

    if final_signal in ["BUY", "SELL"] and not strategy_setup_complete:
        simple_momentum_completion = (
            SIMPLE_MOMENTUM_MODE
            and (
                (
                    final_signal == "BUY"
                    and recent_pressure == "BULLISH"
                    and momentum_strength in ["MEDIUM", "STRONG"]
                    and buy_score >= SIMPLE_MOMENTUM_MIN_SCORE
                    and confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
                )
                or
                (
                    final_signal == "SELL"
                    and recent_pressure == "BEARISH"
                    and momentum_strength in ["MEDIUM", "STRONG"]
                    and sell_score >= SIMPLE_MOMENTUM_MIN_SCORE
                    and confidence >= SIMPLE_MOMENTUM_MIN_CONFIDENCE
                )
            )
        )

        if simple_momentum_completion:
            strategy_setup_complete = True
            strategy_setup_type = f"{final_signal}_SIMPLE_MOMENTUM"
            entry_quality = "SIMPLE_MOMENTUM"
            entry_timing = f"SIMPLE MOMENTUM {final_signal}"
            plan_type = f"SIMPLE MOMENTUM {final_signal}"
            plan_side = final_signal
            reasons.append("Simple Momentum Mode: completed without requiring perfect SMC retest")
        else:
            blocked_side = final_signal
            final_signal = "WAIT"
            plan_side = "WAIT"
            entry_quality = "WAIT"
            entry_timing = (
                "WAIT RETEST OR STRONG BREAKOUT"
                if blocked_side == "BUY"
                else "WAIT RETEST OR STRONG BREAKOUT"
            )
            plan_type = (
                "WAIT BUY SETUP COMPLETION"
                if blocked_side == "BUY"
                else "WAIT SELL SETUP COMPLETION"
            )
            reasons.append(
                "Strategy incomplete: waiting for completed retest entry or strong breakout setup"
            )

    scoring_context = {
        "symbol": symbol,
        "final_signal": final_signal,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "confidence": confidence,
        "htf_structure": htf_structure,
        "market_state": market_state,
        "c1": c1,
        "ema9": ema9,
        "ema21": ema21,
        "bearish_choch": bearish_choch,
        "bearish_bos": bearish_bos,
        "bearish_displacement": bearish_displacement,
        "bearish_fvg": bearish_fvg,
        "recent_pressure": recent_pressure,
        "bullish_displacement": bullish_displacement,
        "bullish_choch": bullish_choch,
        "bullish_bos": bullish_bos,
        "entry_timing": entry_timing,
        "zone_position": zone_position,
        "market_condition": market_condition,
        "choch_signal": choch_signal,
        "reasons": reasons,
    }
    corrected_scoring_context = call_pair_strategy_hook(
        symbol,
        "apply_scoring_correction",
        scoring_context,
    )
    if corrected_scoring_context:
        buy_score = corrected_scoring_context["buy_score"]
        sell_score = corrected_scoring_context["sell_score"]
        confidence = corrected_scoring_context["confidence"]

    if final_signal == "BUY" and swing_low:
        entry_price = round(c1, decimals)
        stop_loss = round(swing_low - buffer, decimals)
        risk = entry_price - stop_loss
        tp2 = round(entry_price + risk * 2.0, decimals)
        tp1 = round(
            entry_price + ((tp2 - entry_price) * get_tp1_ratio_of_tp2()),
            decimals,
        )
        invalidation = "Exit if bearish CHOCH or break below SL"
        plan_reason = breakout_plan_reason or "BUY after 15m swing break and close confirmation"

    elif final_signal == "SELL" and swing_high:
        entry_price = round(c1, decimals)
        stop_loss = round(swing_high + buffer, decimals)
        risk = stop_loss - entry_price
        tp2 = round(entry_price - risk * 2.0, decimals)
        tp1 = round(
            entry_price - ((entry_price - tp2) * get_tp1_ratio_of_tp2()),
            decimals,
        )
        invalidation = "Exit if bullish CHOCH or break above SL"
        plan_reason = breakout_plan_reason or "SELL after 15m swing break and close confirmation"

    else:
        entry_price = "--"
        stop_loss = "--"
        tp1 = "--"
        tp2 = "--"
        invalidation = "Wait for 15m swing break + close"
        plan_reason = "No entry until 15m structure confirms"

    signal_before_filters = final_signal
    no_trade = False
    kill_trade = False

    # =========================
    # ✅ FINAL TRADE GATE — SOFTER SCALPER VERSION
    # =========================
    allow_trade = True

    # Confidence was too strict at 80. Scalper can start at 65.
    if confidence < 65:
        if breakout_entry_allowed and confidence >= 55:
            reasons.append("SMC breakout confidence allowed at 55+")
        else:
            allow_trade = False
            reasons.append("Blocked: confidence below 65")

    # Fake breakout still blocks trade
    if fake_breakout != "NONE":
        allow_trade = False
        reasons.append("Blocked: fake breakout")

    # Displacement should warn, not always block
    if final_signal == "BUY" and not bullish_displacement:
        if breakout_entry_allowed and displacement_score >= 4:
            reasons.append("SMC breakout allowed by displacement score")
        elif confidence < 75:
            allow_trade = False
            reasons.append("Blocked: weak bullish displacement under 75 confidence")
        else:
            reasons.append("Warning: weak bullish displacement allowed by confidence")

    if final_signal == "SELL" and not bearish_displacement:
        if breakout_entry_allowed and displacement_score >= 4:
            reasons.append("SMC breakout allowed by displacement score")
        elif confidence < 75:
            allow_trade = False
            reasons.append("Blocked: weak bearish displacement under 75 confidence")
        else:
            reasons.append("Warning: weak bearish displacement allowed by confidence")
            
    # Inactive session should not fully kill SMC trades
    if not session_active:
        confidence = max(1, confidence - 8)
        reasons.append(f"Session inactive: confidence reduced ({session_name})")

    # MTF conflict should block only weak trades, not all trades
    if final_signal == "BUY" and mtf_bias == "BEARISH":
        if confidence < 78 and not bullish_bos and not bullish_choch:
            allow_trade = False
            reasons.append("Blocked: bearish MTF conflict")
        else:
            reasons.append("MTF bearish conflict allowed by strong BUY structure")

    if final_signal == "SELL" and mtf_bias == "BULLISH":
        if confidence < 78 and not bearish_bos and not bearish_choch:
            allow_trade = False
            reasons.append("Blocked: bullish MTF conflict")
        else:
            reasons.append("MTF bullish conflict allowed by strong SELL structure")

    if not allow_trade and final_signal in ["BUY", "SELL"]:
        final_signal = "WAIT"
        plan_side = "WAIT"
        entry_timing = "NO TRADE"

    call_pair_strategy_hook(
        symbol,
        "apply_debug_after_trade_gate",
        {
            "score_contribution_debug": score_contribution_debug,
            "htf_structure": htf_structure,
            "choch_signal": choch_signal,
            "bearish_fvg": bearish_fvg,
            "bullish_fvg": bullish_fvg,
            "bearish_bos": bearish_bos,
            "bullish_choch": bullish_choch,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "confidence": confidence,
            "final_signal": final_signal,
            "allow_trade": allow_trade,
        },
    )

    if final_signal == "BUY":
        signal_text = f"BUY 🟢 ({confidence}% | {entry_timing})"
    elif final_signal == "SELL":
        signal_text = f"SELL 🔴 ({confidence}% | {entry_timing})"
    else:
        signal_text = f"WAIT ⚪ ({entry_timing})"

    structure_type = (
        "CHOCH BUY" if bullish_choch
        else "CHOCH SELL" if bearish_choch
        else "BOS BUY" if bullish_bos
        else "BOS SELL" if bearish_bos
        else "NEUTRAL"
    )
    bos_signal = (
        "BULLISH" if bullish_bos
        else "BEARISH" if bearish_bos
        else "NONE"
    )
    score_gap = abs(buy_score - sell_score)
    blocked_by = None
    blocked_reason = None
    blocker_rule_name = None

    if final_signal == "WAIT":
        blocked_reasons = [
            reason for reason in reasons
            if str(reason).startswith("Blocked:")
        ]

        if signal_before_filters in ["BUY", "SELL"] and not allow_trade:
            blocked_by = "final_trade_gate"
            blocked_reason = blocked_reasons[-1] if blocked_reasons else "Final trade gate blocked signal"
            blocked_text = str(blocked_reason).lower()
            if "bearish displacement" in blocked_text:
                blocker_rule_name = "bearish_displacement"
            elif "confidence below 65" in blocked_text:
                blocker_rule_name = "confidence_under_65"
            elif "under 75 confidence" in blocked_text:
                blocker_rule_name = "confidence_under_75"
            else:
                blocker_rule_name = "final_trade_gate"
        elif "RETEST" in str(entry_timing).upper() or "RETEST" in str(plan_type).upper():
            blocked_by = "missing_15m_swing_break"
            blocked_reason = "Waiting for fresh 15m swing break and close"
            blocker_rule_name = "fifteen_m_closed_swing_break_required"
        elif "PULLBACK" in str(entry_timing).upper() or "LATE" in str(entry_quality).upper():
            blocked_by = "late_entry"
            blocked_reason = reasons[-1] if reasons else plan_reason
            blocker_rule_name = "late_entry_or_pullback"
        elif "CONFLICT" in str(entry_quality).upper() or "CONFLICT" in str(plan_type).upper():
            blocked_by = "htf_conflict"
            blocked_reason = reasons[-1] if reasons else plan_reason
            blocker_rule_name = "mtf_conflict"
        elif fake_breakout != "NONE":
            blocked_by = "fake_breakout"
            blocked_reason = fake_breakout
            blocker_rule_name = "fake_breakout"
        elif not session_active and "SESSION" in str(entry_timing).upper():
            blocked_by = "session_filter"
            blocked_reason = f"Session inactive: {session_name}"
            blocker_rule_name = "session_inactive"
        elif confidence < 65:
            blocked_by = "confidence_filter"
            blocked_reason = "Blocked: confidence below 65"
            blocker_rule_name = "confidence_below_65"
        else:
            blocked_by = "structure_wait"
            blocked_reason = plan_reason or (reasons[-1] if reasons else "Waiting for structure confirmation")
            blocker_rule_name = "structure_confirmation_required"

        print(f"BLOCKED BY: {blocker_rule_name}")

    print("FINAL_SIGNAL_DECISION_DEBUG =", {
        "symbol": symbol,
        "timeframe": "15m",
        "signal_before_filters": signal_before_filters,
        "final_signal": final_signal,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "confidence": confidence,
        "score_gap": score_gap,
        "plan_type": plan_type,
        "structure_type": structure_type,
        "htf_structure": htf_structure,
        "choch_signal": choch_signal,
        "bos_signal": bos_signal,
        "entry_timing": entry_timing,
        "market_condition": market_condition,
        "session_active": session_active,
        "fake_breakout": fake_breakout,
        "no_trade": no_trade,
        "kill_trade": kill_trade,
        "blocked_by": blocked_by,
        "blocked_reason": blocked_reason,
        "blocker_rule_name": blocker_rule_name,
    })

    trend_score = mtf_score
    choch_score = 0

    if bullish_choch:
        choch_score = 1
    elif bearish_choch:
        choch_score = -1

    bos_score = 0

    if bullish_bos:
        bos_score = 1
    elif bearish_bos:
        bos_score = -1

    final_score = max(buy_score, sell_score, confidence)

    try:
        data_last_time = pd.Timestamp(data.index[-1]).isoformat()
    except Exception:
        data_last_time = None

    try:
        htf_last_time = pd.Timestamp(htf_data.index[-1]).isoformat()
    except Exception:
        htf_last_time = None

    print("SYMBOL_SCORE_DEBUG =", {
        "symbol": symbol,
        "state_key": state_key or symbol,
        "buy_percent": buy_score,
        "sell_percent": sell_score,
        "confidence": confidence,
        "trend_score": trend_score,
        "choch_score": choch_score,
        "bos_score": bos_score,
        "displacement_score": displacement_score,
        "final_score": final_score,
        "data_rows": len(data) if data is not None else 0,
        "htf_rows": len(htf_data) if htf_data is not None else 0,
        "data_last_time": data_last_time,
        "htf_last_time": htf_last_time,
        "last_close": round(c1, decimals),
        "recent_high": round(recent_high, decimals),
        "recent_low": round(recent_low, decimals),
        "fifteen_m_pattern": fifteen_m_pattern,
        "pending_setup": state.get("pending"),
        "setup_candle_time": state.get("setup_candle_time"),
        "entry_confirm_level": state.get("entry_confirm_level"),
        "retest_seen": state.get("retest_seen"),
        "strategy_setup_complete": strategy_setup_complete,
        "strategy_setup_type": strategy_setup_type,
        "htf_structure": htf_structure,
        "structure_type": structure_type,
        "plan_type": plan_type,
        "final_signal": final_signal,
        "smc_branch": smc_branch,
        "dynamic_score_components": dynamic_score_components,
        "hardcoded_score_removed": hardcoded_score_removed,
        "score_contribution_debug": score_contribution_debug,
        "smc_breakout_debug": smc_breakout_debug,
        "momentum_breakout_debug": momentum_breakout_debug,
    })

    result = {
        "signal": final_signal,
        "signal_text": signal_text,
        "buy_pct": buy_score,
        "sell_pct": sell_score,
        "confidence": confidence,
        "market_condition": "STRUCTURE",
        "entry_quality": entry_quality,
        "entry_timing": entry_timing,
        "fake_kill": False,
        "debug_reasons": reasons[-14:],
        "price": round(c1, decimals),
        "plan_type": plan_type,
        "plan_bias": plan_side,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
       "risk_reward": "1:2" if final_signal in ["BUY", "SELL"] else "--",
        "invalidation": invalidation,
        "plan_reason": plan_reason,
        "blocked_by": blocked_by,
        "blocked_reason": blocked_reason,
        "signal_before_filters": signal_before_filters,
        "blocker_rule_name": blocker_rule_name,
        "structure_trend": htf_structure,
        "structure_type": structure_type,
        "structure_next": plan_type,
        "structure_resistance": round(recent_high, decimals),
        "structure_support": round(recent_low, decimals),
        "fifteen_m_pattern": fifteen_m_pattern,
        "entry_confirm_level": state.get("entry_confirm_level"),
        "retest_seen": state.get("retest_seen"),
        "strategy_setup_complete": strategy_setup_complete,
        "strategy_setup_type": strategy_setup_type,
        "smc_swings": smc_swings,
        "equal_highs": liquidity_levels["equal_highs"],
        "equal_lows": liquidity_levels["equal_lows"],
        "fvg": fvg,
        "session": session_name,
        "session_active": session_active,
        "displacement": displacement,
        "displacement_score": displacement_score,
        "fake_breakout": fake_breakout,
        "mtf_bias": mtf_bias,
        "mtf_score": mtf_score,
        "smc_branch": smc_branch,
        "dynamic_score_components": dynamic_score_components,
        "hardcoded_score_removed": hardcoded_score_removed,
        "score_contribution_debug": score_contribution_debug,
        "smc_breakout_debug": smc_breakout_debug,
        "momentum_breakout_debug": momentum_breakout_debug,
    }

    return result

def detect_1h_trend_filter(data_1h, symbol):
    if data_1h is None or data_1h.empty or len(data_1h) < 30:
        return {
            "bias": "NEUTRAL",
            "shift": "NONE",
            "buy_allowed": True,
            "sell_allowed": True,
            "reason": "1h data unavailable"
        }

    close = data_1h["Close"]
    ema9 = close.ewm(span=9).mean().iloc[-1].item()
    ema21 = close.ewm(span=21).mean().iloc[-1].item()
    ema50 = close.ewm(span=50).mean().iloc[-1].item()
    structure = detect_htf_structure(data_1h)
    pressure, pressure_score = detect_recent_pressure(data_1h, symbol)
    momentum, momentum_score = detect_momentum_strength(data_1h, symbol)

    if ema9 > ema21 > ema50 or structure == "BULLISH":
        bias = "BULLISH"
    elif ema9 < ema21 < ema50 or structure == "BEARISH":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    bullish_shift = (
        pressure == "BULLISH"
        and momentum in ["MEDIUM", "STRONG"]
        and close.iloc[-1].item() > ema21
    )
    bearish_shift = (
        pressure == "BEARISH"
        and momentum in ["MEDIUM", "STRONG"]
        and close.iloc[-1].item() < ema21
    )

    shift = "BULLISH" if bullish_shift else "BEARISH" if bearish_shift else "NONE"

    return {
        "bias": bias,
        "shift": shift,
        "buy_allowed": bias in ["BULLISH", "NEUTRAL"] or bullish_shift,
        "sell_allowed": bias in ["BEARISH", "NEUTRAL"] or bearish_shift,
        "structure": structure,
        "pressure": pressure,
        "pressure_score": pressure_score,
        "momentum": momentum,
        "momentum_score": momentum_score,
    }

def detect_lightweight_5m_confirmation(data_5m, htf_data, symbol):
    if data_5m is None or data_5m.empty or len(data_5m) < 30:
        return "WAIT"

    close = data_5m["Close"]
    open_ = data_5m["Open"]
    high = data_5m["High"]
    low = data_5m["Low"]
    c1 = close.iloc[-1].item()
    o1 = open_.iloc[-1].item()
    h1 = high.iloc[-1].item()
    l1 = low.iloc[-1].item()

    try:
        _swing_high, _swing_low, prev_swing_high, prev_swing_low, _structure, _swings = detect_swing_liquidity(data_5m)
        recent_high = prev_swing_high if prev_swing_high is not None else high.iloc[-20:-1].max().item()
        recent_low = prev_swing_low if prev_swing_low is not None else low.iloc[-20:-1].min().item()
    except Exception:
        recent_high = high.iloc[-20:-1].max().item()
        recent_low = low.iloc[-20:-1].min().item()

    if h1 > recent_high and c1 > o1:
        return "BUY"

    if l1 < recent_low and c1 < o1:
        return "SELL"

    return "WAIT"

def remove_current_forming_candle(data, timeframe_minutes):
    if data is None or data.empty:
        return data

    try:
        last_time = pd.Timestamp(data.index[-1])

        if last_time.tzinfo is None:
            last_time = last_time.tz_localize("UTC")
        else:
            last_time = last_time.tz_convert("UTC")

        now_utc = datetime.now(timezone.utc)
        current_bucket_minute = (now_utc.minute // timeframe_minutes) * timeframe_minutes
        current_bucket = now_utc.replace(
            minute=current_bucket_minute,
            second=0,
            microsecond=0
        )

        if last_time >= current_bucket and len(data) >= 2:
            return data.iloc[:-1].copy()
    except Exception:
        pass

    return data.copy()

def calculate_tp1_from_tp2_price(entry, tp2, side):
    entry_value = float(entry)
    tp2_value = float(tp2)
    tp1_ratio = 0.80

    if str(side or "").upper() == "BUY":
        return entry_value + ((tp2_value - entry_value) * tp1_ratio)

    return entry_value - ((entry_value - tp2_value) * tp1_ratio)

def calculate_protected_sl_from_tp2_price(entry, tp2, side):
    entry_value = float(entry)
    tp2_value = float(tp2)

    if str(side or "").upper() == "BUY":
        return entry_value + ((tp2_value - entry_value) * 0.50)

    return entry_value - ((entry_value - tp2_value) * 0.50)

def get_strategy_pip_size(symbol):
    normalized_symbol = normalize_symbol(symbol)
    return get_pair_strategy_rule(normalized_symbol, "pip_size")

def get_strategy_decimals(symbol):
    normalized_symbol = normalize_symbol(symbol)
    return get_pair_strategy_rule(normalized_symbol, "decimals")

def build_15m_swing_risk_levels(
    closed_data_15m,
    entry,
    side,
    symbol,
    setup_candle_time=None,
):
    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    decimals = get_strategy_decimals(normalized_symbol)
    pip_size = get_strategy_pip_size(normalized_symbol)
    buffer_pips = 5
    buffer = pip_size * buffer_pips
    max_swing_age_candles = 20
    broker_min_distance = get_pair_strategy_rule(
        normalized_symbol,
        "broker_min_distance",
    )
    debug = {
        "ok": False,
        "reason": "WAIT_INVALID_SWING_SL",
        "reason_if_wait": "WAIT_INVALID_SWING_SL",
        "entry": entry,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "risk": None,
        "reward": None,
        "risk_reward": None,
        "risk_reward_ratio": None,
        "risk_dollars": None,
        "structure_reward_dollars": None,
        "minimum_required_reward": None,
        "maximum_allowed_reward": None,
        "minimum_reward_ratio": 1.2,
        "maximum_reward_ratio": 2.0,
        "sl_swing_used": None,
        "tp_structure_used": None,
        "tp_structure_source": None,
        "swing_low": None,
        "swing_high": None,
        "sl_buffer": round(buffer, decimals),
        "sl_buffer_pips": buffer_pips,
        "sl_source_swing": None,
        "sl_before_broker_adjustment": None,
        "final_sl": None,
        "sl_reason": None,
        "minimum_risk_pips": None,
        "selected_swing_sl": None,
        "selected_swing_time": None,
        "selected_swing_age_candles": None,
        "sl_source": None,
        "sl_distance": None,
        "sl_distance_pips": None,
        "sl_candles_searched": None,
        "sl_swing_candidates_found": 0,
        "sl_chosen_swing": None,
        "sl_failure_reason": None,
        "rejected_tp_candidates": [],
        "broker_min_distance": broker_min_distance,
        "rejected_swing_candidates": [],
        "swing_sl_validation": {
            "swing_found": "NO",
            "swing_price": None,
            "swing_time": None,
            "swing_type": None,
            "distance_from_entry_to_swing": None,
            "validation_rules": [],
            "failed_validation": None,
            "why_swing_sl_no": None,
        },
        "final_entry": None,
        "final_tp1": None,
        "final_tp2": None,
    }
    rejected_candidates = []

    def log_sl_selection():
        print("SWING_SL_SELECTION_DEBUG =", {
            "symbol": normalized_symbol,
            "direction": side,
            "entry": entry,
            "candles_searched": debug.get("sl_candles_searched"),
            "swing_candidates_found": debug.get("sl_swing_candidates_found"),
            "chosen_swing": debug.get("sl_chosen_swing"),
            "sl_price": debug.get("final_sl") or debug.get("stop_loss"),
            "sl_distance_pips": debug.get("sl_distance_pips"),
            "failure_reason": None if debug.get("ok") else (
                debug.get("sl_failure_reason")
                or debug.get("reason")
                or debug.get("reason_if_wait")
            ),
        })

    def log_swing_sl_validation():
        validation = debug.get("swing_sl_validation") or {}
        swing_sl_visible = bool(
            debug.get("selected_swing_sl")
            or debug.get("sl_source")
            or debug.get("sl_valid")
        )
        if not swing_sl_visible and not validation.get("why_swing_sl_no"):
            validation["why_swing_sl_no"] = (
                validation.get("failed_validation")
                or debug.get("reason")
                or debug.get("reason_if_wait")
                or "No selected swing SL debug field was produced"
            )
        debug["swing_sl_validation"] = validation
        print("SWING_SL_VALIDATION_DEBUG =", {
            "symbol": normalized_symbol,
            "side": side,
            "entry": entry,
            "swing_sl_visible": "YES" if swing_sl_visible else "NO",
            **validation,
        })

    def return_with_swing_log():
        debug["rejected_swing_candidates"] = rejected_candidates
        log_sl_selection()
        log_swing_sl_validation()
        return debug

    if side not in ["BUY", "SELL"] or closed_data_15m is None or len(closed_data_15m) < 10:
        debug["swing_sl_validation"]["failed_validation"] = "invalid input/data"
        debug["swing_sl_validation"]["why_swing_sl_no"] = (
            "Side must be BUY/SELL and at least 10 closed 15m candles are required"
        )
        return return_with_swing_log()

    try:
        entry_price = float(entry)
        structure_data = closed_data_15m.copy()

        if setup_candle_time:
            try:
                setup_ts = pd.Timestamp(setup_candle_time)
                if setup_ts.tzinfo is None:
                    setup_ts = setup_ts.tz_localize("UTC")
                else:
                    setup_ts = setup_ts.tz_convert("UTC")

                capped_rows = []
                for candle_time, candle in structure_data.iterrows():
                    candle_ts = pd.Timestamp(candle_time)
                    if candle_ts.tzinfo is None:
                        candle_ts = candle_ts.tz_localize("UTC")
                    else:
                        candle_ts = candle_ts.tz_convert("UTC")
                    if candle_ts <= setup_ts:
                        capped_rows.append((candle_time, candle))

                if capped_rows:
                    structure_data = pd.DataFrame(
                        [row for _, row in capped_rows],
                        index=[time for time, _ in capped_rows],
                    )
            except Exception:
                structure_data = closed_data_15m.copy()

        swing_source = structure_data.iloc[:-1].copy() if len(structure_data) > 1 else structure_data.copy()
        (
            swing_high,
            swing_low,
            _prev_high,
            _prev_low,
            _structure,
            _swings,
        ) = detect_swing_liquidity(swing_source)
        debug["swing_low"] = swing_low
        debug["swing_high"] = swing_high
        rejected_candidates = []
        last_swing_position = len(swing_source) - 1
        debug["sl_candles_searched"] = len(swing_source)

        def record_rule(name, passed, reason=None, candidate=None):
            rule = {
                "rule": name,
                "passed": bool(passed),
                "reason": reason,
            }
            if candidate:
                rule.update({
                    "candidate_price": candidate.get("price"),
                    "candidate_time": candidate.get("time"),
                    "candidate_type": candidate.get("type"),
                    "candidate_age_candles": candidate.get("age_candles"),
                })
            debug["swing_sl_validation"]["validation_rules"].append(rule)
            if not passed and not debug["swing_sl_validation"].get("failed_validation"):
                debug["swing_sl_validation"]["failed_validation"] = reason or name

        def numeric_candidate(value, source):
            try:
                candidate = float(value)
                if math.isfinite(candidate):
                    return {"price": candidate, "source": source}
            except (TypeError, ValueError):
                pass
            return None

        swing_candidates = []

        for swing in _swings or []:
            if not isinstance(swing, dict):
                continue
            swing_type = str(swing.get("type") or "").upper()
            if side == "BUY" and swing_type != "LOW":
                continue
            if side == "SELL" and swing_type != "HIGH":
                continue
            candidate = numeric_candidate(
                swing.get("price"),
                f"detected_15m_swing_{swing_type.lower()}",
            )
            if candidate:
                candidate["index"] = swing.get("index")
                candidate["time"] = swing.get("time")
                candidate["type"] = swing_type.lower()
                swing_candidates.append(candidate)

        if side == "BUY":
            candidate = numeric_candidate(swing_low, "detected_15m_swing_low")
            if candidate:
                candidate["index"] = None
                candidate["time"] = None
                candidate["type"] = "low"
                swing_candidates.append(candidate)
        else:
            candidate = numeric_candidate(swing_high, "detected_15m_swing_high")
            if candidate:
                candidate["index"] = None
                candidate["time"] = None
                candidate["type"] = "high"
                swing_candidates.append(candidate)

        seen_candidates = set()
        deduped_candidates = []
        for candidate in swing_candidates:
            key = round(candidate["price"], decimals)
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            deduped_candidates.append(candidate)
        swing_candidates = deduped_candidates

        def normalize_candidate_index(candidate):
            try:
                index_value = candidate.get("index")
                if index_value is None:
                    return None
                return int(index_value)
            except (TypeError, ValueError):
                return None

        def candidate_time(candidate, position):
            if candidate.get("time") is not None:
                return candidate.get("time")
            if position is None:
                return None
            try:
                return str(swing_source.index[position])
            except Exception:
                return None

        def enrich_candidate(candidate):
            enriched = dict(candidate)
            position = normalize_candidate_index(candidate)
            if position is not None and position < 0:
                position = None
            if position is not None and position >= len(swing_source):
                position = len(swing_source) - 1
            age = None
            if position is not None:
                age = max(0, last_swing_position - position)
            enriched["index"] = position
            enriched["time"] = candidate_time(candidate, position)
            enriched["age_candles"] = age
            return enriched

        swing_candidates = [enrich_candidate(candidate) for candidate in swing_candidates]
        debug["swing_sl_validation"]["swing_found"] = "YES" if swing_candidates else "NO"
        debug["sl_swing_candidates_found"] = len(swing_candidates)
        swing_candidates.sort(
            key=lambda item: (
                item["age_candles"] is None,
                item["age_candles"] if item["age_candles"] is not None else 9999,
            )
        )

        def mark_wait(reason):
            debug["reason"] = reason
            debug["reason_if_wait"] = reason
            debug["rejected_swing_candidates"] = rejected_candidates
            debug["sl_failure_reason"] = reason
            validation = debug["swing_sl_validation"]
            if not validation.get("failed_validation"):
                validation["failed_validation"] = reason
            validation["why_swing_sl_no"] = reason

        def mark_selected_swing(candidate):
            validation = debug["swing_sl_validation"]
            validation["swing_found"] = "YES"
            validation["swing_price"] = round(candidate["price"], decimals)
            validation["swing_time"] = candidate.get("time")
            validation["swing_type"] = candidate.get("type")
            validation["distance_from_entry_to_swing"] = round(
                abs(entry_price - candidate["price"]),
                decimals,
            )
            validation["failed_validation"] = None
            validation["why_swing_sl_no"] = None
            debug["sl_chosen_swing"] = {
                "price": round(candidate["price"], decimals),
                "time": candidate.get("time"),
                "type": candidate.get("type"),
                "source": candidate.get("source"),
                "age_candles": candidate.get("age_candles"),
            }

        def clear_selected_swing_sl():
            debug["selected_swing_sl"] = None
            debug["sl_source_swing"] = None
            debug["selected_swing_time"] = None
            debug["selected_swing_age_candles"] = None
            debug["sl_source"] = None
            debug["sl_reason"] = None
            debug["sl_chosen_swing"] = None

        def find_swing_meta_for_price(swing_type, price):
            try:
                price_value = float(price)
            except (TypeError, ValueError):
                return {}

            for swing in _swings or []:
                if not isinstance(swing, dict):
                    continue
                if str(swing.get("type") or "").upper() != swing_type:
                    continue
                try:
                    swing_price = float(swing.get("price"))
                except (TypeError, ValueError):
                    continue
                if abs(swing_price - price_value) <= max(pip_size * 0.1, 1e-10):
                    index_value = normalize_candidate_index(swing)
                    if index_value is not None and index_value >= len(swing_source):
                        index_value = len(swing_source) - 1
                    age = (
                        max(0, last_swing_position - index_value)
                        if index_value is not None
                        else None
                    )
                    return {
                        "index": index_value,
                        "time": swing.get("time") or candidate_time(swing, index_value),
                        "age_candles": age,
                    }

            return {}

        def add_rejected_candidate(candidate, reason):
            rejected_candidates.append({
                "price": (
                    round(candidate.get("price"), decimals)
                    if candidate and candidate.get("price") is not None
                    else None
                ),
                "time": candidate.get("time") if candidate else None,
                "type": candidate.get("type") if candidate else None,
                "source": candidate.get("source") if candidate else None,
                "age_candles": candidate.get("age_candles") if candidate else None,
                "reason": reason,
            })

        def candidate_stop_loss(candidate, candidate_side):
            if candidate_side == "BUY":
                return candidate["price"] - buffer
            return candidate["price"] + buffer

        def final_sl_valid(candidate, candidate_side):
            stop = candidate_stop_loss(candidate, candidate_side)
            distance = abs(stop - entry_price)
            min_distance = None
            try:
                min_distance = float(broker_min_distance)
            except (TypeError, ValueError):
                min_distance = None

            if candidate_side == "BUY":
                side_valid = stop < entry_price
            else:
                side_valid = stop > entry_price

            if not side_valid:
                return False, stop, (
                    "BUY buffered SL is not below entry"
                    if candidate_side == "BUY"
                    else "SELL buffered SL is not above entry"
                )

            if min_distance is not None and distance < min_distance:
                return False, stop, (
                    f"SL distance {round(distance, decimals)} is below "
                    f"minimum {round(min_distance, decimals)}"
                )

            return True, stop, None

        def select_sl_candidate(candidate_side):
            required_type = "low" if candidate_side == "BUY" else "high"
            strict_candidates = [
                candidate for candidate in swing_candidates
                if str(candidate.get("type") or "").lower() == required_type
            ]

            for candidate in strict_candidates:
                valid, stop, invalid_reason = final_sl_valid(candidate, candidate_side)
                if valid:
                    return candidate, stop, "strict"
                add_rejected_candidate(
                    candidate,
                    invalid_reason or "SL candidate invalid",
                )

            return None, None, None

        def build_target_candidates(target_side):
            target_type = "HIGH" if target_side == "BUY" else "LOW"
            target_source = (
                "detected_15m_swing_high"
                if target_side == "BUY"
                else "detected_15m_swing_low"
            )
            targets = []

            for swing in _swings or []:
                if not isinstance(swing, dict):
                    continue
                swing_type = str(swing.get("type") or "").upper()
                if swing_type != target_type:
                    continue
                candidate = numeric_candidate(swing.get("price"), target_source)
                if candidate:
                    candidate["index"] = swing.get("index")
                    candidate["time"] = swing.get("time")
                    targets.append(candidate)

            fallback_value = swing_high if target_side == "BUY" else swing_low
            candidate = numeric_candidate(fallback_value, target_source)
            if candidate:
                candidate["index"] = None
                candidate["time"] = None
                targets.append(candidate)

            seen_targets = set()
            deduped_targets = []
            for candidate in targets:
                key = round(candidate["price"], decimals)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                deduped_targets.append(candidate)

            enriched_targets = [enrich_candidate(candidate) for candidate in deduped_targets]
            if target_side == "BUY":
                valid_targets = [
                    candidate for candidate in enriched_targets
                    if candidate["price"] > entry_price
                ]
                valid_targets.sort(key=lambda item: item["price"])
            else:
                valid_targets = [
                    candidate for candidate in enriched_targets
                    if candidate["price"] < entry_price
                ]
                valid_targets.sort(key=lambda item: item["price"], reverse=True)

            return valid_targets

        if side == "BUY":
            selected, stop_loss, selection_source = select_sl_candidate("BUY")

            record_rule(
                "BUY previous swing low or fallback low must exist",
                selected is not None,
                None if selected is not None else "BUY previous swing low missing",
                selected,
            )

            if not selected:
                mark_wait("WAIT_VALID_SWING_SL")
                return return_with_swing_log()

            mark_selected_swing(selected)
            swing_low = selected["price"]
            debug["sl_source"] = selected["source"]
            debug["sl_reason"] = "BUY SL = last previous swing low - 5 pips"
            debug["selected_swing_sl"] = round(swing_low, decimals)
            debug["sl_source_swing"] = round(swing_low, decimals)
            debug["selected_swing_time"] = selected.get("time")
            debug["selected_swing_age_candles"] = selected.get("age_candles")
            debug["stop_loss"] = round(stop_loss, decimals)
            debug["sl_before_broker_adjustment"] = round(stop_loss, decimals)
            debug["final_sl"] = round(stop_loss, decimals)
            debug["sl_distance"] = round(abs(stop_loss - entry_price), decimals)
            debug["sl_distance_pips"] = round(abs(stop_loss - entry_price) / pip_size, 2)

            final_sl_below_entry = stop_loss < entry_price
            record_rule(
                "BUY final buffered SL must be below entry",
                final_sl_below_entry,
                None if final_sl_below_entry else "WAIT_BUY_SL_NOT_BELOW_ENTRY",
                selected,
            )
            if not final_sl_below_entry:
                clear_selected_swing_sl()
                mark_wait("WAIT_BUY_SL_NOT_BELOW_ENTRY")
                return return_with_swing_log()
            risk = entry_price - stop_loss
        else:
            selected, stop_loss, selection_source = select_sl_candidate("SELL")

            record_rule(
                "SELL previous swing high or fallback high must exist",
                selected is not None,
                None if selected is not None else "SELL previous swing high missing",
                selected,
            )

            if not selected:
                mark_wait("WAIT_VALID_SWING_SL")
                return return_with_swing_log()

            mark_selected_swing(selected)
            swing_high = selected["price"]
            debug["sl_source"] = selected["source"]
            debug["sl_reason"] = "SELL SL = last previous swing high + 5 pips"
            debug["selected_swing_sl"] = round(swing_high, decimals)
            debug["sl_source_swing"] = round(swing_high, decimals)
            debug["selected_swing_time"] = selected.get("time")
            debug["selected_swing_age_candles"] = selected.get("age_candles")
            debug["stop_loss"] = round(stop_loss, decimals)
            debug["sl_before_broker_adjustment"] = round(stop_loss, decimals)
            debug["final_sl"] = round(stop_loss, decimals)
            debug["sl_distance"] = round(abs(stop_loss - entry_price), decimals)
            debug["sl_distance_pips"] = round(abs(stop_loss - entry_price) / pip_size, 2)

            final_sl_above_entry = stop_loss > entry_price
            record_rule(
                "SELL final buffered SL must be above entry",
                final_sl_above_entry,
                None if final_sl_above_entry else "WAIT_SELL_SL_NOT_ABOVE_ENTRY",
                selected,
            )
            if not final_sl_above_entry:
                clear_selected_swing_sl()
                mark_wait("WAIT_SELL_SL_NOT_ABOVE_ENTRY")
                return return_with_swing_log()
            risk = stop_loss - entry_price

        positive_risk = risk > 0
        record_rule(
            "SL risk distance must be positive",
            positive_risk,
            None if positive_risk else "risk distance is not positive",
            selected,
        )
        if not positive_risk:
            debug["rejected_swing_candidates"] = rejected_candidates
            debug["swing_sl_validation"]["why_swing_sl_no"] = "risk distance is not positive"
            return return_with_swing_log()

        target_candidates = build_target_candidates(side)
        if not target_candidates:
            mark_wait("WAIT_VALID_STRUCTURE_TP")
            return return_with_swing_log()

        minimum_reward = risk * 1.2
        maximum_reward = risk * 2.0
        rejected_tp_candidates = []
        structure_target = None
        structure_reward = None
        structure_rr = None

        for candidate in target_candidates:
            candidate_reward = abs(candidate["price"] - entry_price)
            candidate_rr = candidate_reward / risk if risk > 0 else 0
            tp_candidate_log = {
                "symbol": normalized_symbol,
                "direction": side,
                "entry": round(entry_price, decimals),
                "sl": round(stop_loss, decimals),
                "risk": round(risk, decimals),
                "candidate_price": round(candidate["price"], decimals),
                "candidate_time": candidate.get("time"),
                "candidate_source": candidate.get("source"),
                "reward": round(candidate_reward, decimals),
                "rr": round(candidate_rr, 4),
                "required_rr_min": 1.2,
                "required_rr_max": 2.0,
            }
            if candidate_rr + 0.01 < 1.2:
                print("STRUCTURE_TP_CANDIDATE_DEBUG =", {
                    **tp_candidate_log,
                    "decision": "rejected",
                    "reason": "TP swing reward below 1.20R",
                })
                rejected_tp_candidates.append({
                    "price": round(candidate["price"], decimals),
                    "time": candidate.get("time"),
                    "source": candidate.get("source"),
                    "risk_reward_ratio": round(candidate_rr, 4),
                    "reason": "TP swing reward below 1.20R",
                })
                continue
            if candidate_rr - 0.01 > 2.0:
                print("STRUCTURE_TP_CANDIDATE_DEBUG =", {
                    **tp_candidate_log,
                    "decision": "rejected",
                    "reason": "TP swing reward above 2.00R",
                })
                rejected_tp_candidates.append({
                    "price": round(candidate["price"], decimals),
                    "time": candidate.get("time"),
                    "source": candidate.get("source"),
                    "risk_reward_ratio": round(candidate_rr, 4),
                    "reason": "TP swing reward above 2.00R",
                })
                continue

            print("STRUCTURE_TP_CANDIDATE_DEBUG =", {
                **tp_candidate_log,
                "decision": "accepted",
                "reason": "TP swing RR inside 1.20 to 2.00 window",
            })
            structure_target = candidate
            structure_reward = candidate_reward
            structure_rr = candidate_rr
            break

        debug["rejected_tp_candidates"] = rejected_tp_candidates
        if structure_target is None:
            debug["reason"] = "WAIT_NO_STRUCTURE_TP_IN_RR_WINDOW"
            debug["reason_if_wait"] = "WAIT_NO_STRUCTURE_TP_IN_RR_WINDOW"
            debug["rejection_reason"] = (
                f"Rejected {normalized_symbol} {side}: no 15m structure TP "
                "swing produced RR between 1.20 and 2.00."
            )
            print(debug["rejection_reason"])
            return return_with_swing_log()

        structure_rr = structure_reward / risk if risk > 0 else 0
        debug.update({
            "risk_reward_ratio": round(structure_rr, 4),
            "minimum_required_reward": round(minimum_reward, decimals),
            "maximum_allowed_reward": round(maximum_reward, decimals),
            "sl_swing_used": round(swing_low if side == "BUY" else swing_high, decimals),
            "tp_structure_used": round(structure_target["price"], decimals),
            "tp_structure_source": structure_target.get("source"),
        })

        tp2 = structure_target["price"]

        tp1 = calculate_tp1_from_tp2_price(entry_price, tp2, side)
        reward = abs(tp2 - entry_price)
        rr = reward / risk

        if rr + 0.01 < 1.2 or rr - 0.01 > 2.0:
            debug["reason"] = "WAIT_RR_OUTSIDE_1_2_TO_2"
            debug["swing_sl_validation"]["why_swing_sl_no"] = "TP reward ratio outside 1.2R-2R window"
            return return_with_swing_log()

        print("STRUCTURE_TP_RR_DEBUG =", {
            "symbol": normalized_symbol,
            "direction": side,
            "entry": round(entry_price, decimals),
            "sl": round(stop_loss, decimals),
            "sl_swing": round(swing_low if side == "BUY" else swing_high, decimals),
            "tp_swing": round(structure_target["price"], decimals),
            "tp_swing_time": structure_target.get("time"),
            "risk": round(risk, decimals),
            "reward": round(reward, decimals),
            "rr": round(rr, 4),
            "tp1": round(tp1, decimals),
            "tp2": round(tp2, decimals),
            "rejected_tp_candidates": rejected_tp_candidates,
        })

        levels = {
            "ok": True,
            "reason": None,
            "entry": round(entry_price, decimals),
            "stop_loss": round(stop_loss, decimals),
            "tp1": round(tp1, decimals),
            "tp2": round(tp2, decimals),
            "risk": round(risk, decimals),
            "reward": round(reward, decimals),
            "risk_reward": f"1:{round(rr, 2):g}",
            "risk_reward_ratio": round(rr, 4),
            "minimum_required_reward": round(minimum_reward, decimals),
            "maximum_allowed_reward": round(maximum_reward, decimals),
            "sl_swing_used": round(
                swing_low if side == "BUY" else swing_high,
                decimals,
            ),
            "tp_structure_used": round(structure_target["price"], decimals),
            "tp_structure_source": structure_target.get("source"),
            "tp_structure_time": structure_target.get("time"),
            "tp_structure_age_candles": structure_target.get("age_candles"),
            "structure_reward": round(structure_reward, decimals),
            "structure_reward_ratio": round(structure_rr, 4),
            "tp_capped_at_2r": False,
            "rejected_tp_candidates": rejected_tp_candidates,
            "level_source": debug.get("sl_source"),
            "selected_swing_sl": round(
                swing_low if side == "BUY" else swing_high,
                decimals,
            ),
            "sl_source_swing": round(
                swing_low if side == "BUY" else swing_high,
                decimals,
            ),
            "selected_swing_time": selected.get("time") if selected else None,
            "selected_swing_age_candles": selected.get("age_candles") if selected else None,
            "sl_source": debug.get("sl_source"),
            "sl_buffer": round(buffer, decimals),
            "sl_buffer_pips": buffer_pips,
            "sl_before_broker_adjustment": round(stop_loss, decimals),
            "final_sl": round(stop_loss, decimals),
            "sl_reason": debug.get("sl_reason"),
            "sl_distance": round(abs(stop_loss - entry_price), decimals),
            "sl_distance_pips": round(abs(stop_loss - entry_price) / pip_size, 2),
            "broker_min_distance": broker_min_distance,
            "rejected_swing_candidates": rejected_candidates,
            "final_entry": round(entry_price, decimals),
            "final_tp1": round(tp1, decimals),
            "final_tp2": round(tp2, decimals),
        }
        debug.update(levels)
    except Exception as exc:
        debug["reason"] = f"WAIT_INVALID_SWING_SL: {exc}"
        debug["swing_sl_validation"]["failed_validation"] = debug["reason"]
        debug["swing_sl_validation"]["why_swing_sl_no"] = debug["reason"]

    return return_with_swing_log()

def validate_trade_levels_1_to_2(result, side):
    side = str(side or "").upper()

    try:
        entry = float(result.get("entry_price"))
        sl = float(result.get("stop_loss"))
        tp1 = float(result.get("tp1"))
        tp2 = float(result.get("tp2"))
    except (TypeError, ValueError):
        return False, "WAIT_TP_LEVELS_MISSING"

    if side == "BUY":
        if not (sl < entry < tp1 < tp2):
            return False, "WAIT_TP_LEVELS_MISSING"
        risk = entry - sl
        reward = tp2 - entry
    elif side == "SELL":
        if not (tp2 < tp1 < entry < sl):
            return False, "WAIT_TP_LEVELS_MISSING"
        risk = sl - entry
        reward = entry - tp2
    else:
        return False, "WAIT_TP_LEVELS_MISSING"

    if risk <= 0 or reward <= 0:
        return False, "WAIT_TP_LEVELS_MISSING"

    expected_tp1 = calculate_tp1_from_tp2_price(entry, tp2, side)
    tp1_rounding_tolerance = (
        0.011
        if abs(entry) >= 100
        else max(abs(entry) * 1e-8, 1e-5)
    )

    if abs(tp1 - expected_tp1) > tp1_rounding_tolerance:
        return False, "WAIT_TP_LEVELS_MISSING"

    rr = reward / risk
    if rr + 0.01 < 1.2:
        return False, "WAIT_STRUCTURE_REWARD_BELOW_MINIMUM"

    if rr - 0.01 > 2.0:
        return False, "WAIT_RR_ABOVE_2R"

    return True, None

def get_15m_swing_watch_key(symbol, side):
    return f"{normalize_symbol(symbol)}:{str(side or '').upper()}"

def eurusd_requires_fresh_5m_entry(symbol):
    return normalize_symbol(symbol) == "EURUSD"

def clear_final_signal_hold_for_symbol(symbol, reason):
    hold_key = get_final_signal_hold_key(symbol)

    if hold_key not in FINAL_SIGNAL_HOLD:
        return False

    FINAL_SIGNAL_HOLD.pop(hold_key, None)
    save_final_signal_hold()
    print("FINAL_SIGNAL_HOLD_CLEARED =", {
        "symbol": hold_key,
        "reason": reason,
    })
    return True

def clear_eurusd_entry_memory(symbol, reason, side=None):
    if not eurusd_requires_fresh_5m_entry(symbol):
        return False

    changed = False

    if clear_final_signal_hold_for_symbol(symbol, reason):
        changed = True

    sides = [str(side).upper()] if str(side or "").upper() in ["BUY", "SELL"] else ["BUY", "SELL"]

    for watch_side in sides:
        watch_key = get_15m_swing_watch_key(symbol, watch_side)
        watched = FIFTEEN_M_SWING_WATCH.get(watch_key)

        if not isinstance(watched, dict):
            continue

        if str(watched.get("status") or "").upper() in ["EXPIRED", "INVALIDATED"]:
            continue

        watched["status"] = "EXPIRED"
        watched["expired_at"] = datetime.now(timezone.utc).isoformat()
        watched["expiration_reason"] = reason
        changed = True

    if changed:
        save_fifteen_m_swing_watch()

    print("EURUSD_ENTRY_MEMORY_CLEARED =", {
        "symbol": normalize_symbol(symbol),
        "side": side,
        "reason": reason,
        "changed": changed,
    })
    return changed

def get_15m_level_tolerance(symbol):
    normalized_symbol = normalize_symbol(symbol)
    return get_pair_strategy_rule(normalized_symbol, "fifteen_m_level_tolerance")


def get_default_15m_level_tolerance(symbol):
    normalized_symbol = normalize_symbol(symbol)
    if normalized_symbol == "XAUUSD":
        return 0.05
    return 0.00005


def resolve_15m_level_tolerance(symbol):
    normalized_symbol = normalize_symbol(symbol)
    raw_tolerance = get_15m_level_tolerance(normalized_symbol)
    default_tolerance = get_default_15m_level_tolerance(normalized_symbol)

    try:
        tolerance = float(raw_tolerance)
        if math.isfinite(tolerance):
            return tolerance, raw_tolerance, None
    except (TypeError, ValueError) as exc:
        return default_tolerance, raw_tolerance, str(exc)

    return (
        default_tolerance,
        raw_tolerance,
        f"non-finite tolerance {raw_tolerance}",
    )

def update_fifteen_m_swing_watch(symbol, side, swing_level, candle_time):
    side = str(side or "").upper()

    if side not in ["BUY", "SELL"] or swing_level is None:
        return None

    key = get_15m_swing_watch_key(symbol, side)
    previous = FIFTEEN_M_SWING_WATCH.get(key) or {}

    try:
        swing_value = float(swing_level)
    except (TypeError, ValueError):
        return previous or None

    previous_level = previous.get("swing_level")
    previous_status = str(previous.get("status") or "").upper()
    same_level = False

    try:
        same_level = abs(float(previous_level) - swing_value) <= 1e-8
    except (TypeError, ValueError):
        same_level = False

    if (
        previous
        and not previous.get("break_confirmed")
        and same_level
        and previous_status not in ["EXPIRED", "INVALIDATED"]
    ):
        return previous

    if not previous or not same_level or previous_status in ["EXPIRED", "INVALIDATED"]:
        FIFTEEN_M_SWING_WATCH[key] = {
            "symbol": normalize_symbol(symbol),
            "side": side,
            "swing_level": swing_value,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "source_candle_time": candle_time,
            "break_confirmed": False,
            "break_candle_time": None,
        }
        save_fifteen_m_swing_watch()

    return FIFTEEN_M_SWING_WATCH.get(key)

def evaluate_closed_15m_swing_break(closed_data_15m, side, symbol=None):
    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol) if symbol else "UNKNOWN"
    debug = {
        "symbol": normalized_symbol,
        "side": side,
        "confirmed": False,
        "closed_candle_time": None,
        "closed_candle_close": None,
        "swing_level": None,
        "comparison": None,
        "reason": "No actionable 15m side",
    }

    if side not in ["BUY", "SELL"]:
        return debug

    if closed_data_15m is None or len(closed_data_15m) < 10:
        debug["reason"] = "Not enough closed 15m candles to validate the swing break"
        return debug

    try:
        close_price = float(closed_data_15m["Close"].iloc[-1])
        previous_close = float(closed_data_15m["Close"].iloc[-2])
        candle_time = pd.Timestamp(closed_data_15m.index[-1]).isoformat()
        swing_source = closed_data_15m.iloc[:-1].copy()
        max_pending_5m_candles = get_15m_pending_max_5m_candles(
            normalized_symbol
        )

        if len(swing_source) < 8:
            swing_source = closed_data_15m.copy()

        (
            swing_high,
            swing_low,
            _prev_swing_high,
            _prev_swing_low,
            _structure,
            _swings,
        ) = detect_swing_liquidity(swing_source)
        actionable_swing_level, actionable_swing_debug = (
            get_actionable_15m_swing_level(
                swing_source,
                side,
                normalized_symbol,
                reference_price=previous_close,
            )
        )

        if actionable_swing_level is None:
            debug.update({
                "closed_candle_time": candle_time,
                "closed_candle_close": close_price,
                "swing_level": None,
                "comparison": None,
                "break_happened": False,
                "close_confirmed": False,
                "reason": (
                    actionable_swing_debug.get("reason")
                    or "No actionable 15m swing level within ATR range"
                ),
                "actionable_swing_debug": actionable_swing_debug,
            })
            return debug

        tolerance, raw_tolerance, tolerance_error = resolve_15m_level_tolerance(
            normalized_symbol
        )

        if side == "BUY":
            swing_level = actionable_swing_level
            trigger_level = float(swing_level) + tolerance

            break_happened = float(closed_data_15m["High"].iloc[-1]) > trigger_level
            close_confirmed = close_price > trigger_level
            confirmed = break_happened and close_confirmed
            comparison = "close > swing high + tolerance"
            if confirmed:
                reason = (
                    f"15m candle closed above swing high "
                    f"{float(swing_level):.5f} + tolerance {tolerance:g}"
                )
            elif break_happened:
                reason = "WAIT_NO_15M_CLOSE_CONFIRMATION"
            else:
                reason = "WAIT_NO_15M_BREAK"
        else:
            swing_level = actionable_swing_level
            trigger_level = float(swing_level) - tolerance

            break_happened = float(closed_data_15m["Low"].iloc[-1]) < trigger_level
            close_confirmed = close_price < trigger_level
            confirmed = break_happened and close_confirmed
            comparison = "close < swing low - tolerance"
            if confirmed:
                reason = (
                    f"15m candle closed below swing low "
                    f"{float(swing_level):.5f} - tolerance {tolerance:g}"
                )
            elif break_happened:
                reason = "WAIT_NO_15M_CLOSE_CONFIRMATION"
            else:
                reason = "WAIT_NO_15M_BREAK"

        watched = update_fifteen_m_swing_watch(
            normalized_symbol,
            side,
            swing_level,
            candle_time,
        )
        watched_level = None

        if watched:
            try:
                watched_level = float(watched.get("swing_level"))
            except (TypeError, ValueError):
                watched_level = None

        if watched_level is not None:
            if side == "BUY":
                trigger_level = watched_level + tolerance
                break_happened = float(closed_data_15m["High"].iloc[-1]) > trigger_level
                close_confirmed = close_price > trigger_level
                confirmed = break_happened and close_confirmed
                swing_level = watched_level
                comparison = "close > saved swing high + tolerance"
                if confirmed:
                    reason = (
                        f"15m candle closed above saved swing high "
                        f"{watched_level:.5f} + tolerance {tolerance:g}"
                    )
                elif break_happened:
                    reason = "WAIT_NO_15M_CLOSE_CONFIRMATION"
                else:
                    reason = "WAIT_NO_15M_BREAK"
            else:
                trigger_level = watched_level - tolerance
                break_happened = float(closed_data_15m["Low"].iloc[-1]) < trigger_level
                close_confirmed = close_price < trigger_level
                confirmed = break_happened and close_confirmed
                swing_level = watched_level
                comparison = "close < saved swing low - tolerance"
                if confirmed:
                    reason = (
                        f"15m candle closed below saved swing low "
                        f"{watched_level:.5f} - tolerance {tolerance:g}"
                    )
                elif break_happened:
                    reason = "WAIT_NO_15M_CLOSE_CONFIRMATION"
                else:
                    reason = "WAIT_NO_15M_BREAK"

        if confirmed and watched and not watched.get("break_confirmed"):
            key = get_15m_swing_watch_key(debug.get("symbol"), side)
            FIFTEEN_M_SWING_WATCH[key] = {
                **watched,
                "break_confirmed": True,
                "bos_time": candle_time,
                "bos_price": close_price,
                "broken_swing_price": swing_level,
                "setup_direction": side,
                "break_candle_time": candle_time,
                "break_close": close_price,
                "status": "PENDING",
                "expires_after_5m_candles": None,
                "expires_at": None,
                "invalidated_at": None,
                "confirmed_5m_time": None,
            }
            save_fifteen_m_swing_watch()

        print("BOS_CHOCH_15M_DEBUG =", {
            "symbol": normalized_symbol,
            "side": side,
            "swing_level": float(swing_level),
            "candle_time": candle_time,
            "candle_close": close_price,
            "tolerance": tolerance,
            "raw_tolerance": raw_tolerance,
            "tolerance_error": tolerance_error,
            "trigger_level": trigger_level,
            "break_happened": bool(break_happened),
            "close_confirmed": bool(close_confirmed),
            "confirmed": bool(confirmed),
            "reason": reason,
        })

        debug.update({
            "confirmed": confirmed,
            "closed_candle_time": candle_time,
            "closed_candle_close": close_price,
            "swing_level": float(swing_level),
            "comparison": comparison,
            "tolerance": tolerance,
            "raw_tolerance": raw_tolerance,
            "tolerance_error": tolerance_error,
            "trigger_level": trigger_level,
            "break_happened": bool(break_happened),
            "close_confirmed": bool(close_confirmed),
            "reason": reason,
            "pending_max_5m_candles": max_pending_5m_candles,
            "actionable_swing_debug": actionable_swing_debug,
        })
    except Exception as exc:
        debug["reason"] = f"15m swing-break validation unavailable: {exc}"

    return debug

def get_pending_15m_swing_setup(symbol, closed_data_5m):
    normalized_symbol = normalize_symbol(symbol)
    candidates = []
    state_changed = False

    if closed_data_5m is None:
        closed_data_5m = pd.DataFrame()

    for side in ["BUY", "SELL"]:
        key = get_15m_swing_watch_key(normalized_symbol, side)
        watched = FIFTEEN_M_SWING_WATCH.get(key)

        if not isinstance(watched, dict) or not watched.get("break_confirmed"):
            continue

        if str(watched.get("status") or "PENDING").upper() != "PENDING":
            continue

        try:
            level = float(watched.get("swing_level"))
            breakout_ts = pd.Timestamp(watched.get("break_candle_time"))
            if breakout_ts.tzinfo is None:
                breakout_ts = breakout_ts.tz_localize("UTC")
            else:
                breakout_ts = breakout_ts.tz_convert("UTC")
        except Exception:
            continue

        following = []
        for candle_time, candle in closed_data_5m.iterrows():
            try:
                candle_ts = pd.Timestamp(candle_time)
                if candle_ts.tzinfo is None:
                    candle_ts = candle_ts.tz_localize("UTC")
                else:
                    candle_ts = candle_ts.tz_convert("UTC")
                if candle_ts > breakout_ts:
                    following.append((candle_ts, candle))
            except Exception:
                continue

        if watched.get("confirmed_5m_time"):
            watched["status"] = "CONFIRMED"
            state_changed = True
            continue

        pending_age = len(following)
        print(
            "FIFTEEN_M_PENDING_SETUP_AGE_DEBUG =",
            {
                "symbol": normalized_symbol,
                "side": side,
                "pending_setup_age": pending_age,
                "max_allowed_age": None,
                "expired": False,
                "remembered_setup": True,
            },
        )

        watched["status"] = "PENDING"
        watched["five_m_candles_elapsed"] = pending_age
        watched["candles_remaining"] = None
        watched["expires_after_5m_candles"] = None
        candidates.append(watched)

    if state_changed:
        save_fifteen_m_swing_watch()

    if not candidates:
        return None

    def breakout_time(item):
        try:
            return pd.Timestamp(item.get("break_candle_time"))
        except Exception:
            return pd.Timestamp.min

    return max(candidates, key=breakout_time)

def get_last_closed_5m_candle(data_5m):
    closed_data = remove_current_forming_candle(data_5m, 5)

    if closed_data is None or closed_data.empty:
        return None, None

    try:
        candle = closed_data.iloc[-1]
        candle_time = pd.Timestamp(closed_data.index[-1]).isoformat()
        return candle, candle_time
    except Exception:
        return None, None

def get_15m_setup_side(result):
    if not isinstance(result, dict):
        return "WAIT"

    structure = str(result.get("structure_type") or "").upper()
    setup_type = str(result.get("strategy_setup_type") or "").upper()
    pending_setup = str(result.get("pending_setup") or "").upper()

    if pending_setup in ["BUY", "SELL"] and result.get("entry_confirm_level") not in [None, "--"]:
        return pending_setup

    if "CHOCH BUY" in structure or "BOS BUY" in structure:
        return "BUY"
    if "CHOCH SELL" in structure or "BOS SELL" in structure:
        return "SELL"
    if setup_type in ["BUY_RETEST", "BUY_BREAKOUT", "BUY_MOMENTUM_BREAKOUT"]:
        return "BUY"
    if setup_type in ["SELL_RETEST", "SELL_BREAKOUT", "SELL_MOMENTUM_BREAKOUT"]:
        return "SELL"

    return "WAIT"

def get_15m_setup_level(result, side):
    if not isinstance(result, dict):
        return None

    side = str(side or "").upper()
    candidate_keys = (
        ["entry_confirm_level", "structure_resistance", "entry_price"]
        if side == "BUY"
        else ["entry_confirm_level", "structure_support", "entry_price"]
    )

    for key in candidate_keys:
        value = result.get(key)

        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue

        if math.isfinite(numeric):
            return numeric

    return None

def confirm_5m_entry_from_15m_setup(
    data_5m,
    side,
    setup_level,
    symbol,
    momentum_breakout=False
):
    closed_5m = remove_current_forming_candle(data_5m, 5)
    candle, candle_time = get_last_closed_5m_candle(data_5m)

    if candle is None:
        return {
            "side": "WAIT",
            "closed_candle_time": None,
            "close_confirmed": False,
            "reason": "5m closed candle unavailable",
            "close": None,
            "open": None,
        }

    try:
        open_price = float(candle["Open"])
        high_price = float(candle["High"])
        low_price = float(candle["Low"])
        close_price = float(candle["Close"])
    except (TypeError, ValueError, KeyError):
        return {
            "side": "WAIT",
            "closed_candle_time": candle_time,
            "close_confirmed": False,
            "reason": "5m candle price invalid",
            "close": None,
            "open": None,
        }

    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    tolerance = get_pair_strategy_rule(normalized_symbol, "five_m_tolerance")
    min_body = get_pair_strategy_rule(normalized_symbol, "five_m_min_body")
    max_confirmation_range = get_pair_strategy_rule(
        normalized_symbol,
        "five_m_max_confirmation_range",
    )
    min_body_ratio = 0.45
    max_wick_to_body = 2.25
    level = setup_level

    if level is None:
        level = close_price

    candle_body = abs(close_price - open_price)
    candle_range = max(high_price - low_price, 0)
    body_ratio = candle_body / candle_range if candle_range > 0 else 0
    upper_wick = high_price - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low_price
    wick_to_body = max(upper_wick, lower_wick) / max(candle_body, 1e-12)
    previous_candle = None

    try:
        if closed_5m is not None and len(closed_5m) >= 2:
            previous_candle = closed_5m.iloc[-2]
    except Exception:
        previous_candle = None

    bullish_engulfing = False
    bearish_engulfing = False

    if previous_candle is not None:
        try:
            prev_open = float(previous_candle["Open"])
            prev_close = float(previous_candle["Close"])
            bullish_engulfing = (
                close_price > open_price
                and prev_close < prev_open
                and close_price >= prev_open
                and open_price <= prev_close
            )
            bearish_engulfing = (
                close_price < open_price
                and prev_close > prev_open
                and close_price <= prev_open
                and open_price >= prev_close
            )
        except (TypeError, ValueError, KeyError):
            bullish_engulfing = False
            bearish_engulfing = False

    if momentum_breakout:
        candle_quality_ok = (
            candle_body >= min_body
            and body_ratio >= 0.35
            and wick_to_body <= 3.0
        )
    else:
        candle_quality_ok = (
            candle_body >= min_body
            and body_ratio >= min_body_ratio
            and wick_to_body <= max_wick_to_body
            and candle_range <= max_confirmation_range
        )
    engulfing_ok = bullish_engulfing if side == "BUY" else bearish_engulfing

    if not candle_quality_ok and not engulfing_ok:
        reasons = []

        if candle_body < min_body:
            reasons.append("5m body too small")
        if body_ratio < min_body_ratio:
            reasons.append("5m candle body not meaningful")
        if wick_to_body > max_wick_to_body:
            reasons.append("5m wick too large")
        if not momentum_breakout and candle_range > max_confirmation_range:
            reasons.append("5m candle too large to chase")

        return {
            "side": "WAIT",
            "closed_candle_time": candle_time,
            "close_confirmed": False,
            "reason": "; ".join(reasons) or f"Waiting for stronger 5m {side} confirmation",
            "close": close_price,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "setup_level": level,
            "candle_body": candle_body,
            "candle_range": candle_range,
            "body_ratio": round(body_ratio, 3),
            "wick_to_body": round(wick_to_body, 3),
            "bullish_engulfing": bullish_engulfing,
            "bearish_engulfing": bearish_engulfing,
            "momentum_breakout": momentum_breakout,
        }

    bullish_close = close_price > open_price
    bearish_close = close_price < open_price
    buy_confirmed = (
        side == "BUY"
        and bullish_close
        and close_price >= float(level) - tolerance
    )
    sell_confirmed = (
        side == "SELL"
        and bearish_close
        and close_price <= float(level) + tolerance
    )

    if buy_confirmed:
        confirmed_side = "BUY"
        reason = "5m bullish closed candle confirmed entry"
    elif sell_confirmed:
        confirmed_side = "SELL"
        reason = "5m bearish closed candle confirmed entry"
    else:
        confirmed_side = "WAIT"
        reason = f"Waiting for 5m {side} candle close confirmation"

    return {
        "side": confirmed_side,
        "closed_candle_time": candle_time,
        "close_confirmed": buy_confirmed or sell_confirmed,
        "reason": reason,
        "close": close_price,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "setup_level": level,
        "candle_body": candle_body,
        "candle_range": candle_range,
        "body_ratio": round(body_ratio, 3),
        "wick_to_body": round(wick_to_body, 3),
        "bullish_engulfing": bullish_engulfing,
        "bearish_engulfing": bearish_engulfing,
        "momentum_breakout": momentum_breakout,
    }

def confirm_5m_close_after_15m_break(data_5m, side, setup_level, breakout_candle_time, symbol):
    side = str(side or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    closed_5m = remove_current_forming_candle(data_5m, 5)
    level = setup_level
    tolerance, raw_tolerance, tolerance_error = resolve_15m_level_tolerance(
        normalized_symbol
    )
    max_pending_5m_candles = get_15m_pending_max_5m_candles(
        normalized_symbol
    )

    base = {
        "side": "WAIT",
        "closed_candle_time": None,
        "close_confirmed": False,
        "reason": "WAIT_NO_5M_CLOSE_CONFIRMATION",
        "close": None,
        "open": None,
        "high": None,
        "low": None,
        "setup_level": level,
        "invalidated": False,
        "expired": False,
        "candles_checked": 0,
    }

    if side not in ["BUY", "SELL"] or closed_5m is None or closed_5m.empty or level is None:
        return base

    try:
        anchor_ts = pd.Timestamp(breakout_candle_time)
        if anchor_ts.tzinfo is None:
            anchor_ts = anchor_ts.tz_localize("UTC")
        else:
            anchor_ts = anchor_ts.tz_convert("UTC")
        level = float(level)
    except Exception:
        return base

    following_candles = []

    for candle_time, candle in closed_5m.iterrows():
        try:
            candle_ts = pd.Timestamp(candle_time)
            if candle_ts.tzinfo is None:
                candle_ts = candle_ts.tz_localize("UTC")
            else:
                candle_ts = candle_ts.tz_convert("UTC")

            if candle_ts <= anchor_ts:
                continue

            following_candles.append((candle_ts, candle))
        except Exception:
            continue

    print(
        "FIFTEEN_M_PENDING_SETUP_AGE_DEBUG =",
        {
            "symbol": normalized_symbol,
            "side": side,
            "pending_setup_age": len(following_candles),
            "max_allowed_age": None,
            "expired": False,
            "remembered_setup": True,
            "tolerance_used": tolerance,
            "raw_tolerance": raw_tolerance,
        },
    )

    for candle_ts, candle in following_candles:
        try:
            open_price = float(candle["Open"])
            close_price = float(candle["Close"])
            high_price = float(candle["High"])
            low_price = float(candle["Low"])

            invalidated = (
                side == "BUY"
                and close_price < level - tolerance
            ) or (
                side == "SELL"
                and close_price > level + tolerance
            )
            directional_close = (
                side == "BUY"
                and close_price > open_price
                and close_price >= level - tolerance
            ) or (
                side == "SELL"
                and close_price < open_price
                and close_price <= level + tolerance
            )

            if tolerance_error:
                print("FIVE_M_CONFIRMATION_TOLERANCE_FALLBACK =", {
                    "symbol": normalized_symbol,
                    "level": level,
                    "tolerance": raw_tolerance,
                    "tolerance_used": tolerance,
                    "candle_time": candle_ts.isoformat(),
                    "error": tolerance_error,
                })

            print("FIVE_M_CONFIRMATION_CANDLE_DEBUG =", {
                "symbol": normalized_symbol,
                "side": side,
                "fifteen_m_level": level,
                "tolerance_used": tolerance,
                "candle_time": candle_ts.isoformat(),
                "five_m_close": close_price,
                "confirmation_pass": bool(directional_close),
                "confirmation_fail": not bool(directional_close),
                "invalidation_pass": bool(invalidated),
                "invalidation_fail": not bool(invalidated),
            })

            if directional_close:
                key = get_15m_swing_watch_key(normalized_symbol, side)
                watched = FIFTEEN_M_SWING_WATCH.get(key)
                watched_matches_setup = False
                if isinstance(watched, dict):
                    try:
                        watched_break_ts = pd.Timestamp(
                            watched.get("break_candle_time")
                        )
                        if watched_break_ts.tzinfo is None:
                            watched_break_ts = watched_break_ts.tz_localize(
                                "UTC"
                            )
                        else:
                            watched_break_ts = watched_break_ts.tz_convert(
                                "UTC"
                            )
                        watched_matches_setup = (
                            abs(
                                float(watched.get("swing_level")) - level
                            ) <= 1e-8
                            and watched_break_ts == anchor_ts
                        )
                    except Exception:
                        watched_matches_setup = False

                if watched_matches_setup:
                    watched["status"] = "CONFIRMED"
                    watched["confirmed_5m_time"] = candle_ts.isoformat()
                    watched["confirmation_close"] = close_price
                    save_fifteen_m_swing_watch()

                return {
                    "side": side,
                    "closed_candle_time": candle_ts.isoformat(),
                    "close_confirmed": True,
                    "reason": (
                        f"{side} confirmed by directional 5m close "
                        "above/near the broken 15m swing"
                    ),
                    "close": close_price,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "setup_level": level,
                    "invalidated": False,
                    "expired": False,
                    "candles_checked": len(following_candles),
                }
        except Exception as exc:
            print("FIVE_M_CONFIRMATION_CANDLE_ERROR =", {
                "symbol": normalized_symbol,
                "level": level,
                "tolerance": raw_tolerance,
                "tolerance_used": tolerance,
                "candle_time": (
                    candle_ts.isoformat()
                    if "candle_ts" in locals()
                    else None
                ),
                "error": str(exc),
            })
            continue

    return {
        **base,
        "reason": f"Remembered 15m {side} setup; waiting for directional 5m close",
        "candles_checked": len(following_candles),
        "candles_remaining": None,
    }

def evaluate_xauusd_buy_continuation_quality(
    closed_data_15m,
    five_m_entry,
    fifteen_m_swing_break,
    five_m_after_setup,
):
    reason = "WAIT: XAUUSD weak BOS / no clean continuation confirmation"
    debug = {
        "blocked": False,
        "reason": None,
        "min_clearance": None,
        "break_close_clearance": None,
        "break_body_ratio": None,
        "closed_above_swing_cleanly": False,
        "bullish_break_body": False,
        "fresh_5m_confirmation": False,
        "cleared_nearby_resistance": False,
        "recent_resistance": None,
    }

    if closed_data_15m is None or closed_data_15m.empty:
        debug["blocked"] = True
        debug["reason"] = reason
        return debug

    try:
        swing_level = float(fifteen_m_swing_break.get("swing_level"))
        break_close = float(fifteen_m_swing_break.get("closed_candle_close"))
        break_time = pd.Timestamp(fifteen_m_swing_break.get("closed_candle_time"))
        tolerance = get_15m_level_tolerance("XAUUSD") or 0
        min_clearance = max(0.50, float(tolerance) * 0.50)
    except (TypeError, ValueError):
        debug["blocked"] = True
        debug["reason"] = reason
        return debug

    if break_time.tzinfo is None:
        break_time = break_time.tz_localize("UTC")
    else:
        break_time = break_time.tz_convert("UTC")

    break_row = None
    pre_break_rows = []

    for candle_time, candle in closed_data_15m.iterrows():
        candle_ts = pd.Timestamp(candle_time)
        if candle_ts.tzinfo is None:
            candle_ts = candle_ts.tz_localize("UTC")
        else:
            candle_ts = candle_ts.tz_convert("UTC")

        if candle_ts < break_time:
            pre_break_rows.append(candle)
        elif candle_ts == break_time:
            break_row = candle

    if break_row is None:
        break_row = closed_data_15m.iloc[-1]
        pre_break = closed_data_15m.iloc[:-1].copy()
    else:
        pre_break = pd.DataFrame(pre_break_rows)

    try:
        break_open = float(break_row["Open"])
        break_high = float(break_row["High"])
        break_low = float(break_row["Low"])
    except (TypeError, ValueError):
        debug["blocked"] = True
        debug["reason"] = reason
        return debug

    break_range = max(abs(break_high - break_low), 1e-12)
    break_body = abs(break_close - break_open)
    break_body_ratio = break_body / break_range
    break_close_clearance = break_close - swing_level
    recent_resistance = None

    if pre_break is not None and not pre_break.empty:
        try:
            recent_resistance = float(pre_break["High"].tail(20).max())
        except Exception:
            recent_resistance = None

    closed_above_swing_cleanly = break_close_clearance >= min_clearance
    bullish_break_body = (
        break_close > break_open
        and break_body_ratio >= 0.45
    )
    cleared_nearby_resistance = (
        recent_resistance is None
        or break_close >= max(swing_level, recent_resistance) + min_clearance
    )

    try:
        five_m_close = float(five_m_entry.get("close"))
        five_m_open = float(five_m_entry.get("open"))
    except (TypeError, ValueError):
        five_m_close = None
        five_m_open = None

    fresh_5m_confirmation = bool(
        five_m_entry.get("side") == "BUY"
        and five_m_entry.get("close_confirmed")
        and five_m_after_setup
        and five_m_close is not None
        and five_m_open is not None
        and five_m_close >= swing_level + min_clearance
        and five_m_close > five_m_open
    )

    debug.update({
        "min_clearance": round(min_clearance, 2),
        "break_close_clearance": round(break_close_clearance, 2),
        "break_body_ratio": round(break_body_ratio, 3),
        "closed_above_swing_cleanly": closed_above_swing_cleanly,
        "bullish_break_body": bullish_break_body,
        "fresh_5m_confirmation": fresh_5m_confirmation,
        "cleared_nearby_resistance": cleared_nearby_resistance,
        "recent_resistance": recent_resistance,
        "swing_level": swing_level,
        "break_close": break_close,
        "five_m_entry": copy.deepcopy(five_m_entry),
    })

    if not (
        closed_above_swing_cleanly
        and bullish_break_body
        and cleared_nearby_resistance
        and fresh_5m_confirmation
    ):
        debug["blocked"] = True
        debug["reason"] = reason

    return debug

def is_current_5m_entry_confirmation(closed_data_5m, confirmation, side):
    side = str(side or "").upper()
    confirmation = confirmation if isinstance(confirmation, dict) else {}

    if (
        side not in ["BUY", "SELL"]
        or closed_data_5m is None
        or closed_data_5m.empty
        or str(confirmation.get("side") or "").upper() != side
        or not confirmation.get("close_confirmed")
        or not confirmation.get("closed_candle_time")
    ):
        return False

    try:
        confirmation_time = pd.Timestamp(
            confirmation["closed_candle_time"]
        )
        latest_closed_time = pd.Timestamp(closed_data_5m.index[-1])

        if confirmation_time.tzinfo is None:
            confirmation_time = confirmation_time.tz_localize("UTC")
        else:
            confirmation_time = confirmation_time.tz_convert("UTC")

        if latest_closed_time.tzinfo is None:
            latest_closed_time = latest_closed_time.tz_localize("UTC")
        else:
            latest_closed_time = latest_closed_time.tz_convert("UTC")

        age_seconds = (
            datetime.now(timezone.utc)
            - latest_closed_time.to_pydatetime()
        ).total_seconds()

        return (
            confirmation_time == latest_closed_time
            and 0 <= age_seconds <= 11 * 60
        )
    except Exception:
        return False


def evaluate_5m_late_entry_guard(data_5m, result, side, setup_level, five_m_entry, symbol):
    normalized_symbol = normalize_symbol(symbol)
    pip_size = get_pair_strategy_rule(normalized_symbol, "late_entry_pip_size")
    max_setup_distance = get_pair_strategy_rule(
        normalized_symbol,
        "late_entry_max_setup_distance",
    )
    min_rr = 1.0
    max_moved_to_tp1 = FINAL_SIGNAL_HOLD_MAX_TP1_PROGRESS
    small_body_ratio = 0.35

    debug = {
        "symbol": normalized_symbol,
        "side": side,
        "entry_price": None,
        "setup_level": setup_level,
        "distance_from_setup": None,
        "distance_from_setup_pips": None,
        "candle_body": None,
        "candle_range": None,
        "body_ratio": None,
        "impulse_before_entry": False,
        "risk_reward": None,
        "already_moved_percent_to_tp1": None,
        "late_entry": False,
        "late_entry_reason": None,
    }

    try:
        entry_price = float(five_m_entry.get("close"))
        open_price = float(five_m_entry.get("open"))
        high_price = float(five_m_entry.get("high"))
        low_price = float(five_m_entry.get("low"))
    except (TypeError, ValueError):
        debug["late_entry"] = True
        debug["late_entry_reason"] = "5m entry candle price invalid"
        return debug

    setup_level = setup_level if setup_level is not None else result.get("entry_confirm_level")

    try:
        setup_level = float(setup_level)
    except (TypeError, ValueError):
        setup_level = entry_price

    candle_body = abs(entry_price - open_price)
    candle_range = max(abs(high_price - low_price), pip_size)
    body_ratio = candle_body / candle_range

    distance_from_setup = (
        entry_price - setup_level
        if side == "BUY"
        else setup_level - entry_price
    )
    distance_from_setup_pips = distance_from_setup / pip_size

    impulse_before_entry = False

    if data_5m is not None and len(data_5m) >= 4:
        prev = data_5m.iloc[-4:-1]
        prev_ranges = [
            abs(float(row["High"]) - float(row["Low"])) / pip_size
            for _, row in prev.iterrows()
        ]
        impulse_before_entry = max(prev_ranges or [0]) >= get_pair_strategy_rule(
            normalized_symbol,
            "late_entry_impulse_range_pips",
        )

    try:
        sl = float(result.get("stop_loss"))
        tp1 = float(result.get("tp1"))
        risk = abs(entry_price - sl)
        reward = abs(tp1 - entry_price)
        risk_reward = reward / risk if risk > 0 else 0
    except (TypeError, ValueError):
        tp1 = None
        risk_reward = None

    moved_to_tp1 = None

    if tp1 is not None:
        total_to_tp1 = abs(tp1 - setup_level)
        moved = abs(entry_price - setup_level)
        moved_to_tp1 = moved / total_to_tp1 if total_to_tp1 > 0 else 1

    reasons = []

    if distance_from_setup_pips > max_setup_distance:
        reasons.append("price too far from 15m setup level")
    if body_ratio <= small_body_ratio and impulse_before_entry:
        reasons.append("small 5m candle after impulse")
    if moved_to_tp1 is not None and moved_to_tp1 >= max_moved_to_tp1:
        reasons.append("price already close to TP1")

    debug.update({
        "entry_price": entry_price,
        "setup_level": setup_level,
        "distance_from_setup": round(distance_from_setup, 6),
        "distance_from_setup_pips": round(distance_from_setup_pips, 1),
        "candle_body": round(candle_body, 6),
        "candle_range": round(candle_range, 6),
        "body_ratio": round(body_ratio, 3),
        "impulse_before_entry": impulse_before_entry,
        "risk_reward": round(risk_reward, 2) if risk_reward is not None else None,
        "already_moved_percent_to_tp1": round(moved_to_tp1, 3) if moved_to_tp1 is not None else None,
        "late_entry": bool(reasons),
        "late_entry_reason": "; ".join(reasons) if reasons else None,
    })

    return debug

def is_valid_15m_entry(result, side):
    if not isinstance(result, dict):
        return False

    side = str(side or "").upper()
    structure = str(result.get("structure_type") or "").upper()
    plan_type = str(result.get("plan_type") or "").upper()
    entry_timing = str(result.get("entry_timing") or "").upper()

    if not result.get("strategy_setup_complete"):
        return False

    has_structure = (
        f"CHOCH {side}" in structure
        or f"BOS {side}" in structure
        or f"SMC {side}" in plan_type
        or f"BOS {side}" in plan_type
        or f"FVG {side}" in plan_type
    )
    valid_timing = (
        entry_timing == f"GOOD_{side}"
        or "RETEST" in entry_timing
        or entry_timing in [f"FVG {side} ENTRY", f"BOS {side} ENTRY"]
    )

    return has_structure and valid_timing

def clear_trade_plan(result, reason):
    print(
        "PRE_WAIT",
        result.get("buy_pct"),
        result.get("sell_pct"),
        result.get("confidence"),
    )
    result["signal"] = "WAIT"
    result["entry_price"] = "--"
    result["stop_loss"] = "--"
    result["tp1"] = "--"
    result["tp2"] = "--"
    result["risk_reward"] = "--"
    result["plan_bias"] = "WAIT"
    result["plan_type"] = "WAIT FOR STRATEGY CONFIRMATION"
    result["entry_quality"] = "WAIT"
    result["entry_timing"] = "WAIT"
    result["signal_text"] = "WAIT ⚪ (strategy filter)"
    result["plan_reason"] = reason
    result.setdefault("debug_reasons", [])
    result["debug_reasons"] = (result["debug_reasons"] + [reason])[-14:]
    print(
        "POST_WAIT",
        result.get("buy_pct"),
        result.get("sell_pct"),
        result.get("confidence"),
    )
    return result

def mark_signal_blocker(result, blocked_by, blocked_reason, blocker_rule_name, signal_before_filters=None):
    result["blocked_by"] = blocked_by
    result["blocked_reason"] = blocked_reason
    result["signal_before_filters"] = signal_before_filters or result.get("signal_before_filters") or result.get("signal") or "WAIT"
    result["blocker_rule_name"] = blocker_rule_name
    trace = list(result.get("blocker_trace") or [])
    diagnostic = {
        "blocked_by": blocked_by,
        "blocked_reason": blocked_reason,
        "blocker_rule_name": blocker_rule_name,
        "signal_before_filters": result["signal_before_filters"],
    }
    if not trace or trace[-1] != diagnostic:
        trace.append(diagnostic)
    result["blocker_trace"] = trace[-20:]
    print(f"BLOCKED BY: {blocker_rule_name}")
    return result

def enforce_15m_entry_requirements(
    result,
    side,
    fifteen_m_setup,
    fifteen_m_swing_break,
    pullback_block_active=False,
    pullback_block_reason=None,
):
    side = str(side or "").upper()
    fifteen_m_setup = str(fifteen_m_setup or "WAIT").upper()
    swing_break = fifteen_m_swing_break or {}
    break_confirmed = bool(
        swing_break.get("confirmed")
        or (
            swing_break.get("break_happened")
            and swing_break.get("close_confirmed")
        )
    )

    if side not in ["BUY", "SELL"]:
        return result

    if pullback_block_active:
        reason = pullback_block_reason or (
            f"WAIT_{side}_PULLBACK_REQUIRES_NEW_15M_BOS"
        )
        result = clear_trade_plan(result, reason)
        return mark_signal_blocker(
            result,
            "pullback_without_new_15m_bos",
            reason,
            "new_15m_bos_required_after_pullback",
            side,
        )

    if fifteen_m_setup != side or not break_confirmed:
        reason = swing_break.get("reason") or (
            f"WAIT_{side}_REQUIRES_15M_SWING_BREAK_CLOSE"
        )
        result = clear_trade_plan(result, reason)
        return mark_signal_blocker(
            result,
            "missing_15m_swing_break",
            reason,
            "fifteen_m_closed_swing_break_required",
            side,
        )

    return result

def get_breakout_entry_allowed(result, side):
    side = str(side or "").upper()
    for item in result.get("smc_breakout_debug") or []:
        if (
            isinstance(item, dict)
            and str(item.get("side") or "").upper() == side
        ):
            return bool(item.get("breakout_entry_allowed"))
    return False

def evaluate_smc_15m_momentum(
    result,
    one_h,
    closed_data_15m,
    fifteen_m_setup,
    symbol,
    side,
):
    side = str(side or "").upper()
    setup_side = str(fifteen_m_setup or "").upper()
    side_rules = {
        "BUY": {
            "htf": "BULLISH",
            "pattern": "HH_HL",
            "opposite": "SELL",
        },
        "SELL": {
            "htf": "BEARISH",
            "pattern": "LH_LL",
            "opposite": "BUY",
        },
    }
    rules = side_rules.get(side)
    context_15m = _closed_frame_score_context(closed_data_15m, symbol)
    score_debug = result.get("score_contribution_debug") or {}
    structure_text = str(result.get("structure_type") or "").upper()
    one_h_bias = str(one_h.get("bias") or "").upper()
    one_h_shift = str(one_h.get("shift") or "").upper()
    htf_structure = (
        one_h_bias
        if one_h_bias in ["BULLISH", "BEARISH"]
        else one_h_shift
        if one_h_shift in ["BULLISH", "BEARISH"]
        else str(result.get("structure_trend") or "").upper()
    )
    structure_flags = {
        candidate_side: {
            "choch": bool(
                score_debug.get(f"{direction.lower()}_choch")
                or f"CHOCH {candidate_side}" in structure_text
            ),
            "bos": bool(
                score_debug.get(f"{direction.lower()}_bos")
                or f"BOS {candidate_side}" in structure_text
            ),
        }
        for candidate_side, direction in [
            ("BUY", "BULLISH"),
            ("SELL", "BEARISH"),
        ]
    }
    explicit_structure_sides = {
        candidate_side
        for candidate_side in ["BUY", "SELL"]
        if (
            f"CHOCH {candidate_side}" in structure_text
            or f"BOS {candidate_side}" in structure_text
        )
    }

    if side in explicit_structure_sides:
        # The latest explicit 15m CHOCH/BOS is authoritative. Score debug can
        # still contain an older opposite-side flag from the preceding leg;
        # that stale flag must not pin the pair to the prior trade direction.
        structure_flags[side]["choch"] = (
            structure_flags[side]["choch"]
            or f"CHOCH {side}" in structure_text
        )
        structure_flags[side]["bos"] = (
            structure_flags[side]["bos"]
            or f"BOS {side}" in structure_text
        )
        structure_flags[rules["opposite"]]["choch"] = False

    try:
        sell_score = float(result.get("sell_pct") or 0)
    except (TypeError, ValueError):
        sell_score = 0
    try:
        buy_score = float(result.get("buy_pct") or 0)
    except (TypeError, ValueError):
        buy_score = 0
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0

    directional_score = buy_score if side == "BUY" else sell_score if side == "SELL" else 0
    price = context_15m.get("price")
    direction = rules["htf"] if rules else None
    direction_label = str(direction or "").lower()
    opposite_side = rules["opposite"] if rules else None
    opposite_direction = (
        side_rules[opposite_side]["htf"].lower()
        if opposite_side
        else "opposite"
    )
    fifteen_m_side_ok = bool(
        rules
        and (
            str(context_15m.get("structure") or "").upper() == direction
            or side in structure_text
            or str(result.get("fifteen_m_pattern") or "").upper()
            == rules["pattern"]
        )
    )
    structural_conditions = {
        "actionable_side": rules is not None,
        "fifteen_m_setup_required_side": setup_side == side,
        f"htf_{direction_label}": bool(rules and htf_structure == direction),
        f"fifteen_m_{direction_label}": fifteen_m_side_ok,
        f"{direction_label}_choch_or_bos": bool(
            rules
            and (
                structure_flags[side]["choch"]
                or structure_flags[side]["bos"]
            )
        ),
        f"{side.lower()}_score_at_least_75": directional_score >= 75,
        f"no_{opposite_direction}_choch": bool(
            rules and not structure_flags[opposite_side]["choch"]
        ),
        "entry_confirmation_available": bool(
            result.get("current_5m_entry_confirmation")
            or get_breakout_entry_allowed(result, side)
        ),
    }
    structurally_qualified = all(structural_conditions.values())
    momentum_confidence = confidence

    if structurally_qualified:
        # The legacy retest/holding branches reduce confidence because a
        # pullback did not occur. That penalty is not applicable to the
        # explicit no-retest momentum continuation model. Rebuild confidence
        # from directional separation while keeping it capped below 90.
        momentum_confidence = max(
            confidence,
            min(88, directional_score - 10),
        )

    conditions = {
        **structural_conditions,
        "confidence_at_least_70": momentum_confidence >= 70,
    }
    rule_names = {
        "actionable_side": "continuation_side_required",
        "fifteen_m_setup_required_side": "fifteen_m_setup_required_side",
        f"htf_{direction_label}": "htf_required_side",
        f"fifteen_m_{direction_label}": "fifteen_m_structure_required_side",
        f"{direction_label}_choch_or_bos": "choch_or_bos_required_side",
        f"{side.lower()}_score_at_least_75": "continuation_score_threshold",
        "confidence_at_least_70": "continuation_confidence_threshold",
        f"no_{opposite_direction}_choch": "opposing_choch_absent",
        "entry_confirmation_available": "current_entry_confirmation_required",
    }
    failed_condition = next(
        (name for name, passed in conditions.items() if not passed),
        None,
    )
    blocker_rule_name = rule_names.get(failed_condition)
    allowed = failed_condition is None
    reason = (
        f"{side} 15m momentum continuation allowed"
        if allowed
        else f"{side or 'UNKNOWN'} 15m momentum continuation blocked by "
        f"{blocker_rule_name or failed_condition}"
    )

    return {
        "allowed": allowed,
        "conditions": conditions,
        "side": side,
        "blocked_by": None if allowed else "15m_momentum_continuation",
        "blocker_rule_name": blocker_rule_name,
        "reason": reason,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "raw_confidence": confidence,
        "confidence": momentum_confidence,
        "bullish_displacement": (
            "BULLISH" in str(context_15m.get("displacement") or "").upper()
            or bool(score_debug.get("bullish_displacement"))
        ),
        "bearish_displacement": (
            "BEARISH" in str(context_15m.get("displacement") or "").upper()
            or bool(score_debug.get("bearish_displacement"))
        ),
        "bearish_choch": structure_flags["SELL"]["choch"],
        "bearish_bos": structure_flags["SELL"]["bos"],
        "bullish_choch": structure_flags["BUY"]["choch"],
        "bullish_bos": structure_flags["BUY"]["bos"],
        "htf_structure": htf_structure,
        "fifteen_m_structure": context_15m.get("structure"),
        "price": price,
        "breakout_entry_allowed": get_breakout_entry_allowed(result, side),
    }

def get_15m_structure_side(result):
    structure = str(result.get("structure_type") or "").upper()
    plan_type = str(result.get("plan_type") or "").upper()

    if "BUY" in structure or "BUY" in plan_type:
        return "BULLISH"
    if "SELL" in structure or "SELL" in plan_type:
        return "BEARISH"

    return "NEUTRAL"

def _score_direction_value(direction):
    normalized = str(direction or "NEUTRAL").upper()

    # Display scoring should show directional bias, not absolute certainty.
    # Using 100/0 made one bearish or bullish context push the UI to extreme
    # values like SELL 100% even when the actual trade confidence was low.
    # Keep fully aligned directional bias strong, but never mathematically
    # perfect unless another engine explicitly sets a live trade.
    if normalized in ["BULLISH", "BUY"]:
        return 90.0
    if normalized in ["BEARISH", "SELL"]:
        return 10.0
    return 50.0

def _closed_frame_score_context(data, symbol):
    context = {
        "price": None,
        "displacement": "NONE",
        "displacement_score": 0,
        "pressure": "NEUTRAL",
        "structure": "NEUTRAL",
        "pattern": "NEUTRAL",
        "last_high": None,
        "last_low": None,
    }
    if data is None or data.empty or len(data) < 3:
        return context

    try:
        close = data["Close"]
        price = float(close.iloc[-1])
        displacement, displacement_score = detect_displacement(data, symbol)
        pressure, _pressure_score = detect_recent_pressure(data, symbol)
        swing_structure = detect_hh_hl_lh_ll_structure(data)
        pattern = str(swing_structure.get("structure") or "NEUTRAL").upper()
        structural_bias = detect_htf_structure(data)

        if pattern == "HH_HL":
            structural_bias = "BULLISH"
        elif pattern == "LH_LL":
            structural_bias = "BEARISH"

        context.update({
            "price": price,
            "displacement": displacement,
            "displacement_score": displacement_score,
            "pressure": pressure,
            "structure": structural_bias,
            "pattern": pattern,
            "last_high": swing_structure.get("last_high"),
            "last_low": swing_structure.get("last_low"),
        })
    except Exception as exc:
        context["error"] = str(exc)

    return context

def get_score_side(buy_score, sell_score, neutral_gap=5):
    try:
        buy_value = float(buy_score)
        sell_value = float(sell_score)
    except (TypeError, ValueError):
        return "NEUTRAL"

    if buy_value - sell_value > neutral_gap:
        return "BUY"
    if sell_value - buy_value > neutral_gap:
        return "SELL"

    return "NEUTRAL"

def clamp_score_delta(previous, target, max_delta):
    try:
        previous_value = float(previous)
        target_value = float(target)
    except (TypeError, ValueError):
        return target

    delta = target_value - previous_value

    if abs(delta) <= max_delta:
        return target_value

    return previous_value + (max_delta if delta > 0 else -max_delta)

def describe_largest_score_factors(components, weights):
    factors = []

    for name, value in dict(components or {}).items():
        try:
            weighted_buy_edge = (float(value) - 50.0) * float(weights.get(name, 0))
        except (TypeError, ValueError):
            continue
        factors.append({
            "factor": name,
            "buy_edge": round(weighted_buy_edge, 2),
        })

    positive = [factor for factor in factors if factor["buy_edge"] > 0]
    negative = [factor for factor in factors if factor["buy_edge"] < 0]
    largest_positive = (
        max(positive, key=lambda item: item["buy_edge"])
        if positive
        else None
    )
    largest_negative = (
        min(negative, key=lambda item: item["buy_edge"])
        if negative
        else None
    )

    return largest_positive, largest_negative

def apply_score_persistence(
    result,
    symbol,
    target_buy_score,
    target_sell_score,
    components,
    weights,
    structure_debug,
):
    memory_key = normalize_symbol(symbol)
    previous_memory = SCORE_PERSISTENCE_MEMORY.get(memory_key)

    def numeric_score(value, fallback):
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return fallback

    previous_buy = numeric_score(
        (previous_memory or {}).get("buy_score"),
        numeric_score(result.get("buy_pct"), target_buy_score),
    )
    previous_sell = numeric_score(
        (previous_memory or {}).get("sell_score"),
        numeric_score(result.get("sell_pct"), target_sell_score),
    )
    target_buy = numeric_score(target_buy_score, previous_buy)
    target_sell = numeric_score(target_sell_score, previous_sell)
    previous_side = get_score_side(previous_buy, previous_sell)
    target_side = get_score_side(target_buy, target_sell)
    structure_debug = structure_debug or {}
    allowed_flip_reasons = []

    for key, label in [
        ("opposite_bos", "opposite BOS"),
        ("opposite_choch", "opposite CHOCH"),
        ("major_liquidity_grab", "major liquidity grab"),
        ("structure_invalidation", "structure invalidation"),
    ]:
        if structure_debug.get(key):
            allowed_flip_reasons.append(label)

    if (
        previous_side == "BUY"
        and structure_debug.get("bearish_displacement")
        and structure_debug.get("displacement_score", 0) >= 4
    ) or (
        previous_side == "SELL"
        and structure_debug.get("bullish_displacement")
        and structure_debug.get("displacement_score", 0) >= 4
    ):
        allowed_flip_reasons.append("strong displacement against current trend")

    if (
        previous_side == "BUY"
        and structure_debug.get("bearish_15m_structure")
    ) or (
        previous_side == "SELL"
        and structure_debug.get("bullish_15m_structure")
    ):
        allowed_flip_reasons.append("structure invalidation")

    hard_bias_change = bool(allowed_flip_reasons)
    bias_would_flip = (
        previous_side in ["BUY", "SELL"]
        and target_side in ["BUY", "SELL"]
        and previous_side != target_side
    )
    smoothing = 1.0 if hard_bias_change else 0.08 if bias_would_flip else 0.35
    max_delta = 100 if hard_bias_change else 12
    smoothed_buy = previous_buy + ((target_buy - previous_buy) * smoothing)
    smoothed_sell = 100.0 - smoothed_buy
    capped_buy = clamp_score_delta(previous_buy, smoothed_buy, max_delta)
    capped_sell = 100.0 - capped_buy
    prevented_flip = False

    if bias_would_flip and not hard_bias_change:
        prevented_flip = True
        if previous_side == "BUY":
            capped_buy = max(capped_buy, 55.0)
            capped_sell = 100.0 - capped_buy
        elif previous_side == "SELL":
            capped_sell = max(capped_sell, 55.0)
            capped_buy = 100.0 - capped_sell

    final_buy = max(0, min(100, int(round(capped_buy))))
    final_sell = 100 - final_buy
    final_side = get_score_side(final_buy, final_sell)
    largest_positive, largest_negative = describe_largest_score_factors(
        components,
        weights,
    )
    bias_changed = (
        previous_side in ["BUY", "SELL"]
        and final_side in ["BUY", "SELL"]
        and previous_side != final_side
    )
    reason_for_bias_change = None

    if bias_changed:
        reason_for_bias_change = (
            "; ".join(allowed_flip_reasons)
            if allowed_flip_reasons
            else "Bias changed after score smoothing"
        )
    elif prevented_flip:
        reason_for_bias_change = (
            "Flip blocked: no opposite BOS/CHOCH, liquidity grab, "
            "structure invalidation, or strong opposing displacement"
        )
    else:
        reason_for_bias_change = "Bias persisted; no confirmed structure reversal"

    debug = {
        "buy_score_before": int(round(previous_buy)),
        "sell_score_before": int(round(previous_sell)),
        "target_buy_score": int(round(target_buy)),
        "target_sell_score": int(round(target_sell)),
        "buy_score_after": final_buy,
        "sell_score_after": final_sell,
        "score_delta": {
            "buy": final_buy - int(round(previous_buy)),
            "sell": final_sell - int(round(previous_sell)),
        },
        "largest_positive_factor": largest_positive,
        "largest_negative_factor": largest_negative,
        "bias_changed": bias_changed,
        "bias_would_flip": bias_would_flip,
        "flip_prevented": prevented_flip,
        "reason_for_bias_change": reason_for_bias_change,
        "smoothing": smoothing,
        "max_delta": max_delta,
        "structure_change_reasons": allowed_flip_reasons,
    }
    SCORE_PERSISTENCE_MEMORY[memory_key] = {
        "buy_score": final_buy,
        "sell_score": final_sell,
        "side": final_side,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    return final_buy, final_sell, debug

def apply_weighted_timeframe_scores(
    result,
    one_h,
    closed_data_1h,
    closed_data_15m,
    closed_data_5m,
    five_m_signal,
    symbol,
):
    weights = {
        "one_h_bias": 0.22,
        "fifteen_m_structure": 0.65,
        "five_m_confirmation": 0.03,
        "momentum_displacement": 0.10,
    }
    one_h_context = _closed_frame_score_context(closed_data_1h, symbol)
    fifteen_m_context = _closed_frame_score_context(closed_data_15m, symbol)
    five_m_context = _closed_frame_score_context(closed_data_5m, symbol)

    one_h_direction = str(
        one_h.get("shift")
        if one_h.get("shift") in ["BULLISH", "BEARISH"]
        else one_h.get("bias") or one_h_context.get("structure") or "NEUTRAL"
    ).upper()
    fifteen_m_direction = str(
        fifteen_m_context.get("structure") or "NEUTRAL"
    ).upper()
    five_m_direction = (
        str(five_m_signal).upper()
        if str(five_m_signal).upper() in ["BUY", "SELL"]
        else str(five_m_context.get("pressure") or "NEUTRAL").upper()
    )

    one_h_displacement = str(one_h_context.get("displacement") or "NONE").upper()
    fifteen_m_displacement = str(
        fifteen_m_context.get("displacement") or "NONE"
    ).upper()
    bearish_impulse = (
        "BEARISH" in one_h_displacement
        or "BEARISH" in fifteen_m_displacement
    )
    bullish_impulse = (
        "BULLISH" in one_h_displacement
        or "BULLISH" in fifteen_m_displacement
    )
    if bearish_impulse and not bullish_impulse:
        momentum_direction = "BEARISH"
    elif bullish_impulse and not bearish_impulse:
        momentum_direction = "BULLISH"
    else:
        momentum_direction = "NEUTRAL"

    components = {
        "one_h_bias": _score_direction_value(one_h_direction),
        "fifteen_m_structure": _score_direction_value(fifteen_m_direction),
        "five_m_confirmation": _score_direction_value(five_m_direction),
        "momentum_displacement": _score_direction_value(momentum_direction),
    }
    raw_buy_score = sum(
        components[name] * weights[name]
        for name in weights
    )
    raw_sell_score = 100.0 - raw_buy_score
    buy_score = int(round(raw_buy_score))
    sell_score = int(round(raw_sell_score))
    caps = []
    score_reason = None

    bullish_choch = "CHOCH BUY" in str(result.get("structure_type") or "").upper()
    bearish_choch = "CHOCH SELL" in str(result.get("structure_type") or "").upper()
    bullish_bos = "BOS BUY" in str(result.get("structure_type") or "").upper()
    bearish_bos = "BOS SELL" in str(result.get("structure_type") or "").upper()
    score_debug = result.get("score_contribution_debug") or {}
    bullish_15m_reversal = (
        fifteen_m_direction == "BULLISH"
        or bullish_choch
        or str(result.get("signal") or "").upper() == "BUY"
    )
    bearish_15m_reversal = (
        fifteen_m_direction == "BEARISH"
        or bearish_choch
        or str(result.get("signal") or "").upper() == "SELL"
    )
    key_level = (
        result.get("structure_resistance")
        or result.get("entry_confirm_level")
    )
    try:
        close_above_key_level = (
            fifteen_m_context.get("price") is not None
            and key_level not in [None, "--"]
            and float(fifteen_m_context.get("price")) > float(key_level)
        )
    except (TypeError, ValueError):
        close_above_key_level = False

    bullish_recovery_complete = (
        bullish_choch
        and close_above_key_level
        and "BULLISH" in fifteen_m_displacement
    )

    def cap_buy(maximum, rule, reason):
        nonlocal buy_score, sell_score, score_reason
        before = buy_score
        if buy_score > maximum:
            buy_score = maximum
            sell_score = max(sell_score, 100 - maximum)
            score_reason = score_reason or reason
            caps.append({
                "side": "BUY",
                "rule": rule,
                "before": before,
                "after": buy_score,
                "reason": reason,
            })

    def cap_sell(maximum, rule, reason):
        nonlocal buy_score, sell_score, score_reason
        before = sell_score
        if sell_score > maximum:
            sell_score = maximum
            buy_score = max(buy_score, 100 - maximum)
            score_reason = score_reason or reason
            caps.append({
                "side": "SELL",
                "rule": rule,
                "before": before,
                "after": sell_score,
                "reason": reason,
            })

    if fifteen_m_direction == "BEARISH":
        cap_buy(45, "15m_bearish_buy_cap", "BUY capped: 15m structure bearish")
    if (
        fifteen_m_direction == "NEUTRAL"
        and "BEARISH" in one_h_displacement
        and not bullish_15m_reversal
    ):
        cap_buy(
            55,
            "15m_neutral_1h_bearish_displacement_buy_cap",
            "BUY capped: 15m neutral + bearish impulse",
        )
    if bearish_impulse and not bullish_recovery_complete and not bullish_15m_reversal:
        cap_buy(
            45,
            "bearish_dump_recovery_requirements_buy_cap",
            "BUY capped: bearish impulse needs SMC reversal + key-level close",
        )
    if fifteen_m_direction == "BULLISH":
        cap_sell(45, "15m_bullish_sell_cap", "SELL capped: 15m structure bullish")

    sell_favored = (
        bearish_impulse
        and fifteen_m_direction == "BEARISH"
        and not bullish_15m_reversal
    )
    if sell_favored and score_reason is None:
        score_reason = "SELL favored: bearish SMC displacement"

    one_h_fifteen_m_disagree = (
        one_h_direction in ["BULLISH", "BEARISH"]
        and fifteen_m_direction in ["BULLISH", "BEARISH"]
        and one_h_direction != fifteen_m_direction
    )
    before_confidence = result.get("confidence")
    if one_h_fifteen_m_disagree:
        try:
            result["confidence"] = min(65, int(round(float(before_confidence or 0))))
        except (TypeError, ValueError):
            result["confidence"] = 0

    # Confidence must follow separation.
    # Example: 55/45 should never display 60%+ confidence because the market
    # is balanced. High confidence requires a clear directional gap.
    score_gap_for_confidence = abs(int(round(buy_score)) - int(round(sell_score)))
    confidence_before_alignment = result.get("confidence")
    confidence_cap_reason = None

    if score_gap_for_confidence < 15:
        confidence_cap = 40
        confidence_cap_reason = "confidence capped: buy/sell nearly balanced"
    elif score_gap_for_confidence < 25:
        confidence_cap = 55
        confidence_cap_reason = "confidence capped: weak directional separation"
    elif score_gap_for_confidence < 35:
        confidence_cap = 65
        confidence_cap_reason = "confidence capped: moderate directional separation"
    elif score_gap_for_confidence < 50:
        confidence_cap = 75
        confidence_cap_reason = "confidence capped: direction not dominant enough"
    else:
        confidence_cap = 88

    try:
        result["confidence"] = min(
            confidence_cap,
            max(0, int(round(float(result.get("confidence") or 0))))
        )
    except (TypeError, ValueError):
        result["confidence"] = 0

    confidence_alignment_debug = {
        "buy_score": int(round(buy_score)),
        "sell_score": int(round(sell_score)),
        "score_gap": score_gap_for_confidence,
        "confidence_before_alignment": confidence_before_alignment,
        "confidence_cap": confidence_cap,
        "confidence_after_alignment": result.get("confidence"),
        "confidence_cap_reason": confidence_cap_reason,
    }

    if confidence_cap_reason:
        result["debug_reasons"] = (
            list(result.get("debug_reasons") or []) + [confidence_cap_reason]
        )[-14:]

    structure_debug = {
        "opposite_bos": bullish_bos or bearish_bos,
        "opposite_choch": bullish_choch or bearish_choch,
        "major_liquidity_grab": bool(
            score_debug.get("bullish_sweep")
            or score_debug.get("bearish_sweep")
            or result.get("liquidity_sweep")
        ),
        "structure_invalidation": bool(
            result.get("structure_invalidated")
            or result.get("pullback_block_active")
        ),
        "bullish_displacement": bullish_impulse,
        "bearish_displacement": bearish_impulse,
        "bullish_15m_structure": fifteen_m_direction == "BULLISH",
        "bearish_15m_structure": fifteen_m_direction == "BEARISH",
        "displacement_score": max(
            float(one_h_context.get("displacement_score") or 0),
            float(fifteen_m_context.get("displacement_score") or 0),
        ),
    }
    buy_score, sell_score, score_persistence_debug = apply_score_persistence(
        result,
        symbol,
        buy_score,
        sell_score,
        components,
        weights,
        structure_debug,
    )

    result["buy_pct"] = buy_score
    result["sell_pct"] = sell_score
    result["score_persistence_debug"] = score_persistence_debug
    result.update({
        "buy_score_before": score_persistence_debug["buy_score_before"],
        "sell_score_before": score_persistence_debug["sell_score_before"],
        "buy_score_after": score_persistence_debug["buy_score_after"],
        "sell_score_after": score_persistence_debug["sell_score_after"],
        "score_delta": score_persistence_debug["score_delta"],
        "largest_positive_factor": score_persistence_debug["largest_positive_factor"],
        "largest_negative_factor": score_persistence_debug["largest_negative_factor"],
        "bias_changed": score_persistence_debug["bias_changed"],
        "reason_for_bias_change": score_persistence_debug["reason_for_bias_change"],
    })
    result["score_cap_reason"] = score_reason
    result["score_weight_debug"] = {
        "weights": weights,
        "directions": {
            "one_h": one_h_direction,
            "fifteen_m": fifteen_m_direction,
            "five_m": five_m_direction,
            "momentum": momentum_direction,
        },
        "components": components,
        "raw_buy_score": round(raw_buy_score, 2),
        "raw_sell_score": round(raw_sell_score, 2),
        "target_buy_score_after_caps": score_persistence_debug["target_buy_score"],
        "target_sell_score_after_caps": score_persistence_debug["target_sell_score"],
        "final_buy_score": result["buy_pct"],
        "final_sell_score": result["sell_pct"],
    }
    result["score_cap_debug"] = {
        "caps": caps,
        "score_cap_reason": score_reason,
        "bearish_impulse": bearish_impulse,
        "bullish_recovery_complete": bullish_recovery_complete,
        "bullish_recovery_requirements": {
            "choch_up": bullish_choch,
            "close_above_key_level": close_above_key_level,
            "bullish_displacement": "BULLISH" in fifteen_m_displacement,
        },
        "sell_favored_bearish_displacement": sell_favored,
    }

    def strip_ema_debug(context):
        return {
            key: value
            for key, value in dict(context or {}).items()
            if "ema" not in str(key).lower()
        }

    result["timeframe_alignment_debug"] = {
        "one_h": strip_ema_debug(one_h_context),
        "fifteen_m": strip_ema_debug(fifteen_m_context),
        "five_m": strip_ema_debug(five_m_context),
        "one_h_fifteen_m_disagree": one_h_fifteen_m_disagree,
        "confidence_before_cap": before_confidence,
        "confidence_after_cap": result.get("confidence"),
        "confidence_alignment": confidence_alignment_debug,
    }
    if score_reason:
        result["debug_reasons"] = (
            list(result.get("debug_reasons") or []) + [score_reason]
        )[-14:]

    print("SCORE_WEIGHT_DEBUG =", result["score_weight_debug"])
    print("SCORE_CAP_DEBUG =", result["score_cap_debug"])
    print("TIMEFRAME_ALIGNMENT_DEBUG =", result["timeframe_alignment_debug"])
    print("CONFIDENCE_ALIGNMENT_DEBUG =", confidence_alignment_debug)
    return result

def apply_directional_wait_bias(result, one_h, symbol):
    bias = one_h.get("bias")
    shift = one_h.get("shift")
    structure_15m = get_15m_structure_side(result)
    one_h_directional_bias = bias or shift or "NEUTRAL"
    directional_bias_strength = 0

    if bias in ["BULLISH", "BEARISH"]:
        directional_bias_strength = 62
    elif shift in ["BULLISH", "BEARISH"]:
        one_h_directional_bias = shift
        directional_bias_strength = 55

    result["one_h_directional_bias"] = one_h_directional_bias
    result["directional_bias_strength"] = directional_bias_strength
    result["wait_reason"] = result.get("plan_reason") or "Waiting for BOS"

    if one_h_directional_bias in ["BULLISH", "BEARISH"]:
        result.setdefault("debug_reasons", [])
        result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
            f"1h directional bias: {one_h_directional_bias} ({directional_bias_strength})"
        ])[-14:]

    print("DIRECTIONAL_PERCENT_DEBUG =", {
        "symbol": symbol,
        "trend_1h": bias,
        "shift_1h": shift,
        "structure_15m": structure_15m,
        "buy_percentage_source": "real_15m_score",
        "sell_percentage_source": "real_15m_score",
        "final_buy_pct": result.get("buy_pct"),
        "final_sell_pct": result.get("sell_pct"),
        "final_confidence": result.get("confidence"),
        "one_h_directional_bias": result.get("one_h_directional_bias"),
        "directional_bias_strength": result.get("directional_bias_strength"),
    })

    return result

def release_signal_if_weighted_scores_reverse(result, symbol):
    weighted_signal = str(result.get("signal") or "WAIT").upper()

    try:
        weighted_buy_score = float(result.get("buy_pct") or 0)
        weighted_sell_score = float(result.get("sell_pct") or 0)
    except (TypeError, ValueError):
        weighted_buy_score = 0
        weighted_sell_score = 0

    score_reversed = (
        weighted_signal == "BUY"
        and weighted_sell_score > weighted_buy_score
    ) or (
        weighted_signal == "SELL"
        and weighted_buy_score > weighted_sell_score
    )

    if not score_reversed:
        return result

    setup_type = str(result.get("strategy_setup_type") or "").upper()
    if weighted_signal in ["BUY", "SELL"] and (
        setup_type.startswith("SMC_")
        or setup_type.endswith("_5M_ENTRY")
        or "15M_SWING" in setup_type
    ):
        result["score_conflict_advisory"] = (
            f"Weighted scores currently oppose {weighted_signal}, but SMC "
            "structure remains authoritative"
        )
        result["debug_reasons"] = (
            list(result.get("debug_reasons") or [])
            + [result["score_conflict_advisory"]]
        )[-14:]
        return result

    setup_type = str(result.get("strategy_setup_type") or "").upper()
    if setup_type.endswith("_5M_ENTRY") or "15M_MOMENTUM_DIRECT" in setup_type:
        result["score_conflict_advisory"] = (
            f"Weighted scores currently oppose {weighted_signal}, but the "
            "completed 15m breakout remains authoritative"
        )
        result["debug_reasons"] = (
            list(result.get("debug_reasons") or [])
            + [result["score_conflict_advisory"]]
        )[-14:]
        return result

    reversed_reason = (
        f"{weighted_signal} setup released because current weighted scores "
        "now favor the opposite side"
    )
    result = clear_trade_plan(result, reversed_reason)
    result = mark_signal_blocker(
        result,
        "score_direction_reversed",
        reversed_reason,
        "current_weighted_score_must_favor_signal",
        weighted_signal,
    )
    FINAL_SIGNAL_HOLD.pop(get_final_signal_hold_key(symbol), None)
    save_final_signal_hold()
    return result

def get_mtf_signal(data_5m, data_15m, data_1h, symbol):
    closed_data_5m = remove_current_forming_candle(data_5m, 5)
    closed_data_15m = remove_current_forming_candle(data_15m, 15)
    closed_data_1h = remove_current_forming_candle(data_1h, 60)
    removed_forming_15m_candle = (
        data_15m is not None
        and closed_data_15m is not None
        and len(data_15m) > len(closed_data_15m)
    )

    setup_result_15m = get_signal(
        closed_data_15m,
        closed_data_1h,
        symbol,
        state_key=f"{symbol}_15M_ENTRY"
    )
    one_h = detect_1h_trend_filter(closed_data_1h, symbol)
    lightweight_5m_signal = detect_lightweight_5m_confirmation(closed_data_5m, closed_data_15m, symbol)
    detected_15m_setup = get_15m_setup_side(setup_result_15m)
    swing_breaks = {
        side: evaluate_closed_15m_swing_break(
            closed_data_15m,
            side,
            symbol,
        )
        for side in ["BUY", "SELL"]
    }
    clean_15m_candidates = []
    clean_15m_rejections = []

    for side, swing_break in swing_breaks.items():
        if swing_break.get("confirmed"):
            clean_15m_candidates.append({
                "side": side,
                "swing_break": swing_break,
                "closed_candle_time": swing_break.get("closed_candle_time"),
            })
        else:
            clean_15m_rejections.append({
                "side": side,
                "swing_break": swing_break,
                "reason": swing_break.get("reason") or "WAIT_NO_15M_BREAK",
            })

    def clean_15m_candidate_time(item):
        try:
            return pd.Timestamp(item.get("closed_candle_time"))
        except Exception:
            return pd.Timestamp.min

    clean_15m_entry = (
        max(clean_15m_candidates, key=clean_15m_candidate_time)
        if clean_15m_candidates
        else None
    )
    pending_15m_setup = get_pending_15m_swing_setup(
        symbol,
        closed_data_5m,
    )
    if clean_15m_entry:
        clean_break = clean_15m_entry["swing_break"]
        pending_15m_setup = {
            "symbol": normalize_symbol(symbol),
            "side": clean_15m_entry["side"],
            "swing_level": clean_break.get("swing_level"),
            "break_candle_time": clean_break.get("closed_candle_time"),
            "break_close": clean_break.get("closed_candle_close"),
            "status": "DIRECT_15M_ENTRY",
            "expires_after_5m_candles": None,
            "candles_remaining": None,
            "direct_15m_entry": True,
        }
    if not pending_15m_setup:
        fallback_break = swing_breaks.get(detected_15m_setup) or {}
        fallback_key = get_15m_swing_watch_key(symbol, detected_15m_setup)
        if (
            detected_15m_setup in ["BUY", "SELL"]
            and fallback_break.get("confirmed")
            and fallback_key not in FIFTEEN_M_SWING_WATCH
        ):
            pending_15m_setup = {
                "symbol": normalize_symbol(symbol),
                "side": detected_15m_setup,
                "swing_level": fallback_break.get("swing_level"),
                "bos_time": fallback_break.get("closed_candle_time"),
                "bos_price": fallback_break.get("closed_candle_close"),
                "broken_swing_price": fallback_break.get("swing_level"),
                "setup_direction": detected_15m_setup,
                "break_candle_time": fallback_break.get(
                    "closed_candle_time"
                ),
                "break_close": fallback_break.get(
                    "closed_candle_close"
                ),
                "status": "PENDING",
                "expires_after_5m_candles": (
                    get_15m_pending_max_5m_candles(symbol)
                ),
            }
    fifteen_m_setup = (
        str(pending_15m_setup.get("side")).upper()
        if pending_15m_setup
        else "WAIT"
    )
    fifteen_m_swing_break = (
        {
            **swing_breaks.get(fifteen_m_setup, {}),
            "confirmed": True,
            "swing_level": pending_15m_setup.get("swing_level"),
            "closed_candle_time": pending_15m_setup.get("break_candle_time"),
            "closed_candle_close": pending_15m_setup.get("break_close"),
            "reason": (
                f"Pending {fifteen_m_setup} from completed 15m swing break"
            ),
            "pending_setup": copy.deepcopy(pending_15m_setup),
        }
        if pending_15m_setup
        else swing_breaks.get(detected_15m_setup, {
            "confirmed": False,
            "reason": "WAIT_NO_15M_BREAK",
        })
    )
    fifteen_m_bos_level = (
        pending_15m_setup.get("swing_level")
        if pending_15m_setup
        else get_15m_setup_level(setup_result_15m, detected_15m_setup)
    )
    fifteen_m_entry_level = (
        fifteen_m_swing_break.get("swing_level")
        if fifteen_m_swing_break.get("swing_level") is not None
        else fifteen_m_bos_level
    )
    fifteen_m_bos_level = fifteen_m_entry_level
    momentum_breakout_setup = str(
        setup_result_15m.get("strategy_setup_type") or ""
    ).upper() in [
        "BUY_MOMENTUM_BREAKOUT",
        "SELL_MOMENTUM_BREAKOUT",
    ]
    preliminary_five_m_entry = confirm_5m_entry_from_15m_setup(
        closed_data_5m,
        fifteen_m_setup,
        fifteen_m_entry_level,
        symbol,
        momentum_breakout=momentum_breakout_setup,
    )
    current_5m_entry_confirmation = is_current_5m_entry_confirmation(
        closed_data_5m,
        preliminary_five_m_entry,
        fifteen_m_setup,
    )
    fifteen_m_breakout_candle_time = (
        pending_15m_setup.get("break_candle_time")
        if pending_15m_setup
        else None
    )
    five_m_entry = confirm_5m_close_after_15m_break(
        closed_data_5m,
        fifteen_m_setup,
        fifteen_m_entry_level,
        fifteen_m_breakout_candle_time,
        symbol,
    )
    if eurusd_requires_fresh_5m_entry(symbol):
        current_5m_entry_confirmation = is_current_5m_entry_confirmation(
            closed_data_5m,
            five_m_entry,
            fifteen_m_setup,
        )
    if pending_15m_setup and (
        five_m_entry.get("invalidated")
        or five_m_entry.get("expired")
    ):
        pending_key = get_15m_swing_watch_key(symbol, fifteen_m_setup)
        watched = FIFTEEN_M_SWING_WATCH.get(pending_key)
        if isinstance(watched, dict):
            if five_m_entry.get("invalidated"):
                watched["status"] = "INVALIDATED"
                watched["invalidated_at"] = five_m_entry.get(
                    "closed_candle_time"
                )
                watched["invalidation_close"] = five_m_entry.get("close")
            else:
                watched["status"] = "EXPIRED"
                watched["expired_at"] = five_m_entry.get(
                    "closed_candle_time"
                )
            save_fifteen_m_swing_watch()
    five_m_signal = five_m_entry.get("side") or "WAIT"
    pullback_block_active = bool(
        fifteen_m_setup in ["BUY", "SELL"]
        and (
            five_m_entry.get("invalidated")
            or five_m_entry.get("expired")
        )
    )
    pullback_block_reason = (
        f"{fifteen_m_setup} blocked: pullback/expiry invalidated the 15m break; "
        f"wait for a new 15m {fifteen_m_setup} swing break"
        if pullback_block_active
        else None
    )
    setup_candle_time = setup_result_15m.get("setup_candle_time")
    confirmation_anchor_time = fifteen_m_breakout_candle_time or setup_candle_time
    five_m_after_setup = True

    try:
        if confirmation_anchor_time and five_m_entry.get("closed_candle_time"):
            setup_ts = pd.Timestamp(confirmation_anchor_time)
            five_ts = pd.Timestamp(five_m_entry.get("closed_candle_time"))

            if setup_ts.tzinfo is None:
                setup_ts = setup_ts.tz_localize("UTC")
            else:
                setup_ts = setup_ts.tz_convert("UTC")

            if five_ts.tzinfo is None:
                five_ts = five_ts.tz_localize("UTC")
            else:
                five_ts = five_ts.tz_convert("UTC")

            five_m_after_setup = five_ts > setup_ts
    except Exception:
        five_m_after_setup = False

    print("STRICT_CANDLE_CLOSE_DEBUG =", {
        "symbol": symbol,
        "raw_5m_rows": len(data_5m) if data_5m is not None else 0,
        "closed_5m_rows": len(closed_data_5m) if closed_data_5m is not None else 0,
        "raw_15m_rows": len(data_15m) if data_15m is not None else 0,
        "closed_15m_rows": len(closed_data_15m) if closed_data_15m is not None else 0,
        "raw_1h_rows": len(data_1h) if data_1h is not None else 0,
        "closed_1h_rows": len(closed_data_1h) if closed_data_1h is not None else 0,
        "setup_candle_time": setup_candle_time,
        "fifteen_m_breakout_candle_time": fifteen_m_breakout_candle_time,
        "five_m_closed_candle_time": five_m_entry.get("closed_candle_time"),
        "five_m_after_setup": five_m_after_setup,
        "preliminary_5m_confirmation": preliminary_five_m_entry,
    })
    original_15m_signal = str(setup_result_15m.get("signal") or "WAIT").upper()
    signal_before_mtf_filters = (
        fifteen_m_setup
        if fifteen_m_setup in ["BUY", "SELL"]
        else original_15m_signal
    )
    result = setup_result_15m

    # 15m structure is the only entry trigger. Older momentum text can still
    # affect confidence, but it must not create BUY/SELL without the explicit
    # closed 15m swing break below.
    plan_text_for_setup = " ".join([
        str(result.get("plan_type") or ""),
        str(result.get("plan_reason") or ""),
        str(result.get("strategy_setup_type") or ""),
        str(result.get("entry_timing") or ""),
    ]).upper()
    if (
        fifteen_m_setup not in ["BUY", "SELL"]
        and original_15m_signal in ["BUY", "SELL"]
        and swing_breaks.get(original_15m_signal, {}).get("confirmed")
        and (
            "MOMENTUM_BREAKOUT" in plan_text_for_setup
            or "MOMENTUM BREAKOUT" in plan_text_for_setup
            or "WITHOUT RETEST" in plan_text_for_setup
        )
    ):
        fifteen_m_setup = original_15m_signal
        signal_before_mtf_filters = original_15m_signal
        fifteen_m_swing_break = {
            **(fifteen_m_swing_break or {}),
            "confirmed": True,
            "break_happened": True,
            "close_confirmed": True,
            "reason": f"{original_15m_signal} 15m swing break accepted without retest",
            "retest_required": False,
        }
        result["blocked_by"] = None
        result["blocked_reason"] = None
        result["blocker_rule_name"] = None
        result["strategy_setup_complete"] = True
        result["strategy_setup_type"] = f"{original_15m_signal}_15M_SWING_BREAK"
        result["entry_timing"] = f"{original_15m_signal} 15M SWING CLOSE"
        result["plan_reason"] = (
            f"{original_15m_signal} accepted: "
            "15m swing break and close confirmed. Retest is not required."
        )
        print("NO_RETEST_15M_SETUP_ACCEPTED =", {
            "symbol": symbol,
            "side": original_15m_signal,
            "original_plan": plan_text_for_setup,
        })

    blocked_reason = None

    result["strategy_model"] = "1h trend / 15m setup / 5m entry"
    result["strategy_trend_timeframe"] = "1h"
    result["strategy_entry_timeframe"] = "5m"
    result["strategy_setup_timeframe"] = "15m"
    result["strategy_confirmation_timeframe"] = "5m closed candle"
    result["fifteen_m_uses_closed_candle_only"] = True
    result["fifteen_m_forming_candle_removed"] = removed_forming_15m_candle
    result["confirmation_5m"] = five_m_signal
    result["confirmation_5m_raw"] = lightweight_5m_signal
    result["current_5m_entry_confirmation"] = (
        current_5m_entry_confirmation
    )
    result["one_h_bias"] = one_h.get("bias")
    result["one_h_shift"] = one_h.get("shift")
    result["one_h_conflict_advisory"] = (
        fifteen_m_setup == "BUY"
        and not one_h.get("buy_allowed")
    ) or (
        fifteen_m_setup == "SELL"
        and not one_h.get("sell_allowed")
    )
    result["setup_timeframe_used"] = "15m"
    result["final_signal_source"] = "get_mtf_signal"
    result["fifteen_m_swing_break"] = fifteen_m_swing_break
    result["fifteen_m_swing_break_confirmed"] = bool(
        fifteen_m_swing_break.get("confirmed")
    )
    result["fifteen_m_swing_level"] = fifteen_m_swing_break.get("swing_level")
    result["fifteen_m_closed_candle_close"] = fifteen_m_swing_break.get(
        "closed_candle_close"
    )
    inherited_blocked_by = result.get("blocked_by")
    inherited_blocker_rule_name = result.get("blocker_rule_name")
    inherited_blocked_reason = result.get("blocked_reason")
    inherited_blocker_debug = None
    late_entry_debug = None

    print("ONE_H_CONFLICT_ADVISORY_DEBUG =", {
        "symbol": symbol,
        "fifteen_m_setup": fifteen_m_setup,
        "one_h_bias": one_h.get("bias"),
        "one_h_shift": one_h.get("shift"),
        "buy_allowed": one_h.get("buy_allowed"),
        "sell_allowed": one_h.get("sell_allowed"),
        "conflict_advisory": result["one_h_conflict_advisory"],
        "blocks_entry": False,
    })

    def score_favors_setup():
        try:
            buy_pct = float(result.get("buy_pct") or 0)
            sell_pct = float(result.get("sell_pct") or 0)
        except (TypeError, ValueError):
            buy_pct = 0
            sell_pct = 0

        if fifteen_m_setup == "BUY":
            return buy_pct > sell_pct
        if fifteen_m_setup == "SELL":
            return sell_pct > buy_pct

        return False

    def confidence_value():
        try:
            return float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            return 0

    def apply_5m_confirmed_signal():
        nonlocal late_entry_debug

        if not fifteen_m_swing_break.get("confirmed"):
            reason = fifteen_m_swing_break.get("reason") or "WAIT_NO_15M_CLOSE_CONFIRMATION"
            clear_trade_plan(result, reason)
            mark_signal_blocker(
                result,
                "missing_15m_swing_break",
                reason,
                "fifteen_m_closed_swing_break_required",
                fifteen_m_setup,
            )
            result["entry_timing"] = f"WAIT 15M {fifteen_m_setup} SWING CLOSE"
            return False

        five_m_valid = (
            five_m_entry.get("side") == fifteen_m_setup
            and five_m_entry.get("close_confirmed")
            and five_m_after_setup
        )
        print("SIMPLIFIED_5M_CONFIRMATION_DEBUG =", {
            "symbol": symbol,
            "side": fifteen_m_setup,
            "fifteen_m_level": fifteen_m_entry_level,
            "fifteen_m_break_time": fifteen_m_swing_break.get("closed_candle_time"),
            "five_m_side": five_m_entry.get("side"),
            "five_m_close": five_m_entry.get("close"),
            "five_m_close_time": five_m_entry.get("closed_candle_time"),
            "five_m_close_confirmed": five_m_entry.get("close_confirmed"),
            "five_m_after_setup": five_m_after_setup,
            "passed": bool(five_m_valid),
            "remembered_setup": bool(pending_15m_setup),
            "reason": five_m_entry.get("reason"),
        })
        if not five_m_valid:
            reason = (
                five_m_entry.get("reason")
                or f"Remembered 15m {fifteen_m_setup} setup; waiting for 5m confirmation"
            )
            clear_trade_plan(result, reason)
            mark_signal_blocker(
                result,
                "missing_5m_confirmation",
                reason,
                "five_m_directional_close_required",
                fifteen_m_setup,
            )
            result["entry_timing"] = "WAIT 5M CONFIRMATION"
            return False

        risk_levels = build_15m_swing_risk_levels(
            closed_data_15m,
            five_m_entry.get("close")
            or fifteen_m_swing_break.get("closed_candle_close"),
            fifteen_m_setup,
            symbol,
            setup_candle_time=fifteen_m_swing_break.get("closed_candle_time"),
        )
        print("SWING_SL_RR_DEBUG =", risk_levels)

        if not risk_levels.get("ok"):
            reason = risk_levels.get("reason") or "WAIT_INVALID_SWING_SL"
            clear_trade_plan(result, reason)
            mark_signal_blocker(
                result,
                "invalid_swing_sl" if reason == "WAIT_INVALID_SWING_SL" else "invalid_risk_reward",
                reason,
                "swing_sl_rr_required",
                fifteen_m_setup,
            )
            result["entry_timing"] = "WAIT VALID SWING SL"
            result["swing_sl_debug"] = risk_levels
            return False

        result["signal"] = fifteen_m_setup
        result["signal_before_filters"] = fifteen_m_setup
        result["plan_bias"] = fifteen_m_setup
        result["entry_quality"] = "SMC"
        result["entry_timing"] = (
            "5M CLOSED CONFIRMATION"
            if five_m_entry.get("close_confirmed")
            else "15M SWING CLOSE"
        )
        result["plan_type"] = (
            f"SMC {fifteen_m_setup} 5M CONFIRMED"
            if five_m_entry.get("close_confirmed")
            else f"SMC {fifteen_m_setup} 15M CONFIRMED"
        )
        result["plan_reason"] = (
            f"{fifteen_m_setup}: 15m BOS/CHOCH close + 5m directional close confirmed"
        )
        result["strategy_setup_complete"] = True
        result["strategy_setup_type"] = (
            f"{fifteen_m_setup}_5M_ENTRY"
            if five_m_entry.get("close_confirmed")
            else f"{fifteen_m_setup}_15M_SWING_ENTRY"
        )
        result["blocker_rule_name"] = None
        result["blocked_by"] = None
        result["blocked_reason"] = None

        try:
            normalized_symbol = normalize_symbol(symbol)
            decimals = get_strategy_decimals(normalized_symbol)
            entry_price = risk_levels["entry"]
            result["entry_price"] = entry_price
            result["price"] = entry_price
            result["stop_loss"] = risk_levels["stop_loss"]
            result["tp1"] = risk_levels["tp1"]
            result["tp2"] = risk_levels["tp2"]
            result["protected_sl_price"] = round(
                calculate_protected_sl_from_tp2_price(
                    entry_price,
                    risk_levels["tp2"],
                    fifteen_m_setup,
                ),
                decimals,
            )
            result["risk_reward"] = risk_levels.get("risk_reward")
            result["swing_sl_debug"] = risk_levels

            result["level_source"] = "15m swing"
            result["tp1_rule"] = "80% of entry-to-TP2"
            result["tp2_rule"] = "15m opposite swing target within 1.20R-2.00R"
            result["protected_sl_rule"] = "50% of entry-to-TP2 after TP1"
        except Exception as exc:
            print("TIMEFRAME_ENTRY_LEVEL_WARNING =", {
                "symbol": symbol,
                "error": str(exc),
            })

        timing_label = "5m close" if five_m_entry.get("close_confirmed") else "15m close"
        result["signal_text"] = f"{fifteen_m_setup} {'🟢' if fifteen_m_setup == 'BUY' else '🔴'} ({result.get('confidence', 0)}% | {timing_label})"
        result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
            f"{fifteen_m_setup} allowed: 15m swing break + close confirmed"
        ])[-14:]
        return True

    momentum_15m = evaluate_smc_15m_momentum(
        result,
        one_h,
        closed_data_15m,
        fifteen_m_setup,
        symbol,
        fifteen_m_setup,
    )
    result["momentum_15m_debug"] = momentum_15m
    continuation_debug = {
        "blocked_by": momentum_15m.get("blocked_by"),
        "blocker_rule_name": momentum_15m.get("blocker_rule_name"),
        "side": momentum_15m.get("side"),
        "reason": momentum_15m.get("reason"),
    }
    result["CONTINUATION_DEBUG"] = continuation_debug
    print("CONTINUATION_DEBUG =", continuation_debug)
    strong_momentum_entry = False

    # SIMPLE ENTRY MODEL:
    # If 15m structure says BUY/SELL and the 15m swing break closed,
    # keep structure authoritative. Confidence, score, and HTF are descriptive
    # only; they must not turn a clean 15m entry back into WAIT.
    fifteen_m_break_confirmed = bool(
        fifteen_m_swing_break.get("confirmed")
        or (
            fifteen_m_swing_break.get("break_happened")
            and fifteen_m_swing_break.get("close_confirmed")
        )
    )
    direct_15m_momentum_entry = (
        SIMPLE_MOMENTUM_DIRECT_15M_ENTRY
        and fifteen_m_setup in ["BUY", "SELL"]
        and fifteen_m_break_confirmed
        and str(result.get("fake_breakout") or "NONE").upper() == "NONE"
    )
    print("SIMPLE_15M_ENTRY_GATE_DEBUG =", {
        "symbol": symbol,
        "fifteen_m_setup": fifteen_m_setup,
        "break_happened": fifteen_m_swing_break.get("break_happened"),
        "close_confirmed": fifteen_m_swing_break.get("close_confirmed"),
        "confirmed": fifteen_m_swing_break.get("confirmed"),
        "fake_breakout": result.get("fake_breakout"),
        "score_favors_setup_warning_only": score_favors_setup(),
        "confidence_warning_only": confidence_value(),
        "five_m_confirmation_warning_only": current_5m_entry_confirmation,
        "allowed": direct_15m_momentum_entry,
    })

    if strong_momentum_entry:
        momentum_side = fifteen_m_setup
        momentum_direction = "bullish" if momentum_side == "BUY" else "bearish"
        result["confidence"] = int(round(momentum_15m["confidence"]))
        momentum_entry = momentum_15m.get("price")
        risk_levels = build_15m_swing_risk_levels(
            closed_data_15m,
            momentum_entry,
            momentum_side,
            symbol,
            setup_candle_time=fifteen_m_swing_break.get("closed_candle_time"),
        )

        if not risk_levels.get("ok"):
            held_setup = FINAL_SIGNAL_HOLD.get(
                get_final_signal_hold_key(symbol)
            ) or {}
            held_fields = held_setup.get("fields") or {}
            held_levels = {
                "entry_price": held_fields.get("entry_price") or held_setup.get("entry"),
                "stop_loss": held_fields.get("stop_loss") or held_setup.get("sl"),
                "tp1": held_fields.get("tp1") or held_setup.get("tp1"),
                "tp2": held_fields.get("tp2") or held_setup.get("tp2"),
            }
            held_levels_ok, _held_reason = validate_trade_levels_1_to_2(
                held_levels,
                momentum_side,
            )
            if (
                str(held_setup.get("signal") or "").upper() == momentum_side
                and held_levels_ok
            ):
                risk_levels = {
                    "ok": True,
                    "reason": None,
                    "entry": float(held_levels["entry_price"]),
                    "stop_loss": float(held_levels["stop_loss"]),
                    "tp1": float(held_levels["tp1"]),
                    "tp2": float(held_levels["tp2"]),
                    "risk_reward": "1:2",
                    "level_source": f"validated held {momentum_side} setup",
                }
                print(f"SMC_{momentum_side}_MOMENTUM_HELD_LEVELS_REUSED =", {
                    "symbol": symbol,
                    **risk_levels,
                })

        if risk_levels.get("ok"):
            result["signal"] = momentum_side
            result["signal_before_filters"] = momentum_side
            result["plan_bias"] = momentum_side
            result["entry_quality"] = "SMC"
            result["entry_timing"] = "15M MOMENTUM ENTRY"
            result["plan_type"] = f"SMC {momentum_side} 15M MOMENTUM"
            result["plan_reason"] = (
                f"Strong {momentum_direction} continuation: HTF and 15m "
                f"{momentum_direction} with CHOCH/BOS confirmation"
            )
            result["strategy_setup_complete"] = True
            result["strategy_setup_type"] = (
                f"SMC_{momentum_side}_15M_MOMENTUM"
            )
            result["blocked_by"] = None
            result["blocked_reason"] = None
            result["blocker_rule_name"] = None
            result["blocker_trace"] = []
            result["entry_price"] = risk_levels["entry"]
            result["price"] = risk_levels["entry"]
            result["stop_loss"] = risk_levels["stop_loss"]
            result["tp1"] = risk_levels["tp1"]
            result["tp2"] = risk_levels["tp2"]
            result["risk_reward"] = "1:2"
            result["swing_sl_debug"] = risk_levels
            result["signal_text"] = (
                f"{momentum_side} "
                f"{'🟢' if momentum_side == 'BUY' else '🔴'} "
                f"({result.get('confidence', 0)}% | 15m momentum)"
            )
            result["debug_reasons"] = (
                list(result.get("debug_reasons") or [])
                + [f"SMC_{momentum_side}_15M_MOMENTUM: retest not required"]
            )[-14:]
            print(
                f"SMC_{momentum_side}_15M_MOMENTUM_DEBUG =",
                momentum_15m,
            )
        else:
            blocked_reason = risk_levels.get("reason") or "WAIT_INVALID_SWING_SL"
            result = clear_trade_plan(result, blocked_reason)
            result = mark_signal_blocker(
                result,
                "invalid_swing_sl",
                blocked_reason,
                "swing_sl_rr_required",
                momentum_side,
            )

    elif direct_15m_momentum_entry:
        momentum_entry = momentum_15m.get("price") or result.get("price")
        risk_levels = build_15m_swing_risk_levels(
            closed_data_15m,
            momentum_entry,
            fifteen_m_setup,
            symbol,
            setup_candle_time=fifteen_m_swing_break.get("closed_candle_time"),
        )
        print("DIRECT_15M_MOMENTUM_RISK_DEBUG =", {
            "symbol": symbol,
            "side": fifteen_m_setup,
            "entry": momentum_entry,
            **risk_levels,
        })

        if not risk_levels.get("ok"):
            blocked_reason = risk_levels.get("reason") or "WAIT_INVALID_SWING_SL"
            result = clear_trade_plan(result, blocked_reason)
            result = mark_signal_blocker(
                result,
                "invalid_swing_sl",
                blocked_reason,
                "direct_15m_momentum_swing_sl_rr_required",
                fifteen_m_setup,
            )
            result["entry_timing"] = "WAIT VALID 15M MOMENTUM SL"
            result["swing_sl_debug"] = risk_levels
        else:
            result["signal"] = fifteen_m_setup
            result["signal_before_filters"] = fifteen_m_setup
            result["plan_bias"] = fifteen_m_setup
            result["entry_quality"] = "SIMPLE_MOMENTUM"
            result["entry_timing"] = "15M MOMENTUM ENTRY"
            result["plan_type"] = f"SMC {fifteen_m_setup} 15M MOMENTUM"
            result["plan_reason"] = (
                f"{fifteen_m_setup} allowed from simple 15m structure: "
                "15m swing break + 15m close confirmed. 5m/score/confidence are warnings only."
            )
            result["strategy_setup_complete"] = True
            result["strategy_setup_type"] = f"SMC_{fifteen_m_setup}_15M_MOMENTUM_DIRECT"
            result["blocked_by"] = None
            result["blocked_reason"] = None
            result["blocker_rule_name"] = None
            result["blocker_trace"] = []
            result["entry_price"] = risk_levels["entry"]
            result["price"] = risk_levels["entry"]
            result["stop_loss"] = risk_levels["stop_loss"]
            result["tp1"] = risk_levels["tp1"]
            result["tp2"] = risk_levels["tp2"]
            result["risk_reward"] = "1:2"
            result["risk_percent"] = 0.5
            result["target_percent"] = 1.0
            result["swing_sl_debug"] = risk_levels
            result["level_source"] = "15m swing direct momentum"
            result["signal_text"] = f"{fifteen_m_setup} {'🟢' if fifteen_m_setup == 'BUY' else '🔴'} ({result.get('confidence', 0)}% | 15m BOS)"
            result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
                f"{fifteen_m_setup} direct 15m momentum entry: 5m confirmation not required"
            ])[-14:]
            print("DIRECT_15M_MOMENTUM_ENTRY_DEBUG =", {
                "symbol": symbol,
                "side": fifteen_m_setup,
                "original_15m_signal": original_15m_signal,
                "swing_break_confirmed": fifteen_m_swing_break.get("confirmed"),
                "fake_breakout": result.get("fake_breakout"),
                "score_favors_setup": score_favors_setup(),
                "confidence": confidence_value(),
                "current_5m_entry_confirmation": current_5m_entry_confirmation,
                "allowed": True,
                "risk_levels": risk_levels,
            })

    elif (
        fifteen_m_setup in ["BUY", "SELL"]
        and not fifteen_m_swing_break.get("confirmed")
    ):
        blocked_reason = fifteen_m_swing_break.get("reason") or "WAIT_NO_15M_CLOSE_CONFIRMATION"
        result = clear_trade_plan(result, blocked_reason)
        result = mark_signal_blocker(
            result,
            "missing_15m_swing_break",
            blocked_reason,
            "fifteen_m_closed_swing_break_required",
            fifteen_m_setup,
        )
        result["entry_timing"] = f"WAIT 15M {fifteen_m_setup} SWING CLOSE"

    elif fifteen_m_setup not in ["BUY", "SELL"]:
        existing_signal = str(result.get("signal") or "").upper()
        blocked_reason = (
            result.get("plan_reason")
            if existing_signal in ["BUY", "SELL"]
            else "Waiting for fresh 15m BOS/CHOCH setup"
        )
        result = clear_trade_plan(result, blocked_reason)
        result = mark_signal_blocker(
            result,
            "missing_15m_setup",
            blocked_reason,
            "fifteen_m_setup_required",
            signal_before_mtf_filters
        )
        result["entry_timing"] = "WAIT 15M SETUP"

    elif five_m_signal != fifteen_m_setup or not five_m_after_setup:
        apply_5m_confirmed_signal()

    else:
        inherited_blocker_debug = {
            "symbol": symbol,
            "fifteen_m_setup": fifteen_m_setup,
            "five_m_signal": five_m_signal,
            "five_m_closed_candle_confirmed": bool(
                five_m_entry.get("close_confirmed")
            ),
            "inherited_blocked_by": inherited_blocked_by,
            "inherited_blocker_rule_name": inherited_blocker_rule_name,
            "inherited_blocked_reason": inherited_blocked_reason,
            "override_allowed": True,
            "override_denied_reason": None,
            "final_signal": fifteen_m_setup,
        }
        print("INHERITED_BLOCKER_OVERRIDE_DEBUG =", inherited_blocker_debug)
        apply_5m_confirmed_signal()

    hold_key = get_final_signal_hold_key(symbol)
    current_final_signal = str(result.get("signal") or "WAIT").upper()
    freshness_candle_time = (
        setup_candle_time
        or five_m_entry.get("closed_candle_time")
    )
    signal_hold_debug = {
        "symbol": hold_key,
        "incoming_signal": current_final_signal,
        "fifteen_m_setup": fifteen_m_setup,
        "held_signal": None,
        "hold_active": False,
        "release_reason": None,
    }

    if current_final_signal in ["BUY", "SELL"]:
        previous_setup = FINAL_SIGNAL_HOLD.get(hold_key)
        clean_15m_direct_active = (
            "15M_MOMENTUM_DIRECT" in str(
                result.get("strategy_setup_type") or ""
            ).upper()
        )
        momentum_continuation_active = (
            current_final_signal in ["BUY", "SELL"]
            and (
                clean_15m_direct_active
                or (
                    result.get("strategy_setup_type")
                    == f"SMC_{current_final_signal}_15M_MOMENTUM"
                    and bool(
                        (result.get("momentum_15m_debug") or {}).get("allowed")
                    )
                )
            )
        )
        confirmed_15m_bos_active = (
            current_final_signal in ["BUY", "SELL"]
            and fifteen_m_setup == current_final_signal
            and fifteen_m_break_confirmed
        )

        if momentum_continuation_active or confirmed_15m_bos_active:
            freshness = {
                "fresh": True,
                "setup_freshness": "FRESH",
                "reason": (
                    f"{current_final_signal} 15m BOS setup is fresh; "
                    "legacy retest/hold freshness does not override "
                    f"{current_final_signal}"
                ),
            }
        else:
            freshness = evaluate_setup_freshness(
                previous_setup,
                current_final_signal,
                fifteen_m_bos_level,
                freshness_candle_time,
                closed_data_5m,
                setup_result_15m,
            )
        result["setup_freshness"] = freshness["setup_freshness"]
        result["setup_freshness_reason"] = freshness["reason"]
        signal_hold_debug["freshness"] = freshness

        if freshness["fresh"]:
            FINAL_SIGNAL_HOLD[hold_key] = {
                "signal": current_final_signal,
                "fifteen_m_setup": fifteen_m_setup,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "symbol": hold_key,
                "bos_level": fifteen_m_bos_level,
                "setup_candle_time": freshness_candle_time,
                "entry": result.get("entry_price"),
                "sl": result.get("stop_loss"),
                "tp1": result.get("tp1"),
                "tp2": result.get("tp2"),
                "setup_level": fifteen_m_bos_level,
                "five_m_candle_time": five_m_entry.get("closed_candle_time"),
                "setup_freshness": "ACTIVE",
                "fields": {
                    key: copy.deepcopy(result.get(key))
                    for key in [
                        "signal",
                        "signal_text",
                        "plan_bias",
                        "plan_type",
                        "entry_quality",
                        "entry_timing",
                        "plan_reason",
                        "strategy_setup_complete",
                        "strategy_setup_type",
                        "entry_price",
                        "stop_loss",
                        "tp1",
                        "tp2",
                        "risk_reward",
                        "price",
                    ]
                },
            }
            save_final_signal_hold()
        else:
            hold_expiration = evaluate_setup_hold_expiration(
                previous_setup,
                five_m_entry.get("closed_candle_time"),
                five_m_entry.get("close") or result.get("price"),
            )
            signal_hold_debug["hold_expiration"] = hold_expiration

            if hold_expiration["expired"]:
                previous_setup["setup_freshness"] = "EXPIRED"
                previous_setup["expired_at"] = datetime.now(timezone.utc).isoformat()
                previous_setup["expiration_reason"] = hold_expiration["reason"]
                FINAL_SIGNAL_HOLD[hold_key] = previous_setup
                save_final_signal_hold()
                result = clear_trade_plan(result, hold_expiration["reason"])
                result["setup_freshness"] = "EXPIRED"
                result["setup_freshness_reason"] = hold_expiration["reason"]
                result["signal_before_filters"] = current_final_signal
            else:
                result["signal"] = f"HOLD {current_final_signal}"
                result["signal_before_filters"] = current_final_signal
                result["plan_bias"] = current_final_signal
                result["plan_type"] = f"HOLD {current_final_signal}"
                result["entry_timing"] = "WAIT FOR FRESH SETUP"
                result["plan_reason"] = freshness["reason"]
                result["strategy_setup_complete"] = False
                result["strategy_setup_type"] = f"{current_final_signal}_SETUP_ACTIVE"
                result["signal_text"] = f"HOLD {current_final_signal} ({result.get('confidence', 0)}% | no fresh setup)"
                result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
                    freshness["reason"]
                ])[-14:]

        signal_hold_debug["held_signal"] = current_final_signal
        signal_hold_debug["hold_active"] = True

    else:
        persisted_hold = load_final_signal_hold()

        if hold_key not in persisted_hold and hold_key in FINAL_SIGNAL_HOLD:
            FINAL_SIGNAL_HOLD.pop(hold_key, None)

        held = FINAL_SIGNAL_HOLD.get(hold_key)

        if held:
            held_signal = str(held.get("signal") or "").upper()
            signal_hold_debug["held_signal"] = held_signal
            signal_hold_debug["release_reason"] = (
                "stale held setup ignored; waiting for a fresh 15m BOS"
            )
            FINAL_SIGNAL_HOLD.pop(hold_key, None)
            save_final_signal_hold()
            result["signal_hold_active"] = False
            result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
                f"Old HOLD {held_signal} cleared; fresh 15m BOS can create a new setup"
            ])[-14:]

    print("FINAL_SIGNAL_HOLD_DEBUG =", signal_hold_debug)

    current_result_signal = str(result.get("signal") or "WAIT").upper()

    if current_result_signal in ["BUY", "SELL"]:
        levels_ok, level_reason = validate_trade_levels_1_to_2(
            result,
            current_result_signal,
        )

        if not levels_ok:
            result = clear_trade_plan(result, level_reason)
            result = mark_signal_blocker(
                result,
                "trade_levels_invalid",
                level_reason,
                "tp_sl_rr_required",
                current_result_signal,
            )
        else:
            try:
                entry_value = float(result.get("entry_price"))
                sl_value = float(result.get("stop_loss"))
                tp_value = float(result.get("tp2"))
                risk_value = abs(entry_value - sl_value)
                reward_value = abs(tp_value - entry_value)
                rr_value = reward_value / risk_value if risk_value > 0 else 0
                result["risk_reward"] = f"1:{round(rr_value, 2):g}"
                result["risk_reward_ratio"] = round(rr_value, 4)
            except (TypeError, ValueError):
                result["risk_reward"] = result.get("risk_reward") or "1:1.2-2"
            result["risk_percent"] = 0.5
            result["target_percent"] = 1.0

    # Display scores are calculated only after execution decisions are complete.
    # This keeps the existing WAIT/BUY/SELL gates authoritative.
    result = apply_weighted_timeframe_scores(
        result,
        one_h,
        closed_data_1h,
        closed_data_15m,
        closed_data_5m,
        five_m_signal,
        symbol,
    )

    result = release_signal_if_weighted_scores_reverse(result, symbol)

    post_score_signal = str(result.get("signal") or "WAIT").upper()
    if post_score_signal in ["BUY", "SELL"]:
        result = enforce_15m_entry_requirements(
            result,
            post_score_signal,
            fifteen_m_setup,
            fifteen_m_swing_break,
            pullback_block_active=pullback_block_active,
            pullback_block_reason=pullback_block_reason,
        )

    if result.get("signal") not in ["BUY", "SELL", "HOLD BUY", "HOLD SELL"]:
        result["signal"] = "WAIT"
        result = apply_directional_wait_bias(result, one_h, symbol)
        print(
            "FINAL_WAIT",
            result.get("buy_pct"),
            result.get("sell_pct"),
            result.get("confidence"),
        )

    result["structure_trend"] = f"1h {one_h.get('bias', 'NEUTRAL')}"
    result["structure_type"] = f"15m {result.get('structure_type', 'NEUTRAL')}"
    result["structure_next"] = f"5m confirmation: {five_m_signal}"
    result["fifteen_m_setup"] = fifteen_m_setup
    result["fifteen_m_bos_level"] = fifteen_m_entry_level
    result["pending_15m_setup"] = copy.deepcopy(pending_15m_setup)
    result["fifteen_m_breakout_candle_time"] = (
        pending_15m_setup.get("break_candle_time")
        if pending_15m_setup
        else None
    )
    result["fifteen_m_setup_expiration"] = (
        pending_15m_setup.get("expires_at")
        if pending_15m_setup
        else None
    )
    result["fifteen_m_setup_candles_remaining"] = (
        pending_15m_setup.get("candles_remaining")
        if pending_15m_setup
        else None
    )
    result["fifteen_m_swing_break"] = fifteen_m_swing_break
    result["fifteen_m_swing_break_confirmed"] = bool(
        fifteen_m_swing_break.get("confirmed")
    )
    result["fifteen_m_swing_level"] = fifteen_m_swing_break.get("swing_level")
    result["fifteen_m_closed_candle_close"] = fifteen_m_swing_break.get(
        "closed_candle_close"
    )
    result["five_m_closed_candle_time"] = five_m_entry.get("closed_candle_time")
    result["five_m_close_confirmed"] = five_m_entry.get("close_confirmed")
    result["current_5m_entry_confirmation"] = (
        current_5m_entry_confirmation
    )
    result["five_m_after_setup"] = five_m_after_setup
    if (
        result.get("signal") in ["BUY", "SELL"]
        and "15M_MOMENTUM" in str(
            result.get("strategy_setup_type") or ""
        ).upper()
    ):
        result["entry_timeframe_used"] = "15m"
    else:
        result["entry_timeframe_used"] = (
            "5m" if result.get("signal") in ["BUY", "SELL"] else None
        )
    result["setup_freshness"] = result.get("setup_freshness") or (
        "FRESH" if result.get("signal") in ["BUY", "SELL"] else "EXPIRED"
    )
    result["debug_reasons"] = (list(result.get("debug_reasons") or []) + [
        f"Model: 1h={one_h.get('bias')} shift={one_h.get('shift')}; 15m_setup={fifteen_m_setup}; 5m_entry={five_m_signal}"
    ])[-14:]
    result["signal_diagnostics"] = {
        "final_signal": result.get("signal"),
        "signal_before_filters": result.get("signal_before_filters"),
        "blocked": result.get("signal") not in ["BUY", "SELL"],
        "blocked_by": result.get("blocked_by"),
        "blocked_reason": result.get("blocked_reason") or result.get("plan_reason"),
        "blocker_rule_name": result.get("blocker_rule_name"),
        "blocker_trace": list(result.get("blocker_trace") or []),
        "one_h_bias": one_h.get("bias"),
        "fifteen_m_setup": fifteen_m_setup,
        "fifteen_m_swing_break_confirmed": bool(
            fifteen_m_swing_break.get("confirmed")
        ),
        "five_m_signal": five_m_signal,
        "five_m_close_confirmed": five_m_entry.get("close_confirmed"),
        "five_m_after_setup": five_m_after_setup,
        "confidence": result.get("confidence"),
        "buy_pct": result.get("buy_pct"),
        "sell_pct": result.get("sell_pct"),
    }
    normalized_final_signal = str(result.get("signal") or "WAIT").upper()
    if normalized_final_signal not in ["BUY", "SELL"]:
        normalized_final_signal = "WAIT"
    bos_detected = bool(
        fifteen_m_swing_break.get("break_happened")
        or fifteen_m_swing_break.get("confirmed")
    )
    swing_structure_text = " ".join([
        str(fifteen_m_swing_break.get("structure_type") or ""),
        str(fifteen_m_swing_break.get("break_type") or ""),
        str(fifteen_m_swing_break.get("type") or ""),
        str(result.get("structure_type") or ""),
        str(result.get("strategy_setup_type") or ""),
    ]).upper()
    choch_detected = "CHOCH" in swing_structure_text
    smc_direction = (
        fifteen_m_setup
        if fifteen_m_setup in ["BUY", "SELL"] and bos_detected
        else "WAIT"
    )
    trend_bias = str(one_h.get("bias") or "NEUTRAL").lower()
    entry_reason = (
        result.get("plan_reason")
        if normalized_final_signal in ["BUY", "SELL"]
        else None
    )
    block_reason = (
        result.get("blocked_reason")
        or result.get("setup_freshness_reason")
        or result.get("plan_reason")
        or "Waiting for fresh 15m BOS"
    )
    signal_after_filters = normalized_final_signal

    result.update({
        "final_signal": normalized_final_signal,
        "signal_after_filters": signal_after_filters,
        "blocked": normalized_final_signal not in ["BUY", "SELL"],
        "blocked_by": result.get("blocked_by"),
        "block_reason": (
            None if normalized_final_signal in ["BUY", "SELL"] else block_reason
        ),
        "blocked_reason": (
            None if normalized_final_signal in ["BUY", "SELL"] else block_reason
        ),
        "entry_reason": entry_reason,
        "bos_detected": bos_detected,
        "choch_detected": choch_detected,
        "smc_direction": smc_direction,
        "fifteen_m_close_confirmed": bool(
            fifteen_m_swing_break.get("close_confirmed")
            or fifteen_m_swing_break.get("confirmed")
        ),
        "five_m_confirmation": bool(
            five_m_entry.get("close_confirmed")
            and five_m_after_setup
        ),
        "pullback_block_active": pullback_block_active,
        "trend_bias": trend_bias,
        "entry": result.get("entry_price"),
        "sl": result.get("stop_loss"),
        "risk_percent": result.get("risk_percent") or (
            0.5 if normalized_final_signal in ["BUY", "SELL"] else None
        ),
        "allowed_risk_percent": None,
        "calculated_lots": None,
        "rounded_lots": None,
        "broker_min_volume": None,
        "broker_volume_step": None,
        "final_risk_percent": None,
        "target_percent": result.get("target_percent") or (
            1.0 if normalized_final_signal in ["BUY", "SELL"] else None
        ),
    })
    result["signal_diagnostics"].update({
        "final_signal": normalized_final_signal,
        "blocked": result["blocked"],
        "block_reason": result["block_reason"],
        "entry_reason": result["entry_reason"],
        "bos_detected": bos_detected,
        "choch_detected": choch_detected,
        "smc_direction": smc_direction,
        "trend_bias": trend_bias,
        "entry": result.get("entry_price"),
        "sl": result.get("stop_loss"),
        "tp1": result.get("tp1"),
        "tp2": result.get("tp2"),
    })

    continuation_reasons = list(result.get("debug_reasons") or [])
    for blocker in result.get("blocker_trace") or []:
        blocker_reason = blocker.get("blocked_reason") if isinstance(blocker, dict) else None
        if blocker_reason and blocker_reason not in continuation_reasons:
            continuation_reasons.append(blocker_reason)
    CONTINUATION_DEBUG = {
        "final_signal": result.get("signal"),
        "fifteen_m_setup": fifteen_m_setup,
        "side": momentum_15m.get("side"),
        "score": (
            result.get("buy_pct")
            if fifteen_m_setup == "BUY"
            else result.get("sell_pct")
        ),
        "confidence": result.get("confidence"),
        "conditions": momentum_15m.get("conditions"),
        "htf_structure": momentum_15m.get("htf_structure") or one_h.get("bias"),
        "breakout_entry_allowed": bool(
            momentum_15m.get("breakout_entry_allowed")
        ),
        "reasons": continuation_reasons,
        "blocked_by": (
            momentum_15m.get("blocked_by")
            or result.get("blocked_by")
        ),
        "blocker_rule_name": (
            momentum_15m.get("blocker_rule_name")
            or result.get("blocker_rule_name")
        ),
        "reason": (
            momentum_15m.get("reason")
            or result.get("blocked_reason")
            or result.get("plan_reason")
        ),
    }
    result["CONTINUATION_DEBUG"] = CONTINUATION_DEBUG
    print("CONTINUATION_DEBUG =", CONTINUATION_DEBUG)

    if (
        fifteen_m_setup in ["BUY", "SELL"]
        and result.get("signal")
        not in [fifteen_m_setup, f"HOLD {fifteen_m_setup}"]
    ):
        print(
            f"BLOCKED BY: "
            f"{CONTINUATION_DEBUG.get('blocker_rule_name') or CONTINUATION_DEBUG.get('blocked_by') or 'unknown_continuation_wait'}"
        )

    print("TIMEFRAME_DECISION_DEBUG =", {
        "symbol": symbol,
        "one_h_bias": one_h.get("bias"),
        "fifteen_m_setup": fifteen_m_setup,
        "fifteen_m_bos_level": fifteen_m_bos_level,
        "fifteen_m_swing_break": fifteen_m_swing_break,
        "fifteen_m_fvg": result.get("fvg"),
        "five_m_signal": five_m_signal,
        "five_m_closed_candle_time": five_m_entry.get("closed_candle_time"),
        "five_m_close_confirmed": five_m_entry.get("close_confirmed"),
        "five_m_after_setup": five_m_after_setup,
        "entry_timeframe_used": result.get("entry_timeframe_used"),
        "final_signal": result.get("signal"),
        "blocked_reason": result.get("blocked_reason") or blocked_reason,
        "inherited_blocker_override": inherited_blocker_debug,
        "signal_hold": signal_hold_debug,
        "late_entry_guard": late_entry_debug,
    })

    final_entry_decision = str(result.get("signal") or "WAIT").upper()
    if final_entry_decision not in ["BUY", "SELL"]:
        final_entry_decision = "WAIT"

    validation_side = (
        final_entry_decision
        if final_entry_decision in ["BUY", "SELL"]
        else str(result.get("signal_before_filters") or fifteen_m_setup or "").upper()
    )

    def valid_strategy_price(value):
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    entry_value = result.get("entry_price")
    sl_value = result.get("stop_loss")
    tp1_value = result.get("tp1")
    tp2_value = result.get("tp2")
    sl_valid = False
    tp1_valid = False
    tp2_valid = False

    if all(valid_strategy_price(value) for value in [
        entry_value,
        sl_value,
        tp1_value,
        tp2_value,
    ]):
        entry_number = float(entry_value)
        sl_number = float(sl_value)
        tp1_number = float(tp1_value)
        tp2_number = float(tp2_value)
        if validation_side == "BUY":
            sl_valid = sl_number < entry_number
            tp1_valid = entry_number < tp1_number <= tp2_number
            tp2_valid = tp2_number > entry_number
        elif validation_side == "SELL":
            sl_valid = sl_number > entry_number
            tp1_valid = entry_number > tp1_number >= tp2_number
            tp2_valid = tp2_number < entry_number

    actionable_buy_debug = (
        (swing_breaks.get("BUY") or {}).get("actionable_swing_debug")
        or {}
    )
    actionable_sell_debug = (
        (swing_breaks.get("SELL") or {}).get("actionable_swing_debug")
        or {}
    )
    actionable_buy_level = (
        (swing_breaks.get("BUY") or {}).get("swing_level")
        or (actionable_buy_debug.get("selected") or {}).get("price")
    )
    actionable_sell_level = (
        (swing_breaks.get("SELL") or {}).get("swing_level")
        or (actionable_sell_debug.get("selected") or {}).get("price")
    )
    actionable_levels_message = None
    if actionable_buy_level is None and actionable_sell_level is None:
        actionable_levels_message = "No actionable 15m swing level within ATR range"

    saved_15m_setups = {}
    expired_15m_setup = None
    for saved_side in ["BUY", "SELL"]:
        saved_key = get_15m_swing_watch_key(symbol, saved_side)
        saved_setup = FIFTEEN_M_SWING_WATCH.get(saved_key)
        if not isinstance(saved_setup, dict):
            continue

        saved_setup_debug = {
            "symbol": saved_setup.get("symbol"),
            "side": saved_side,
            "status": str(saved_setup.get("status") or "").upper(),
            "swing_level": saved_setup.get("swing_level"),
            "break_confirmed": bool(saved_setup.get("break_confirmed")),
            "break_candle_time": saved_setup.get("break_candle_time"),
            "break_close": saved_setup.get("break_close"),
            "expires_after_5m_candles": saved_setup.get(
                "expires_after_5m_candles"
            ),
            "expires_at": saved_setup.get("expires_at"),
            "expired_at": saved_setup.get("expired_at"),
            "expiration_reason": saved_setup.get("expiration_reason"),
            "confirmed_5m_time": saved_setup.get("confirmed_5m_time"),
        }
        saved_15m_setups[saved_side] = saved_setup_debug

        if saved_setup_debug["status"] == "EXPIRED":
            if expired_15m_setup is None:
                expired_15m_setup = saved_setup_debug
            else:
                current_time = str(
                    expired_15m_setup.get("expired_at")
                    or expired_15m_setup.get("expires_at")
                    or expired_15m_setup.get("break_candle_time")
                    or ""
                )
                candidate_time = str(
                    saved_setup_debug.get("expired_at")
                    or saved_setup_debug.get("expires_at")
                    or saved_setup_debug.get("break_candle_time")
                    or ""
                )
                if candidate_time > current_time:
                    expired_15m_setup = saved_setup_debug

    result["entry_strategy_debug"] = {
        "symbol": normalize_symbol(symbol),
        "final_signal": final_entry_decision,
        "signal_before_filters": result.get("signal_before_filters") or validation_side,
        "signal_after_filters": signal_after_filters,
        "blocked_by": result.get("blocked_by"),
        "blocked_reason": (
            None if final_entry_decision in ["BUY", "SELL"] else (
                result.get("blocked_reason")
                or result.get("setup_freshness_reason")
                or result.get("plan_reason")
                or "Waiting for fresh 15m BOS"
            )
        ),
        "smc_direction": smc_direction,
        "bos_detected": bos_detected,
        "choch_detected": choch_detected,
        "fifteen_m_swing_break": bool(
            fifteen_m_swing_break.get("break_happened")
            or fifteen_m_swing_break.get("confirmed")
        ),
        "fifteen_m_close_confirmed": bool(
            fifteen_m_swing_break.get("close_confirmed")
            or fifteen_m_swing_break.get("confirmed")
        ),
        "fifteen_m_candle_close_confirmed": bool(
            fifteen_m_swing_break.get("close_confirmed")
            or fifteen_m_swing_break.get("confirmed")
        ),
        "fifteen_m_bos_level": (
            (pending_15m_setup or {}).get("swing_level")
            or fifteen_m_swing_break.get("swing_level")
        ),
        "actionable_15m_buy_level": actionable_buy_level,
        "actionable_15m_sell_level": actionable_sell_level,
        "actionable_15m_levels_message": actionable_levels_message,
        "actionable_15m_buy_debug": actionable_buy_debug,
        "actionable_15m_sell_debug": actionable_sell_debug,
        "saved_15m_setups": saved_15m_setups,
        "expired_15m_setup": expired_15m_setup,
        "saved_15m_setup_status": (
            ((pending_15m_setup or {}).get("status"))
            or (
                saved_15m_setups.get(validation_side, {}).get("status")
                if validation_side in ["BUY", "SELL"]
                else None
            )
        ),
        "saved_swing_level": (
            (pending_15m_setup or {}).get("swing_level")
            or fifteen_m_swing_break.get("swing_level")
        ),
        "five_m_confirmation": bool(
            five_m_entry.get("close_confirmed")
            and five_m_after_setup
        ),
        "selected_swing_sl": (result.get("swing_sl_debug") or {}).get("selected_swing_sl"),
        "sl_source": (result.get("swing_sl_debug") or {}).get("sl_source"),
        "pullback_block_active": pullback_block_active,
        "trend_bias": trend_bias,
        "entry": result.get("entry_price"),
        "sl": result.get("stop_loss"),
        "tp1": result.get("tp1"),
        "tp2": result.get("tp2"),
        "calculated_lots": result.get("calculated_lots"),
        "rounded_lots": result.get("rounded_lots"),
        "broker_min_volume": result.get("broker_min_volume"),
        "broker_volume_step": result.get("broker_volume_step"),
        "final_risk_percent": result.get("final_risk_percent"),
        "allowed_risk_percent": result.get("allowed_risk_percent"),
        "sl_valid": sl_valid,
        "tp1_valid": tp1_valid,
        "tp2_valid": tp2_valid,
        "final_entry_decision": final_entry_decision,
        "block_reason": (
            result.get("blocked_reason")
            or result.get("setup_freshness_reason")
            or result.get("plan_reason")
            or "None"
        ),
        "setup_freshness": result.get("setup_freshness"),
        "setup_age_candles": result.get("setup_age_candles"),
        "confirmation_age_candles": result.get("confirmation_age_candles"),
        "distance_from_bos_pips": result.get("distance_from_bos_pips"),
        "freshness_score": result.get("freshness_score"),
        "freshness_reason": result.get("freshness_reason"),
        "pending_5m_candles_remaining": (
            (pending_15m_setup or {}).get("candles_remaining")
        ),
    }
    result["strategy_debug"] = copy.deepcopy(result["entry_strategy_debug"])
    print("ENTRY_STRATEGY_UI_DEBUG =", {
        "symbol": symbol,
        **result["entry_strategy_debug"],
    })

    return result

# =========================
# 🔗 PANEL DATA
# =========================
def _df_to_candles(df, limit=120):
    if df is None or df.empty:
        return []

    df = df.tail(limit).copy()
    candles = []

    for idx, row in df.iterrows():
        try:
            ts = int(pd.Timestamp(idx).timestamp())
            candles.append({
                "time": ts,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
        except Exception:
            continue

    return candles

def _minutes_since_last_candle(df):
    if df is None or df.empty:
        return None

    try:
        last_idx = pd.Timestamp(df.index[-1])
        if last_idx.tzinfo is None:
            last_idx = last_idx.tz_localize("UTC")
        else:
            last_idx = last_idx.tz_convert("UTC")

        now_utc = datetime.now(timezone.utc)
        age_minutes = (now_utc - last_idx.to_pydatetime()).total_seconds() / 60
        return round(age_minutes, 1)
    except Exception:
        return None


def _is_feed_stale(df, max_age_minutes=20):
    age = _minutes_since_last_candle(df)
    if age is None:
        return True
    return age > max_age_minutes

def _is_ctrader_signal_frame_ready(symbol, timeframe, df):
    if df is None or df.empty:
        return False

    settings = CTRADER_HEALTH_TIMEFRAMES.get(str(timeframe or "").lower())
    last_timestamp = _get_last_candle_timestamp(df)

    if not settings or last_timestamp is None:
        return False

    now_utc = datetime.now(timezone.utc)
    age_seconds = (
        now_utc - last_timestamp.to_pydatetime()
    ).total_seconds()

    if (
        call_pair_strategy_hook(symbol, "is_daily_pause", now_utc)
        and 0 <= age_seconds <= get_pair_strategy_rule(
            symbol,
            "daily_pause_max_stale_seconds",
        )
    ):
        print("XAUUSD_SESSION_PAUSE_FRESHNESS", {
            "timeframe": timeframe,
            "latest_candle_time": last_timestamp.isoformat(),
            "age_minutes": round(age_seconds / 60, 1),
            "market_time": now_utc.astimezone(MARKET_TIMEZONE).isoformat(),
            "accepted": True,
        })
        return True

    return age_seconds <= settings["stale_after"]

def _get_ctrader_frame_source_label(symbol, timeframe, df):
    if df is None or df.empty:
        return "ctrader_unavailable"

    if not _is_ctrader_signal_frame_ready(symbol, timeframe, df):
        return "ctrader_stale"

    health = get_ctrader_candle_health(symbol, timeframe)

    if health.get("usable"):
        return health.get("source") or "ctrader_cache"

    return "ctrader_cache"

def _get_ctrader_signal_wait_reason(source_state):
    labels = [
        source_state.get("tf_1h_source"),
        source_state.get("tf_15m_source"),
        source_state.get("tf_5m_source"),
    ]

    if all(label in ["ctrader", "ctrader_cache"] for label in labels):
        return None

    if "ctrader_unavailable" in labels:
        return "cTrader candles unavailable"

    if (
        source_state.get("tf_1h_source") == "ctrader_stale"
        and source_state.get("tf_15m_source") in ["ctrader", "ctrader_cache"]
        and source_state.get("tf_5m_source") in ["ctrader", "ctrader_cache"]
    ):
        return None

    if "ctrader_stale" in labels:
        return "cTrader candles stale"

    return "cTrader candles unavailable"

def _has_ctrader_stale_candles(source_state):
    return "ctrader_stale" in [
        source_state.get("tf_1h_source"),
        source_state.get("tf_15m_source"),
        source_state.get("tf_5m_source"),
    ]

def _is_ctrader_signal_state_available(source_state):
    if (
        source_state.get("tf_1h_source") == "ctrader_stale"
        and source_state.get("tf_15m_source") in ["ctrader", "ctrader_cache"]
        and source_state.get("tf_5m_source") in ["ctrader", "ctrader_cache"]
    ):
        return True

    return (
        source_state.get("tf_1h_source") in ["ctrader", "ctrader_cache"]
        and source_state.get("tf_15m_source") in ["ctrader", "ctrader_cache"]
        and source_state.get("tf_5m_source") in ["ctrader", "ctrader_cache"]
    )

def _get_ctrader_signal_source_state(symbol, data_5m, data_15m, data_1h):
    latest_5m = _get_last_candle_timestamp(data_5m)
    latest_15m = _get_last_candle_timestamp(data_15m)
    latest_1h = _get_last_candle_timestamp(data_1h)
    stale_5m_minutes = _minutes_since_last_candle(data_5m)
    stale_15m_minutes = _minutes_since_last_candle(data_15m)
    stale_1h_minutes = _minutes_since_last_candle(data_1h)
    health_5m = get_ctrader_candle_health(symbol, "5m")
    health_15m = get_ctrader_candle_health(symbol, "15m")
    health_1h = get_ctrader_candle_health(symbol, "1h")
    state = {
        "symbol": normalize_symbol(symbol),
        "tf_1h_source": _get_ctrader_frame_source_label(symbol, "1h", data_1h),
        "tf_15m_source": _get_ctrader_frame_source_label(symbol, "15m", data_15m),
        "tf_5m_source": _get_ctrader_frame_source_label(symbol, "5m", data_5m),
        "candles_5m_count": len(data_5m) if data_5m is not None else 0,
        "candles_15m_count": len(data_15m) if data_15m is not None else 0,
        "candles_1h_count": len(data_1h) if data_1h is not None else 0,
        "latest_5m_time": latest_5m.isoformat() if latest_5m is not None else None,
        "latest_15m_time": latest_15m.isoformat() if latest_15m is not None else None,
        "latest_1h_time": latest_1h.isoformat() if latest_1h is not None else None,
        "stale_minutes": stale_5m_minutes,
        "stale_5m_minutes": stale_5m_minutes,
        "stale_15m_minutes": stale_15m_minutes,
        "stale_1h_minutes": stale_1h_minutes,
        "used_for_signal": "ctrader",
        "candle_source": None,
        "last_successful_fetch": health_5m.get("last_successful_fetch"),
        "missed_fetch_count": health_5m.get("missed_fetch_count", 0),
        "timeframes": {
            "5m": health_5m,
            "15m": health_15m,
            "1h": health_1h,
        },
    }
    state["candle_source"] = state["tf_5m_source"]
    state["available"] = _is_ctrader_signal_state_available(state)
    state["reason"] = _get_ctrader_signal_wait_reason(state)
    print("SIGNAL_DATA_SOURCE =", state)
    print("CHART_CANDLE_DEBUG =", {
        "symbol": state["symbol"],
        "latest_5m_time": state["latest_5m_time"],
        "candles_5m_count": state["candles_5m_count"],
        "source": state["tf_5m_source"],
        "missed_fetch_count": state["missed_fetch_count"],
    })

    if state["symbol"] == "XAUUSD":
        print("XAUUSD_DATA_SOURCE_STATE", {
            "available": state["available"],
            "reason": state["reason"],
            "candles_5m_count": state["candles_5m_count"],
            "candles_15m_count": state["candles_15m_count"],
            "candles_1h_count": state["candles_1h_count"],
            "latest_5m_time": state["latest_5m_time"],
            "latest_15m_time": state["latest_15m_time"],
            "latest_1h_time": state["latest_1h_time"],
            "stale_minutes": state["stale_minutes"],
        })

    return state

def _make_ctrader_unavailable_result(symbol, source_state=None):
    reason = (
        (source_state or {}).get("reason")
        or _get_ctrader_signal_wait_reason(source_state or {})
    )
    result = {
        "signal": "WAIT",
        "signal_text": f"WAIT ⚪ ({reason})",
        "buy_pct": 0,
        "sell_pct": 0,
        "confidence": 0,
        "market_condition": "CTRADER_UNAVAILABLE",
        "entry_quality": "WAIT",
        "entry_timing": "WAIT",
        "entry_price": "--",
        "stop_loss": "--",
        "tp1": "--",
        "tp2": "--",
        "risk_reward": "--",
        "plan_bias": "WAIT",
        "plan_type": "WAIT",
        "plan_reason": reason,
        "blocked_by": "ctrader_data",
        "blocked_reason": reason,
        "signal_before_filters": "WAIT",
        "blocker_rule_name": "ctrader_candles_required",
        "structure_trend": "Trend: 1h unavailable",
        "structure_type": "Entry: 15m unavailable",
        "structure_next": "5m: confirmation unavailable",
        "debug_reasons": [reason],
        "signal_data_source": source_state or {},
    }
    result["signal_diagnostics"] = {
        "final_signal": "WAIT",
        "signal_before_filters": "WAIT",
        "blocked": True,
        "blocked_by": "ctrader_data",
        "blocked_reason": reason,
        "blocker_rule_name": "ctrader_candles_required",
        "blocker_trace": [{
            "blocked_by": "ctrader_data",
            "blocked_reason": reason,
            "blocker_rule_name": "ctrader_candles_required",
            "signal_before_filters": "WAIT",
        }],
        "data_source": source_state or {},
    }
    print("SIGNAL_NO_DATA_DEBUG =", {
        "symbol": normalize_symbol(symbol),
        "reason": reason,
        "source_state": source_state or {},
    })
    return result

def _make_closed_result(symbol, base_result=None, stale_minutes=None):
    result = copy.deepcopy(base_result) if isinstance(base_result, dict) else {}

    result["signal"] = "WAIT"
    result["signal_text"] = f"WAIT ⚪ (market closed)"
    result["market_condition"] = "MARKET_CLOSED"
    result["entry_quality"] = result.get("entry_quality", "WEAK")
    result["entry_timing"] = "CLOSED"
    result["market_closed"] = True
    result["stale_minutes"] = stale_minutes
    result["blocked_by"] = "market_closed"
    result["blocked_reason"] = "market closed"
    result["signal_before_filters"] = result.get("signal_before_filters") or "WAIT"
    result["blocker_rule_name"] = "market_closed"

    # Keep the last/calculated pressure visible in the UI bars. The WAIT signal
    # and market_closed blocker still prevent treating these as tradable setups.
    for score_key in ("buy_pct", "sell_pct", "confidence"):
        try:
            result[score_key] = max(0, min(100, int(round(float(result.get(score_key) or 0)))))
        except (TypeError, ValueError):
            result[score_key] = 0

    result.setdefault("debug_reasons", [])
    result["debug_reasons"] = list(result["debug_reasons"]) + ["Market closed / stale feed"]

    return result

def get_panel_data(force_refresh=False):
    run_weekly_paper_reset()

    eurusd, gold, eurusd_htf, gold_htf, eurusd_1h, gold_1h = fetch_market_data(
        force_refresh=force_refresh
    )
    if not hasattr(get_panel_data, "_last_open_payload"):
        get_panel_data._last_open_payload = None

    eurusd_stale_minutes = _minutes_since_last_candle(eurusd)
    gold_stale_minutes = _minutes_since_last_candle(gold)

    calendar_closed = is_market_calendar_closed()

    eurusd_closed = calendar_closed
    gold_closed = calendar_closed

    print("Calendar closed:", calendar_closed)
    print("EURUSD closed:", eurusd_closed)
    print("XAUUSD closed:", gold_closed)

    all_closed = eurusd_closed and gold_closed

    # =========================
    # MARKET CLOSED / STALE FEED MODE
    # =========================
    if all_closed and get_panel_data._last_open_payload is not None:
        payload = bounded_panel_snapshot(get_panel_data._last_open_payload)

        payload["EURUSD"] = _make_closed_result(
            "EURUSD",
            payload.get("EURUSD"),
            stale_minutes=eurusd_stale_minutes
        )
        payload["XAUUSD"] = _make_closed_result(
            "XAUUSD",
            payload.get("XAUUSD"),
            stale_minutes=gold_stale_minutes
        )

        payload["market_closed"] = True
        payload["feed_status"] = {
            "EURUSD": {
                "market_closed": True,
                "stale_minutes": eurusd_stale_minutes,
            },
            "XAUUSD": {
                "market_closed": True,
                "stale_minutes": gold_stale_minutes,
            },
        }
        payload["paper_trades"] = AUTO_TRADES
        payload["paper_active_trades"] = PAPER_ACTIVE_TRADES
        payload["paper_trade_history"] = PAPER_TRADE_HISTORY[-20:]
        payload["paper_trade_stats"] = get_paper_trade_stats()

        return payload

    # =========================
    # NORMAL LIVE MODE
    # =========================
    eurusd_source_state = _get_ctrader_signal_source_state(
        "EURUSD",
        eurusd,
        eurusd_htf,
        eurusd_1h
    )
    gold_source_state = _get_ctrader_signal_source_state(
        "XAUUSD",
        gold,
        gold_htf,
        gold_1h
    )

    if eurusd_source_state.get("available"):
        eurusd_result = get_mtf_signal(eurusd, eurusd_htf, eurusd_1h, "EURUSD")
        eurusd_result["signal_data_source"] = eurusd_source_state
    else:
        eurusd_result = _make_ctrader_unavailable_result(
            "EURUSD",
            eurusd_source_state
        )

    if gold_source_state.get("available"):
        gold_result = get_mtf_signal(gold, gold_htf, gold_1h, "XAUUSD")
        gold_result["signal_data_source"] = gold_source_state
    else:
        gold_result = _make_ctrader_unavailable_result(
            "XAUUSD",
            gold_source_state
        )

    for decision_symbol, decision_result, decision_source in [
        ("EURUSD", eurusd_result, eurusd_source_state),
        ("XAUUSD", gold_result, gold_source_state),
    ]:
        print("SIGNAL_DECISION_DEBUG =", {
            "symbol": decision_symbol,
            "signal": decision_result.get("signal"),
            "buy_pct": decision_result.get("buy_pct"),
            "sell_pct": decision_result.get("sell_pct"),
            "confidence": decision_result.get("confidence"),
            "blocked_by": decision_result.get("blocked_by"),
            "blocked_reason": decision_result.get("blocked_reason"),
            "blocker_rule_name": decision_result.get("blocker_rule_name"),
            "candle_source": decision_source.get("candle_source"),
            "last_candle_time": decision_source.get("latest_5m_time"),
            "last_successful_fetch": decision_source.get("last_successful_fetch"),
            "missed_fetch_count": decision_source.get("missed_fetch_count"),
        })

    # Diagnostics are a best-effort observer of the completed calculation.
    # persist_cycle_safely catches all storage failures and never changes a
    # signal, gate, level, or execution decision.
    persist_cycle_safely(
        "EURUSD",
        eurusd_result,
        data_5m=eurusd,
        data_15m=eurusd_htf,
        source_state=eurusd_source_state,
    )
    persist_cycle_safely(
        "XAUUSD",
        gold_result,
        data_5m=gold,
        data_15m=gold_htf,
        source_state=gold_source_state,
    )

    # Strategy V2 is an independent, persistence-backed observer.  This call
    # has no broker, LIVE/PAPER, cooldown, or V1-memory path and cannot alter
    # either result object.  Failures are contained inside the shadow service.
    evaluate_v2_shadow_cycle_safely(
        "EURUSD",
        eurusd_result,
        data_5m=eurusd,
        data_15m=eurusd_htf,
    )
    evaluate_v2_shadow_cycle_safely(
        "XAUUSD",
        gold_result,
        data_5m=gold,
        data_15m=gold_htf,
    )

    if eurusd_source_state.get("available") and not eurusd.empty:
       update_paper_trade(
           "EURUSD",
           eurusd_result,
           eurusd["Close"].iloc[-1].item(),
           eurusd["Low"].iloc[-1].item(),
           eurusd["High"].iloc[-1].item()
       )

    if gold_source_state.get("available") and not gold.empty:
        update_paper_trade(
            "XAUUSD",
            gold_result,
            gold["Close"].iloc[-1].item(),
            gold["Low"].iloc[-1].item(),
            gold["High"].iloc[-1].item()
        )

    if eurusd_closed:
        eurusd_result = _make_closed_result(
            "EURUSD",
            eurusd_result,
            stale_minutes=eurusd_stale_minutes
        )
    else:
        eurusd_result["market_closed"] = False
        eurusd_result["stale_minutes"] = eurusd_stale_minutes

    if gold_closed:
        gold_result = _make_closed_result(
            "XAUUSD",
            gold_result,
            stale_minutes=gold_stale_minutes
        )
    else:
        gold_result["market_closed"] = False
        gold_result["stale_minutes"] = gold_stale_minutes

    # only update history/results when that symbol feed is live
    if not eurusd_closed:
        update_signal_history("EURUSD", eurusd_result)

    if not eurusd_closed and eurusd_source_state.get("available"):
        update_trade_results(eurusd, "EURUSD")

    if not gold_closed:
        update_signal_history("XAUUSD", gold_result)

    if not gold_closed and gold_source_state.get("available"):
        update_trade_results(gold, "XAUUSD")

    print("XAUUSD_RESULT_DEBUG", {
        "source_available": gold_source_state.get("available"),
        "source_reason": gold_source_state.get("reason"),
        "result_source": (
            "get_mtf_signal"
            if gold_source_state.get("available")
            else "_make_ctrader_unavailable_result"
        ),
        "signal": gold_result.get("signal"),
        "buy_percentage": gold_result.get("buy_pct"),
        "sell_percentage": gold_result.get("sell_pct"),
        "confidence": gold_result.get("confidence"),
        "strategy_setup_complete": gold_result.get("strategy_setup_complete"),
        "plan_reason": gold_result.get("plan_reason"),
        "blocked_by": gold_result.get("blocked_by"),
        "market_condition": gold_result.get("market_condition"),
        "debug_reasons": gold_result.get("debug_reasons"),
        "candles_5m_count": gold_source_state.get("candles_5m_count"),
        "candles_15m_count": gold_source_state.get("candles_15m_count"),
        "candles_1h_count": gold_source_state.get("candles_1h_count"),
    })

    payload = {
        "EURUSD": eurusd_result,
        "XAUUSD": gold_result,
        "candles": {
            "EURUSD": {
                "5m": _df_to_candles(eurusd, limit=5000),
                "15m": _df_to_candles(eurusd_htf, limit=5000),
                "1h": _df_to_candles(eurusd_1h, limit=5000),
            },
            "XAUUSD": {
                "5m": _df_to_candles(gold, limit=5000),
                "15m": _df_to_candles(gold_htf, limit=5000),
                "1h": _df_to_candles(gold_1h, limit=5000),
            }
        },
        "history": SIGNAL_HISTORY[-20:],
        "paper_trades": AUTO_TRADES,
        "paper_active_trades": PAPER_ACTIVE_TRADES,
        "paper_trade_history": PAPER_TRADE_HISTORY[-20:],
        "paper_trade_stats": get_paper_trade_stats(),
        "live_trades": LIVE_TRADES,
        "live_trade_history": LIVE_TRADE_HISTORY[-20:],

        "market_closed": all_closed,
        "feed_status": {
            "EURUSD": {
                "market_closed": eurusd_closed,
                "stale_minutes": eurusd_stale_minutes,
                "signal_data_source": eurusd_source_state,
            },
            "XAUUSD": {
                "market_closed": gold_closed,
                "stale_minutes": gold_stale_minutes,
                "signal_data_source": gold_source_state,
            },
        }
    }

    print("CANONICAL_SYMBOL_CHECK", {
        "raw_symbol": "XAUUSD",
        "normalized_symbol": normalize_symbol("XAUUSD"),
        "payload_keys": [
            key for key in payload.keys()
            if key in ["EURUSD", "XAUUSD"]
        ],
        "paper_symbol": (
            AUTO_TRADES.get("XAUUSD", {}).get("symbol")
            if AUTO_TRADES.get("XAUUSD")
            else "XAUUSD"
        ),
        "live_symbol": "XAUUSD",
    })

    # save last valid fully-live payload
    if not all_closed:
        remember_last_open_payload(payload)

    return payload

# =========================
# 🕘 SIGNAL HISTORY
# =========================
SIGNAL_HISTORY = []


def update_signal_history(symbol, result):

    global SIGNAL_HISTORY

    symbol = normalize_symbol(symbol)
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "symbol": symbol,
        "signal": str(
            result.get("history_signal")
            or result.get("display_signal")
            or result.get("signal_display_state")
            or result.get("signal", "WAIT")
        ).upper(),
        "signal_text": result.get("signal_text", ""),
        "buy_pct": result.get("buy_pct", 0),
        "sell_pct": result.get("sell_pct", 0),
        "confidence": result.get("confidence", 0),
        "market_condition": result.get("market_condition", "UNKNOWN"),
        "entry_quality": result.get("entry_quality", "WEAK"),
        "entry_timing": result.get("entry_timing", "NEUTRAL"),
        "fake_kill": result.get("fake_kill", False),

         "entry_price": None,
        "result": "RUNNING",
        "pips": 0,
    }


    previous_symbol_entry = next(
        (
            history_entry
            for history_entry in reversed(SIGNAL_HISTORY)
            if normalize_symbol(history_entry.get("symbol")) == symbol
        ),
        None
    )

    if (
        previous_symbol_entry
        and str(previous_symbol_entry.get("signal", "WAIT")).upper()
        == entry["signal"]
    ):
        return

    SIGNAL_HISTORY.append(entry)
    if entry["signal"] in ["BUY", "SELL"]:
        entry["entry_price"] = result.get("price", None)

    if len(SIGNAL_HISTORY) > 20:
        SIGNAL_HISTORY = SIGNAL_HISTORY[-20:]

def update_trade_results(data, symbol):
    global SIGNAL_HISTORY

    if not SIGNAL_HISTORY or data.empty:
        return

    current_price = data["Close"].iloc[-1].item()

    for trade in SIGNAL_HISTORY:
        if trade["symbol"] != symbol:
            continue

        if trade["result"] != "RUNNING":
            continue

        entry_price = trade.get("entry_price")

        if entry_price is None:
            continue

        pips = (
            current_price - entry_price
        ) / get_pair_strategy_rule(symbol, "paper_pip_value")

        if trade["signal"] == "SELL":
            pips *= -1

        trade["pips"] = round(pips, 1)

        if pips >= 10:
            trade["result"] = "WIN"
        elif pips <= -10:
            trade["result"] = "LOSS"
    
# =========================
# 🧪 TEST MODE
# =========================
if __name__ == "__main__":
    panel_data = get_panel_data()

    print("===== SIGNALS =====")
    for symbol in ["EURUSD", "XAUUSD"]:
        result = panel_data[symbol]
        print(f"{symbol}: {result['signal_text']}")
        print(f"Buy: {result['buy_pct']}%")
        print(f"Sell: {result['sell_pct']}%")
        print(f"Confidence: {result['confidence']}%")
        print(f"Market Condition: {result['market_condition']}")
        print(f"Entry Quality: {result['entry_quality']}")
        print()
