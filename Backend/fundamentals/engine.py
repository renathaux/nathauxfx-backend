from __future__ import annotations

from fundamentals.confidence import calculate_confidence
from fundamentals.config import FACTOR_WEIGHTS, MINIMUM_ACTIVE_COVERAGE
from fundamentals.explanations import select_top_reasons
from fundamentals.factors.central_bank import calculate_policy_factor
from fundamentals.factors.employment import calculate_employment_factor
from fundamentals.factors.growth import calculate_growth_factor
from fundamentals.factors.inflation import calculate_inflation_factor
from fundamentals.factors.surprises import score_currency_surprises
from fundamentals.pair_bias import SUPPORTED_PAIRS, synthesize_pair_bias


FACTOR_NAMES = (
    "policy_score",
    "inflation_score",
    "employment_score",
    "growth_score",
    "surprise_score",
)


def build_currency_strength(currency, observations, history_lookup=None, now=None):
    policy = calculate_policy_factor(observations, currency, now=now)
    factors = {
        "policy_score": policy,
        "inflation_score": calculate_inflation_factor(
            observations, currency, now=now, policy_context=policy
        ),
        "employment_score": calculate_employment_factor(observations, currency, now=now),
        "growth_score": calculate_growth_factor(observations, currency, now=now),
        "surprise_score": score_currency_surprises(
            observations,
            currency,
            history_lookup=history_lookup,
            now=now,
        ),
    }
    active = {
        name: factor
        for name, factor in factors.items()
        if factor.get("score") is not None and factor.get("status") == "ACTIVE"
    }
    active_weight = sum(FACTOR_WEIGHTS[name] for name in active)
    score = None
    if active_weight > 0:
        score = sum(
            float(factor["score"]) * FACTOR_WEIGHTS[name]
            for name, factor in active.items()
        ) / active_weight
    evidence = [item for factor in active.values() for item in factor.get("evidence") or []]
    weighted_confidence = (
        sum(
            float(factor.get("confidence") or 0) * FACTOR_WEIGHTS[name]
            for name, factor in active.items()
        ) / active_weight
        if active_weight > 0
        else 0.0
    )
    coverage = round(active_weight, 4)
    reliable = coverage >= MINIMUM_ACTIVE_COVERAGE
    status = "ACTIVE" if reliable else "INSUFFICIENT_DATA"
    confidence = min(weighted_confidence, coverage * 100.0)
    return {
        "currency": currency,
        "score": round(score, 2) if score is not None else None,
        "status": status,
        "coverage": coverage,
        "confidence": round(confidence, 2),
        "active_factors": sorted(active),
        "missing_factors": sorted(set(FACTOR_NAMES) - set(active)),
        "normalized_weights": {
            name: round(FACTOR_WEIGHTS[name] / active_weight, 4)
            for name in active
        } if active_weight else {},
        "factors": factors,
        "evidence": evidence,
    }


def calculate_fundamental_state(symbol, observations, history_lookup=None, now=None):
    normalized = str(symbol or "").upper().replace("/", "")
    currencies = SUPPORTED_PAIRS.get(normalized)
    if not currencies:
        return {
            "currency_strength": {},
            "pair": synthesize_pair_bias(normalized, {}),
            "confidence": 0.0,
            "top_reasons": [],
        }
    currency_results = {
        currency: build_currency_strength(
            currency,
            observations,
            history_lookup=history_lookup,
            now=now,
        )
        for currency in currencies
    }
    pair = synthesize_pair_bias(normalized, currency_results)
    return {
        "currency_strength": currency_results,
        "pair": pair,
        "confidence": calculate_confidence(currency_results, pair),
        "top_reasons": select_top_reasons(currency_results),
    }
