from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EconomicEventSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    event_name: str
    indicator: str
    country: str | None = None
    currency: str
    impact: str = "UNKNOWN"
    release_time: datetime
    actual: Any = None
    forecast: Any = None
    previous: Any = None
    revised_previous: Any = None
    provider: str
    provider_event_id: str | None = None
    provider_timestamp: datetime | None = None
    fetched_at: datetime
    data_status: str
    raw: dict[str, Any] | None = None


class FactorResult(BaseModel):
    factor: str
    score: float | None = Field(default=None, ge=-100, le=100)
    status: str
    coverage: float = Field(default=0, ge=0, le=1)
    confidence: float = Field(default=0, ge=0, le=100)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    method: str | None = None
    updated_at: datetime | None = None
    evidence_count: int = 0
    provisional_count: int = 0
    revision_stability: float = Field(default=0, ge=0, le=1)


class CurrencyStrengthResult(BaseModel):
    currency: str
    score: float | None = Field(default=None, ge=-100, le=100)
    status: str
    coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=100)
    factors: dict[str, FactorResult]
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class PairBiasResult(BaseModel):
    symbol: str
    pair_score: float | None = Field(default=None, ge=-100, le=100)
    direction: str
    status: str
