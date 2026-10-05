def select_top_reasons(currency_results, limit=3):
    reasons = []
    for currency, result in currency_results.items():
        for factor_name, factor in (result.get("factors") or {}).items():
            evidence = factor.get("evidence") or []
            score = factor.get("score")
            if score is None and evidence:
                score = evidence[0].get("score")
            if score is None or not evidence or factor.get("status", "ACTIVE") != "ACTIVE":
                continue
            normalized_weight = float((result.get("normalized_weights") or {}).get(factor_name) or 1.0)
            contribution = float(score) * normalized_weight
            if not contribution:
                continue
            direction = "BULLISH" if contribution > 0 else "BEARISH"
            event_names = [
                item.get("event_name") or item.get("indicator")
                for item in evidence[:2]
                if item.get("event_name") or item.get("indicator")
            ]
            label = factor_name.replace("_score", "").replace("_", " ")
            summary = (
                f"{currency} {label} evidence is {direction.lower()}"
                + (f" ({', '.join(event_names)})." if event_names else ".")
            )
            reasons.append({
                "currency": currency,
                "factor": factor_name,
                "direction": direction,
                "contribution": round(contribution, 2),
                "summary": summary,
                "evidence_event_ids": [
                    item.get("event_id") for item in evidence if item.get("event_id")
                ],
            })
    reasons.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    return [{"rank": index + 1, **item} for index, item in enumerate(reasons[:limit])]
