# Claim-Level Evidence Verifier — Audit and Revision (verification-v2)

Date: 2026-10-16. Scope: the claim verification stage
(`backend/app/services/verification/`), its reuse by the arm-blind external judge
(`backend/app/services/benchmark/external_judge.py`), the citation-validity and
downgrade rules in the repository/service layer, and the labeled validation set
(`data/benchmarks/verifier_validation.json`).

## 1. What the audit asked

Does the verifier check the **meaning** of each claim against the cited passages,
or does it lean on embedding similarity, LLM confidence, or source-level relevance?

## 2. Findings — what the design already gets right

The architecture is sound, and none of it was changed:

- **Verdicts are meaning-based, not similarity-based.** Embedding similarity is
  used only to *retrieve* the top-k candidate passages per claim; it is recorded on
  the evidence row but never consulted for the verdict. The verdict comes from an
  LLM judging the claim against passage text, and even that is not taken at its
  word (below).
- **A relevant source is not supporting evidence.** The prompt states it (rule 2),
  and the code enforces it: a supports/contradicts relation whose quote is not a
  verbatim substring of its passage is downgraded to neutral, and an
  evidence-bearing verdict left with no quoted relation becomes
  `not_enough_evidence` with `downgraded=True` — recorded, never silent.
- **Citation validity is structural, not judged.** A citation is `valid` only when
  a supporting passage belongs to the cited source
  (`record_claim_verification`); a real, topical source whose passages do not
  support the claim is `misattributed`, with the reason stored.
- **Absence is not falsity.** A claim with no retrieved passages takes a
  no-model-call path to `not_enough_evidence`; two labeled "true but absent from
  pool" cases pin that absence never yields `contradicted`.
- **Malformed output is handled safely.** Structured output is Pydantic-validated;
  one retry, then the claim is a counted skip (verifier) or a counted judging
  failure (external judge) — never defaulted to any verdict. Confidence is stored
  but no threshold anywhere converts it into a verdict.
- **Provenance is preserved.** Each judgment stores the exact quoted span with
  character offsets, the chunk/source identity, claim id, verifier model, prompt
  version, rationale, and the downgrade flag.

## 3. Findings — gaps, all previously measured in stages 20–21

The `verification-v1` prompt had one rule about numbers and nothing about the
other specifics. Three failure modes were already on the record:

1. **Compound partial support** (stage 20, 0/2): a claim whose one conjunct is
   supported and another explicitly negated was judged `contradicted` — partial
   support inflated into contradiction.
2. **Hedged agreement read as contradiction** (stage 21 inspection): "can
   potentially lead to high blood pressure" marked as contradicting "may increase
   blood pressure".
3. **In-range value read as contradiction** (stage 21 inspection): "58%" marked
   as contradicting "30–60%".

Also found in this audit:

4. **An enforcement gap** (observed live on `val-partial-02`): the schema allows
   one relation per passage, so a *single* passage that establishes one part of a
   compound claim and negates the rest carries only one relation. When the model
   quoted the negated part (relation `contradicts`), `_verdict_anchor` accepted
   only `supports` for a `partially_supported` verdict — so a correct, verbatim-
   grounded partial verdict was downgraded to `not_enough_evidence`: quoted
   evidence silently converted into "insufficient", the exact failure the
   downgrade rule exists to prevent.
5. **The `unsupported` / `not_enough_evidence` boundary was undefined** — the v1
   glossary ("nothing relevant found" vs "too thin to judge") overlapped, the
   labeled set had no `unsupported` case, and the verdict was effectively unused.
6. **Vocabulary note:** stored `not_enough_evidence` is this project's spelling of
   the spec vocabulary's INSUFFICIENT_EVIDENCE. Renaming the stored value would
   orphan the database enum and every frozen benchmark artifact; the mapping is
   now documented on `VerificationVerdict` instead of applied.

## 4. Changes made

- **`verification-v2` prompt** (`prompts.py`): specifics extended from numbers to
  dates, units and named entities; an in-range value declared agreement; negation
  vs hedging (hedged agreement is support); causal claims capped at
  `partially_supported` on correlational evidence; compound claims with a negated
  conjunct ruled `partially_supported`, including the single-passage case;
  conflicting evidence explicitly distinguished from insufficient; absence
  explicitly never contradiction; a five-verdict glossary with the
  `unsupported`/`not_enough_evidence` boundary stated. The prompt envelope is
  unchanged. Per the standing stage-21 instruction, **all stage 16–22 results
  remain attributed to verification-v1** and the old 9/11 judge validation is not
  carried over to v2; the external judge version follows automatically as
  `external-verification-v2`.
- **Anchor fallback** (`verifier.py::_verdict_anchor`): `partially_supported`
  accepts a contradicting anchor when no supporting relation survived — the
  verbatim quote for the negated part grounds the verdict. `supported` and
  `contradicted` requirements are unchanged; nothing is accepted without a
  verbatim quote.
- **Validation set v1.2.0** (`verifier_validation.json`): the 11 v1.1.0 cases kept
  verbatim; 10 development cases added — the three measured failure modes, the
  previously uncovered `unsupported` verdict (2), unit/date/entity mismatches,
  causal-vs-correlational wording, a third compound anchor, and
  conflicting-is-not-insufficient. Cases where several verdicts are defensible
  declare `acceptable_verdicts`; the label_basis names the pinned wrong answer.
  This is a development set; **the frozen test set was not touched or used.**
- **`scripts/validate_verifier.py`**: a persistent runner for the labeled set
  through the real production path (versioned prompt, schema, quote enforcement,
  downgrade), writing each measurement to
  `data/benchmarks/results/verifier-validation/` keyed by prompt version.
- **Tests**: six new scenario tests (numerical mismatch → contradicted with the
  verbatim quote and offsets; partial support kept and its citation graded
  `partially_valid`; single-passage partial grounded by the negating quote not
  downgraded; insufficient evidence stored without downgrade; `unsupported`
  standing without a quote), four prompt-pin tests (version strings, each
  measured failure mode named in the rules, the spec checklist, the unchanged
  envelope), and three validation-set shape tests (all five statuses labeled,
  the failure-mode anchors present, `acceptable_verdicts` well-formed).

## 5. Measured agreement with the human labels (21 cases, gpt-4o-mini, 2 passes each)

| | pass 1 | pass 2 | compound-partial cases | severe misses* |
|---|---|---|---|---|
| verification-v1 prompt | 19/21 | 18/21 | 2/6 correct | 4 (partial → contradicted ×3, → NEE ×1) |
| **verification-v2 prompt** | **19/21** | **19/21** | **6/6 correct** | **0** |

\* a miss that crosses a metric boundary (support ↔ contradiction ↔ insufficient).
v2's two misses are identical in both passes and metric-neutral:
`val-true-but-absent-01` and `val-insufficient-02` judged `unsupported` instead of
`not_enough_evidence` — both non-support, non-contradiction verdicts that every
metric treats identically, and the never-`contradicted` pin held on every absence
case in every pass. The v1 baseline was run with the same current enforcement, so
the table isolates the prompt.

**What this does and does not show.** It shows the revised prompt eliminates the
compound-partial failure class on constructed cases and holds the absence and
conflict rules; the agreement count alone (19 vs 19/18) is NOT the story, and 21
hand-written cases at two passes is not a reliability certificate. The stage-21
inspection patterns (meta-claims judged contradicted by one side of the conflict
they describe; contested-topic pools generating cross-fire) are not addressed by
this revision and remain open.

## 6. Observed limitations

- The `unsupported`/`not_enough_evidence` boundary remains soft in the judge even
  after the glossary (2/21 stable swaps). Kept visible rather than tuned away:
  the two verdicts are metric-equivalent, and relabeling the historical cases to
  match the model would be tuning the labels.
- Quote enforcement is exact-substring: an honest quote differing by whitespace or
  smart quotes is downgraded. Safe direction (uncertainty made visible), but it
  can understate support; `downgraded` counts keep it measurable.
- The scripted unit tests prove the **enforcement machinery**, not the judge's
  judgment; only the labeled-set runs measure the latter, at n=21.
- Meta-claims about the evidence landscape, and pools that honestly contain both
  sides of a contested question, still confuse the judge (stage-21 record).

## 7. Recommended next step

Re-run the external judge validation and a development-set benchmark pass under
`external-verification-v2` (the same gate discipline as stages 19–21) before any
new validation/test-set evaluation, so v2's effect on the external support and
contradiction rates is measured on the development set first. The frozen test set
stays untouched until that is approved. A larger labeled set (≥50 cases, second
annotator, inter-annotator agreement) is the next investment if verifier
reliability itself becomes a claim the project needs to make.

**Addendum (2026-10-16):** the development-set re-evaluation was executed —
stage 25 in the development log; artifacts in
`data/benchmarks/results/development/20261016T044109Z-v2eval/`. Headline: on the
identical 418 claims and byte-identical pools, the v2 judge raises external
support for every arm, cuts external contradiction sharply everywhere (21 of
v1's 40 contradicted verdicts move to partially_supported), preserves the arm
ordering, and widens the matched A-vs-D gap from +0.117 to +0.2165. Only
228/418 verdicts are unchanged between judge versions: absolute values are
judge-version-bound and must never be compared across versions.
