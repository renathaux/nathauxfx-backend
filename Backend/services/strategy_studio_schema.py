"""Canonical saved-definition schema for Strategy Studio.

This module is deliberately broker/LIVE independent.  It owns only the persisted
vocabulary, structural validation, normalization, and human-readable summary.
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SYMBOLS = {"EURUSD", "XAUUSD"}
TRADING_TIMEFRAMES = {"5m", "15m", "1h"}
TREND_METHODS = {"BOS_CHOCH", "EMA_50", "EMA_200", "SWING_STRUCTURE"}
BREAK_RULES = {"CLOSE_BEYOND", "MIN_BODY_PERCENT", "MIN_DISTANCE"}
CONFIRMATION_RULES = {"NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND", "RETEST_LEVEL", "MIN_BODY_PERCENT"}
ENTRY_METHODS = {"BOS_CHOCH_CLOSE", "CONFIRMATION_CLOSE", "RETEST"}
STOP_METHODS = {"LAST_SWING", "FIXED_DISTANCE"}
TP2_METHODS = {"FIXED_R", "FIXED_DISTANCE", "OPPOSITE_SWING"}
RISK_METHODS = {"PERCENT_BALANCE", "FIXED_DOLLARS"}
FUNDAMENTAL_MODES = {"BLOCK_OPPOSITE", "REQUIRE_ALIGNMENT"}
TIMEFRAME_RANK = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrendDefinition(_StrictModel):
    timeframe: Literal["15m", "1h", "4h"] | None = None
    methods: list[Literal["BOS_CHOCH", "EMA_50", "EMA_200", "SWING_STRUCTURE"]] = Field(default_factory=list)


class StructureDefinition(_StrictModel):
    trigger: Literal["BOS_CHOCH"]
    break_validation: list[Literal["CLOSE_BEYOND", "MIN_BODY_PERCENT", "MIN_DISTANCE"]] = Field(default_factory=list)
    minimum_body_percent: float | None = None
    minimum_distance_pips: float | None = None


class ConfirmationDefinition(_StrictModel):
    rules: list[Literal["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND", "RETEST_LEVEL", "MIN_BODY_PERCENT"]] = Field(default_factory=list)
    minimum_body_percent: float | None = None


class EntryDefinition(_StrictModel):
    method: Literal["BOS_CHOCH_CLOSE", "CONFIRMATION_CLOSE", "RETEST"]


class StopLossDefinition(_StrictModel):
    method: Literal["LAST_SWING", "FIXED_DISTANCE"]
    buffer_pips: float | None = None
    fixed_distance: float | None = None


class TP1Definition(_StrictModel):
    enabled: bool
    target_r: float | None = None
    close_percent: float | None = None
    protection_r: float | None = None


class TP2Definition(_StrictModel):
    method: Literal["FIXED_R", "FIXED_DISTANCE", "OPPOSITE_SWING"]
    value: float | None = None


class RiskDefinition(_StrictModel):
    method: Literal["PERCENT_BALANCE", "FIXED_DOLLARS"]
    value: float


class FundamentalDefinition(_StrictModel):
    mode: Literal["BLOCK_OPPOSITE", "REQUIRE_ALIGNMENT"] = "BLOCK_OPPOSITE"


class StrategyDefinition(_StrictModel):
    schema_version: Literal[1]
    symbols: list[Literal["EURUSD", "XAUUSD"]]
    trading_timeframe: Literal["5m", "15m", "1h"]
    trend: TrendDefinition
    structure: StructureDefinition
    confirmation: ConfirmationDefinition
    entry: EntryDefinition
    stop_loss: StopLossDefinition
    tp1: TP1Definition
    tp2: TP2Definition
    risk: RiskDefinition
    fundamentals: FundamentalDefinition = Field(default_factory=FundamentalDefinition)

    @model_validator(mode="after")
    def validate_cross_fields(self):
        errors = _cross_field_errors(self)
        if errors:
            joined = "; ".join(f"{path}: {message}" for path, message in errors.items())
            raise ValueError(joined)
        return self


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _valid_positive(value) -> bool:
    return _is_finite_number(value) and float(value) > 0


def _valid_nonnegative(value) -> bool:
    return _is_finite_number(value) and float(value) >= 0


def _valid_percent(value) -> bool:
    return _is_finite_number(value) and 0 < float(value) <= 100


def _duplicates(values: list[str]) -> bool:
    return len(values) != len(set(values))


def _cross_field_errors(value: StrategyDefinition) -> dict[str, str]:
    errors: dict[str, str] = {}

    if not value.symbols:
        errors["symbols"] = "Select at least one symbol"
    elif _duplicates(value.symbols):
        errors["symbols"] = "Symbols cannot contain duplicates"

    if _duplicates(value.trend.methods):
        errors["trend.methods"] = "Trend methods cannot contain duplicates"
    if value.trend.methods:
        if value.trend.timeframe is None:
            errors["trend.timeframe"] = "Trend timeframe is required when trend filters are selected"
        elif TIMEFRAME_RANK[value.trend.timeframe] <= TIMEFRAME_RANK[value.trading_timeframe]:
            errors["trend.timeframe"] = "Trend timeframe must be higher than trading timeframe"
    elif value.trend.timeframe is not None:
        errors["trend.methods"] = "Select at least one trend method or clear trend timeframe"

    if _duplicates(value.structure.break_validation):
        errors["structure.break_validation"] = "Break validation rules cannot contain duplicates"
    if "MIN_BODY_PERCENT" in value.structure.break_validation:
        if not _valid_percent(value.structure.minimum_body_percent):
            errors["structure.minimum_body_percent"] = "Minimum candle body must be greater than 0 and no more than 100"
    elif value.structure.minimum_body_percent is not None:
        errors["structure.minimum_body_percent"] = "Minimum candle body requires the Minimum candle body rule"
    if "MIN_DISTANCE" in value.structure.break_validation:
        if not _valid_positive(value.structure.minimum_distance_pips):
            errors["structure.minimum_distance_pips"] = "Minimum distance must be greater than 0"
    elif value.structure.minimum_distance_pips is not None:
        errors["structure.minimum_distance_pips"] = "Minimum distance requires the Minimum distance rule"

    if _duplicates(value.confirmation.rules):
        errors["confirmation.rules"] = "Confirmation rules cannot contain duplicates"
    if "MIN_BODY_PERCENT" in value.confirmation.rules:
        if not _valid_percent(value.confirmation.minimum_body_percent):
            errors["confirmation.minimum_body_percent"] = "Minimum candle body must be greater than 0 and no more than 100"
    elif value.confirmation.minimum_body_percent is not None:
        errors["confirmation.minimum_body_percent"] = "Minimum candle body requires the Minimum candle body rule"

    if value.entry.method == "CONFIRMATION_CLOSE" and not value.confirmation.rules:
        errors["entry.method"] = "Confirmation-close entry requires at least one confirmation rule"
    elif value.entry.method == "RETEST" and "RETEST_LEVEL" not in value.confirmation.rules:
        errors["entry.method"] = "Retest entry requires Retest broken level confirmation"
    elif value.entry.method == "BOS_CHOCH_CLOSE" and value.confirmation.rules:
        errors["entry.method"] = "BOS/CHOCH-close entry cannot depend on future confirmation rules"

    if value.stop_loss.method == "LAST_SWING":
        if value.stop_loss.fixed_distance is not None:
            errors["stop_loss.fixed_distance"] = "Fixed distance is not used with Last Swing"
        if value.stop_loss.buffer_pips is not None and not _valid_nonnegative(value.stop_loss.buffer_pips):
            errors["stop_loss.buffer_pips"] = "Swing buffer must be zero or greater"
    else:
        if not _valid_positive(value.stop_loss.fixed_distance):
            errors["stop_loss.fixed_distance"] = "Fixed stop distance must be greater than 0"
        if value.stop_loss.buffer_pips is not None:
            errors["stop_loss.buffer_pips"] = "Swing buffer is only used with Last Swing"

    if value.tp2.method == "OPPOSITE_SWING":
        if value.tp2.value is not None:
            errors["tp2.value"] = "Opposite Swing TP2 does not use a numeric value"
    elif not _valid_positive(value.tp2.value):
        errors["tp2.value"] = "TP2 value must be greater than 0"

    if value.tp1.enabled:
        if not _valid_positive(value.tp1.target_r):
            errors["tp1.target_r"] = "TP1 target must be greater than 0"
        if not _valid_percent(value.tp1.close_percent):
            errors["tp1.close_percent"] = "TP1 close percent must be greater than 0 and no more than 100"
        if not _is_finite_number(value.tp1.protection_r):
            errors["tp1.protection_r"] = "TP1 protection is required"

    if not _valid_positive(value.risk.value):
        errors["risk.value"] = "Risk value must be a finite number greater than 0"

    return errors


def _normalize_tp1(payload: dict) -> dict:
    normalized = dict(payload or {})
    tp1 = normalized.get("tp1")
    if isinstance(tp1, dict) and tp1.get("enabled") is False:
        normalized["tp1"] = {
            **tp1,
            "target_r": None,
            "close_percent": None,
            "protection_r": None,
        }
    return normalized


def _pydantic_error_path(loc: tuple) -> str:
    return ".".join(str(part) for part in loc if part != "__root__") or "strategy"


def validation_errors(payload: dict) -> dict[str, str]:
    """Return field-addressable validation errors for inline builder rendering."""
    normalized = _normalize_tp1(payload if isinstance(payload, dict) else {})
    try:
        candidate = StrategyDefinition.model_validate(normalized)
    except Exception as exc:
        if hasattr(exc, "errors"):
            result: dict[str, str] = {}
            for item in exc.errors():
                path = _pydantic_error_path(tuple(item.get("loc") or ()))
                message = str(item.get("msg") or "Invalid value")
                if message.startswith("Value error, "):
                    # Cross-field validator encodes path/message pairs.  Recover
                    # them below via a construct-only parse when possible.
                    continue
                result.setdefault(path, message)
            # Re-parse nested models without the top-level validator so we can
            # report cross-field locations deterministically.
            try:
                bare = _parse_without_cross_validation(normalized)
                result.update(_cross_field_errors(bare))
            except Exception:
                pass
            return result
        return {"strategy": str(exc)}
    return _cross_field_errors(candidate)


def _parse_without_cross_validation(payload: dict) -> StrategyDefinition:
    """Parse all fields using nested schemas while bypassing only top-level cross validation."""
    data = {
        "schema_version": payload["schema_version"],
        "symbols": payload["symbols"],
        "trading_timeframe": payload["trading_timeframe"],
        "trend": TrendDefinition.model_validate(payload["trend"]),
        "structure": StructureDefinition.model_validate(payload["structure"]),
        "confirmation": ConfirmationDefinition.model_validate(payload["confirmation"]),
        "entry": EntryDefinition.model_validate(payload["entry"]),
        "stop_loss": StopLossDefinition.model_validate(payload["stop_loss"]),
        "tp1": TP1Definition.model_validate(payload["tp1"]),
        "tp2": TP2Definition.model_validate(payload["tp2"]),
        "risk": RiskDefinition.model_validate(payload["risk"]),
        "fundamentals": FundamentalDefinition.model_validate(
            payload.get("fundamentals") or {"mode": "BLOCK_OPPOSITE"}
        ),
    }
    # model_construct bypasses validators but keeps typed nested objects.
    return StrategyDefinition.model_construct(**data)


def normalize_definition(payload: dict) -> dict:
    normalized = _normalize_tp1(payload)
    errors = validation_errors(normalized)
    if errors:
        raise ValueError("; ".join(f"{path}: {message}" for path, message in errors.items()))
    parsed = StrategyDefinition.model_validate(normalized)
    return parsed.model_dump(mode="json")


def _fmt(value: float | int | None) -> str:
    if value is None:
        return ""
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:g}"


def strategy_summary(definition: dict) -> str:
    value = normalize_definition(definition)
    parts: list[str] = []
    tf = value["trading_timeframe"]

    trend = value["trend"]
    if trend["methods"]:
        labels = {
            "BOS_CHOCH": "BOS/CHOCH",
            "EMA_50": "EMA 50",
            "EMA_200": "EMA 200",
            "SWING_STRUCTURE": "swing structure",
        }
        parts.append(f"{trend['timeframe']} trend " + " + ".join(labels[item] for item in trend["methods"]))

    parts.append(f"{tf} BOS/CHOCH")

    structure = value["structure"]
    validation_labels = []
    for rule in structure["break_validation"]:
        if rule == "CLOSE_BEYOND":
            validation_labels.append("close beyond level")
        elif rule == "MIN_BODY_PERCENT":
            validation_labels.append(f"body >= {_fmt(structure['minimum_body_percent'])}%")
        elif rule == "MIN_DISTANCE":
            validation_labels.append(f"distance >= {_fmt(structure['minimum_distance_pips'])} pips")
    if validation_labels:
        parts.append(" + ".join(validation_labels))

    confirmation = value["confirmation"]
    confirmation_labels = []
    for rule in confirmation["rules"]:
        if rule == "NEXT_SAME_DIRECTION":
            confirmation_labels.append("next candle same direction")
        elif rule == "SECOND_CLOSE_BEYOND":
            confirmation_labels.append("second close beyond level")
        elif rule == "RETEST_LEVEL":
            confirmation_labels.append("retest broken level")
        elif rule == "MIN_BODY_PERCENT":
            confirmation_labels.append(f"body >= {_fmt(confirmation['minimum_body_percent'])}%")
    if confirmation_labels:
        parts.append(" + ".join(confirmation_labels))

    entry_labels = {
        "BOS_CHOCH_CLOSE": "BOS/CHOCH close",
        "CONFIRMATION_CLOSE": "confirmation close",
        "RETEST": "retest entry",
    }
    parts.append(entry_labels[value["entry"]["method"]])

    stop = value["stop_loss"]
    if stop["method"] == "LAST_SWING":
        stop_text = f"{tf} swing SL"
        if stop["buffer_pips"] is not None and float(stop["buffer_pips"]) != 0:
            stop_text += f" + {_fmt(stop['buffer_pips'])} pip buffer"
    else:
        stop_text = f"SL {_fmt(stop['fixed_distance'])} pips/points"
    parts.append(stop_text)

    tp1 = value["tp1"]
    if tp1["enabled"]:
        protect = "breakeven" if float(tp1["protection_r"]) == 0 else f"+{_fmt(tp1['protection_r'])}R"
        parts.append(
            f"TP1 {_fmt(tp1['target_r'])}R / close {_fmt(tp1['close_percent'])}% / protect {protect}"
        )

    tp2 = value["tp2"]
    if tp2["method"] == "FIXED_R":
        parts.append(f"TP2 {_fmt(tp2['value'])}R")
    elif tp2["method"] == "FIXED_DISTANCE":
        parts.append(f"TP2 {_fmt(tp2['value'])} pips/points")
    else:
        parts.append("TP2 opposite swing")

    risk = value["risk"]
    if risk["method"] == "PERCENT_BALANCE":
        parts.append(f"risk {_fmt(risk['value'])}% balance")
    else:
        parts.append(f"risk ${_fmt(risk['value'])}")

    fundamental_mode = value["fundamentals"]["mode"]
    if fundamental_mode == "REQUIRE_ALIGNMENT":
        parts.append("LIVE fundamentals require alignment")
    else:
        parts.append("LIVE fundamentals block opposite bias")

    return " -> ".join(parts)
