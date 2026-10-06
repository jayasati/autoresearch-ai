# migrations/ — Alembic

**Initialised.** One revision: the initial schema, creating all 17 tables.

```bash
cd backend
python ../scripts/init_db.py          # create the database + upgrade to head
alembic upgrade head                  # apply migrations
alembic current                       # which revision is applied
alembic downgrade -1                  # roll back one
alembic revision --autogenerate -m "what changed"
```

## Two things differ from a stock Alembic setup

**No connection string in `alembic.ini`.** That file is committed, so a URL written
there would be a credential in version control. `env.py` reads `DATABASE_URL`
through the application's own `Settings`, which also guarantees migrations and the
running service can never disagree about which database they mean.

**`render_as_batch=True`.** SQLite cannot `ALTER` a column, so Alembic rewrites the
table instead. Harmless on PostgreSQL, and it is what lets the test suite run the
*real* migration rather than a stand-in — `tests/integration/test_migrations.py`
applies it, reverses it, and reapplies it.

## The test that matters most

`test_the_migration_matches_the_models` applies the migration and then asks Alembic
to autogenerate against the result. An empty diff means the migration and the models
agree.

Without it the two drift silently: the test suite passes because it builds its schema
from metadata, while the deployed database is missing a column nobody noticed, and
the failure only shows up in production.

## Writing a new revision

1. Change the models in `app/models/`.
2. `alembic revision --autogenerate -m "short description"`.
3. **Read the generated file.** Autogenerate does not detect everything — a renamed
   column looks like a drop plus an add, which would destroy data.
4. Replace the generated docstring with one that says *why*.
5. Run the tests. `test_the_migration_matches_the_models` will fail if the revision
   is incomplete.

Generated files are exempt from ruff's cosmetic rules (see `pyproject.toml`):
reformatting them would make every future autogenerate diff noisy.
