"""Pure setup fingerprint shared with production."""
import hashlib
import copy
import json
import pandas as pd
def _pending_identity(prior_state, timeline, timestamp):
    pending = None
    if prior_state is not None:
        pending = copy.deepcopy(getattr(prior_state, "pending_setup", None))
    if pending:
        return {
            "structure_event_time": pending.get("event_timestamp"),
            "broken_level": pending.get("broken_level"),
        }

    try:
        event = timeline.structure_event(timestamp)
    except Exception:
        event = None
    if event is None:
        return {"structure_event_time": None, "broken_level": None}
    return {
        "structure_event_time": pd.Timestamp(event.timestamp).isoformat(),
        "broken_level": float(event.broken_level),
    }


def _setup_id(*, owner_id, strategy_id, schema_version, account_scope, symbol,
              direction, structure_event_time, entry_trigger_time, broken_level,
              evaluator_setup_id, generation_bindings=None, strategy_identity=None) -> str:
    identity = {
        "owner_id": str(owner_id),
        "strategy_id": str(strategy_id),
        "schema_version": int(schema_version),
        "account_scope": str(account_scope),
        "symbol": str(symbol),
        "direction": str(direction),
        "structure_event_time": structure_event_time,
        "entry_trigger_time": entry_trigger_time,
        "broken_level": broken_level,
        "evaluator_setup_id": evaluator_setup_id,
        "strategy_identity": strategy_identity,
    }
    if generation_bindings:
        identity["stream_generations"] = sorted(generation_bindings, key=lambda b: (b["root_key"], b["timeframe"], b["generation"]))
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return "sts1_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
