from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Literal["EURUSD"] = "EURUSD"
    strategy: Literal["baseline_v1"] = "baseline_v1"
    start: datetime
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("timezone is required")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be after start")
        return self
