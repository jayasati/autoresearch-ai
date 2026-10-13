# Final analysis plan — frozen test set

**Status: LOCKED (2026-10-13, Revision 3) — Option B selected on the reviewer's
recommendation.** Every analysis below is pre-registered: it will be computed and
reported regardless of how the numbers come out, nothing else will be added after
seeing results, and any deviation requires a visible amendment to this document
before the run. **Locking authorizes building the §6 implementation and its tests
only; launching the final analysis requires separate, explicit approval.**

*Revision 2 (2026-10-13): methodological clarifications §3.1 (matched-primary),
§3a (averaging), §3b (missing cells vs failed judgments), §4a (operational
arm-blinding), §6 (test obligations split into existing vs to-be-built).*
*Revision 3 (2026-10-13, at lock): §3.1 — A and D share one matched denominator,
with per-arm exclusion reasons and actual n reported; §6(b)/(c) — "byte-match"
precised: source files verified byte-unchanged by SHA-256, imported v1 values
tested for exact field-level equality; the new report is not expected to be
byte-identical to the old document. No change to the judge, metric formulas, pool
semantics, frozen-test identifiers, historical results, or the §8 forbidden list.*

## 1. The central design decision (requires your choice)

The frozen test set's **product runs already happened once** — stage 17, benchmark
run `14693741…`, results in `results/test/20261013T060512Z/` (v1 metrics only;
metrics-v2 did not exist then). Stage-15 governance says the test set runs once per
final configuration, and the product configuration is unchanged since. Two options:

**Option B — recommended: evaluate the existing frozen-test runs with metrics-v2;
no new product runs.**
The stage-17 runs' claims are all stored (every arm's claims were extracted and
verified then). The final analysis builds the eight **test-question external pools**
(evaluator-owned, `pool-v1` semantics, cached with canonical hashes — pool building
reads only the questions, never the test set's ground truth or any report) and
judges those stored claims against them, arm-blind, with the unchanged
`external-verification-v1` judge. The product exposure of the frozen set stays
exactly one; v2 is a pure evaluation addition over immutable artifacts.
*Costs/limits:* stage 17's two infrastructure-lost cells stay lost (D and the
baseline each miss one item; §3b defines the handling); requires a small approved
code addition — a re-evaluation mode that judges stored claims without creating
runs (implementation and tests in §6, built only after this plan locks).

**Option A — alternative: a full fresh grid (new product runs + v1 + v2).**
Cleaner single-table reporting and a chance to recover the two lost cells, at the
cost of a second product exposure of the frozen set. If chosen, the run is labelled
**"frozen-test exposure 2 (same configuration)"**, stage 17's results are reported
alongside it unchanged, and neither is presented as the other.

Everything below is written for Option B; §7 notes the deltas if Option A is chosen.

## 2. Inputs, all pinned before the run

| Input | Identity |
|---|---|
| Test set | v1.0.0, fingerprint `7f6aff0e2413` — loaded under freeze enforcement, not modified |
| Product runs | stage-17 benchmark run `14693741…` (30/32 cells), artifacts committed at `9c2eece` |
| v1 metrics | the committed `results/test/20261013T060512Z/` files, reported unchanged |
| Judge | `external-verification-v1`, **unchanged**, with its labeled validation on record (9/11; 0/2 on compound partials) |
| Pools | `pool-v1` semantics; the 8 `tst-*` pools do not exist and are built once at run time, cached + hashed; never refreshed after |
| Metrics | `metrics-v2` definitions as committed in METRICS.md — no formula changes |

## 3. Pre-registered analyses (computed and reported regardless of outcome)

1. **Primary analysis: external_support_rate, A versus D, over the MATCHED test
   questions.** A question belongs to the matched set only when **every** of the
   following holds for **both** A and D: a completed stage-17 product cell, an
   available external pool (pool availability is per-question and therefore reduces
   both arms symmetrically), and at least one successfully judged claim for that
   arm on that question. **A and D use the identical matched set as the primary
   denominator — never different question sets.** With stage 17's known failures
   (A missing `tst-current_information-01`, D missing
   `tst-citation_verification-01`), the matched set is expected to be 6 of 8
   questions; the **actual** matched list is reported by id, together with every
   excluded question and its exclusion reason (missing cell / unavailable pool /
   zero judged claims, per arm), and the actual n. The comparison reports the
   macro-averaged difference (see §3a) computed over that one matched set for both
   arms, per-question values for every matched question, and micro-averages over
   the same matched set as the secondary view.
   **The all-available-items analysis (each arm over its own completed cells) is
   reported separately and labelled secondary**, with each arm's item subset named.
2. **Secondary:** external_contradiction_rate (all arms, matched and all-available
   views) — reported with the pre-registered semantic caveat that on contested
   topics it partly measures pool-internal conflict (stage-21 inspection), and with
   the judge's partial-support limitation restated.
3. v1 self-grounding suite: restated from the stage-17 files, byte-identical.
4. Matched-items machinery (CD-1) applied to every cross-arm table, naming missing
   items.
5. Per-category table (8 categories × 1 item): descriptive only — pre-registered as
   uninterpretable for significance at n=1 per cell.
6. **Mandatory post-run contradiction-level inspection** (stage-21 method): every
   externally-contradicted claim read against its quote, classified
   (contested-pool cross-fire / meta-claim / judge error / genuine correction),
   before any interpretation is written.
7. Judging integrity stats: claims judged, judging failures, pool hashes, per-metric
   eligible-question counts.

### 3a. Averaging — definitions and the primary convention

- **Macro-average (PRIMARY):** compute the metric per question (e.g., that
  question's supported + partially_supported judged claims ÷ that question's judged
  claims), then take the unweighted mean over eligible questions. Every question
  counts equally regardless of how many claims its report produced.
- **Micro-average (reported alongside, secondary):** pool numerators and
  denominators over all eligible questions first, then divide once. Questions with
  more claims weigh more.
- A question is **eligible** for a metric when the metric is defined for it
  (non-empty denominator, pool available, product cell completed). Every reported
  mean carries its eligible-question count (n); per-question values for the primary
  comparison are tabulated in full, so both averages can be recomputed by a reader.

### 3b. Missing product cells vs failed external judgments — separate categories,
separate reporting

- **Missing product-run cell** (a stage-17 infrastructure failure): the question is
  ineligible for that arm. It is named in every affected table (CD-1 machinery),
  excluded from that arm's denominators, and drives the matched-versus-all split in
  §3.1. It is never imputed.
- **Failed external judgment** (the judge's structured output fails validation
  twice for one claim): the claim is recorded in `external_judging_failures` with
  its claim id, **excluded from both numerator and denominator** of every v2
  metric, and **never defaulted to any verdict — not unsupported, not contradicted,
  not not_enough_evidence.** (This is the committed behaviour of
  `judge_claims_against_pool`: a failed claim's verdict field is null and the
  verdict counts exclude it — pinned by
  `test_a_twice_failing_claim_is_a_failure_not_a_verdict`.)
- **Unavailable pool** (both providers fail or no usable text): the question is
  ineligible for v2 metrics for **all** arms symmetrically; recorded as an explicit
  `unavailable` status with the reason; never a zero.
- The report states all three counts separately; none of the three categories is
  mixed with genuine `not_enough_evidence` verdicts, which are measurements.

### 4. Pre-registered interpretation rules

- No factual-accuracy claim from supportability; no statistical-significance or
  "reliable superiority" claim at this n — differences reported descriptively with
  per-question values.
- Structural v1 baseline zeros stay footnoted; the unsupported-rate single-metric
  trap is restated wherever that row appears.
- The judge's 0/2 compound-partial behaviour appears in the limitations section of
  the final write-up, verbatim from the validation record.
- C-vs-D differences are pre-declared uninterpretable (documented run variance).
- Dev (+0.117) and validation (+0.096) external-support gaps are the only repeat
  context and will be shown next to the test number for consistency-of-direction —
  explicitly not pooled, and those were all-available-items macro figures, compared
  to the test run's corresponding all-available view, not to the primary matched
  view.
- Failed/missing cells visible everywhere; no silent exclusion.

### 4a. Arm-blinded judging, operationally

- **Inputs to the judge, exhaustively:** the claim text and, per pool passage, a
  fixed-format block `[P<i>] (from: <source title or URL>)` + passage text — built
  by `build_verification_prompt`, the same function run-owned verification uses. No
  arm name, mode, run id, report text, or metadata beyond the passage's own source
  title reaches the prompt; the judge cannot ask for more (it answers over passage
  labels). This is the already-implemented mechanism, pinned by the existing test
  that judging prompts contain no arm identifiers.
- **Standardized format:** claims are submitted as bare claim text in stored-claim
  order, identically for every arm; passages appear in pool-retrieval rank order
  for the claim; no per-arm formatting differences exist anywhere in the input
  path.
- **Acknowledged residual channel:** blinding removes *labels*, not *style*. Claim
  wording can still reveal its source — the baseline's claims read as textbook
  prose, and verification-aware reports produce meta-claims ("the evidence is
  conflicting…") that only grounded arms write. No operational fix is proposed;
  the limitation is pre-registered and will be stated in the final write-up's
  limitations section.

## 5. Deliverables

A new directory `data/benchmarks/results/test/<timestamp>-v2eval/`:
`raw_results.json` (per-claim external verdicts, pool hashes, provenance),
`raw_results.csv`, `evaluation_report.md` (tables incl. matched-items), plus
`FINAL_ANALYSIS.md` (the pre-registered analyses of §3 and nothing else), a
`MANIFEST.sha256`, 8 committed `tst-*` pool caches, and a DEVELOPMENT_LOG entry.
Stage-17 artifacts are not touched, regenerated or re-stated as new.

## 6. Implementation to be built after lock (Option B), with test obligations

A re-evaluation entry point: `python -m app.services.benchmark.runner
--set test --reevaluate 14693741-...` — for each completed cell of the named
benchmark run: load its stored claims (by report), build/load the question's pool,
judge arm-blind, compute v2 metrics (matched-primary per §3.1, both averages per
§3a), merge the stage-17 v1 numbers (read from the committed files, not
recomputed), and write the §5 outputs. Creates no research runs, writes no
run-owned rows.

**Test obligations, honestly split:**

*Already implemented and passing (stages 19–21):* deterministic pool reuse without
provider calls; pool canonical-hash validation and tamper refusal; failed judgments
recorded as failures with null verdicts, excluded from denominators, never
defaulted; pool-unavailable as explicit status, never zero; arm-identifier-free
judge prompts; v1 formulas and outputs unchanged.

*To be built with the §6 implementation (do not exist yet — stated as obligations,
not as current safeguards):* (a) re-evaluation creates **zero** new research runs,
reports, or run-owned database rows (asserted by row counts before/after against
the test database); (b) the stage-17 artifact **files themselves** are
byte-unchanged after a re-evaluation (SHA-256 of the source files verified before
and after — the new report is a different document and is **not** expected to be
byte-identical to the old one); (c) the **imported v1 metric values** in the new
output are exactly equal to the values in the committed stage-17 rows (field-level
equality test on the imported values, not whole-file comparison); (d) a partial
re-evaluation failure (one cell's judging aborts) is visible in the outputs and
recoverable by re-running, with already-judged cells reused rather than re-spent;
(e) matched-primary and all-available views compute from the same payload and agree
with hand-computed values on a synthetic fixture, including the rule that A and D
share one matched denominator.

## 7. If Option A is chosen instead

The standard runner command (`--set test --depth quick`) executes a fresh grid
labelled exposure 2; §3–§5 apply unchanged (the matched-primary rule then operates
on exposure-2's own completed cells) plus a pre-registered exposure-1 vs
exposure-2 consistency table (v1 metrics, both exposures, same configuration —
a variance measurement, not a do-over).

## 8. Forbidden throughout

Modifying the test set, ground truth, prompts, verifier, judge, formulas or pool
semantics; refreshing any existing pool; re-running after seeing results; selective
reporting; presenting validation or development numbers as test numbers; committing
or launching anything before this plan is LOCKED and the launch separately approved.
