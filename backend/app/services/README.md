# services/ — Use cases, and the transaction boundary

**`research_service.py` and `retrieval/` are implemented.** The rest arrive with
their stages.

This layer answers "what happened, and did *all* of it happen". Each public method is
one atomic operation: it either completes and commits, or raises and leaves the
database exactly as it was.

`unit_of_work()` is the boundary. Passing it an existing session **joins** that
transaction rather than opening a second one, so a service method is safe to call
standalone *or* as one step of a larger operation — and in the second case it does
not commit work the caller may still abandon.

## What `ResearchService` owns

- `create_run` — configuration and run in **one** transaction, so a rejected run
  cannot strand an orphan settings row
- `get_run`, `list_runs`, `run_summary` — reads, with relations eager-loaded
- `advance_status` — enforces the legal status transitions. A completed run cannot go
  back to `retrieving`, which would produce a second report for one run and break the
  one-report-per-run invariant from the far side
- `fail_run` — requires an error code, because a silently failed run is
  indistinguishable from one that produced nothing
- `record_plan` — all sub-questions or none; a partial plan would make coverage's
  denominator too small, biasing the metric in the project's favour
- `attach_source` — deduplicate and link together, since a source row with no link is
  invisible to the run that found it

No LLM calls and no retrieval here: this service persists what those stages will
eventually produce.

Planned: `source_service.py`, `metrics_service.py`, `job_queue.py`.

## `retrieval/` — getting candidate sources from outside

Two providers are implemented, both over `httpx`, both with timeout, bounded retry,
429 handling that obeys `Retry-After`, 5xx handling and deduplication. The retry loop,
status mapping and backoff live once in `http_backend.py`.

| Provider | Returns | Deduplicated by |
|---|---|---|
| `tavily_service.py` | web pages, with snippets | canonical URL |
| `semantic_scholar_service.py` | papers, with abstracts when available | DOI, else URL, else paper id |

Semantic Scholar allows **one request per second, cumulative across all endpoints**, so
`rate_limit.py` throttles before sending with a process-wide limiter shared across
instances — the limit belongs to the account, not the object. A 429 penalises that
shared limiter, so the whole process waits once rather than each caller finding the wall
separately.

**Both return candidate sources, not evidence**, and that is enforced rather than
documented: neither candidate type has a `relation`, a verdict or a "supports" field,
and `evidence_depth` records honestly what text we actually hold — `snippet` for a web
result, `abstract` for a paper that came with one, `metadata` for a paper that did not.
A paper with no abstract is kept rather than dropped, because discarding every paper
whose abstract is not indexed would bias retrieval toward whatever is well indexed.

The only bridge to the database is `to_source_create()`, which produces a `source` row
and nothing else — evidence needs a fetched chunk, and a citation needs a claim.
**Nothing is downloaded**: where a paper has an open-access PDF the address is recorded,
which is a different thing from fetching it.

It does not sit in the transaction-owning part of this package. It makes no database
calls at all; the orchestrator will persist what it returns.

Planned alongside it: Semantic Scholar search, the fetcher, the chunker, the embedder
and the vector store.
