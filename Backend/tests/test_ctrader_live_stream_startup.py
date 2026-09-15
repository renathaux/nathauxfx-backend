import api
import app_bootstrap
from services.indicator_event_stream_service import IndicatorStreamUnavailable


def test_live_price_stream_starts_even_if_indicator_stream_startup_fails(monkeypatch):
    starts = []

    monkeypatch.setattr(
        app_bootstrap,
        "_restore_ctrader_selection_before_market_data",
        lambda: {"ok": True, "restored": False},
    )
    monkeypatch.setattr(app_bootstrap, "verify_execution_protocol", lambda: True)
    monkeypatch.setattr(
        app_bootstrap,
        "reconcile_incomplete_submissions",
        lambda **_kwargs: {"ok": True, "reconciled": 0},
    )
    monkeypatch.setattr(
        api,
        "start_ctrader_live_price_stream",
        lambda: starts.append("started") or {"ok": True, "status": "started"},
    )

    def fail_indicator_startup(*_args, **_kwargs):
        raise IndicatorStreamUnavailable("forced reconciliation fence")

    monkeypatch.setattr(api, "get_ctrader_market_data", fail_indicator_startup)

    app_bootstrap._start_forex_background_task()

    assert starts == ["started"]
    assert api.ENGINE_RUNTIME_STATE["indicator_stream_startup"]["ready"] is False
