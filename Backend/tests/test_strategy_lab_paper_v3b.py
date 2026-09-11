from types import SimpleNamespace

import pandas as pd
import pytest

from services.strategy_lab.paper_v3b_bridge import (
    PAPER_V3B_MODEL,
    build_paper_v3b_candidate,
)


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


def test_eurusd_bridge_builds_frozen_candidate_without_mutating_lifecycle():
    index = pd.to_datetime([
        "2026-09-10T10:00:00Z",
        "2026-09-10T10:05:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [1.1000, 1.1017],
            "High": [1.1020, 1.1028],
            "Low": [1.0995, 1.1015],
            "Close": [1.1018, 1.1025],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_eur_v3b",
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 1.1015,
        "broken_swing_timestamp": "2026-09-10T09:50:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.1000},
        "event_identity": {"stable": "eur"},
        "tradable": True,
    }

    result = build_paper_v3b_candidate(
        "EURUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["paper_entry_ready"] is True
    assert result["paper_entry_model"] == PAPER_V3B_MODEL
    assert result["signal"] == "BUY"
    assert result["source_indicator_event_id"] == "smc1_eur_v3b"
    assert result["setup_identity"]["setup_timeframe"] == "5m"
    assert result["setup_identity"]["swing_type"] == "HIGH"
    assert result["risk_reward_ratio"] == pytest.approx(1.90)
    assert result["protection_trigger_tp2_fraction"] == pytest.approx(0.70)
    assert result["protected_stop_tp2_fraction"] == pytest.approx(0.60)
    assert result["no_partial_close_at_protection_trigger"] is True


def test_gold_bridge_uses_gold_specific_frozen_point_math():
    index = pd.to_datetime([
        "2026-09-10T11:00:00Z",
        "2026-09-10T11:05:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [4400.0, 4397.5],
            "High": [4402.0, 4398.0],
            "Low": [4396.0, 4395.0],
            "Close": [4397.0, 4395.5],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_gold_v3b",
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BEARISH",
        "broken_level": 4396.0,
        "broken_swing_timestamp": "2026-09-10T10:50:00Z",
        "event_invalidation_swing": {"type": "HIGH", "price": 4400.0},
        "event_identity": {"stable": "gold"},
        "tradable": True,
    }

    result = build_paper_v3b_candidate(
        "XAUUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["paper_entry_ready"] is True
    assert result["signal"] == "SELL"
    assert result["stop_loss"] == pytest.approx(4400.50)
    assert result["tp2"] == pytest.approx(4386.00)
    assert result["tp1"] == pytest.approx(4388.85)
    assert result["protected_sl_price"] == pytest.approx(4389.80)
    assert result["setup_identity"]["swing_type"] == "LOW"


def test_bridge_rejects_historical_authoritative_event():
    index = pd.to_datetime([
        "2026-09-10T12:00:00Z",
        "2026-09-10T12:05:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [1.1000, 1.1017],
            "High": [1.1020, 1.1028],
            "Low": [1.0995, 1.1015],
            "Close": [1.1018, 1.1025],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_historical",
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 1.1015,
        "broken_swing_timestamp": "2026-09-10T11:50:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.1000},
        "tradable": False,
    }

    result = build_paper_v3b_candidate(
        "EURUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["signal"] == "WAIT"
    assert result["paper_entry_reason"] == "WAIT_V3B_PAPER_5M_BOS"


def test_bridge_final_gate_can_fail_closed_without_opening_any_trade():
    index = pd.to_datetime([
        "2026-09-10T13:00:00Z",
        "2026-09-10T13:05:00Z",
    ])
    frame = pd.DataFrame(
        {
            "Open": [1.1000, 1.1017],
            "High": [1.1020, 1.1028],
            "Low": [1.0995, 1.1015],
            "Close": [1.1018, 1.1025],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_gate",
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 1.1015,
        "broken_swing_timestamp": "2026-09-10T12:50:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.1000},
        "tradable": True,
    }

    def blocked(*args, **kwargs):
        return {"ok": False, "reason": "WAIT_TEST_BLOCK"}

    result = build_paper_v3b_candidate(
        "EURUSD",
        frame,
        strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
        final_gate=blocked,
    )

    assert result["signal"] == "WAIT"
    assert result["paper_entry_ready"] is False
    assert result["paper_entry_reason"] == "WAIT_TEST_BLOCK"
