# Controlled comparison protocol — claim-level verification on vs off

**Status: APPROVED (2026-10-17) with the decisions in §0. Phase A (the paired
evaluator, offline) is implemented at stage 38. Nothing has been run. No paid call may
be made under this protocol until the approval points in §9 are signed off.**

Research question: *Does explicit claim-level evidence verification improve the
factual grounding and citation correctness of LLM-generated research reports?*

This is a development-phase study. It does not amend, replace or feed
`docs/FINAL_ANALYSIS_PLAN.md` (locked); its numbers are development numbers and are
never presented as validation or test results. **Every support measure here is
support relative to the available evidence (the frozen external pool, or the cited
passage) — not absolute factual accuracy.**

---

## 0. Decisions recorded at approval

1. A paired evaluator scores the draft and the final report **separately**, against
   the same question and evidence pool, preserving each body sentence's mapping to
   its rendered citations and the passages behind them; every rate is reported with
   its counts.
2. The pipeline's own verifier is not the sole evaluator. The evaluator supports an
   independent judge model (`--judge-model`, refused if it equals the pipeline's model
   unless explicitly overridden and labelled "not independent") and blinded human
   annotation (`--export-annotations` / `--annotations`). Phase A uses fakes and
   offline fixtures only.
3. The pilot compares the draft with the verified-and-revised final report. **It
   estimates the combined effect of verification plus verification-informed
   revision**, not verification alone. No rewrite-only control arm yet.
4. One paired run per question for the two-question pilot.
5. **Strict citation-specific support is the primary metric; lenient is the
   sensitivity analysis.** Definitions are fixed in §4 and §6 before any pilot run.

## 1. Arms — why the existing benchmark arms cannot answer the question

Traced in `backend/app/services/benchmark/runner.py` (`_run_cell`):

| | `search_grounded` (C) | `full_autoresearch_verified` (D) |
|---|---|---|
| Orchestrator call | its own `orchestrator.run(SEARCH_GROUNDED)` | a **second, independent** `orchestrator.run(SEARCH_GROUNDED)` |
| Retrieval, selection, drafting | own searches and draft | own searches and draft (different results, stochastic draft) |
| Extraction + verification | run, billed as evaluation overhead | the same calls, billed as product |
| Final report | none | legacy `ReportFinalizer.finalize` |
| What is measured | claims from the **draft** | claims from the **draft** — extraction runs before the finalizer; the final text is never evaluated |

C and D differ in retrieval, evidence, drafting and run noise, and D's measured claims
come from an unverified draft; the locked plan already records C-vs-D differences as
uninterpretable.

**Arms used here — paired, within one report-quality-v1 run:**

- **OFF** = the run's structured draft (`synthesis-structured-v1`), rendered as the
  pipeline renders drafts: a sentence's markers are the source labels of its known
  passage aliases, each marker backed by those passages.
- **ON** = the same run's final report body after verification, citation checks,
  revision (`final-report-structured-v2`), re-verification and validation, read from
  the audit record `finalization.body` (sentence, subquestion, kind, claims, and per
  rendered marker the passages behind it — added at stage 38 for this purpose).

## 2. Questions and evidence pool

- **Pilot:** `dev-normal_research-01` and `dev-conflicting_literature-01` from
  `data/benchmarks/development.json` v1.0.0. Run 1 (`dev-normal_research-01`,
  2026-10-17) predates the body record and is not part of the study.
- **Later (separate approval):** the other 6 development questions.
- **Evidence for judging pool items:** the frozen `pool-v1` development pool of each
  question, verified byte-for-byte against `MANIFEST.sha256` before use. A missing or
  changed pool is a refusal; the evaluator never builds or refreshes a pool. The pool
  is indexed locally inside the evaluation directory, never in the project store.

## 3. Held constant

- One commit for the pilot (recorded, clean tree); no change between pilot questions.
- One `Settings`; pipeline model `gpt-4o-mini` with the returned snapshot recorded per
  call; temperature unset (provider default — stochastic; hence the paired design).
- `quick` depth and the configured budgets (60 attempts, 120k tokens, 30 claims,
  600 s), recorded in each artifact.
- Retrieval and evidence: identical for OFF and ON by construction.
- Evaluator: the same judge model, prompt (`verification-v2`, verbatim-quote
  enforcement), pool hash, `top_k = 4`, seed and evaluator version for every item.

## 4. Metric definitions (pre-registered)

**Units.** A *body sentence* is a sentence under a planned-subquestion or synthesis
heading. Excluded from the body: the degraded banner, every labelled section,
evidence gaps, References. Templated prefixes ("The evidence indicates:") are removed
before judging. An *assertion sentence* is a body sentence of kind finding or
qualification, or any body sentence carrying a citation; uncertainty/transition
sentences without citations are counted but not judged. A *citation pair* is (body
sentence, one rendered marker `[S#]`) with the passages behind that marker.

**Judgments.** A pool item (assertion sentence vs the pool) gets one verdict:
supported, partially_supported, unsupported, contradicted, not_enough_evidence. A pair
item (sentence vs the passages behind one marker) gets one outcome: the best passage
outcome under verbatim-quote enforcement — `supports` (supporting quote AND verdict
supported), `partially_supports` (supporting quote AND verdict partially supported),
`contradicts`, else `does_not_establish`. Human labels map the same way.

**Primary (per question; aggregated as the macro mean over questions with both arms):**

- **P1 strict citation-specific support** = pairs judged `supports` / pairs with a
  judgment. *Sensitivity (lenient):* (`supports` + `partially_supports`) / same.

**Also reported (per arm, with counts):**

- **Body claims vs pool:** strict support = supported / judged; lenient =
  (supported + partially_supported) / judged; **unsupported-or-contradicted** =
  (unsupported + contradicted) / judged; contradicted / judged; not_enough_evidence /
  judged (a pool-coverage limit, not a falsehood).
- **Contradiction handling:** draft assertion sentences the pool contradicts, and the
  fate of their claims in ON — body, contested section, other labelled section,
  recorded omission, unaccounted, untracked (no aligned claim), or ON missing.
- **Core-question coverage:** core subquestions with ≥1 assertion sentence judged
  supported (strict; lenient sensitivity) / core subquestions; plus the core questions
  ON itself disclosed as unanswered.
- **Supported findings lost in revision:** draft assertion sentences judged supported
  (strict; lenient sensitivity), by the fate of their claims in ON; anything other
  than body is a loss, split by how it was accounted for.
- **Finalization failures**, **latency**, **tokens** (measured and estimated apart)
  and **list-price cost** per arm: OFF = planning + drafting; ON = the whole run. OFF
  latency is approximate (claim extraction runs inside draft_and_align and its model
  time is subtracted). Evaluation cost is reported separately, charged to neither arm.

## 5. Counts shown with every rate

Body sentences, assertion sentences, items judged, missing judgments, verdict and
outcome counts, citation pairs, unresolvable pairs, multi-passage pairs, core
subquestions covered, and per-fate counts. A higher ON rate with fewer supported body
sentences is reported as such, not as an improvement.

## 6. Missing, ambiguous and not-evaluated cases (pre-registered)

- **Partial support** counts as support only in the lenient sensitivity view.
- **Ambiguous citation — one marker backed by several passages** (a sentence citing
  two passages of one source): judged as one pair over all of them; supported if any
  passage supports. Counted as `multi_passage`.
- **Unresolvable citation** (a rendered marker with no passage behind it): stays in
  the denominator as not supported, without a judgment; counted as `unresolvable`.
- **Missing judgment** (judge output invalid twice, or a blank/unknown human label):
  excluded from that metric's denominator and counted as `missing_judgments`.
- **Failed finalization:** ON is a missing result — never substituted by the draft.
  Primary aggregates use questions with both arms; the **missing-ON sensitivity**
  scores a missing ON as 0 strict/lenient citation support, 0 pool support and 0 core
  coverage, shown beside the primary with n.
- **Run failure** before a draft exists: both arms missing, reported.
- **Degraded** ON reports are scored as produced; labelled sections are disclosures,
  not assertions, and enter only the contradiction and loss accounting.
- **Pipeline `not_evaluated` claims** appear only in labelled sections; not scored.

## 7. Blinding

- Every unit becomes an item with a random id from a seeded generator; identical
  units across arms are judged once and credited to each arm; items are shuffled.
  `items.jsonl` (what a judge may see: type, text, passages) and `key.json` (item →
  arm, sentence, marker) are separate files; judgments are stored to
  `judgments.jsonl` before the key is joined.
- Judge prompts contain claim text and passages only — no arm, run id or item id.
- **Residual channel:** style (e.g. verification-aware wording) can still reveal the
  arm; stated as a limitation, as in the locked plan §4a.
- **Independence:** the judge prompt is the pipeline's own verification-v2 prompt, so
  independence comes from a different model and/or human annotation, not a different
  procedure. Pool items use different evidence (the pool) than the pipeline did;
  pair items use the same passages the pipeline judged — P1 is therefore the metric
  most exposed to shared bias, and the one human annotation should cover first.

## 8. Data rules

- **Allowed:** the 8 development questions, their frozen dev pools (read-only), run
  artifacts under `data/exports/quality-runs/` and evaluation outputs beside them
  (git-ignored).
- **Untouched:** `validation.json`, `test.json`, `test.freeze.json`, all `val-*` and
  `tst-*` pools (never built), pool manifests, every file under
  `data/benchmarks/results/`, `quantitative_audit_cases.json`,
  `docs/FINAL_ANALYSIS_PLAN.md`. No tuning against any result; a change after seeing
  results is a new configuration and a new study.

## 9. Budget and approval points

| Phase | Scope | Paid calls | Hard cap |
|---|---|---|---|
| A — evaluator | build and test offline (stage 38) | none | $0 — **done** |
| B — pilot | 2 dev questions: 2 product runs + their evaluation | yes | **$1.20 total** |
| C — remaining 6 | same frozen configuration | yes | separate approval |

Pilot estimate (from measured sizes: verification system prompt 3,454 characters,
mean dev-pool passage 637 characters, `top_k` 4, ≈ 30 items per question):

- product: run 1 measured $0.011 per quick run; configured cap $0.072 per run;
- evaluation with `--judge-model gpt-4o`: ≈ 42k input + 18k output tokens per
  question ≈ $0.29 at the August-2024 snapshot's prices; `--max-usd` is enforced at
  run time by a dollar ledger across every judge request and retry (stage 39: each
  request carries an 800-token output limit and reserves its worst case — byte-bounded
  input at the model family's highest list price — before it is sent; failures are
  charged in full; a request that does not fit is refused and becomes a missing
  judgment);
- worst case 2 × $0.072 + 2 × $0.50 = **$1.144; expected ≈ $0.60** — pilot cap
  **$1.20**; stop after the first question if its actual cost exceeds $0.60.
- retrieval outside model budgets: ≤ 3 Tavily + ≤ 3 Semantic Scholar searches per run.

Human annotation of the pilot's citation pairs (≈ 30 per question) is recommended
alongside the model judge; it costs no API spend.

**Approval points:** (1) this protocol — given; (2) the Phase-A diff — this commit;
(3) Phase B launch; (4) Phase C after reviewing the pilot for harness defects only.

## 10. Remaining limitations

1. **Combined effect.** ON = verification + revision; no rewrite-only control.
2. **Judge independence is partial** (§7): same prompt family; P1 shares passages
   with the pipeline. Mitigated by a different judge model and human annotation.
3. **n = 2 (pilot), n = 8 (full dev set), one question per category, one run each:**
   descriptive per-question values only; no significance, no generalisation.
4. **Stochastic generation** (temperature unset) — one paired run per question cannot
   estimate run-to-run variance of the paired difference.
5. **Unit is the sentence**, not an extracted atomic claim: a compound sentence is
   judged as one unit (partial verdicts absorb mixed content). Chosen to preserve the
   sentence → citation → subquestion mapping and to avoid an extra stochastic
   extraction step; it differs from metrics-v2's extracted-claim unit.
6. **Pool coverage:** `not_enough_evidence` reflects what the pool contains;
   pool-v1 was built with the same providers, so pool and run evidence can overlap.
7. **Kind labels** (finding, qualification, uncertainty) come from the drafting and
   revision model; an assertion mislabelled as uncertainty without a citation is not
   judged (counted as a non-assertion sentence).
