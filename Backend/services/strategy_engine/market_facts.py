"""Deterministic market-fact timeline for Strategy Studio evaluation."""
from __future__ import annotations

from bisect import bisect_right

import pandas as pd

from indicators.smc import analyze_structure, detect_confirmed_swings
from services.strategy_engine.types import CandleFacts, StructureEventFacts, TrendFacts


POINT_SIZE = {"EURUSD": 0.00001, "XAUUSD": 0.01}


def _utc(value) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp) and str(value.tzinfo) == "UTC":
        return value
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


def _serialise_confirmed_swings(frame: pd.DataFrame) -> list[dict]:
    """Build non-repainting swing facts independently from the active BOS engine.

    The current exported SMC authority intentionally uses the legacy TradingView
    structure engine for BOS/CHOCH parity. That engine returns no `swings`
    collection, so Strategy Studio must not rely on analyze_structure(...)["swings"]
    for SWING_STRUCTURE trend filters or OPPOSITE_SWING targets.
    """
    swings = detect_confirmed_swings(frame)
    return [
        {
            "type": swing.swing_type,
            "timestamp": swing.timestamp,
            "confirmed_timestamp": swing.confirmed_timestamp,
            "price": float(swing.price),
            "index": int(swing.index),
            "confirmed_index": int(swing.confirmed_index),
        }
        for swing in swings
    ]


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


class SwingDirectionIndex:
    """Prefix state of confirmed swings; only confirmations at/before query time.

    detect_confirmed_swings supplies stable confirmation order. No future swing
    participates in the prefix direction, including tied confirmation times.
    """
    def __init__(self, swings):
        self.times = []
        self.directions = []
        highs, lows = [], []
        for swing in swings:
            (highs if swing['type'] == 'HIGH' else lows).append(float(swing['price']))
            direction = None
            if len(highs) >= 2 and len(lows) >= 2:
                if highs[-1] > highs[-2] and lows[-1] > lows[-2]: direction = 'BUY'
                elif highs[-1] < highs[-2] and lows[-1] < lows[-2]: direction = 'SELL'
            self.times.append(_utc(swing['confirmed_timestamp']))
            self.directions.append(direction)
    def at(self, stamp):
        position = bisect_right(self.times, stamp) - 1
        return self.directions[position] if position >= 0 else None


class MarketFactsTimeline:
    def __init__(self, *, candles, events, trends, timestamps, trading_swings, structure_candles=None):
        self._structure_candles = structure_candles or candles
        self._candles = candles
        self._events = events
        self._trends = trends
        self._trend_keys = sorted(trends)
        self._timestamps = sorted(_utc(value) for value in timestamps)
        self._trading_swings = list(trading_swings)

    def timestamps(self) -> list[pd.Timestamp]:
        return list(self._timestamps)

    def candle(self, timestamp) -> CandleFacts | None:
        return self._candles.get(_utc(timestamp))

    def structure_candle(self, timestamp):
        return self._structure_candles.get(_utc(timestamp))

    def structure_event(self, timestamp) -> StructureEventFacts | None:
        return self._events.get(_utc(timestamp))

    def trend(self, timestamp) -> TrendFacts:
        stamp = _utc(timestamp)
        direct = self._trends.get(stamp)
        if direct is not None:
            return direct
        keys = self._trend_keys
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
                       trend_timeframe: str | None, structure_timeframe: str | None = None, *, compact=False,
                       precomputed_swings_by_tf=None, compact_candle_stores=None) -> MarketFactsTimeline:
    public_symbol = str(symbol or "").upper().replace("/", "")
    trading_tf = str(trading_timeframe or "").lower()
    trading = _normalize(bundle.get(trading_tf))
    if trading.empty:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    def swings_for(timeframe, frame):
        if precomputed_swings_by_tf is not None and timeframe in precomputed_swings_by_tf:
            return precomputed_swings_by_tf[timeframe]
        return _serialise_confirmed_swings(frame)

    point_size = POINT_SIZE.get(public_symbol)
    structure_tf = structure_timeframe or trading_tf
    structure_frame = _normalize(bundle.get(structure_tf))
    if structure_frame.empty:
        raise ValueError("SIMULATION_STRUCTURE_HISTORY_UNAVAILABLE")
    structure_analysis = analyze_structure(structure_frame, timeframe=structure_tf, point_size=point_size)
    # Bundles use candle OPEN timestamps. A higher-frame event is available on
    # the trading candle whose CLOSE matches its close, never at its open.
    minutes = {"5m": 5, "15m": 15, "1h": 60}
    availability_offset = pd.Timedelta(minutes=minutes[structure_tf] - minutes[trading_tf])
    if compact:
        from services.strategy_engine.market_facts_compact import CandleStore, CompactTimeline
        if compact_candle_stores is None:
            structure_candles = CandleStore(structure_frame, availability_offset)
            candles = CandleStore(trading)
        else:
            candles, structure_candles = compact_candle_stores
        trading_swings = swings_for(trading_tf, trading)
    else:
        structure_candles = {}
        for row in structure_frame.itertuples():
            timestamp = row.Index
            stamp = _utc(timestamp) + availability_offset
            span = max(float(row.High) - float(row.Low), 1e-12)
            structure_candles[stamp] = CandleFacts(stamp, float(row.Open), float(row.High), float(row.Low), float(row.Close), abs(float(row.Close)-float(row.Open))/span*100.0)
        trading_swings = swings_for(trading_tf, trading)

        candles: dict[pd.Timestamp, CandleFacts] = {}
        for row in trading.itertuples():
            timestamp = row.Index
            span = max(float(row.High) - float(row.Low), 1e-12)
            candles[_utc(timestamp)] = CandleFacts(
                timestamp=_utc(timestamp),
                open=float(row.Open), high=float(row.High), low=float(row.Low), close=float(row.Close),
                body_percent=abs(float(row.Close) - float(row.Open)) / span * 100.0,
            )
    events: dict[pd.Timestamp, StructureEventFacts] = {}
    for raw in structure_analysis.get("events") or []:
        stamp = _utc(raw["timestamp"]) + availability_offset
        if stamp not in candles:
            continue
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
    trend_analysis = structure_analysis if trend_tf == structure_tf else analyze_structure(trend_frame, timeframe=trend_tf, point_size=point_size)
    trend_events = sorted(
        [(_utc(item["timestamp"]), _direction(item.get("direction"))) for item in trend_analysis.get("events") or []],
        key=lambda pair: pair[0],
    )
    trend_swings = trading_swings if trend_tf == trading_tf else swings_for(trend_tf, trend_frame)
    swing_directions = SwingDirectionIndex(trend_swings)
    ema50 = trend_frame.Close.astype(float).ewm(span=50, adjust=False).mean()
    ema200 = trend_frame.Close.astype(float).ewm(span=200, adjust=False).mean()

    trend_at_source: dict[pd.Timestamp, TrendFacts] = {}
    latest_structure = None
    event_cursor = 0
    for row, ema50_value, ema200_value in zip(trend_frame.itertuples(), ema50, ema200):
        timestamp = row.Index
        stamp = _utc(timestamp)
        while event_cursor < len(trend_events) and trend_events[event_cursor][0] <= stamp:
            latest_structure = trend_events[event_cursor][1]
            event_cursor += 1
        trend_at_source[stamp] = TrendFacts(
            bos_choch_direction=latest_structure,
            ema50_direction=_ema_direction(float(row.Close), float(ema50_value)),
            ema200_direction=_ema_direction(float(row.Close), float(ema200_value)),
            swing_structure_direction=swing_directions.at(stamp),
        )

    timeline_class = CompactTimeline if compact else MarketFactsTimeline
    timeline = timeline_class(
        candles=candles,
        events=events,
        trends=trend_at_source,
        timestamps=trading.index,
        trading_swings=trading_swings,
        structure_candles=structure_candles,
    )
    return timeline
