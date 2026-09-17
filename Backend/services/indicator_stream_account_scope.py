"""Account-scope the durable SMC stream without changing the database schema.

The original durable indicator tables predate cTrader account switching and key a
stream only by ``symbol`` + ``timeframe``. Different cTrader accounts can expose
slightly different historical trendbars for the same public symbol, so reusing
that key can incorrectly put a healthy stream into RECONCILIATION_REQUIRED.

This compatibility layer keeps the public strategy/chart symbol unchanged while
using a deterministic, account-specific storage symbol internally. Existing
legacy rows are left untouched. Switching back to a previously used account
reuses the same deterministic stream instead of rebuilding or rewriting it.
"""
from __future__ import annotations

import copy
import hashlib
import threading

import pandas as pd
from ctrader_account_context import AccountIdentity, current_identity, pinned_account

from services import indicator_event_stream_service as stream
from services.broker_account_state_service import load_active_account_selection


_INSTALL_LOCK = threading.RLock()
_INSTALLED = False
_ORIGINALS = {}
_SCOPE_META_BY_STORAGE = {}


def _normal_public_symbol(value):
    text = str(value or "").upper().replace("/", "")
    return text.split("~", 1)[0]


def _normal_scope(value):
    text = str(value or "").strip().upper()
    return text or None


def active_ctrader_stream_scope():
    """Return a stable stream scope for the currently selected cTrader account."""
    if current_identity() is not None:
        return current_identity().scope
    selected = load_active_account_selection() or {}
    account_id = str(selected.get("active_account_id") or "").strip()
    environment = str(selected.get("active_account_env") or "").strip().upper()
    if not account_id or environment not in {"DEMO", "LIVE"}:
        return None
    return f"CTRADER:{environment}:{account_id}"


def storage_symbol_for_scope(symbol, scope):
    """Build a <=20-char deterministic DB key while retaining the public prefix."""
    public = _normal_public_symbol(symbol)
    normalized_scope = _normal_scope(scope)
    if not normalized_scope:
        return public
    digest = hashlib.sha256(f"{public}|{normalized_scope}".encode("utf-8")).hexdigest()[:10].upper()
    storage = f"{public[:9]}~{digest}"
    _SCOPE_META_BY_STORAGE[storage] = {
        "public_symbol": public,
        "stream_scope": normalized_scope,
    }
    return storage


def _scope_meta(storage_symbol, payload=None):
    storage = str(storage_symbol or "").upper()
    payload = payload if isinstance(payload, dict) else {}
    public = str(payload.get("public_symbol") or "").upper().replace("/", "")
    scope = _normal_scope(payload.get("stream_scope"))
    known = _SCOPE_META_BY_STORAGE.get(storage) or {}
    public = public or known.get("public_symbol") or _normal_public_symbol(storage)
    scope = scope or known.get("stream_scope")
    return public, scope


def _resolved_scope(stream_scope, session_factory):
    explicit = _normal_scope(stream_scope)
    if explicit:
        return explicit
    # Unit tests inject their own DB sessions. Do not make those tests depend on
    # the production RuntimeSetting table unless a scope is explicitly supplied.
    if session_factory is not None:
        return None
    return active_ctrader_stream_scope()


def _scoped_analyzer(analyzer, public_symbol, scope):
    def wrapped(frame, **kwargs):
        result = analyzer(frame, **kwargs)
        output = copy.deepcopy(result or {})
        events = []
        for raw in output.get("events") or []:
            if not isinstance(raw, dict):
                events.append(raw)
                continue
            event = copy.deepcopy(raw)
            event["public_symbol"] = public_symbol
            event["stream_scope"] = scope
            events.append(event)
        output["events"] = events
        return output

    return wrapped


def account_scoped_get_authoritative_structure(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=None,
    session_factory=None,
    initialize=False,
    allow_sparse_trendbars=False,
    stream_scope=None,
):
    original = _ORIGINALS.get("get_authoritative_structure") or stream.get_authoritative_structure
    public = _normal_public_symbol(symbol)
    # Internal recursive retries from the original service already carry the
    # deterministic storage key. Do not scope it a second time.
    if "~" in str(symbol or ""):
        kwargs = {
            "session_factory": session_factory,
            "initialize": initialize,
            "allow_sparse_trendbars": allow_sparse_trendbars,
        }
        if analyzer is not None:
            kwargs["analyzer"] = analyzer
        return original(frame, symbol, timeframe, point_size, **kwargs)

    scope = _resolved_scope(stream_scope, session_factory)
    frame_scope = getattr(frame, "attrs", {}).get("ctrader_stream_scope")
    if frame_scope and frame_scope != scope:
        raise stream.IndicatorStreamUnavailable("candle account scope does not match target account")
    if not scope:
        kwargs = {
            "session_factory": session_factory,
            "initialize": initialize,
            "allow_sparse_trendbars": allow_sparse_trendbars,
        }
        if analyzer is not None:
            kwargs["analyzer"] = analyzer
        return original(frame, public, timeframe, point_size, **kwargs)

    storage = storage_symbol_for_scope(public, scope)
    effective_analyzer = analyzer or stream.legacy_analyze_structure
    wrapped_analyzer = _scoped_analyzer(effective_analyzer, public, scope)
    kwargs = {
        "analyzer": wrapped_analyzer,
        "session_factory": session_factory,
        "initialize": initialize,
        # cTrader creates trendbars only when ticks exist. Account-scoped streams
        # must therefore permit sparse broker bars but never synthesize OHLC.
        "allow_sparse_trendbars": True,
        # This wrapper resolves the active cTrader account. Keep the controlled
        # correction exception on its V3B 5m storage key only.
        "allow_authoritative_correction_repair": (
            public in {"EURUSD", "XAUUSD"}
            and stream._normal_timeframe(timeframe) == "5m"
        ),
    }
    if (
        scope == "CTRADER:DEMO:47810571"
        and public in {"EURUSD", "XAUUSD"}
        and stream._normal_timeframe(timeframe) == "5m"
    ):
        def fetch_closed_history(start, end):
            from ctrader_connector import fetch_ctrader_historical_candles
            from strategies import strict_trader

            with pinned_account(AccountIdentity("47810571", "demo")):
                fresh = fetch_ctrader_historical_candles(public, "5m", start, end)
            if getattr(fresh, "attrs", {}).get("ctrader_stream_scope") != scope:
                raise stream.IndicatorStreamUnavailable(
                    "fresh broker history account scope does not match V3B stream"
                )
            return strict_trader.closed_frame(fresh, 5)

        kwargs["revalidation_fetcher"] = fetch_closed_history
    try:
        result = original(frame, storage, timeframe, point_size, **kwargs)
    except stream.IndicatorStreamUnavailable as exc:
        if initialize or "indicator stream is not initialized" not in str(exc).lower():
            raise
        kwargs["initialize"] = True
        result = original(frame, storage, timeframe, point_size, **kwargs)

    result = copy.deepcopy(result or {})
    result["symbol"] = public
    result["stream_scope"] = scope
    result["storage_symbol"] = storage
    return result


def account_scoped_initialize_indicator_stream(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=None,
    session_factory=None,
    allow_sparse_trendbars=False,
    stream_scope=None,
):
    return account_scoped_get_authoritative_structure(
        frame,
        symbol,
        timeframe,
        point_size,
        analyzer=analyzer,
        session_factory=session_factory,
        initialize=True,
        allow_sparse_trendbars=allow_sparse_trendbars,
        stream_scope=stream_scope,
    )


def account_scoped_read_authoritative_structure(
    frame,
    symbol,
    timeframe,
    point_size,
    *,
    analyzer=None,
    session_factory=None,
    stream_scope=None,
):
    original = _ORIGINALS.get("read_authoritative_structure") or stream.read_authoritative_structure
    public = _normal_public_symbol(symbol)
    if "~" in str(symbol or ""):
        kwargs = {"session_factory": session_factory}
        if analyzer is not None:
            kwargs["analyzer"] = analyzer
        return original(frame, symbol, timeframe, point_size, **kwargs)

    scope = _resolved_scope(stream_scope, session_factory)
    frame_scope = getattr(frame, "attrs", {}).get("ctrader_stream_scope")
    if frame_scope and frame_scope != scope:
        raise stream.IndicatorStreamUnavailable("candle account scope does not match target account")
    if not scope:
        kwargs = {"session_factory": session_factory}
        if analyzer is not None:
            kwargs["analyzer"] = analyzer
        return original(frame, public, timeframe, point_size, **kwargs)

    storage = storage_symbol_for_scope(public, scope)
    effective_analyzer = analyzer or stream.legacy_analyze_structure
    wrapped_analyzer = _scoped_analyzer(effective_analyzer, public, scope)
    try:
        result = original(
            frame,
            storage,
            timeframe,
            point_size,
            analyzer=wrapped_analyzer,
            session_factory=session_factory,
        )
    except stream.IndicatorStreamUnavailable as exc:
        if "indicator stream is not initialized" not in str(exc).lower():
            raise
        # First access for a newly selected account creates an isolated stream.
        # The underlying service marks every event in this initial backfill as
        # historical, so this cannot retroactively create a LIVE setup.
        result = account_scoped_get_authoritative_structure(
            frame,
            public,
            timeframe,
            point_size,
            analyzer=effective_analyzer,
            session_factory=session_factory,
            initialize=True,
            stream_scope=scope,
        )

    result = copy.deepcopy(result or {})
    result["symbol"] = public
    result["stream_scope"] = scope
    result["storage_symbol"] = storage
    return result


def _safe_bridge_utc_factory(original_utc):
    def safe_utc(value):
        if value is None:
            raise ValueError("timestamp unavailable")
        stamp = original_utc(value)
        if pd.isna(stamp):
            raise ValueError("timestamp unavailable")
        return stamp

    return safe_utc


def install_account_scoped_indicator_stream():
    """Install account scoping before API/strategy modules bind stream functions."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return {"ok": True, "installed": False, "reason": "already_installed"}

        _ORIGINALS.update({
            "initialize_indicator_stream": stream.initialize_indicator_stream,
            "get_authoritative_structure": stream.get_authoritative_structure,
            "read_authoritative_structure": stream.read_authoritative_structure,
            "build_event_identity": stream.build_event_identity,
            "event_payload": stream._event_payload,
            "expected_market_candle": stream._expected_market_candle,
            "known_market_closure": stream._known_market_closure,
        })

        original_build_identity = _ORIGINALS["build_event_identity"]
        original_event_payload = _ORIGINALS["event_payload"]
        original_expected = _ORIGINALS["expected_market_candle"]
        original_closure = _ORIGINALS["known_market_closure"]

        def scoped_build_event_identity(event, symbol, timeframe, point_size):
            identity, event_id = original_build_identity(event, symbol, timeframe, point_size)
            public, scope = _scope_meta(symbol, event)
            identity = copy.deepcopy(identity)
            identity["symbol"] = public
            if scope:
                identity["stream_scope"] = scope
            return identity, event_id

        def scoped_event_payload(row):
            payload = original_event_payload(row)
            public, scope = _scope_meta(row.symbol, payload)
            payload["symbol"] = public
            payload.pop("public_symbol", None)
            if scope:
                payload["stream_scope"] = scope
            identity = payload.get("event_identity")
            if isinstance(identity, dict):
                identity = copy.deepcopy(identity)
                identity["symbol"] = public
                if scope:
                    identity["stream_scope"] = scope
                payload["event_identity"] = identity
            return payload

        def scoped_expected_market_candle(symbol, timestamp):
            return original_expected(_normal_public_symbol(symbol), timestamp)

        def scoped_known_market_closure(symbol, previous, following):
            return original_closure(_normal_public_symbol(symbol), previous, following)

        stream.build_event_identity = scoped_build_event_identity
        stream._event_payload = scoped_event_payload
        stream._expected_market_candle = scoped_expected_market_candle
        stream._known_market_closure = scoped_known_market_closure
        stream.initialize_indicator_stream = account_scoped_initialize_indicator_stream
        stream.get_authoritative_structure = account_scoped_get_authoritative_structure
        stream.read_authoritative_structure = account_scoped_read_authoritative_structure

        # Fix a separate error uncovered by the account switch: pd.Timestamp(None)
        # produces NaT, which previously flowed into int(NaN) while building V3B
        # freshness diagnostics and masked the real authority error.
        from services import paper_v3b_bridge as bridge
        _ORIGINALS["paper_bridge_utc"] = bridge._utc
        bridge._utc = _safe_bridge_utc_factory(bridge._utc)

        _INSTALLED = True
        return {"ok": True, "installed": True, "reason": None}


def uninstall_account_scoped_indicator_stream_for_tests():
    """Restore module globals after isolated tests; never called in production."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            return
        stream.initialize_indicator_stream = _ORIGINALS["initialize_indicator_stream"]
        stream.get_authoritative_structure = _ORIGINALS["get_authoritative_structure"]
        stream.read_authoritative_structure = _ORIGINALS["read_authoritative_structure"]
        stream.build_event_identity = _ORIGINALS["build_event_identity"]
        stream._event_payload = _ORIGINALS["event_payload"]
        stream._expected_market_candle = _ORIGINALS["expected_market_candle"]
        stream._known_market_closure = _ORIGINALS["known_market_closure"]
        if "paper_bridge_utc" in _ORIGINALS:
            from services import paper_v3b_bridge as bridge
            bridge._utc = _ORIGINALS["paper_bridge_utc"]
        _SCOPE_META_BY_STORAGE.clear()
        _ORIGINALS.clear()
        _INSTALLED = False
