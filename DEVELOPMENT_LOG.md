# Development Log

A running journal of what was built, when, and why. Newest entry at the top.
This file is the honest record: it says what is *not* done as plainly as what is.

---

## Stage 5 — PostgreSQL persistence — 2026-10-06

**Goal:** a working persistence layer — configuration, sessions, migrations,
repositories, services, transaction handling and database health checking. No
retrieval, no LLM calls.

### What was implemented

| Requirement | How |
|---|---|
| 1. Config through `DATABASE_URL` | one URL, never split into host/user/password; `postgres://` and `postgresql://` rewritten to pin psycopg |
| 2. Connection/session management | lazy engine, pool sizing, pre-ping, bounded connect, `get_db` dependency, `unit_of_work` |
| 3. Models for the core entities | from stage 4, unchanged |
| 4. Alembic | initialised; `env.py` reads `DATABASE_URL` through `Settings`, not `alembic.ini` |
| 5. Initial migration | one revision, all 17 tables, 40 check constraints, with a working downgrade |
| 6. Repository/service abstraction | 15 repositories + `ResearchService`; repositories never commit |
| 7. No hardcoded credentials | `CHANGEME` placeholder, no URL in `alembic.ini`, and a test that scans the tree |
| 8. Transaction handling | `unit_of_work` commits once; nesting joins rather than committing early |
| Database health checking | `/api/health/ready` and `/api/health/database`, kept separate from liveness |

### Files added

- `app/db/session.py` — rewritten: `build_engine`, pool configuration, `get_db`,
  `unit_of_work`, `check_connection`, `require_database`, `reset_engine`
- `app/repositories/` — `base.py`, `research.py`, `evidence.py`, `evaluation.py`,
  `__init__.py`, `README.md` (15 repositories)
- `app/services/research_service.py` — use cases and the transaction boundary
- `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`,
  `migrations/README.md`
- `migrations/versions/20261006_1530_eeba0c5c5e04_initial_schema.py`
- `scripts/init_db.py` — create the database and upgrade to head, idempotent
- `tests/integration/test_persistence.py` (54), `test_migrations.py` (11),
  `test_database_health.py` (20)

### Files changed

- `app/core/config.py` — `DATABASE_URL` normalisation, pool settings,
  `DB_CONNECT_TIMEOUT`, `database_url_safe`, `database_backend`,
  `database_credentials_look_unset`; `integration_status["postgres"]` now reflects
  whether a real credential is set
- `app/api/routes/health.py` — added readiness and database endpoints
- `app/schemas/common.py` — `ServiceDependency`, `ReadinessResponse`
- `tests/conftest.py` — `db_engine` now file-backed and built through `build_engine`;
  added `db_settings` and `service_session`
- `pyproject.toml` — ruff per-file ignores for generated migrations
- `.env.example`, `README.md`, `ARCHITECTURE.md`, `docs/api/README.md`,
  `app/services/README.md`

### Commands used

```bash
cd backend
./.venv/Scripts/python.exe -m pip install "alembic>=1.13" "psycopg[binary]>=3.2"

alembic init -t generic migrations
DATABASE_URL="sqlite:///<tmp>/autogen.db" alembic revision --autogenerate -m "initial schema"
DATABASE_URL="sqlite:///<tmp>/mig.db" alembic upgrade head
DATABASE_URL="sqlite:///<tmp>/mig.db" alembic current
DATABASE_URL="sqlite:///<tmp>/mig.db" alembic downgrade base
DATABASE_URL="sqlite:///<tmp>/mig.db" alembic upgrade head      # round trip

python ../scripts/init_db.py
pytest ; ruff check . ; mypy app
```

### Tests performed

**370 backend tests, all passing** (89 new).

| File | Tests | Covers |
|---|---|---|
| `test_persistence.py` | 54 | connection, create research, create source, create claim, create verification result, retrieve relationships, transaction handling |
| `test_database_health.py` | 20 | liveness with the database down, readiness 200/503, redaction, no hardcoded credentials |
| `test_migrations.py` | 11 | upgrade, downgrade, round trip, revision stamping, constraints preserved, no seed rows, **migration matches the models** |

The six required test areas, named:

| Required | Where |
|---|---|
| database connection | `TestDatabaseConnection` — 6 tests, including that an unreachable database is *reported* rather than raised, and that the error never leaks the password |
| create research | `TestCreateResearch` + `TestStatusTransitions` — 18 tests |
| create source | `TestCreateSource` — 7 tests, including deduplication and re-fetch handling |
| create claim | `TestCreateClaim` — 6 tests, including the four citation outcomes |
| create verification result | `TestCreateVerificationResult` — 6 tests, including the downgrade rule |
| retrieve relationships | `TestRetrieveRelationships` — 7 tests, all three chains |

**Migrations were run**, in both directions, and verified three ways: the schema
matches the models (`compare_metadata` returns an empty diff), the downgrade removes
every table, and up→down→up works.

**Live HTTP checks**, against a real uvicorn server:

- With a reachable database: **11/11** — liveness 200, readiness 200, latency
  measured, all three health paths in the OpenAPI schema.
- Against the **real PostgreSQL 18 service** on this machine: readiness correctly
  returned **503** with the server's own message (`password authentication failed for
  user "postgres"`), the password redacted from `target`, and **liveness still 200** —
  which is the whole point of separating the two.

### Result

| Check | Result |
|---|---|
| `pytest` | **370 passed** in 62s |
| `ruff check .` | All checks passed |
| `mypy app` | Success: no issues found in 49 source files |
| `alembic upgrade head` | 17 tables + `alembic_version`; revision `253b523fd670` |
| migration vs. models | **0 pending differences** |
| `alembic downgrade base` | every table removed |
| up → down → up | works |
| `scripts/init_db.py` | succeeds, idempotent, refuses the `CHANGEME` placeholder |
| live health checks | 11/11 healthy path; 503-with-reason when unreachable |
| **live PostgreSQL 18.6 (Neon)** | **migration applied; 12/12 repository checks; readiness 200** |
| frontend | unchanged — 67 tests still passing |

### Decisions made

1. **One `DATABASE_URL`, never split into parts.** A single URL is what psql, Alembic
   and every hosting platform already understand; splitting it into host/user/password
   settings would create four places a credential could leak instead of one.

2. **`postgres://` and `postgresql://` are rewritten to pin psycopg.** Those are the
   forms hosting dashboards hand out, and SQLAlchemy's default driver for them is
   psycopg2 — which this project does not install. The failure would be a confusing
   `ImportError` at first connection instead of a clear configuration error.

3. **The default URL contains `CHANGEME`.** A fresh checkout must fail to connect
   rather than silently reach a real database, and `capabilities` reports
   `postgres: false` while the placeholder is present — the same rule already applied
   to `sk-replace-me`.

4. **No URL in `alembic.ini`.** That file is committed, so a URL there is a credential
   in version control. `env.py` reads it through `Settings`, which also guarantees
   migrations and the service cannot disagree about which database they mean.

5. **Repositories never commit.** The transaction belongs to whoever opened the unit of
   work. A research run is a connected graph — report, claims, evidence, citations,
   verdicts — and committing it in pieces means a mid-way failure leaves a run whose
   claim set is silently incomplete. Every metric over it would then be wrong in a way
   that looks like a finding rather than a bug.

6. **`unit_of_work(session)` joins rather than opening a second transaction.** So a
   service method is safe standalone *or* as one step of a larger operation, and in the
   second case it cannot commit work the caller may still abandon. A test asserts it.

7. **Liveness and readiness are different endpoints.** `/api/health` does no I/O;
   `/api/health/ready` runs `SELECT 1`. A liveness probe that failed on a slow database
   would make the orchestrator restart a healthy process — fixing nothing and removing
   capacity exactly when the system is struggling. Readiness failing instead removes
   the instance from the load balancer, and it recovers by itself.

8. **A 503 from readiness still returns the full body**, including the reason and which
   database was checked (password redacted). A probe that says only "not ready" forces
   whoever is paged to go and find out why.

9. **Status transitions are validated in the service.** A completed run cannot return to
   `retrieving`, which would produce a second report for one run and break the
   one-report-per-run invariant from the far side. Re-running a topic creates a new run,
   which is what keeps a benchmark reproducible rather than editable.

10. **A bounded connect timeout.** Without one, an unreachable host can block until the
    OS gives up — hanging the readiness probe, the one request that must always answer
    promptly. Found because a test hung.

### Issues found

1. **`autoflush=False` was a mistake.** I had set it for "explicit ordering". With it
   off, a query in the same unit of work does not see rows added but not yet flushed,
   so a repository read can silently miss a write the same operation just made — a
   stale read that looks like a missing row. Caught by a failing test; switched to
   SQLAlchemy's default.

2. **A test hung for over seven minutes.** Two causes, both real bugs rather than test
   artefacts: the engine had no `connect_timeout`, so connecting to a dead host blocked
   (which would hang the readiness endpoint in production); and the credential scan used
   `rglob("*")`, which descends into `node_modules`. Fixed with `DB_CONNECT_TIMEOUT` and
   `os.walk` with pruning.

3. **`/api/health/database` bypassed its own test.** It did
   `from app.db.session import get_engine` and called it, so patching the module
   attribute had no effect — and the endpoint resolved the engine differently from
   `/api/health/ready`. Collapsed to one resolution path.

4. **The health response labelled a SQLite connection `postgresql`.** A hardcoded
   dependency name. Now derived from the URL — a small lie, but told at exactly the
   moment someone is diagnosing a misconfigured environment.

5. **An in-memory SQLite database cannot support the transaction tests.** A second
   connection sees its own empty database, which would make every "is this committed
   yet" assertion vacuous. The test engine is now file-backed and built through
   `build_engine`, so the real construction path is exercised too.

6. **Monkeypatching `migrations.env` broke the test collection.** Importing that module
   outside a migration run fails, because `alembic.context` is only populated while one
   is in progress. Patching `app.core.config.get_settings` is enough, since Alembic
   re-imports `env.py` on every run.

7. **CI caught a layering mistake the local environment hid.** `alembic` and
   `psycopg` were in `requirements.txt`, one layer above the `requirements-base.txt`
   that `requirements-dev.txt` installs — but the test suite needs both: it applies
   the real migration, and it exercises the PostgreSQL code path deliberately
   (unreachable server, URL normalisation, DDL compilation). My own virtualenv had
   them installed from earlier commands, so the suite passed locally and failed to
   even *collect* in CI. Both moved into the foundation layer, and the fix was then
   verified by building a **clean Python 3.11 venv from `requirements-dev.txt` alone**
   and running the whole suite in it: 368 passed, ruff and mypy clean.

8. **Two bugs that only a live PostgreSQL could find** (found after the first commit of
   this stage, when the user supplied a Neon connection string):

   **`str(url)` masks the password.** `init_db.py` built its admin connection with
   `str(admin_url)`, and SQLAlchemy's `URL.__str__` renders the password as `***` by
   default. The script was therefore authenticating with three literal asterisks, and
   reported `password authentication failed` — a failure indistinguishable from a wrong
   password, which sends you looking in entirely the wrong place. Fixed with
   `render_as_string(hide_password=False)`.

   **Two tables shared a constraint name.** `report_section` and `claim` both declared
   `UniqueConstraint(..., name="position_unique_per_report")`. An **explicit** name
   bypasses the metadata naming convention entirely — SQLAlchemy only generates one when
   none is given — so both asked for the same name. SQLite accepts that; PostgreSQL
   scopes constraint names per schema and rejected the second part-way through
   `alembic upgrade head`. Every unique constraint is now `uq_<table>_...`, the migration
   was regenerated, and **two new tests assert that no two constraints or indexes
   anywhere in the schema share a name** — so the next one is caught with no database at
   all. PostgreSQL's transactional DDL rolled the failed migration back completely,
   leaving nothing to clean up.

   The lesson worth keeping: the suite passed on SQLite *and* the DDL compiled correctly
   for the PostgreSQL dialect, and the schema still failed to deploy. Dialect compilation
   is a useful check, not a substitute for applying the migration.

9. **`init_db.py` assumed it could create the database.** It connected to the `postgres`
   maintenance database first and tried `CREATE DATABASE`. Managed providers pre-provision
   the database and the application role usually cannot create one, so that order fails
   against every hosted PostgreSQL for no reason. It now tries the target first and only
   falls back to the maintenance database when the target is unreachable.

### Verified against live PostgreSQL

Run against **PostgreSQL 18.6 on Neon** (`ap-southeast-1`, pooled endpoint,
`sslmode=require&channel_binding=require`):

| Check | Result |
|---|---|
| `scripts/init_db.py` | succeeded; detected the database as reachable and skipped creation |
| `alembic upgrade head` | applied revision `253b523fd670` |
| tables | **18** (17 + `alembic_version`) |
| check constraints | **40** |
| unique constraints | **13** |
| foreign keys | **26** |
| column types | native `uuid`, `timestamp with time zone` |
| migration vs. models | **0 pending differences** |
| rows | **0** — the schema shipped empty |
| repository layer against PostgreSQL | **12/12** — inserts across all seven entity types, enum stored as its *value* (`search_grounded`), a fabricated citation accepted with a NULL source, PostgreSQL **rejecting** a fabricated citation that names a source and a `supported` verdict with no evidence, and a full traversal of the evidence chain |
| transaction rollback | the whole exercise ran in a transaction that was rolled back; the database was left empty |
| `GET /api/health` | 200 |
| `GET /api/health/ready` | **200**, `healthy: true`, latency reported, password redacted from `target` |
| `GET /api/health/database` | 200, agrees with readiness |

One operational note: readiness measured **1.6 s** on the first request. Neon suspends
idle compute, so the first connection after a pause pays a cold start. The latency is in
the response, so a probe timeout can be set above it — otherwise an instance looks
unready while it is merely waking up.

### Not implemented

**AI functionality is NOT implemented yet.** No OpenAI calls, no prompts, no planning,
no retrieval, no embeddings, no vector store, no claim extraction, no verification
logic, no citation validation, no conflict detection, no metric computation, no
benchmark execution.

Also absent: **no research API endpoints** — `POST /api/v1/research` does not exist, so
the frontend's submit button stays disabled; no background job execution; no caching.
And **no rows**: a test asserts the migration creates none.

### Next stage

Nothing from this stage is left outstanding — the live PostgreSQL run that was pending
at first commit is done.

**Stage 6 — the research API and the model-only pipeline.** `POST /api/v1/research`,
`GET /api/v1/research/{id}` and `GET /api/v1/research` over the service layer built
here; then the OpenAI client wrapper with retry, timeout and `llm_call_log` writing,
versioned prompts, and `pipelines/model_only.py` end to end. **Done when** a topic
produces a stored report using no external sources — the baseline the other two modes
must beat — and the New Research form's submit button is enabled.

---

## Stage 4 — Core data models — 2026-10-06

**Goal:** the complete data model — SQLAlchemy tables and Pydantic schemas — with
stable IDs, logical relationships, the three required traceability chains, and
validation tests. No external APIs.

**[DATA_MODEL.md](DATA_MODEL.md) is the deliverable document**; it explains every
entity and the reasoning behind each separation. This entry records the work.

### What was implemented

**17 tables, 40 check constraints, 44 exported schemas.**

| Requested entity | Table | Schemas |
|---|---|---|
| Research | `research_run` | `ResearchRunRead`, `ResearchRunSummary` |
| ResearchRequest | — (API DTO) | `ResearchRequest` |
| ResearchResponse | — (API DTO) | `ResearchResponse` |
| Source | `source` + `research_source` | `SourceCreate`, `SourceRead`, `ResearchSourceRead` |
| Document | `document` | `DocumentCreate`, `DocumentRead` |
| DocumentChunk | `document_chunk` | `DocumentChunkCreate`, `DocumentChunkRead` |
| Claim | `claim` | `ClaimCreate`, `ClaimRead` |
| Evidence | `evidence` | `EvidenceCreate`, `EvidenceRead` |
| Citation | `citation` | `CitationCreate`, `CitationRead` |
| VerificationResult | `verification_result` | `VerificationResultCreate/Read` |
| Conflict | `conflict` | `ConflictCreate`, `ConflictRead` |
| EvaluationResult | `evaluation_result` | `EvaluationResultCreate/Read` |

**Five tables beyond the requested list**, each with a specific reason:

| Added | Why |
|---|---|
| `report` + `report_section` | **required by the specified chain** Research → Report → Claim |
| `run_configuration` | **required by the specified chain** Research run → Configuration → Metrics; content-addressed so "same configuration" is joinable |
| `research_source` | `source` is global and deduplicated across runs, but the query, rank and evidence depth are per-run |
| `document` | a source is a thing in the world; a document is one extraction of its text at one moment. Without it, a re-fetched page that changed cannot be represented |
| `subquestion`, `llm_call_log` | both already promised in ARCHITECTURE.md §5; coverage needs the plan, cost needs the call log |

### Files added

- `app/db/base.py` — declarative base, constraint naming convention,
  `UUIDPrimaryKey`, `Timestamped`, `enum_column()`
- `app/db/session.py` — lazy engine, session factory, `get_db` dependency,
  `session_scope()`, SQLite foreign-key pragma
- `app/models/` — `research.py`, `source.py`, `report.py`, `evidence.py`,
  `evaluation.py`, `observability.py`, and an `__init__.py` that registers all of it
- `app/schemas/` — `base.py`, `research.py`, `source.py`, `evidence.py`,
  `evaluation.py`, `trace.py`, and an `__init__.py` exporting 44 names
- `tests/unit/test_schema_validation.py` (79), `tests/unit/test_trace_schemas.py`
  (21), `tests/unit/test_db_session.py` (7)
- `tests/integration/test_traceability.py` (13),
  `tests/integration/test_constraints.py` (37),
  `tests/integration/test_schema_portability.py` (45),
  `tests/integration/builders.py`
- `DATA_MODEL.md`

### Files changed

- `app/core/constants.py` — added `EvidenceRelation`, `EvidenceDepth`,
  `PipelineStage`, `MetricKey` (with `is_ratio`)
- `tests/conftest.py` — `db_engine` and `db` fixtures over in-memory SQLite
- `requirements-base.txt` / `requirements.txt` — SQLAlchemy moved into the
  foundation layer; the PostgreSQL *driver* stays in the layer above
- `ARCHITECTURE.md` §5 — rewritten to match what was actually built, noting the four
  departures from the original sketch; header no longer claims to be a stage-1 doc
- `app/models/README.md`, `app/db/README.md`, `app/schemas/README.md`

### Commands used

```bash
cd backend
./.venv/Scripts/python.exe -m pip install "sqlalchemy>=2.0.35" "psycopg[binary]>=3.2"
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/python.exe -m pytest --cov=app --cov-report=term
./.venv/Scripts/python.exe -m ruff check . --fix
./.venv/Scripts/python.exe -m mypy app
```

### Tests performed

**282 backend tests, all passing. 97% statement coverage of `app/`.**

| File | Tests | Covers |
|---|---|---|
| `test_schema_validation.py` | 79 | request validation, configuration/mode agreement, fingerprints, source identity, spans, the four citation outcomes, the verdict downgrade rule, metric arithmetic |
| `test_schema_portability.py` | 45 | PostgreSQL DDL compiles for all 17 tables; native UUID; timezone-aware timestamps; VARCHAR enums; every constraint named; every FK has an `ON DELETE`; no seed data |
| `test_constraints.py` | 37 | ids stable across persist, uniqueness, spans, citation/verdict integrity, cascades, RESTRICT on configuration, enum values stored not names |
| `test_trace_schemas.py` | 21 | the three chains as types, including chains that must be refused |
| `test_traceability.py` | 13 | all three chains **traversed** end to end against a real schema |
| `test_db_session.py` | 7 | credential redaction, lazy engine, boot without a database |
| earlier suites | 80 | endpoints, errors, config, middleware — unchanged |

Traceability tests *walk* the graph by following relationships rather than asserting
on the ids they just set. Traversal is the only thing that proves a chain is
navigable in the direction a user needs it.

### Result

| Check | Result |
|---|---|
| `pytest` | **282 passed** in 2.0s |
| coverage of `app/` | **97%** (1352 statements, 43 missed) |
| `ruff check .` | All checks passed |
| `mypy app` | Success: no issues found in 43 source files |
| PostgreSQL DDL compilation | all 17 tables, all indexes |
| SQLite schema creation | 17 tables, foreign keys enforced |
| frontend | unchanged — 67 tests still passing |

### Decisions made

1. **UUIDv4 assigned at construction, not at INSERT.** So a verification pass can
   build a whole claim/evidence/citation graph in memory with cross-references and
   persist it in one transaction. See *Issues found* — this did not work on the first
   attempt.

2. **Identity (UUID) is separate from equality (fingerprint).** The UUID says which
   row; the SHA-256 fingerprint says whether two things are the same. Configurations,
   sources and documents all carry one.

3. **Prompt versions are inside the configuration fingerprint.** Changing a prompt
   changes the output, so it must make two runs non-comparable. A parametrised test
   asserts that changing any single field — including a prompt version — changes the
   hash.

4. **`citation.source_id` is nullable, and a constraint enforces why.** A fabricated
   citation points at a source that does not exist, so there is no row to reference.
   `(status = 'fabricated') = (source_id IS NULL)` makes "fabricated" a structural
   fact rather than a label someone remembered to set.

5. **Citation and evidence are separate tables.** Evidence is what we found; a
   citation is what the model said. The gap between them is the project's main
   result, and one table could not represent a citation with no evidence behind it.

6. **The verifier mitigation is in the schema.** ARCHITECTURE.md §7 promised "a
   verdict without a quote is `not_enough_evidence`". That is now a check constraint
   *and* a Pydantic validator — implemented as a **downgrade, not a rejection**,
   because the verifier's overclaim is itself data. `downgraded=True` is recorded, so
   "how often did the verifier overclaim" is measurable instead of lost.

7. **Metrics are stored long, with their numerator and denominator.** "Claim support
   rate: 0.80" is not reportable alone — over five claims it is weak, over five
   hundred it is strong. A validator rejects a value that does not equal
   `numerator / denominator`, and a zero denominator is refused: a run that produced
   no claims has an *undefined* support rate, and storing `0.0` would make it look
   maximally unreliable.

8. **`metrics_version` is part of the metric's uniqueness key.** If a definition
   changes the old numbers are not wrong, they measured something else. Both versions
   coexist so the change is visible.

9. **Conflicts link two `evidence` rows, not two sources.** "These two papers
   disagree" is not actionable; "these two passages disagree about this claim" names
   the claim, both passages and both sources, so the UI can show the disagreement.

10. **`evidence.relation` is per passage, not per claim.** A claim can have
    supporting *and* contradicting evidence at once — exactly the input conflict
    detection needs. A boolean on the claim would erase it.

11. **The three chains are explicit response models**, with validators that make a
    broken chain impossible to serialise: a fabricated citation that resolves to a
    source, a decisive passage absent from the evidence list, or a trace mixing two
    metric versions are all rejected.

12. **Invariants are enforced twice, deliberately.** Pydantic covers HTTP; database
    constraints cover scripts, migrations and `psql`. A rule living only in the API
    layer is a rule the next data-loading script will break.

### Issues found

1. **`default=uuid.uuid4` does not assign the id at construction.** It is an
   *insert-time* default, so `obj.id` was `None` until flush — defeating the reason I
   had documented for choosing UUIDs at all. Two of my own tests caught it. Fixed
   with an `init` event listener on the declarative base, keeping the column default
   as a safety net for bulk inserts and migrations.

2. **SQLAlchemy stores an enum's member *name*, not its value, by default.** The
   database would have held `MODEL_ONLY` while the API, frontend and benchmark tables
   all say `model_only`, so every raw SQL query and export would have disagreed with
   the API. Fixed with `values_callable` in `enum_column()`; a test asserts the stored
   string directly.

3. **PostgreSQL is running on this machine but its password is unknown to me.** I did
   not guess at the credentials. Instead the schema is verified by compiling the DDL
   for the PostgreSQL dialect — which proves native `UUID`, `TIMESTAMP WITH TIME
   ZONE` and named constraints all render correctly — and by running against SQLite
   **with `PRAGMA foreign_keys=ON`**, since SQLite otherwise ignores foreign keys and
   the tests would pass against constraints PostgreSQL would reject.
   **A live round-trip is still unverified.**

4. **Circular imports between model modules.** `research.py` importing `source.py`
   while `source.py` imported `research.py` broke at import time. Fixed by moving
   `enum_column()` into `app/db/base.py` and letting `app/models/__init__.py` register
   every module once — SQLAlchemy resolves string relationship targets from its own
   registry, so trailing imports were never needed.

5. **Two of my own error messages were ordered unhelpfully.** A web source with no
   URL reported the generic "requires at least one of url, doi or external_id" when
   the specific "web sources require a url" was available. Reordered.

6. **Six lint findings and three type errors** on first run — unsorted imports, a
   deprecated `timezone.utc` alias, a blind `pytest.raises(Exception)`, and optional
   offsets mypy could not narrow. All fixed rather than suppressed.

### Not implemented

**AI functionality is NOT implemented yet.** No OpenAI calls, no prompts, no
planning, no retrieval, no embeddings, no vector store, no claim extraction, no
verification, no citation validation, no conflict detection, no metric computation,
no benchmark execution.

Also absent at this stage, deliberately: **no Alembic migrations** (the schema is
created from metadata in tests), **no repositories or services**, **no API endpoints
exposing any of this**, and **no rows of any kind**. A test asserts that creating the
schema leaves all 17 tables empty.

### No fake records

Nothing in the project inserts data. Tests build throwaway rows in an in-memory
database and discard them; their values are deliberately non-plausible — topics read
`<topic under test>`, claims read `<claim 0 under test>`, and URLs use
`example.invalid`, a TLD reserved by RFC 2606 that can never resolve. If any of it
escaped into a screenshot it would be unmistakable.

### Next stage

**Stage 5 — run lifecycle endpoints and the first migration.** Alembic initialised
against the real PostgreSQL (needs the database password); `POST /api/v1/research`
and `GET /api/v1/research/{id}` persisting `RunStatus` transitions; a readiness
endpoint that verifies the database alongside the existing liveness check. The
acceptance test is still the same concrete one: **the New Research form's submit
button gets enabled.**

---

## Stage 3 — Frontend foundation — 2026-10-06

**Goal:** a clean, navigable React interface with all six pages routed, a proper
component architecture, and the three async states implemented for real. Still no
research functionality.

> **Roadmap renumbered.** Stage 2 said the data model was next. The frontend was
> built instead, so it takes stage 3 and everything after it shifts by one. The
> roadmap below reflects the new order; nothing was dropped.

### What was implemented

| Requirement | How |
|---|---|
| React + Vite | React 18, Vite 5 |
| Language consistency | **Plain JavaScript.** The project started in JS, and a half-migrated codebase would be worse than either choice. API shapes are JSDoc typedefs in `src/types/api.js` — editor completion without a build step. |
| lucide-react for icons | sidebar navigation, state icons, integration status |
| Clean component architecture | `api/` → `hooks/` → `components/ui/` → `components/layout/` → `pages/`; dependencies point one way |
| React routing | react-router-dom 6; `routes.js` is one table driving both the router and the sidebar |
| API client abstraction | `api/client.js` (transport) + `api/endpoints.js` (named operations). Components never call `fetch` |
| Environment-based backend URL | `VITE_API_BASE`; empty in dev so Vite proxies `/api` and CORS never applies |
| Loading state | `LoadingState` with `role="status"` and `aria-busy` |
| Error state | `ErrorState` showing the backend's stable `code`, the correlation id, and a retry |
| Empty state | `EmptyState`, kept distinct from the not-implemented placeholder |

### The six pages

| Page | Route | What it does |
|---|---|---|
| Dashboard | `/` | **Real data.** Reads `GET /api/v1/system/capabilities` and reports integration status, missing credentials and declared modes |
| New Research | `/research/new` | **Real, interactive form** — topic input, validation, mode selection, live request preview. Submit permanently disabled |
| Research Results | `/research/results` | Placeholder + the structure a report will have |
| Sources | `/sources` | Placeholder + the real table columns, including evidence depth |
| Evidence Audit | `/evidence` | Placeholder + the real verdict and citation-status vocabularies from the backend enums |
| Evaluation | `/evaluation` | Placeholder + metric definitions; every cell reads *not measured* |

### Files added

- `src/routes.js` — the single route table
- `src/App.jsx` — rewritten: routes built from that table
- `src/main.jsx` — mounts `BrowserRouter`
- `src/api/endpoints.js` — named operations, grouped by domain
- `src/hooks/useApi.js` — request state modelled once
- `src/hooks/useBackendStatus.js` — `useHealth`, `useCapabilities`, `useServiceInfo`
- `src/components/layout/AppLayout.jsx` — shell, sidebar, backend status badge
- `src/components/ui/states.jsx` — `LoadingState`, `ErrorState`, `EmptyState`,
  `NotImplemented`, `AsyncBoundary`, `Spinner`
- `src/components/ui/primitives.jsx` — `Card`, `PageHeader`, `Badge`, `Button`,
  `StatTile`, `PlannedContents`
- `src/pages/` — the six pages plus `NotFoundPage.jsx`
- `src/lib/constants.js` — research modes, verdicts, citation statuses, stages
- `src/types/api.js` — JSDoc typedefs mirroring `backend/app/schemas/common.py`
- `src/test/` — `setup.js`, `utils.jsx`, and four test files
- `eslint.config.js` — the `lint` script previously had no config and could not run

### Files changed

- `src/api/client.js` — reworked into a transport module: `http` verbs, `ApiError`
  with `isUnreachable` / `isNotImplemented`, 204 handling, abort passthrough
- `src/styles/index.css` — rewritten: design tokens, light **and** dark via
  `prefers-color-scheme`, full component styles, narrow-screen layout
- `vite.config.js` — Vitest config; dropped the redundant `/health` proxy entry
- `package.json` — added lucide-react, Vitest, Testing Library, ESLint plugins;
  `test` and `test:watch` scripts
- `frontend/README.md`, root `README.md`, `.github/workflows/ci.yml`

### Commands used

```bash
cd frontend
npm install lucide-react
npm install -D vitest jsdom @testing-library/react @testing-library/jest-dom \
               @testing-library/user-event globals eslint-plugin-react eslint-plugin-react-hooks
npm test
npm run lint
npm run build
npm run dev
```

### Tests performed

**67 automated tests, all passing**, across four files:

| File | Tests | Covers |
|---|---|---|
| `pages.test.jsx` | 22 | shell renders, all six nav links, each route renders its heading, every placeholder names its stage, unknown routes, navigation without reload |
| `states.test.jsx` | 21 | loading (in flight, announced, replaced), error (offline, codes, request id, retry re-issues the request), empty on all five pages, dashboard reports only what the backend says |
| `client.test.js` | 13 | base URL, endpoint paths, error-envelope parsing, missing envelope fallback, network failure, abort passthrough, request bodies, 204 |
| `newResearch.test.jsx` | 11 | form interaction, validation, request preview, and that submit is disabled and **never** POSTs |

Tests stub `fetch`, not the api client, so the client's own envelope parsing is
exercised rather than mocked away.

Plus **23 live checks against the real dev stack** — a Python script starts
uvicorn and `npm run dev` as actual processes, then verifies over HTTP that
`index.html` is served, each source module compiles, the proxy forwards `/api` to
the backend with the correlation header intact, all five client routes fall back to
the app shell, and an absent backend route still returns the error envelope.

### Result

| Check | Result |
|---|---|
| `npm test` | **67 passed** (4 files) |
| `npm run lint` | clean |
| `npm run build` | built in 9.0s — 202 kB JS (65 kB gzip), 9.8 kB CSS |
| `npm run dev` + backend | **23/23 live checks passed** |
| backend `pytest` | still 80 passed (unchanged) |

### Decisions made

1. **No mock research output anywhere.** Not a single sample report, claim, source
   or metric value. This project exists to measure how often generated text is
   unsupported; a fabricated report in a screenshot — even labelled — would
   undermine the thing being measured. Pages describe *structure* instead: the
   columns a table will have, never the rows.

2. **Placeholders name a stage.** "Not implemented" is useless on its own, so
   `NotImplemented` requires a `stage` prop and every page supplies one.

3. **The not-implemented notice is distinct from the empty state.** Empty means the
   feature works and has no content; the placeholder means the feature does not
   exist. Collapsing them would hide which is which.

4. **The New Research form is real but cannot submit.** Building it now settles the
   request shape before the endpoint is written, and the live JSON preview makes
   that shape reviewable. Submit stays disabled rather than calling a route that
   does not exist — a fake success and a confusing 404 are both worse than saying
   so plainly.

5. **One route table.** `routes.js` drives the router *and* the sidebar, so a page
   cannot be routable but unreachable, or listed but broken.

6. **Plain JavaScript, deliberately.** Consistency was the stated requirement and
   the project began in JS. `src/types/api.js` carries the API shapes as JSDoc.

7. **`loading` is derived, not stored.** `useApi` tags each settled result with the
   key of the request that produced it; loading is "the stored key is not the key I
   want". This makes showing a stale result structurally impossible and removes
   `setState` from the effect body.

8. **The dashboard cross-checks the backend's mode list** against the copy in
   `lib/constants.js` and warns on screen if they disagree, so a drift between
   frontend and backend surfaces instead of silently mislabelling a benchmark
   configuration.

### Issues found

1. **`npm run lint` had no ESLint config** — the script shipped in stage 1 could
   never have run. Added `eslint.config.js` (flat config, React + hooks plugins),
   and it immediately found four real errors, below.

2. **`useApi` updated a ref during render** and **called `setState` synchronously
   inside an effect** — both flagged by `react-hooks`, both real. Fixed by
   deriving `loading` from a request key and syncing the ref in its own effect.
   Not a rule I disabled.

3. **Two unescaped quote characters** in JSX text. Fixed with typographic quotes.

4. **The dashboard rendered the same failed request as two error blocks.** Two
   cards shared one `useCapabilities()` call, so an outage produced duplicate
   error panels. The static notice was moved out of the boundary.

5. **Vite binds to `localhost`, which resolves to `::1` here.** The first live
   verification reported the dev server as not started while it was running fine —
   `127.0.0.1:5173` never connects. Worth knowing before debugging a phantom.

6. **A stale uvicorn process held port 8000** from an earlier run and served old
   code, which made several checks behave inexplicably until it was killed.

### Not implemented

**AI functionality is NOT implemented yet.** No OpenAI calls, no prompts, no
planning, no retrieval, no embeddings, no vector store, no claim extraction, no
verification, no citation validation, no conflict detection, no metrics, no
benchmark. No database tables, no migrations, no research endpoints. The frontend
displays no research output of any kind, real or sample.

### Next stage

**Stage 4 — data model and run lifecycle.** SQLAlchemy models for the tables in
ARCHITECTURE.md §5; Alembic initialised with a first migration;
`POST /api/v1/research` and `GET /api/v1/research/{id}` persisting `RunStatus`
transitions; a readiness endpoint that verifies the database. The New Research
form's submit button gets enabled at the end of that stage — it is the acceptance
test for it.

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

Stages 1–5 are done; see the entries above. Two reorderings so far: the frontend
arrived before the data model, and the data model's endpoints moved out of stage 4 —
stage 5 became persistence (migrations, repositories, services, health), and the API
moved to stage 6.

### Stage 6 — Research API and the model-only pipeline (the baseline)
- `POST /api/v1/research`, `GET /api/v1/research/{id}`, `GET /api/v1/research`
  over the stage-5 service layer
- OpenAI client wrapper with retry, timeout, and `llm_call_log` writing
- `prompts/planner.md`, `prompts/synthesizer.md` (versioned)
- `pipelines/model_only.py` end to end
- **Done when:** a topic produces a stored report using no external sources — the
  baseline the other two modes must beat — and the New Research form's submit button
  is enabled.

### Stage 7 — Retrieval
- Tavily web search; Semantic Scholar academic search
- Fetcher (clean text extraction), chunker with character offsets
- Sentence Transformers embedder, ChromaDB store, top-k RAG retrieval
- `pipelines/hybrid.py`, `pipelines/search_grounded.py`
- **First stage that needs `requirements-ml.txt`.** Install it on the Python 3.11
  venv; whether torch and chromadb resolve there is still unverified.
- **Done when:** reports cite real, retrievable sources and every chunk can be
  traced to a character span in its source.

### Stage 8 — Evidence layer
- Claim extraction, evidence linking, per-claim verification
- Citation validation (valid / broken / misattributed / fabricated)
- Conflict detection, traceability serialization
- **Done when:** a report comes back annotated — every claim carries a verdict
  and a quoted supporting span, or an explicit "no evidence".

### Stage 9 — Evaluation and benchmark
- Metrics module; benchmark harness over a committed topic set
- Comparison tables across the three modes
- **Done when:** running the benchmark regenerates the comparison table from
  scratch, and the numbers support (or refute) the project's hypothesis.

### Stage 10 — Wire the UI to real data, harden, write up
- Replace each placeholder with the real view: run timeline, report with inline
  citations, claim inspector with evidence drill-down, benchmark dashboard
- Background job execution, caching, retry on partial failure
- Final report, figures from `notebooks/`, reproducibility instructions
- **Done when:** the whole system is usable and auditable without a terminal.

---

## Log conventions

Each stage entry records: **what was implemented**, **files changed**, **commands
used**, **tests performed**, **result**, **decisions made** (and why — the
reasoning is the part worth keeping), **issues found**, and **what is not
implemented**. Reversed decisions get an ADR in `docs/adr/` rather than a quiet
edit to an earlier entry.
