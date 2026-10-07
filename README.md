# AutoResearch AI

An evidence-grounded agentic research system. Give it a research topic; it
produces a research report where **every claim is traceable to a real source**,
and it measures how well it did.

> **Status: Stage 7 of 12 — foundations, persistence, and retrieval.**
> The backend serves configuration, logging, error handling, versioned routing and
> health; the full data model is implemented and migrated; the frontend routes six
> pages and reads real system status; web **and academic** search return candidate
> sources. **No LLM calls, no generated text, nothing downloaded, and nothing treated
> as evidence yet.** See
> [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) for exactly what is and is not done, stage
> by stage, and [DATA_MODEL.md](DATA_MODEL.md) for the schema.

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

### The three configurations under comparison

| Mode | Sources | Purpose |
|---|---|---|
| `model_only` | none (parametric knowledge) | the baseline we expect to hallucinate |
| `hybrid` | + web search (Tavily) | does web grounding help? |
| `search_grounded` | + academic papers + RAG over full text | does deep grounding help more? |

The project's result is not just the report generator — it is the **evidence
that grounding measurably improves factuality**.

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

## Running it (stage 3)

Right now "running it" means: the backend boots with full configuration, logging,
error handling and versioned routing, and the frontend loads and shows that it
reached the backend. **No research functionality.**

### Prerequisites

- **Python 3.11** — required, see the note below
- **Node.js 18+**
- **PostgreSQL 14+** — needed now, for persistence
- A **Tavily** key — needed now, for web search ([free tier](https://app.tavily.com))
- A Semantic Scholar key — **optional**; the public API works without one, a key only
  raises the quota
- An OpenAI key — *not needed until stage 8*

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
pytest              # 593 tests
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

Six pages are routed:

| Page | State |
|---|---|
| Dashboard | **real data** — reads integration status from the backend |
| New Research | real, interactive form; submit disabled (no endpoint yet) |
| Research Results | placeholder |
| Sources | placeholder |
| Evidence Audit | placeholder — shows the real verdict/citation vocabularies |
| Evaluation | placeholder — shows metric definitions, no measurements |

Every placeholder names the stage its feature arrives in. **No page shows sample or
mock research output.** That is a deliberate rule, not an oversight: this project
exists to measure how often generated text is unsupported, and invented output in
a screenshot would undermine the thing being measured.

The dev server proxies `/api` to port 8000, so the browser only talks to the Vite
origin and CORS never applies in development. For a deployed build, set
`VITE_API_BASE` to the backend's origin.

---

## Next stage

Stage 8 is fetching and chunking: turning a candidate source into a `document` with
chunks whose character offsets address its text exactly — the offsets every traceability
claim in this project depends on. See the
[roadmap in DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md#roadmap).

---

## Academic honesty note

This is a college project. The point of the evidence layer is that the system's
own output can be audited — including by whoever grades it. Benchmark numbers
are reproducible from `data/benchmarks/` plus the committed prompt versions in
`backend/app/prompts/`; re-running a benchmark regenerates the comparison tables
rather than hand-editing them.
