# Evaluation pipeline audit — after the first frozen test-set run

**Date:** 2026-10-13 · **Auditor:** project author (AI-assisted) · **Scope:** the
evaluation pipeline that produced `data/benchmarks/results/test/20261013T060512Z/`
(benchmark run `14693741-45ab-4dad-9349-73f761b56754`).
**Constraint honoured:** nothing was tuned, the frozen test set was not altered, no
historical result was rewritten, and no evaluation semantics were changed. Every
correction below is a proposal awaiting approval.

## Files inspected

| Artefact | What was checked |
|---|---|
| `data/benchmarks/results/test/20261013T060512Z/raw_results.json` | every row: claims, verdicts, citation statuses, relevance pairs, errors |
| `…/raw_results.csv`, `…/evaluation_report.md` | agreement with the JSON payload |
| `data/benchmarks/test.json`, `test.freeze.json` | untouched; fingerprint `7f6aff0e2413` matches the manifest |
| `backend/app/services/benchmark/runner.py` | arm wiring, cost/latency accounting, aggregation, comparison, failure handling |
| `backend/app/services/benchmark/metrics.py` + `data/benchmarks/METRICS.md` | formulas, denominators, documentation agreement |
| `backend/app/services/benchmark/relevance.py` | the retrieval relevance judge |
| `backend/app/services/orchestration/prompts.py` | per-arm instructions (synthesis vs model-only) |
| `backend/app/services/claims/` (prompts, models, extractor) | extraction behaviour per arm |
| `backend/app/services/verification/verifier.py` | the per-mode evidence paths, incl. the model-only no-evidence path |
| Live database | traceability spot-check (below) |

## Answers to the audit questions

### 1. Why did the model-only baseline receive zero verifiable claims?

**By construction, not by observation.** `ClaimVerifier._evidence_store()` returns
`None` for `MODEL_ONLY` ("there is nothing to verify against"), and
`_verify_claim()` then records every claim as `not_enough_evidence` **without any
verifier LLM call**. The raw rows confirm it: all seven measured model-only cells
show every verdict in `not_enough_evidence` (e.g. `tst-false_premise-01`: 28 claims,
28 × not_enough_evidence) and empty citation sets. A support rate above 0.000 is
unreachable for this arm. The number is correct under the stated definition
("claims verified against the run's own retrieved evidence") and that definition is
documented in METRICS.md — but the *comparison* on this metric is therefore
partially tautological: it demonstrates the definition, and the empirical content of
the experiment lives in the differences among arms B, C and D. See CD-2.

### 2. Did it actually produce factual claims that the evaluator failed to extract?

**It produced claims, and the evaluator extracted them.** Extraction ran identically
for every arm; the model-only cells yielded 14–28 claims each (124 claims over 7
cells — more than any other arm, consistent with stage 11's observation that the
ungrounded baseline writes the longest, most confident reports). Nothing failed to
extract; the claims were then structurally unverifiable per Q1.

### 3. Did each configuration receive comparable instructions about citations and research quality?

**No — deliberately, and this must be read into the numbers.**
`MODEL_ONLY_SYSTEM` rule 1: *"Do NOT cite anything… There are no sources; inventing
one would be fabrication."* `SYNTHESIS_SYSTEM` rule 2: *"Every sentence that states
a fact must cite its evidence…"* The arms share format/honesty instructions but
differ exactly where the experimental manipulation lives. Two consequences:
(a) `citation_coverage = 0.0` for model-only is structural (the arm was forbidden
citations), like Q1; (b) the baseline's *fabrication* behaviour is unmeasured — a
model-only arm permitted to cite from memory would measure fabricated-citation rate
(`MetricKey.FABRICATION_RATE` exists and is never exercised). Recorded as PC-4,
future work, not a defect of the current design.

### 4. Are missing outputs, malformed citations, and infrastructure failures treated consistently?

**Mostly yes; one asymmetry noted.**
- *Infrastructure failures:* identical across arms — any exception becomes a
  recorded error row with its code, the cell is excluded from means, `failure_rate`
  reports it. Both failures in this run (`tst-citation_verification-01 × D`,
  `tst-current_information-01 × model_only`) are `psycopg.OperationalError` from
  Neon closing in-flight connections; pipeline-attributable failures were zero.
- *Malformed citations:* consistent — markers resolving to no run source are stored
  `fabricated` at extraction and count against citation precision; unvalidated
  citations (from verification-skipped claims) are excluded from the precision
  denominator; `claims_skipped_in_verification` was 0 in every measured cell.
- *Missing outputs:* **asymmetric criterion exists** — evidence arms can fail a cell
  for an uncited report (`UncitedReportError` after one retry); model-only has no
  equivalent failure mode. It did not fire in this run (zero effect on these
  results); recorded as PC-3.

### 5. Are metric denominators correct, including questions with no eligible claims?

**Yes, with one aggregation-level caveat.** Per-metric: empty denominators yield
`None`, not zero (`metrics._ratio`), and `None`s are excluded from means with `n`
reported — verified in the outputs (model-only citation precision shows "—", n per
metric varies). No cell in this run had zero extracted claims. The caveat is
**CD-1**: because one D cell and one model-only cell failed, each arm's means cover
a *different 7-item subset* (D is missing the citation-verification question — its
hardest citation-integrity item; model-only is missing current-information). The
headline baseline-vs-AutoResearch table compares means over non-identical question
sets. With structurally-zero baselines the headline conclusions survive, but the
table nowhere says so.

### 6. Can every reported metric be traced to raw observations?

**Yes — verified by independent recomputation.** Spot-check
(`tst-normal_research-01 × D`, run `…` from the row's `research_id`): claim count,
verdict counts, support rate (0.6364) and citation precision (0.8333) recomputed
from `claim` / `verification_result` / `citation` rows match the reported row
exactly; the aggregate means recompute from the rows (macro-average confirmed for
two arms); the relevance judge's raw pairs are stored per row; `evaluation_result`
rows carry `metrics-v1`. The chain file → row → database → chunk text is intact.

### 7. Are any metrics mislabeled, misleading, or mathematically incorrect?

**No arithmetic errors found.** Found and classified:
- *Misleading without annotation* (CD-2): the comparison table classifies
  `unsupported_claim_rate` as "worse" for AutoResearch against the baseline's 0.0 —
  mechanically correct, semantically incomplete: a system that cites nothing cannot
  be contradicted by its citations. The stage-17 log explains this; the generated
  report does not.
- *Documentation gap* (CD-3): aggregates are **macro-averages** (items weighted
  equally regardless of claim count); METRICS.md does not say so. Micro vs macro for
  D's support rate: 52/78 = 0.667 vs 0.671 — immaterial here, unbounded in general.
- *Label deviation, documented* (PC-2): `precision@K` divides by `min(K, retrieved)`
  rather than K, stated in the docstring and METRICS.md.
- *Low discriminative power, not an error* (PC-1): retrieval recall@K = 1.0 for all
  arms because items carry only 1–2 `expected_evidence` statements, which top-4
  retrieval covers easily.
- *Inert rule* (PC-5): `partially_supported` counts as support, but the verifier
  returned zero partials in all 78 verified D-claims (and across the run); the rule
  had no effect on these numbers.

## Confirmed defects (all in reporting/comparability; none in stored data or arithmetic)

| ID | Defect | Smallest safe correction (proposal — not implemented) |
|---|---|---|
| CD-1 | Arm means cover non-identical item subsets when cells fail; the comparison table does not say so | Report generator additionally emits means over the intersection of items completed by both compared arms, and footnotes which items each arm is missing. Test: a synthetic payload with one failed cell produces the intersection table. |
| CD-2 | Structurally-determined values (model-only support, coverage, unsupported rate) presented without annotation; "worse" classification on a structurally-zero baseline is semantically misleading | Report generator marks structural cells (e.g. "0.0 *(structural: arm retrieves no evidence / is forbidden citations)*") via a per-arm/metric structural map. Test: generated report contains the annotations for model_only rows. |
| CD-3 | Aggregation method (macro-average) undocumented | One paragraph in METRICS.md; optionally also report micro-averages alongside. Test: extend the code-docs agreement test. |

All three change **report presentation/documentation only** — no formula, no stored
row, no historical output. The existing `results/test/20261013T060512Z/` files are
not regenerated; corrections apply to future runs' reports.

## Possible concerns (not defects)

- **PC-1** recall@K saturates at 1.0 (1–2 expected statements per item) — a future
  dataset version could carry more statements; the current frozen set stays as is.
- **PC-2** precision@K denominator deviation (documented).
- **PC-3** uncited-report failure path exists only for evidence arms (no occurrence
  in this run).
- **PC-4** baseline fabrication behaviour unmeasured by design; an additional
  "model_only, citations permitted" arm would measure `FABRICATION_RATE`.
- **PC-5** partially_supported verdicts never occurred; the support definition's
  partial-counting clause was inert in this run.

## Tests run during the audit

No production code was changed, so no new tests were added; evidence was gathered by
re-reading the committed outputs and recomputing from the live database (scripts in
the session scratchpad, reproduced in this document's figures). The existing suite
(832 tests, including the formula hand-computations and the code-docs agreement
test) was green at the time of the evaluated run.

## Limitations of this audit

- One traceability spot-check cell was fully recomputed; others were checked at the
  aggregate level only.
- The relevance judge's labels were audited for storage and plumbing, not re-judged
  for quality — judging the judge needs human-labelled pairs, which do not exist yet.
- The audit covers the evaluation pipeline, not report *content* quality (whether a
  "supported" verdict is correct is the verifier's measured behaviour, audited in
  stage 13's enforcement, not here).

## Recommended next prompt

> Approve (or amend) corrections CD-1, CD-2 and CD-3 — intersection-subset
> comparison, structural-value annotations, and aggregation documentation — as
> report-generation changes only, with the proposed tests, leaving all formulas,
> stored rows and historical outputs untouched.
