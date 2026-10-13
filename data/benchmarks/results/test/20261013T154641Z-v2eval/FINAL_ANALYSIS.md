# Final analysis — frozen test set, metrics-v2 re-evaluation

Executed 2026-10-13 under `docs/FINAL_ANALYSIS_PLAN.md` (LOCKED Revision 3,
Option B). Source: the stage-17 frozen-test product runs (benchmark
`14693741-45ab-4dad-9349-73f761b56754`), verified **byte-identical to the
committed git blobs before launch and unchanged after it**
(`source_files_unchanged: true`). No new product runs were made; no database rows
were written; the frozen set's product exposure remains exactly one. The judge is
`external-verification-v1`, untuned, with its labeled validation on record (9/11;
0/2 on compound partial-support). This document contains the §3 pre-registered
analyses and nothing else.

## §3.1 Primary analysis — external support, A vs D, one shared matched denominator

Matched questions (n = 6): conflicting_literature, false_premise,
insufficient_evidence, multi_paper_synthesis, normal_research, numerical_claims.
Excluded, with reasons: citation_verification × D and current_information × A
(stage-17 infrastructure-lost product cells), plus each one's unmatched counterpart
in the other arm.

| Question | baseline (A) | AutoResearch (D) |
|---|---|---|
| conflicting_literature | 0.6429 | 0.7000 |
| false_premise | 0.2857 | 0.9091 |
| insufficient_evidence | 0.3158 | 0.5714 |
| multi_paper_synthesis | 0.4286 | 0.7333 |
| normal_research | 0.5263 | 0.5455 |
| numerical_claims | 0.5333 | 0.5455 |
| **macro (PRIMARY)** | **0.4554** | **0.6675** |
| micro (secondary, same matched set) | 0.4312 | 0.6769 |

**Primary result: macro difference D − A = +0.2121 over n = 6 matched questions.**
D's per-question value is ≥ A's on all six (two near-ties, four clear leads; the
largest on false_premise: 0.286 → 0.909). Direction-consistency context (§4 rule —
all-available views, not pooled): development +0.117, validation +0.096, test
all-available +0.140 (0.476 → 0.616).

## §3.2 Secondary — external contradiction (all-available view)

| Arm | n | contradiction macro | micro | contradicted / judged |
|---|---|---|---|---|
| model_only | 7 | 0.0483 | 0.0484 | 6 / 124 |
| hybrid | 8 | 0.0995 | 0.0964 | 8 / 83 |
| search_grounded | 8 | 0.0996 | 0.1111 | 10 / 90 |
| full_verified (D) | 7 | 0.0642 | 0.0641 | 5 / 78 |

Pre-registered caveats apply: contradiction-vs-pool is not all hallucinations; on
contested and time-sensitive topics it partly measures pool-internal conflict; the
judge's compound-partial behaviour pushes some partials into this count.

## §3.3 v1 self-grounding suite

Restated by reference, byte-identical, from the committed
`results/test/20261013T060512Z/evaluation_report.md` (imported values also carried
per-row in this directory's raw_results.json, field-equality guaranteed by test):
claim support 0.0† / 0.631 / 0.584 / 0.671; citation precision — / 0.487 / 0.561 /
0.710; coverage 0.0† / 0.770 / 0.573 / 0.687; unsupported 0.0† / 0.100 / 0.086 /
0.092 († structural).

## §3.4–3.5 Matched machinery and per-category view

The matched-items rule was applied to the primary analysis (above). Per-category:
one item per category per arm — pre-registered as uninterpretable for significance;
the per-question table above is the per-category table for the matched set.

## §3.6 Mandatory contradiction-level inspection (before interpretation)

All 29 externally-contradicted claims were read against their judge quotes.
Patterns, with examples in the inspection record:

- **Genuine corrections — the metric doing its job:** the baseline's reef-"declared
  dead" claim contradicted by *"It's not dead yet, folks"*; hybrid's "Python 3.14 is
  latest" against dated pool passages; numeric discrepancies (29% claimed vs 22% in
  the pool); and grounded arms' wrong meta-claims ("the evidence does not specify
  X") contradicted by passages specifying X.
- **Pool-internal conflict / staleness cross-fire:** the current-information
  question's pool itself contains "3.13.7 is newest", "3.14 is latest" and "3.15 is
  the latest" — every arm's version claim draws a contradiction from some passage;
  omega-3 claims likewise sit in a genuinely conflicted pool.
- **Judge errors, visible and counted:** several contradictions whose quotes agree
  with the claim (e.g., D's MBSR claim contradicted by a quote restating it;
  C's VITAL-no-AF claim contradicted by an agreeing quote) — consistent with the
  judge's documented limitations.
- Judging failures: **0 of 375 claims** across all arms.

## §3.7 Integrity

Pools: 8 `tst-*` pools built once, cached under `pool-v1` with canonical hashes;
identical pool per question for every arm (hashes in every row). Source files:
SHA-256-verified unchanged; pre-launch git verification against committed blobs
passed. Database: zero new runs, reports, or metric rows (behaviour pinned by
tests). Missing cells: the two stage-17 infrastructure losses, named above — never
imputed.

## Interpretation, bounded by the §4 rules

On the frozen test set, judged arm-blind against identical evaluator-owned evidence
pools, **the full AutoResearch system's claims are substantially more externally
supportable than the ungrounded baseline's (+0.21 macro over 6 matched questions,
leading on every question)**, while keeping a comparable external contradiction
rate (0.064 vs 0.048, all-available) — and, from v1, it is the only configuration
whose claims arrive with validated citations, at roughly 19× the baseline's cost.
This is **not** a ground-truth factual-accuracy claim (supportability is bounded by
what the evaluator could retrieve), **not** a statistical-significance claim
(n = 6 matched questions, single pass), and C-vs-D differences remain
uninterpretable per the pre-registration. Limitations carried verbatim: the judge
resolves compound partial-support claims to contradicted (0/2 on labeled cases);
contradiction counts include pool-internal cross-fire on contested and
time-sensitive topics; two product cells were lost to infrastructure in stage 17
and are visible, not imputed.
