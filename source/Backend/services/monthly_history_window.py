from __future__ import annotations

import builtins
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("America/New_York")
OPEN_STATUSES = {"RUNNING", "OPEN", "TP1 HIT", "CLOSING", "TP2 HIT"}


def calendar_month_start_ts(now=None):
    current = now or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def legacy_week_start_ts(now=None):
    current = now or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    reset = current.replace(hour=17, minute=0, second=0, microsecond=0)
    days_since_sunday = (current.weekday() - 6) % 7
    reset -= timedelta(days=days_since_sunday)
    if current.weekday() == 6 and current < reset:
        reset -= timedelta(days=7)
    return reset.timestamp()


def _timestamp_from_trade(trade):
    if not isinstance(trade, dict):
        return None
    for key in ("closed_at", "opened_at", "time", "timestamp"):
        value = trade.get(key)
        if value in (None, ""):
            continue
        try:
            ts = float(value)
            return ts / 1000 if ts > 10_000_000_000 else ts
        except (TypeError, ValueError):
            text = str(value).strip()
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass
            # PAPER V1 stores opened_at/closed_at in this legacy form.
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                continue
    return None


def trade_is_current_month(trade, month_start_ts=None):
    ts = _timestamp_from_trade(trade)
    if ts is None:
        return False
    return ts >= (calendar_month_start_ts() if month_start_ts is None else month_start_ts)


def filter_month_history(history, month_start_ts=None, open_match_keys=None, match_key_builder=None):
    start = calendar_month_start_ts() if month_start_ts is None else float(month_start_ts)
    open_match_keys = {str(v) for v in (open_match_keys or set()) if v not in (None, "")}
    kept = []
    for trade in history or []:
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or trade.get("result") or "").upper()
        match_key = None
        if match_key_builder is not None:
            try:
                match_key = match_key_builder(trade)
            except Exception:
                match_key = None
        if (match_key is not None and str(match_key) in open_match_keys) or status in OPEN_STATUSES:
            kept.append(trade)
            continue
        if trade_is_current_month(trade, start):
            kept.append(trade)
    return kept


def guarded_import_api(module_name="api"):
    """Import api without letting the legacy import-time weekly history prune run.

    The legacy loader calls its weekly reset while importing. We only mask the
    persisted reset marker in-memory for that one read; the backup file itself is
    not changed. After import, the runtime is patched to use a calendar-month
    history window while the original weekly execution housekeeping remains weekly.
    """
    import importlib
    if module_name in __import__("sys").modules:
        return __import__("sys").modules[module_name]

    real_open = builtins.open
    week_marker = legacy_week_start_ts()

    def guarded_open(file, mode="r", *args, **kwargs):
        path = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else ""
        if "r" in mode and "+" not in mode and str(path).endswith("live_backup.json"):
            try:
                with real_open(file, mode, *args, **kwargs) as handle:
                    data = json.load(handle)
                data["last_live_reset"] = max(float(data.get("last_live_reset") or 0), week_marker)
                return io.StringIO(json.dumps(data))
            except Exception:
                pass
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    try:
        return importlib.import_module(module_name)
    finally:
        builtins.open = real_open


def install_monthly_history_window(api_module, paper_shared_module):
    if getattr(api_module, "_MONTHLY_HISTORY_WINDOW_INSTALLED", False):
        return {"ok": True, "installed": False}

    original_week_start = api_module.get_live_week_start_ts
    original_save_live_backup = api_module.save_live_backup

    def run_monthly_live_history_reset(force=False):
        month_start = api_module.get_live_month_start_ts()
        active_ids = {
            str(api_module.get_live_trade_match_key(trade))
            for trade in api_module.LIVE_ACTIVE_ORDERS.values()
            if trade and api_module.get_live_trade_match_key(trade)
        }
        before = len(api_module.LIVE_TRADE_HISTORY)
        kept = filter_month_history(
            api_module.LIVE_TRADE_HISTORY,
            month_start_ts=month_start,
            open_match_keys=active_ids,
            match_key_builder=api_module.get_live_trade_match_key,
        )
        api_module.LIVE_TRADE_HISTORY[:] = kept[: api_module.MAX_LIVE_TRADE_HISTORY]
        changed = before != len(api_module.LIVE_TRADE_HISTORY)
        if changed or force:
            api_module.LIVE_BROKER_CLOSED_HISTORY.clear()
            api_module.LIVE_BROKER_HISTORY_CACHE["history"] = []
            api_module.LIVE_BROKER_HISTORY_CACHE["updated_at"] = 0
            original_save_live_backup()
            print("LIVE MONTHLY HISTORY RESET:", {
                "month_start": datetime.fromtimestamp(month_start, MARKET_TIMEZONE).isoformat(),
                "removed_closed_trades": before - len(api_module.LIVE_TRADE_HISTORY),
                "kept_trades": len(api_module.LIVE_TRADE_HISTORY),
                "broker_positions_modified": False,
            })
        return changed

    def run_weekly_execution_housekeeping_and_monthly_history(force=False):
        """Preserve the old weekly execution housekeeping; history itself is monthly."""
        week_ts = original_week_start()
        if force or week_ts > float(api_module.LAST_LIVE_RESET or 0):
            for symbol, timestamp in list(api_module.LIVE_LAST_EXECUTION_TIME.items()):
                try:
                    if float(timestamp or 0) < week_ts:
                        api_module.LIVE_LAST_EXECUTION_TIME[symbol] = 0
                except (TypeError, ValueError):
                    api_module.LIVE_LAST_EXECUTION_TIME[symbol] = 0

            for symbol in api_module.LIVE_AUTO_STATUS_BY_SYMBOL:
                if api_module.LIVE_ACTIVE_ORDERS.get(symbol):
                    continue
                api_module.LIVE_AUTO_STATUS_BY_SYMBOL[symbol] = {
                    **api_module.LIVE_AUTO_STATUS_BY_SYMBOL[symbol],
                    "signal": None,
                    "action": None,
                    "status": "WAIT",
                    "reason": "Waiting for BUY/SELL signal",
                    "checked_at": time.time(),
                    "active_trade": None,
                }
            if not any(api_module.LIVE_ACTIVE_ORDERS.values()):
                api_module.AUTO_TRADE_LAST_STATUS.update({
                    "symbol": None,
                    "signal": None,
                    "action": None,
                    "status": "WAIT",
                    "reason": "Waiting for BUY/SELL signal",
                    "timestamp": time.time(),
                })
            api_module.LAST_LIVE_RESET = week_ts
            original_save_live_backup()
            print("LIVE WEEKLY EXECUTION HOUSEKEEPING:", {
                "reset_time": datetime.fromtimestamp(week_ts, MARKET_TIMEZONE).isoformat(),
                "history_pruned": False,
            })
        return run_monthly_live_history_reset(force=force)

    def get_live_broker_closed_history_monthly(force=False):
        run_weekly_execution_housekeeping_and_monthly_history()
        now = time.time()
        if (
            not force
            and api_module.LIVE_BROKER_HISTORY_CACHE.get("history")
            and now - api_module.LIVE_BROKER_HISTORY_CACHE.get("updated_at", 0) < 20
        ):
            return [
                api_module.enrich_broker_closed_trade_levels(trade)
                for trade in api_module.LIVE_BROKER_HISTORY_CACHE.get("history") or []
            ]
        try:
            broker_history = [
                api_module.enrich_broker_closed_trade_levels(trade)
                for trade in api_module.get_closed_deals_for_current_month(max_rows=500)
            ]
        except Exception as exc:
            print("LIVE_BROKER_MONTH_HISTORY_SYNC_ERROR:", exc)
            broker_history = []
        api_module.LIVE_BROKER_CLOSED_HISTORY[:] = broker_history[: api_module.MAX_LIVE_TRADE_HISTORY]
        api_module.LIVE_BROKER_HISTORY_CACHE["history"] = list(api_module.LIVE_BROKER_CLOSED_HISTORY)
        api_module.LIVE_BROKER_HISTORY_CACHE["updated_at"] = now
        return list(api_module.LIVE_BROKER_CLOSED_HISTORY)

    def calculate_live_trade_stats_monthly():
        run_weekly_execution_housekeeping_and_monthly_history()
        month_history = get_live_broker_closed_history_monthly()
        history_source = month_history if month_history else list(api_module.LIVE_TRADE_HISTORY)
        active_ids = {
            str(api_module.get_live_trade_match_key(trade))
            for trade in api_module.LIVE_ACTIVE_ORDERS.values()
            if trade and api_module.get_live_trade_match_key(trade)
        }
        wins = losses = closed = 0
        realized = 0.0
        seen = set()
        for trade in history_source:
            if not trade_is_current_month(trade):
                continue
            key = api_module.get_live_trade_match_key(trade) or id(trade)
            if str(key) in active_ids or str(key) in seen:
                continue
            seen.add(str(key))
            status = api_module.get_live_trade_status(trade)
            if status in OPEN_STATUSES:
                continue
            broker_pl, _ = api_module.extract_broker_trade_pl(trade)
            pl = float(broker_pl or 0)
            realized += pl
            closed += 1
            if status in {"WIN", "WON", "PROFIT"} or pl > 0:
                wins += 1
            elif status in {"LOSS", "LOST"} or pl < 0:
                losses += 1
        running = sum(
            1 for trade in api_module.LIVE_ACTIVE_ORDERS.values()
            if trade and api_module.get_live_trade_status(trade) in OPEN_STATUSES
        )
        floating = sum(
            api_module.get_stored_live_floating_pl(trade)
            for trade in api_module.LIVE_ACTIVE_ORDERS.values()
            if trade and api_module.get_live_trade_status(trade) in OPEN_STATUSES
        )
        total = closed + running
        return {
            "window": "calendar_month",
            "strategy_identity": "LIVE — V3B",
            "wins": wins,
            "losses": losses,
            "running": running,
            "closed": closed,
            "total": total,
            "total_today": total,
            "win_rate": round((wins / (wins + losses)) * 100, 2) if wins + losses else 0,
            "realized_pl": round(realized, 2),
            "total_pl": round(realized + floating, 2),
            "total_pnl": round(realized + floating, 2),
        }

    def get_performance_data_monthly():
        closed_trades = get_live_broker_closed_history_monthly(force=False)
        active_trades = [
            trade for trade in api_module.LIVE_ACTIVE_ORDERS.values()
            if isinstance(trade, dict) and api_module.get_live_trade_status(trade) in OPEN_STATUSES
        ]
        return {
            "closed_trades": closed_trades,
            "monthly_trades": list(closed_trades),
            "active_trades": active_trades,
            "floating_pnl": sum(api_module.get_stored_live_floating_pl(t) for t in active_trades),
            "history_window": "calendar_month",
            "strategy_identity": "LIVE — V3B",
        }

    # PAPER V1: only its history/stat window changes. Entry generation is untouched.
    def run_monthly_paper_reset(force=False):
        month_start = calendar_month_start_ts()
        before = len(paper_shared_module.PAPER_TRADE_HISTORY)
        active_keys = set()
        for trade in getattr(paper_shared_module, "PAPER_ACTIVE_TRADES", []) or []:
            if isinstance(trade, dict):
                key = trade.get("trade_id") or trade.get("signal_key") or trade.get("setup_lock_key")
                if key:
                    active_keys.add(str(key))

        def paper_key(trade):
            return trade.get("trade_id") or trade.get("signal_key") or trade.get("setup_lock_key")

        paper_shared_module.PAPER_TRADE_HISTORY = filter_month_history(
            paper_shared_module.PAPER_TRADE_HISTORY,
            month_start_ts=month_start,
            open_match_keys=active_keys,
            match_key_builder=paper_key,
        )
        paper_shared_module.LAST_PAPER_RESET = month_start
        changed = before != len(paper_shared_module.PAPER_TRADE_HISTORY)
        if changed or force:
            paper_shared_module.save_paper_backup()
            print("PAPER MONTHLY RESET:", {
                "month_start": datetime.fromtimestamp(month_start, MARKET_TIMEZONE).isoformat(),
                "removed_closed_trades": before - len(paper_shared_module.PAPER_TRADE_HISTORY),
                "kept_trades": len(paper_shared_module.PAPER_TRADE_HISTORY),
                "strategy_identity": "PAPER — V1",
            })
        return changed

    api_module.run_monthly_live_history_reset = run_monthly_live_history_reset
    api_module.run_weekly_live_reset = run_weekly_execution_housekeeping_and_monthly_history
    api_module.get_live_broker_closed_history = get_live_broker_closed_history_monthly
    api_module.calculate_live_trade_stats = calculate_live_trade_stats_monthly
    api_module.get_performance_data = get_performance_data_monthly
    api_module.configure_performance_data_provider(get_performance_data_monthly)

    paper_shared_module.run_monthly_paper_reset = run_monthly_paper_reset
    paper_shared_module.run_weekly_paper_reset = run_monthly_paper_reset

    api_module._MONTHLY_HISTORY_WINDOW_INSTALLED = True
    paper_shared_module._MONTHLY_HISTORY_WINDOW_INSTALLED = True
    return {
        "ok": True,
        "installed": True,
        "paper_strategy": "V1",
        "live_strategy": "V3B",
        "history_window": "calendar_month",
        "weekly_risk_window_changed": False,
    }
