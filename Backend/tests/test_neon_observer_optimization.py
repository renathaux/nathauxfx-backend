from copy import deepcopy

from services import neon_observer_optimization
from services import strategy_diagnostics_service as diagnostics


def _snapshot(decision="WAIT", setup_identifier=None):
    return {
        "symbol": "EURUSD",
        "state_controls": {
            "overall_strategy_state": {"plan_type": "WAIT"},
            "signal_direction": decision,
            "stage_states": {},
            "news": {},
            "execution": {"eligible": False},
            "active_position": None,
            "setup_identifier": setup_identifier,
        },
        "trend": {"classification": "NEUTRAL"},
        "bos": {},
        "m5_confirmation": {},
        "noise_consolidation": {},
        "trade_plan": {},
        "final_decision": {
            "decision": decision,
            "reason": "WAITING",
            "prevented_by": {},
        },
    }


def test_unchanged_lifecycle_observer_collapses_duplicate_db_calls(monkeypatch):
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_INSTALLED is True
    diagnostics._reset_neon_lifecycle_throttle_for_tests()
    calls = []

    def fake_original(snapshot, source_state=None):
        calls.append((deepcopy(snapshot), deepcopy(source_state)))
        return {"ok": True, "sequence": len(calls)}

    monkeypatch.setattr(
        diagnostics,
        "_NEON_LIFECYCLE_THROTTLE_ORIGINAL",
        fake_original,
    )
    monkeypatch.setattr(
        neon_observer_optimization,
        "_configured_heartbeat_seconds",
        lambda environment=None: 300,
    )

    first = diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot(),
        {"account_scope": "TEST"},
    )
    second = diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot(),
        {"account_scope": "TEST"},
    )

    assert first == second
    assert len(calls) == 1
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_STATS["db_calls"] == 1
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_STATS["skipped_unchanged"] == 1


def test_meaningful_state_change_persists_immediately(monkeypatch):
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_INSTALLED is True
    diagnostics._reset_neon_lifecycle_throttle_for_tests()
    calls = []

    def fake_original(snapshot, source_state=None):
        calls.append((deepcopy(snapshot), deepcopy(source_state)))
        return {"ok": True, "sequence": len(calls)}

    monkeypatch.setattr(
        diagnostics,
        "_NEON_LIFECYCLE_THROTTLE_ORIGINAL",
        fake_original,
    )
    monkeypatch.setattr(
        neon_observer_optimization,
        "_configured_heartbeat_seconds",
        lambda environment=None: 300,
    )

    diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot("WAIT"),
        {"account_scope": "TEST"},
    )
    diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot("BUY_READY", setup_identifier="setup-2"),
        {"account_scope": "TEST"},
    )

    assert len(calls) == 2
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_STATS["state_change_calls"] == 2


def test_failed_observer_write_is_not_cached(monkeypatch):
    assert diagnostics._NEON_LIFECYCLE_THROTTLE_INSTALLED is True
    diagnostics._reset_neon_lifecycle_throttle_for_tests()
    calls = []

    def fake_original(snapshot, source_state=None):
        calls.append(1)
        if len(calls) == 1:
            return None
        return {"ok": True}

    monkeypatch.setattr(
        diagnostics,
        "_NEON_LIFECYCLE_THROTTLE_ORIGINAL",
        fake_original,
    )

    assert diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot(), {"account_scope": "TEST"}
    ) is None
    assert diagnostics.persist_lifecycle_evaluation_safely(
        _snapshot(), {"account_scope": "TEST"}
    ) == {"ok": True}
    assert len(calls) == 2
