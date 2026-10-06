# db/ — Database plumbing

Planned modules:
- `base.py` — declarative base + naming convention
- `session.py` — engine, session factory, FastAPI dependency
- `init_db.py` — dev-only schema creation

Migrations live in `backend/migrations/` (Alembic).
