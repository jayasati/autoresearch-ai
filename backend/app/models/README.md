# models/ — SQLAlchemy ORM models (PostgreSQL)

**Implemented.** 17 tables. See [DATA_MODEL.md](../../../DATA_MODEL.md) at the
repository root for what each entity is for and why it exists separately from its
neighbours — that document is the authoritative reference.

| Module | Tables |
|---|---|
| `research.py` | `run_configuration`, `benchmark_run`, `research_run`, `subquestion` |
| `source.py` | `source`, `research_source`, `document`, `document_chunk` |
| `report.py` | `report`, `report_section` |
| `evidence.py` | `claim`, `evidence`, `citation`, `verification_result`, `conflict` |
| `evaluation.py` | `evaluation_result` |
| `observability.py` | `llm_call_log` |

Import from `app.models`, never from a submodule directly: the package `__init__`
registers every class with the mapper registry, and string-based relationship
targets are resolved from it.

Shared machinery lives in `app/db/base.py` — the declarative base, the constraint
naming convention, the UUID primary key mixin, timestamps, and `enum_column()`.

Migrations are not set up yet; `backend/migrations/` is still a placeholder.
