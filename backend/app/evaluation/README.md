# evaluation/ — Metrics and benchmarking

Planned modules:
- `metrics.py` — groundedness, citation precision/recall, claim support rate,
  hallucination rate, coverage, source diversity, redundancy (req. 9)
- `benchmark.py` — run the same topic set across all three `ResearchMode`s
  and tabulate results (req. 10)
- `datasets.py` — loads the topic set from `data/benchmarks/`
- `report.py` — comparison tables + chart-ready JSON for the frontend
