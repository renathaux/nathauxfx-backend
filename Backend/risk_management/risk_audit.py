import json
import math


def first_valid_live_price(*values):
    for value in values:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_value) and numeric_value > 0:
            return numeric_value
    return None


def build_live_risk_calculation_audit(
    symbol,
    side,
    trade_payload,
    risk_size,
    *,
    normalize_symbol,
    expected_loss_calculator,
    reason=None,
):
    trade_payload = trade_payload or {}
    risk_size = risk_size or {}
    metadata = risk_size.get("symbol_metadata") or {}
    normalized_symbol = normalize_symbol(symbol)

    def to_float(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    account_balance = to_float(risk_size.get("account_balance"))
    account_equity = to_float(risk_size.get("account_equity"))
    account_equity_used = to_float(risk_size.get("account_equity_used"))
    account_value = account_equity_used or account_equity or account_balance
    configured_risk_percent = to_float(risk_size.get("risk_percent"))
    entry = to_float(trade_payload.get("entry"))
    stop_loss = to_float(trade_payload.get("sl"))
    pip_size = to_float(risk_size.get("pip_size") or metadata.get("pip_size"))
    sl_distance_price = None
    if entry is not None and stop_loss is not None:
        sl_distance_price = abs(entry - stop_loss)
    elif risk_size.get("stop_loss_price_distance") is not None:
        sl_distance_price = to_float(risk_size.get("stop_loss_price_distance"))

    sl_distance_pips = to_float(risk_size.get("sl_pips"))
    if sl_distance_pips is None and sl_distance_price is not None and pip_size:
        sl_distance_pips = sl_distance_price / pip_size

    pip_value_per_lot = to_float(
        risk_size.get("pip_value_per_lot") or metadata.get("pip_value_per_lot")
    )
    raw_lot_size = to_float(
        risk_size.get("raw_lots") or risk_size.get("calculated_lots")
    )
    rounded_lot_size = to_float(
        risk_size.get("lot_size") or risk_size.get("rounded_lots")
    )
    expected_loss_usd = expected_loss_calculator(risk_size)
    calculated_broker_risk_percent = None
    if expected_loss_usd is not None and account_value and account_value > 0:
        calculated_broker_risk_percent = (expected_loss_usd / account_value) * 100

    max_allowed_risk_usd = None
    if account_value is not None and configured_risk_percent is not None:
        max_allowed_risk_usd = account_value * (configured_risk_percent / 100)

    maximum_allowed_risk_percent = to_float(
        risk_size.get("maximum_allowed_risk_percent")
        or risk_size.get("allowed_risk_percent")
    )

    suspected_cause = None
    if metadata and metadata.get("metadata_source") != "ctrader":
        suspected_cause = "symbol metadata is not confirmed from cTrader"
    elif risk_size.get("minimum_volume_rounded_up"):
        suspected_cause = "broker minimum volume forced lot size above target risk"
    elif expected_loss_usd is not None and max_allowed_risk_usd is not None and expected_loss_usd > max_allowed_risk_usd:
        suspected_cause = "rounded lot size and SL distance exceed configured risk"
    elif expected_loss_usd is None:
        suspected_cause = "expected loss could not be calculated from broker metadata"

    return {
        "symbol": normalized_symbol,
        "side": str(side or "").upper(),
        "reason": reason,
        "account_balance": round(account_balance, 2) if account_balance is not None else None,
        "account_equity": round(account_equity, 2) if account_equity is not None else None,
        "account_equity_used": round(account_value, 2) if account_value is not None else None,
        "configured_risk_percent": configured_risk_percent,
        "max_allowed_risk_usd": round(max_allowed_risk_usd, 2) if max_allowed_risk_usd is not None else None,
        "entry_price": entry,
        "stop_loss_price": stop_loss,
        "sl_distance_price": round(sl_distance_price, 8) if sl_distance_price is not None else None,
        "sl_distance_pips": round(sl_distance_pips, 4) if sl_distance_pips is not None else None,
        "symbol_pip_size": pip_size,
        "pip_value_per_1_lot": pip_value_per_lot,
        "contract_size": (
            risk_size.get("lot_contract_size")
            or metadata.get("lot_size")
            or metadata.get("lot_contract_size")
        ),
        "raw_lot_size": raw_lot_size,
        "rounded_lot_size": rounded_lot_size,
        "raw_volume_units": risk_size.get("raw_volume_units"),
        "rounded_volume_units": risk_size.get("volume_units"),
        "volume_step_units": risk_size.get("volume_step_units") or metadata.get("volume_step_units"),
        "min_volume_units": risk_size.get("min_volume_units") or metadata.get("min_volume_units"),
        "max_volume_units": risk_size.get("max_volume_units") or metadata.get("max_volume_units"),
        "expected_dollar_loss_at_sl": (
            round(expected_loss_usd, 2)
            if isinstance(expected_loss_usd, (int, float))
            else expected_loss_usd
        ),
        "calculated_broker_risk_percent": (
            round(calculated_broker_risk_percent, 4)
            if calculated_broker_risk_percent is not None
            else None
        ),
        "allowed_broker_risk_percent": maximum_allowed_risk_percent,
        "metadata_source": metadata.get("metadata_source"),
        "metadata_raw": metadata.get("raw"),
        "formula_expected_loss_usd": "sl_distance_pips * pip_value_per_1_lot * rounded_lot_size",
        "formula_broker_risk_percent": "(expected_loss_usd / account_equity_used) * 100",
        "formula_values": {
            "expected_loss_usd": {
                "sl_distance_pips": sl_distance_pips,
                "pip_value_per_1_lot": pip_value_per_lot,
                "rounded_lot_size": rounded_lot_size,
            },
            "broker_risk_percent": {
                "expected_loss_usd": expected_loss_usd,
                "account_equity_used": account_value,
            },
        },
        "suspected_cause": suspected_cause,
    }


def log_live_risk_calculation_audit(*args, **kwargs):
    audit = build_live_risk_calculation_audit(*args, **kwargs)
    print("LIVE_RISK_CALCULATION_AUDIT =", json.dumps(audit, default=str))
    return audit

