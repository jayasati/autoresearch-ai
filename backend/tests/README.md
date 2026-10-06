# tests/

- `unit/` — pure functions: chunking, metrics math, claim parsing
- `integration/` — DB, vector store, API routes
- `fixtures/` — recorded API payloads so tests never hit paid services

`test_health.py` at this level is the stage-1 smoke test.
