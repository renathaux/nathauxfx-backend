from contextlib import contextmanager
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import strategy_simulator as route


def definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": [],
            "minimum_body_percent": None,
            "minimum_distance_pips": None,
        },
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {
            "method": "FIXED_DISTANCE",
            "buffer_pips": None,
            "fixed_distance": 20,
        },
        "tp1": {
            "enabled": False,
            "target_r": None,
            "close_percent": None,
            "protection_r": None,
        },
        "tp2": {"method": "FIXED_R", "value": 2},
        "risk": {"method": "PERCENT_BALANCE", "value": 1},
    }


def candles():
    return [
        {
            "timestamp": "2026-09-01T00:00:00Z",
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.105,
            "volume": 0,
        },
        {
            "timestamp": "2026-09-01T00:05:00Z",
            "open": 1.105, "high": 1.12, "low": 1.10, "close": 1.115,
            "volume": 0,
        },
    ]


def payload(**overrides):
    base = dict(
        strategy_id="strat_test",
        strategy_name="Test",
        strategy_definition=definition(),
        symbol="EURUSD",
        start="2026-09-01T00:00:00Z",
        end="2026-09-02T00:00:00Z",
        mode="FAST",
        risk_override=None,
        candles_5m=candles(),
    )
    base.update(overrides)
    return route.SimulationRequest(**base)


def test_route_source_has_no_neon_history_or_broker_mutation_calls():
    source = inspect.getsource(route)
    forbidden = [
        "load_market_bundle",
        "IndicatorCandle",
        "get_strategy(",
        "place_market_order",
        "modify_position_sltp",
        "modify_position_stop_loss",
        "close_position",
        "save_durable_auto_trade",
        "activate_strategy(",
        "deactivate_strategy(",
    ]
    for token in forbidden:
        assert token not in source


def test_settings_aggregator_mounts_strategy_simulator_router():
    settings_source = (
        Path(__file__).resolve().parents[1] / "routes" / "settings.py"
    ).read_text(encoding="utf-8")
    assert (
        "from routes.strategy_simulator import router as strategy_simulator_router"
        in settings_source
    )
    assert "router.include_router(strategy_simulator_router)" in settings_source


def test_symbol_must_be_allowed_by_client_strategy(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(symbol="XAUUSD"), SimpleNamespace())
    assert exc.value.status_code == 400
    assert "symbol" in str(exc.value.detail).lower()


def test_route_uses_static_candles_and_positive_balance(monkeypatch):
    calls = {}
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        calls["pinned"] = True
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(
        route, "get_ctrader_account_snapshot", lambda: {"balance": 9895.11}
    )

    def fake_bundle(rows, start, end):
        calls["rows"] = rows
        calls["range"] = (start, end)
        return {"5m": object(), "15m": object(), "1h": object(), "4h": object()}

    monkeypatch.setattr(route, "build_static_market_bundle", fake_bundle)

    def fake_run(
        strategy_definition, bundle, symbol, balance,
        *, risk_override=None, include_replay=False,
        evaluation_start=None, evaluation_end=None,
        continuation=None, finalize_open_trade=True,
    ):
        calls["definition"] = strategy_definition
        calls["balance"] = balance
        calls["replay"] = include_replay
        calls["evaluation_start"] = evaluation_start
        calls["evaluation_end"] = evaluation_end
        calls["continuation"] = continuation
        calls["finalize"] = finalize_open_trade
        return {
            "metrics": {"starting_balance": balance},
            "trades": [],
            "equity_curve": [],
            "replay": [],
        }

    monkeypatch.setattr(route, "run_simulation", fake_run)
    result = route.strategy_simulation_run(
        payload(mode="REPLAY"), SimpleNamespace()
    )

    assert result["ok"] is True
    assert result["history_source"] == "STATIC_REPLAY_JSON"
    assert result["neon_candle_reads"] is False
    assert calls["pinned"] is True
    assert len(calls["rows"]) == 2
    assert calls["balance"] == pytest.approx(9895.11)
    assert calls["replay"] is True
    assert calls["evaluation_start"] == payload().start
    assert calls["evaluation_end"] == payload().end
    assert calls["continuation"] is None
    assert calls["finalize"] is True


def test_missing_static_history_is_rejected_before_simulation(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(
            payload(candles_5m=[]),
            SimpleNamespace(),
        )
    assert exc.value.status_code == 409
    assert "STATIC_SIMULATION_HISTORY_REQUIRED" in str(exc.value.detail)


def test_nonpositive_balance_is_rejected(monkeypatch):
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(route, "get_ctrader_account_snapshot", lambda: {"balance": 0})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(), SimpleNamespace())
    assert exc.value.status_code == 409
    assert "balance" in str(exc.value.detail).lower()


def test_manual_history_database_endpoint_is_retired(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    request = route.ManualHistoryRequest(
        symbol="EURUSD",
        timeframe="5m",
        start="2026-09-01T00:00:00Z",
        end="2026-09-01T01:00:00Z",
    )
    with pytest.raises(HTTPException) as exc:
        route.manual_replay_history(request, SimpleNamespace())
    assert exc.value.status_code == 410
    assert "STATIC_JSON" in str(exc.value.detail)


def test_route_forwards_simulator_continuation(monkeypatch):
    calls = {}
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(
        route, "get_ctrader_account_snapshot",
        lambda: pytest.fail("continuation chunks must reuse virtual balance"),
    )
    monkeypatch.setattr(
        route, "build_static_market_bundle",
        lambda rows, start, end: {"5m": object(), "15m": object(), "1h": object(), "4h": object()},
    )

    def fake_run(*args, **kwargs):
        calls.update(kwargs)
        return {
            "metrics": {"starting_balance": 990.0},
            "trades": [],
            "equity_curve": [],
            "diagnostics": {},
            "continuation": kwargs.get("continuation"),
        }

    monkeypatch.setattr(route, "run_simulation", fake_run)
    continuation = {
        "balance": 990.0,
        "evaluator_status": "WAITING",
        "pending_setup": None,
        "active_trade": None,
        "ordinal": 3,
    }
    result = route.strategy_simulation_run(
        payload(continuation=continuation, finalize=False),
        SimpleNamespace(),
    )

    assert result["ok"] is True
    assert calls["continuation"] == continuation
    assert calls["finalize_open_trade"] is False


def fast_payload(**overrides):
    values = payload().model_dump(exclude={'mode', 'candles_5m', 'continuation', 'finalize_open_trade'})
    values.update(overrides)
    return route.FastJobRequest(**values)


@pytest.mark.parametrize('operation', ['create', 'get', 'cancel'])
def test_fast_job_routes_require_authenticated_actor_before_job_access(monkeypatch, operation):
    calls = []
    def denied(request, mutation=False):
        calls.append(mutation)
        raise HTTPException(status_code=401, detail='AUTHENTICATION_REQUIRED')
    monkeypatch.setattr(route, '_actor', denied)
    monkeypatch.setattr(route, '_fast_jobs', lambda: pytest.fail('must authenticate first'))
    with pytest.raises(HTTPException) as exc:
        if operation == 'create': route.create_fast_job(fast_payload(), SimpleNamespace())
        elif operation == 'get': route.get_fast_job('job', SimpleNamespace())
        else: route.cancel_fast_job('job', SimpleNamespace())
    assert exc.value.status_code == 401
    assert calls == [operation != 'get']


def test_fast_job_captures_read_only_account_balance_and_owner(monkeypatch, tmp_path):
    from services.strategy_fast_jobs import FastJobs
    jobs = FastJobs(tmp_path, start_worker=False)
    actors = []
    def actor(request, mutation=False):
        actors.append(mutation)
        return {'email': 'alice@example.com'}
    @contextmanager
    def pinned(): yield SimpleNamespace(scope='CTRADER:DEMO:123')
    monkeypatch.setattr(route, '_actor', actor)
    monkeypatch.setattr(route, '_fast_jobs', lambda: jobs)
    monkeypatch.setattr(route, 'pinned_account', pinned)
    monkeypatch.setattr(route, 'get_ctrader_account_snapshot', lambda: {'balance': 12345})
    created = route.create_fast_job(fast_payload(), SimpleNamespace())
    import json
    saved = json.loads((tmp_path / created['job_id'] / 'input.json').read_text())
    assert saved['starting_balance'] == 12345
    assert saved['account_scope'] == 'CTRADER:DEMO:123'
    assert actors == [True]
    monkeypatch.setattr(route, '_actor', lambda request, mutation=False: {'email': 'bob@example.com'})
    for operation in (route.get_fast_job, route.cancel_fast_job):
        with pytest.raises(HTTPException) as exc: operation(created['job_id'], SimpleNamespace())
        assert exc.value.status_code == 404


@pytest.mark.parametrize('overrides', [
    {'end': '2036-09-02T00:00:00Z'}, {'end': '2026-08-01T00:00:00Z'},
    {'start': '2026-09-01T00:00:00'}, {'symbol': 'XAUUSD'},
])
def test_fast_job_rejects_invalid_range_and_symbol_before_account_read(monkeypatch, overrides):
    monkeypatch.setattr(route, '_actor', lambda request, mutation=False: {'email': 'alice@example.com'})
    monkeypatch.setattr(route, '_fast_jobs', lambda: pytest.fail('invalid payload'))
    with pytest.raises(HTTPException) as exc: route.create_fast_job(fast_payload(**overrides), SimpleNamespace())
    assert exc.value.status_code == 400
