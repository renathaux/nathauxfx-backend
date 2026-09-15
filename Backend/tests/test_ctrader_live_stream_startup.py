import api
import app_bootstrap
from services.indicator_event_stream_service import IndicatorStreamUnavailable


def test_production_startup_starts_live_price_stream_before_indicator_fence(monkeypatch):
    """A strategy reconciliation fence must not suppress the read-only tick feed."""
    # Import the production entrypoint only for this test so its compatibility
    # installers do not mutate indicator-stream globals during test collection.
    import closed_market_bootstrap

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

    live_handler = (
        closed_market_bootstrap
        ._start_ctrader_live_price_stream_before_indicator_fences
    )
    handlers = api.app.router.on_startup
    assert handlers.index(live_handler) < handlers.index(
        app_bootstrap._start_forex_background_task
    )

    live_handler()
    app_bootstrap._start_forex_background_task()

    assert starts == ["started"]
    assert api.ENGINE_RUNTIME_STATE["indicator_stream_startup"]["ready"] is False
