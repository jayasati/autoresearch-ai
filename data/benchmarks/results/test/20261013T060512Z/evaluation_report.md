# Evaluation summary

- Benchmark set: **test v1.0.0** (fingerprint `7f6aff0e2413`)
- Items: 8 × arms: model_only, hybrid, search_grounded, full_autoresearch_verified
- Started: 2026-10-13T06:05:12.485547+00:00  ·  benchmark_run: `14693741-45ab-4dad-9349-73f761b56754`
- Constants: model=gpt-4o-mini, temperature=None, depth=quick, metrics_version=metrics-v1, extraction_prompt=claim-extraction-v1, verification_prompt=verification-v1, relevance_judge=relevance-judge-v1

Per-arm means over items where the metric is defined (n in parentheses). **There is deliberately no overall score**: each metric answers one question, and the formulas are in `data/benchmarks/METRICS.md` and `app/services/benchmark/metrics.py`.

| Metric | model_only | hybrid | search_grounded | full_autoresearch_verified |
|---|---|---|---|---|
| Claim support rate | 0.0 (n=7) | 0.6305 (n=8) | 0.5841 (n=8) | 0.6709 (n=7) |
| Citation precision | — | 0.4867 (n=8) | 0.5612 (n=8) | 0.7096 (n=7) |
| Citation coverage | 0.0 (n=7) | 0.7699 (n=8) | 0.5729 (n=8) | 0.6873 (n=7) |
| Unsupported claim rate | 0.0 (n=7) | 0.0995 (n=8) | 0.0859 (n=8) | 0.0921 (n=7) |
| Retrieval precision@K | — | 0.6786 (n=7) | 0.6071 (n=7) | 0.5833 (n=6) |
| Retrieval recall@K | — | 1.0 (n=7) | 1.0 (n=7) | 1.0 (n=6) |
| Retrieval nDCG@K | — | 0.9335 (n=7) | 0.7597 (n=7) | 0.7519 (n=6) |
| Product latency (s) | 8.9624 (n=7) | 20.5051 (n=8) | 21.2752 (n=8) | 79.0013 (n=7) |
| Product tokens | 737.5714 (n=7) | 1438.875 (n=8) | 1443.375 (n=8) | 28298 (n=7) |
| Product cost (USD) | 0.0003 (n=7) | 0.0004 (n=8) | 0.0004 (n=8) | 0.0058 (n=7) |
| Conflict detection rate | 0.0 (0/1) | 1.0 (1/1) | 1.0 (1/1) | 1.0 (1/1) |
| Items | 7 ok / 1 failed | 8 ok / 0 failed | 8 ok / 0 failed | 7 ok / 1 failed |
| Failure rate | 0.125 | 0.0 | 0.0 | 0.125 |

## Baseline vs AutoResearch

Baseline: `model_only` · AutoResearch: `full_autoresearch_verified`. Differences are computed, and the direction column classifies them against each metric's better direction — mechanically, so prose cannot soften a regression. Relative difference is omitted where not meaningful (undefined values, or a zero baseline).

| Metric | better is | baseline | AutoResearch | abs. diff | rel. diff | direction |
|---|---|---|---|---|---|---|
| claim_support_rate | higher | 0.0 | 0.6709 | 0.6709 | — | improved |
| citation_precision | higher | — | 0.7096 | — | — | — |
| citation_coverage | higher | 0.0 | 0.6873 | 0.6873 | — | improved |
| unsupported_claim_rate | lower | 0.0 | 0.0921 | 0.0921 | — | worse |
| retrieval_precision_at_k | higher | — | 0.5833 | — | — | — |
| retrieval_recall_at_k | higher | — | 1.0 | — | — | — |
| retrieval_ndcg_at_k | higher | — | 0.7519 | — | — | — |
| product_latency_seconds | lower | 8.9624 | 79.0013 | 70.0389 | 7.8147 | worse |
| product_total_tokens | lower | 737.5714 | 28298 | 27560.4286 | 37.3665 | worse |
| product_cost_usd | lower | 0.0003 | 0.0058 | 0.0055 | 18.3333 | worse |
| failure_rate | lower | 0.125 | 0.125 | 0.0 | 0.0 | unchanged |

Raw per-item numbers, run ids and the relevance judge's labelled pairs are in the raw results JSON; the flat table is the CSV. Nothing in this file was computed independently of those.
