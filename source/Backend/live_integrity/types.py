"""Shared, broker-free Strategy Studio evaluator types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    trigger_close: float | None = None


@dataclass(frozen=True)
class TrendFacts:
    bos_choch_direction: str | None
    ema50_direction: str | None
    ema200_direction: str | None
    swing_structure_direction: str | None


@dataclass(frozen=True)
class EvaluationState:
    status: str = "WAITING"
    pending_setup: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvaluationResult:
    signal: str
    steps: dict[str, dict[str, Any]]
    setup_id: str | None
    entry: float | None
    sl: float | None
    tp1: float | None
    tp2: float | None
    risk_budget: dict[str, Any] | None
    next_state: EvaluationState
