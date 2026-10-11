"""Read-only V3B vs Strategy Studio entry-parity diagnostics.

This module deliberately has no database-write, broker-order, LIVE Auto, or
Strategy Studio activation imports.  It compares the frozen V3B entry rules to
the shared Strategy Studio evaluator on the same closed 5m candles.  Legacy-only
SL buffer/minimum-distance rules are applied only as comparison inputs; they are
not added to the user-facing Strategy Studio schema.
"""
from __future__ import annotations

import hashlib
import json
import math

import pandas as pd

from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.market_facts import build_market_facts
from services.strategy_engine.types import EvaluationState
from services.strategy_lab import v3b_m5_frozen_candidate as eur_v3b
from services.strategy_lab import v3b_xauusd_frozen_candidate as gold_v3b


PARITY_SCOPE = ["side", "event_time", "confirmation_time", "entry", "sl", "tp2"]


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _frame(frame_5m: pd.DataFrame) -> pd.DataFrame:
    if frame_5m is None or not isinstance(frame_5m, pd.DataFrame) or frame_5m.empty:
        raise ValueError("PARITY_HISTORY_UNAVAILABLE")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame_5m.columns):
        raise ValueError("PARITY_HISTORY_INVALID")
    data = frame_5m.copy().sort_index()
    data.index = pd.to_datetime(data.index, utc=True)
    data = data[~data.index.duplicated(keep="last")]
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    if data.empty:
        raise ValueError("PARITY_HISTORY_UNAVAILABLE")
    if "Volume" not in data.columns:
        data["Volume"] = 0.0
    return data


def _legacy_module(symbol: str):
    public_symbol = str(symbol or "").upper().replace("/", "")
    if public_symbol == "EURUSD":
        return public_symbol, eur_v3b, 5
    if public_symbol == "XAUUSD":
        return public_symbol, gold_v3b, 2
    raise ValueError("PARITY_SYMBOL_UNSUPPORTED")


def v3b_entry_parity_definition(symbol: str) -> dict:
    public_symbol, _module, _digits = _legacy_module(symbol)
    return {
        "schema_version": 1,
        "symbols": [public_symbol],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": ["CLOSE_BEYOND", "MIN_BODY_PERCENT"],
            "minimum_body_percent": 50.0,
            "minimum_distance_pips": None,
        },
        "confirmation": {
            "rules": ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
            "minimum_body_percent": None,
        },
        "entry": {"method": "CONFIRMATION_CLOSE"},
        # V3B's 50-point buffer and 100-point minimum are legacy-only details.
        # Keep this user-schema-compatible and add those rules only in the
        # read-only comparison adapter below.
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 1.90},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def _identity(symbol: str, side: str, event_time, broken_level: float) -> str:
    payload = {
        "symbol": symbol,
        "side": side,
        "event_time": _utc(event_time).isoformat(),
        "broken_level": round(float(broken_level), 10),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "v3bp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _record(symbol: str, digits: int, *, side: str, event_time, confirmation_time,
            entry: float, sl: float, tp2: float, broken_level: float) -> dict:
    return {
        "setup_identity": _identity(symbol, side, event_time, broken_level),
        "side": side,
        "event_time": _utc(event_time).isoformat(),
        "confirmation_time": _utc(confirmation_time).isoformat(),
        "entry": round(float(entry), digits),
        "sl": round(float(sl), digits),
        "tp2": round(float(tp2), digits),
    }


def _legacy_decisions(symbol: str, module, digits: int, frame: pd.DataFrame) -> list[dict]:
    start = _utc(frame.index[0])
    end = _utc(frame.index[-1]) + pd.Timedelta(minutes=5)
    decisions: list[dict] = []
    for event, timestamp, prefix5, side, leg, structure_ok, _meta in module.candidates(
        pd.DataFrame(), frame, start, end, {}
    ):
        if not structure_ok:
            continue
        trade, rejection, _trace = module.evaluate_event(
            event,
            timestamp,
            prefix5,
            frame,
            side,
            leg,
            {},
            end,
            previous_close=None,
        )
        if rejection or not trade:
            continue
        decisions.append(_record(
            symbol,
            digits,
            side=trade["side"],
            event_time=trade["event_timestamp"],
            confirmation_time=trade["m5_confirmation_timestamp"],
            entry=trade["entry"],
            sl=trade["sl"],
            tp2=trade["tp2"],
            broken_level=trade["broken_level"],
        ))
    return decisions


def _legacy_levels(module, side: str, entry: float, invalidation_price: float | None):
    if invalidation_price is None:
        return {"ok": False, "reason": "WAIT_NO_5M_STRUCTURAL_SL_SWING"}
    invalidation = {
        "type": "LOW" if side == "BUY" else "HIGH",
        "price": float(invalidation_price),
    }
    return module._fixed_levels(side, float(entry), invalidation)


def _studio_decisions(symbol: str, module, digits: int, frame: pd.DataFrame) -> list[dict]:
    definition = v3b_entry_parity_definition(symbol)
    timeline = build_market_facts({"5m": frame}, symbol, "5m", None)
    state = EvaluationState()
    current_event = None
    decisions: list[dict] = []

    for timestamp in timeline.timestamps():
        event = timeline.structure_event(timestamp)
        if event is not None:
            current_event = event

        evaluation = evaluate_strategy(
            definition,
            timeline,
            timestamp,
            state,
            symbol=symbol,
            # Risk budget is not part of entry parity. A stable positive balance
            # is sufficient to exercise the shared evaluator without account I/O.
            account_balance=10000.0,
        )
        state = evaluation.next_state
        if evaluation.signal not in {"BUY", "SELL"}:
            if state.pending_setup is None and evaluation.next_state.status == "BLOCKED":
                current_event = None
            continue
        if current_event is None or evaluation.entry is None:
            continue

        levels = _legacy_levels(
            module,
            evaluation.signal,
            float(evaluation.entry),
            current_event.invalidation_price,
        )
        # This applies V3B's legacy-only 50-point buffer + 100-point minimum as
        # comparison inputs. It does not mutate the Studio definition/schema.
        if not levels.get("ok"):
            current_event = None
            continue

        decisions.append(_record(
            symbol,
            digits,
            side=evaluation.signal,
            event_time=current_event.timestamp,
            confirmation_time=timestamp,
            entry=evaluation.entry,
            sl=levels["stop_loss"],
            tp2=levels["tp2"],
            broken_level=current_event.broken_level,
        ))
        current_event = None
    return decisions


def _same(left, right) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    return left == right


def _mismatches(legacy: list[dict], studio: list[dict]) -> list[dict]:
    legacy_by_id = {row["setup_identity"]: row for row in legacy}
    studio_by_id = {row["setup_identity"]: row for row in studio}
    mismatches: list[dict] = []
    for setup_id in sorted(set(legacy_by_id) | set(studio_by_id)):
        old = legacy_by_id.get(setup_id)
        new = studio_by_id.get(setup_id)
        timestamp = (old or new or {}).get("event_time")
        if old is None or new is None:
            mismatches.append({
                "timestamp": timestamp,
                "setup_identity": setup_id,
                "field": "presence",
                "legacy": old is not None,
                "studio": new is not None,
            })
            continue
        for field in PARITY_SCOPE:
            if not _same(old.get(field), new.get(field)):
                mismatches.append({
                    "timestamp": timestamp,
                    "setup_identity": setup_id,
                    "field": field,
                    "legacy": old.get(field),
                    "studio": new.get(field),
                })
    return mismatches


def compare_v3b_entry_decisions(symbol: str, frame_5m: pd.DataFrame, *, account_scope: str) -> dict:
    scope = str(account_scope or "").strip().upper()
    if not scope:
        raise ValueError("PARITY_ACCOUNT_SCOPE_REQUIRED")
    public_symbol, module, digits = _legacy_module(symbol)
    frame = _frame(frame_5m)
    legacy = _legacy_decisions(public_symbol, module, digits, frame)
    studio = _studio_decisions(public_symbol, module, digits, frame)
    mismatches = _mismatches(legacy, studio)
    compared = len(set(row["setup_identity"] for row in legacy) | set(row["setup_identity"] for row in studio))
    return {
        "match": not mismatches,
        "compared_setups": compared,
        "legacy": legacy,
        "studio": studio,
        "mismatches": mismatches,
        "parity_scope": list(PARITY_SCOPE),
        "post_entry_management_compared": False,
        "account_scope": scope,
        "legacy_only_level_policy": {
            "sl_buffer_points": int(module.SL_BUFFER_POINTS),
            "minimum_sl_points": int(module.MIN_SL_POINTS),
            "target_rr": float(module.TARGET_RR),
        },
    }
