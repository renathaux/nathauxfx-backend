"""Display-only V3B dashboard state bridge.

The production trading engine already keeps its authoritative per-symbol V3B
runtime result in ``LIVE_AUTO_STATUS_BY_SYMBOL``.  The legacy dashboard payload,
however, can still contain the older 15m strategy block reason.  This module
projects the current V3B runtime state into dashboard JSON without changing
signals, execution eligibility, risk, order submission, or lifecycle state.
"""
from __future__ import annotations

import copy
import json

from fastapi.responses import Response


_DASHBOARD_PATHS = {"/panel-data", "/dashboard-feed"}


def _text(value):
    return str(value or "").strip()


def _is_v3b_payload(payload, live_status_by_symbol):
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    for value in (
        meta.get("live_strategy_identity"),
        meta.get("live_strategy_id"),
        meta.get("live_strategy_model"),
        payload.get("live_strategy_identity"),
        payload.get("live_strategy_model"),
    ):
        if "V3B" in _text(value).upper():
            return True

    for status in (live_status_by_symbol or {}).values():
        if not isinstance(status, dict):
            continue
        reason = _text(status.get("reason")).upper()
        details = status.get("details") if isinstance(status.get("details"), dict) else {}
        profile = _text(details.get("strategy_execution_profile")).upper()
        source = details.get("source_candidate") if isinstance(details.get("source_candidate"), dict) else {}
        model = _text(source.get("live_strategy_model")).upper()
        if "V3B" in reason or "V3B" in profile or "V3B" in model:
            return True
    return False


def enrich_dashboard_payload(payload, live_status_by_symbol, *, signal_history=None):
    """Return a copy with authoritative V3B runtime diagnostics per symbol.

    Generic dashboard blocker fields follow the active V3B runtime rather than
    the obsolete 15m diagnostic. This is presentation-only; the trading engine
    and stored indicator-stream state are not changed.
    """
    if not isinstance(payload, dict):
        return payload

    result = copy.deepcopy(payload)
    statuses = live_status_by_symbol if isinstance(live_status_by_symbol, dict) else {}
    if not _is_v3b_payload(result, statuses):
        return result
    if signal_history is not None:
        # The legacy process-memory list is never authoritative while V3B runs.
        result["history"] = copy.deepcopy(signal_history[:10])
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    explicit_v3b_identity = any(
        "V3B" in _text(value).upper()
        for value in (
            meta.get("live_strategy_identity"),
            meta.get("live_strategy_id"),
            meta.get("live_strategy_model"),
            result.get("live_strategy_identity"),
            result.get("live_strategy_model"),
        )
    )

    for symbol in ("EURUSD", "XAUUSD"):
        plan = result.get(symbol)
        status = statuses.get(symbol)
        if not isinstance(plan, dict) or not isinstance(status, dict):
            continue

        reason = status.get("reason")
        details = status.get("details") if isinstance(status.get("details"), dict) else {}
        details = copy.deepcopy(details)
        account_scope = _text(meta.get("account_scope"))
        status_scope = _text(details.get("account_scope"))
        if account_scope and status_scope != account_scope:
            # A previous account's in-flight evaluation is never a display
            # candidate for the currently selected account.
            continue

        source_candidate = details.get("source_candidate")
        if not isinstance(source_candidate, dict):
            source_candidate = {}

        model = (
            source_candidate.get("live_strategy_model")
            or plan.get("live_strategy_model")
            or "LIVE_V3B_M5_FROZEN"
        )

        plan["live_strategy_model"] = model
        plan["live_v3b_reason"] = reason
        plan["live_v3b_details"] = details
        plan["live_v3b_status"] = status.get("status")
        plan["live_v3b_checked_at"] = status.get("checked_at")

        if _text(reason) and ("V3B" in _text(reason).upper() or explicit_v3b_identity):
            waiting = _text(status.get("status")).upper() in {"WAIT", "BLOCKED"}
            active_reason = reason if waiting else None
            plan["block_reason"] = active_reason
            plan["blocked_reason"] = active_reason
            plan["plan_reason"] = active_reason
            plan["blocked_by"] = "v3b_runtime" if waiting else None
            plan["blocker_rule_name"] = "v3b_runtime" if waiting else None

    return result


def install_v3b_dashboard_state_middleware(app, api_module):
    """Install a response-only bridge for dashboard GET endpoints."""
    if getattr(app.state, "v3b_dashboard_state_middleware_installed", False):
        return False

    @app.middleware("http")
    async def _v3b_dashboard_state_response(request, call_next):
        response = await call_next(request)
        if (
            str(request.method or "").upper() != "GET"
            or str(request.url.path or "") not in _DASHBOARD_PATHS
            or int(response.status_code) != 200
            or "application/json" not in _text(response.headers.get("content-type")).lower()
        ):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            payload = json.loads(body.decode("utf-8"))
            signal_history = None
            if _is_v3b_payload(payload, getattr(api_module, "LIVE_AUTO_STATUS_BY_SYMBOL", {})):
                from services.v3b_signal_history import list_v3b_transitions

                meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
                try:
                    signal_history = list_v3b_transitions(meta.get("account_scope"), limit=10)
                except Exception:
                    # Never expose stale legacy rows as if they were V3B history.
                    signal_history = []
            enriched = enrich_dashboard_payload(
                payload,
                getattr(api_module, "LIVE_AUTO_STATUS_BY_SYMBOL", {}),
                signal_history=signal_history,
            )
            encoded = json.dumps(
                enriched,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            encoded = body

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        }
        return Response(
            content=encoded,
            status_code=response.status_code,
            headers=headers,
            media_type="application/json",
            background=response.background,
        )

    app.state.v3b_dashboard_state_middleware_installed = True
    return True
