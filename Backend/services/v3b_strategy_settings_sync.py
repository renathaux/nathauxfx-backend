"""Install production V3B settings synchronization without mutating Strategy Lab.

The frozen Strategy Lab modules stay unchanged for reproducible research. This
installer wraps only the production PAPER/LIVE bridge and execution contract so
owner-approved active-profile settings become the source of truth for future
trades. Existing open-trade amendment logic is intentionally untouched.
"""
from __future__ import annotations

import copy
import threading

from services import active_strategy_config_service as active_config


_INSTALL_LOCK = threading.RLock()
_INSTALLED = False


def _matches(value, expected, tolerance=1e-9):
    try:
        return abs(float(value) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def _round_price(symbol, value):
    return round(float(value), 2 if str(symbol or "").upper().replace("/", "") == "XAUUSD" else 5)


def _management_values_from_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    stamped = payload.get("strategy_config")
    stamped = stamped if isinstance(stamped, dict) else {}
    defaults = active_config.defaults()
    target_rr = payload.get("v3b_frozen_target_rr", stamped.get("target_rr", defaults["target_rr"]))
    trigger_fraction = payload.get(
        "protection_trigger_tp2_fraction",
        float(stamped.get("protection_trigger_percent", defaults["protection_trigger_percent"])) / 100.0,
    )
    protected_fraction = payload.get(
        "protected_stop_tp2_fraction",
        float(stamped.get("protected_stop_percent", defaults["protected_stop_percent"])) / 100.0,
    )
    return {
        "target_rr": float(target_rr),
        "protection_trigger_fraction": float(trigger_fraction),
        "protected_stop_fraction": float(protected_fraction),
    }


def _apply_management_to_candidate(candidate, values):
    candidate = candidate if isinstance(candidate, dict) else {}
    side = str(candidate.get("side") or candidate.get("signal") or "").upper()
    entry = candidate.get("entry", candidate.get("entry_price"))
    sl = candidate.get("sl", candidate.get("stop_loss"))
    if side not in {"BUY", "SELL"} or entry in {None, ""} or sl in {None, ""}:
        return candidate

    entry = float(entry)
    sl = float(sl)
    risk = abs(entry - sl)
    if risk <= 0:
        return candidate
    sign = 1.0 if side == "BUY" else -1.0
    target_rr = float(values["target_rr"])
    trigger_fraction = float(values["protection_trigger_percent"]) / 100.0
    protected_fraction = float(values["protected_stop_percent"]) / 100.0
    tp2 = entry + sign * target_rr * risk
    trigger = entry + (tp2 - entry) * trigger_fraction
    protected = entry + (tp2 - entry) * protected_fraction
    symbol = candidate.get("symbol")

    candidate.update(
        {
            "tp1": _round_price(symbol, trigger),
            "tp2": _round_price(symbol, tp2),
            "protected_sl_price": _round_price(symbol, protected),
            "risk_reward_ratio": target_rr,
            "risk_reward": f"1:{target_rr:g}",
            "protection_trigger_tp2_fraction": trigger_fraction,
            "protected_stop_tp2_fraction": protected_fraction,
            "no_partial_close_at_protection_trigger": True,
            "strategy_config_profile": active_config.ACTIVE_STRATEGY_PROFILE,
            "strategy_config_version": active_config.ACTIVE_STRATEGY_VERSION,
            "strategy_config": copy.deepcopy(values),
        }
    )
    details = candidate.get("paper_entry_details")
    if isinstance(details, dict):
        details.update(
            {
                "target_rr": target_rr,
                "protection_trigger_tp2_fraction": trigger_fraction,
                "protected_stop_tp2_fraction": protected_fraction,
                "strategy_config_profile": active_config.ACTIVE_STRATEGY_PROFILE,
                "strategy_config_version": active_config.ACTIVE_STRATEGY_VERSION,
            }
        )
    return candidate


def _install_paper_bridge():
    from services import paper_v3b_bridge as bridge

    if getattr(bridge, "_ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED", False):
        return bridge
    original = bridge.build_paper_v3b_candidate

    def build_paper_v3b_candidate(*args, **kwargs):
        candidate = original(*args, **kwargs)
        if not isinstance(candidate, dict) or not candidate.get("paper_entry_ready"):
            return candidate
        values = active_config.get_active_values()
        return _apply_management_to_candidate(candidate, values)

    bridge.build_paper_v3b_candidate = build_paper_v3b_candidate
    bridge._ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED = True
    bridge._ACTIVE_STRATEGY_SETTINGS_SYNC_ORIGINAL = original
    return bridge


def _install_execution_profile():
    from services import live_v3b_execution_profile as profile

    if getattr(profile, "_ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED", False):
        return profile

    def validate_management_contract(payload):
        payload = payload if isinstance(payload, dict) else {}
        expected = _management_values_from_payload(payload)
        checks = {}
        try:
            entry = float(payload.get("entry"))
            sl = float(payload.get("sl"))
            trigger = float(
                payload.get("protection_trigger_price")
                if payload.get("protection_trigger_price") is not None
                else payload.get("tp1")
            )
            tp2 = float(payload.get("tp2"))
            protected = float(payload.get("protected_sl_price"))
            side = str(payload.get("side") or payload.get("action") or "").upper()
            risk = abs(entry - sl)
            reward = abs(tp2 - entry)
            trigger_path = abs(trigger - entry)
            protected_path = abs(protected - entry)
            directional = (
                side == "BUY" and sl < entry < protected <= trigger < tp2
            ) or (
                side == "SELL" and sl > entry > protected >= trigger > tp2
            )
            checks = {
                "profile": profile.is_v3b_execution_profile(payload),
                "directional_levels": directional,
                "target_rr": risk > 0 and abs((reward / risk) - expected["target_rr"]) <= 1e-6,
                "trigger_fraction": reward > 0 and abs(
                    (trigger_path / reward) - expected["protection_trigger_fraction"]
                ) <= 1e-6,
                "protected_fraction": reward > 0 and abs(
                    (protected_path / reward) - expected["protected_stop_fraction"]
                ) <= 1e-6,
                "no_partial_close": payload.get("no_partial_close_at_protection_trigger") is True,
            }
        except (TypeError, ValueError, ZeroDivisionError):
            checks = {"numeric_levels": False}
        return {
            "ok": bool(checks) and all(checks.values()),
            "reason": None if checks and all(checks.values()) else "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
            "details": {"checks": checks, "expected": expected},
        }

    def stamp_active_trade(trade, payload):
        target = trade if isinstance(trade, dict) else {}
        source = payload if isinstance(payload, dict) else {}
        expected = _management_values_from_payload(source)
        target.update(
            {
                "strategy_execution_profile": profile.V3B_EXECUTION_PROFILE,
                "live_strategy_model": source.get("live_strategy_model"),
                "protection_trigger_price": source.get("protection_trigger_price"),
                "protected_sl_price": source.get("protected_sl_price"),
                "protection_trigger_tp2_fraction": expected["protection_trigger_fraction"],
                "protected_stop_tp2_fraction": expected["protected_stop_fraction"],
                "no_partial_close_at_protection_trigger": True,
                "v3b_frozen_target_rr": expected["target_rr"],
                "strategy_config_profile": source.get("strategy_config_profile") or active_config.ACTIVE_STRATEGY_PROFILE,
                "strategy_config_version": source.get("strategy_config_version") or active_config.ACTIVE_STRATEGY_VERSION,
                "strategy_config": copy.deepcopy(source.get("strategy_config") or {}),
                "setup_identity": profile.stamp_v3b_identity(source.get("setup_identity") or {}),
            }
        )
        if source.get("protection_trigger_price") is not None:
            target["tp1"] = source.get("protection_trigger_price")
        return target

    profile.validate_frozen_management_contract = validate_management_contract
    profile.stamp_active_trade_with_v3b_profile = stamp_active_trade
    profile._ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED = True
    return profile


def _install_live_adapter(profile):
    from services import live_v3b_execution_adapter as adapter
    from services import live_v3b_service as live_service

    if getattr(adapter, "_ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED", False):
        return adapter

    def build_broker_core_payload(candidate):
        candidate = candidate if isinstance(candidate, dict) else {}
        try:
            values = active_config.get_active_values(force_refresh=True, fail_closed=True)
        except Exception as exc:
            return adapter._blocked(
                "WAIT_V3B_STRATEGY_CONFIG_UNAVAILABLE",
                details={"error": str(exc)},
            )

        target_rr = float(values["target_rr"])
        trigger_fraction = float(values["protection_trigger_percent"]) / 100.0
        protected_fraction = float(values["protected_stop_percent"]) / 100.0
        declared_checks = {
            "target_rr": _matches(candidate.get("risk_reward_ratio"), target_rr),
            "protection_trigger_fraction": _matches(
                candidate.get("protection_trigger_tp2_fraction"), trigger_fraction
            ),
            "protected_stop_fraction": _matches(
                candidate.get("protected_stop_tp2_fraction"), protected_fraction
            ),
            "no_partial_close": candidate.get("no_partial_close_at_protection_trigger") is True,
            "protected_stop_present": candidate.get("protected_sl_price") not in {None, ""},
            "trigger_price_present": candidate.get("tp1") not in {None, ""},
        }
        stamped_profile = candidate.get("strategy_config_profile")
        if stamped_profile not in {None, "", active_config.ACTIVE_STRATEGY_PROFILE}:
            declared_checks["strategy_profile"] = False
        stamped = candidate.get("strategy_config")
        if isinstance(stamped, dict) and stamped:
            declared_checks["strategy_config_current"] = all(
                _matches(stamped.get(key), values[key]) for key in active_config.FIELD_DEFINITIONS
            )
        if not all(declared_checks.values()):
            return adapter._blocked(
                "WAIT_V3B_STRATEGY_CONFIG_STALE",
                details={
                    "declared_checks": declared_checks,
                    "active_profile": active_config.ACTIVE_STRATEGY_PROFILE,
                    "active_values": copy.deepcopy(values),
                },
            )

        handoff = live_service.build_live_v3b_execution_payload(candidate)
        if not handoff.get("ok"):
            return adapter._blocked(
                handoff.get("reason") or "WAIT_V3B_LIVE_PAYLOAD",
                details=handoff.get("details"),
            )

        payload = copy.deepcopy(handoff.get("payload") or {})
        payload.update(
            {
                "live_strategy_model": live_service.LIVE_V3B_MODEL,
                "strategy_execution_profile": profile.V3B_EXECUTION_PROFILE,
                "protection_trigger_price": candidate.get("tp1"),
                "protected_sl_price": candidate.get("protected_sl_price"),
                "protection_trigger_tp2_fraction": trigger_fraction,
                "protected_stop_tp2_fraction": protected_fraction,
                "no_partial_close_at_protection_trigger": True,
                "v3b_frozen_target_rr": target_rr,
                "strategy_config_profile": active_config.ACTIVE_STRATEGY_PROFILE,
                "strategy_config_version": active_config.ACTIVE_STRATEGY_VERSION,
                "strategy_config": copy.deepcopy(values),
                "setup_identity": profile.stamp_v3b_identity(payload.get("setup_identity") or {}),
            }
        )
        contract = profile.validate_frozen_management_contract(payload)
        if not contract.get("ok"):
            return adapter._blocked(
                contract.get("reason") or "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
                payload=payload,
                details=contract.get("details"),
            )
        return {
            "ok": True,
            "submitted": False,
            "reason": None,
            "payload": payload,
            "execution_profile": profile.V3B_EXECUTION_PROFILE,
        }

    adapter.build_v3b_broker_core_payload = build_broker_core_payload
    adapter.validate_frozen_management_contract = profile.validate_frozen_management_contract
    adapter.stamp_v3b_identity = profile.stamp_v3b_identity
    adapter._ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED = True
    return adapter


def _install_strategy_settings_api_compat():
    """Make the existing authenticated /strategy/settings API describe V3B."""
    from services import strategy_settings_service as legacy

    if getattr(legacy, "_ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED", False):
        return legacy

    original_history = legacy.get_strategy_settings_history

    def get_strategy_settings(session_factory=None):
        return active_config.get_active_strategy_settings(session_factory)

    def save_strategy_settings(payload, updated_by, session_factory=None, now=None):
        try:
            return active_config.save_active_strategy_settings(
                payload,
                updated_by=updated_by,
                session_factory=session_factory,
                now=now,
            )
        except active_config.ActiveStrategyConfigError as exc:
            raise legacy.StrategySettingsValidationError(str(exc)) from exc

    def reset_strategy_settings(*, confirmed, updated_by, session_factory=None, now=None):
        try:
            return active_config.reset_active_strategy_settings(
                confirmed=confirmed,
                updated_by=updated_by,
                session_factory=session_factory,
                now=now,
            )
        except active_config.ActiveStrategyConfigError as exc:
            raise legacy.StrategySettingsValidationError(str(exc)) from exc

    legacy.get_strategy_settings = get_strategy_settings
    legacy.save_strategy_settings = save_strategy_settings
    legacy.reset_strategy_settings = reset_strategy_settings
    legacy.get_strategy_settings_history = original_history
    legacy._ACTIVE_STRATEGY_SETTINGS_SYNC_INSTALLED = True
    return legacy


def install_v3b_strategy_settings_sync():
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return {"ok": True, "installed": False, "profile": active_config.ACTIVE_STRATEGY_PROFILE}

        # Patch PAPER first so live_v3b_service imports the synchronized bridge.
        _install_paper_bridge()
        profile = _install_execution_profile()
        _install_live_adapter(profile)
        _install_strategy_settings_api_compat()
        _INSTALLED = True
        return {"ok": True, "installed": True, "profile": active_config.ACTIVE_STRATEGY_PROFILE}
