from __future__ import annotations

import json
from datetime import datetime

from db import SessionLocal
from fundamentals.config import FACTOR_WEIGHTS
from models import (
    CurrencyStrengthSnapshot,
    FundamentalFactorInput,
    FundamentalInsightSnapshot,
)


def persist_insight(calculation_id, insight_id, response, currency_results, *, session_factory=None):
    factory = session_factory or SessionLocal
    generated_at = response["generated_at"]
    if isinstance(generated_at, str):
        generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    safe_response = json.loads(json.dumps(response, default=str))
    with factory() as session:
        for currency, result in currency_results.items():
            for factor, factor_result in result["factors"].items():
                session.add(FundamentalFactorInput(
                    calculation_id=calculation_id,
                    currency=currency,
                    factor=factor,
                    score=factor_result.get("score"),
                    status=factor_result.get("status", "INSUFFICIENT_DATA"),
                    weight=float(FACTOR_WEIGHTS.get(factor, 0.0)),
                    evidence=factor_result.get("evidence") or [],
                    calculated_at=generated_at,
                ))
            session.add(CurrencyStrengthSnapshot(
                snapshot_id=f"{calculation_id}:{currency}",
                calculation_id=calculation_id,
                currency=currency,
                score=result.get("score"),
                status=result.get("status", "INSUFFICIENT_DATA"),
                coverage=float(result.get("coverage") or 0),
                confidence=float(result.get("confidence") or 0),
                factors=result.get("factors") or {},
                evidence=result.get("evidence") or [],
                calculated_at=generated_at,
            ))
        overall = response["overall_bias"]
        session.add(FundamentalInsightSnapshot(
            insight_id=insight_id,
            calculation_id=calculation_id,
            symbol=response["symbol"],
            pair_score=overall.get("pair_score"),
            direction=overall.get("direction", "NEUTRAL"),
            confidence=float(overall.get("confidence") or 0),
            status=overall.get("status", "INSUFFICIENT_DATA"),
            response_json=safe_response,
            generated_at=generated_at,
        ))
        session.commit()
