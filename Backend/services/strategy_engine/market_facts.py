"""Deterministic market-fact timeline for Strategy Studio evaluation."""
from __future__ import annotations

from bisect import bisect_right

import pandas as pd

from indicators.smc import analyze_structure
from services.strategy_engine.types import CandleFacts, StructureEventFacts, TrendFacts


POINT_SIZE = {"EURUSD": 0.00001, "XAUUSD": 0.01}


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or not isinstance(frame, pd.DataFrame):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    data = frame.copy().sort_index()
    data.index = pd.to_datetime(data.index, utc=True)
    return data


def _direction(value) -> str | None:
    if value == "BULLISH":
        return "BUY"
    if value == "BEARISH":
        return "SELL"
    return None


def _ema_direction(close: float, ema: float) -> str | None:
    if pd.isna(ema):
        return None
    if close > ema:
        return "BUY"
    if close < ema:
        return "SELL"
    return None


def _swing_structure(swings: list[dict], timestamp: pd.Timestamp) -> str | None:
    available = [s for s in swings if _utc(s["confirmed_timestamp"]) <= timestamp]
    highs = [s for s in available if s.get("type") == "HIGH"][-2:]
    lows = [s for s in available if s.get("type") == "LOW"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    high_up = float(highs[-1]["price"]) > float(highs[-2]["price"])
    low_up = float(lows[-1]["price"]) > float(lows[-2]["price"])
    high_down = float(highs[-1]["price"]) < float(highs[-2]["price"])
    low_down = float(lows[-1]["price"]) < float(lows[-2]["price"])
    if high_up and low_up:
        return "BUY"
    if high_down and low_down:
        return "SELL"
    return None


class MarketFactsTimeline:
    def __init__(self, *, candles, events, trends, timestamps, trading_swings):
        self._candles = candles
        self._events = events
        self._trends = trends
        self._timestamps = sorted(_utc(value) for value in timestamps)
        self._trading_swings = list(trading_swings)

    def timestamps(self) -> list[pd.Timestamp]:
        return list(self._timestamps)

    def candle(self, timestamp) -> CandleFacts | None:
        return self._candles.get(_utc(timestamp))

    def structure_event(self, timestamp) -> StructureEventFacts | None:
        return self._events.get(_utc(timestamp))

    def trend(self, timestamp) -> TrendFacts:
        stamp = _utc(timestamp)
        direct = self._trends.get(stamp)
        if direct is not None:
            return direct
        keys = sorted(self._trends)
        position = bisect_right(keys, stamp) - 1
        if position < 0:
            return TrendFacts(None, None, None, None)
        return self._trends[keys[position]]

    def previous_timestamp(self, timestamp) -> pd.Timestamp | None:
        stamp = _utc(timestamp)
        position = bisect_right(self._timestamps, stamp) - 1
        if position <= 0:
            return None
        return self._timestamps[position - 1]

    def next_timestamp(self, timestamp) -> pd.Timestamp | None:
        stamp = _utc(timestamp)
        position = bisect_right(self._timestamps, stamp)
        if position >= len(self._timestamps):
            return None
        return self._timestamps[position]

    def opposite_swing(self, timestamp, direction: str, entry: float) -> float | None:
        stamp = _utc(timestamp)
        available = [s for s in self._trading_swings if _utc(s["confirmed_timestamp"]) <= stamp]
        if direction == "BUY":
            candidates = [float(s["price"]) for s in available if s.get("type") == "HIGH" and float(s["price"]) > entry]
            return min(candidates) if candidates else None
        candidates = [float(s["price"]) for s in available if s.get("type") == "LOW" and float(s["price"]) < entry]
        return max(candidates) if candidates else None


def build_market_facts(bundle: dict[str, pd.DataFrame], symbol: str, trading_timeframe: str,
                       trend_timeframe: str | None) -> MarketFactsTimeline:
    public_symbol = str(symbol or "").upper().replace("/", "")
    trading_tf = str(trading_timeframe or "").lower()
    trading = _normalize(bundle.get(trading_tf))
    if trading.empty:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    point_size = POINT_SIZE.get(public_symbol)
    trading_analysis = analyze_structure(trading, timeframe=trading_tf, point_size=point_size)

    candles: dict[pd.Timestamp, CandleFacts] = {}
    for timestamp, row in trading.iterrows():
        span = max(float(row.High) - float(row.Low), 1e-12)
        candles[_utc(timestamp)] = CandleFacts(
            timestamp=_utc(timestamp),
            open=float(row.Open), high=float(row.High), low=float(row.Low), close=float(row.Close),
            body_percent=abs(float(row.Close) - float(row.Open)) / span * 100.0,
        )

    events: dict[pd.Timestamp, StructureEventFacts] = {}
    for raw in trading_analysis.get("events") or []:
        stamp = _utc(raw["timestamp"])
        invalidation = raw.get("event_invalidation_swing") or {}
        events[stamp] = StructureEventFacts(
            timestamp=stamp,
            direction=_direction(raw.get("direction")),
            event_type=str(raw.get("event_type") or "").upper(),
            broken_level=float(raw["broken_level"]),
            invalidation_price=float(invalidation["price"]) if invalidation.get("price") is not None else None,
            trigger_close=float(raw["close"]) if raw.get("close") is not None else None,
        )

    trend_tf = str(trend_timeframe or trading_tf).lower()
    trend_frame = _normalize(bundle.get(trend_tf))
    if trend_frame.empty:
        trend_frame = trading
        trend_tf = trading_tf
    trend_analysis = analyze_structure(trend_frame, timeframe=trend_tf, point_size=point_size)
    trend_events = sorted(
        [(_utc(item["timestamp"]), _direction(item.get("direction"))) for item in trend_analysis.get("events") or []],
        key=lambda pair: pair[0],
    )
    trend_swings = trend_analysis.get("swings") or []
    ema50 = trend_frame.Close.astype(float).ewm(span=50, adjust=False).mean()
    ema200 = trend_frame.Close.astype(float).ewm(span=200, adjust=False).mean()

    trend_at_source: dict[pd.Timestamp, TrendFacts] = {}
    latest_structure = None
    event_cursor = 0
    for timestamp, row in trend_frame.iterrows():
        stamp = _utc(timestamp)
        while event_cursor < len(trend_events) and trend_events[event_cursor][0] <= stamp:
            latest_structure = trend_events[event_cursor][1]
            event_cursor += 1
        trend_at_source[stamp] = TrendFacts(
            bos_choch_direction=latest_structure,
            ema50_direction=_ema_direction(float(row.Close), float(ema50.loc[timestamp])),
            ema200_direction=_ema_direction(float(row.Close), float(ema200.loc[timestamp])),
            swing_structure_direction=_swing_structure(trend_swings, stamp),
        )

    source_keys = sorted(trend_at_source)
    trends: dict[pd.Timestamp, TrendFacts] = {}
    for timestamp in trading.index:
        stamp = _utc(timestamp)
        position = bisect_right(source_keys, stamp) - 1
        trends[stamp] = (
            trend_at_source[source_keys[position]]
            if position >= 0
            else TrendFacts(None, None, None, None)
        )

    return MarketFactsTimeline(
        candles=candles,
        events=events,
        trends=trends,
        timestamps=trading.index,
        trading_swings=trading_analysis.get("swings") or [],
    )
