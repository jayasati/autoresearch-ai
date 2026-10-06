# db/ — Database plumbing

**Implemented.**

- `base.py` — the declarative `Base`, the constraint naming convention,
  `UUIDPrimaryKey`, `Timestamped`, and `enum_column()`. Three decisions live here
  and nowhere else: how identifiers are generated, how constraints are named, and
  how enums are stored.
- `session.py` — lazily created engine, session factory, the `get_db` FastAPI
  dependency, a `session_scope()` context manager for scripts, and
  `enable_sqlite_foreign_keys()` for tests.

The engine is created **on first use, not at import**, so the application can boot
and answer `/api/health` with no database reachable. That is what makes the liveness
check meaningful rather than a second database check.

Migrations are not set up yet — see `backend/migrations/`.
