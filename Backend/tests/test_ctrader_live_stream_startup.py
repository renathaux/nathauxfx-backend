from types import SimpleNamespace

from services.ctrader_live_stream_startup import install_ctrader_live_stream_startup


def test_live_stream_handler_is_installed_before_strategy_fence():
    calls = []

    def strategy_startup():
        calls.append("strategy")
        raise RuntimeError("forced indicator fence")

    app = SimpleNamespace(
        router=SimpleNamespace(on_startup=[strategy_startup]),
    )
    api_module = SimpleNamespace(
        start_ctrader_live_price_stream=(
            lambda: calls.append("live_stream")
            or {"ok": True, "status": "started"}
        ),
    )

    handler = install_ctrader_live_stream_startup(
        app,
        api_module,
        strategy_startup,
        lambda: calls.append("restore_account"),
    )

    assert app.router.on_startup == [handler, strategy_startup]
    assert handler() == {"ok": True, "status": "started"}
    assert calls == ["restore_account", "live_stream"]

    try:
        strategy_startup()
    except RuntimeError:
        pass

    assert calls == ["restore_account", "live_stream", "strategy"]


def test_live_stream_startup_installer_is_idempotent():
    def strategy_startup():
        return None

    app = SimpleNamespace(
        router=SimpleNamespace(on_startup=[strategy_startup]),
    )
    api_module = SimpleNamespace(
        start_ctrader_live_price_stream=lambda: {"ok": True},
    )

    first = install_ctrader_live_stream_startup(
        app, api_module, strategy_startup, lambda: None
    )
    second = install_ctrader_live_stream_startup(
        app, api_module, strategy_startup, lambda: None
    )

    assert first is second
    assert app.router.on_startup == [first, strategy_startup]
