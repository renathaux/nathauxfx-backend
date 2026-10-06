"""Small process-local cache for read-only Fundamental Insight responses."""

from __future__ import annotations

import copy
import os
import threading
import time


DEFAULT_TTL_SECONDS = 600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900


def _configured_ttl_seconds(environment=None):
    environment = os.environ if environment is None else environment
    raw_value = environment.get(
        "FUNDAMENTAL_INSIGHT_CACHE_TTL_SECONDS",
        str(DEFAULT_TTL_SECONDS),
    )
    try:
        configured = int(raw_value)
    except (TypeError, ValueError):
        configured = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, configured))


CACHE_TTL_SECONDS = _configured_ttl_seconds()
_STATE_LOCK = threading.RLock()
_ENTRIES = {}
_SYMBOL_LOCKS = {}


def _normalized_symbol(symbol):
    return str(symbol or "").upper().replace("/", "")


def _symbol_lock(symbol):
    with _STATE_LOCK:
        return _SYMBOL_LOCKS.setdefault(symbol, threading.Lock())


def _read_fresh(symbol, now_monotonic, ttl_seconds):
    with _STATE_LOCK:
        entry = _ENTRIES.get(symbol)
        if entry is None or now_monotonic - entry["stored_at"] >= ttl_seconds:
            return None
        return copy.deepcopy(entry["response"])


def get_or_calculate(
    symbol,
    calculate,
    *,
    bypass=False,
    ttl_seconds=None,
    monotonic=None,
):
    """Return one symbol's cached response or calculate it exactly once.

    Different symbols use different locks, so a slow EURUSD miss does not block
    an XAUUSD hit/miss. A bypass still refreshes the cache for later callers.
    """
    normalized = _normalized_symbol(symbol)
    ttl = CACHE_TTL_SECONDS if ttl_seconds is None else max(0, float(ttl_seconds))
    clock = monotonic or time.monotonic
    if not bypass:
        cached = _read_fresh(normalized, clock(), ttl)
        if cached is not None:
            return cached

    with _symbol_lock(normalized):
        if not bypass:
            cached = _read_fresh(normalized, clock(), ttl)
            if cached is not None:
                return cached
        response = calculate()
        with _STATE_LOCK:
            _ENTRIES[normalized] = {
                "stored_at": clock(),
                "response": copy.deepcopy(response),
            }
        return copy.deepcopy(response)


def invalidate(symbol=None):
    """Invalidate one symbol or every Fundamental Insight response."""
    normalized = _normalized_symbol(symbol) if symbol else None
    with _STATE_LOCK:
        if normalized:
            return int(_ENTRIES.pop(normalized, None) is not None)
        removed = len(_ENTRIES)
        _ENTRIES.clear()
        return removed


def _reset_for_tests():
    with _STATE_LOCK:
        _ENTRIES.clear()
        _SYMBOL_LOCKS.clear()
