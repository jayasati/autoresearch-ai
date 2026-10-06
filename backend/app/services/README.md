# services/ — Use cases, and the transaction boundary

**`research_service.py` is implemented.** The rest arrive with their stages.

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
