# Evaluation summary

- Benchmark set: **development v1.0.0** (fingerprint `27a3ec16f52f`)
- Items: 8 × arms: model_only, hybrid, search_grounded, full_autoresearch_verified
- Started: 2026-10-12T18:18:24.710075+00:00  ·  benchmark_run: `9c67e694-d49c-41ef-9803-33e63c7b8859`
- Constants: model=gpt-4o-mini, temperature=None, depth=quick, metrics_version=metrics-v1, extraction_prompt=claim-extraction-v1, verification_prompt=verification-v1, relevance_judge=relevance-judge-v1

Per-arm means over items where the metric is defined (n in parentheses). **There is deliberately no overall score**: each metric answers one question, and the formulas are in `data/benchmarks/METRICS.md` and `app/services/benchmark/metrics.py`.

| Metric | model_only | hybrid | search_grounded | full_autoresearch_verified |
|---|---|---|---|---|
| Claim support rate | 0.0 (n=8) | 0.6878 (n=7) | 0.709 (n=8) | 0.5737 (n=8) |
| Citation precision | — | 0.6571 (n=7) | 0.5888 (n=8) | 0.5394 (n=8) |
| Citation coverage | 0.0 (n=8) | 0.6913 (n=7) | 0.6497 (n=8) | 0.614 (n=8) |
| Unsupported claim rate | 0.0 (n=8) | 0.0554 (n=7) | 0.0217 (n=8) | 0.0826 (n=8) |
| Retrieval precision@K | — | 0.625 (n=6) | 0.5 (n=7) | 0.5357 (n=7) |
| Retrieval recall@K | — | 0.9167 (n=6) | 0.8571 (n=7) | 0.8571 (n=7) |
| Retrieval nDCG@K | — | 0.9866 (n=6) | 0.8099 (n=7) | 0.7717 (n=7) |
| Product latency (s) | 7.8388 (n=8) | 13.0844 (n=7) | 16.8116 (n=8) | 57.3961 (n=8) |
| Product tokens | 692.375 (n=8) | 1332.2857 (n=7) | 1357.875 (n=8) | 29921.125 (n=8) |
| Product cost (USD) | 0.0003 (n=8) | 0.0004 (n=7) | 0.0004 (n=8) | 0.0061 (n=8) |
| Conflict detection rate | 0.0 (0/1) | 1.0 (1/1) | 1.0 (1/1) | 1.0 (1/1) |
| Items | 8 ok / 0 failed | 7 ok / 1 failed | 8 ok / 0 failed | 8 ok / 0 failed |

Raw per-item numbers, run ids and the relevance judge's labelled pairs are in `results.json`; the flat table is `results.csv`. Nothing in this file was computed independently of those.
