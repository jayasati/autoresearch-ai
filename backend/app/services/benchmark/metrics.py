"""
The experiment's metrics — exact formulas, one function each.

Every formula here is also written out in `data/benchmarks/METRICS.md`; the two must
not drift, and a test compares the docstrings against that file's formula lines.

Three rules hold throughout:

- **No arbitrary composites.** There is no overall score, no weighted blend. Each
  metric answers one question and is reported alone, with its numerator and
  denominator, because 0.8 over five claims and 0.8 over five hundred are different
  findings.
- **Undefined is None, not zero.** A precision with an empty denominator is not a
  bad score, it is no measurement; returning 0.0 would quietly punish a run for a
  question that gave it nothing to be measured on.
- **`partially_supported` counts as support** in the support rate and is also
  reported on its own. The alternative — a fractional weight — would be an
  arbitrary constant wearing a formula's clothes.

`METRICS_VERSION` is stored with every database row; if any definition here changes,
bump it — old numbers measured something else and must stay distinguishable.
"""

import math

METRICS_VERSION = "metrics-v1"

#: USD per token, from the provider's published prices. "Estimated" because
#: published prices change and cached-input discounts are ignored; the estimate is
#: an upper bound at these rates.
MODEL_PRICES_PER_TOKEN: dict[str, tuple[float, float]] = {
    # model prefix -> (input USD/token, output USD/token)
    "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
}


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def claim_support_rate(verdicts: dict[str, int]) -> float | None:
    """claim_support_rate = (supported + partially_supported) / total_claims"""
    total = sum(verdicts.values())
    return _ratio(verdicts.get("supported", 0) + verdicts.get("partially_supported", 0), total)


def unsupported_claim_rate(verdicts: dict[str, int]) -> float | None:
    """unsupported_claim_rate = (unsupported + contradicted) / total_claims"""
    total = sum(verdicts.values())
    return _ratio(verdicts.get("unsupported", 0) + verdicts.get("contradicted", 0), total)


def citation_precision(citation_statuses: dict[str, int]) -> float | None:
    """citation_precision = valid / (valid + misattributed + fabricated + broken)

    Unvalidated citations are excluded from the denominator: they are citations
    validation never reached (a skipped claim), not judged ones.
    """
    judged = sum(
        citation_statuses.get(status, 0)
        for status in ("valid", "misattributed", "fabricated", "broken")
    )
    return _ratio(citation_statuses.get("valid", 0), judged)


def citation_coverage(claims_with_citations: int, claims_requiring_citation: int) -> float | None:
    """citation_coverage = claims_with_at_least_one_citation / claims_requiring_citation"""
    return _ratio(claims_with_citations, claims_requiring_citation)


def precision_at_k(relevance: list[bool], k: int) -> float | None:
    """precision@K = |relevant chunks among the first K retrieved| / min(K, retrieved)

    `relevance` is in retrieval rank order. The denominator is the number actually
    inspected: retrieving 3 chunks and getting 3 relevant is precision 1.0, not 0.75
    against a K the collection could not fill.
    """
    if k <= 0 or not relevance:
        return None
    inspected = relevance[:k]
    return _ratio(sum(inspected), len(inspected))


def recall_at_k(statements_covered_at_k: int, expected_statements: int) -> float | None:
    """recall@K = |expected evidence statements covered by the first K chunks|
    / |expected statements|

    The classical denominator — all relevant documents in the corpus — is unknowable
    against the live web, so recall is defined over the item's expected_evidence
    statements: the ground truth the dataset actually has.
    """
    return _ratio(statements_covered_at_k, expected_statements)


def ndcg_at_k(relevance: list[bool], k: int) -> float | None:
    """nDCG@K = DCG@K / IDCG@K, binary gains, log2 discounting

    DCG@K  = sum over i=1..K of rel_i / log2(i + 1), rel_i in {0, 1},
             in retrieval rank order.
    IDCG@K = the same sum with the relevant items moved to the top ranks.
    None when no retrieved chunk is relevant (IDCG = 0): "how well ordered" is
    meaningless when there is nothing to order well.
    """
    if k <= 0 or not relevance:
        return None
    inspected = relevance[:k]
    dcg = sum(rel / math.log2(index + 2) for index, rel in enumerate(inspected))
    ideal = sorted(inspected, reverse=True)
    idcg = sum(rel / math.log2(index + 2) for index, rel in enumerate(ideal))
    return round(dcg / idcg, 4) if idcg else None


def conflict_detection_rate(items_detected: int, items_expected: int) -> float | None:
    """conflict_detection_rate = |items with expected conflicts where >=1 conflict was stored|
    / |items with expected conflicts|

    An arm-level metric: per item it is binary, and items whose ground truth expects
    no conflict contribute to neither term (detecting an unexpected conflict is not
    penalised here; it may be genuine).
    """
    return _ratio(items_detected, items_expected)


def estimated_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """estimated_cost_usd = prompt_tokens * input_price + completion_tokens * output_price

    Prices from MODEL_PRICES_PER_TOKEN, matched by longest model-name prefix (the
    provider reports dated snapshots like gpt-4o-mini-2024-07-18). None for a model
    with no listed price — a missing price must surface as a gap, not as $0.00.
    """
    match = max(
        (prefix for prefix in MODEL_PRICES_PER_TOKEN if model.startswith(prefix)),
        key=len,
        default=None,
    )
    if match is None:
        return None
    input_price, output_price = MODEL_PRICES_PER_TOKEN[match]
    return round(prompt_tokens * input_price + completion_tokens * output_price, 6)
