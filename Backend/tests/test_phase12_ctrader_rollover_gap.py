import pandas as pd

from services import indicator_event_stream_service as stream


def test_eurusd_ctrader_rollover_gap_is_expected_during_dst():
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T20:50:00Z")) is True
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T20:55:00Z")) is False
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T21:00:00Z")) is False
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T21:05:00Z")) is True


def test_eurusd_ctrader_rollover_gap_tracks_new_york_standard_time():
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-12-14T21:50:00Z")) is True
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-12-14T21:55:00Z")) is False
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-12-14T22:00:00Z")) is False
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-12-14T22:05:00Z")) is True


def test_rollover_exception_does_not_hide_unrelated_eurusd_gaps():
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T20:45:00Z")) is True
    assert stream._expected_market_candle("EURUSD", pd.Timestamp("2026-08-17T21:10:00Z")) is True
