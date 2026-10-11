def calculate_confidence(currency_results, pair_result):
    results = list(currency_results.values())
    if not results or pair_result.get("pair_score") is None:
        return 0.0
    coverage = min(float(item.get("coverage") or 0) for item in results)
    freshness_quality = sum(float(item.get("confidence") or 0) for item in results) / len(results) / 100.0
    magnitude = min(1.0, abs(float(pair_result["pair_score"])) / 100.0)
    pair_factor_contributions = []
    revision_values = []
    history_counts = []
    provisional_counts = []
    if len(results) == 2:
        base, quote = results
        common = set(base.get("active_factors") or []) & set(quote.get("active_factors") or [])
        for name in common:
            base_factor = (base.get("factors") or {}).get(name) or {}
            quote_factor = (quote.get("factors") or {}).get(name) or {}
            pair_factor_contributions.append(
                float(base_factor.get("score") or 0) - float(quote_factor.get("score") or 0)
            )
    for result in results:
        for factor in (result.get("factors") or {}).values():
            history_counts.append(int(factor.get("evidence_count") or 0))
            provisional_counts.append(int(factor.get("provisional_count") or 0))
            for evidence in factor.get("evidence") or []:
                revision_values.append(float(evidence.get("revision_stability") or 1.0))
    agreement = 0.5
    if pair_factor_contributions:
        signs = {1 if value > 0 else -1 if value < 0 else 0 for value in pair_factor_contributions}
        agreement = 1.0 if len(signs - {0}) <= 1 else 0.35
    revision_stability = sum(revision_values) / len(revision_values) if revision_values else 0.5
    history_depth = min(1.0, sum(history_counts) / 20.0)
    total_evidence = sum(history_counts)
    standardized_quality = (
        max(0.35, 1.0 - sum(provisional_counts) / total_evidence)
        if total_evidence
        else 0.35
    )
    raw = 100.0 * (
        0.30 * coverage
        + 0.20 * freshness_quality
        + 0.15 * agreement
        + 0.10 * magnitude
        + 0.10 * revision_stability
        + 0.10 * history_depth
        + 0.05 * standardized_quality
    )
    # Missing coverage is a hard ceiling. Sparse or provisional evidence also
    # cannot produce institutional-looking confidence.
    evidence_cap = 75.0 if total_evidence < 16 or sum(provisional_counts) else 85.0
    return round(min(raw, coverage * 100.0, evidence_cap), 2)
