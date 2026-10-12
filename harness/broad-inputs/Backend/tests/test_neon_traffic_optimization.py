from datetime import datetime, timezone
from types import SimpleNamespace

import services.auto_trade_state_service as auto_state


class _FakeQuery:
    def __init__(self, rows, counters):
        self.rows = rows
        self.counters = counters

    def filter(self, *args, **kwargs):
        self.counters["filters"] += 1
        return self

    def all(self):
        self.counters["all"] += 1
        return list(self.rows)


class _FakeSession:
    def __init__(self, rows, counters):
        self.rows = rows
        self.counters = counters

    def __enter__(self):
        self.counters["sessions"] += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, model):
        self.counters["queries"] += 1
        return _FakeQuery(self.rows, self.counters)


class _FakeFactory:
    def __init__(self, rows, counters):
        self.rows = rows
        self.counters = counters

    def __call__(self):
        return _FakeSession(self.rows, self.counters)


def _rows(paper="true", live="false"):
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            setting_name=auto_state.PAPER_SETTING,
            setting_value=paper,
            updated_at=now,
            updated_by="test",
        ),
        SimpleNamespace(
            setting_name=auto_state.LIVE_SETTING,
            setting_value=live,
            updated_at=now,
            updated_by="test",
        ),
    ]


def _counters():
    return {"sessions": 0, "queries": 0, "filters": 0, "all": 0}


def test_injected_load_state_reads_both_preferences_in_one_query(monkeypatch):
    counters = _counters()
    monkeypatch.setattr(
        auto_state,
        "persistence_info",
        lambda: {"backend": "test", "durable_across_deployments": True},
    )

    state = auto_state.load_state(
        session_factory=_FakeFactory(_rows(), counters),
        force_refresh=True,
    )

    assert state["paper_enabled"] is True
    assert state["live_enabled"] is False
    assert counters == {"sessions": 1, "queries": 1, "filters": 1, "all": 1}


def test_default_load_state_collapses_burst_reads_into_process_cache(monkeypatch):
    counters = _counters()
    factory = _FakeFactory(_rows(paper="true", live="true"), counters)
    monkeypatch.setattr(auto_state, "SessionLocal", factory)
    monkeypatch.setattr(auto_state, "_cache_ttl_seconds", lambda: 5.0)
    monkeypatch.setattr(
        auto_state,
        "persistence_info",
        lambda: {"backend": "test", "durable_across_deployments": True},
    )
    auto_state.clear_state_cache()

    first = auto_state.load_state()
    second = auto_state.load_state()
    third = auto_state.load_state()

    assert first == second == third
    assert first["live_enabled"] is True
    assert counters["sessions"] == 1
    assert counters["queries"] == 1


def test_force_refresh_bypasses_cached_state(monkeypatch):
    counters = _counters()
    rows = _rows(paper="true", live="false")
    factory = _FakeFactory(rows, counters)
    monkeypatch.setattr(auto_state, "SessionLocal", factory)
    monkeypatch.setattr(auto_state, "_cache_ttl_seconds", lambda: 5.0)
    monkeypatch.setattr(
        auto_state,
        "persistence_info",
        lambda: {"backend": "test", "durable_across_deployments": True},
    )
    auto_state.clear_state_cache()

    first = auto_state.load_state()
    rows[1].setting_value = "true"
    cached = auto_state.load_state()
    refreshed = auto_state.load_state(force_refresh=True)

    assert first["live_enabled"] is False
    assert cached["live_enabled"] is False
    assert refreshed["live_enabled"] is True
    assert counters["sessions"] == 2
    assert counters["queries"] == 2
