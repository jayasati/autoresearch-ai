# Benchmark dataset — ground truth and governance

Three question sets probe eight behaviours: normal research, multi-paper synthesis,
conflicting literature, numerical claims, citation verification, insufficient
evidence, false premise, current information. One item per category per set,
validated against `app/schemas/benchmark.py` — a file that fails the schema does not
load at all.

| File | Role | May change? |
|---|---|---|
| `development.json` | debug the pipeline, observe failure modes | yes — version bump + log entry |
| `validation.json` | tune scoring thresholds and prompts, once, after development | yes — version bump + log entry |
| `test.json` | the final evaluation | **frozen** (see below) |
| `test.freeze.json` | the freeze manifest | written only by `write_freeze_manifest` |

## How ground truth is established

Every item names its basis, because the tiers warrant different scoring confidence.
Collapsing them would let weak ground truth masquerade as strong — the exact failure
this project measures in models.

1. **`verifiable_fact`** — checkable against the public record: a paper's retraction
   (Wakefield 1998), a declaration that never happened (the Great Barrier Reef
   "obituary"), a published trial figure (DPP's 58%). Strongest tier; scoring may
   treat deviations as errors.
2. **`established_consensus`** — textbook-level findings reproduced across major
   reviews and trials (statins' per-mmol/L effect, the sleep-mortality U-shape, the
   omega-3 and alcohol disagreements). Strong, but phrased as ranges and
   characterisations rather than exact values, and the item notes name the anchor
   literature.
3. **`behavioral`** — definable *a priori*, before any literature is read: a false
   premise must be challenged; an essentially unstudied question must yield
   "insufficient evidence"; a time-varying answer must be grounded in dated, recent
   sources. These items deliberately store **no answer** — the correct behaviour is
   checkable from the run's own output and citations at evaluation time.
4. **`author_judgment`** — advisory hints only (`expected_sources`,
   `expected_evidence` on softer items). Never used for hard scoring; the schema
   forbids `known_claims` at this tier.

Who wrote it: the items were authored during stage 15 by the project author (with
AI assistance), from literature they could verify at tiers 1–2 and from a-priori
behavioural definitions at tier 3. No benchmark run and no model output contributed
to any expectation in these files, and **no benchmark results exist as of their
creation** — the instrument was finished before the first measurement, which is the
only order in which the measurement means anything.

## The freeze

`test.json` was frozen at creation — before any evaluation has ever run — by
`write_freeze_manifest()`, which records a canonical content hash (key-order- and
whitespace-independent, so identical across operating systems) in
`test.freeze.json`. The loader (`app/services/benchmark/dataset.py`) recomputes the
hash on every load of the test set and **refuses to return a set that does not
match** (`FrozenSetViolation`). Evaluations therefore cannot run against an edited
test set by accident.

Rules, in force from the first freeze:

- **Benchmark results may never motivate a change to the test set.** A test item
  that scores "badly" is reporting on the system, which is its job.
- A genuine pre-evaluation defect in a test item (a typo, a premise that turns out
  to be true) may be fixed **only before final evaluation begins**, with a version
  bump, a re-freeze, and a DEVELOPMENT_LOG entry stating what changed and why.
  After final evaluation begins, the set is immutable; defects are reported in the
  write-up instead.
- Development and validation items may be refined from development-run
  observations; the test set may not be touched for any such reason.
- Tuning (prompts, thresholds, retrieval settings) uses the development and
  validation sets only. The test set is run once per final configuration.

## What the expectations are, and are not

`expected_sources` and `expected_evidence` are hints about what a good run surfaces,
not requirements for exact URLs — search indexes shift daily, and pinning URLs would
measure index drift, not system quality. `known_claims` carry the verdict a correct
verification should reach, at tiers strong enough to say so. `known_conflicts`
describe disagreements the literature is known to contain, as statements of what
each side reports — never an explanation of why they disagree, the same rule the
conflict detector itself obeys.
