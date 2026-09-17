"""Broker-free value types shared by Strategy Studio evaluation/simulation."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class CandleFacts:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    body_percent: float


@dataclass(frozen=True)
class StructureEventFacts:
    timestamp: pd.Timestamp
    direction: str
    event_type: str
    broken_level: float
    invalidation_price: float | None


@dataclass(frozen=True)
class TrendFacts:
    bos_choch_direction: str | None
    ema50_direction: str | None
    ema200_direction: str | None
    swing_structure_direction: str | None
