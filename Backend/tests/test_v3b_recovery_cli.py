import json

import pandas as pd

from scripts import v3b_5m_stream_recovery as cli


def test_recovery_cli_accepts_15m_dry_run(monkeypatch, capsys):
    frame = pd.DataFrame(
        {
            "Open": [1.1, 1.2],
            "High": [1.2, 1.3],
            "Low": [1.0, 1.1],
            "Close": [1.15, 1.25],
        },
        index=pd.DatetimeIndex(pd.to_datetime([
            "2026-09-10T09:30:00Z",
            "2026-09-10T09:45:00Z",
        ], utc=True)),
    )
    monkeypatch.setattr(
        cli,
        "_load_state",
        lambda storage_key, timeframe: (
            pd.Timestamp("2026-09-10T09:30:00Z"),
            pd.Timestamp("2026-09-14T21:00:00Z"),
        ),
    )
    monkeypatch.setattr(
        cli,
        "fetch_ctrader_historical_candles",
        lambda symbol, timeframe, start, end: frame,
    )
    monkeypatch.setattr(
        cli.recovery,
        "plan_recovery",
        lambda request, closed: {
            "safe": True,
            "timeframe": request.timeframe,
            "dry_run": request.dry_run,
        },
    )

    cli.main([
        "--account-id", "47810571",
        "--symbol", "EURUSD",
        "--timeframe", "15m",
        "--storage-key", "EURUSD~09C2948873",
        "--dry-run",
        "--lookback-candles", "4",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"dry_run": True, "safe": True, "timeframe": "15m"}
