# Development Log

A running journal of what was built, when, and why. Newest entry at the top.
This file is the honest record: it says what is *not* done as plainly as what is.

---

## Stage 2 — Backend foundation — 2026-10-06

**Goal:** a complete, correct application foundation — configuration, logging,
correlation, error handling, versioned routing, health — with a real test suite.
Still no research functionality.

### What was implemented

| Requirement | How |
|---|---|
| FastAPI application | `create_app()` factory + module-level `app`; a factory so tests can build isolated instances |
| CORS for the React frontend | origins from `CORS_ORIGINS`; `X-Request-ID` / `X-Response-Time-ms` in `expose_headers` so browser JS can actually read them |
| `GET /` | service info: name, version, environment, build stage, and links to docs / health / API version |
| `GET /api/health` | liveness; does no I/O, so it cannot fail because an upstream is slow |
| API versioning structure | `/api/health` unversioned, `/api/v1/...` versioned; a future `/api/v2` mounts as a sibling router |
| Central configuration | one `Settings` class; only `API_PREFIX` is settable, version paths are derived from it |
| Environment variable loading | pydantic-settings over `.env`, case-insensitive, unknown keys ignored |
| Proper error handling | exception hierarchy + four handlers producing one uniform envelope |
| Logging configuration | one root handler, request id on every line, uvicorn's loggers folded into the same format |

### Files added

- `app/core/exceptions.py` — `AppError` hierarchy: `BadRequestError`,
  `ValidationError`, `NotFoundError`, `ConflictError`, `ConfigurationError`,
  `ExternalServiceError`, `RateLimitError`, `BudgetExceededError`
- `app/core/errors.py` — four handlers (`AppError`, `RequestValidationError`,
  `StarletteHTTPException`, catch-all `Exception`) and the shared envelope
- `app/core/middleware.py` — `RequestContextMiddleware`: correlation id + timing
- `app/utils/request_context.py` — the request-id `ContextVar`
- `app/schemas/common.py` — `ErrorResponse`, `ErrorDetail`, `HealthResponse`,
  `ServiceInfoResponse`
- `app/api/router.py` — the `/api` router, where versioning is expressed
- `app/api/routes/health.py` — the unversioned health route
- `requirements-base.txt`, `requirements-ml.txt` — dependency layering
- `tests/conftest.py`, `tests/test_endpoints.py`, `tests/test_errors.py`,
  `tests/test_config.py`, `tests/test_middleware.py`

### Files changed

- `app/main.py` — rewritten: lifespan, CORS, middleware, handler registration,
  `GET /`, startup credential warning
- `app/core/config.py` — `API_PREFIX` replaces `API_V1_PREFIX` as the *setting*;
  added derived `API_V1_PREFIX`, `HEALTH_URL`, `is_production`,
  `integration_status`, `missing_credentials`
- `app/core/logging.py` — rewritten: request-id filter, uvicorn logger alignment
- `app/api/v1/router.py` — export renamed to `router`, now mounted at `/v1`
- `app/api/v1/routes/system.py` — capabilities reads `Settings.integration_status`
- `requirements.txt`, `requirements-dev.txt` — layered on `requirements-base.txt`
- `frontend/src/api/client.js` — `/api/health`; added `ApiError` parsing the envelope
- `frontend/src/App.jsx` — health field renamed `env` → `environment`
- `frontend/vite.config.js` — dropped the now-redundant `/health` proxy entry
- `.env.example`, `README.md`, `docs/api/README.md`, `.github/workflows/ci.yml`
- `tests/test_health.py` — **deleted**, replaced by `tests/test_endpoints.py`

### Commands used

```bash
# Virtual environment rebuilt on Python 3.11 (was 3.14)
cd backend
py -3.11 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# Verification
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/python.exe -m pytest --cov=app --cov-report=term-missing
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m mypy app
./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

cd ../frontend && npm run build
```

### Tests performed

**80 automated tests, all passing, 100% statement coverage of `app/`.**

| Module | Tests | Covers |
|---|---|---|
| `test_endpoints.py` | 14 | `GET /`, `GET /api/health`, system routes, OpenAPI schema, `/docs` |
| `test_errors.py` | 25 | exception hierarchy, all four handlers, request-id resolution, no leakage when `DEBUG=false` |
| `test_config.py` | 24 | defaults, env loading, validation rejection, derived paths, credential status, caching |
| `test_middleware.py` | 17 | request-id generation and propagation, timing header, logging config, CORS |

Plus **22 live checks against a real uvicorn server** — started programmatically,
exercised over HTTP with `httpx`, shut down — covering every endpoint, both error
envelopes, the correlation headers, CORS preflight and rejection, and
`/docs` + `/openapi.json`.

### Result

| Check | Result |
|---|---|
| `pytest` | **80 passed** in 0.68s |
| coverage of `app/` | **100%** (335 statements, 0 missed) |
| `ruff check .` | All checks passed |
| `mypy app` | Success: no issues found in 29 source files |
| uvicorn startup | clean; logs the stage, CORS origins, and which credentials are missing |
| live HTTP checks | **22/22 passed** |
| `npm run build` | built in 7.4s |

### Decisions made

1. **Health is unversioned, at `/api/health`.** Health is a property of the
   process, not of the API contract. Putting it under `/v1` would mean container
   probes and monitoring need updating whenever the API version bumps — a cost
   paid forever for no benefit. The bare `/health` from stage 1 was removed rather
   than kept as an alias, so there is exactly one canonical path.

2. **Health does no I/O.** A liveness check that can fail because the database is
   slow triggers restarts that fix nothing. A *readiness* check that does verify
   dependencies arrives in stage 3, when there is a database to verify.

3. **Application code raises `AppError`, never `HTTPException`.** Retrieval,
   evidence and evaluation can then signal failure without importing FastAPI or
   choosing a status code; one module does that translation.

4. **`external_service_error` (502) is distinct from `internal_error` (500).** A
   run that failed because Tavily was down is not the same result as a run that
   failed because our logic is wrong, and the benchmark must not conflate them.

5. **Every error carries a stable `code`.** The frontend switches on `code`, never
   on `message`, so wording can improve without breaking the UI.

6. **Correlation ids are honoured, not overwritten.** An inbound `X-Request-ID`
   passes through, so a trace can be followed from the frontend into the backend,
   and every log line a request emits carries the same id.

7. **Requirements are layered.** `requirements-base.txt` (foundation) →
   `requirements.txt` (+ database, OpenAI) → `requirements-ml.txt` (+ torch,
   ChromaDB), with `requirements-dev.txt` on the base. Forced by a real problem
   (see *Issues found*), and worth it: `pip install -r requirements-dev.txt` now
   finishes in seconds instead of stalling for minutes on a dependency graph that
   nothing in stages 1–3 uses.

8. **Only `API_PREFIX` is configurable.** `API_V1_PREFIX` and `HEALTH_URL` are
   derived from it, so a prefix can never end up half-renamed.

### Issues found

1. **`pip install -r requirements-dev.txt` stalled.** The stage-1 file pulled
   `sentence-transformers` → `torch` (~2.5 GB) and then sat in pip's dependency
   backtracking for minutes. Nothing in stages 1–3 imports any of it. **Fixed** by
   splitting requirements into layers; the ML layer installs at stage 4.
   *Consequence:* whether torch and chromadb actually resolve on 3.11 is now
   untested — still a stage-4 risk, no longer verified early.

2. **Unhandled exceptions reported `request_id: "-"`.** Starlette's
   `ServerErrorMiddleware` sits *outside* application middleware, so by the time
   the catch-all handler ran, the `ContextVar` holding the id had already been
   reset — meaning precisely the errors that most need correlating arrived with no
   id. **Fixed** by resolving the id from `request.state` first, with the context
   variable as fallback. Covered by a named regression test.

3. **`capabilities` reported `openai: true` for a placeholder key.**
   `.env.example` ships `sk-replace-me`, a non-empty string, so a `bool()` check
   passed it. The startup warning checked for the placeholder and the endpoint did
   not — the two disagreed. Found by reading the live server's actual output, not
   by a test. **Fixed** by defining "configured" once, in
   `Settings._is_real_credential`, and having both read it.

4. **Python version.** The venv was rebuilt on **3.11.9** (was 3.14.4), matching
   the `requires-python = ">=3.11,<3.14"` pin. CI now uses 3.11 as well.

5. **Two lint findings** on the first run — an unused import and `Depends()` in a
   default argument — fixed by switching to the `Annotated` dependency idiom.

### Not implemented

**AI functionality is NOT implemented yet.** No OpenAI calls, no prompts, no
planning, no retrieval, no embeddings, no vector store, no claim extraction, no
verification, no citation validation, no conflict detection, no metrics, no
benchmark. No database tables, no migrations, no research endpoints, no
authentication, no background jobs. `capabilities.implemented` is `[]`, and the
`GET /` response says so in its `stage` field.

### Next stage

**Stage 3 — data model and run lifecycle.** SQLAlchemy models for the tables in
ARCHITECTURE.md §5; Alembic initialised with a first migration; `POST /api/v1/research`
and `GET /api/v1/research/{id}` persisting `RunStatus` transitions; a readiness
endpoint that does verify the database. Done when a run can be created and polled
through the API with no research actually happening.

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
