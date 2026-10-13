# Experiment protocol — v1 findings on the baseline, and the proposed v2

**Status: v2 is a PROPOSAL.** Nothing below changes evaluation behaviour yet; no
code has been modified, the frozen test set is untouched, and every historical
result (metrics-v1 rows, the committed `results/` directories) is preserved as-is.
Approval gates implementation.

## Part 1 — Why model_only scored zero verifiable claims: the determination

Evidence: the frozen run's raw outputs, the stored reports and claims, the prompts,
and the verifier source. One cell examined in full (`tst-numerical_claims-01 ×
model_only`, run `…` per raw_results.json).

1. **Did it generate factual claims? Yes — real, checkable, largely textbook-true
   ones.** The statins report asserts, e.g., *"Statins work primarily by inhibiting
   the HMG-CoA reductase enzyme"* — a specific, verifiable, and in fact correct
   claim. The baseline's reports are full of such content (14–28 claims per
   question).
2. **Were claims lost to output-format differences? No.** The same extractor ran on
   every arm; it extracted 15 typed claims from that report (124 across the seven
   measured baseline cells), all marked `requires_citation=True`. Extraction is
   format-robust and arm-blind.
3. **Did the prompt fail to request citations? It did the opposite — it forbade
   them.** `MODEL_ONLY_SYSTEM` rule 1: *"Do NOT cite anything… There are no
   sources; inventing one would be fabrication."* Zero citations on all 124 claims
   is instructed behaviour, not model failure.
4. **Did the evaluator exclude its outputs or assign structural zeros? Structural
   zeros, precisely.** `ClaimVerifier._evidence_store()` returns no store for
   MODEL_ONLY; every claim is recorded `not_enough_evidence` with **zero verifier
   LLM calls**. The claims were never judged against anything — there was, by the
   arm's definition, nothing to judge them against.
5. **Are the denominators comparable across arms? Yes — and that is exactly the
   problem.** Claim-support's denominator (all extracted claims) and citation-
   coverage's denominator (claims requiring citation) are produced identically for
   every arm. The *numerators* are structurally pinned at zero for model_only. The
   comparison is procedurally fair and semantically one-sided: the baseline
   competes in a discipline it was forbidden to enter.

**Conclusion.** v1's `claim_support_rate` measures **self-grounding** — support
within the run's own retrieved evidence — and for a no-retrieval arm that is a
definition, not a measurement (audit CD-2, now footnoted in reports). What v1 does
**not** measure, for any arm, is **factual accuracy**. The baseline's claims could
be 95% true or 5% true; v1 cannot tell, and neither can it for the retrieval arms'
unsupported claims.

## Part 2 — The four properties, distinguished

| Property | Question it answers | Measured in v1? |
|---|---|---|
| **Factual accuracy** | Is the claim true, against evidence gathered independently of the run? | **No** — for any arm |
| **Evidence support (self-grounding)** | Does the run's own retrieved evidence support the claim? | Yes (`claim_support_rate`; structural 0 for A) |
| **Citation precision** | Of the citations made, how many point at a source whose passages support the claim? | Yes (undefined for A, which cites nothing) |
| **Citation coverage** | Of claims needing citations, how many cite anything? | Yes (structural 0 for A) |

v1 conflates none of these in storage, but its headline comparison invites reading
self-grounding as accuracy. v2 separates them explicitly.

## Part 3 — The proposed fair baseline protocol (v2)

**Same underlying model, same questions, same generation settings** — unchanged.
Two additions:

### 3a. A common external evaluation procedure (the core of fairness)

For each benchmark question, the **evaluator** — not any arm — builds one
**external evidence pool**: a fresh retrieval (web + academic, the question as the
query, a fixed pool size), processed and indexed into an evaluation-owned
collection (`eval_<question_id>`), identical machinery to the pipeline's. Every
arm's extracted claims are then verified **against that same pool** with the same
verifier, prompts and quote enforcement.

New metrics (exact formulas to be added to `metrics.py` + `METRICS.md` under
**metrics-v2**; v1 rows remain v1):

- `external_support_rate` = (supported + partially_supported against the pool) /
  total claims — the **factual supportability** of each arm's prose, on equal
  footing. The baseline can now score above zero; so can it be contradicted.
- `external_contradiction_rate` = contradicted / total claims — the externally
  measured hallucination signal that v1's structural zero could not provide.
- The v1 self-grounding metrics are kept, unchanged, clearly labelled as
  self-grounding; historical comparability is preserved.

Fairness properties: the pool derives from the question alone (no arm's report can
shape it); all arms face the identical pool; the judge/verifier never knows which
arm produced a claim. Known limitation, stated up front: the pool inherits the
retrieval providers' blind spots, so "externally unsupported" still means
"unsupported by what the evaluator could retrieve", not "false" — accuracy against
retrievable evidence, the strongest honest proxy available without human labels.

Cost estimate: 8 pool retrievals + ~(4 arms × 8 items × ≤15 claims) verifications
≈ 500 mini calls ≈ roughly $0.20–0.30 and ~45 minutes per full grid; a per-report
verification cap (first 15 claims by position) bounds it.

### 3b. A citation-capable baseline arm (A′, optional second phase)

`model_only_cited`: the same model and question, no retrieval, but the prompt
**requires** references from memory (free-text: title/author/year/URL). Measures
what the forbidden-citations baseline cannot: **memory-citation fabrication**
(`MetricKey.FABRICATION_RATE`, in the schema since stage 4, never yet exercised).
Requires new resolution machinery (parse free-text references; URL liveness → .
`broken`; title search against Semantic Scholar → exists/fabricated), so it is
scoped as phase 2, after 3a proves out on the development set.

## Part 4 — Implementation and tests (awaiting approval; nothing built yet)

| Piece | Change | Tests |
|---|---|---|
| `external_pool.py` (new, benchmark pkg) | build + index the per-question pool, evaluator-owned | pool derives from question only; identical pool hash reused across arms; pool failure → external metrics None, never zeros |
| `verifier` reuse | verify a claim list against a named collection (parameterise the store; no change to run-owned verification) | external verdicts stored separately (never touching run rows); quote enforcement identical |
| `metrics.py` | `external_support_rate`, `external_contradiction_rate`; `METRICS_VERSION = "metrics-v2"` | hand-computed values; docs-agreement test extended |
| `runner.py` | evaluation phase gains the external pass for all arms; results files gain the external columns | fake-chain smoke: baseline scores non-zero external support when the fake pool supports it; v1 columns unchanged |
| Storage | external verdicts recorded as `evaluation_result` rows under metrics-v2 + raw in results.json | traceability spot-check test |
| Historical results | untouched; v1/v2 never mixed in one table | freeze/regression tests already in place |

Dry-run order (also awaiting approval): development set first; validation if
thresholds need tuning; the frozen test set **only after** v2 is reviewed — and
that run would be a new, clearly-versioned evaluation, not a rewrite of
`20261013T060512Z`.

## Remaining uncertainty

- External-pool quality bounds what "factual accuracy" can mean here; a human-
  labelled claim subset would calibrate the external verifier (future work).
- Whether the pool should be fixed per question across repeat runs (cache it) or
  fresh per run (measures retrieval variance too) — proposal: cache per
  (question, pool-version) for comparability; decide at approval.
- A′'s reference-resolution accuracy (title matching) will need its own small
  validation set before its numbers are trusted.
