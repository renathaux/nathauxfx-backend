import json
from pathlib import Path
from paths import DATA_DIR

TRADE_HISTORY_PATH = DATA_DIR / "trade_history_store.json"


def load_trade_history(path=TRADE_HISTORY_PATH):
    path = Path(path)

    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text())
    except Exception:
        return []

    return data if isinstance(data, list) else []


def save_trade_history(history, path=TRADE_HISTORY_PATH):
    path = Path(path)
    payload = history if isinstance(history, list) else []
    path.write_text(json.dumps(payload, indent=2, default=str))
    print("TRADE_HISTORY_SAVE =", {
        "count": len(payload),
        "path": str(path),
    })
    return payload
