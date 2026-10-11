import json
from datetime import datetime, timezone
from pathlib import Path
from paths import DATA_DIR

SETTINGS_PATH = DATA_DIR / "app_settings.json"
FEATURE_FLAGS_PATH = DATA_DIR / "feature_flags.json"

DEFAULT_RISK_SETTINGS = {
    "riskPerTradePct": 1.0,
    "maxDailyLoss": None,
    "maxWeeklyLoss": None,
    "maxOpenTrades": 1,
    "tp1PercentOfTp2": 80,
    "protectedSlPercentOfTp2": 40,
    "breakEvenEnabled": True,
    "allowedSymbols": ["EURUSD", "XAUUSD"],
    "defaultTradingMode": "PAPER",
}

MIN_RISK_PER_TRADE_PCT = 0.05
MAX_RISK_PER_TRADE_PCT = 1.0
MIN_TP1_PERCENT_OF_TP2 = 1
MAX_TP1_PERCENT_OF_TP2 = 100

DEFAULT_FEATURE_FLAGS = {
    "brokerAccounts": True,
    "liveTrading": True,
    "flowAssistant": True,
    "performance": True,
    "settings": True,
    "healthPage": True,
}


def _read_json(path, default):
    from startup_recovery.checkpoint_store import read_runtime
    from startup_recovery.types import RecoveryError
    kind = 'feature_flags' if path == FEATURE_FLAGS_PATH else 'app_settings'
    data = read_runtime(path, kind)
    if data is None:  # Explicitly reconciled absence, never a parse-error default.
        return dict(default)
    if not isinstance(data, dict):
        raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')

    return {
        **default,
        **data,
    }


def _write_json(path, data):
    payload = {
        **data,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    from startup_recovery.checkpoint_store import write_runtime
    kind = 'feature_flags' if path == FEATURE_FLAGS_PATH else 'app_settings'
    write_runtime(path, kind, payload)
    return payload


def load_risk_settings():
    settings = _read_json(SETTINGS_PATH, {"risk": DEFAULT_RISK_SETTINGS})
    risk = settings.get("risk") if isinstance(settings.get("risk"), dict) else {}
    return {
        **DEFAULT_RISK_SETTINGS,
        **risk,
    }


def get_tp1_percent_of_tp2():
    try:
        value = float(
            load_risk_settings().get(
                "tp1PercentOfTp2",
                DEFAULT_RISK_SETTINGS["tp1PercentOfTp2"],
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_RISK_SETTINGS["tp1PercentOfTp2"]

    if not MIN_TP1_PERCENT_OF_TP2 <= value <= MAX_TP1_PERCENT_OF_TP2:
        return DEFAULT_RISK_SETTINGS["tp1PercentOfTp2"]

    return value


def get_tp1_ratio_of_tp2():
    return get_tp1_percent_of_tp2() / 100.0


def save_risk_settings(payload):
    payload = dict(payload or {})

    if "riskPerTradePct" in payload:
        try:
            risk_percent = float(payload["riskPerTradePct"])
        except (TypeError, ValueError):
            raise ValueError("Risk per trade must be a number")

        if not MIN_RISK_PER_TRADE_PCT <= risk_percent <= MAX_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"Risk per trade must be between "
                f"{MIN_RISK_PER_TRADE_PCT:.2f}% and "
                f"{MAX_RISK_PER_TRADE_PCT:.2f}%"
            )

        payload["riskPerTradePct"] = risk_percent

    if "tp1PercentOfTp2" in payload:
        try:
            tp1_percent = float(payload["tp1PercentOfTp2"])
        except (TypeError, ValueError):
            raise ValueError("TP1 percent of TP2 must be a number")

        if not MIN_TP1_PERCENT_OF_TP2 <= tp1_percent <= MAX_TP1_PERCENT_OF_TP2:
            raise ValueError(
                f"TP1 percent of TP2 must be between "
                f"{MIN_TP1_PERCENT_OF_TP2}% and "
                f"{MAX_TP1_PERCENT_OF_TP2}%"
            )

        payload["tp1PercentOfTp2"] = tp1_percent

    current = _read_json(SETTINGS_PATH, {"risk": DEFAULT_RISK_SETTINGS})
    risk = {
        **DEFAULT_RISK_SETTINGS,
        **(current.get("risk") if isinstance(current.get("risk"), dict) else {}),
        **payload,
    }
    current["risk"] = risk
    saved = _write_json(SETTINGS_PATH, current)
    print("SETTINGS_SAVE =", {
        "section": "risk",
        "keys": sorted(payload.keys()),
    })
    return saved["risk"]


def load_feature_flags():
    return _read_json(FEATURE_FLAGS_PATH, DEFAULT_FEATURE_FLAGS)


def save_feature_flags(payload):
    flags = {
        **DEFAULT_FEATURE_FLAGS,
        **(payload or {}),
    }
    saved = _write_json(FEATURE_FLAGS_PATH, flags)
    print("SETTINGS_SAVE =", {
        "section": "feature_flags",
        "keys": sorted((payload or {}).keys()),
    })
    return saved
