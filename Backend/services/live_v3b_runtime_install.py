"""Runtime installation for the frozen V3B LIVE execution profile.

The installer is deliberately dormant unless the existing V3B feature gates are
explicitly enabled.  With V3B disabled, every wrapped function delegates to the
existing V1 implementation unchanged.

This module does not enable LIVE Auto, does not change either V3B environment
switch, and does not submit an order during installation.  It only teaches the
server-owned runtime how to route an explicitly stamped ``V3B_M5_FROZEN``
payload without silently applying V1's 15m EMA or legacy TP1/protected-stop
math.
"""
from __future__ import annotations

import copy
import time

from services.live_v3b_execution_adapter import (
    dispatch_v3b_to_live_core,
    v3b_broker_handoff_enabled,
)
from services.live_v3b_execution_profile import (
    V3B_EXECUTION_PROFILE,
    is_v3b_execution_profile,
    stamp_active_trade_with_v3b_profile,
    stamp_v3b_identity,
    validate_frozen_management_contract,
    validate_v3b_locked_entry_state,
)
from services.live_v3b_service import (
    build_live_v3b_candidate,
    live_v3b_enabled,
)


_INSTALL_MARKER = "_V3B_RUNTIME_PROFILE_INSTALLED"


def _blocked(reason, details=None):
    return {
        "ok": False,
        "submitted": False,
        "reason": reason,
        "details": copy.deepcopy(details or {}),
        "execution_profile": V3B_EXECUTION_PROFILE,
    }


def _copy_v3b_execution_fields(target, source):
    """Restore fields that legacy V1 preparation is not allowed to derive."""
    target = target if isinstance(target, dict) else {}
    source = source if isinstance(source, dict) else {}
    for key in (
        "strategy_execution_profile",
        "live_strategy_model",
        "protection_trigger_price",
        "protected_sl_price",
        "protection_trigger_tp2_fraction",
        "protected_stop_tp2_fraction",
        "no_partial_close_at_protection_trigger",
        "v3b_frozen_target_rr",
        "signal_setup_id",
        "source_indicator_event_id",
        "indicator_event_identity",
        "m5_confirmation_id",
        "m5_confirmation_identity",
        "confirmation_5m",
        "five_m_break_time",
        "five_m_break_close_time",
        "five_m_closed_candle_time",
        "setup_candle_time",
        "strategy_setup_type",
    ):
        if key in source:
            target[key] = copy.deepcopy(source.get(key))
    target["setup_identity"] = stamp_v3b_identity(
        source.get("setup_identity") or target.get("setup_identity") or {}
    )
    # V3B calls this level TP1 for compatibility with the existing lifecycle,
    # but it is a protection trigger only; no partial close occurs there.
    if source.get("protection_trigger_price") is not None:
        target["tp1"] = source.get("protection_trigger_price")
    elif source.get("tp1") is not None:
        target["tp1"] = source.get("tp1")
        target["protection_trigger_price"] = source.get("tp1")
    return target


def _get_lifecycle(api_module, event_id):
    if not event_id:
        return {}
    try:
        account_id = str(
            api_module.LIVE_ACCOUNT_STATE.get("account_id")
            or api_module.LIVE_ACCOUNT_STATE.get("active_account_id")
            or ""
        )
        return (
            api_module.get_event_lifecycles(
                [event_id],
                owner_id="OWNER",
                account_id=account_id,
            ).get(event_id, {}).get("LIVE")
            or {}
        )
    except Exception:
        return {}


def _protect_v3b_trade(api_module, trade):
    """Move broker SL to the frozen +1.14R price, never legacy V1's 50% price."""
    if not isinstance(trade, dict):
        return trade
    if trade.get("hit_tp1") and api_module.live_sl_protection_confirmed(trade):
        return trade

    try:
        requested = float(trade.get("protected_sl_price"))
        protected_sl, tick_size, digits = api_module.normalize_price_to_broker_increment(
            requested,
            trade,
        )
    except (TypeError, ValueError):
        trade["sl_protection_failed"] = True
        trade["sl_protection_warning"] = "BROKER SL PROTECTION FAILED"
        trade["sl_protection_error"] = "V3B protected SL unavailable"
        api_module.persist_live_trade_state(trade)
        return trade

    position_id = trade.get("position_id") or trade.get("broker_position_id")
    original_sl = trade.get("original_sl", trade.get("sl"))
    trade.update({
        "hit_tp1": True,
        "tp1_hit": True,
        "original_sl": original_sl,
        "protected_sl_price": protected_sl,
        "result": "TP1 HIT",
    })
    if not position_id:
        trade.update({
            "protection_requested": False,
            "protection_confirmed": False,
            "profit_protected": False,
            "sl_protection_failed": True,
            "sl_protection_warning": "BROKER SL PROTECTION FAILED",
            "sl_protection_error": "Missing cTrader position id",
        })
        api_module.persist_live_trade_state(trade)
        return trade

    trade.update({
        "protection_requested": True,
        "protection_confirmed": False,
    })
    api_module.persist_live_trade_state(trade)
    modify_result = api_module.modify_position_stop_loss(
        position_id,
        protected_sl,
        take_profit_price=trade.get("tp2"),
    )
    trade["sl_protection_broker_result"] = modify_result
    if not isinstance(modify_result, dict) or not modify_result.get("ok"):
        trade.update({
            "profit_protected": False,
            "sl_protection_failed": True,
            "sl_protection_warning": "BROKER SL PROTECTION FAILED",
            "sl_protection_error": (
                (modify_result or {}).get("reason")
                if isinstance(modify_result, dict)
                else "Unknown cTrader SL modify error"
            ) or "Unknown cTrader SL modify error",
        })
        api_module.persist_live_trade_state(trade)
        return trade

    broker_sl, readback = api_module.read_back_broker_stop_loss(trade)
    try:
        verified = (
            broker_sl is not None
            and abs(float(broker_sl) - float(protected_sl)) <= float(tick_size) + 1e-12
        )
    except (TypeError, ValueError):
        verified = False
    trade["sl_protection_verification"] = {
        "ok": verified,
        "requested_sl": protected_sl,
        "broker_sl": broker_sl,
        "tick_size": tick_size,
        "digits": digits,
        "within_tick_tolerance": verified,
        "readback": readback,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
    }
    if not verified:
        trade.update({
            "profit_protected": False,
            "protection_confirmed": False,
            "sl_protection_failed": True,
            "sl_protection_warning": "BROKER SL PROTECTION FAILED",
            "sl_protection_error": (
                (readback or {}).get("reason")
                if isinstance(readback, dict)
                else None
            ) or f"Broker SL {broker_sl} did not match requested SL {protected_sl}",
        })
        api_module.persist_live_trade_state(trade)
        return trade

    trade.update({
        "profit_protected": True,
        "protection_confirmed": True,
        "sl": protected_sl,
        "sl_protection_failed": False,
        "sl_protection_warning": None,
        "sl_protection_error": None,
    })
    api_module.persist_live_trade_state(trade)
    return trade


def install_live_v3b_runtime(api_module, *, strict_trader_module=None):
    """Install profile-aware wrappers once; V1 remains default when V3B is OFF."""
    if getattr(api_module, _INSTALL_MARKER, False):
        return {"ok": True, "installed": False, "reason": "already_installed"}

    if strict_trader_module is None:
        from strategies import strict_trader as strict_trader_module

    original_prepare = api_module.prepare_ctrader_trade
    original_locked_gate = api_module.validate_auto_entry_state_locked
    original_fresh_gate = api_module.validate_fresh_ema_permission_locked
    original_protect = api_module.protect_live_trade_after_tp1
    original_execute = api_module.execute_live_order_core
    original_auto_cycle = api_module.run_ctrader_auto_trade_checks

    def prepare_ctrader_trade_profile_aware(payload, volume=0.01):
        if not is_v3b_execution_profile(payload):
            return original_prepare(payload, volume=volume)
        prepared = original_prepare(payload, volume=volume)
        if not isinstance(prepared, dict) or not prepared.get("ok"):
            return prepared
        _copy_v3b_execution_fields(prepared, payload)
        contract = validate_frozen_management_contract(prepared)
        if not contract.get("ok"):
            return {
                **prepared,
                "ok": False,
                "reason": contract.get("reason"),
                "message": contract.get("reason"),
                "v3b_frozen_management": contract,
            }
        prepared["v3b_frozen_management"] = contract
        return prepared

    def validate_auto_entry_state_locked_profile_aware(
        symbol,
        side,
        trade_payload,
        broker_positions,
        now=None,
    ):
        if not is_v3b_execution_profile(trade_payload):
            return original_locked_gate(
                symbol,
                side,
                trade_payload,
                broker_positions,
                now=now,
            )
        normalized = api_module.normalize_symbol(symbol)
        return validate_v3b_locked_entry_state(
            normalized,
            side,
            trade_payload,
            broker_positions,
            active_trade=api_module.LIVE_ACTIVE_ORDERS.get(normalized),
            now=(time.time() if now is None else now),
            last_closed_at=api_module.LIVE_LAST_POSITION_CLOSED_AT.get(normalized, 0),
            cooldown_seconds=api_module.get_live_post_close_cooldown_seconds(),
            setup_id_builder=api_module.get_signal_setup_id,
            lifecycle=_get_lifecycle(
                api_module,
                trade_payload.get("source_indicator_event_id"),
            ),
        )

    def validate_fresh_ema_permission_locked_profile_aware(
        symbol,
        side,
        setup_identity=None,
    ):
        identity = setup_identity if isinstance(setup_identity, dict) else {}
        if str(identity.get("strategy_execution_profile") or "").upper() != V3B_EXECUTION_PROFILE:
            return original_fresh_gate(symbol, side, setup_identity=setup_identity)
        return {
            "ok": True,
            "reason": None,
            "details": {
                "symbol": api_module.normalize_symbol(symbol),
                "side": str(side or "").upper(),
                "strategy_execution_profile": V3B_EXECUTION_PROFILE,
                "setup_timeframe": "5m",
                "v1_15m_ema_bypassed": True,
                "v1_consolidation_bypassed": True,
                "v3b_profile_rules_only": True,
            },
        }

    def protect_live_trade_after_tp1_profile_aware(trade):
        if not is_v3b_execution_profile(trade):
            return original_protect(trade)
        return _protect_v3b_trade(api_module, trade)

    def execute_live_order_core_profile_aware(payload, source="manual"):
        result = original_execute(payload, source=source)
        if not is_v3b_execution_profile(payload) or not isinstance(result, dict) or not result.get("ok"):
            return result
        symbol = api_module.normalize_symbol(payload.get("symbol"))
        active = api_module.LIVE_ACTIVE_ORDERS.get(symbol)
        if isinstance(active, dict):
            stamp_active_trade_with_v3b_profile(active, payload)
            for key in (
                "signal_setup_id",
                "source_indicator_event_id",
                "indicator_event_identity",
                "m5_confirmation_id",
                "m5_confirmation_identity",
            ):
                if key in payload:
                    active[key] = copy.deepcopy(payload.get(key))
            active["planned_tp1"] = payload.get("protection_trigger_price") or payload.get("tp1")
            active["planned_tp2"] = payload.get("tp2")
            api_module.persist_live_trade_state(active)
            try:
                api_module.save_live_backup()
            except Exception:
                pass
            result["active_order"] = active
        return result

    def run_ctrader_auto_trade_checks_profile_aware(panel_data):
        # Absolutely no behavior change while the V3B strategy switch is OFF.
        if not live_v3b_enabled():
            return original_auto_cycle(panel_data)

        api_module.refresh_auto_trade_state_from_persistence("v3b_execution_cycle")
        api_module.sync_ctrader_account_state()
        live_auto_on = bool(api_module.LIVE_AUTO_TRADE_ENABLED.get("enabled"))
        broker_ready = bool(
            api_module.LIVE_ACCOUNT_STATE.get("connected")
            and api_module.LIVE_ACCOUNT_STATE.get("execution_ready")
        )
        results = []

        for symbol in ("EURUSD", "XAUUSD"):
            try:
                data_5m = api_module.get_ctrader_market_data(
                    symbol,
                    "5m",
                    limit=250,
                    force_refresh=False,
                )
                candidate = build_live_v3b_candidate(
                    symbol,
                    data_5m,
                    strict_trader_module=strict_trader_module,
                    setup_id_builder=api_module.get_signal_setup_id,
                    enabled=True,
                )
            except Exception as exc:
                candidate = {
                    "symbol": symbol,
                    "live_v3b_ready": False,
                    "live_v3b_reason": "WAIT_V3B_RUNTIME_EVALUATION",
                    "live_v3b_details": {"error": str(exc)},
                }

            side = str(candidate.get("side") or candidate.get("signal") or "WAIT").upper()
            if not candidate.get("live_v3b_ready"):
                reason = candidate.get("live_v3b_reason") or "WAIT_V3B_LIVE_QUALIFICATION"
                try:
                    api_module.set_auto_trade_status(
                        symbol=symbol,
                        signal=side,
                        action=side if side in {"BUY", "SELL"} else None,
                        status="WAIT",
                        reason=reason,
                        details=candidate.get("live_v3b_details"),
                    )
                except Exception:
                    pass
                results.append(_blocked(reason, candidate.get("live_v3b_details")))
                continue

            if not broker_ready:
                reason = "Live Auto paused — broker disconnected"
                results.append(_blocked(reason))
                try:
                    api_module.set_auto_trade_status(
                        symbol=symbol,
                        signal=side,
                        action=side,
                        status="BLOCKED",
                        reason=reason,
                    )
                except Exception:
                    pass
                continue

            result = dispatch_v3b_to_live_core(
                candidate,
                executor=api_module.execute_live_order_core,
                live_auto_enabled=live_auto_on,
                strategy_enabled=True,
                broker_handoff_enabled=v3b_broker_handoff_enabled(),
                execution_profile_supported=True,
            )
            results.append(result)
            try:
                api_module.set_auto_trade_status(
                    symbol=symbol,
                    signal=side,
                    action=side if side in {"BUY", "SELL"} else None,
                    status=("EXECUTED" if result.get("ok") else "BLOCKED"),
                    reason=result.get("reason") or (
                        "V3B broker handoff accepted" if result.get("ok") else "V3B blocked"
                    ),
                    details={
                        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
                        "submitted": bool(result.get("submitted")),
                    },
                )
            except Exception:
                pass

        return results

    api_module.prepare_ctrader_trade = prepare_ctrader_trade_profile_aware
    api_module.validate_auto_entry_state_locked = validate_auto_entry_state_locked_profile_aware
    api_module.validate_fresh_ema_permission_locked = validate_fresh_ema_permission_locked_profile_aware
    api_module.protect_live_trade_after_tp1 = protect_live_trade_after_tp1_profile_aware
    api_module.execute_live_order_core = execute_live_order_core_profile_aware
    api_module.run_ctrader_auto_trade_checks = run_ctrader_auto_trade_checks_profile_aware
    setattr(api_module, _INSTALL_MARKER, True)
    api_module.ENGINE_RUNTIME_STATE["v3b_runtime_profile"] = {
        "installed": True,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "strategy_enabled": live_v3b_enabled(),
        "broker_handoff_enabled": v3b_broker_handoff_enabled(),
        "live_auto_enabled": bool(api_module.LIVE_AUTO_TRADE_ENABLED.get("enabled")),
    }
    return {
        "ok": True,
        "installed": True,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "strategy_enabled": live_v3b_enabled(),
        "broker_handoff_enabled": v3b_broker_handoff_enabled(),
        "live_auto_enabled": bool(api_module.LIVE_AUTO_TRADE_ENABLED.get("enabled")),
    }
