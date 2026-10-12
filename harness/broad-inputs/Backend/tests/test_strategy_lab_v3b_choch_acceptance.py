import pandas as pd

from services.paper_v3b_bridge import build_paper_v3b_candidate
from services.strategy_lab import v3b_m5_frozen_candidate as eur_v3b
from services.strategy_lab import v3b_xauusd_frozen_candidate as gold_v3b


class _Strict:
    @staticmethod
    def closed_frame(frame, minutes):
        assert minutes == 5
        return frame.copy()

    @staticmethod
    def point_size(symbol):
        return 0.01 if symbol == "XAUUSD" else 0.00001


def _authority(event):
    def reader(frame, symbol, timeframe, point_size):
        assert timeframe == "5m"
        assert symbol == event["symbol"]
        return {"events": [event], "source": "test_authority"}

    return reader


def test_eurusd_v3b_accepts_fresh_5m_choch_with_same_confirmation_rules():
    index = pd.to_datetime([
        "2026-09-14T14:35:00Z",
        "2026-09-14T14:40:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [1.15363, 1.15388],
            "High": [1.15395, 1.15422],
            "Low": [1.15361, 1.15382],
            "Close": [1.15389, 1.15420],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_eur_choch",
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "CHOCH",
        "direction": "BULLISH",
        "broken_level": 1.15374,
        "broken_swing_timestamp": "2026-09-14T13:30:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.15231},
        "event_identity": {"stable": "eur-choch"},
        "tradable": True,
    }

    result = build_paper_v3b_candidate(
        "EURUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["paper_entry_ready"] is True
    assert result["signal"] == "BUY"
    assert result["source_structure_event_type"] == "CHOCH"
    assert result["five_m_swing_break"]["event_type"] == "CHOCH"
    assert "CHOCH" in result["signal_text"]
    assert result["risk_reward_ratio"] == 1.90


def test_xauusd_v3b_accepts_fresh_5m_choch_with_same_confirmation_rules():
    index = pd.to_datetime([
        "2026-09-14T16:15:00Z",
        "2026-09-14T16:20:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [4294.53, 4298.69],
            "High": [4299.19, 4305.10],
            "Low": [4294.42, 4298.40],
            "Close": [4298.74, 4304.44],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_gold_choch",
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "CHOCH",
        "direction": "BULLISH",
        "broken_level": 4297.38,
        "broken_swing_timestamp": "2026-09-14T12:15:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 4286.26},
        "event_identity": {"stable": "gold-choch"},
        "tradable": True,
    }

    result = build_paper_v3b_candidate(
        "XAUUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["paper_entry_ready"] is True
    assert result["signal"] == "BUY"
    assert result["source_structure_event_type"] == "CHOCH"
    assert result["five_m_swing_break"]["event_type"] == "CHOCH"
    assert "CHOCH" in result["signal_text"]
    assert result["risk_reward_ratio"] == 1.90


def _candidate_frame():
    index = pd.to_datetime([
        "2026-09-14T14:35:00Z",
        "2026-09-14T14:40:00Z",
    ])
    return pd.DataFrame(
        {
            "Open": [1.15363, 1.15388],
            "High": [1.15395, 1.15422],
            "Low": [1.15361, 1.15382],
            "Close": [1.15389, 1.15420],
        },
        index=index,
    )


def _choch_event(frame):
    return {
        "timestamp": frame.index[0].isoformat(),
        "event_type": "CHOCH",
        "direction": "BULLISH",
        "broken_level": 1.15374,
        "close": 1.15389,
        "event_invalidation_swing": {"type": "LOW", "price": 1.15231},
    }


def test_eurusd_strategy_lab_candidates_include_choch(monkeypatch):
    frame = _candidate_frame()
    event = _choch_event(frame)
    monkeypatch.setattr(eur_v3b, "analyze_structure", lambda *args, **kwargs: {"events": [event]})

    rows = list(eur_v3b.candidates(
        None,
        frame,
        frame.index[0],
        frame.index[-1] + pd.Timedelta(minutes=5),
        {},
    ))

    assert len(rows) == 1
    assert rows[0][0]["event_type"] == "CHOCH"
    assert rows[0][6]["reason"] == "five_minute_choch"


def test_xauusd_strategy_lab_candidates_include_choch(monkeypatch):
    frame = _candidate_frame() * 3700
    event = _choch_event(frame)
    event["broken_level"] = float(frame.iloc[0]["Close"])
    event["close"] = float(frame.iloc[0]["Close"])
    event["event_invalidation_swing"] = {"type": "LOW", "price": float(frame.iloc[0]["Low"] - 10)}
    monkeypatch.setattr(gold_v3b, "analyze_structure", lambda *args, **kwargs: {"events": [event]})

    rows = list(gold_v3b.candidates(
        None,
        frame,
        frame.index[0],
        frame.index[-1] + pd.Timedelta(minutes=5),
        {},
    ))

    assert len(rows) == 1
    assert rows[0][0]["event_type"] == "CHOCH"
    assert rows[0][6]["reason"] == "five_minute_choch"
