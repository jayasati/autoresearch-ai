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
