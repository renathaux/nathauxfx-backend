from __future__ import annotations

from pathlib import Path


API = Path("Backend/api.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    start_pos = text.find(start)
    if start_pos < 0:
        raise RuntimeError(f"{label}: start anchor not found")
    end_pos = text.find(end, start_pos)
    if end_pos < 0:
        raise RuntimeError(f"{label}: end anchor not found")
    return text[:start_pos] + replacement + text[end_pos:]


def main():
    text = API.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "from services.trade_submission_service import (\n    claim_submission,\n    complete_submission,\n    mark_request_started,\n    recover_unsent_claim,\n    require_reconciliation,\n)\n",
        "from services.trade_submission_service import (\n    claim_submission,\n    claim_strategy_submission,\n    complete_submission,\n    mark_request_started,\n    recover_unsent_claim,\n    require_reconciliation,\n)\nfrom services.strategy_studio_live_state import get_enabled_studio_live_owner\nfrom services.strategy_studio_live_candidate import build_studio_candidate\nfrom services.strategy_studio_execution_adapter import (\n    normalize_studio_trade_levels,\n    studio_risk_reward_details,\n)\n",
        "studio imports",
    )

    text = replace_once(
        text,
        "@account_operation\ndef calculate_live_risk_size(symbol, entry, sl):",
        "@account_operation\ndef calculate_live_risk_size(symbol, entry, sl, risk_percent_override=None):",
        "risk override signature",
    )
    text = replace_once(
        text,
        "    balance = account_verification.get(\"balance\")\n    account_value = account_verification.get(\"account_equity_used\")\n",
        "    balance = account_verification.get(\"balance\")\n    if risk_percent_override is None:\n        account_value = account_verification.get(\"account_equity_used\")\n    else:\n        try:\n            account_value = float(balance)\n        except (TypeError, ValueError):\n            account_value = 0\n        if account_value <= 0:\n            return {\n                \"ok\": False,\n                \"reason\": \"Strategy Studio requires a verified positive account balance\",\n            }\n",
        "studio uses balance",
    )
    text = replace_once(
        text,
        "    configured_risk_percent = get_configured_live_risk_percent()\n    position_size = calculate_position_size(\n        execution_symbol,\n        account_value,\n        configured_risk_percent,\n        sl_pips\n    )\n",
        "    if risk_percent_override is None:\n        configured_risk_percent = get_configured_live_risk_percent()\n    else:\n        try:\n            configured_risk_percent = float(risk_percent_override)\n        except (TypeError, ValueError):\n            configured_risk_percent = 0\n        if not math.isfinite(configured_risk_percent) or configured_risk_percent <= 0:\n            return {\n                \"ok\": False,\n                \"reason\": \"Strategy Studio risk percent is invalid\",\n            }\n    position_size = calculate_position_size(\n        execution_symbol,\n        account_value,\n        configured_risk_percent,\n        sl_pips\n    )\n",
        "risk override value",
    )

    panel_anchor = '''def get_panel_trade_plan(panel_data, symbol):
    if not isinstance(panel_data, dict):
        return None

    execution_symbol = normalize_symbol(symbol)

    return panel_data.get(execution_symbol)
'''
    panel_replacement = panel_anchor + '''

def load_strategy_studio_market_bundle(symbol, account_scope):
    from services.strategy_simulator_data_source import load_market_bundle

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=35)
    return load_market_bundle(
        normalize_symbol(symbol),
        start,
        end,
        stream_scope=str(account_scope),
    )


def _studio_wait_execution_plan(symbol, reason, *, owner_id=None, candidate=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    return {
        "symbol": normalize_symbol(symbol),
        "signal": "WAIT",
        "final_signal": "WAIT",
        "strategy_setup_complete": False,
        "strategy_setup_type": "STRATEGY_STUDIO",
        "plan_type": "STRATEGY_STUDIO_LIVE",
        "execution_source": "STRATEGY_STUDIO",
        "studio_owner_id": owner_id,
        "studio_strategy_id": candidate.get("strategy_id"),
        "studio_setup_id": candidate.get("setup_id"),
        "studio_account_scope": candidate.get("account_scope"),
        "signal_setup_id": candidate.get("setup_id"),
        "studio_live_ready": False,
        "blocked_by": "strategy_studio_live_handoff",
        "blocked_reason": str(reason or "WAIT_STUDIO_EVALUATOR"),
        "plan_reason": str(reason or "WAIT_STUDIO_EVALUATOR"),
        "evaluator_steps": copy.deepcopy(candidate.get("evaluator_steps") or {}),
    }


def studio_candidate_execution_plan(candidate, *, account_balance, owner_id):
    candidate = candidate if isinstance(candidate, dict) else {}
    signal = str(candidate.get("signal") or "WAIT").upper()
    if signal not in {"BUY", "SELL"} or not candidate.get("studio_live_ready"):
        return _studio_wait_execution_plan(
            candidate.get("symbol"),
            candidate.get("reason") or "WAIT_STUDIO_EVALUATOR",
            owner_id=owner_id,
            candidate=candidate,
        )

    risk = candidate.get("risk_budget") or {}
    method = str(risk.get("method") or "").upper()
    try:
        risk_value = float(risk.get("value"))
        balance = float(account_balance)
    except (TypeError, ValueError):
        raise ValueError("STRATEGY_STUDIO_RISK_BUDGET_INVALID")
    if not math.isfinite(risk_value) or risk_value <= 0 or not math.isfinite(balance) or balance <= 0:
        raise ValueError("STRATEGY_STUDIO_RISK_BUDGET_INVALID")

    if method == "PERCENT_BALANCE":
        requested_risk_percent = risk_value
    elif method == "FIXED_DOLLARS":
        try:
            fixed_dollars = float(risk.get("dollars", risk_value))
        except (TypeError, ValueError):
            fixed_dollars = 0
        if not math.isfinite(fixed_dollars) or fixed_dollars <= 0:
            raise ValueError("STRATEGY_STUDIO_RISK_BUDGET_INVALID")
        requested_risk_percent = fixed_dollars / balance * 100.0
    else:
        raise ValueError("STRATEGY_STUDIO_RISK_METHOD_UNSUPPORTED")

    tp1_definition = copy.deepcopy(candidate.get("tp1_definition") or {})
    tp1_enabled = bool(tp1_definition.get("enabled"))
    return {
        "symbol": normalize_symbol(candidate.get("symbol")),
        "signal": signal,
        "final_signal": signal,
        "entry_price": candidate.get("entry"),
        "stop_loss": candidate.get("sl"),
        "tp1": candidate.get("tp1") if tp1_enabled else None,
        "tp2": candidate.get("tp2"),
        "strategy_setup_complete": True,
        "fresh_entry_available": True,
        "strategy_setup_type": "STRATEGY_STUDIO",
        "plan_type": "STRATEGY_STUDIO_LIVE",
        "entry_timing": "READY",
        "execution_source": "STRATEGY_STUDIO",
        "studio_owner_id": str(owner_id),
        "studio_strategy_id": candidate.get("strategy_id"),
        "studio_setup_id": candidate.get("setup_id"),
        "studio_account_scope": candidate.get("account_scope"),
        "signal_setup_id": candidate.get("setup_id"),
        "studio_risk_method": method,
        "studio_risk_value": risk_value,
        "requested_risk_percent": requested_risk_percent,
        "studio_tp1_enabled": tp1_enabled,
        "tp1_definition": tp1_definition,
        "studio_structure_event_time": candidate.get("structure_event_time"),
        "studio_entry_trigger_time": candidate.get("entry_trigger_time"),
        "studio_broken_level": candidate.get("broken_level"),
        "evaluator_steps": copy.deepcopy(candidate.get("evaluator_steps") or {}),
        "studio_live_ready": True,
        "plan_reason": candidate.get("reason") or "STUDIO_CANDIDATE_READY",
    }


def select_auto_execution_candidate(panel_data, symbol):
    v3b_plan = get_panel_trade_plan(panel_data, symbol) or {}
    try:
        owner_id = get_enabled_studio_live_owner()
    except Exception as exc:
        return {
            "source": "STRATEGY_STUDIO",
            "plan": _studio_wait_execution_plan(
                symbol,
                f"WAIT_STUDIO_OWNER_STATE: {exc}",
            ),
        }
    if not owner_id:
        return {"source": "V3B", "plan": v3b_plan}

    try:
        from ctrader_account_context import selected_identity

        identity = current_identity() or selected_identity()
        if identity is None:
            return {
                "source": "STRATEGY_STUDIO",
                "plan": _studio_wait_execution_plan(
                    symbol,
                    "WAIT_STUDIO_ACCOUNT_NOT_SELECTED",
                    owner_id=owner_id,
                ),
            }
        snapshot = get_ctrader_account_snapshot()
        verified = validate_verified_account_snapshot(snapshot)
        if not verified.get("ok"):
            return {
                "source": "STRATEGY_STUDIO",
                "plan": _studio_wait_execution_plan(
                    symbol,
                    verified.get("reason") or "WAIT_STUDIO_ACCOUNT_BALANCE_UNVERIFIED",
                    owner_id=owner_id,
                ),
            }
        try:
            balance = float(verified.get("balance"))
        except (TypeError, ValueError):
            balance = 0
        if balance <= 0:
            return {
                "source": "STRATEGY_STUDIO",
                "plan": _studio_wait_execution_plan(
                    symbol,
                    "WAIT_STUDIO_ACCOUNT_BALANCE_UNVERIFIED",
                    owner_id=owner_id,
                ),
            }
        bundle = load_strategy_studio_market_bundle(symbol, identity.scope)
        assert_current_selection(identity)
        candidate = build_studio_candidate(
            owner_id,
            identity,
            symbol,
            bundle,
            account_balance=balance,
            prior_state=None,
        )
        assert_current_selection(identity)
        if candidate.get("signal") not in {"BUY", "SELL"} or not candidate.get("studio_live_ready"):
            return {
                "source": "STRATEGY_STUDIO",
                "plan": _studio_wait_execution_plan(
                    symbol,
                    candidate.get("reason") or "WAIT_STUDIO_EVALUATOR",
                    owner_id=owner_id,
                    candidate=candidate,
                ),
            }
        return {
            "source": "STRATEGY_STUDIO",
            "plan": studio_candidate_execution_plan(
                candidate,
                account_balance=balance,
                owner_id=owner_id,
            ),
        }
    except AccountSelectionChanged:
        return {
            "source": "STRATEGY_STUDIO",
            "plan": _studio_wait_execution_plan(
                symbol,
                "WAIT_STUDIO_ACCOUNT_SELECTION_CHANGED",
                owner_id=owner_id,
            ),
        }
    except Exception as exc:
        return {
            "source": "STRATEGY_STUDIO",
            "plan": _studio_wait_execution_plan(
                symbol,
                f"WAIT_STUDIO_CANDIDATE_ERROR: {exc}",
                owner_id=owner_id,
            ),
        }
'''
    text = replace_once(text, panel_anchor, panel_replacement, "candidate source helpers")

    text = replace_once(
        text,
        '        "signal_setup_id": get_signal_setup_id(plan, side),\n',
        '        "signal_setup_id": plan.get("signal_setup_id") or get_signal_setup_id(plan, side),\n',
        "preserve Studio setup id",
    )

    freshness_anchor = '''def normal_plan_is_fresh_after_news(plan, decision):
    fresh_after = decision.get("normal_fresh_after")
    if not fresh_after:
        return True
'''
    freshness_replacement = '''def normal_plan_is_fresh_after_news(plan, decision):
    fresh_after = decision.get("normal_fresh_after")
    if not fresh_after:
        return True
    if str((plan or {}).get("execution_source") or "").upper() == "STRATEGY_STUDIO":
        try:
            watermark = news_trading.parse_time(fresh_after)
            structure_time = news_trading.parse_time(plan.get("studio_structure_event_time"))
            entry_time = news_trading.parse_time(plan.get("studio_entry_trigger_time"))
        except Exception:
            return False
        return bool(
            watermark
            and structure_time
            and entry_time
            and structure_time > watermark
            and entry_time > structure_time
        )
'''
    text = replace_once(text, freshness_anchor, freshness_replacement, "Studio news freshness")

    trade_levels_function = '''def trade_payload_has_required_levels(trade_payload):
    if not isinstance(trade_payload, dict):
        return False, "Trade payload missing"

    side = str(
        trade_payload.get("action")
        or trade_payload.get("side")
        or trade_payload.get("signal")
        or ""
    ).upper()
    studio_execution = str(trade_payload.get("execution_source") or "").upper() == "STRATEGY_STUDIO"
    tp1_enabled = bool(trade_payload.get("studio_tp1_enabled", True)) if studio_execution else True

    required_values = [
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp2"),
    ]
    if tp1_enabled:
        required_values.append(trade_payload.get("tp1"))

    if any(is_missing_trade_value(value) for value in required_values):
        return False, (
            "Entry, SL, and TP2 are required before execution"
            if studio_execution and not tp1_enabled
            else "Entry, SL, TP1, and TP2 are required before execution"
        )

    if studio_execution:
        normalized = normalize_studio_trade_levels(
            trade_payload.get("symbol"),
            side,
            trade_payload.get("entry"),
            trade_payload.get("sl"),
            trade_payload.get("tp1"),
            trade_payload.get("tp2"),
            tp1_enabled=tp1_enabled,
        )
        rr_validation = studio_risk_reward_details(
            trade_payload.get("symbol"),
            side,
            normalized.get("entry"),
            normalized.get("sl"),
            normalized.get("tp2"),
        ) if normalized.get("ok") else {"ok": False, "reason": normalized.get("reason")}
    else:
        normalized = normalize_trade_levels(
            trade_payload.get("symbol"),
            side,
            trade_payload.get("entry"),
            trade_payload.get("sl"),
            trade_payload.get("tp1"),
            trade_payload.get("tp2"),
        )
        rr_validation = validate_live_trade_risk_reward(
            trade_payload.get("symbol"),
            side,
            normalized.get("entry"),
            normalized.get("sl"),
            normalized.get("tp2"),
        ) if normalized.get("ok") else {"ok": False, "reason": normalized.get("reason")}

    if not normalized.get("ok"):
        return False, "LIVE BLOCKED: invalid SL/TP distance."
    if not rr_validation.get("ok"):
        return False, rr_validation.get("reason")
    return True, None

'''
    text = replace_between(
        text,
        "def trade_payload_has_required_levels(trade_payload):\n",
        "def log_paper_live_signal_compare(\n",
        trade_levels_function,
        "trade level validator",
    )

    prepare_function = '''def prepare_ctrader_trade(payload, volume=0.01):
    sync_ctrader_account_state()

    raw_symbol = str(payload.get("symbol", "")).upper()
    symbol = normalize_symbol(raw_symbol)
    action = str(payload.get("action") or payload.get("side") or "").upper()
    plan = get_signal_trade_plan(symbol) or {}
    payload_signal = str(payload.get("signal") or "").upper()
    plan_signal = str(plan.get("signal") or "").upper()
    if payload_signal in ["BUY", "SELL"]:
        signal = payload_signal
    else:
        signal = str(plan_signal or payload_signal or "WAIT").upper()
    value_plan = {} if payload_signal in ["BUY", "SELL"] else plan
    studio_execution = str(payload.get("execution_source") or "").upper() == "STRATEGY_STUDIO"

    entry, _ = choose_backend_trade_value(value_plan, payload, "entry_price", "entry", "entry_price")
    sl, _ = choose_backend_trade_value(value_plan, payload, "stop_loss", "sl", "stop_loss")
    tp1, _ = choose_backend_trade_value(value_plan, payload, "tp1", "tp1")
    tp2, _ = choose_backend_trade_value(value_plan, payload, "tp2", "tp2")

    if not studio_execution:
        try:
            decimals = 2 if symbol == "XAUUSD" else 5
            tp1 = round(calculate_tp1_from_tp2(entry, tp2, action), decimals)
        except (TypeError, ValueError):
            pass

    log_frontend_trade_level_mismatch(symbol, action, "entry", plan.get("entry_price"), payload.get("entry", payload.get("entry_price")))
    log_frontend_trade_level_mismatch(symbol, action, "sl", plan.get("stop_loss"), payload.get("sl", payload.get("stop_loss")))
    log_frontend_trade_level_mismatch(symbol, action, "tp1", plan.get("tp1"), payload.get("tp1"))
    log_frontend_trade_level_mismatch(symbol, action, "tp2", plan.get("tp2"), payload.get("tp2"))

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

    if studio_execution:
        normalized = normalize_studio_trade_levels(
            symbol,
            action,
            entry,
            sl,
            tp1,
            tp2,
            tp1_enabled=bool(payload.get("studio_tp1_enabled", tp1 is not None)),
        )
    else:
        normalized = normalize_trade_levels(symbol, action, entry, sl, tp1, tp2)

    normalized["mode"] = LIVE_ACCOUNT_STATE.get("mode", "demo")
    normalized["signal"] = signal
    execution_metadata = {} if studio_execution else get_plan_execution_metadata(plan, action)
    metadata_keys = [
        "signal_setup_id", "fifteen_m_break_time", "fifteen_m_break_close_time",
        "five_m_confirmation_close_time", "trend_15m", "setup_identity",
        "source_indicator_event_id", "indicator_event_identity", "m5_confirmation_id",
        "m5_confirmation_identity", "news_event_id", "news_event", "news_confirmation",
        "execution_source", "studio_owner_id", "studio_strategy_id", "studio_setup_id",
        "studio_account_scope", "studio_risk_method", "studio_risk_value",
        "requested_risk_percent", "studio_tp1_enabled", "studio_structure_event_time",
        "studio_entry_trigger_time", "studio_broken_level", "tp1_definition",
    ]
    for key in metadata_keys:
        payload_value = payload.get(key)
        normalized[key] = (
            copy.deepcopy(payload_value)
            if payload_value not in [None, "", {}]
            else copy.deepcopy(execution_metadata.get(key))
        )

    if not normalized.get("ok"):
        normalized["message"] = normalized.get("reason")
        log_rejected_ctrader_trade(symbol, action, entry, sl, tp1, tp2, normalized.get("reason"))
        return normalized

    rr_validation = (
        studio_risk_reward_details(
            symbol,
            action,
            normalized.get("entry"),
            normalized.get("sl"),
            normalized.get("tp2"),
        )
        if studio_execution
        else validate_live_trade_risk_reward(
            symbol,
            action,
            normalized.get("entry"),
            normalized.get("sl"),
            normalized.get("tp2"),
        )
    )
    if not rr_validation.get("ok"):
        rejected = reject_ctrader_order(
            symbol, action, normalized.get("entry"), normalized.get("sl"),
            normalized.get("tp1"), normalized.get("tp2"), rr_validation.get("reason"),
        )
        rejected["details"] = rr_validation
        rejected["risk_reward_ratio"] = rr_validation.get("risk_reward_ratio")
        return rejected

    normalized["volume"] = None
    return normalized

'''
    text = replace_between(
        text,
        "def prepare_ctrader_trade(payload, volume=0.01):\n",
        "def log_structure_tp_trade_audit(",
        prepare_function,
        "prepare trade",
    )

    text = replace_once(
        text,
        '    for symbol in ["EURUSD", "XAUUSD"]:\n        plan = get_panel_trade_plan(panel_data, symbol) or {}\n        initial_plan = plan\n',
        '    for symbol in ["EURUSD", "XAUUSD"]:\n        execution_selection = select_auto_execution_candidate(panel_data, symbol)\n        plan = execution_selection.get("plan") or {}\n        initial_plan = plan\n',
        "auto candidate source switch",
    )
    text = replace_once(
        text,
        '                "execution_source": "V1",\n',
        '                "execution_source": plan.get("execution_source") or "V1",\n',
        "preserve Studio source in active-position block",
    )
    text = replace_once(
        text,
        '''            consumed_setup = consume_signal_setup_for_active_trade(
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
''',
        '''            consumed_setup = False
            if str(plan.get("execution_source") or "").upper() != "STRATEGY_STUDIO":
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
''',
        "do not consume V3B state for Studio",
    )

    auto_payload_anchor = '''                **get_plan_execution_metadata(plan, signal),
                "news_event_id": plan.get("news_event_id"),
'''
    auto_payload_replacement = '''                **get_plan_execution_metadata(plan, signal),
                "execution_source": plan.get("execution_source"),
                "studio_owner_id": plan.get("studio_owner_id"),
                "studio_strategy_id": plan.get("studio_strategy_id"),
                "studio_setup_id": plan.get("studio_setup_id"),
                "studio_account_scope": plan.get("studio_account_scope"),
                "studio_risk_method": plan.get("studio_risk_method"),
                "studio_risk_value": plan.get("studio_risk_value"),
                "requested_risk_percent": plan.get("requested_risk_percent"),
                "studio_tp1_enabled": plan.get("studio_tp1_enabled"),
                "studio_structure_event_time": plan.get("studio_structure_event_time"),
                "studio_entry_trigger_time": plan.get("studio_entry_trigger_time"),
                "studio_broken_level": plan.get("studio_broken_level"),
                "tp1_definition": copy.deepcopy(plan.get("tp1_definition") or {}),
                "news_event_id": plan.get("news_event_id"),
'''
    text = replace_once(text, auto_payload_anchor, auto_payload_replacement, "Studio auto payload metadata")

    claim_helper = '''def claim_execution_submission(trade_payload):
    """Claim V3B or Strategy Studio setup through the shared durable protocol."""
    payload = trade_payload if isinstance(trade_payload, dict) else {}
    studio_execution = str(payload.get("execution_source") or "").upper() == "STRATEGY_STUDIO"
    symbol = normalize_symbol(payload.get("symbol"))
    side = str(payload.get("action") or payload.get("side") or payload.get("signal") or "").upper()
    account_id = str(get_active_ctrader_account_id() or "")

    if studio_execution:
        identity = current_identity()
        expected_scope = str(payload.get("studio_account_scope") or "")
        if identity is None or not expected_scope or identity.scope.upper() != expected_scope.upper():
            return {"ok": False, "reason": "Strategy Studio account scope changed before submission"}
        if str(identity.account_id) != account_id:
            return {"ok": False, "reason": "Strategy Studio account changed before submission"}
        owner_id = str(payload.get("studio_owner_id") or "").strip()
        strategy_id = str(payload.get("studio_strategy_id") or "").strip()
        setup_id = str(payload.get("studio_setup_id") or payload.get("signal_setup_id") or "").strip()
        if not owner_id or not strategy_id or not setup_id:
            return {"ok": False, "reason": "Strategy Studio submission identity is incomplete"}
        return claim_strategy_submission(
            setup_id,
            account_id,
            symbol,
            side,
            payload,
            owner_id=owner_id,
            strategy_id=strategy_id,
        )

    source_event_id = payload.get("source_indicator_event_id")
    if not source_event_id:
        return {"ok": True, "claimed": False, "idempotency_key": None}
    if not update_event_lifecycle(
        source_event_id,
        "LIVE",
        "ELIGIBLE",
        m5_confirmation_id=payload.get("m5_confirmation_id"),
        m5_confirmation_identity=payload.get("m5_confirmation_identity"),
        signal_setup_id=payload.get("signal_setup_id"),
        owner_id="OWNER",
        account_id=account_id,
    ):
        return {"ok": False, "reason": "account-scoped indicator lifecycle is not eligible"}
    return claim_submission(
        source_event_id,
        "LIVE",
        account_id,
        symbol,
        payload.get("signal_setup_id"),
        payload,
    )


'''
    text = replace_once(
        text,
        "def _execute_live_order_core_impl(payload: dict, source=\"manual\", _inflight_guard=None):\n",
        claim_helper + "def _execute_live_order_core_impl(payload: dict, source=\"manual\", _inflight_guard=None):\n",
        "submission claim helper",
    )

    core_start = '''    symbol = trade_payload.get("symbol")
    side = trade_payload.get("action")
    plan = get_signal_trade_plan(symbol) or {}
    log_live_xauusd_execution_debug(
'''
    core_replacement = '''    symbol = trade_payload.get("symbol")
    side = trade_payload.get("action")
    studio_execution = str(trade_payload.get("execution_source") or "").upper() == "STRATEGY_STUDIO"
    plan = get_signal_trade_plan(symbol) or {}
    if studio_execution:
        plan = {
            "symbol": symbol,
            "signal": trade_payload.get("signal"),
            "entry_price": trade_payload.get("entry"),
            "stop_loss": trade_payload.get("sl"),
            "tp1": trade_payload.get("tp1"),
            "tp2": trade_payload.get("tp2"),
            "signal_setup_id": trade_payload.get("signal_setup_id"),
            "execution_source": "STRATEGY_STUDIO",
            "studio_owner_id": trade_payload.get("studio_owner_id"),
            "studio_strategy_id": trade_payload.get("studio_strategy_id"),
            "studio_setup_id": trade_payload.get("studio_setup_id"),
            "studio_account_scope": trade_payload.get("studio_account_scope"),
        }
    studio_risk_percent = trade_payload.get("requested_risk_percent") if studio_execution else None
    def risk_size_fn(calc_symbol, calc_entry, calc_sl):
        return calculate_live_risk_size(
            calc_symbol,
            calc_entry,
            calc_sl,
            risk_percent_override=studio_risk_percent,
        )
    rr_validator = studio_risk_reward_details if studio_execution else validate_live_trade_risk_reward
    log_live_xauusd_execution_debug(
'''
    text = replace_once(text, core_start, core_replacement, "core Studio context")

    initial_risk = '''    risk_size = calculate_live_risk_size(
        symbol,
        trade_payload.get("entry"),
        trade_payload.get("sl")
    )
'''
    text = replace_once(
        text,
        initial_risk,
        '''    risk_size = risk_size_fn(
        symbol,
        trade_payload.get("entry"),
        trade_payload.get("sl")
    )
''',
        "initial Studio risk sizing",
    )
    text = replace_once(
        text,
        '''    rr_validation = validate_live_trade_risk_reward(
        symbol,
        side,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp2"),
    )
''',
        '''    rr_validation = rr_validator(
        symbol,
        side,
        trade_payload.get("entry"),
        trade_payload.get("sl"),
        trade_payload.get("tp2"),
    )
''',
        "initial Studio RR validation",
    )

    text = replace_once(
        text,
        '        if source == "auto" and not is_news_order:\n',
        '        if source == "auto" and not is_news_order and not studio_execution:\n',
        "skip V3B final gate for Studio",
    )
    text = replace_once(
        text,
        '        if strategy_generated_order and not is_news_order:\n',
        '        if strategy_generated_order and not is_news_order and not studio_execution:\n',
        "skip V3B EMA gate for Studio",
    )

    studio_locked_gate = '''        if studio_execution and not is_news_order:
            identity = current_identity()
            expected_scope = str(trade_payload.get("studio_account_scope") or "")
            if identity is None or not expected_scope or identity.scope.upper() != expected_scope.upper():
                return reject_live_execution_block(
                    symbol, side, trade_payload,
                    "WAIT_STUDIO_ACCOUNT_SELECTION_CHANGED",
                    "WAIT_STUDIO_ACCOUNT_SELECTION_CHANGED",
                )
            try:
                assert_current_selection(identity)
            except AccountSelectionChanged:
                return reject_live_execution_block(
                    symbol, side, trade_payload,
                    "WAIT_STUDIO_ACCOUNT_SELECTION_CHANGED",
                    "WAIT_STUDIO_ACCOUNT_SELECTION_CHANGED",
                )
            market_health = check_live_market_data_health(symbol)
            if not market_health.get("ok"):
                return reject_live_execution_block(
                    symbol, side, trade_payload,
                    "WAIT_STALE_MARKET_FEED",
                    "WAIT_STALE_MARKET_FEED",
                    details=market_health,
                )
            locked_risk_size = risk_size_fn(
                symbol,
                trade_payload.get("entry"),
                trade_payload.get("sl"),
            )
            locked_rr = rr_validator(
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
                reason = "WAIT_RISK_CHANGED_BEFORE_EXECUTION" if risk_changed else "WAIT_INVALID_RR"
                return reject_live_execution_block(
                    symbol, side, trade_payload, reason, reason,
                    details={
                        "initial_risk_size": risk_size,
                        "locked_risk_size": locked_risk_size,
                        "locked_risk_reward": locked_rr,
                    },
                )

'''
    text = replace_once(
        text,
        '        LIVE_ORDER_IN_FLIGHT.add(symbol)\n',
        studio_locked_gate + '        LIVE_ORDER_IN_FLIGHT.add(symbol)\n',
        "Studio final account/risk gate",
    )

    text = replace_once(
        text,
        '''        calculate_live_risk_size,
        validate_live_trade_risk_reward,
    )
''',
        '''        risk_size_fn,
        rr_validator,
    )
''',
        "pre-submit Studio risk callbacks",
    )

    old_submission = '''    submission_key = None
    submission_claim = None
    if trade_payload.get("source_indicator_event_id"):
        submission_account_id = (
            get_active_ctrader_account_id()
        )
        if not update_event_lifecycle(
            trade_payload.get("source_indicator_event_id"),
            "LIVE",
            "ELIGIBLE",
            m5_confirmation_id=trade_payload.get("m5_confirmation_id"),
            m5_confirmation_identity=trade_payload.get("m5_confirmation_identity"),
            signal_setup_id=trade_payload.get("signal_setup_id"),
            owner_id="OWNER",
            account_id=submission_account_id,
        ):
            return reject_live_execution_block(
                symbol, side, trade_payload,
                "account-scoped indicator lifecycle is not eligible",
                "LIVE EXECUTION BLOCKED: account-scoped lifecycle unavailable",
            )
        submission_claim = claim_submission(
            trade_payload.get("source_indicator_event_id"),
            "LIVE",
            submission_account_id,
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
'''
    new_submission = '''    submission_key = None
    submission_claim = claim_execution_submission(trade_payload)
    if not submission_claim.get("ok"):
        return reject_live_execution_block(
            symbol, side, trade_payload,
            submission_claim.get("reason") or "durable submission claim failed",
            "LIVE EXECUTION BLOCKED: durable submission claim failed",
            details=submission_claim,
        )
    submission_key = submission_claim.get("idempotency_key")
    if submission_key:
        trade_payload["submission_idempotency_key"] = submission_key
'''
    text = replace_once(text, old_submission, new_submission, "shared submission claim")

    text = replace_once(
        text,
        '''        filled_risk = calculate_live_risk_size(
            symbol, actual_fill, trade_payload.get("sl")
        )
        filled_rr = validate_live_trade_risk_reward(
''',
        '''        filled_risk = risk_size_fn(
            symbol, actual_fill, trade_payload.get("sl")
        )
        filled_rr = rr_validator(
''',
        "post-fill Studio risk",
    )

    success_lifecycle = '''    update_event_lifecycle(
        trade_payload.get("source_indicator_event_id"),
        "LIVE",
        "CONSUMED",
        m5_confirmation_id=trade_payload.get("m5_confirmation_id"),
        m5_confirmation_identity=trade_payload.get("m5_confirmation_identity"),
        signal_setup_id=trade_payload.get("signal_setup_id"),
        owner_id="OWNER",
        account_id=(
            get_active_ctrader_account_id()
        ),
    )
'''
    success_lifecycle_new = '''    if trade_payload.get("source_indicator_event_id"):
        update_event_lifecycle(
            trade_payload.get("source_indicator_event_id"),
            "LIVE",
            "CONSUMED",
            m5_confirmation_id=trade_payload.get("m5_confirmation_id"),
            m5_confirmation_identity=trade_payload.get("m5_confirmation_identity"),
            signal_setup_id=trade_payload.get("signal_setup_id"),
            owner_id="OWNER",
            account_id=(
                get_active_ctrader_account_id()
            ),
        )
'''
    text = replace_once(text, success_lifecycle, success_lifecycle_new, "Studio lifecycle completion")

    text = replace_once(
        text,
        '        "signal_setup_id": get_signal_setup_id(plan, side),\n        "source_indicator_event_id": trade_payload.get("source_indicator_event_id"),\n',
        '        "signal_setup_id": trade_payload.get("signal_setup_id") or get_signal_setup_id(plan, side),\n        "execution_source": trade_payload.get("execution_source") or "V3B",\n        "studio_owner_id": trade_payload.get("studio_owner_id"),\n        "studio_strategy_id": trade_payload.get("studio_strategy_id"),\n        "studio_setup_id": trade_payload.get("studio_setup_id"),\n        "studio_account_scope": trade_payload.get("studio_account_scope"),\n        "studio_tp1_enabled": trade_payload.get("studio_tp1_enabled"),\n        "studio_risk_method": trade_payload.get("studio_risk_method"),\n        "studio_risk_value": trade_payload.get("studio_risk_value"),\n        "source_indicator_event_id": trade_payload.get("source_indicator_event_id"),\n',
        "persist Studio active-order identity",
    )

    API.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
