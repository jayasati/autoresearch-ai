# Validation-set experiment protocol — finalized for approval (not yet run)

**Status: LOCKED PENDING APPROVAL.** Nothing has been run against the validation
set; the frozen test set is untouched; nothing is committed. This document is the
exact protocol the validation experiment will execute once approved. Any deviation
after approval requires a new version of this document.

## Dataset identity

| | |
|---|---|
| Set | `validation` v1.0.0 (`data/benchmarks/validation.json`) |
| Content fingerprint | `43d63fb1c0bc…` (canonical hash, recorded at stage 15; recomputed at load) |
| Items | 8, one per category |
| Modifiable? | Refinable per stage-15 governance, but **not modified** for this run — it runs as committed |

## Run matrix

8 items × 4 arms = 32 cells, sequential, one pass:

| Arm | Mode | Product scope |
|---|---|---|
| A `model_only` | model_only | synthesis only |
| B `hybrid` | hybrid | retrieval + synthesis |
| C `search_grounded` | search_grounded | retrieval + indexing + synthesis |
| D `full_autoresearch_verified` | search_grounded | C + extraction + verification + final report |

## Constants (identical to the stage-19 development run; no tuning since)

- Model `gpt-4o-mini`, temperature unset (provider default), depth `quick`
  (profile: 4 web + 4 academic, top_k 6, 900-token report budget).
- Prompts: `claim-extraction-v1`, `verification-v1`, `relevance-judge-v1`,
  external judge `external-verification-v1`. **The judge's measured partial-support
  weakness (9/11 on the labeled set v1.1.0; 0/2 on compound partials, judged
  contradicted) is documented and deliberately not tuned for this run.**
- Pool version **`pool-v1`**: validation-question pools do not exist yet and will be
  built once at run time (8 web + 8 academic per question, evaluator-owned, cached
  with canonical hashes under `data/benchmarks/pools/pool-v1/`, reused identically
  across arms; an existing cache is never refreshed).
- Metrics: v1 set under `metrics-v1`, external set under `metrics-v2`, exactly as
  defined in `data/benchmarks/METRICS.md` (formulas, denominators, eligibility,
  empty-denominator and failure semantics all stated there; v2 database rows remain
  deferred — files only).

## Exclusions and failure policy (unchanged from stages 16–19)

- Any exception in a cell → a recorded error row with its code; the grid continues;
  after a database-flavoured failure the runner waits for recovery before the next
  cell. **Failed cells stay visible**: per-arm `items ok / failed`, `failure_rate`
  (= failed / attempted), and per-metric `n` appear in every output; means cover
  completed cells only, and the matched-items comparison (audit CD-1) names each
  arm's missing items whenever subsets differ.
- A claim whose external judging fails twice is a counted `judging_failure`,
  excluded from v2 denominators — never defaulted to a verdict.
- A pool failure is an explicit `external: unavailable` with None metrics.
- No claim cap: every extracted claim is judged (dev maximum observed: 28/report).

## Reporting commitments

Actual results only; per-question rows in results.csv/json (per-question variation
reported); failure counts with denominators; structural baseline values footnoted;
**no factual-accuracy claim and no statistical-superiority claim from external
supportability alone** — at n=8 single-pass, differences are reported with their n
and the run-variance caveats already on record (C/D reversals, conflict-detection
variance with its stage-20 root-cause analysis).

## Exact commands to be executed on approval

```powershell
# from backend/, venv python
# 1. warm the database (retry-safe)
./.venv/Scripts/python.exe -c "from sqlalchemy import text; from app.db.session import get_engine; print(get_engine().connect().execute(text('SELECT 1')).scalar_one())"

# 2. the validation experiment (background, ~2h)
./.venv/Scripts/python.exe -m app.services.benchmark.runner --set validation --depth quick
```

No other command touches the experiment. Outputs land in a new directory
`data/benchmarks/results/validation/<timestamp>/` (results.json / results.csv /
evaluation_summary.md), leaving every development and test artifact untouched —
the stage-19 development artifact and the pool caches are additionally pinned by
sha256 manifests (`MANIFEST.sha256`) written at stage 20.

## Explicitly out of scope for this run

Committing anything; the frozen final test set; Part 3b (the citing baseline);
any prompt, threshold or formula change; regeneration of any historical output.
