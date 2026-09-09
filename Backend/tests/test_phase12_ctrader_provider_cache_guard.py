from datetime import datetime, timezone

import pandas as pd

import api
import ctrader_connector as ctrader
import services


def _frame(times, closes):
    index = pd.DatetimeIndex(pd.to_datetime(times, utc=True), name="Datetime")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1] * len(closes),
        },
        index=index,
    )


def _prepare_guard(monkeypatch, fake_get):
    monkeypatch.setattr(ctrader, "get_ctrader_market_data", fake_get)
    monkeypatch.setattr(
        ctrader,
        "_PROVIDER_ONLY_CANDLE_CACHE_GUARD_INSTALLED",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        ctrader,
        "_PROVIDER_ONLY_CANDLE_CACHE_ORIGINAL_GET",
        None,
        raising=False,
    )
    monkeypatch.setattr(api, "get_ctrader_market_data", fake_get)
    assert services._install_ctrader_provider_cache_guard() is True
    assert api.get_ctrader_market_data is ctrader.get_ctrader_market_data


def test_cache_hit_returns_synthetic_candle_without_persisting_it(monkeypatch):
    provider = _frame(["2026-09-09T19:15:00Z"], [1.1627])
    synthetic_time = pd.Timestamp("2026-09-09T19:30:00Z")
    fetched_at = datetime(2026, 9, 9, 19, 35, tzinfo=timezone.utc)
    cache_key = ctrader.get_ctrader_candle_cache_key("EURUSD", "15m")
    monkeypatch.setitem(
        ctrader.CTRADER_CANDLE_CACHE,
        cache_key,
        {
            "data": provider.copy(deep=True),
            "fetched_at": fetched_at,
            "source": "ctrader_cache",
            "symbol": "EURUSD",
            "timeframe": "15m",
        },
    )

    def legacy_cache_hit(symbol, timeframe, *args, **kwargs):
        cached = ctrader.CTRADER_CANDLE_CACHE[cache_key]
        synthetic = cached["data"].copy(deep=True)
        synthetic.loc[synthetic_time] = {
            "Open": 1.1628,
            "High": 1.1629,
            "Low": 1.1628,
            "Close": 1.1629,
            "Volume": 1,
        }
        # Reproduce the verified legacy bug: the display frame was written
        # back into the provider cache on cache hits.
        cached["data"] = synthetic.copy(deep=True)
        return synthetic

    _prepare_guard(monkeypatch, legacy_cache_hit)
    returned = api.get_ctrader_market_data("EURUSD", "15m")

    assert list(returned.index) == [provider.index[0], synthetic_time]
    cached_after = ctrader.CTRADER_CANDLE_CACHE[cache_key]["data"]
    assert list(cached_after.index) == [provider.index[0]]
    assert float(cached_after.iloc[-1]["Close"]) == 1.1627


def test_real_provider_refresh_replaces_cache_and_is_not_rolled_back(monkeypatch):
    old_provider = _frame(["2026-09-09T19:15:00Z"], [4399.5])
    refreshed_provider = _frame(
        ["2026-09-09T19:15:00Z", "2026-09-09T19:30:00Z"],
        [4399.5, 4397.2],
    )
    synthetic_time = pd.Timestamp("2026-09-09T19:45:00Z")
    old_fetched_at = datetime(2026, 9, 9, 19, 35, tzinfo=timezone.utc)
    new_fetched_at = datetime(2026, 9, 9, 19, 50, tzinfo=timezone.utc)
    cache_key = ctrader.get_ctrader_candle_cache_key("XAUUSD", "15m")
    monkeypatch.setitem(
        ctrader.CTRADER_CANDLE_CACHE,
        cache_key,
        {
            "data": old_provider.copy(deep=True),
            "fetched_at": old_fetched_at,
            "source": "ctrader_cache",
            "symbol": "XAUUSD",
            "timeframe": "15m",
        },
    )

    def real_refresh(symbol, timeframe, *args, **kwargs):
        cached = ctrader.CTRADER_CANDLE_CACHE[cache_key]
        cached["data"] = refreshed_provider.copy(deep=True)
        cached["fetched_at"] = new_fetched_at
        cached["source"] = "ctrader"
        returned = refreshed_provider.copy(deep=True)
        returned.loc[synthetic_time] = {
            "Open": 4397.2,
            "High": 4398.0,
            "Low": 4396.8,
            "Close": 4397.8,
            "Volume": 1,
        }
        return returned

    _prepare_guard(monkeypatch, real_refresh)
    returned = ctrader.get_ctrader_market_data(
        "XAUUSD", "15m", force_refresh=True
    )

    assert list(returned.index) == [
        old_provider.index[0],
        pd.Timestamp("2026-09-09T19:30:00Z"),
        synthetic_time,
    ]
    cached_after = ctrader.CTRADER_CANDLE_CACHE[cache_key]["data"]
    assert list(cached_after.index) == list(refreshed_provider.index)
    assert float(cached_after.iloc[-1]["Close"]) == 4397.2
    assert ctrader.CTRADER_CANDLE_CACHE[cache_key]["fetched_at"] == new_fetched_at
