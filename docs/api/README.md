# API notes

The authoritative, always-current spec is the running backend's OpenAPI page:
http://localhost:8000/docs

This directory holds what OpenAPI cannot express: conventions, the error
contract, and streaming/polling behaviour for long runs.

## Versioning

| Path | Versioned? | Why |
|---|---|---|
| `/` | no | service information |
| `/api/health` | **no** | health is a property of the process, not of the API contract. Probes and monitoring must not need updating when the API goes v1 → v2. |
| `/api/v1/...` | yes | the application API |

A future breaking revision mounts at `/api/v2` as a *sibling* router, so v1 keeps
working untouched. Only `API_PREFIX` is configurable; version segments are
derived from it, so a prefix can never be half-renamed.

## Current endpoints (stage 2)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | name, version, environment, stage, and where to find docs/health |
| GET | `/api/health` | **liveness** — no database, no network, no credentials needed |
| GET | `/api/health/ready` | **readiness** — verifies the database; 503 when it is down |
| GET | `/api/health/database` | database connectivity alone, for a targeted alert |
| GET | `/api/v1/system/ping` | trivial reachability check |
| GET | `/api/v1/system/capabilities` | which integrations are configured, which credentials are still missing by name, and which stages have landed |

### Liveness and readiness are different checks

Conflating them is a real operational mistake.

**Liveness** (`/api/health`) answers "is this process working?" It does no I/O. A
liveness probe that fails because the database is slow makes the orchestrator restart
a perfectly healthy process — which fixes nothing and removes capacity exactly when
the system is already struggling.

**Readiness** (`/api/health/ready`) answers "can this process serve traffic?" It runs
`SELECT 1`. A failing readiness probe takes the instance out of the load balancer
without killing it, so it recovers on its own when the dependency does.

A 503 from readiness still returns the **full body**, because a probe that says only
"not ready" forces whoever is paged to go and find out why:

```json
{
  "status": "not_ready",
  "version": "0.1.0",
  "environment": "development",
  "dependencies": [
    {
      "name": "postgresql",
      "healthy": false,
      "latency_ms": 213.2,
      "detail": "connection failed: ... password authentication failed for user \"postgres\"",
      "target": "postgresql+psycopg://postgres:***@localhost:5432/autoresearch"
    }
  ]
}
```

`target` shows **which** database the instance is pointed at, with the password
removed — that is most of the diagnosis when a deployment is misconfigured. `name`
reflects the URL actually configured, so a SQLite connection is not labelled
`postgresql`.

Note that `capabilities` reporting `postgres: true` means *configured*, not
*reachable*. Readiness is the endpoint that answers reachability.

### A placeholder is not a credential

`capabilities` reports `openai: false` when `OPENAI_API_KEY` is still
`sk-replace-me`, and lists the variable in `missing_credentials`. `.env.example`
ships placeholders so the file documents itself, which means a non-empty value is
not evidence of a usable key. One rule (`Settings._is_real_credential`) backs both
this endpoint and the startup warning, so the two cannot disagree.

## The error contract

**Every** failure — validation, not-found, wrong method, upstream outage,
unhandled crash — returns the same envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "No research run with id 42.",
    "details": { "run_id": 42 },
    "request_id": "9f2c1ab40e77"
  }
}
```

**Switch on `code`, never on `message`.** Codes are stable; wording is not.

| `code` | Status | Meaning |
|---|---|---|
| `bad_request` | 400 | malformed request |
| `not_found` | 404 | no such resource |
| `method_not_allowed` | 405 | wrong verb for the path |
| `conflict` | 409 | valid request, conflicts with current state |
| `validation_error` | 422 | body failed validation; `details.fields[]` lists each problem |
| `rate_limited` | 429 | an upstream provider rate-limited us |
| `budget_exceeded` | 429 | a per-run cost guardrail from `core/config.py` was hit — a deliberate stop, not a bug |
| `configuration_error` | 500 | a required setting or credential is missing |
| `internal_error` | 500 | unanticipated; the traceback is in the server log, never in the response |
| `external_service_error` | 502 | OpenAI, Tavily, Semantic Scholar or a page fetch failed; `details.service` names which |

`external_service_error` is kept distinct from `internal_error` on purpose: a run
that failed because a provider was down is not the same result as a run that
failed because our logic is wrong, and the benchmark must not conflate the two.

### Validation errors

```json
{
  "error": {
    "code": "validation_error",
    "message": "The request failed validation.",
    "details": {
      "fields": [
        { "field": "topic", "problem": "Field required", "type": "missing" },
        { "field": "depth", "problem": "Input should be a valid integer", "type": "int_parsing" }
      ]
    }
  }
}
```

Flattened per field so a UI can render the message next to the offending input
rather than dumping Pydantic's raw error list.

## Correlation

| Header | Direction | Meaning |
|---|---|---|
| `X-Request-ID` | request *and* response | correlation id. Send one and it is honoured, so a trace can be followed from the frontend into the backend; omit it and one is generated. |
| `X-Response-Time-ms` | response | server-side handling time |

Both are listed in `Access-Control-Expose-Headers`, so browser JavaScript can
actually read them — without that, the frontend cannot show a user the id to
quote in a bug report. Every log line the request produces carries the same id.

## Not implemented yet

No research endpoints exist. Stage 2 (next) adds `POST /api/v1/research` and
`GET /api/v1/research/{id}` over the real data model.
