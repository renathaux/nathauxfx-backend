import sys

from strategies import shared as _shared
from strategies import strict_trader as _strict_trader
from strategies.eurusd_strategy import analyze_eurusd
from strategies.smc_breakout_adapter import evaluate_15m_breakout as evaluate_smc_15m_breakout
from strategies.xauusd_risk_guard import build_xauusd_risk_levels
from strategies.xauusd_strategy import analyze_xauusd


# Make the TradingView-style SMC structure engine authoritative for V1's
# closed-15m BOS/CHoCH gate. Everything after that gate remains strict_trader:
# EMA, later 5m confirmation, SL/TP/RR, risk and execution.
_strict_trader.evaluate_15m_breakout = evaluate_smc_15m_breakout


# Consolidation is observation-only now. Keep calculating the old detector for
# diagnostics, but it is no longer allowed to block or invalidate a valid SMC
# BOS/CHoCH setup.
_original_classify_consolidation = _strict_trader.classify_consolidation


def classify_consolidation_observation_only(data_15m, symbol):
    result = _original_classify_consolidation(data_15m, symbol)
    detected = bool(result.get("is_consolidation")) if isinstance(result, dict) else False
    payload = dict(result or {})
    payload.update({
        "detected_is_consolidation": detected,
        "is_consolidation": False,
        "reason": None,
        "filter_enabled": False,
        "blocking": False,
        "entry_gate_enabled": False,
        "policy": "OBSERVATION_ONLY",
    })
    return payload


_strict_trader.classify_consolidation = classify_consolidation_observation_only


# Route both symbols' accepted 15m SMC snapshot through risk construction.
# Gold retains its existing five-pip structural buffer implementation.
_original_build_risk_levels = _strict_trader.build_risk_levels


def build_risk_levels_with_xauusd_15m(
    data_15m,
    side,
    entry,
    symbol,
    setup_break_time=None,
    execution_settings=None,
    event_invalidation_swing=None,
):
    normalized = _shared.normalize_symbol(symbol)
    if normalized != "XAUUSD":
        return _original_build_risk_levels(
            data_15m,
            side,
            entry,
            symbol,
            setup_break_time=setup_break_time,
            execution_settings=execution_settings,
            event_invalidation_swing=event_invalidation_swing,
        )
    return build_xauusd_risk_levels(
        data_15m,
        side,
        entry,
        symbol,
        setup_break_time=setup_break_time,
        execution_settings=execution_settings,
        event_invalidation_swing=event_invalidation_swing,
    )


_strict_trader.build_risk_levels = build_risk_levels_with_xauusd_15m


# Do not let a persisted setup that was blocked by the old consolidation gate
# survive this policy change. Remove those watches so only a fresh SMC event can
# become a setup after deployment.
def _clear_legacy_consolidation_blocked_watches():
    from startup_recovery.checkpoint_store import read_runtime, write_runtime
    from startup_recovery.types import RecoveryError
    # Read admitted state, not an import-time global or unverified legacy file.
    watches = read_runtime(_shared.FIFTEEN_M_SWING_WATCH_FILE, 'fifteen_m_swing_watch')
    if watches is None:
        return
    if not isinstance(watches, dict) or any(not isinstance(w, dict) for w in watches.values()):
        raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
    replacement = {key: watch for key, watch in watches.items()
                   if str(watch.get('status') or '').upper() != _strict_trader.BLOCKED_BREAKOUT_STATUS}
    if replacement != watches:
        # Commit the admitted transition before changing memory. Old candidate
        # files/generations remain evidence even if publication fails.
        write_runtime(_shared.FIFTEEN_M_SWING_WATCH_FILE, 'fifteen_m_swing_watch', replacement)
        from startup_recovery.operation_context import publication
        with publication():
            _shared.FIFTEEN_M_SWING_WATCH = replacement


# Applied explicitly after admitted watch restoration, not during import.
_shared._clear_legacy_consolidation_blocked_watches = _clear_legacy_consolidation_blocked_watches
get_strict_mtf_signal = _strict_trader.get_mtf_signal

_original_get_mtf_signal = _shared.get_mtf_signal


def get_mtf_signal(data_5m, data_15m, data_1h, symbol):
    return get_strict_mtf_signal(data_5m, data_15m, data_1h, symbol)


_shared.get_mtf_signal = get_mtf_signal
_shared._original_get_mtf_signal = _original_get_mtf_signal
_shared.analyze_eurusd = analyze_eurusd
_shared.analyze_xauusd = analyze_xauusd
_shared.get_strict_mtf_signal = get_strict_mtf_signal
_shared.evaluate_smc_15m_breakout = evaluate_smc_15m_breakout
_shared.classify_consolidation_observation_only = classify_consolidation_observation_only
_shared.build_risk_levels_with_xauusd_15m = build_risk_levels_with_xauusd_15m

sys.modules[__name__] = _shared
