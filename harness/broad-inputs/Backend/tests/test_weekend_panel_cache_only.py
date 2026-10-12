from datetime import datetime, timezone
import inspect

import api


def test_panel_data_has_server_side_weekend_cache_guard():
    source = inspect.getsource(api.panel_data)
    assert "weekend_idle = forex_weekend_closed()" in source
    assert "refresh=not weekend_idle" in source
    assert '"weekend_cache_only"' in source
    assert "not weekend_idle" in source


def test_weekend_force_request_does_not_refresh_panel(monkeypatch):
    monkeypatch.setattr(api, "forex_weekend_closed", lambda: True)
    monkeypatch.setattr(
        api,
        "refresh_panel_cache_direct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("weekend force must not refresh panel")
        ),
    )
    monkeypatch.setattr(
        api,
        "schedule_panel_cache_refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("weekend stale cache must not schedule refresh")
        ),
    )
    monkeypatch.setattr(api, "get_live_prices", lambda: {})
    monkeypatch.setattr(
        api,
        "auto_trade_state_response",
        lambda refresh=True: {
            "paper_enabled": False,
            "live_enabled": False,
            "live_execution_active": False,
            "live_execution_paused": False,
            "pause_reason": None,
        } if refresh is False else (_ for _ in ()).throw(
            AssertionError("weekend panel must not refresh durable auto state")
        ),
    )

    # Existing cache is sufficient for the route; this test is specifically
    # asserting that force=1 cannot trigger the expensive refresh path.
    api.PANEL_CACHE["data"] = api.default_panel()
    api.PANEL_CACHE["last_update"] = 0
    result = api.panel_data(force=1)
    assert isinstance(result, dict)
    assert result.get("_meta", {}).get("weekend_idle") is True
    assert result.get("_meta", {}).get("source") == "weekend_cache_only"
