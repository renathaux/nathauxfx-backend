from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from services.monthly_history_window import (
    MARKET_TIMEZONE,
    OPEN_STATUSES,
    calendar_month_start_ts,
    filter_month_history,
    install_monthly_history_window,
    trade_is_current_month,
)
from services import paper_live_entry_service
from services import live_v3b_service


def ts(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_strategy_identities_are_unchanged():
    assert paper_live_entry_service.PAPER_ENTRY_MODEL == "PAPER_LIVE_INDICATOR_EVENT_V1"
    assert live_v3b_service.LIVE_V3B_MODEL == "LIVE_V3B_M5_FROZEN"


def test_calendar_month_boundary_uses_trading_timezone():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=MARKET_TIMEZONE)
    start = datetime.fromtimestamp(calendar_month_start_ts(now), MARKET_TIMEZONE)
    assert start == datetime(2026, 10, 1, 0, 0, tzinfo=MARKET_TIMEZONE)


def test_current_month_closed_kept_previous_month_excluded_and_open_survives():
    month_start = ts(2026, 10, 1, 0)
    current = {"trade_id": "current", "closed_at": ts(2026, 10, 4), "result": "WIN", "status": "CLOSED"}
    previous = {"trade_id": "previous", "closed_at": ts(2026, 9, 30), "result": "LOSS", "status": "CLOSED"}
    open_old = {"trade_id": "open-old", "opened_at": ts(2026, 9, 28), "result": "RUNNING", "status": "OPEN"}
    kept = filter_month_history(
        [current, previous, open_old],
        month_start_ts=month_start,
        open_match_keys={"open-old"},
        match_key_builder=lambda trade: trade.get("trade_id"),
    )
    assert current in kept
    assert previous not in kept
    assert open_old in kept
    assert trade_is_current_month(current, month_start)
    assert not trade_is_current_month(previous, month_start)


def _fake_runtime():
    closed_this_month = [
        {"trade_id": "live-win", "closed_at": ts(2026, 10, 5), "status": "WIN", "profit": 125.0, "source": "broker"},
        {"trade_id": "live-loss", "closed_at": ts(2026, 10, 8), "status": "LOSS", "profit": -50.0, "source": "broker"},
    ]
    close_calls = []

    api = SimpleNamespace()
    api._MONTHLY_HISTORY_WINDOW_INSTALLED = False
    api.get_live_week_start_ts = lambda now=None: ts(2026, 10, 11, 17)
    api.get_live_month_start_ts = lambda now=None: ts(2026, 10, 1, 0)
    api.LAST_LIVE_RESET = ts(2026, 10, 4, 17)
    api.LIVE_LAST_EXECUTION_TIME = {"EURUSD": ts(2026, 10, 1)}
    api.LIVE_ACTIVE_ORDERS = {
        "EURUSD": {"trade_id": "running", "position_id": "broker-pos-1", "status": "OPEN", "profit": 20.0}
    }
    api.LIVE_AUTO_STATUS_BY_SYMBOL = {
        "EURUSD": {"status": "WAIT"},
        "XAUUSD": {"status": "WAIT"},
    }
    api.AUTO_TRADE_LAST_STATUS = {}
    api.LIVE_TRADE_HISTORY = [
        {"trade_id": "old", "closed_at": ts(2026, 9, 20), "status": "LOSS", "profit": -10.0},
        {"trade_id": "running", "opened_at": ts(2026, 9, 28), "status": "OPEN", "profit": 20.0},
    ]
    api.MAX_LIVE_TRADE_HISTORY = 50
    api.LIVE_BROKER_CLOSED_HISTORY = []
    api.LIVE_BROKER_HISTORY_CACHE = {"history": [], "updated_at": 0}
    api.save_live_backup = lambda: None
    api.get_live_trade_match_key = lambda trade: trade.get("trade_id")
    api.enrich_broker_closed_trade_levels = lambda trade: dict(trade)
    api.get_closed_deals_for_current_month = lambda max_rows=500: list(closed_this_month)
    api.get_live_trade_status = lambda trade: str(trade.get("status") or trade.get("result") or "").upper()
    api.extract_broker_trade_pl = lambda trade: (float(trade.get("profit") or 0), "test")
    api.get_stored_live_floating_pl = lambda trade: float(trade.get("profit") or 0)
    api.configure_performance_data_provider = lambda provider: setattr(api, "performance_provider", provider)
    api.close_position = lambda *args, **kwargs: close_calls.append((args, kwargs))

    paper = SimpleNamespace()
    paper._MONTHLY_HISTORY_WINDOW_INSTALLED = False
    paper.PAPER_TRADE_HISTORY = [
        {"trade_id": "paper-win", "closed_at": ts(2026, 10, 3), "status": "CLOSED", "result": "WIN", "profit": 80.0},
        {"trade_id": "paper-old", "closed_at": ts(2026, 9, 25), "status": "CLOSED", "result": "LOSS", "profit": -40.0},
        {"trade_id": "paper-open", "opened_at": ts(2026, 9, 29), "status": "OPEN", "result": "RUNNING"},
    ]
    paper.PAPER_ACTIVE_TRADES = [{"trade_id": "paper-open", "status": "OPEN"}]
    paper.LAST_PAPER_RESET = 0
    paper.save_paper_backup = lambda: None
    return api, paper, close_calls


def test_install_keeps_histories_separate_and_monthly_without_broker_mutation(monkeypatch):
    api, paper, close_calls = _fake_runtime()
    monkeypatch.setattr("services.monthly_history_window.calendar_month_start_ts", lambda now=None: ts(2026, 10, 1, 0))

    result = install_monthly_history_window(api, paper)
    assert result["paper_strategy"] == "V1"
    assert result["live_strategy"] == "V3B"
    assert result["weekly_risk_window_changed"] is False

    paper.run_monthly_paper_reset(force=True)
    assert {t["trade_id"] for t in paper.PAPER_TRADE_HISTORY} == {"paper-win", "paper-open"}

    api.run_monthly_live_history_reset(force=True)
    assert {t["trade_id"] for t in api.LIVE_TRADE_HISTORY} == {"running"}
    stats = api.calculate_live_trade_stats()
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["running"] == 1
    assert stats["total"] == 3
    assert stats["win_rate"] == 50.0
    assert stats["strategy_identity"] == "LIVE — V3B"

    assert close_calls == []
    assert api.LIVE_ACTIVE_ORDERS["EURUSD"]["position_id"] == "broker-pos-1"
    assert paper.PAPER_TRADE_HISTORY is not api.LIVE_TRADE_HISTORY


def test_weekly_execution_marker_remains_weekly_while_history_is_monthly(monkeypatch):
    api, paper, _ = _fake_runtime()
    monkeypatch.setattr("services.monthly_history_window.calendar_month_start_ts", lambda now=None: ts(2026, 10, 1, 0))
    install_monthly_history_window(api, paper)
    api.run_weekly_live_reset(force=True)
    assert api.LAST_LIVE_RESET == ts(2026, 10, 11, 17)
    assert api.get_live_week_start_ts() == ts(2026, 10, 11, 17)
    assert api.get_live_month_start_ts() == ts(2026, 10, 1, 0)
