# Final analysis — metrics-v2 re-evaluation of the frozen test runs

- Source: `..\data\benchmarks\results\test\20261013T060512Z` (benchmark run `14693741-45ab-4dad-9349-73f761b56754`)
- Source files unchanged during re-evaluation: **True**
- Set: test v1.0.0 (fingerprint `7f6aff0e2413`)
- Constants: metrics_v2_version=metrics-v2, external_judge=external-verification-v1, pool_version=pool-v1, v1_source=imported verbatim from source rows; not recomputed

## Primary analysis (§3.1): external support, matched questions, one shared denominator

- Matched questions (n=6): tst-conflicting_literature-01, tst-false_premise-01, tst-insufficient_evidence-01, tst-multi_paper_synthesis-01, tst-normal_research-01, tst-numerical_claims-01
- Excluded: tst-citation_verification-01 × full_autoresearch_verified — missing product cell
- Excluded: tst-current_information-01 × model_only — missing product cell
- Excluded: tst-citation_verification-01 × model_only — unmatched: the other arm is ineligible here
- Excluded: tst-current_information-01 × full_autoresearch_verified — unmatched: the other arm is ineligible here

| Question | baseline | system |
|---|---|---|
| tst-conflicting_literature-01 | 0.6429 | 0.7 |
| tst-false_premise-01 | 0.2857 | 0.9091 |
| tst-insufficient_evidence-01 | 0.3158 | 0.5714 |
| tst-multi_paper_synthesis-01 | 0.4286 | 0.7333 |
| tst-normal_research-01 | 0.5263 | 0.5455 |
| tst-numerical_claims-01 | 0.5333 | 0.5455 |
| **macro (primary)** | 0.4554 | 0.6675 |
| micro (secondary) | 0.4312 | 0.6769 |

Macro difference (system − baseline): **0.2121** over n=6.

## All-available v2 aggregates (secondary view; per-arm item subsets named)

| Arm | n | support macro | support micro | contradiction macro | contradiction micro |
|---|---|---|---|---|---|
| full_autoresearch_verified | 7 | 0.6161 | 0.6154 | 0.0642 | 0.0641 |
| hybrid | 8 | 0.5453 | 0.5301 | 0.0995 | 0.0964 |
| model_only | 7 | 0.4761 | 0.4516 | 0.0483 | 0.0484 |
| search_grounded | 8 | 0.5117 | 0.5 | 0.0996 | 0.1111 |

v1 values in the rows are imported verbatim from the source files and were not recomputed. Terminology per METRICS.md: external supportability is not ground-truth factual accuracy; contradiction-vs-pool is not all hallucinations.
