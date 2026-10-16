# Metrics-v2 re-evaluation of the stored development-set runs

- Source: `..\data\benchmarks\results\development\20261013T090058Z` (benchmark run `eb70c1e1-2333-4ce5-90df-8e4f7ddb13f2`)
- Source files unchanged during re-evaluation: **True**
- Set: development v1.0.0 (fingerprint `27a3ec16f52f`)
- Constants: metrics_v2_version=metrics-v2, external_judge=external-verification-v2, pool_version=pool-v1, v1_source=imported verbatim from source rows; not recomputed

## Primary analysis (§3.1): external support, matched questions, one shared denominator

- Matched questions (n=8): dev-citation_verification-01, dev-conflicting_literature-01, dev-current_information-01, dev-false_premise-01, dev-insufficient_evidence-01, dev-multi_paper_synthesis-01, dev-normal_research-01, dev-numerical_claims-01

| Question | baseline | system |
|---|---|---|
| dev-citation_verification-01 | 0.5909 | 0.7 |
| dev-conflicting_literature-01 | 0.5714 | 0.5833 |
| dev-current_information-01 | 0.4286 | 0.5 |
| dev-false_premise-01 | 0.45 | 0.7692 |
| dev-insufficient_evidence-01 | 0.4545 | 0.5 |
| dev-multi_paper_synthesis-01 | 0.3125 | 0.7692 |
| dev-normal_research-01 | 0.72 | 1.0 |
| dev-numerical_claims-01 | 0.3125 | 0.75 |
| **macro (primary)** | 0.48 | 0.6965 |
| micro (secondary) | 0.5 | 0.7188 |

Macro difference (system − baseline): **0.2165** over n=8.

## All-available v2 aggregates (secondary view; per-arm item subsets named)

| Arm | n | support macro | support micro | contradiction macro | contradiction micro |
|---|---|---|---|---|---|
| full_autoresearch_verified | 8 | 0.6965 | 0.7188 | 0.0505 | 0.0521 |
| hybrid | 8 | 0.642 | 0.6429 | 0.017 | 0.0204 |
| model_only | 8 | 0.48 | 0.5 | 0.0297 | 0.029 |
| search_grounded | 6 | 0.6945 | 0.6977 | 0.0 | 0.0 |

v1 values in the rows are imported verbatim from the source files and were not recomputed. Terminology per METRICS.md: external supportability is not ground-truth factual accuracy; contradiction-vs-pool is not all hallucinations.
