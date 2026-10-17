# AutoResearch AI

An evidence-grounded agentic research system. Give it a research topic; it
produces a research report where **every claim is traceable to a real source**,
and it measures how well it did.

> **Status: complete — stage 22 of 22, final analysis done.**
> The full pipeline runs end to end (plan → retrieve → write → extract claims →
> verify → validate citations → final report), the benchmark was frozen before
> evaluation, and the pre-registered final analysis has been executed on the frozen
> test set. **The headline results are below.** See
> [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) for exactly what was done, stage by
> stage, and [DATA_MODEL.md](DATA_MODEL.md) for the schema.

---

## What makes this different from "ask a chatbot"

A language model asked for a research report will produce fluent text with
plausible-looking citations, some of which do not exist. This project treats
that as the problem to be measured and reduced. It does so by:

1. Decomposing the topic into sub-questions (planning)
2. Retrieving real sources — web + academic papers
3. Writing the report grounded in retrieved passages
4. **Extracting every atomic claim** from the report
5. **Verifying each claim** against retrieved evidence
6. **Validating each citation** actually exists and supports its claim
7. **Detecting conflicts** between sources
8. Keeping a **traceability chain**: claim → passage → source → URL
9. Scoring the run with quality metrics
10. **Benchmarking** three configurations against each other

### The configurations under comparison

| Arm | What it is | Purpose |
|---|---|---|
| `model_only` | no sources (parametric knowledge) | the ungrounded baseline |
| `hybrid` | + web search (Tavily) | does web grounding help? |
| `search_grounded` | + academic papers + RAG over retrieved snippets and abstracts (no full-text fetching, no claim verification) | does deep grounding help more? |
| `full_autoresearch_verified` | search-grounded + claim verification + post-verification final report | the complete system, verification as a product cost |

The project's result is not just the report generator — it is the **measured
evidence of what grounding and verification buy**.

---

## Final results (frozen test set)

The final analysis was pre-registered and locked before launch
([docs/FINAL_ANALYSIS_PLAN.md](docs/FINAL_ANALYSIS_PLAN.md)), run once on a frozen
test set the product code never saw twice, and judged **arm-blind** against
identical evaluator-owned evidence pools (metrics-v2 — built because the v1
self-grounding metrics structurally zero the baseline; see
[docs/EVALUATION_AUDIT.md](docs/EVALUATION_AUDIT.md)).

**Primary result — external support rate, baseline vs full system, 6 matched
questions, one shared denominator:**

| Question | `model_only` | `full_autoresearch_verified` |
|---|---|---|
| conflicting_literature | 0.643 | 0.700 |
| false_premise | 0.286 | **0.909** |
| insufficient_evidence | 0.316 | 0.571 |
| multi_paper_synthesis | 0.429 | 0.733 |
| normal_research | 0.526 | 0.545 |
| numerical_claims | 0.533 | 0.545 |
| **macro average (primary)** | **0.4554** | **0.6675** |

The full system's claims are **+0.21 more externally supportable than the
baseline's, leading on all six questions**, and the direction replicates on the
development (+0.117) and validation (+0.096) sets. It does this while keeping an
external contradiction rate close to the baseline's (0.064 vs 0.048; the
unverified grounded arms sit near 0.100) — and, from the v1 suite, it is the only
configuration whose claims arrive with validated citations (citation precision
0.710, coverage 0.687), at roughly 19× the baseline's token cost.

Honest bounds, carried from the pre-registration: supportability against retrieved
pools is **not** ground-truth factual accuracy; n = 6 matched questions in a
single pass, so no statistical-significance claim is made; the external judge
validated 9/11 on labeled cases and its known failure mode (compound
partial-support claims) is documented, not tuned away. Every per-claim verdict,
pool hash, and integrity check is in
[`data/benchmarks/results/test/20261013T154641Z-v2eval/`](data/benchmarks/results/test/20261013T154641Z-v2eval/FINAL_ANALYSIS.md),
with 0/375 judging failures and source files verified byte-identical to the
committed blobs before and after the run.

---

## Tech stack

We are **not training a model.** Everything below is orchestration we build.

| Concern | Choice |
|---|---|
| Foundation model | OpenAI API |
| Embeddings | Sentence Transformers (local, free) |
| Vector store | ChromaDB (embedded, persistent) |
| Web search | Tavily |
| Academic search | Semantic Scholar API |
| Structured data | PostgreSQL |
| Backend | FastAPI (Python) |
| Frontend | React + Vite |

---

## Repository layout

```
autoresearch-ai/
├── backend/            FastAPI service — orchestration, retrieval, evidence, evaluation
├── frontend/           React + Vite UI
├── docs/               Architecture, ADRs, API notes, evaluation protocol
├── data/               Vector store, benchmark topic sets, exports (mostly gitignored)
├── scripts/            Dev helper scripts
├── notebooks/          Exploratory analysis of benchmark results
├── .env.example        Every environment variable the system reads
├── ARCHITECTURE.md     How the system is designed and why
└── DEVELOPMENT_LOG.md  Stage-by-stage build journal
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full module-by-module breakdown.

---

## Running it

### Prerequisites

- **Python 3.11** — required, see the note below
- **Node.js 18+**
- **PostgreSQL 14+** — needed now, for persistence
- A **Tavily** key — needed now, for web search ([free tier](https://app.tavily.com))
- A Semantic Scholar key — **optional**; the public API works without one, a key only
  raises the quota
- An **OpenAI** key — needed for generation, claim extraction, and verification

> **⚠️ Python version:** use **3.11**. `torch` / `sentence-transformers` /
> `chromadb` do not reliably ship wheels for 3.14, so a 3.14 venv will fail or
> try to build from source when the retrieval stage lands. `pyproject.toml` pins
> `requires-python = ">=3.11,<3.14"`.
>
> On Windows, `py -3.11 -m venv .venv` picks the right interpreter even when
> `python` resolves to a newer one.

### Backend

```bash
cd backend
py -3.11 -m venv .venv             # Windows
# python3.11 -m venv .venv         # macOS / Linux

source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate        # macOS / Linux

pip install -r requirements-dev.txt
pip install -r requirements.txt    # adds the PostgreSQL driver
cp ../.env.example ../.env
```

### Database

Edit `.env` and replace `CHANGEME` in `DATABASE_URL` with your PostgreSQL password:

```
DATABASE_URL=postgresql+psycopg://postgres:<your password>@localhost:5432/autoresearch
```

`CHANGEME` is deliberate — a fresh checkout must fail to connect rather than silently
reach a real database. Then create the database and apply the migration:

```bash
cd backend
python ../scripts/init_db.py
```

That script checks whether the target database is already reachable and only creates
it if not — which matters for managed PostgreSQL (Neon, Supabase, RDS), where the
database is pre-provisioned and the application role usually cannot `CREATE DATABASE`
at all. It then runs `alembic upgrade head` and prints the applied revision. It is
idempotent, and it never takes a password on the command line (which would put the
credential in your shell history). Equivalent by hand:

```bash
createdb autoresearch        # or: CREATE DATABASE autoresearch;
alembic upgrade head
```

### Start the backend

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

Verify:

| URL | Expected |
|---|---|
| http://localhost:8000/ | name, version, environment, stage, where to find docs and health |
| http://localhost:8000/api/health | liveness: `{"status":"ok",...}` — works with the database down |
| http://localhost:8000/api/health/ready | readiness: verifies the database; 503 with the reason if it is unreachable |
| http://localhost:8000/docs | interactive OpenAPI page |
| http://localhost:8000/api/v1/system/capabilities | which integrations are configured |
| http://localhost:8000/nope | the error envelope, with a `request_id` |

Run the tests:

### Try a search

```bash
# Web pages, via Tavily
backend/.venv/Scripts/python.exe scripts/search_web.py "does retrieval reduce factual errors"

# Academic papers, via Semantic Scholar
backend/.venv/Scripts/python.exe scripts/search_papers.py "retrieval augmented generation" --limit 5
```

Both print candidate sources with canonical URLs or DOIs, identity fingerprints, and
the **evidence depth** of each — `snippet` for a web result, `abstract` for a paper
whose abstract the provider returned, and `metadata` for one where it did not, meaning
there is no text for that paper at all.

The web search costs Tavily credits, which is why both are scripts you run deliberately
rather than part of the test suite — the suite is fully mocked and makes **no network
requests**.

What they print are *candidates*: things a ranking model thinks are topical. Nothing
has been fetched, nothing downloaded, nothing checked against a claim, and the services
are written so nothing downstream can mistake one for evidence.

Semantic Scholar allows one request per second across all endpoints, so the client
throttles itself below that before sending — `--limit` controls results per query, not
request rate.

### Run the tests

```bash
cd backend
pytest              # 860 tests
ruff check .        # lint
mypy app            # types
```

The suite needs **no running PostgreSQL**: it uses a throwaway SQLite database with
foreign keys enforced, applies the real Alembic migration to it, and separately checks
that the same schema compiles for the PostgreSQL dialect.

That is not a substitute for a live run, and the project has learned not to treat it
as one — see the stage 5 entry in DEVELOPMENT_LOG.md. The schema is verified against
PostgreSQL 18.6 (Neon): 17 tables, 40 check constraints, 13 unique constraints, 26
foreign keys, and zero pending differences against the models.

**A note if you use a serverless provider:** Neon suspends idle compute, so the first
connection after a pause can take over a second. `/api/health/ready` measures and
reports that latency; set your probe timeout above it, or the instance will look
unready while it is merely waking up.

### Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
npm test             # 67 tests
npm run lint
```

Open http://localhost:5173. You should see the dashboard, a header badge reading
**"Backend v0.1.0 · development"**, and six pages in the sidebar. If the badge says
**"Backend offline"**, the backend isn't running on port 8000.

Six pages are routed, and every one reads real backend data:

| Page | What it shows |
|---|---|
| Dashboard | live run list, integration status, and the benchmark headline |
| New Research | submits to `POST /api/v1/research/start` and renders the finished run |
| Research Results | stored runs: header, plan, counts, and the report (draft or final, labelled) |
| Sources | every source a run retrieved — label, retriever, rank, evidence depth |
| Evidence Audit | each claim's verdict, citation outcomes, rationales, and conflicts |
| Evaluation | the committed benchmark comparison, served verbatim from the artifacts |

**No page shows sample or mock research output.** Everything rendered is a stored
row or a committed artifact read back; an empty database shows an empty state, a
missing artifact shows "no benchmark artifacts", and a failed request shows the
backend's error code and request id. This project exists to measure how often
generated text is unsupported, and invented output in a screenshot would undermine
the thing being measured.

The dev server proxies `/api` to port 8000, so the browser only talks to the Vite
origin and CORS never applies in development. For a deployed build, set
`VITE_API_BASE` to the backend's origin.

---

## Reproducing the results

- The frozen benchmark sets, freeze manifest, and evaluator-owned pools live under
  [`data/benchmarks/`](data/benchmarks/); prompts are versioned in
  `backend/app/prompts/`.
- The evaluation protocol, metric definitions, and audit are in
  [`docs/`](docs/) — start with `FINAL_ANALYSIS_PLAN.md`, `METRICS.md`, and
  `EVALUATION_AUDIT.md`.
- Every result directory carries its raw per-claim verdicts, a SHA-256 manifest,
  and the pool hashes used for each row; comparison tables are regenerated by the
  runner, never hand-edited.

---

## Academic honesty note

This is a college project. The point of the evidence layer is that the system's
own output can be audited — including by whoever grades it. Benchmark numbers
are reproducible from `data/benchmarks/` plus the committed prompt versions in
`backend/app/prompts/`; re-running a benchmark regenerates the comparison tables
rather than hand-editing them.
