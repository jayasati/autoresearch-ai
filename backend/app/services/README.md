# services/ — Application services

Use-case orchestration between the API layer and the domain layer. Owns
transactions; agents and retrieval stay persistence-ignorant.

Planned modules:
- `research_service.py` — create/read research runs, persist artifacts
- `source_service.py` — deduplicate and store sources
- `metrics_service.py` — compute and cache run metrics
- `job_queue.py` — background execution of long runs (status polling / SSE)
