# Evaluation summary

- Benchmark set: **validation v1.0.0** (fingerprint `43d63fb1c0bc`)
- Items: 8 × arms: model_only, hybrid, search_grounded, full_autoresearch_verified
- Started: 2026-10-13T10:53:46.690078+00:00  ·  benchmark_run: `a42c2de3-e9f0-4715-8466-3dc5d3f2daf6`
- Constants: model=gpt-4o-mini, temperature=None, depth=quick, metrics_version=metrics-v1, metrics_v2_version=metrics-v2, extraction_prompt=claim-extraction-v1, verification_prompt=verification-v1, relevance_judge=relevance-judge-v1, external_judge=external-verification-v1, pool_version=pool-v1

Per-arm means over items where the metric is defined (n in parentheses). **There is deliberately no overall score**: each metric answers one question, and the formulas are in `data/benchmarks/METRICS.md` and `app/services/benchmark/metrics.py`.

| Metric | model_only | hybrid | search_grounded | full_autoresearch_verified |
|---|---|---|---|---|
| Claim support rate | 0.0 (n=8) | 0.6146 (n=8) | 0.5837 (n=8) | 0.6185 (n=8) |
| Citation precision | — | 0.7146 (n=8) | 0.682 (n=8) | 0.7576 (n=8) |
| Citation coverage | 0.0 (n=8) | 0.7283 (n=8) | 0.6998 (n=8) | 0.7222 (n=8) |
| Unsupported claim rate | 0.0 (n=8) | 0.2202 (n=8) | 0.1923 (n=8) | 0.218 (n=8) |
| Retrieval precision@K | — | 0.5 (n=7) | 0.4286 (n=7) | 0.5357 (n=7) |
| Retrieval recall@K | — | 1.0 (n=7) | 0.8571 (n=7) | 0.9286 (n=7) |
| Retrieval nDCG@K | — | 0.7823 (n=7) | 0.7111 (n=7) | 0.7549 (n=7) |
| External support rate (pool, v2) | 0.4602 (n=8) | 0.5866 (n=8) | 0.5728 (n=8) | 0.5559 (n=8) |
| External contradiction rate (pool, v2) | 0.1273 (n=8) | 0.172 (n=8) | 0.125 (n=8) | 0.1591 (n=8) |
| Product latency (s) | 7.9052 (n=8) | 14.112 (n=8) | 20.5605 (n=8) | 79.6928 (n=8) |
| Product tokens | 720.25 (n=8) | 1330.875 (n=8) | 1277.625 (n=8) | 26656.25 (n=8) |
| Product cost (USD) | 0.0003 (n=8) | 0.0004 (n=8) | 0.0004 (n=8) | 0.0056 (n=8) |
| Conflict detection rate | 0.0 (0/1) | 1.0 (1/1) | 1.0 (1/1) | 1.0 (1/1) |
| Items | 8 ok / 0 failed | 8 ok / 0 failed | 8 ok / 0 failed | 8 ok / 0 failed |
| Failure rate | 0.0 | 0.0 | 0.0 | 0.0 |

## Baseline vs AutoResearch

Baseline: `model_only` · AutoResearch: `full_autoresearch_verified`. Differences are computed, and the direction column classifies them against each metric's better direction — mechanically, so prose cannot soften a regression. Relative difference is omitted where not meaningful (undefined values, or a zero baseline).

| Metric | better is | baseline | AutoResearch | abs. diff | rel. diff | direction |
|---|---|---|---|---|---|---|
| claim_support_rate | higher | 0.0† | 0.6185 | 0.6185 | — | improved |
| citation_precision | higher | — | 0.7576 | — | — | — |
| citation_coverage | higher | 0.0† | 0.7222 | 0.7222 | — | improved |
| unsupported_claim_rate | lower | 0.0† | 0.218 | 0.218 | — | worse |
| retrieval_precision_at_k | higher | — | 0.5357 | — | — | — |
| retrieval_recall_at_k | higher | — | 0.9286 | — | — | — |
| retrieval_ndcg_at_k | higher | — | 0.7549 | — | — | — |
| external_support_rate | higher | 0.4602 | 0.5559 | 0.0957 | 0.208 | improved |
| external_contradiction_rate | lower | 0.1273 | 0.1591 | 0.0318 | 0.2498 | worse |
| product_latency_seconds | lower | 7.9052 | 79.6928 | 71.7876 | 9.0811 | worse |
| product_total_tokens | lower | 720.25 | 26656.25 | 25936.0 | 36.0097 | worse |
| product_cost_usd | lower | 0.0003 | 0.0056 | 0.0053 | 17.6667 | worse |
| failure_rate | lower | 0.0 | 0.0 | 0.0 | — | unchanged |

Structural baseline values — definitions, not measurements (audit CD-2):

- † `claim_support_rate`: structural: the arm retrieves no evidence, so no claim can verify above not_enough_evidence — 0.0 is the only possible value.
- † `citation_coverage`: structural: the arm's prompt forbids citations, so coverage cannot exceed 0.0.
- † `unsupported_claim_rate`: structural: claims citing nothing cannot be contradicted by their citations — 0.0 here is not a quality signal.

Raw per-item numbers, run ids and the relevance judge's labelled pairs are in the raw results JSON; the flat table is the CSV. Nothing in this file was computed independently of those.
