import pandas as pd
import pytest

from services.paper_v3b_bridge import (
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
    setup = result["v3b_setup_state"]
    assert setup["indicator_event_id"] == "smc1_eur_v3b"
    assert setup["m5_confirmation_id"] == result["m5_confirmation_id"]
    assert setup["bos_body_pass"] is True
    assert setup["second_5m_same_direction"] is True
    assert setup["second_5m_stays_beyond_bos_level"] is True
    assert setup["structural_sl_found"] is True
    assert setup["entry_ready"] is True
    assert setup["signal"] == "BUY"
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


def test_fresh_bos_before_second_close_exposes_time_bounded_progress_without_entry():
    index = pd.to_datetime(["2026-09-16T12:00:00Z", "2026-09-16T12:05:00Z"])
    frame = pd.DataFrame(
        {
            "Open": [1.1000, 1.1001],
            "High": [1.1002, 1.1014],
            "Low": [1.0998, 1.1000],
            "Close": [1.1001, 1.1012],
        }, index=index,
    )
    event = {
        "event_id": "fresh_bos_1205", "symbol": "EURUSD", "timeframe": "5m",
        "timestamp": index[-1].isoformat(), "event_type": "BOS",
        "direction": "BULLISH", "broken_level": 1.1009,
        "broken_swing_timestamp": index[0].isoformat(),
        "event_invalidation_swing": {"type": "LOW", "price": 1.0998},
        "tradable": True,
    }

    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )

    assert result["signal"] == "WAIT"
    assert result["paper_entry_ready"] is False
    assert result["paper_entry_reason"] == "WAIT_V3B_PAPER_SECOND_5M"
    assert result["source_indicator_event_id"] == "fresh_bos_1205"
    assert result["paper_entry_details"]["bos_body_ratio"] == pytest.approx(1.1 / 1.4)
    assert result["paper_entry_details"]["bos_candle_time"] == index[-1].isoformat()
    setup = result["v3b_setup_state"]
    assert setup["indicator_event_id"] == "fresh_bos_1205"
    assert setup["has_bos"] is True
    assert setup["bos_body_pass"] is True
    assert setup["second_5m_same_direction"] is None
    assert setup["second_5m_stays_beyond_bos_level"] is None
    assert setup["structural_sl_found"] is None
    assert setup["lifecycle_state"] == "WAITING_CONFIRMATION"
    assert setup["signal"] == "WAIT"
    assert "entry_price" not in result

    invalid_direction = dict(event, direction="UNKNOWN")
    invalid = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_reader=_authority(invalid_direction),
    )
    assert invalid["paper_entry_reason"] == "WAIT_V3B_PAPER_5M_BOS"


def test_new_closed_bos_progress_is_not_masked_by_expired_older_pair():
    index = pd.to_datetime([
        "2026-09-16T12:00:00Z", "2026-09-16T12:05:00Z",
        "2026-09-16T12:10:00Z",
    ])
    frame = pd.DataFrame({
        "Open": [1.1000, 1.1010, 1.1018],
        "High": [1.1018, 1.1020, 1.1034],
        "Low": [1.0998, 1.1008, 1.1017],
        "Close": [1.1017, 1.1019, 1.1032],
    }, index=index)
    def event(event_id, timestamp, level):
        return {
            "event_id": event_id, "symbol": "EURUSD", "timeframe": "5m",
            "timestamp": timestamp.isoformat(), "event_type": "BOS",
            "direction": "BULLISH", "broken_level": level,
            "broken_swing_timestamp": "2026-09-16T11:50:00Z",
            "event_invalidation_swing": {"type": "LOW", "price": 1.0998},
            "tradable": True,
        }
    events = [event("old-bos", index[0], 1.1015), event("new-bos", index[2], 1.1030)]
    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_reader=lambda *_args: {"events": events},
    )
    assert result["paper_entry_reason"] == "WAIT_V3B_PAPER_SECOND_5M"
    assert result["v3b_setup_state"]["indicator_event_id"] == "new-bos"
    assert result["v3b_setup_state"]["lifecycle_state"] == "WAITING_CONFIRMATION"


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


def _historical_sell_case(event_time, bos, second, broken_level, invalidation=1.15955):
    index = pd.to_datetime([event_time, pd.Timestamp(event_time) + pd.Timedelta(minutes=5)])
    frame = pd.DataFrame(
        {
            "Open": [bos[0], second[0]],
            "High": [bos[1], second[1]],
            "Low": [bos[2], second[2]],
            "Close": [bos[3], second[3]],
        },
        index=index,
    )
    event = {
        "event_id": "smc1_" + pd.Timestamp(event_time).strftime("%H%M"),
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BEARISH",
        "broken_level": broken_level,
        "broken_swing_timestamp": (index[0] - pd.Timedelta(minutes=20)).isoformat(),
        "event_invalidation_swing": {"type": "HIGH", "price": invalidation},
        "event_identity": {"fixture": pd.Timestamp(event_time).isoformat()},
        "tradable": True,
    }
    return frame, event


@pytest.mark.parametrize(
    "event_time,bos,second,broken_level,expected",
    [
        (
            "2026-09-14T00:25:00Z",
            (1.15893, 1.15896, 1.15852, 1.15857),
            (1.15858, 1.15879, 1.15854, 1.15862),
            1.15886,
            "WAIT_V3B_PAPER_SECOND_5M",
        ),
        (
            "2026-09-14T01:05:00Z",
            (1.15858, 1.15865, 1.15838, 1.15845),
            (1.15843, 1.15853, 1.15837, 1.15840),
            1.15851,
            "WAIT_V3B_PAPER_BOS_BODY",
        ),
        (
            "2026-09-14T04:05:00Z",
            (1.15702, 1.15704, 1.15669, 1.15679),
            (1.15678, 1.15687, 1.15678, 1.15680),
            1.15690,
            "WAIT_V3B_PAPER_SECOND_5M",
        ),
    ],
)
def test_verified_sep14_rejections_keep_their_exact_v3b_reason(
    event_time, bos, second, broken_level, expected
):
    frame, event = _historical_sell_case(event_time, bos, second, broken_level)
    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )
    assert result["paper_entry_ready"] is False
    assert result["paper_entry_reason"] == expected
    assert result["source_indicator_event_id"] == event["event_id"]
    assert result["paper_entry_details"]["bos_candle_time"] == frame.index[0].isoformat()
    assert result["paper_entry_details"]["broken_level"] == broken_level
    setup = result["v3b_setup_state"]
    assert setup["indicator_event_id"] == event["event_id"]
    assert setup["has_bos"] is True
    assert setup["lifecycle_state"] == "INVALIDATED"
    assert setup["signal"] == "WAIT"
    if expected == "WAIT_V3B_PAPER_SECOND_5M":
        assert setup["bos_body_pass"] is True
        assert setup["second_5m_same_direction"] is False
    else:
        assert setup["bos_body_pass"] is False


def test_verified_sep14_0250_bos_and_0255_confirmation_are_selected():
    frame, event = _historical_sell_case(
        "2026-09-14T02:50:00Z",
        (1.15851, 1.15853, 1.15832, 1.15833),
        (1.15832, 1.15837, 1.15811, 1.15816),
        1.15835,
        invalidation=1.15879,
    )
    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )
    assert result["paper_entry_ready"] is True
    assert result["signal"] == "SELL"
    assert result["five_m_break_time"] == "2026-09-14T02:50:00+00:00"
    assert result["five_m_closed_candle_time"] == "2026-09-14T03:00:00+00:00"
    assert result["paper_entry_details"]["bos_body_ratio"] == pytest.approx(6 / 7)


def test_stale_authority_is_blocked_with_freshness_diagnostics():
    frame, event = _historical_sell_case(
        "2026-09-14T02:50:00Z",
        (1.15851, 1.15853, 1.15832, 1.15833),
        (1.15832, 1.15837, 1.15811, 1.15816),
        1.15835,
    )

    def stale_updater(*args, **kwargs):
        assert kwargs.get("analyzer") is not None
        return {
            "events": [event],
            "stream_status": "READY",
            "stream_last_candle": "2026-09-13T21:40:00Z",
        }

    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_updater=stale_updater,
    )
    assert result["paper_entry_reason"] == "WAIT_V3B_5M_AUTHORITY_STALE"
    assert result["paper_entry_details"]["latest_source_closed_candle"] == "2026-09-14T02:55:00+00:00"
    assert result["paper_entry_details"]["latest_durable_candle"] == "2026-09-13T21:40:00+00:00"
    assert result["paper_entry_details"]["lag_candles"] > 0


def test_future_authority_is_blocked_as_desync_with_signed_lag():
    frame, event = _historical_sell_case(
        "2026-09-14T02:50:00Z",
        (1.15851, 1.15853, 1.15832, 1.15833),
        (1.15832, 1.15837, 1.15811, 1.15816),
        1.15835,
    )

    def future_updater(*args, **kwargs):
        assert kwargs.get("analyzer") is not None
        return {
            "events": [event],
            "stream_status": "READY",
            "stream_last_candle": "2026-09-14T03:00:00Z",
        }

    result = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_updater=future_updater,
    )
    assert result["paper_entry_ready"] is False
    assert result["paper_entry_reason"] == "WAIT_V3B_5M_AUTHORITY_DESYNC"
    assert result["paper_entry_details"]["lag_minutes"] == pytest.approx(-5.0)
    assert result["paper_entry_details"]["lag_candles"] == -1


def test_recent_scan_recovers_one_missed_cycle_for_audit_but_never_executes_late():
    frame, event = _historical_sell_case(
        "2026-09-14T02:50:00Z",
        (1.15851, 1.15853, 1.15832, 1.15833),
        (1.15832, 1.15837, 1.15811, 1.15816),
        1.15835,
    )
    one_late = pd.concat([
        frame,
        pd.DataFrame(
            {"Open": [1.15816], "High": [1.15822], "Low": [1.15810], "Close": [1.15818]},
            index=pd.to_datetime(["2026-09-14T03:00:00Z"]),
        ),
    ])
    recovered = build_paper_v3b_candidate(
        "EURUSD", one_late, strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )
    assert recovered["paper_entry_ready"] is False
    assert recovered["signal"] == "WAIT"
    assert recovered["paper_entry_reason"] == "WAIT_V3B_RECOVERY_ENTRY_EXPIRED"
    assert recovered["paper_entry_details"]["historically_valid_setup"] is True
    assert recovered["paper_entry_details"]["recovery_lag_candles"] == 1
    assert recovered["paper_entry_details"]["historical_confirmation_close"] == pytest.approx(1.15816)
    assert "entry_price" not in recovered

    too_late = pd.concat([
        one_late,
        pd.DataFrame(
            {"Open": [1.15818], "High": [1.15825], "Low": [1.15812], "Close": [1.15820]},
            index=pd.to_datetime(["2026-09-14T03:05:00Z"]),
        ),
    ])
    expired = build_paper_v3b_candidate(
        "EURUSD", too_late, strict_trader_module=_Strict,
        authoritative_reader=_authority(event),
    )
    assert expired["paper_entry_ready"] is False
    assert expired["paper_entry_reason"] == "WAIT_V3B_PAPER_5M_BOS"
