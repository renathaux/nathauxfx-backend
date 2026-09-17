"""Deterministic closed-candle facts for the shared Strategy Studio evaluator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from indicators.smc.engine import analyze_structure
from services.strategy_engine.types import CandleFacts, StructureEventFacts, TrendFacts


_POINT_SIZE = {"EURUSD": 0.00001, "XAUUSD": 0.01}


def _direction(value) -> str | None:
    normalized = str(value or "").upper()
    if normalized in {"BULLISH", "BUY"}:
        return "BUY"
    if normalized in {"BEARISH", "SELL"}:
        return "SELL"
    return None


def _timestamp(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        return result.tz_localize("UTC")
    return result.tz_convert("UTC")


def _body_percent(row) -> float:
    full_range = max(float(row.High) - float(row.Low), 1e-12)
    return abs(float(row.Close) - float(row.Open)) / full_range * 100.0


def _ema_direction(close: float, ema: float) -> str | None:
    if close > ema:
        return "BUY"
    if close < ema:
        return "SELL"
    return None


def _swing_direction(swings: list[dict]) -> str | None:
    highs = [float(item["price"]) for item in swings if item.get("type") == "HIGH"]
    lows = [float(item["price"]) for item in swings if item.get("type") == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BUY"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "SELL"
    return None


@dataclass
class MarketFactsTimeline:
    trading_timeframe: str
    trend_timeframe: str | None
    _candles: dict[pd.Timestamp, CandleFacts]
    _events: dict[pd.Timestamp, StructureEventFacts]
    _trends: dict[pd.Timestamp, TrendFacts]
    _trading_index: list[pd.Timestamp]
    _trading_swings: list[dict]

    def candle(self, timestamp) -> CandleFacts | None:
        return self._candles.get(_timestamp(timestamp))

    def structure_event(self, timestamp) -> StructureEventFacts | None:
        return self._events.get(_timestamp(timestamp))

    def trend(self, timestamp) -> TrendFacts:
        target = _timestamp(timestamp)
        candidates = [value for value in self._trends if value <= target]
        if not candidates:
            return TrendFacts(None, None, None, None)
        return self._trends[max(candidates)]

    @property
    def trading_timestamps(self) -> tuple[pd.Timestamp, ...]:
        return tuple(self._trading_index)

    def next_trading_timestamp(self, timestamp) -> pd.Timestamp | None:
        target = _timestamp(timestamp)
        for candidate in self._trading_index:
            if candidate > target:
                return candidate
        return None

    def nearest_opposite_swing(self, timestamp, direction: str, entry: float) -> float | None:
        """Return nearest confirmed opposing swing that is profitable from entry."""
        target = _timestamp(timestamp)
        swing_type = "HIGH" if direction == "BUY" else "LOW"
        values: list[float] = []
        for item in self._trading_swings:
            if item.get("type") != swing_type:
                continue
            confirmed = item.get("confirmed_timestamp") or item.get("timestamp")
            if confirmed is None or _timestamp(confirmed) > target:
                continue
            price = float(item["price"])
            if direction == "BUY" and price > entry:
                values.append(price)
            elif direction == "SELL" and price < entry:
                values.append(price)
        if not values:
            return None
        return min(values) if direction == "BUY" else max(values)


def _build_event_map(analysis: dict) -> dict[pd.Timestamp, StructureEventFacts]:
    result: dict[pd.Timestamp, StructureEventFacts] = {}
    for item in analysis.get("events") or []:
        direction = _direction(item.get("direction"))
        event_type = str(item.get("event_type") or "").upper()
        if direction is None or event_type not in {"BOS", "CHOCH"}:
            continue
        invalidation = item.get("event_invalidation_swing") or {}
        invalidation_price = invalidation.get("price")
        event = StructureEventFacts(
            timestamp=_timestamp(item["timestamp"]),
            direction=direction,
            event_type=event_type,
            broken_level=float(item["broken_level"]),
            invalidation_price=(
                float(invalidation_price) if invalidation_price is not None else None
            ),
        )
        result[event.timestamp] = event
    return result


def _confirmed_swings_through(swings: Iterable[dict], timestamp: pd.Timestamp) -> list[dict]:
    confirmed: list[dict] = []
    for item in swings:
        confirmed_at = item.get("confirmed_timestamp") or item.get("timestamp")
        if confirmed_at is not None and _timestamp(confirmed_at) <= timestamp:
            confirmed.append(item)
    return confirmed


def build_market_facts(
    bundle: dict[str, pd.DataFrame],
    symbol: str,
    trading_timeframe: str,
    trend_timeframe: str | None,
) -> MarketFactsTimeline:
    trading = bundle.get(trading_timeframe)
    if trading is None or trading.empty:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    trading = trading.copy().sort_index()
    trading.index = pd.to_datetime(trading.index, utc=True)
    point_size = _POINT_SIZE.get(str(symbol).upper())
    trading_analysis = analyze_structure(
        trading,
        timeframe=trading_timeframe,
        point_size=point_size,
    )

    candles = {
        _timestamp(timestamp): CandleFacts(
            timestamp=_timestamp(timestamp),
            open=float(row.Open),
            high=float(row.High),
            low=float(row.Low),
            close=float(row.Close),
            body_percent=_body_percent(row),
        )
        for timestamp, row in trading.iterrows()
    }
    events = _build_event_map(trading_analysis)

    source_timeframe = trend_timeframe or trading_timeframe
    trend_frame = bundle.get(source_timeframe)
    if trend_frame is None or trend_frame.empty:
        trend_frame = trading
        source_timeframe = trading_timeframe
    else:
        trend_frame = trend_frame.copy().sort_index()
        trend_frame.index = pd.to_datetime(trend_frame.index, utc=True)

    if source_timeframe == trading_timeframe:
        trend_analysis = trading_analysis
    else:
        trend_analysis = analyze_structure(
            trend_frame,
            timeframe=source_timeframe,
            point_size=point_size,
        )

    ema50 = trend_frame["Close"].astype(float).ewm(span=50, adjust=False).mean()
    ema200 = trend_frame["Close"].astype(float).ewm(span=200, adjust=False).mean()
    trend_events = sorted(
        _build_event_map(trend_analysis).values(),
        key=lambda item: item.timestamp,
    )
    trend_swings = list(trend_analysis.get("swings") or [])

    trends: dict[pd.Timestamp, TrendFacts] = {}
    last_structure_direction: str | None = None
    event_index = 0
    for index, (timestamp, row) in enumerate(trend_frame.iterrows()):
        current = _timestamp(timestamp)
        while event_index < len(trend_events) and trend_events[event_index].timestamp <= current:
            last_structure_direction = trend_events[event_index].direction
            event_index += 1
        confirmed = _confirmed_swings_through(trend_swings, current)
        close = float(row.Close)
        trends[current] = TrendFacts(
            bos_choch_direction=last_structure_direction,
            ema50_direction=_ema_direction(close, float(ema50.iloc[index])),
            ema200_direction=_ema_direction(close, float(ema200.iloc[index])),
            swing_structure_direction=_swing_direction(confirmed),
        )

    return MarketFactsTimeline(
        trading_timeframe=trading_timeframe,
        trend_timeframe=trend_timeframe,
        _candles=candles,
        _events=events,
        _trends=trends,
        _trading_index=sorted(candles),
        _trading_swings=list(trading_analysis.get("swings") or []),
    )
