# Development Log

A running journal of what was built, when, and why. Newest entry at the top.
This file is the honest record: it says what is *not* done as plainly as what is.

---

## Stage 1 — Project scaffolding — 2026-10-06

**Goal:** a clean, navigable skeleton for the whole system, with the
configuration surface and the shared vocabulary fixed before any logic is
written.

### Done

- Repository structure created: `backend/`, `frontend/`, `docs/`, `data/`,
  `scripts/`, `notebooks/`
- `backend/app/core/config.py` — every tunable the system will read, in one
  Pydantic `Settings` class
- `backend/app/core/constants.py` — the shared enums: `ResearchMode`,
  `SourceType`, `RunStatus`, `VerificationVerdict`, `CitationStatus`,
  `ConflictType`
- `backend/app/core/logging.py` — startup logging config
- `backend/app/main.py` — FastAPI app factory, CORS, lifespan hook, `/health`
- `backend/app/api/v1/` — router aggregation + `system` routes (`/ping`,
  `/capabilities`)
- `backend/tests/test_health.py` — three smoke tests
- `requirements.txt`, `requirements-dev.txt`, `pyproject.toml` (pytest, ruff,
  mypy config)
- `frontend/` — Vite + React shell that calls `/health` and reports whether the
  backend is reachable; API access centralised in `src/api/client.js`
- `.gitignore`, root `.env.example`, `frontend/.env.example`
- `README.md`, `ARCHITECTURE.md`, this log
- A `README.md` in each backend module directory stating what will live there
- `.gitattributes` (LF normalisation, so the repo is identical across machines)
- `.github/workflows/ci.yml` — lint + test backend, build frontend
- `docs/adr/0001-post-hoc-verification.md` — first decision record
- `git init` run; 79 files staged, nothing committed yet

### Verified on this machine (2026-10-06)

| Check | Result |
|---|---|
| `pytest` (backend) | 3 passed |
| `ruff check .` (backend) | all checks passed |
| `GET /health` | `{"status":"ok","version":"0.1.0","env":"development"}` |
| `GET /api/v1/system/ping` | `{"ping":"pong"}` |
| `GET /api/v1/system/capabilities` | 3 modes listed, integrations reported |
| `GET /docs` | HTTP 200 |
| `npm install` + `npm run build` | 153 packages, built in 8.9s, 144 kB bundle |
| `.gitignore` behaviour | `node_modules/`, `.venv/`, `dist/`, `.env` ignored; both `.env.example` files tracked |

Installed with FastAPI 0.142.2 / Pydantic 2.13.5 on Python 3.14 (stage-1 subset
only — no ML dependencies).

### Decisions made

1. **The enums came first.** `ResearchMode`, `RunStatus`,
   `VerificationVerdict`, `CitationStatus` and `ConflictType` are defined before
   any code uses them. The database schema, the API responses and the React UI
   all derive their vocabulary from this one file, so they cannot drift apart as
   the project grows.

2. **Verification is a separate post-generation stage,** not a constraint applied
   while writing. If the writer graded its own groundedness, hallucination would
   become unmeasurable — the measurement and the behaviour would share a failure
   mode. See ARCHITECTURE.md §1.

3. **Three pipelines as three files,** not one function with a mode flag. The
   benchmark's credibility rests on the differences between configurations being
   readable in the source.

4. **Citation failures are four distinct outcomes,** not one "bad citation" flag.
   A fabricated source and a real source cited for the wrong claim are different
   defects with different causes, and the project should report them separately.

5. **An `llm_call_log` table is in the data model from the start.** Without a
   per-call record of model, prompt version, tokens and cost, neither the
   benchmark results nor the API bill can be explained afterwards.

6. **Embeddings run locally.** Sentence Transformers over an embedding API:
   embeddings are called thousands of times during chunking, and local inference
   makes that free.

### Issues found

- **Python 3.14 on this machine.** `torch` / `sentence-transformers` /
  `chromadb` do not reliably publish wheels for 3.14 yet; installing them will
  likely fail or attempt a source build. `pyproject.toml` therefore pins
  `requires-python = ">=3.11,<3.14"`. **Action before stage 4: create the venv
  with Python 3.12.** The stage-1 backend (FastAPI only) runs on 3.14 fine, so
  this is not blocking yet.
- Not a git repository yet. `.gitignore` is in place; `git init` and the first
  commit are the user's call.

### Not implemented (deliberately)

No business logic exists. Specifically: no planning, no retrieval, no
embeddings, no vector store, no database tables, no claim extraction, no
verification, no citation validation, no conflict detection, no metrics, no
benchmark, and no research UI. Every one of these has a directory and a
`README.md` describing what will go in it, and nothing more.

---

## Roadmap

Each stage ends with something runnable and testable. No stage depends on a
later one.

### Stage 2 — Data model and run lifecycle
- SQLAlchemy models for the tables in ARCHITECTURE.md §5
- Alembic initialised; first migration
- `POST /api/v1/research` creates a run row; `GET /api/v1/research/{id}` reads it
- `RunStatus` transitions persisted and exposed
- **Done when:** a run can be created and polled through the API, with no
  research actually happening.

### Stage 3 — Model-only pipeline (the baseline)
- OpenAI client wrapper with retry, timeout, and `llm_call_log` writing
- `prompts/planner.md`, `prompts/synthesizer.md` (versioned)
- `pipelines/model_only.py` end to end
- **Done when:** a topic produces a stored report using no external sources.
  This is the baseline the other two modes must beat.

### Stage 4 — Retrieval
- Tavily web search; Semantic Scholar academic search
- Fetcher (clean text extraction), chunker with character offsets
- Sentence Transformers embedder, ChromaDB store, top-k RAG retrieval
- `pipelines/hybrid.py`, `pipelines/search_grounded.py`
- **Done when:** reports cite real, retrievable sources and every chunk can be
  traced to a character span in its source. *Needs the Python 3.12 venv.*

### Stage 5 — Evidence layer
- Claim extraction, evidence linking, per-claim verification
- Citation validation (valid / broken / misattributed / fabricated)
- Conflict detection, traceability serialization
- **Done when:** a report comes back annotated — every claim carries a verdict
  and a quoted supporting span, or an explicit "no evidence".

### Stage 6 — Evaluation and benchmark
- Metrics module; benchmark harness over a committed topic set
- Comparison tables across the three modes
- **Done when:** running the benchmark regenerates the comparison table from
  scratch, and the numbers support (or refute) the project's hypothesis.

### Stage 7 — Frontend
- Topic submission, live run timeline, report view with inline citations
- Claim inspector with evidence drill-down
- Benchmark dashboard
- **Done when:** the whole system is usable and auditable without a terminal.

### Stage 8 — Hardening and write-up
- Background job execution, caching, error states
- Final report, figures from `notebooks/`, reproducibility instructions

---

## Log conventions

Each stage entry records: **Done** (what exists now), **Decisions made** (and
why — the reasoning is the part worth keeping), **Issues found**, and
**Not implemented**. Reversed decisions get an ADR in `docs/adr/` rather than a
quiet edit to an earlier entry.
