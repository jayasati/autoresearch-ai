# Report-quality pipeline v1 (`report-quality-v1`) — design record

**Status: approved to proceed in stages (2026-10-16). Stage S0 only is
implemented** (this document, `PipelineVersion`, inert configuration knobs,
documentation corrections — see `DEVELOPMENT_LOG.md`, stage 27). Nothing in this
document describes behaviour the system has today unless it says "existing".

Goal: more research breadth and depth **without** giving up claim-level evidence
support, citation traceability, or explicit uncertainty handling.

## 0. Ground rules (approved)

- **Opt-in, versioned.** `legacy` stays the default for every request, route,
  benchmark arm and script. The new behaviour is reachable only through
  `pipeline="report-quality-v1"` (introduced in a later stage) **and** the
  server-side gate `REPORT_QUALITY_ENABLED` (off by default).
- **Nothing historical changes.** Frozen test data (`test.json`,
  `test.freeze.json`), every committed benchmark result, the evaluator-owned
  pools, `FINAL_ANALYSIS_PLAN.md`, metrics-v1/v2 definitions, the default
  benchmark `ARMS`, verification-v2 and claim-extraction-v1 are untouched.
  Existing API contracts stay backward-compatible.
- **SEARCH_GROUNDED is not the verified pipeline.** `/start` returns an
  unverified draft built from snippets and abstracts; verification is the separate
  claims → verify → finalize path. Nothing may describe the two as equivalent.
- **`citation-check-v1` is separate from `verification-v2`.** No silent change to
  the verifier; no claim that a citation is verified when only the claim was.
- **Quantitative findings are audit-only in v1.** They never change a verdict,
  never delete a claim, never appear in report text.
- **No silent source substitution.** A claim whose cited passage does not
  support it is moved to a clearly labelled section; the renderer never swaps in
  a different source's marker.
- **Opposing evidence is a possible scientific disagreement,** not proof that a
  claim is false. It is presented as contested, with what each side states.
- **Every number in this document is a hypothesis to measure** — alignment
  accuracy, quantitative-audit precision, quality thresholds, cost and latency
  figures. None is an established result.
- **No paid evaluation is authorized yet** (development or validation).

## 1. What the existing verifier does (traced, stage 26 HEAD)

1. Per claim, `ClaimVerifier._verify_claim` runs `store.search(claim_text, 4)`
   over the run's **whole** collection (every processed chunk, not only the
   passages shown to the synthesizer). The claim's citations play no part in
   which passages are retrieved.
2. One LLM call (verification-v2) returns a verdict plus a per-passage relation
   and quote; code keeps a supports/contradicts relation only when its quote is a
   verbatim substring of that chunk, and downgrades an anchorless support verdict.
3. Citation outcome: `valid` when *any* of those 4 chunks that belongs to the
   **cited source** carries an enforced SUPPORTS relation; otherwise
   `misattributed`; an unknown marker was already `fabricated` at extraction.

`[S#]` labels identify a **source**, not a passage (every chunk of a source shares
its label), so the passage a draft sentence relied on is not recorded anywhere.
Existing citation validity therefore means *some passage of the cited source,
found by similarity to the claim, was quoted as supporting it* — not that the
passage the writer used supports it. Pool verdicts and the legacy
`citation.status` keep exactly that meaning.

## 2. Pipeline stages

```
topic
 1+2 analyze_and_plan ─LLM─▶ QuestionAnalysis + CoveragePlan ─▶ sub_question rows (existing table)
 3   retrieve per subquestion (existing Tavily/S2 services + cross-channel dedup)
 4   source metadata profile (code; provider fields only; no quality score, no ranking effect)
 5   process + index (existing); select per subquestion (search_mmr); freeze S# labels;
     passage aliases E#; QuantityMention extraction (code)
 10a structured draft ─LLM─▶ sentences carry passage aliases; code renders [S#]
 6   ClaimExtractor (existing, claim-extraction-v1) + deterministic claim→sentence alignment
 7   ClaimVerifier (existing, verification-v2, unchanged)
 7b  citation-check-v1: each aligned claim vs ONLY its cited passages
 8   quantitative audit (code, audit-only)
 9   contradiction & uncertainty aggregation (code)
 10b structured revision ─LLM─▶ sentences carry claim ids + passage aliases
 11  final validation (code): references, citation support, meaning-change re-verification,
     placement, omission restore, outcome status
 12  rendered user-facing report  +  separate internal ReportQualityAudit
```

| # | Stage | LLM | Failure behaviour |
|---|---|---|---|
| 1–2 | Analysis + coverage plan | yes (one call) | Invalid twice → single-subquestion plan (the topic), `plan_fallback=true`, warned; run continues |
| 3 | Retrieval | no | Existing policy per call; run fails only if every provider call fails; an empty subquestion becomes `evidence_gap(no_evidence)` or `evidence_gap(provider_failure)` |
| 4 | Source metadata | no | Missing fields stay null, never inferred |
| 5 | Evidence | no | Existing `NoEvidenceError` if the run has zero chunks |
| 10a | Draft | yes | Invalid → one retry → finalization path not entered; existing `UncitedReportError` if nothing is cited |
| 6 | Claims | yes (existing) | Existing; overflow beyond 50 is recorded, not silently discarded |
| 7 | Pool verification | yes (existing) | Existing per-claim skip → recorded `not_evaluated(technical)` |
| 7b | Citation check | yes | Per claim; failure → `not_evaluated(technical)` |
| 8 | Quantitative audit | no | Parse failure skips that claim, counted |
| 9 | Disagreement & uncertainty | no | Pure aggregation over stored rows |
| 10b | Revision | yes | Invalid → one retry → `finalization_failed`, draft kept |
| 11 | Validation | no | Deterministic repair; every action recorded |
| 12 | Report + audit | no | Audit write failure fails finalization (no unaudited final report) |

**Stable identity.** Models see only short aliases (`Q#`, `S#`, `E#`, `C#`); a
code-owned alias table maps them to `sub_question.id`, `source.id`, content-derived
chunk UUIDv5s and `claim.id`, and is stored in the audit. `S#` labels are frozen
at stage 5. An alias the model emits that is not in the table is a validation
error. Titles, URLs, DOIs, years, venues and authors come only from `source` rows;
no model schema has a field that can carry them.

## 3. Citation-specific verification (`citation-check-v1`)

- **Passage identity:** the structured draft cites passage aliases `E#`; code
  renders the visible `[S#]` of the passage's source, so the report format and
  `CITATION_PATTERN` are unchanged while the cited chunk is recorded.
- **Claim→sentence alignment (deterministic, keeps claim-extraction-v1):** a
  claim aligns to the draft sentence whose markers contain the claim's
  `citation_labels`, highest local-embedding cosine, with a margin ≥
  `REPORT_QUALITY_ALIGNMENT_MIN_MARGIN` over the runner-up. No alignment →
  `not_evaluated(unaligned)`; never guessed. Alignment accuracy is measured.
- **The check:** per aligned claim, one call over **only its cited passages**,
  reusing the verification-v2 system prompt, schema and enforcement unchanged,
  recorded under procedure version `citation-check-v1`. No verification logic is
  duplicated; the procedure, not the prompt, is what is new.
- **Shortcut:** when pool verification already holds an enforced SUPPORTS
  relation with a verbatim quote on the **exact cited chunk**, the citation is
  established without a second call (`established_by="pool_verification"`).

Each claim carries two independent results:

- pool (verification-v2): `supported | partially_supported | contradicted |
  unsupported | not_enough_evidence | not_evaluated(reason)`
- citation, per cited passage (citation-check-v1): `supports |
  partially_supports | contradicts | does_not_establish | invalid_reference |
  not_evaluated(reason)`

| Shown as | Rule |
|---|---|
| Supported by the cited source | pool ∈ {supported, partial} and ≥1 cited passage supports / partially supports |
| Supported by other evidence — citation not confirmed | pool ∈ {supported, partial}, no cited passage supports. **Moved to a labelled section; the cited marker is not rendered and no other source is substituted** (approved). |
| Contested — sources disagree | pool = contradicted, or a cited passage contradicts. Presented as a possible scientific disagreement with what each side states, never as established falsity |
| Insufficient evidence | pool ∈ {unsupported, not_enough_evidence} |
| Not evaluated / technical failure | either result `not_evaluated`, reason shown |

## 4. Quantitative audit (`quant-audit-v1`, audit-only)

Extraction (`QuantityMention`, regex + fixed tables): value and interval (ranges,
±, CIs), stated precision, approximation words, normalized unit and dimension (%,
percentage points, currency, mass, rate per N, multiplier), denominator, period,
geography (plan entities + fixed list), baseline phrases, method words
(mean/median, nominal/real/PPP, market/book). Unknown is `None` and **never equals**
anything.

Pairs: claim–claim (grouped by masked-number embedding similarity), claim–own
supporting quote, claim–cited passage. First matching rule wins:

1. different dimension, % vs pp, or different denominator → `unit_or_denominator_difference`
2. both periods (or baselines) known and different → `baseline_or_period_difference`
3. both methods (or geographies) known and different → `method_or_scope_difference`
4. intervals overlap under rounding tolerance (half the least precise digit) and
   `REPORT_QUALITY_QUANT_APPROX_REL_TOLERANCE`, or point inside a stated range →
   `consistent_within_rounding_or_uncertainty`
5. every dimension explicitly known and equal, values do not overlap →
   `same_metric_value_disagreement`
6. otherwise → `unresolved_discrepancy` (never presented as a contradiction)

Evaluation: a new hand-labelled case set (target ~80 pairs: constructed cases
per category including hard negatives, plus pairs sampled from **development**
data only), labelled before the auditor is run on it, split into tuning and
held-out halves. Precision and recall per category on the held-out half, with
counts and Wilson 95% intervals. Results are reported as "measured on N cases",
never as general reliability.

**S1 implementation readings (stage 28; recorded for review).** Where the rules
above left room, the implementation (`app/services/quality/quantities.py`) reads
them as follows:

- *Rule 5, "every dimension explicitly known and equal"*: required dimensions are
  the unit, the period and the scope (at least one of place / population / care
  setting / entity stated on both sides, none stated on one side only); the
  optional qualifiers — baseline and method — must be equal or absent on **both**
  sides. A literal reading (method stated on both sides) would almost never fire.
- *Rule 1*: a denominator stated on one side only counts as a denominator
  difference (per capita vs total, annual vs cumulative); the same number written
  with different units of one dimension (500 mg vs 500 g, 60 km/h vs 60 mph) is a
  unit difference, not a value disagreement.
- *Rule 2*: periods differ only when both are stated in a comparable form; a
  duration ("over five seasons") and a calendar span are not compared.
- *Scope* includes a care setting / endpoint vocabulary (outpatient,
  hospitalisation, ...), added on the tuning split.
- *Attribution*: context is attached to the nearest quantity in the sentence; a
  context phrase before the first quantity frames all of them; ties go to the
  preceding quantity; denominators attach to the quantity they follow.

**Development diagnostics (stage 28) — NOT independent validation, and not the M3
result.** The numbers below describe how the auditor behaves on cases its own
author wrote; they do not establish its reliability on unseen text. The labeller
(Claude) also wrote the rules and **saw every case, the held-out split included,
during rule development** — the held-out split therefore guards only against
tuning after its single run, not against the author's knowledge of its contents.
Labels were written before any auditor code existed (case file sha256
`f1a83873…`, recorded first). 82 cases, seeded label-stratified split 39 tuning /
43 held-out (`floor(n/2)` per label sends the odd case to held-out). Rule changes
were made against the tuning split only; the held-out split was run once, on the
frozen auditor (`quantities.py` sha256 `128d6a19…`):

| split | all | constructed | dev-pool text | predicted disagreements correct |
|---|---|---|---|---|
| tuning | 35/39 | 24/24 | 11/15 | 5/5 |
| held-out | 38/43 | 23/23 | 15/20 | 5/5 |

Constructed cases share an author with the rules and say little; the
development-pool cases (real retrieved text) are the informative part, and they
are where every miss is. All misses are conservative (a disagreement or difference
filed as `unresolved`) except one consistent-for-scope error (heart disease vs
heart disease *or* stroke). Held-out `same_metric_value_disagreement` precision
5/5, Wilson 95% [0.566, 1.0]; with n this small that interval, not the point
estimate, is the result. Artifacts: `data/benchmarks/results/quant-audit/`.

Limitations: regex misses spelled-out numbers, multi-sentence quantities,
tables and implicit denominators, and can mistake date or citation fragments for
quantities; the geography and method lists are incomplete by construction;
embedding similarity conflates related metrics (incidence vs prevalence) and
splits paraphrases; snippets and abstracts often omit period or scope, so
`unresolved` will be common.

## 5. Final-report validation

The revision returns sentences with `claim_ids`, `evidence_ids`, `kind`
(finding / qualification / uncertainty / transition) and `finding_refs`. Code
validates, repairs and renders; every action is recorded with the original text.

| # | Check | Action |
|---|---|---|
| V1 | unknown claim/evidence alias, or a source outside the alias table | reference rejected — fabricated references are never rendered |
| V2 | meaning change vs the sentence's claims: number multiset, negation, hedging/qualifier words, scope tokens, cosine < `REPORT_QUALITY_MEANING_CHANGE_MIN_COSINE` | re-verify the revised text (pool + citation check), stored with lineage to the original claim |
| V3 | citation support | render `[S#]` for an `E#` only when that exact chunk has an established supports/partial relation for the sentence's claim; otherwise the marker is not rendered |
| V4 | placement by status (§3 table) | body / qualified / labelled sections — nothing is deleted outright |
| V5 | finding with no claim ids; a "transition" with a number, entity or citation | moved to Unresolved as an untraceable statement |
| V6 | repetition | flagged only in v1 |
| V7 | a high-importance, cited-source-supported claim missing from the body | restored as a code-templated sentence |

Outcome statuses are distinct: **run failed** (existing `research_run.status =
failed`, no report); **finalization failed** (run completed, draft kept and shown
as "draft — not verified", no final report); **degraded** or **complete** (a final
report exists; `degraded` carries a code-built banner with its reasons).

`body_loss_ratio` = finding/qualification sentences proposed by the revision that
did not end in the body ÷ finding/qualification sentences proposed (transitions
excluded; V7 restorations do not reduce it). Degraded when it exceeds
`REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO` (0.10), any high-importance claim is
unresolved or unevaluated, or verification coverage < 1. Finalization fails when
it exceeds `REPORT_QUALITY_FAIL_BODY_LOSS_RATIO` (0.25), no sentence is supported
by its cited source, core-subquestion substantive coverage is below
`REPORT_QUALITY_MIN_CORE_COVERAGE` (0.5), or a rejected reference survives
rendering. All thresholds are configurable, copied into the run configuration,
tuned on development only and frozen before validation.

**Meaning-change detection (V2) is heuristic** (recorded at S2). It compares
number multisets, fixed negation, hedge and quantifier word lists, stated context
facets and, when an embedder is supplied, a cosine threshold. These catch common,
surface-visible changes; they do not understand meaning. Passing every check means
only "no change detected" — never that the rewritten sentence provably means what
the verified claim meant. A reworded causal claim, a swapped subject or a qualifier
outside the word lists can pass, and a high cosine is similarity, not equivalence.

**Safety invariant (approved at S2).** A sentence flagged for re-verification is
not treated as verified in the final report until a re-verification has actually
run and succeeded against the exact evidence the sentence cites. Enforced in three
places: validation keeps it out of the body (`not_evaluated` until a result is
supplied, and a result's markers render only for supporting chunks the sentence
itself cites, else it moves to the citation-not-confirmed section); the store
refuses a `supported_by_cited_source` result whose supporting chunks are not among
the cited chunks, and never overwrites a decided result; and the database refuses
support without a support verdict, a model and a prompt version.

Implementation note (stage 28): a sentence awaiting re-verification counts as
*not in the body*, so the outcome computed on the first validation pass is
provisional — final only once the caller has re-verified those sentences and
validated again (`ValidatedReport.needs_reverification` empty). `reasons` lists
every failing and degrading condition, not only the deciding one.

## 6. Cost and execution safeguards

Per-depth hard budgets (`REPORT_QUALITY_MAX_LLM_ATTEMPTS_*`,
`REPORT_QUALITY_MAX_TOTAL_TOKENS_*`, `REPORT_QUALITY_MAX_CLAIMS_VERIFIED_*`,
`REPORT_QUALITY_RUN_DEADLINE_SECONDS_*`). A budgeted LLM wrapper disables SDK
retries (today they are invisible to `calls_made` and to reported usage) and
performs retries itself, each checked against the budget **before** sending, so a
retry can never bypass it. The revision and a re-verification reserve
(`REPORT_QUALITY_REVERIFY_RESERVE_FRACTION`) are reserved before verification
starts; a run that cannot fit its minimum is refused before any paid call.

Verification is **never reduced silently**: claims past the cap, past the
extractor's overflow, or interrupted by budget or deadline are recorded as
`not_evaluated(reason)`, listed in a "Not evaluated" section, counted in
verification coverage, and make the report degraded. Priority when the cap binds:
high importance, then core subquestion, then reading order — recorded.

Per-stage accounting (attempts, calls, retries, tokens, estimated USD, wall
time, provider calls/failures/timeouts) is stored in the audit; totals must equal
the stage sum. Long runs go through an asynchronous job endpoint with progress
polling (`/start` stays synchronous and legacy-only); interrupted in-process jobs
are swept to `failed(interrupted)` at startup.

Cost estimate (unmeasured hypothesis, scaled from stage-19 arm D at quick depth ≈
30k tokens, 78 s, $0.0063): quick ≈ 70–95k tokens, 150–240 s, hard-capped by the
quick budget. Measured per-stage accounts replace this at development evaluation.

## 7. Final-text metrics (`metrics-v3`, additive)

Draft-claim metrics (v1/v2) are unchanged and, for the new arm, labelled as draft
metrics. Each v3 metric is computed per run; aggregates report macro (primary,
mean over questions) and micro (pooled), with `n_runs_with_metric` and
`n_runs_failed`; nulls are never imputed.

| Metric | Unit | Numerator / denominator |
|---|---|---|
| M1 final_sentence_traceability | final-body finding/qualification/uncertainty sentence | sentences with ≥1 valid claim id of an admissible status and ≥1 rendered marker / all such sentences |
| M2 citation_specific_support_rate | rendered (sentence, marker) pair | pairs whose exact chunk has an established supports/partial relation / all pairs; strict and evaluated-only variants |
| M3 quantitative_audit_quality | labelled pair (case set) | per-category precision and recall, held-out half |
| M4 planned_dimension_coverage | core subquestion | substantive (≥1 M1-traceable body sentence) and accounted (substantive or labelled evidence gap) / core subquestions |
| M5 important_claim_omission_rate | high-importance, cited-source-supported draft claim | not represented before V7 / such claims; plus restored and relocated counts |
| M6 verification_coverage | claim (including overflow and re-verifications) | evaluated / total; plus technical-failure, budget-skip, deadline-skip, citation-check and alignment-failure rates |

Regression scenario (required test): a draft sentence verified as supported
("may reduce … by 15% in adults") is revised to a changed number, a removed hedge
and a widened scope, plus one pure paraphrase. The three changes must trigger
re-verification and keep the changed meaning out of the body unless re-verified;
the paraphrase must not trigger re-verification.

## 8. Schema changes (S2; none before it)

One additive migration: `run_configuration.pipeline_version` and `options`
(fingerprint includes them only when non-null, so legacy fingerprints are
unchanged); new tables `report_quality_audit`, `claim_audit`, `citation_check`,
`final_claim_check`. Requirements that cannot be met without it: passage-level
citation storage, citation verdicts separate from `citation.status`, explicit
not-evaluated claim states, and re-verification lineage that does not pollute
draft-claim metrics. Also required (no migration): a behaviour-identical
extraction of the verifier's judging step from its persistence, an extractor
overflow return, and a budget-aware LLM wrapper.

**Implemented at S2 (stage 29), D1 = full design set.** Migration `ed421047b86d`
(`20261017_0915_…_report_quality_v1.py`), additive only: the two nullable
`run_configuration` columns with two CHECKs (legacy is NULL, never the string;
`options` need a pipeline) and the four tables, each with CHECKs for what SQL can
state (a not-evaluated row names its reason; a quoted relation names its procedure;
support for a rewritten sentence needs a support verdict, a model and a prompt
version; an audit is never legacy). `compute_fingerprint` hashes `pipeline_version`
and `options` only when set, so legacy fingerprints are byte-identical (pinned by a
test). The store (`app/services/quality/store.py`) adds what SQL cannot: claim
audits agree with the stored verdict, quotes are verbatim, a pool-verification
shortcut reuses a stored evidence row on that exact chunk, and a rewritten
sentence's support must come through a chunk it cites. The status vocabularies
moved to `app/core/constants.py` (re-exported unchanged) so the model layer does not
load the pipeline package. Verified on SQLite (upgrade, model/migration parity,
round trip) and as offline PostgreSQL SQL; **not yet applied to any PostgreSQL
database** — the Neon-branch round trip remains an open acceptance item that needs
the user's approval.

## 9. Staged sequence

| Stage | Scope | Paid calls |
|---|---|---|
| **S0** | this document, `PipelineVersion`, inert knobs, documentation corrections | none — **done (stage 27)** |
| S1 | deterministic modules (quantities, assessment, validation, rendering, alignment) + local quantitative case-set evaluation | none — **done (stage 28)** |
| S2 | migration + persistence | none — **done (stage 29)**; Neon-branch round trip pending approval |
| S3 | planner + per-subquestion retrieval/selection behind the gate, fake providers | none |
| S4 | structured draft + rendering + alignment wiring | none |
| S5 | citation check, budget wrapper, revision, validation, audit (fakes) | none |
| S6 | additive API (jobs, progress, quality) + frontend | none |
| S7 | opt-in benchmark arm; development evaluation | **requires separate authorization** |

Evaluation phases: E1 tests on local fixtures → E2 development evaluation
(labelled as development, not independent) → E3 error analysis and versioned
fixes, then a frozen configuration → E4 one validation run of that frozen
configuration → E5 any test-set evaluation only under a new, separately locked
analysis plan.

## 10. Decisions still open

Resolved at S1 (2026-10-17): D2 — deterministic alignment, implemented; D5 — ~80
cases with a 40/40 target (realised 82, 39/43), labelled by Claude at the user's
request and recorded as author-labelled, not independent.

Resolved at S2 (2026-10-17): D1 — full design set (one additive migration).

Still open: citation
check reusing the v2 prompt (D3, recommended); threshold values (D6); per-depth budget values and reserve (D7);
claim priority order (D8); in-process jobs vs a queue (D9); macro as the primary
aggregation (D10); authorization and cost approval for E2 and E4 (D11). D4 was
resolved by the approval: no substitution, labelled section.
