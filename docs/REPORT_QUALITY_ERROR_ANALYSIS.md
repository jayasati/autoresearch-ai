# report-quality-v1 — error analysis

Development-phase error analysis (design §9, phase E3) of real-provider runs of
`report-quality-v1`. Every run here is a **development** run: the findings are
hypotheses for versioned fixes, not measurements of the pipeline's quality. One run
on one question cannot estimate a rate.

---

## Run 1 — 2026-10-17

| | |
|---|---|
| Question | `dev-normal_research-01` — "What are the health effects of regular moderate coffee consumption in adults?" |
| Code | `bdc4a29` (runner `quality-runner-v1`), quick depth, gpt-4o-mini-2024-07-18 |
| Artifact | `data/exports/quality-runs/20261017T131939Z-1d893453/` (`run.json`, `final_report.md`; git-ignored) |
| Outcome | **degraded** (exit 3): "high-importance claims unresolved or not evaluated: ['C7']" |
| Usage | 24/60 attempts, 57,894/120,000 tokens, all measured; ≈ $0.011 at list prices; ≈ 115 s |

### What the run produced

- **Plan:** Q1 cardiovascular benefits (core), Q2 mental-health risks (core), Q3
  variation by age (supporting). Retrieval selected 3 / 3 / 2 passages (E1–E8 from
  S1–S7); no provider failures.
- **Draft:** one section per subquestion plus a synthesis section; 13 sentences, 12
  claims extracted, all 12 aligned and verified (no claim cap, budget or deadline
  effect).
- **Verification:** 5 claims supported by their cited source (C1, C2, C5, C6, C9),
  2 contested (C4, C10), 3 insufficient (C3, C7, C8), 2 supported only by uncited
  evidence (C11, C12).
- **Revision:** made (some claims could not stand as drafted). One sentence was flagged
  for a changed qualification and re-verified against its cited passage only.
- **Final report:** 4 body sentences citing S1, S2, S6; References list exactly those
  three; the 7 non-presentable claims listed in labelled sections.

What held: every rendered marker comes from a passage established as supporting its
claim; the References section matches the markers; contested claims stayed visible;
nothing the verifier did not establish was presented as a finding; budget accounting
was complete (no estimated tokens).

### Findings, most severe first

#### F1 — The revision dropped two planned sections, and the losses were not all recorded (high)

The draft had sections for Q1, Q2, Q3 and SYNTHESIS. The revision returned **only Q1**,
breaking rule 1 of `final-report-structured-v1` ("keep one section per planned
subquestion"). Validation only warned ("The draft has no section for planned
subquestion(s) ['Q2', 'Q3']"). Consequences:

- **C9, a claim supported by its cited source, disappeared silently.** It was not
  high-importance, so V7 did not restore it; it was supported, so V7b did not list it.
  No action records the omission.
- **C6 was restored under the wrong heading.** V7 restored it (high importance), but
  its subquestion (Q3) had no section, so `validate_final` fell back to the *last*
  section (`target = sections[-1]`, `validation.py`), placing an all-cause mortality
  finding under "Potential Benefits … on Cardiovascular Health".
- The draft's Q2 uncertainty sentence ("The retrieved evidence does not address …
  mental health") was lost with its section.

#### F2 — An unanswered core subquestion is not disclosed (high)

Q2 (mental-health risks, **core**) produced no claims: its subquestion assessment is
`addressed: false`. Yet the final report has **no Evidence gaps entry** for it — the
gaps section is driven by `gap_reason`, which is set only for retrieval failures or
empty retrieval, and here retrieval returned three (off-topic) passages. The reader is
not told that half of the question went unanswered; "mental health" appears only inside
a listed claim (C11). The outcome did not reflect it either: `core_coverage` was 0.5,
equal to the provisional `REPORT_QUALITY_MIN_CORE_COVERAGE` of 0.5, and the check fails
only *below* it.

#### F3 — An overstated claim reached the body as a qualified finding (high)

C1, stated in the body with [S1]: "moderate coffee consumption (3 to 5 cups daily) is
**highly beneficial** for cardiovascular health in **all adults**". S1 is a hospital
marketing article titled "… A Health Guide for Adults 45-65+".

- verification-v2 judged C1 `partially_supported`; the citation check reused that
  relation on E1 (`partially_supports`), so C1's status was
  `supported_by_cited_source`. Partial support is allowed in the body as a
  qualification.
- The revision qualified it only with the empty phrase "albeit with some
  qualification regarding its benefits", keeping "highly beneficial" and "all adults".
- The meaning-change detector flagged the sentence (`qualification`); re-verification
  against E1 alone returned **`supported`** — stronger than the original partial
  verdict — so it stayed in the body.
- The body now reads as self-contradictory: its first sentence says consensus is
  lacking (C5, [S2]); the second says the benefit is high for all adults.

Whether E1 states the scope cannot be checked from the artifact: **passage texts,
verifier quotes and rationales were not saved** (see F6). The structural gap is real
regardless: partial support plus a generic hedge lets an overstated scope through, and
re-verification of the hedged sentence may upgrade the verdict.

#### F4 — `body_loss_ratio` cannot see what the revision omitted (medium)

`proposed_findings` (3) counts the *revision's* findings, so `findings_not_in_body` was 0
and `body_loss_ratio` 0.0 although the revision dropped supported claims C6 and C9 and
two sections. The complete / degraded / failed thresholds are blind to the largest
loss in this run.

#### F5 — Many verifier quotes failed the verbatim check (medium)

15 "quote … is not verbatim in the passage; relation downgraded … to neutral" warnings
across 9 of 12 claims, plus 3 dropped conflicts. The check is an exact substring test
(`quote in hit.text`, `verifier.py`) with no whitespace or Unicode normalisation. Web
page text from Tavily often carries irregular whitespace, line breaks and typographic
quotes, so faithful quotes may fail it. Plausible victim: C7 ("Drinking caffeinated
coffee was associated with a 25% to 30% lower risk of fall in older adults"), a near
copy of source wording, ended `does_not_establish` on its own cited passage. **Not
confirmed:** the raw quotes were not saved (see F6). The downgrade is the safe direction
— it never creates support — but it inflates `insufficient_evidence`.

#### F6 — The artifact cannot support its own error analysis (medium, runner)

`run.json` holds statuses but not the selected passage texts, the verifier's quotes and
rationales, or the evidence rows; the workspace database is deleted after the run.
F3 and F5 could not be confirmed for that reason. This is a runner gap, fixable with no
paid call.

#### F7 — Labelling and source quality (low)

- C11 ("The evidence does not establish clear risks … mental health …"), an
  absence-of-evidence statement from an `uncertainty` sentence, was extracted as a
  claim and listed under "Supported by other evidence — citation not confirmed", which
  reads oddly for a statement about missing evidence.
- Two of the three cited sources are a marketing blog (S1) and a news article (S6).
  Source selection has only provider-level profiles and no quality signal — a known
  design limitation, not a regression.

#### F8 — Workspace cleanup failed on Windows (runner) — fixed

ChromaDB held `data_level0.bin` open, `shutil.rmtree` deleted the marker before failing,
and the temporary workspace was left behind (removed by hand). Root cause:
`SharedSystemClient.clear_system_cache()` forgets cached systems without stopping
them. Fixed in the runner (not yet committed): stop only the ChromaDB systems whose
directory is inside the workspace, retry briefly, delete the marker last. A regression
test with an open ChromaDB store reproduces the original `WinError 32` against the old
code and passes with the fix.

### Recommendations (R1 at stage 36; R2, R3 and the partial policy at stage 37)

Ordered by value per cost. R1 needs no paid call and should precede any further paid
run, so the next run can be analysed properly.

1. **R1 — Save diagnostics in the artifact (runner) — done (stage 36):** selected passage texts by alias,
   the verifier's quotes/relations/rationales per claim, and the citation-check and
   final-check rationales.
2. **R2 — Section integrity after revision (F1):** in code, re-insert every planned
   section the revision dropped (from the draft, through the same validation), place
   restorations under their own subquestion, and record an action for *every* claim
   that ends up neither in the body nor in a labelled section.
3. **R3 — Disclose unaddressed core subquestions (F2):** list `addressed: false`
   subquestions under Evidence gaps whatever the cause, and decide whether
   `core_coverage == threshold` should pass (the knob is provisional).
4. **R4 — Partial support policy (F3) — a decision for you:** options include
   rendering partially supported claims only with the verifier's stated limitation,
   forbidding re-verification from upgrading a verdict above the original pool
   verdict, or holding partial claims out of the body. Each changes what the report
   may state.
5. **R5 — Count revision omissions in `body_loss_ratio` (F4)** against the claims
   supported before revision.
6. **R6 — Quote matching (F5):** first measure with R1's saved quotes. A whitespace- and
   Unicode-normalised match would be a new verifier version (verification-v3),
   re-validated on the 21 hand-labelled verifier cases (a paid step) before use.

Any change above is a new configuration version; development questions only, no
tuning against validation or test data.

R1 (stage 36): the runner now writes `diagnostics.json` (passage texts and sources,
verification results and evidence rows, citation and final checks with rationales) and
`model_calls.json` (every sent model attempt with its prompt and raw answer, so a quote
rejected by the verbatim check stays visible). Run 1's artifact predates it; F3 and F5
can be confirmed only on a later run.

Stage 37 — the minimum integrity fixes before the controlled comparison (user
directive: no further infrastructure):

- **F1 → fixed.** The finalizer re-inserts, from the draft, every planned section the
  revision dropped, keeping only draft sentences whose claims the revision does not
  state anywhere (no outdated version comes back); a V7 restoration goes under the
  claim's own subquestion (a new section headed by its planned text if needed), never
  the last section; V8 records every claim that ends in neither the body nor a labelled
  section as `omission_recorded`, with why (`metrics.claims_omitted`).
- **F2 → fixed.** V9: every core subquestion with no supported body sentence is
  `unanswered_core`, makes the outcome degraded at least, and is listed under Evidence
  gaps — the coverage threshold no longer hides it.
- **F3 → policy implemented.** A partly supported claim (pool verdict partial, or no
  cited passage fully supporting it) reaches the body only when the revision narrowed
  its wording (a content word removed or replaced, or a scope or number changed) AND
  re-verification on its cited passages found it fully supported. Unchanged wording,
  or a hedge that keeps every content word, goes to "Unresolved findings" prefixed
  "Only partly supported by the cited evidence:" and is never sent to re-verification.
  Partial claims now trigger a revision; the revision prompt (`final-report-structured-v2`)
  shows the quote each partial claim rests on and asks for narrowing, not hedging.
  The narrowing test is a heuristic: a rewrite that passes it still has to pass
  re-verification.
- **B1 (records).** Each recorded model call carries its prompt version; each claim and
  final check in `diagnostics.json` lists the indexes of the exact verifier calls that
  judged it; passages carry source ids and evidence rows their ids.

Still open: F4 (`body_loss_ratio` counts the revision's own findings, not the draft's
supported claims — omissions are now recorded but not scored), F5 (quote matching;
needs a run with the new records), F7 (labelling, source quality).

---

## Pilot (protocol Phase B) — 2026-10-17

The two-question pilot of `docs/VERIFICATION_COMPARISON_PROTOCOL.md`: one paired run
per question, OFF = the run's draft, ON = the same run's final report. It estimates the
**combined effect of verification plus revision**, and every support figure is support
relative to the frozen evidence pool or the cited passage — **not factual accuracy**.
n = 2, one stochastic run each: descriptive only, no significance or generalisation.

| | |
|---|---|
| Code | `b66175b` (clean tree) — pipeline `gpt-4o-mini`, quick depth; evaluator `pair-eval-v1` |
| Judge | `gpt-4o` (every answer from `gpt-4o-2024-08-06`), a different model from the pipeline's; budgeted by the dollar ledger at `--max-usd 1` per question |
| Pools | frozen `pool-v1` dev pools, verified against `MANIFEST.sha256`, never rebuilt |
| Q1 | `dev-normal_research-01` — artifact `data/exports/quality-runs/20261017T151541Z-a8a44720/` |
| Q2 | `dev-conflicting_literature-01` — artifact `data/exports/quality-runs/20261017T173313Z-e7da20d5/` |
| Human annotation | blinded sheets prepared (`annotation/annotations.csv`, 35 and 34 items); **no human labels yet** |

Run 1 above predates the stage-36/38 records and is not part of the pilot.

### Results

| Metric (counts in brackets) | Q1 OFF | Q1 ON | Q2 OFF | Q2 ON |
|---|---|---|---|---|
| Finalization | — | degraded | — | complete |
| Body sentences / assertions | 19 / 14 | 6 / 6 | 16 / 13 | 14 / 12 |
| **Strict citation support (primary)** | **0.652** (15/23) | **0.833** (5/6) | **0.867** (13/15) | **0.923** (12/13) |
| Lenient citation support (sensitivity) | 0.957 (22/23) | 0.833 (5/6) | 1.000 (15/15) | 1.000 (13/13) |
| Pool: supported / partial / not enough evidence | 6 / 5 / 3 | 2 / 1 / 3 | 4 / 3 / 6 | 5 / 3 / 4 |
| Pool support strict / lenient | 0.429 / 0.786 | 0.333 / 0.500 | 0.308 / 0.538 | 0.417 / 0.667 |
| Unsupported or contradicted | 0/14 | 0/6 | 0/13 | 0/12 |
| Core coverage strict / lenient | 0.5 / 0.5 | 0.5 / 0.5 | 0.5 / 1.0 | 0.5 / 1.0 |
| Strict-supported draft findings → ON body / listed / untracked | 2 / 1 / 3 (of 6) | | 4 / 0 / 0 (of 4) | |
| Missing judgments, refusals, retries | 0 | | 0 | |

Paired macro means (both questions have both arms): strict citation support **OFF
0.759 → ON 0.878**; lenient OFF 0.978 → ON 0.917; strict pool support OFF 0.368 → ON
0.375; unsupported-or-contradicted 0 → 0; strict core coverage 0.5 → 0.5. No missing ON,
so the missing-ON sensitivity view equals the primary.

Cost and time:

| | Q1 | Q2 |
|---|---|---|
| Pipeline (all tokens measured, none estimated) | 23 calls, 57,221 tokens, $0.0112 | 23 calls, 58,969 tokens, $0.0118 |
| of which OFF (planning + drafting) | 2 calls, 4,441 tokens, $0.0013 | 2 calls, 4,181 tokens, $0.0012 |
| Judging (gpt-4o, 0 refused / failed / retried) | 35 requests, $0.2510 | 34 requests, $0.2368 |
| Latency OFF (approximate) / ON | ≈100 s / 164.6 s | ≈74 s / 148.6 s |

Pilot total ≈ **$0.511** (Tavily and Semantic Scholar searches not included). Ledger
bounds held with a wide margin: measured input was ≤ 15% of each reservation's input
bound, and the largest answer was 371 tokens against the 800 limit.

**Reading, cautiously:** on both questions the final report's citations were more often
strictly supported by their own cited passages; on Q1 that came with stating far less
(6 judged body sentences against 14), and under lenient scoring the draft was ahead on
Q1. Against the pool, strict support barely moved and neither arm stated anything the
judge found unsupported or contradicted. This is consistent with verification plus
revision trading coverage for citation precision on these two questions — not evidence
for or against the research hypothesis.

### Findings from the pilot

#### P1 — The narrowing heuristic counts a broadened claim as narrowed (high)

Q1, claim C6, partly supported (pool `partially_supported`; its cited passage E6
`partially_supports`): "High coffee consumption **(≥ 3 cups/day)** was inversely
associated with mortality and disability." The revision wrote "High coffee consumption
was inversely associated with mortality and disability **with some qualification**."

- The revision *removed* the restriction "(≥ 3 cups/day)". `validation.narrowed` treats
  any changed number or scope as narrowing, so a rewrite that **widens** the claim
  passed the stage-37 policy and was sent to re-verification.
- The pipeline's re-verification (gpt-4o-mini, verification-v2) judged the rewrite fully
  `supported` on E6 — an upgrade from the original partial verdict — so it reached the
  body with its citation.
- The independent judge (gpt-4o) found E6 does **not** establish the sentence; it is the
  one Q1 ON citation pair that is not supported.
- The appended phrase "with some qualification" is the exact phrase the
  `final-report-structured-v2` prompt gives as an example of what is *not* narrowing; the
  prompt may be priming it.

The stage-37 guarantee ("a hedge cannot upgrade a verdict") holds only for rewrites that
keep every content word; a rewrite that drops a restriction and adds a hedge escapes it.

**Status — fixed at stage 40 (`b9fe274`, 2026-10-18).** The pilot results above were
produced under the old rule and are not re-run or changed.

- `validation.narrowing` replaces the old test and fails closed: a rewrite counts as
  narrower only if nothing restrictive was dropped or loosened **and** something was
  tightened. It is "not demonstrated narrower" when negation changed; an original
  number is gone, unless replaced in the same unit by a stricter bound of the same kind
  (a lower bound — "≥", "at least", "more than" — raised, or an upper bound — "≤", "up
  to", "less than" — lowered); an original scope restriction is gone or replaced; or a
  content word other than an overstatement word ("all", "highly", "significantly", ...)
  was removed. Positive signals: a restriction or quantity added, a bound tightened, an
  overstatement word removed. Hedges and wording alone never narrow.
- A partial claim's rewrite that is not demonstrably narrower is never re-verified and
  goes to "Unresolved findings" (`superficial_hedge`, or the new `not_narrowed` action
  with its reason). A narrower rewrite reaches the body only when the citation-specific
  re-check fully supports the exact rewrite; partial, failed, missing, contested and
  uncited-only re-checks keep their own placements. Genuine narrowing (an added
  restriction, a raised lower bound, a lowered upper bound) can still be upgraded.
- Revision prompt `final-report-structured-v3`: keep every existing restriction
  (numbers, thresholds, doses, populations, periods, places); narrowing is defined;
  hedging alone never narrows; the example phrase "with some qualification" is gone.
- The exact pilot claim and rewrite are a regression test: held back, never re-checked,
  listed as unresolved even when a fully supporting re-check is on offer (17 new tests;
  mutation probes restoring the stage-37 rule, always-narrowed and never-narrowed made
  9, 11 and 17 tests fail).
- Remaining limits: comparators are read only from the 16 characters before a number
  (a bound such as "3 cups or more" is not recognised, so its change fails closed); the
  test is lexical, so a genuinely narrower reformulation is held back until phrased more
  literally; once narrowing is established, the upgrade still rests on the pipeline's own
  gpt-4o-mini re-check, and the P1 disagreement with the independent judge is not
  otherwise addressed. The fix has not yet been exercised on a real run.

#### P2 — Contradiction handling remains untested, including on the conflict item (high)

`dev-conflicting_literature-01` is labelled `expect_conflicts: true`. The pipeline stored
no conflicts and produced no contested section; the judge found no draft sentence
contradicted by the pool on either question. The disagreement appears only in prose
("Randomized controlled trials … have provided inconsistent results"; "no statistically
significant overall effect"). The protocol's contradiction-handling metric therefore had
nothing to measure on the one question designed to exercise it.

#### P3 — Half of Q1's supported draft findings cannot be followed into ON (medium)

3 of the 6 strict-supported draft sentences (6 of 11 lenient) are `untracked`: their
draft sentences have no aligned claim, so the evaluator cannot say whether the revision
kept, moved or dropped them. The "supported findings lost in revision" metric is
incomplete exactly where Q1 lost the most content (19 → 6 body sentences).

#### P4 — Pipeline and evaluator disagree on core coverage (medium)

Q2: the pipeline judged core question Q2 answered (no gap disclosed), while against the
pool Q2's sentences were only partially supported or not established, so the evaluator's
strict coverage counts Q2 as uncovered (lenient: covered). The two use different evidence
(cited passages vs the pool); the pipeline's "answered" is not pool-supported in the
strict sense.

#### P5 — Update to F5: the verbatim-quote failures are mostly real (informational)

With the stage-36 transcripts (pipeline verifier, citation-check and re-verification
calls), 62 non-neutral quotes across the two runs: **54 verbatim**, **2** match only after
whitespace/punctuation normalisation, **6** are absent even normalised (paraphrases and
"[...]" splices of passage text). Normalised matching would recover 2 of 8 failures; the
rest are correctly downgraded by enforcement. F5's whitespace hypothesis is largely
refuted; no change to quote matching is indicated.

#### P6 — Stage-37 fixes worked as designed (informational)

Q1's revision again dropped two planned sections; both were re-inserted, no claim was
unaccounted (`claims_omitted` empty), one unchanged partial claim went to Unresolved
(`partial_not_narrowed`), and the unanswered core question Q2 was disclosed as an
evidence gap with the outcome degraded. Q2's revision dropped nothing.

### Open questions for review (nothing implemented, protocol and metrics unchanged)

1. **P1:** should narrowing require that no restriction (number, population, period,
   setting) be removed — and should re-verification be forbidden from returning a
   stronger verdict than the original partial one? Either changes the stage-37 policy.
   *Resolved at stage 40:* narrowing now fails closed and never accepts a dropped or
   loosened restriction. Re-verification is not forbidden from upgrading; instead only a
   demonstrably narrower rewrite is re-checked at all, and it is upgraded only on a full
   citation-specific re-check of the exact rewrite.
2. **P1:** remove the quoted example phrase from the revision prompt (a prompt change,
   hence a new prompt version and a new configuration). *Resolved at stage 40:*
   `final-report-structured-v3`.
3. **P2:** contradiction handling needs a question where the pipeline actually records a
   conflict; whether to wait for the remaining six dev questions or treat it as a known
   gap.
4. **P3:** align draft sentences to claims more completely, or report untracked findings
   as a limitation only.
5. **Human annotation** of the 69 prepared items, so P1-style pipeline/judge
   disagreements can be adjudicated.

---

## Retrospective offline analysis of the pilot: P3, P4, P2 — 2026-10-18

**Retrospective and offline.** Everything below is derived from the saved pilot artifacts
(`run.json`, `diagnostics.json`, `model_calls.json`, `evaluation/`), with no new run,
provider call or judgment. The pilot section and its figures above are unchanged; any
number recomputed here is a retrospective view and does not replace them.

Q1 = `data/exports/quality-runs/20261017T151541Z-a8a44720/`;
Q2 = `data/exports/quality-runs/20261017T173313Z-e7da20d5/`.

### P3 — Were Q1's untracked supported findings lost, or only unlinked?

**Root cause.** The drafting model emitted the synthesis section **three times**: its raw
answer (`model_calls.json`, the `synthesis-structured-v1` call) has sections 3, 4 and 5 all
`SYNTHESIS`, with identical headings and sentences, and the stored draft keeps all three
(`persisted.audit.payload.draft.report.sections[3..5]`). Nothing downstream merges them.
With three identical candidate sentences, claim alignment for the claims extracted from
them was `unaligned` / `ambiguous` (`alignments`: C12, C13, C14), so those claims got no
citation check and C12 and C13 ended `not_evaluated`. The evaluator links a draft
sentence to the final report only through alignment, so those sentences became
`untracked` — and, scoring per sentence, it counted each copy separately.

Reconciliation, one row per unique supported draft finding (D# = draft sentence index in
reading order; pool verdict = the independent judge, OFF arm; F# = final body sentence):

| ID | Draft finding | Pool verdict | Pipeline claim | Final report | Classification | Evidence |
|---|---|---|---|---|---|---|
| D0 | "Moderate coffee intake is more beneficial than harmful across a wide range of health outcomes" | supported | C1, supported by cited source (E2 supports) | F0, body, same text, [S2] → E2 | preserved, traceable | same alias, same passage, text identical apart from the draft's kind tag |
| D1 | "Numerous large-scale, prospective cohort studies have consistently shown … can be moderately beneficial" | supported | C2, supported by cited source (E2 supports) | F1, body, same text, [S2] → E2 | preserved, traceable | as D0 |
| D4 | "High coffee consumption (≥ 3 cups/day) was inversely associated with mortality and disability" | partially supported (lenient only) | C6, partial (E6 partially supports) | F5, body: threshold dropped, "with some qualification" added | materially changed | P1 |
| D7 | "In health research, moderate coffee consumption is typically defined as three to five cups per day" | supported | C9, supported by cited source, but E2 only partially supports (partial) | "Unresolved findings", same text, partial prefix | preserved, traceable; moved out of the body | action `partial_not_narrowed` (stage-37 policy) |
| D8 | "While some studies have cited moderate intake as 2-3 cups a day, others suggest three to four …" | partially supported (lenient only) | C10, citation not confirmed (E4 does not establish) | "Supported by other evidence — citation not confirmed", same text | preserved, traceable; listed | action `moved` |
| D10 = D13 = D16 | "Regular moderate coffee consumption appears to provide several health benefits, including lower risks of early death, cardiovascular disease, and disability" | supported (one item, credited three times) | C12, unaligned (ambiguous), not evaluated | "Not evaluated", same text | substantively preserved but no longer linked to an evaluated claim; listed, not in the body | text identical apart from the kind tag; action `omission_listed` for C12 |
| D11 = D14 = D17 | "The specific definition of 'moderate' varies in health research, generally falling between two to five cups per day" | partially supported (lenient only) | C13, unaligned (ambiguous), not evaluated | "Not evaluated", same text | as D10 | action `omission_listed` for C13 |

Genuinely omitted: **0**. Indeterminate: **0**. (The draft sentences carry a literal
"(finding)"/"(qualification)" tag the drafting model wrote into the text; apart from that
tag, the matched texts are identical.)

Reconciled fates, counting each unique finding once:

| | Stored metric (per sentence) | Reconciled (per unique finding) |
|---|---|---|
| Strict-supported | 6 → body 2, listed 1, untracked 3 | 4 → body 2, listed 2 (1 unresolved, 1 not evaluated) |
| Lenient-supported | 11 → body 3, listed 2, untracked 6 | 7 → body 3 (1 materially changed), listed 4 |

Retained **in the report** (body or a labelled section): 4 of 4 strict-supported and 7
of 7 lenient-supported unique findings. Retained **in the body**: 2 of 4 strict and 3 of
7 lenient (one of those three materially changed).

**Effect on interpretation.** No supported finding was silently omitted: the untracked
ones were lost to the *evaluator's linkage*, not to the report — they are listed verbatim
in its "Not evaluated" section. But they were lost from the **body**: of 4 unique
strict-supported draft findings, 2 left the body, one by the partial-support policy and
one because the duplication made it unalignable, hence never citation-checked. The
duplication also inflates Q1's OFF per-sentence metrics. *Retrospective sensitivity view,
not a replacement for the frozen pilot figures above* — counting each unique sentence
once: OFF strict citation support 9/13 = 0.692 (stored 0.652), lenient
12/13 = 0.923 (stored 0.957); OFF strict pool support 4/10 = 0.400 (stored 0.429), lenient
7/10 = 0.700 (stored 0.786). Q2 has no duplicated sentences. The two-question strict
citation support, as the unweighted mean of the two per-question rates (the protocol's
macro convention), would read OFF 0.780 vs ON 0.878 (stored OFF 0.759); pooled over
citation pairs instead it would be OFF 22/28 = 0.786 vs ON 17/19 = 0.895. Same
direction either way.

Further observations (not fixed): the drafting validation accepts a repeated section;
the drafting model's kind tags ("(uncertainty)", "(finding)") reach the final report's
labelled lists; and the independent judge rated D1's citation `does_not_establish` but
F1's `supports`, though the two texts differ only by that tag — judge variability on
near-identical inputs.

### P4 — Which core-coverage definition matches the protocol?

**The definitions in use:**

| | Evaluator (`pair-eval-v1`) | Pipeline (validation V9) |
|---|---|---|
| Unit | assertion sentences in the core subquestion's own section (synthesis sentences do not count) | body sentences in that section |
| Evidence | the independent judge, against the **frozen pool** | the pipeline's own verifier, against the sentence's **cited passages** |
| Covered when | ≥1 sentence judged `supported` (strict); or `supported` / `partially_supported` (lenient sensitivity) | ≥1 body sentence with a rendered marker (support established on its cited passage) and a claim |
| Partial support | lenient only | partial claims are not in the body since stage 37 |
| Unsupported, not enough evidence, unresolved, labelled | not counted | not counted (not in the body) |
| Disclosed gaps | reported separately (`on_unanswered_core_disclosed`), never counted as coverage | an uncovered core question is listed as a gap and degrades the outcome |

**Protocol wording (verbatim, §4):** "Core-question coverage: core subquestions with ≥1
assertion sentence judged supported (strict; lenient sensitivity) / core subquestions;
plus the core questions ON itself disclosed as unanswered." In §4, "supported" is the
pool-item verdict vocabulary (pair items get "supports"), and the line is in the
per-arm "Also reported" list after the pool metrics, so the literal reading is the evaluator's pool-based definition;
the pipeline's own judgment is what "the core questions ON itself disclosed as
unanswered" reports. The wording never states the evidence source explicitly, though —
a minor ambiguity.

**Manual recomputation for Q2 (ON).** Core subquestions Q1 and Q2. Q2's section has five
assertion sentences, F7–F11:

| | F7 | F8 | F9 | F10 | F11 |
|---|---|---|---|---|---|
| Pool verdict (judge) | not enough evidence | partially supported | not enough evidence | partially supported | not enough evidence |
| Cited-passage outcome (judge)¹ | supports | supports | supports | partially supports | supports |
| Pipeline | supported by cited source, in body with a marker (all five) | | | | |

Strict pool-based: no `supported` sentence → Q2 not covered; coverage 1/2 = **0.5**, as
stored. Lenient: F8, F10 → covered; **1.0**, as stored. The OFF arm gives the same. The
evaluator computes the protocol's metric correctly.

¹ Shown only to explain the discrepancy. Pool-supported coverage (the protocol's
metric) and coverage by cited-passage support are distinct measures; only the first is
defined by the protocol or computed by the evaluator.

**Explanation.** Not an implementation error: the two systems measure different
constructs — pool supportability versus cited-passage support — and the gap is driven by
pool coverage. The pool was built from the question text (the clinical effect of vitamin
D); Q2 asks about immune mechanisms, and three of its five sentences are "not enough
evidence" against the pool while the independent judge finds four of five fully supported
by their own cited passages. "Unanswered" under the pool metric here reflects what the
pool contains, not what the report says.

**Proposal only — not implemented in the evaluator and not adopted by the protocol; the
locked plan is not affected.** Before further runs, amend the comparison protocol to name the current metric "pool-supported
core coverage" and to add "citation-supported core coverage" from the evaluator's own
pair outcomes, both reported, with a note that pool-based coverage is unreliable for
subquestions outside the pool's retrieval scope.

### P2 — What the conflict item can and cannot tell us

- **Does the pool contain conflicting passages?** Lexically it holds both directions:
  benefit-like passages ("modest protection … reducing incidence"; "reduced ARI risk by
  about 30% … in children aged 1–16") and null-like ones ("no statistically significant
  overall effect … OR 0.94"). That is consistent with the overall-versus-subgroup
  disagreement the item targets, but it is a keyword signal, not a judgment; the records
  cannot establish that the passages genuinely conflict.
- **Did the pipeline detect conflicts?** Partly at the passage level, not as conflicts.
  The verifier recorded four `contradicts` relations with verbatim quotes on claims C4, C6
  and C7 (`diagnostics.json`, evidence rows; raw answers in `model_calls.json`, calls 6–8).
  No answer reported a `conflict` object, no conflict was stored
  (`persisted.audit.payload.conflicts` is empty), and the claims' verdicts stayed
  `supported` / `partially_supported`.
- **Was contradiction handling invoked?** The passage-relation step ran; the
  conflict-recording and contested-placement paths did not trigger. Contested status
  requires a contradicting **cited** passage, and every cited-passage outcome here was
  `supports` or `partially_supports`; contradicting evidence on uncited passages does not
  reach the report by design. (C4's and C7's contradicting chunks were not among the
  run's selected passages; C6's was E1, a selected passage its sentence does not cite.)
- **Did the final report present anything as contested?** No: no contested section. The
  disagreement appears only in body prose ("Randomized controlled trials of vitamin D
  supplementation have provided inconsistent results", C7).
- **On their face** (a reading, not a measurement), the four `contradicts` relations look
  weak: C4's "contradicting" quote ("does not prevent ARI in older adults") agrees with
  its claim of "little to no difference"; C6's concerns the effect of supplementation
  against a claim about the risk of low vitamin D status; C7's are null trial results set
  against a claim that results are inconsistent. This assessment is **provisional**: the
  saved evidence does not establish whether these relations are genuine
  contradictions, and human review is needed to decide.
- The independent judge found no draft or final sentence contradicted by the pool.

**Conclusion.** Contradiction handling — recording a conflict and presenting a claim as
contested — was **not exercised** by the pilot. The absence of contested findings is not
evidence that it works, nor that the report misses a real conflict: the saved records
cannot settle whether a competent detector should have flagged one here. It remains a
test-coverage gap.
