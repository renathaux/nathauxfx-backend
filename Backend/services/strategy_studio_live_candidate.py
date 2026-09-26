"""Default-OFF Strategy Studio LIVE candidate builder.

This module evaluates the selected saved strategy for the pinned cTrader account
and may persist an ELIGIBLE StrategySetupLifecycle.  It never places or modifies
broker orders, and it never changes LIVE Auto state.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy.exc import IntegrityError

from db import SessionLocal
from models import StrategySetupLifecycle
from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.market_facts import build_market_facts
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_live_state import get_studio_live_state
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
from services.strategy_studio_schema import normalize_definition


def _wait(reason: str, *, account_scope=None, strategy_id=None, steps=None, next_state=None,
          setup_id=None) -> dict:
    return {
        "signal": "WAIT",
        "reason": reason,
        "studio_live_ready": False,
        "setup_id": setup_id,
        "strategy_id": strategy_id,
        "account_scope": account_scope,
        "evaluator_steps": steps or {},
        "next_state": next_state,
    }


def _clean_symbol(symbol) -> str:
    value = str(symbol or "").upper().replace("/", "").strip()
    if not value:
        raise ValueError("symbol is required")
    return value


def _account_scope(account_identity) -> str:
    if account_identity is None or not getattr(account_identity, "account_id", None):
        raise ValueError("account identity is required")
    scope = str(getattr(account_identity, "scope", "") or "")
    if not scope:
        raise ValueError("account scope is required")
    return scope


def _load_active_strategy(owner_id: str, factory):
    with factory() as session:
        selection = session.get(StrategyStudioSelection, owner_id)
        if selection is None:
            return None, None
        row = session.query(SavedStrategy).filter(
            SavedStrategy.strategy_id == selection.strategy_id,
            SavedStrategy.owner_id == owner_id,
        ).one_or_none()
        if row is None:
            return selection.strategy_id, None
        return row.strategy_id, normalize_definition(copy.deepcopy(row.definition_json))


def _bundle_scope_matches(market_bundle, account_scope: str) -> bool:
    for frame in (market_bundle or {}).values():
        attrs = getattr(frame, "attrs", None)
        if not isinstance(attrs, dict):
            continue
        source_scope = attrs.get("ctrader_stream_scope") or attrs.get("stream_scope")
        if source_scope and str(source_scope).upper() != str(account_scope).upper():
            return False
    return True


def _pending_identity(prior_state, timeline, timestamp):
    pending = None
    if prior_state is not None:
        pending = copy.deepcopy(getattr(prior_state, "pending_setup", None))
    if pending:
        return {
            "structure_event_time": pending.get("event_timestamp"),
            "broken_level": pending.get("broken_level"),
        }

    try:
        event = timeline.structure_event(timestamp)
    except Exception:
        event = None
    if event is None:
        return {"structure_event_time": None, "broken_level": None}
    return {
        "structure_event_time": pd.Timestamp(event.timestamp).isoformat(),
        "broken_level": float(event.broken_level),
    }


def _setup_id(*, owner_id, strategy_id, schema_version, account_scope, symbol,
              direction, structure_event_time, entry_trigger_time, broken_level,
              evaluator_setup_id, generation_bindings=None) -> str:
    identity = {
        "owner_id": str(owner_id),
        "strategy_id": str(strategy_id),
        "schema_version": int(schema_version),
        "account_scope": str(account_scope),
        "symbol": str(symbol),
        "direction": str(direction),
        "structure_event_time": structure_event_time,
        "entry_trigger_time": entry_trigger_time,
        "broken_level": broken_level,
        "evaluator_setup_id": evaluator_setup_id,
    }
    if generation_bindings:
        identity["stream_generations"] = sorted(generation_bindings, key=lambda b: (b["root_key"], b["timeframe"], b["generation"]))
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return "sts1_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _existing_reason(status: str) -> str:
    value = str(status or "").upper()
    return {
        "CONSUMED": "WAIT_STUDIO_SETUP_CONSUMED",
        "SUBMITTING": "WAIT_STUDIO_SETUP_SUBMITTING",
        "RECONCILIATION_REQUIRED": "WAIT_STUDIO_SETUP_RECONCILIATION_REQUIRED",
        "BLOCKED": "WAIT_STUDIO_SETUP_BLOCKED",
    }.get(value, "WAIT_STUDIO_SETUP_NOT_ELIGIBLE")


def _persist_eligible_setup(factory, *, setup_id, owner_id, strategy_id,
                            account_identity, account_scope, symbol, direction,
                            definition, generation_bindings=None, event_time=None, confirmation_time=None):
    now = datetime.now(timezone.utc)
    session = factory()
    try:
        from stream_generations import validate_studio_bindings
        from models import StrategySetupGeneration
        validate_studio_bindings(session, generation_bindings or [], event_time, confirmation_time)
        row = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.setup_id == setup_id
        ).with_for_update().one_or_none()
        if row is None:
            row = StrategySetupLifecycle(
                setup_id=setup_id,
                owner_id=str(owner_id),
                strategy_id=str(strategy_id),
                account_id=str(account_identity.account_id),
                account_scope=account_scope,
                symbol=symbol,
                direction=direction,
                status="ELIGIBLE",
                definition_snapshot=copy.deepcopy(definition),
                updated_at=now,
            )
            session.add(row)
            session.flush()
            for binding in generation_bindings or []:
                session.add(StrategySetupGeneration(setup_id=setup_id, root_key=binding['root_key'], timeframe=binding['timeframe'], generation=binding['generation'], event_time=pd.Timestamp(event_time).to_pydatetime(), confirmation_time=pd.Timestamp(confirmation_time).to_pydatetime()))
            session.commit()
            return "ELIGIBLE"

        expected = (
            str(owner_id), str(strategy_id), str(account_identity.account_id),
            account_scope, symbol, direction,
        )
        observed = (
            str(row.owner_id), str(row.strategy_id), str(row.account_id),
            str(row.account_scope), str(row.symbol), str(row.direction),
        )
        if observed != expected:
            session.rollback()
            raise RuntimeError("Strategy Studio setup identity collision across account scope")
        status = str(row.status).upper()
        session.rollback()
        return status
    except IntegrityError:
        session.rollback()
        row = session.get(StrategySetupLifecycle, setup_id)
        if row is None:
            raise
        expected = (
            str(owner_id), str(strategy_id), str(account_identity.account_id),
            account_scope, symbol, direction,
        )
        observed = (
            str(row.owner_id), str(row.strategy_id), str(row.account_id),
            str(row.account_scope), str(row.symbol), str(row.direction),
        )
        if observed != expected:
            raise RuntimeError("Strategy Studio setup identity collision across account scope")
        return str(row.status).upper()
    finally:
        session.close()


def _evaluate_latest(definition, timeline, timestamps, public_symbol, account_balance, prior_state):
    """Return the latest evaluation and the state immediately before it.

    A provided prior_state evaluates only the newest candle.  With no durable
    runtime state (including after a restart), replay the closed-candle timeline
    so an immediate-next-candle confirmation is reconstructed deterministically.
    """
    latest = pd.Timestamp(timestamps[-1])
    if isinstance(prior_state, EvaluationState):
        before = prior_state
        result = evaluate_strategy(
            definition,
            timeline,
            latest,
            before,
            symbol=public_symbol,
            account_balance=float(account_balance),
        )
        return latest, before, result

    state = EvaluationState()
    before_latest = state
    result = None
    for raw_timestamp in timestamps:
        stamp = pd.Timestamp(raw_timestamp)
        before = state
        result = evaluate_strategy(
            definition,
            timeline,
            stamp,
            state,
            symbol=public_symbol,
            account_balance=float(account_balance),
        )
        state = result.next_state
        if stamp == latest:
            before_latest = before
    return latest, before_latest, result


def build_studio_candidate(owner_id, account_identity, symbol, market_bundle,
                           *, account_balance, prior_state=None, session_factory=None) -> dict:
    owner = str(owner_id or "").strip()
    if not owner:
        raise ValueError("owner_id is required")
    public_symbol = _clean_symbol(symbol)
    scope = _account_scope(account_identity)
    factory = session_factory or SessionLocal

    live_state = get_studio_live_state(owner, factory)
    if not live_state.get("enabled"):
        return _wait("WAIT_STUDIO_LIVE_DISABLED", account_scope=scope)

    strategy_id, definition = _load_active_strategy(owner, factory)
    if strategy_id is None or definition is None:
        return _wait(
            "WAIT_STUDIO_NO_ACTIVE_STRATEGY",
            account_scope=scope,
            strategy_id=strategy_id,
        )

    enabled_strategy_id = live_state.get("enabled_strategy_id")
    if enabled_strategy_id and str(enabled_strategy_id) != str(strategy_id):
        return _wait(
            "WAIT_STUDIO_LIVE_STRATEGY_MISMATCH",
            account_scope=scope,
            strategy_id=strategy_id,
        )

    if public_symbol not in definition["symbols"]:
        return _wait(
            "WAIT_STUDIO_SYMBOL_DISABLED",
            account_scope=scope,
            strategy_id=strategy_id,
        )

    if not _bundle_scope_matches(market_bundle, scope):
        return _wait(
            "WAIT_STUDIO_ACCOUNT_SCOPE_MISMATCH",
            account_scope=scope,
            strategy_id=strategy_id,
        )

    from stream_generations import studio_bundle, GenerationBlocked
    try:
        with factory() as generation_session:
            market_bundle, generation_bindings = studio_bundle(generation_session, scope, public_symbol, market_bundle)
        if generation_bindings:
            prior_state = None  # rebuild deterministically from canonical full history on restart/cutover
    except GenerationBlocked as exc:
        return _wait("WAIT_STUDIO_GENERATION: " + str(exc), account_scope=scope)

    timeline = build_market_facts(
        market_bundle,
        public_symbol,
        definition["trading_timeframe"],
        definition["trend"]["timeframe"],
        definition.get("structure_timeframe", definition["trading_timeframe"]),
    )
    timestamps = list(timeline.timestamps())
    if not timestamps:
        return _wait(
            "WAIT_STUDIO_HISTORY_UNAVAILABLE",
            account_scope=scope,
            strategy_id=strategy_id,
        )

    timestamp, state_before_latest, result = _evaluate_latest(
        definition,
        timeline,
        timestamps,
        public_symbol,
        account_balance,
        prior_state,
    )

    if result.signal not in {"BUY", "SELL"}:
        return _wait(
            "WAIT_STUDIO_EVALUATOR",
            account_scope=scope,
            strategy_id=strategy_id,
            steps=result.steps,
            next_state=result.next_state,
            setup_id=result.setup_id,
        )

    setup_facts = _pending_identity(state_before_latest, timeline, timestamp)
    if generation_bindings and (not setup_facts['structure_event_time'] or any(pd.Timestamp(setup_facts['structure_event_time']) <= pd.Timestamp(b['activation_watermark']) or timestamp <= pd.Timestamp(b['activation_watermark']) for b in generation_bindings)):
        return _wait("WAIT_STUDIO_GENERATION_HISTORICAL", account_scope=scope)
    stable_setup_id = _setup_id(
        owner_id=owner,
        strategy_id=strategy_id,
        schema_version=definition["schema_version"],
        account_scope=scope,
        symbol=public_symbol,
        direction=result.signal,
        structure_event_time=setup_facts["structure_event_time"],
        entry_trigger_time=timestamp.isoformat(),
        broken_level=setup_facts["broken_level"],
        evaluator_setup_id=result.setup_id,
        generation_bindings=generation_bindings,
    )
    try:
        status = _persist_eligible_setup(
            factory,
            setup_id=stable_setup_id,
            owner_id=owner,
            strategy_id=strategy_id,
            account_identity=account_identity,
            account_scope=scope,
            symbol=public_symbol,
            direction=result.signal,
            definition=definition,
            generation_bindings=generation_bindings,
            event_time=setup_facts["structure_event_time"],
            confirmation_time=timestamp,
        )
    except GenerationBlocked:
        return _wait("WAIT_STUDIO_GENERATION_CHANGED", account_scope=scope)
    if status != "ELIGIBLE":
        return _wait(
            _existing_reason(status),
            account_scope=scope,
            strategy_id=strategy_id,
            steps=result.steps,
            next_state=result.next_state,
            setup_id=stable_setup_id,
        )

    return {
        "signal": result.signal,
        "reason": "STUDIO_CANDIDATE_READY",
        "studio_live_ready": True,
        "setup_id": stable_setup_id,
        "strategy_id": strategy_id,
        "account_id": str(account_identity.account_id),
        "account_scope": scope,
        "symbol": public_symbol,
        "entry": result.entry,
        "sl": result.sl,
        "tp1": result.tp1,
        "tp2": result.tp2,
        "risk_budget": result.risk_budget,
        "tp1_definition": copy.deepcopy(definition.get("tp1") or {}),
        "fundamental_policy": str(
            ((definition.get("fundamentals") or {}).get("mode") or "BLOCK_OPPOSITE")
        ).upper(),
        "evaluator_steps": result.steps,
        "next_state": result.next_state,
    }
