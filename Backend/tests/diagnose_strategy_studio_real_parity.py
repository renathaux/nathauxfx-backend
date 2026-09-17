from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.market_facts import build_market_facts
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_parity import v3b_entry_parity_definition

_FIXTURE_PATH = Path(__file__).with_name("test_strategy_studio_real_history_parity_snapshot.py")
_SPEC = importlib.util.spec_from_file_location("real_history_fixture", _FIXTURE_PATH)
_FIXTURE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_FIXTURE)
EURUSD_B64 = _FIXTURE.EURUSD_B64
_decode = _FIXTURE._decode

TARGETS = {
    pd.Timestamp("2026-09-17T04:30:00Z"),
    pd.Timestamp("2026-09-17T06:05:00Z"),
    pd.Timestamp("2026-09-17T12:15:00Z"),
}


def main():
    frame = _decode(EURUSD_B64)
    timeline = build_market_facts({"5m": frame}, "EURUSD", "5m", None)
    definition = v3b_entry_parity_definition("EURUSD")
    state = EvaluationState()
    watch = set(TARGETS)
    watch.update(target + pd.Timedelta(minutes=5) for target in TARGETS)

    print("=== REAL EURUSD PARITY DIAGNOSTIC ===")
    for stamp in timeline.timestamps():
        prior = state
        result = evaluate_strategy(
            definition,
            timeline,
            stamp,
            state,
            symbol="EURUSD",
            account_balance=10000.0,
        )
        state = result.next_state
        if stamp in watch:
            event = timeline.structure_event(stamp)
            print({
                "timestamp": stamp.isoformat(),
                "event": None if event is None else {
                    "type": event.event_type,
                    "direction": event.direction,
                    "broken_level": event.broken_level,
                    "invalidation_price": event.invalidation_price,
                    "trigger_close": event.trigger_close,
                },
                "prior_status": prior.status,
                "prior_pending": prior.pending_setup,
                "signal": result.signal,
                "setup_id": result.setup_id,
                "steps": result.steps,
                "next_status": state.status,
                "next_pending": state.pending_setup,
            })


if __name__ == "__main__":
    main()
