from datetime import datetime, timezone
import math

from fastapi import APIRouter

router = APIRouter()
PERFORMANCE_DATA_PROVIDER = None


def configure_performance_data_provider(provider):
    global PERFORMANCE_DATA_PROVIDER
    PERFORMANCE_DATA_PROVIDER = provider


def _number(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _trade_profit(trade):
    for key in [
        "broker_realized_profit",
        "broker_pnl",
        "profit",
        "pnl",
        "pl",
    ]:
        value = _number((trade or {}).get(key))
        if value is not None:
            return value
    return 0.0


def _trade_timestamp(trade):
    value = (trade or {}).get("closed_at") or (trade or {}).get("opened_at")
    numeric = _number(value)
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric /= 1000
        return numeric
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None


def build_performance_summary(
    closed_trades,
    monthly_trades=None,
    active_trades=None,
    floating_pnl=0,
    now=None,
):
    current = now or datetime.now(timezone.utc)
    month_start = current.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).timestamp()
    trades = [
        trade for trade in (closed_trades or [])
        if isinstance(trade, dict)
    ]
    supplied_month_trades = [
        trade for trade in (monthly_trades or [])
        if isinstance(trade, dict)
    ]
    month_trades = supplied_month_trades or [
        trade for trade in trades
        if (_trade_timestamp(trade) or 0) >= month_start
    ]
    profits = [_trade_profit(trade) for trade in trades]
    wins = sum(value > 0 for value in profits)
    losses = sum(value < 0 for value in profits)
    gross_profit = sum(value for value in profits if value > 0)
    gross_loss = abs(sum(value for value in profits if value < 0))
    weekly_realized = sum(profits)
    floating = _number(floating_pnl) or 0
    symbol_stats = {}

    for symbol in ["EURUSD", "XAUUSD"]:
        symbol_profits = [
            _trade_profit(trade)
            for trade in trades
            if str(trade.get("symbol") or "").upper() == symbol
        ]
        symbol_wins = sum(value > 0 for value in symbol_profits)
        symbol_stats[symbol] = {
            "trades": len(symbol_profits),
            "wins": symbol_wins,
            "losses": sum(value < 0 for value in symbol_profits),
            "pnl": round(sum(symbol_profits), 2),
            "winRate": (
                round(symbol_wins / len(symbol_profits) * 100, 1)
                if symbol_profits else 0
            ),
        }

    equity = []
    cumulative = 0
    for trade in sorted(trades, key=lambda item: _trade_timestamp(item) or 0):
        cumulative += _trade_profit(trade)
        timestamp = _trade_timestamp(trade)
        equity.append({
            "time": timestamp,
            "equity": round(cumulative, 2),
        })

    return {
        "ok": True,
        "source": "ctrader",
        "winRate": round(wins / len(trades) * 100, 1) if trades else 0,
        "totalTrades": len(trades),
        "openTrades": sum(
            1 for trade in (active_trades or []) if isinstance(trade, dict)
        ),
        "wins": wins,
        "losses": losses,
        "weeklyPnl": round(weekly_realized + floating, 2),
        "weeklyRealizedPnl": round(weekly_realized, 2),
        "floatingPnl": round(floating, 2),
        "monthlyPnl": round(
            sum(_trade_profit(trade) for trade in month_trades) + floating,
            2,
        ),
        "eurusd": symbol_stats["EURUSD"],
        "xauusd": symbol_stats["XAUUSD"],
        "bestTrade": round(max(profits), 2) if profits else 0,
        "worstTrade": round(min(profits), 2) if profits else 0,
        "averageRr": None,
        "profitFactor": (
            round(gross_profit / gross_loss, 2)
            if gross_loss > 0
            else None
        ),
        "equityCurve": equity,
        "updatedAt": current.isoformat(),
    }


@router.get("/performance/summary")
def performance_summary():
    if PERFORMANCE_DATA_PROVIDER is None:
        return build_performance_summary([])

    data = PERFORMANCE_DATA_PROVIDER() or {}
    return build_performance_summary(
        data.get("closed_trades"),
        monthly_trades=data.get("monthly_trades"),
        active_trades=data.get("active_trades"),
        floating_pnl=data.get("floating_pnl"),
    )
