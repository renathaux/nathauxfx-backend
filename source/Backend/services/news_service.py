import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False


ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

JBLANKED_DEFAULT_BASE_URL = "https://www.jblanked.com/news/api"
JBLANKED_CALENDAR_PATH = "/mql5/calendar/today/"
FMP_ECONOMIC_CALENDAR_URL = (
    "https://financialmodelingprep.com/stable/economic-calendar"
)
FINNHUB_ECONOMIC_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"
CALENDAR_CACHE_SECONDS = 15 * 60
POST_RELEASE_ACTUAL_REFRESH_SECONDS = 60
POST_RELEASE_ACTUAL_REFRESH_WINDOW_SECONDS = 30 * 60

SUPPORTED_SYMBOLS = {"EURUSD", "XAUUSD"}
SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "XAUUSD": ["USD"],
}
IMPACT_WEIGHT = {
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
}

LOWER_IS_BETTER_KEYWORDS = [
    "unemployment",
    "jobless",
    "claims",
    "claimant",
    "layoff",
]
BETTER_IS_BULLISH_KEYWORDS = [
    "cpi",
    "inflation",
    "pce",
    "payroll",
    "employment",
    "jobs",
    "gdp",
    "retail",
    "sales",
    "wage",
    "earnings",
    "pmi",
    "ism",
    "production",
]
IMPORTANT_EVENT_KEYWORDS = [
    "cpi",
    "core cpi",
    "consumer price",
    "non-farm payroll",
    "nonfarm payroll",
    "nfp",
    "unemployment rate",
    "fomc",
    "fed interest rate",
    "federal funds rate",
    "fed chair",
    "powell",
    "ecb interest rate",
    "ecb rate",
    "gdp",
    "gross domestic product",
    "pce",
    "personal consumption expenditures",
    "retail sales",
    "pmi",
    "purchasing managers",
]

UPCOMING_EVENT_PRIORITY = [
    ("fomc_rate_decision", ["fomc rate decision", "fed interest rate", "federal funds rate"]),
    ("fomc_press_conference", ["fomc press conference", "fed press conference", "fed chair", "powell"]),
    ("nfp", ["non-farm payroll", "nonfarm payroll", "nfp", "unemployment rate"]),
    ("cpi", ["us cpi", "usd cpi", "cpi", "consumer price", "core cpi"]),
    ("pce", ["pce", "personal consumption expenditures"]),
    ("gdp", ["gdp", "gross domestic product"]),
    ("retail_sales", ["retail sales"]),
    ("ecb_rate_decision", ["ecb rate decision", "ecb interest rate"]),
    ("pmi", ["pmi", "purchasing managers"]),
]

MANUAL_HIGH_IMPACT_EVENTS = [
    {
        "event_name": "US CPI",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 1,
        "hour": 12,
        "minute": 30,
        "actual": None,
        "forecast": "0.3%",
        "previous": "0.2%",
    },
    {
        "event_name": "Core CPI",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 1,
        "hour": 12,
        "minute": 30,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "Retail Sales",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 2,
        "hour": 12,
        "minute": 30,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "PCE",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 2,
        "hour": 13,
        "minute": 0,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "GDP",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 3,
        "hour": 12,
        "minute": 30,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "ECB Rate Decision",
        "currency": "EUR",
        "impact": "HIGH",
        "weekday": 3,
        "hour": 12,
        "minute": 15,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "FOMC Rate Decision",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 3,
        "hour": 18,
        "minute": 0,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "Fed Chair Speech",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 3,
        "hour": 18,
        "minute": 30,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "NFP",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 4,
        "hour": 12,
        "minute": 30,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
    {
        "event_name": "Manual calendar fallback: FOMC / Fed Chair speech watch",
        "currency": "USD",
        "impact": "HIGH",
        "weekday": 4,
        "hour": 18,
        "minute": 0,
        "actual": None,
        "forecast": None,
        "previous": None,
    },
]

_CALENDAR_CACHE = {
    "events": None,
    "fetched_at": 0,
    "last_fetch_time": None,
    "last_error": None,
    "raw_event_count": 0,
    "normalized_event_count": 0,
    "last_source": "none",
    "api_status": [],
    "actual_refresh": {},
}


def get_calendar_data_age_seconds(now_timestamp=None):
    fetched_at = float(_CALENDAR_CACHE.get("fetched_at") or 0)
    if fetched_at <= 0:
        return float("inf")
    current = float(now_timestamp if now_timestamp is not None else time.time())
    return max(0.0, current - fetched_at)


def load_news_env_file():
    if not os.path.exists(ENV_PATH):
        return False

    loaded = False
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key not in [
                    "JBLANKED_API_KEY",
                    "JBLANKED_NEWS_BASE_URL",
                    "FMP_API_KEY",
                    "FMP_NEWS_BASE_URL",
                    "FINNHUB_API_KEY",
                    "FINNHUB_TOKEN",
                    "FINNHUB_NEWS_BASE_URL",
                ]:
                    continue
                if os.getenv(key):
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
                loaded = True
    except OSError as exc:
        print("NEWS_ENV_LOAD_ERROR =", {
            "env_path": ENV_PATH,
            "error": str(exc),
        })
    return loaded




def mask_secret(value):
    text = str(value or "")
    if len(text) <= 8:
        return "****" if text else ""
    return f"{text[:4]}...{text[-4:]}"


def preview_text(value, limit=1200):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def normalize_symbol(symbol):
    normalized = str(symbol or "EURUSD").upper().replace("GOLD", "XAUUSD")
    return normalized if normalized in SUPPORTED_SYMBOLS else "EURUSD"


def utc_now():
    return datetime.now(timezone.utc)


def iso_timestamp(dt=None):
    return (dt or utc_now()).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def get_jblanked_api_key():
    key = os.getenv("JBLANKED_API_KEY", "").strip()
    if not key or key == "PUT_YOUR_KEY_HERE":
        return None
    return key


def get_fmp_api_key():
    key = os.getenv("FMP_API_KEY", "").strip()
    if not key or key == "PUT_YOUR_KEY_HERE":
        return None
    return key


def get_finnhub_api_key():
    key = (
        os.getenv("FINNHUB_API_KEY", "")
        or os.getenv("FINNHUB_TOKEN", "")
    ).strip()
    if not key or key == "PUT_YOUR_KEY_HERE":
        return None
    return key


def get_jblanked_calendar_url():
    base_url = (
        os.getenv("JBLANKED_NEWS_BASE_URL")
        or JBLANKED_DEFAULT_BASE_URL
    ).rstrip("/")
    return f"{base_url}{JBLANKED_CALENDAR_PATH}"


def get_fmp_calendar_url():
    return (
        os.getenv("FMP_NEWS_BASE_URL")
        or FMP_ECONOMIC_CALENDAR_URL
    ).rstrip("/")


def get_finnhub_calendar_url():
    return (
        os.getenv("FINNHUB_NEWS_BASE_URL")
        or FINNHUB_ECONOMIC_CALENDAR_URL
    ).rstrip("/")


def calendar_date_range(now=None):
    now = now or utc_now()
    today = now.astimezone(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    return today.isoformat(), tomorrow.isoformat()


def build_news_debug(
    symbol,
    *,
    source=None,
    is_live_news=False,
    is_fallback=False,
    fetch_error=None,
    raw_event_count=None,
    filtered_event_count=None,
):
    normalized_symbol = normalize_symbol(symbol)
    fetched_at = _CALENDAR_CACHE.get("last_fetch_time")

    return {
        "news_source": source or _CALENDAR_CACHE.get("last_source") or "none",
        "is_live_news": bool(is_live_news),
        "is_fallback": bool(is_fallback),
        "api_key_loaded": bool(get_jblanked_api_key()),
        "fmp_api_key_loaded": bool(get_fmp_api_key()),
        "finnhub_api_key_loaded": bool(get_finnhub_api_key()),
        "api_status": _CALENDAR_CACHE.get("api_status") or [],
        "last_fetch_time": fetched_at,
        "fetch_error": (
            str(fetch_error)
            if fetch_error is not None
            else _CALENDAR_CACHE.get("last_error")
        ),
        "raw_event_count": (
            int(raw_event_count)
            if raw_event_count is not None
            else int(_CALENDAR_CACHE.get("raw_event_count") or 0)
        ),
        "filtered_event_count": (
            int(filtered_event_count)
            if filtered_event_count is not None
            else 0
        ),
        "currency_filter_used": SYMBOL_CURRENCIES[normalized_symbol],
        "symbol_filter_used": normalized_symbol,
    }


def safe_news_fallback(
    symbol,
    decision="NEWS_UNAVAILABLE",
    fetch_error=None,
    source="fallback",
    is_fallback=True,
    raw_event_count=None,
    filtered_event_count=None,
):
    normalized_symbol = normalize_symbol(symbol)
    try:
        from services import news_trading
        authoritative_news = news_trading.evaluate([], normalized_symbol)
    except Exception:
        authoritative_news = {
            "mode": os.getenv("NEWS_TRADING_MODE", "OFF").upper(),
            "phase": "NORMAL",
            "authoritative_status": "NORMAL",
            "allow_normal_entry": True,
            "allow_news_entry": False,
            "blocking_reason": decision,
        }
    event = (
        "No major news now"
        if decision == "NO_MAJOR_NEWS"
        else "News unavailable"
    )
    return {
        "symbol": normalized_symbol,
        "event": event,
        "event_name": event,
        "next_event": event,
        "currency": " / ".join(SYMBOL_CURRENCIES[normalized_symbol]),
        "impact": "LOW",
        "forecast": None,
        "actual": None,
        "previous": None,
        "release_time": None,
        "time": None,
        "time_utc": None,
        "time_until": "--",
        "news_bias": "Neutral",
        "display_status": (
            "Data unavailable"
            if decision == "NEWS_UNAVAILABLE"
            else "Neutral"
            if decision == "NO_MAJOR_NEWS"
            else "Waiting for actual data"
        ),
        "status": (
            "Data unavailable"
            if decision == "NEWS_UNAVAILABLE"
            else "Neutral"
            if decision == "NO_MAJOR_NEWS"
            else "Waiting for actual data"
        ),
        "news_reason": (
            "No matching high-impact economic event is available."
            if decision == "NO_MAJOR_NEWS"
            else "Economic calendar data is unavailable."
        ),
        "effect": f"{normalized_symbol} Neutral",
        "score": 0,
        "news_score": 0,
        "symbol_score": 0,
        "decision": decision,
        "trade_decision": authoritative_news.get("authoritative_status"),
        "blocking_reason": authoritative_news.get("blocking_reason"),
        "news_trading": authoritative_news,
        "last_update": iso_timestamp(),
        "source": source,
        "source_used": source,
        "source_label": (
            "Manual calendar fallback"
            if str(source or "").startswith("manual")
            else str(source or "fallback").upper()
        ),
        **build_news_debug(
            normalized_symbol,
            source=source,
            is_live_news=source == "jblanked_live",
            is_fallback=is_fallback,
            fetch_error=fetch_error,
            raw_event_count=raw_event_count,
            filtered_event_count=filtered_event_count,
        ),
    }


def fetch_jblanked_calendar_events(force=False, timeout=8):
    from startup_recovery.operation_context import publication, check_current
    check_current()
    api_key = get_jblanked_api_key()
    if not api_key:
        with publication():
            _CALENDAR_CACHE["last_source"] = "fallback"
            _CALENDAR_CACHE["last_error"] = "JBlanked API key missing"
        print("NEWS_JBLANKED_ENV_DEBUG =", {
            "env_path": ENV_PATH,
            "api_key_loaded": False,
            "api_key_masked": "",
            "base_url": os.getenv("JBLANKED_NEWS_BASE_URL"),
            "calendar_url": get_jblanked_calendar_url(),
        })
        raise RuntimeError("JBlanked API key missing")

    now_ts = time.time()
    cached_events = _CALENDAR_CACHE.get("events")
    cache_age = now_ts - float(_CALENDAR_CACHE.get("fetched_at") or 0)

    if (
        not force
        and cached_events is not None
        and cache_age < CALENDAR_CACHE_SECONDS
    ):
        with publication():
            _CALENDAR_CACHE["last_source"] = "jblanked_cache"
        return cached_events

    url = get_jblanked_calendar_url()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {api_key}",
    }
    print("NEWS_JBLANKED_REQUEST_DEBUG =", {
        "url": url,
        "api_key_loaded": True,
        "api_key_masked": mask_secret(api_key),
        "headers": {
            "Content-Type": headers["Content-Type"],
            "Authorization": f"Api-Key {mask_secret(api_key)}",
        },
    })

    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
    )
    status_code = getattr(response, "status_code", None)
    print("NEWS_JBLANKED_RESPONSE_DEBUG =", {
        "url": url,
        "status_code": status_code,
    })

    if status_code != 200:
        body_preview = preview_text(getattr(response, "text", ""))
        with publication():
            _CALENDAR_CACHE["last_source"] = "fallback"
            _CALENDAR_CACHE["last_error"] = f"JBlanked HTTP {status_code}: {body_preview}"
        print("NEWS_JBLANKED_RESPONSE_BODY_DEBUG =", {
            "status_code": status_code,
            "body_preview": body_preview,
        })
        if status_code in [401, 403]:
            raise RuntimeError(
                f"JBlanked auth issue: HTTP {status_code}: {body_preview}"
            )
        if status_code == 404:
            raise RuntimeError(
                f"JBlanked endpoint path is wrong: HTTP 404: {body_preview}"
            )
        if status_code == 429:
            raise RuntimeError(
                f"JBlanked rate limit/free usage issue: HTTP 429: {body_preview}"
            )
        response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        with publication():
            _CALENDAR_CACHE["last_source"] = "fallback"
            _CALENDAR_CACHE["last_error"] = "JBlanked response was not valid JSON"
        print("NEWS_JBLANKED_JSON_DEBUG =", {
            "status_code": status_code,
            "body_preview": preview_text(getattr(response, "text", "")),
        })
        raise RuntimeError("JBlanked response was not valid JSON") from exc

    raw_events = extract_raw_events(payload)
    events = []
    for raw_event in raw_events:
        if isinstance(raw_event, dict):
            raw_event = {**raw_event, "source": "jblanked_live"}
        event = normalize_event(raw_event)
        if event:
            event["source"] = "jblanked_live"
            events.append(event)
    print("NEWS_JBLANKED_PARSE_DEBUG =", {
        "payload_type": type(payload).__name__,
        "raw_events": len(raw_events),
        "normalized_events": len(events),
        "sample_raw_keys": (
            list(raw_events[0].keys())
            if raw_events and isinstance(raw_events[0], dict)
            else []
        ),
    })
    if not raw_events:
        print("NEWS_JBLANKED_EMPTY_RESPONSE_DEBUG =", {
            "status_code": status_code,
            "body_preview": preview_text(getattr(response, "text", "")),
        })

    with publication():
        _CALENDAR_CACHE["events"] = events
        _CALENDAR_CACHE["fetched_at"] = now_ts
        _CALENDAR_CACHE["last_fetch_time"] = iso_timestamp()
        _CALENDAR_CACHE["last_error"] = None
        _CALENDAR_CACHE["raw_event_count"] = len(raw_events)
        _CALENDAR_CACHE["normalized_event_count"] = len(events)
        _CALENDAR_CACHE["last_source"] = "jblanked_live"
    return events


def fetch_fmp_calendar_events(timeout=8, now=None):
    api_key = get_fmp_api_key()
    if not api_key:
        raise RuntimeError("FMP API key missing")

    date_from, date_to = calendar_date_range(now)
    url = get_fmp_calendar_url()
    params = {
        "from": date_from,
        "to": date_to,
        "apikey": api_key,
    }
    print("NEWS_PROVIDER_REQUEST =", {
        "provider": "fmp",
        "url": url,
        "from": date_from,
        "to": date_to,
        "api_key_loaded": True,
        "api_key_masked": mask_secret(api_key),
    })
    response = requests.get(url, params=params, timeout=timeout)
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        raise RuntimeError(
            f"FMP HTTP {status_code}: {preview_text(getattr(response, 'text', ''))}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("FMP response was not valid JSON") from exc

    raw_events = extract_raw_events(payload)
    normalized_events = []
    for raw_event in raw_events:
        if isinstance(raw_event, dict):
            raw_event = {
                **raw_event,
                "event": first_present(raw_event, "event", "name"),
                "forecast": first_present(
                    raw_event, "forecast", "estimate", "consensus"
                ),
                "previous": first_present(raw_event, "previous", "prev"),
                "source": "fmp",
            }
        event = normalize_event(raw_event)
        if event:
            event["source"] = "fmp"
            normalized_events.append(event)

    return raw_events, normalized_events


def fetch_finnhub_calendar_events(timeout=8, now=None):
    api_key = get_finnhub_api_key()
    if not api_key:
        raise RuntimeError("Finnhub API key missing")

    date_from, date_to = calendar_date_range(now)
    url = get_finnhub_calendar_url()
    params = {
        "from": date_from,
        "to": date_to,
        "token": api_key,
    }
    print("NEWS_PROVIDER_REQUEST =", {
        "provider": "finnhub",
        "url": url,
        "from": date_from,
        "to": date_to,
        "api_key_loaded": True,
        "api_key_masked": mask_secret(api_key),
    })
    response = requests.get(url, params=params, timeout=timeout)
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        raise RuntimeError(
            "Finnhub HTTP "
            f"{status_code}: {preview_text(getattr(response, 'text', ''))}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Finnhub response was not valid JSON") from exc

    raw_events = extract_raw_events(payload)
    normalized_events = []
    for raw_event in raw_events:
        if isinstance(raw_event, dict):
            raw_event = {
                **raw_event,
                "event": first_present(raw_event, "event", "name"),
                "forecast": first_present(raw_event, "forecast", "estimate"),
                "previous": first_present(raw_event, "previous", "prev"),
                "source": "finnhub",
            }
        event = normalize_event(raw_event)
        if event:
            event["source"] = "finnhub"
            normalized_events.append(event)

    return raw_events, normalized_events


def build_manual_calendar_events(now=None):
    now = now or utc_now()
    start_date = now.astimezone(timezone.utc).date()
    dates = [start_date, start_date + timedelta(days=1)]
    events = []

    for event_template in MANUAL_HIGH_IMPACT_EVENTS:
        for event_date in dates:
            if event_date.weekday() != event_template.get("weekday"):
                continue
            release_time = datetime(
                event_date.year,
                event_date.month,
                event_date.day,
                int(event_template.get("hour") or 0),
                int(event_template.get("minute") or 0),
                tzinfo=timezone.utc,
            )
            events.append({
                "event_name": event_template["event_name"],
                "event": event_template["event_name"],
                "currency": event_template["currency"],
                "impact": "UNKNOWN",
                "time": release_time,
                "release_time": release_time,
                "time_utc": release_time.isoformat(),
                "source": "manual",
                "data_status": "UNRELIABLE_STATIC",
                "informational_only": True,
                "actual": event_template.get("actual"),
                "forecast": event_template.get("forecast"),
                "previous": event_template.get("previous"),
            })

    return events


def fetch_manual_calendar_events(now=None):
    events = build_manual_calendar_events(now=now)
    return events, events


def provider_status(provider, status, raw_count=0, event_count=0, error=None):
    return {
        "provider": provider,
        "status": status,
        "events_returned": int(raw_count or 0),
        "normalized_events": int(event_count or 0),
        "error": str(error) if error else None,
    }


def fetch_calendar_events(force=False, timeout=8, now=None):
    now = now or utc_now()
    now_ts = time.time()
    cached_events = _CALENDAR_CACHE.get("events")
    cache_age = now_ts - float(_CALENDAR_CACHE.get("fetched_at") or 0)

    if (
        not force
        and cached_events is not None
        and cache_age < CALENDAR_CACHE_SECONDS
        and _CALENDAR_CACHE.get("last_source") in [
            "jblanked_live", "jblanked_cache", "fmp", "finnhub", "manual"
        ]
    ):
        cached_source = str(_CALENDAR_CACHE.get("last_source") or "cache")
        print("NEWS_PROVIDER_SELECTED =", {
            "provider": cached_source,
            "api_status": "cache_hit",
            "events_returned": len(cached_events),
        })
        return cached_events

    provider_errors = []
    statuses = []
    def fetch_jblanked_for_aggregator(timeout=8, now=None):
        events = fetch_jblanked_calendar_events(force=force, timeout=timeout)
        return events, events

    providers = [
        ("jblanked", fetch_jblanked_for_aggregator),
        ("fmp", fetch_fmp_calendar_events),
        ("finnhub", fetch_finnhub_calendar_events),
    ]

    for provider_name, fetcher in providers:
        try:
            raw_events, events = fetcher(timeout=timeout, now=now)
            status = provider_status(
                provider_name,
                "ok",
                raw_count=len(raw_events),
                event_count=len(events),
            )
            statuses.append(status)
            _CALENDAR_CACHE.update({
                "events": events,
                "fetched_at": now_ts,
                "last_fetch_time": iso_timestamp(now),
                "last_error": None,
                "raw_event_count": len(raw_events),
                "normalized_event_count": len(events),
                "last_source": (
                    "jblanked_live"
                    if provider_name == "jblanked"
                    else provider_name
                ),
                "api_status": statuses,
            })
            print("NEWS_PROVIDER_STATUS =", status)
            print("NEWS_PROVIDER_SELECTED =", {
                "provider": provider_name,
                "api_status": "ok",
                "events_returned": len(raw_events),
                "normalized_events": len(events),
            })
            return events
        except Exception as exc:
            provider_errors.append(f"{provider_name}: {exc}")
            status = provider_status(provider_name, "failed", error=exc)
            statuses.append(status)
            print("NEWS_PROVIDER_STATUS =", status)

    raw_events, events = fetch_manual_calendar_events(now=now)
    statuses.append(provider_status(
        "manual",
        "fallback",
        raw_count=len(raw_events),
        event_count=len(events),
    ))
    _CALENDAR_CACHE.update({
        "events": events,
        "fetched_at": now_ts,
        "last_fetch_time": iso_timestamp(now),
        "last_error": "; ".join(provider_errors) if provider_errors else None,
        "raw_event_count": len(raw_events),
        "normalized_event_count": len(events),
        "last_source": "manual",
        "api_status": statuses,
    })
    print("NEWS_PROVIDER_SELECTED =", {
        "provider": "manual",
        "api_status": "fallback",
        "events_returned": len(raw_events),
        "normalized_events": len(events),
        "errors": provider_errors,
    })
    return events


def actual_value_available(event):
    return event.get("actual") not in [None, "", "--", "N/A"]


def event_identity_key(symbol, event):
    event_time = get_event_time(event)
    return "|".join([
        normalize_symbol(symbol),
        str(event.get("currency") or ""),
        str(event.get("event_name") or event.get("event") or "").lower(),
        event_time.isoformat() if event_time else "no-time",
    ])


def event_keyword_set(event):
    title = str(
        event.get("event_name")
        or event.get("event")
        or ""
    ).lower()
    tokens = set()
    for keyword in IMPORTANT_EVENT_KEYWORDS:
        if keyword in title:
            tokens.add(keyword)
    if "consumer price" in title:
        tokens.add("cpi")
    if "non-farm" in title or "nonfarm" in title:
        tokens.add("nfp")
    if "federal funds" in title:
        tokens.add("fomc")
    return tokens


def event_matches_for_actual_refresh(base_event, candidate_event):
    if candidate_event.get("currency") != base_event.get("currency"):
        return False
    if not event_is_high_impact(candidate_event) or not event_is_important(candidate_event):
        return False

    base_keywords = event_keyword_set(base_event)
    candidate_keywords = event_keyword_set(candidate_event)
    if base_keywords and candidate_keywords and base_keywords.isdisjoint(candidate_keywords):
        return False

    base_time = get_event_time(base_event)
    candidate_time = get_event_time(candidate_event)
    if base_time and candidate_time:
        seconds_apart = abs((candidate_time - base_time).total_seconds())
        if seconds_apart > 2 * 60 * 60:
            return False

    return True


def fetch_api_calendar_events_for_actual_refresh(timeout=8, now=None):
    statuses = []
    api_events = []
    selected_provider = None
    def fetch_jblanked_refresh(timeout=8, now=None):
        events = fetch_jblanked_calendar_events(force=True, timeout=timeout)
        return events, events

    providers = [
        ("jblanked", fetch_jblanked_refresh),
        ("fmp", fetch_fmp_calendar_events),
        ("finnhub", fetch_finnhub_calendar_events),
    ]

    for provider_name, fetcher in providers:
        try:
            raw_events, events = fetcher(timeout=timeout, now=now)
            status = provider_status(
                provider_name,
                "ok",
                raw_count=len(raw_events),
                event_count=len(events),
            )
            statuses.append(status)
            print("NEWS_ACTUAL_REFRESH_PROVIDER_STATUS =", status)
            api_events.extend(events)
            selected_provider = selected_provider or provider_name
        except Exception as exc:
            status = provider_status(provider_name, "failed", error=exc)
            statuses.append(status)
            print("NEWS_ACTUAL_REFRESH_PROVIDER_STATUS =", status)

    return api_events, selected_provider, statuses


def replace_cached_event_with_actual(symbol, original_event, updated_event):
    cached_events = _CALENDAR_CACHE.get("events")
    if not isinstance(cached_events, list):
        return

    original_key = event_identity_key(symbol, original_event)
    updated_events = []
    replaced = False

    for cached_event in cached_events:
        if event_identity_key(symbol, cached_event) == original_key:
            updated_events.append(updated_event)
            replaced = True
        else:
            updated_events.append(cached_event)

    if replaced:
        _CALENDAR_CACHE["events"] = updated_events


def refresh_actual_after_release(event, symbol, now=None, timeout=8):
    now = now or utc_now()
    release_time = get_event_time(event)
    if not release_time:
        return event
    if actual_value_available(event):
        return event

    seconds_after_release = (now - release_time).total_seconds()
    if seconds_after_release < 0:
        return event
    if seconds_after_release > POST_RELEASE_ACTUAL_REFRESH_WINDOW_SECONDS:
        print("NEWS_ACTUAL_REFRESH_SKIPPED =", {
            "symbol": normalize_symbol(symbol),
            "event": event.get("event_name") or event.get("event"),
            "reason": "outside_30_minute_window",
            "seconds_after_release": int(seconds_after_release),
        })
        return event

    refresh_state = _CALENDAR_CACHE.setdefault("actual_refresh", {})
    refresh_key = event_identity_key(symbol, event)
    now_ts = time.time()
    last_attempt = float(refresh_state.get(refresh_key, {}).get("last_attempt") or 0)
    if now_ts - last_attempt < POST_RELEASE_ACTUAL_REFRESH_SECONDS:
        print("NEWS_ACTUAL_REFRESH_SKIPPED =", {
            "symbol": normalize_symbol(symbol),
            "event": event.get("event_name") or event.get("event"),
            "reason": "throttled_1_minute",
        })
        return event

    refresh_state[refresh_key] = {
        "last_attempt": now_ts,
        "event": event.get("event_name") or event.get("event"),
    }

    print("NEWS_ACTUAL_REFRESH_START =", {
        "symbol": normalize_symbol(symbol),
        "event": event.get("event_name") or event.get("event"),
        "release_time": release_time.isoformat(),
        "seconds_after_release": int(seconds_after_release),
    })

    api_events, provider_name, statuses = fetch_api_calendar_events_for_actual_refresh(
        timeout=timeout,
        now=now,
    )
    _CALENDAR_CACHE["api_status"] = statuses

    matching_events = [
        candidate_event for candidate_event in api_events
        if event_matches_for_actual_refresh(event, candidate_event)
        and actual_value_available(candidate_event)
    ]
    if not matching_events:
        print("NEWS_ACTUAL_REFRESH_RESULT =", {
            "symbol": normalize_symbol(symbol),
            "event": event.get("event_name") or event.get("event"),
            "actual_found": False,
            "provider": provider_name,
            "matching_events": 0,
        })
        return event

    matching_events.sort(
        key=lambda candidate_event: abs(
            (
                (get_event_time(candidate_event) or release_time)
                - release_time
            ).total_seconds()
        )
    )
    actual_event = matching_events[0]
    updated_event = {
        **event,
        "actual": actual_event.get("actual"),
        "forecast": actual_event.get("forecast") or event.get("forecast"),
        "previous": actual_event.get("previous") or event.get("previous"),
        "source": actual_event.get("source") or provider_name,
        "actual_source": actual_event.get("source") or provider_name,
    }
    replace_cached_event_with_actual(symbol, event, updated_event)
    _CALENDAR_CACHE["fetched_at"] = time.time()
    _CALENDAR_CACHE["last_fetch_time"] = iso_timestamp(now)
    print("NEWS_ACTUAL_REFRESH_RESULT =", {
        "symbol": normalize_symbol(symbol),
        "event": event.get("event_name") or event.get("event"),
        "actual_found": True,
        "actual": updated_event.get("actual"),
        "provider": updated_event.get("actual_source"),
    })
    return updated_event


def extract_raw_events(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    if any(
        key.lower() in ["event", "title", "name", "indicator"]
        for key in payload.keys()
    ):
        return [payload]

    for key in [
        "events",
        "data",
        "calendar",
        "economicCalendar",
        "economic_calendar",
        "results",
        "response",
        "today",
        "items",
    ]:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_raw_events(value)
            if nested:
                return nested

    dict_values = [value for value in payload.values() if isinstance(value, dict)]
    if dict_values and all(
        any(
            str(key).lower() in ["event", "title", "name", "indicator"]
            for key in value.keys()
        )
        for value in dict_values
    ):
        return dict_values

    for value in payload.values():
        nested = extract_raw_events(value)
        if nested:
            return nested

    return []


def normalize_event(raw):
    if not isinstance(raw, dict):
        return None

    event = first_present(
        raw,
        "event_name",
        "event",
        "title",
        "name",
        "indicator",
        "description",
    )
    currency = first_present(
        raw,
        "currency",
        "country_currency",
        "ccy",
        "symbol",
        "country",
    )
    impact = first_present(
        raw,
        "impact",
        "importance",
        "priority",
        "strength",
    )
    forecast = first_present(raw, "forecast", "consensus", "expected")
    actual = first_present(raw, "actual", "actual_value", "value")
    previous = first_present(raw, "previous", "prior", "prev", "revised")
    release_time = first_present(
        raw,
        "release_time",
        "time_utc",
        "time",
        "date",
        "datetime",
        "timestamp",
        "calendar_time",
    )
    provider_timestamp = first_present(
        raw,
        "provider_timestamp",
        "updated_at",
        "last_update",
    )

    normalized_currency = normalize_currency(currency, raw)
    normalized_event = str(event or "").strip()

    if not normalized_event or not normalized_currency:
        return None

    parsed_time = normalize_event_time(release_time)
    source = str(raw.get("source") or "").strip() or None
    return {
        "event": normalized_event,
        "event_name": normalized_event,
        "currency": normalized_currency,
        "impact": normalize_impact(impact),
        "forecast": empty_to_none(forecast),
        "actual": empty_to_none(actual),
        "previous": empty_to_none(previous),
        "release_time": parsed_time,
        "time": parsed_time,
        "time_utc": parsed_time.isoformat() if parsed_time else None,
        "source": source,
        "provider_timestamp": provider_timestamp,
        "raw": raw,
    }


def first_present(source, *keys):
    for key in keys:
        value = source.get(key)
        if value in [None, "", "--", "N/A"]:
            value = source.get(key.upper())
        if value in [None, "", "--", "N/A"]:
            value = source.get(key.title())
        if value in [None, "", "--", "N/A"]:
            normalized_key = key.replace("_", "").lower()
            for source_key, source_value in source.items():
                source_normalized = str(source_key).replace("_", "").lower()
                if source_normalized == normalized_key:
                    value = source_value
                    break
        if value not in [None, "", "--", "N/A"]:
            return value
    return None


def normalize_currency(currency, raw=None):
    text = str(currency or "").upper().strip()
    if text in ["US", "USA", "UNITED STATES"]:
        return "USD"
    if text in ["EU", "EMU", "EUROZONE", "EURO AREA"]:
        return "EUR"
    if text in ["XAU", "GOLD", "XAUUSD"]:
        return "XAUUSD"

    match = re.search(r"\b[A-Z]{3}\b", text)
    if match:
        code = match.group(0)
        if code in ["USD", "EUR", "XAU"]:
            return "XAUUSD" if code == "XAU" else code

    country = str((raw or {}).get("country") or "").upper()
    if "UNITED STATES" in country:
        return "USD"
    if "EURO" in country or "GERMANY" in country or "FRANCE" in country:
        return "EUR"

    return text[:3] if len(text) >= 3 else ""


def normalize_impact(impact):
    text = str(impact or "").upper().strip()
    if "HIGH" in text or text in ["3", "RED"]:
        return "HIGH"
    if "MED" in text or text in ["2", "ORANGE"]:
        return "MEDIUM"
    if "LOW" in text or text in ["1", "YELLOW"]:
        return "LOW"
    return "UNKNOWN"


def normalize_event_time(value):
    if value in [None, "", "--", "N/A"]:
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m-%d-%Y %H:%M",
            "%m/%d/%Y %H:%M",
            "%b %d, %Y %H:%M",
            "%Y.%m.%d %H:%M",
        ]:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def empty_to_none(value):
    if value in [None, "", "--", "N/A"]:
        return None
    return value


def parse_numeric(value):
    if value in [None, "", "--", "N/A"]:
        return None

    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000.0
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.upper().endswith("B"):
        multiplier = 1_000_000_000.0
        text = text[:-1]

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return None


def event_is_relevant(event, symbol):
    normalized_symbol = normalize_symbol(symbol)
    return event.get("currency") in SYMBOL_CURRENCIES[normalized_symbol]


def event_is_important(event):
    title = str(
        event.get("event_name")
        or event.get("event")
        or ""
    ).lower()
    return any(keyword in title for keyword in IMPORTANT_EVENT_KEYWORDS)


def event_is_high_impact(event):
    return event.get("impact") == "HIGH"


def count_relevant_events(events, symbol):
    return len([event for event in events if event_is_relevant(event, symbol)])


def count_relevant_high_impact_events(events, symbol):
    return len([
        event for event in events
        if event_is_relevant(event, symbol)
        and event_is_high_impact(event)
        and event_is_important(event)
    ])


def get_event_time(event):
    return event.get("release_time") or event.get("time")


def event_display_name(event):
    return str(event.get("event_name") or event.get("event") or "").strip()


def normalized_event_title(event):
    name = event_display_name(event).lower()
    name = re.sub(r"^manual calendar fallback:\s*", "", name)
    name = re.sub(r"\b(us|usd|eurozone|euro area|eur)\b", "", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def upcoming_event_group(event):
    name = normalized_event_title(event)
    for group_name, patterns in UPCOMING_EVENT_PRIORITY:
        if any(pattern in name for pattern in patterns):
            return group_name
    return name


def upcoming_event_priority(event):
    group = upcoming_event_group(event)
    for index, (group_name, _) in enumerate(UPCOMING_EVENT_PRIORITY):
        if group == group_name:
            return index
    return len(UPCOMING_EVENT_PRIORITY)


def upcoming_event_display_priority(event):
    name = normalized_event_title(event)
    if "fomc rate decision" in name or "fed interest rate" in name or "federal funds rate" in name:
        return 0
    if "fomc press conference" in name or "fed press conference" in name or "fed chair" in name or "powell" in name:
        return 1
    if "non farm payroll" in name or "nonfarm payroll" in name or "nfp" in name or "unemployment rate" in name:
        return 2
    if name == "cpi" or name.endswith(" cpi") and "core cpi" not in name or "consumer price" in name:
        return 3
    if "core cpi" in name:
        return 4
    if "pce" in name or "personal consumption expenditures" in name:
        return 5
    if "gdp" in name or "gross domestic product" in name:
        return 6
    if "retail sales" in name:
        return 7
    if "ecb rate decision" in name or "ecb interest rate" in name:
        return 8
    return len(UPCOMING_EVENT_PRIORITY)


def event_deduplication_key(event):
    release_time = get_event_time(event)
    event_date = release_time.date().isoformat() if release_time else ""
    return (
        str(event.get("currency") or "").upper(),
        upcoming_event_group(event),
        event_date,
    )


def event_report_key(event):
    release_time = get_event_time(event)
    return (
        str(event.get("currency") or "").upper(),
        upcoming_event_group(event),
        release_time.isoformat() if release_time else "",
    )


def serialize_upcoming_event(event, source=None, now=None):
    release_time = get_event_time(event)
    event_source = event.get("source") or source or _CALENDAR_CACHE.get("last_source")
    return {
        "event_name": event_display_name(event),
        "currency": event.get("currency"),
        "impact": event.get("impact"),
        "time": release_time.isoformat() if release_time else None,
        "time_utc": release_time.isoformat() if release_time else None,
        "countdown": format_time_until(release_time, now=now),
        "source": event_source,
        "source_label": (
            "Manual calendar fallback"
            if str(event_source or "").startswith("manual")
            else str(event_source or "").upper()
        ),
    }


def get_upcoming_high_impact_events(
    events,
    symbol,
    limit=2,
    now=None,
    source=None,
    current_event=None,
):
    now = now or utc_now()
    current_group = upcoming_event_group(current_event) if current_event else None
    current_time = get_event_time(current_event) if current_event else None
    candidates = [
        event for event in events
        if event_is_relevant(event, symbol)
        and event_is_high_impact(event)
        and event_is_important(event)
        and (
            not get_event_time(event)
            or get_event_time(event) >= now
        )
    ]
    candidates.sort(
        key=lambda event: (
            get_event_time(event) or datetime.max.replace(tzinfo=timezone.utc),
            upcoming_event_priority(event),
            upcoming_event_display_priority(event),
            event_display_name(event).lower(),
        )
    )

    grouped = {}
    for event in candidates:
        event_time = get_event_time(event)
        event_group = upcoming_event_group(event)
        if (
            current_event
            and current_group == event_group
            and (
                not current_time
                or not event_time
                or event_time >= current_time
            )
        ):
            continue

        report_key = event_report_key(event)
        existing = grouped.get(report_key)
        if (
            not existing
            or upcoming_event_display_priority(event) < upcoming_event_display_priority(existing)
        ):
            grouped[report_key] = event

    ordered = sorted(
        grouped.values(),
        key=lambda event: (
            get_event_time(event) or datetime.max.replace(tzinfo=timezone.utc),
            upcoming_event_priority(event),
            upcoming_event_display_priority(event),
            event_display_name(event).lower(),
        ),
    )

    unique = []
    seen = set()
    for event in ordered:
        key = event_deduplication_key(event)
        if key in seen:
            continue
        seen.add(key)
        unique.append(serialize_upcoming_event(event, source=source, now=now))
        if len(unique) >= limit:
            break

    return unique


def choose_relevant_event(events, symbol, now=None):
    now = now or utc_now()
    relevant = [
        event for event in events
        if event_is_relevant(event, symbol)
        and event_is_high_impact(event)
        and event_is_important(event)
    ]

    if not relevant:
        return None

    upcoming = [
        event for event in relevant
        if not get_event_time(event) or get_event_time(event) >= now
    ]
    if upcoming:
        return min(
            upcoming,
            key=lambda event: (
                -IMPACT_WEIGHT.get(event.get("impact"), 1),
                get_event_time(event)
                or datetime.max.replace(tzinfo=timezone.utc),
            ),
        )

    released_with_actual = [
        event for event in relevant
        if event.get("actual") is not None
        and event.get("forecast") is not None
    ]
    if released_with_actual:
        return max(
            released_with_actual,
            key=lambda event: (
                IMPACT_WEIGHT.get(event.get("impact"), 1),
                get_event_time(event)
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )

    return max(
        relevant,
        key=lambda event: (
            IMPACT_WEIGHT.get(event.get("impact"), 1),
            get_event_time(event)
            or datetime.min.replace(tzinfo=timezone.utc),
        ),
    )


def format_time_until(event_time, now=None):
    if not event_time:
        return "--"

    now = now or utc_now()
    seconds = int((event_time - now).total_seconds())
    released = seconds < 0
    seconds = abs(seconds)
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days:
        value = f"{days}d {hours % 24}h"
    elif hours:
        value = f"{hours}h {minutes % 60}m"
    else:
        value = f"{minutes}m"

    return f"Released {value} ago" if released else value


def classify_currency_bias(event):
    actual = parse_numeric(event.get("actual"))
    forecast = parse_numeric(event.get("forecast"))

    if actual is None or forecast is None:
        return "Neutral", 0

    title = str(event.get("event") or "").lower()
    if any(keyword in title for keyword in LOWER_IS_BETTER_KEYWORDS):
        if actual > forecast:
            direction = -1
        elif actual < forecast:
            direction = 1
        else:
            direction = 0
    elif any(keyword in title for keyword in BETTER_IS_BULLISH_KEYWORDS):
        if actual > forecast:
            direction = 1
        elif actual < forecast:
            direction = -1
        else:
            direction = 0
    else:
        direction = 0

    currency = event.get("currency")
    if direction > 0:
        return f"{currency} Bullish", direction
    if direction < 0:
        return f"{currency} Bearish", direction
    return "Neutral", 0


def score_currency_surprise(event, currency_direction):
    actual = parse_numeric(event.get("actual"))
    forecast = parse_numeric(event.get("forecast"))
    if actual is None or forecast is None or not currency_direction:
        return 0

    delta = abs(actual - forecast)
    baseline = max(abs(forecast), 0.1)
    surprise_ratio = delta / baseline
    title = str(event.get("event") or "").lower()

    if any(keyword in title for keyword in ["cpi", "pce", "inflation"]):
        if delta >= 0.2:
            magnitude = 25
        elif delta >= 0.1:
            magnitude = 20
        else:
            magnitude = 10
    elif any(keyword in title for keyword in ["payroll", "nfp", "employment", "jobs"]):
        if delta >= 100_000:
            magnitude = 25
        elif delta >= 50_000:
            magnitude = 20
        else:
            magnitude = 10
    elif any(keyword in title for keyword in ["unemployment", "jobless", "claims"]):
        if delta >= 0.3:
            magnitude = 25
        elif delta >= 0.1:
            magnitude = 20
        else:
            magnitude = 10
    else:
        if surprise_ratio >= 0.15:
            magnitude = 20
        elif surprise_ratio >= 0.05:
            magnitude = 10
        else:
            magnitude = 5

    return max(-25, min(25, magnitude * currency_direction))


def build_news_reason(event, news_bias):
    actual = event.get("actual")
    forecast = event.get("forecast")
    previous = event.get("previous")
    event_name = event.get("event_name") or event.get("event") or "event"

    if actual in [None, "", "--", "N/A"]:
        return (
            "Waiting for the actual release. "
            f"Forecast: {forecast or '--'}; Previous: {previous or '--'}."
        )
    if forecast in [None, "", "--", "N/A"]:
        return (
            f"Actual {event_name} ({actual}) is available, but forecast data "
            "is unavailable."
        )

    actual_value = parse_numeric(actual)
    forecast_value = parse_numeric(forecast)
    if actual_value is None or forecast_value is None:
        return (
            f"Actual {event_name} ({actual}) and forecast ({forecast}) are "
            "available, but the surprise could not be scored numerically."
        )

    title = str(event_name).lower()
    if any(keyword in title for keyword in LOWER_IS_BETTER_KEYWORDS):
        if actual_value < forecast_value:
            explanation = "The lower-than-expected reading is currency bullish."
        elif actual_value > forecast_value:
            explanation = "The higher-than-expected reading is currency bearish."
        else:
            explanation = "The release matched expectations."
    elif any(keyword in title for keyword in BETTER_IS_BULLISH_KEYWORDS):
        if actual_value > forecast_value:
            explanation = "The reading exceeded expectations."
        elif actual_value < forecast_value:
            explanation = "The reading missed expectations."
        else:
            explanation = "The release matched expectations."
    else:
        explanation = "The release has been compared with forecast."

    return (
        f"Actual {event_name} ({actual}); Forecast ({forecast}); "
        f"Previous ({previous or '--'}). {explanation} Bias: {news_bias}."
    )


def human_news_status(event, score):
    release_time = get_event_time(event)
    if event.get("actual") not in [None, "", "--", "N/A"]:
        return "Released"
    if not release_time:
        return "Data unavailable"
    if release_time > utc_now():
        return "Waiting for release"
    return "Waiting for actual data"


def direction_for_symbol(symbol, currency, currency_direction):
    if not currency_direction:
        return 0

    if symbol == "EURUSD":
        if currency == "EUR":
            return currency_direction
        if currency == "USD":
            return -currency_direction

    if symbol == "XAUUSD" and currency == "USD":
        return -currency_direction

    return 0


def score_news_direction(event, symbol, now=None):
    now = now or utc_now()
    normalized_symbol = normalize_symbol(symbol)
    release_time = get_event_time(event)

    actual_missing = event.get("actual") in [None, "", "--", "N/A"]
    forecast_missing = event.get("forecast") in [None, "", "--", "N/A"]

    if not release_time or release_time > now or actual_missing or forecast_missing:
        display_status = "Data unavailable"
        if event.get("actual") not in [None, "", "--", "N/A"]:
            display_status = "Released"
        elif release_time and release_time > now:
            display_status = "Waiting for release"
        elif release_time:
            display_status = "Waiting for actual data"

        return {
            "news_bias": "Neutral",
            "effect": f"{normalized_symbol} Neutral",
            "score": 0,
            "symbol_score": 0,
            "decision": "WAITING_FOR_ACTUAL_DATA",
            "display_status": display_status,
            "reason": build_news_reason(event, "Neutral"),
        }

    news_bias, currency_direction = classify_currency_bias(event)
    currency_score = score_currency_surprise(event, currency_direction)
    symbol_direction = direction_for_symbol(
        normalized_symbol,
        event.get("currency"),
        currency_direction,
    )

    if symbol_direction > 0:
        effect = f"{normalized_symbol} BUY PRESSURE"
        symbol_score = min(25, abs(currency_score)) or 10
    elif symbol_direction < 0:
        effect = f"{normalized_symbol} SELL PRESSURE"
        symbol_score = -(min(25, abs(currency_score)) or 10)
    else:
        effect = f"{normalized_symbol} Neutral"
        symbol_score = 0

    return {
        "news_bias": news_bias,
        "effect": effect,
        "score": max(-25, min(25, currency_score)),
        "symbol_score": max(-25, min(25, symbol_score)),
        "decision": "WAITING_FOR_ACTUAL_DATA" if currency_score == 0 else "SUPPORTS_SETUP",
        "display_status": "Released",
        "reason": build_news_reason(event, news_bias),
    }


def get_news_impact(
    symbol,
    candles_5m=None,
    candles_15m=None,
    entry_price=None,
    now=None,
    provider_timeout=8,
):
    normalized_symbol = normalize_symbol(symbol)
    now = now or utc_now()

    try:
        events = fetch_calendar_events(now=now, timeout=provider_timeout)
    except Exception as exc:
        _CALENDAR_CACHE["last_source"] = "manual"
        _CALENDAR_CACHE["last_error"] = str(exc)
        print("NEWS_IMPACT_ERROR =", {
            "symbol": normalized_symbol,
            "error": str(exc),
        })
        events = build_manual_calendar_events(now=now)

    source = _CALENDAR_CACHE.get("last_source") or "manual"
    raw_event_count = int(_CALENDAR_CACHE.get("raw_event_count") or len(events))
    filtered_event_count = count_relevant_high_impact_events(
        events,
        normalized_symbol,
    )
    relevant_event_count = count_relevant_events(events, normalized_symbol)
    event = choose_relevant_event(events, normalized_symbol, now=now)
    upcoming_high_impact = get_upcoming_high_impact_events(
        events,
        normalized_symbol,
        limit=2,
        now=now,
        source=source,
        current_event=event,
    )
    print("NEWS_IMPACT_FILTER_DEBUG =", {
        "symbol": normalized_symbol,
        "selected_provider": source,
        "api_status": _CALENDAR_CACHE.get("api_status") or [],
        "events_returned": raw_event_count,
        "relevant_events_after_filtering": filtered_event_count,
        "relevant_currency_events": relevant_event_count,
        "upcoming_high_impact_count": len(upcoming_high_impact),
    })
    if not event:
        print("NEWS_IMPACT_FINAL_EVENT =", {
            "symbol": normalized_symbol,
            "selected_provider": source,
            "event": None,
            "reason": "NO_MAJOR_NEWS",
        })
        return safe_news_fallback(
            normalized_symbol,
            decision="NO_MAJOR_NEWS",
            source=source,
            is_fallback=source.startswith("manual"),
            raw_event_count=raw_event_count,
            filtered_event_count=filtered_event_count,
        )

    original_event = event
    event = refresh_actual_after_release(
        event,
        normalized_symbol,
        now=now,
        timeout=provider_timeout,
    )
    if event is not original_event:
        original_key = event_identity_key(normalized_symbol, original_event)
        events = [
            event
            if event_identity_key(normalized_symbol, candidate) == original_key
            else candidate
            for candidate in events
        ]
    score = score_news_direction(event, normalized_symbol, now=now)
    release_time = get_event_time(event)

    try:
        from services import news_trading

        fetched_at = float(_CALENDAR_CACHE.get("fetched_at") or time.time())
        news_trading_status = news_trading.evaluate(
            events,
            normalized_symbol,
            candles_5m=candles_5m,
            candles_15m=candles_15m,
            entry_price=entry_price,
            now=now,
            data_age_seconds=max(0.0, time.time() - fetched_at),
        )
    except Exception as exc:
        print("NEWS_TRADING_STATUS_ERROR =", {
            "symbol": normalized_symbol,
            "error": str(exc),
        })
        news_trading_status = {
            "mode": os.getenv("NEWS_TRADING_MODE", "OFF").upper(),
            "phase": "RELEASE_LOCK",
            "authoritative_status": "NEWS BLOCK",
            "allow_normal_entry": False,
            "allow_news_entry": False,
            "blocking_reason": "WAIT_NEWS_STATUS_UNAVAILABLE",
        }

    result = {
        "symbol": normalized_symbol,
        "event": event.get("event_name") or event.get("event"),
        "event_name": event.get("event_name") or event.get("event"),
        "next_event": event.get("event_name") or event.get("event"),
        "currency": event.get("currency"),
        "impact": event.get("impact"),
        "forecast": event.get("forecast"),
        "actual": event.get("actual"),
        "previous": event.get("previous"),
        "release_time": release_time.isoformat() if release_time else None,
        "time": release_time.isoformat() if release_time else None,
        "time_utc": release_time.isoformat() if release_time else None,
        "time_until": format_time_until(release_time, now=now),
        "countdown": format_time_until(release_time, now=now),
        "news_bias": score["news_bias"],
        "display_status": score["display_status"],
        "status": score["display_status"],
        "news_reason": score["reason"],
        "reason": score["reason"],
        "effect": score["effect"],
        "score": score["score"],
        "news_score": score["score"],
        "symbol_score": score.get("symbol_score", score["score"]),
        "decision": score["decision"],
        "last_update": iso_timestamp(now),
        "source": event.get("source") or source,
        "source_used": event.get("source") or source,
        "source_label": (
            "Manual calendar fallback"
            if (event.get("source") or source).startswith("manual")
            else (event.get("source") or source).upper()
        ),
        "upcoming_high_impact": upcoming_high_impact,
        **build_news_debug(
            normalized_symbol,
            source=source,
            is_live_news=source in [
                "jblanked_live", "jblanked_cache", "fmp", "finnhub"
            ],
            is_fallback=source.startswith("manual"),
            raw_event_count=raw_event_count,
            filtered_event_count=filtered_event_count,
        ),
        "relevant_event_count": relevant_event_count,
        "news_trading": news_trading_status,
        "trade_decision": news_trading_status.get("authoritative_status"),
        "blocking_reason": news_trading_status.get("blocking_reason"),
        "expected_symbol_direction": news_trading_status.get(
            "expected_symbol_direction"
        ),
        "normalized_surprise": news_trading_status.get(
            "normalized_surprise"
        ),
        "opportunity_expiration": news_trading_status.get(
            "opportunity_expiration"
        ),
    }
    print("NEWS_IMPACT_FINAL_EVENT =", {
        "symbol": normalized_symbol,
        "selected_provider": source,
        "event": result["event_name"],
        "currency": result["currency"],
        "impact": result["impact"],
        "time_utc": result["time_utc"],
        "source": result["source_label"],
    })
    return result
