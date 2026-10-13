# Evaluation summary

- Benchmark set: **development v1.0.0** (fingerprint `27a3ec16f52f`)
- Items: 8 × arms: model_only, hybrid, search_grounded, full_autoresearch_verified
- Started: 2026-10-13T09:00:58.685444+00:00  ·  benchmark_run: `eb70c1e1-2333-4ce5-90df-8e4f7ddb13f2`
- Constants: model=gpt-4o-mini, temperature=None, depth=quick, metrics_version=metrics-v1, metrics_v2_version=metrics-v2, extraction_prompt=claim-extraction-v1, verification_prompt=verification-v1, relevance_judge=relevance-judge-v1, external_judge=external-verification-v1, pool_version=pool-v1

Per-arm means over items where the metric is defined (n in parentheses). **There is deliberately no overall score**: each metric answers one question, and the formulas are in `data/benchmarks/METRICS.md` and `app/services/benchmark/metrics.py`.

| Metric | model_only | hybrid | search_grounded | full_autoresearch_verified |
|---|---|---|---|---|
| Claim support rate | 0.0 (n=8) | 0.6363 (n=8) | 0.6281 (n=6) | 0.6081 (n=8) |
| Citation precision | — | 0.6627 (n=8) | 0.7188 (n=6) | 0.6861 (n=8) |
| Citation coverage | 0.0 (n=8) | 0.6767 (n=8) | 0.6559 (n=6) | 0.6423 (n=8) |
| Unsupported claim rate | 0.0 (n=8) | 0.1049 (n=8) | 0.0876 (n=6) | 0.0885 (n=8) |
| Retrieval precision@K | — | 0.5 (n=7) | 0.45 (n=5) | 0.5 (n=7) |
| Retrieval recall@K | — | 1.0 (n=7) | 1.0 (n=5) | 1.0 (n=7) |
| Retrieval nDCG@K | — | 0.7999 (n=7) | 0.8387 (n=5) | 0.7752 (n=7) |
| External support rate (pool, v2) | 0.4417 (n=8) | 0.5401 (n=8) | 0.6538 (n=6) | 0.5587 (n=8) |
| External contradiction rate (pool, v2) | 0.104 (n=8) | 0.1121 (n=8) | 0.0573 (n=6) | 0.0973 (n=8) |
| Product latency (s) | 7.5057 (n=8) | 13.3159 (n=8) | 16.524 (n=6) | 78.1471 (n=8) |
| Product tokens | 668.875 (n=8) | 1316.875 (n=8) | 1420.1667 (n=6) | 30088.625 (n=8) |
| Product cost (USD) | 0.0003 (n=8) | 0.0004 (n=8) | 0.0004 (n=6) | 0.0063 (n=8) |
| Conflict detection rate | 0.0 (0/1) | 0.0 (0/1) | — | 0.0 (0/1) |
| Items | 8 ok / 0 failed | 8 ok / 0 failed | 6 ok / 2 failed | 8 ok / 0 failed |
| Failure rate | 0.0 | 0.0 | 0.25 | 0.0 |

## Baseline vs AutoResearch

Baseline: `model_only` · AutoResearch: `full_autoresearch_verified`. Differences are computed, and the direction column classifies them against each metric's better direction — mechanically, so prose cannot soften a regression. Relative difference is omitted where not meaningful (undefined values, or a zero baseline).

| Metric | better is | baseline | AutoResearch | abs. diff | rel. diff | direction |
|---|---|---|---|---|---|---|
| claim_support_rate | higher | 0.0† | 0.6081 | 0.6081 | — | improved |
| citation_precision | higher | — | 0.6861 | — | — | — |
| citation_coverage | higher | 0.0† | 0.6423 | 0.6423 | — | improved |
| unsupported_claim_rate | lower | 0.0† | 0.0885 | 0.0885 | — | worse |
| retrieval_precision_at_k | higher | — | 0.5 | — | — | — |
| retrieval_recall_at_k | higher | — | 1.0 | — | — | — |
| retrieval_ndcg_at_k | higher | — | 0.7752 | — | — | — |
| external_support_rate | higher | 0.4417 | 0.5587 | 0.117 | 0.2649 | improved |
| external_contradiction_rate | lower | 0.104 | 0.0973 | -0.0067 | -0.0644 | improved |
| product_latency_seconds | lower | 7.5057 | 78.1471 | 70.6414 | 9.4117 | worse |
| product_total_tokens | lower | 668.875 | 30088.625 | 29419.75 | 43.9839 | worse |
| product_cost_usd | lower | 0.0003 | 0.0063 | 0.006 | 20.0 | worse |
| failure_rate | lower | 0.0 | 0.0 | 0.0 | — | unchanged |

Structural baseline values — definitions, not measurements (audit CD-2):

- † `claim_support_rate`: structural: the arm retrieves no evidence, so no claim can verify above not_enough_evidence — 0.0 is the only possible value.
- † `citation_coverage`: structural: the arm's prompt forbids citations, so coverage cannot exceed 0.0.
- † `unsupported_claim_rate`: structural: claims citing nothing cannot be contradicted by their citations — 0.0 here is not a quality signal.

Raw per-item numbers, run ids and the relevance judge's labelled pairs are in the raw results JSON; the flat table is the CSV. Nothing in this file was computed independently of those.
