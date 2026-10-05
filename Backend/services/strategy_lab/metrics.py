from __future__ import annotations


def summarize_r(trades):
    resolved = [trade for trade in trades if trade.get("r_result") is not None]
    values = [float(trade["r_result"]) for trade in resolved]
    equity = peak = max_drawdown = 0.0
    consecutive = maximum_consecutive = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if value < 0:
            consecutive += 1
            maximum_consecutive = max(maximum_consecutive, consecutive)
        else:
            consecutive = 0
    wins = sum(value > 0 for value in values)
    return {
        "win_rate": round((wins / len(resolved) * 100.0), 2) if resolved else 0.0,
        "total_r": round(sum(values), 4),
        "total_r_before_rounding": sum(values),
        "average_r_per_trade": round(sum(values) / len(resolved), 4) if resolved else 0.0,
        "max_consecutive_losses": maximum_consecutive,
        "max_drawdown_r": round(max_drawdown, 4),
    }
