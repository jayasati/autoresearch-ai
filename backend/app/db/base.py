"""
Declarative base, naming conventions and the mixins every table uses.

Two things are decided here and nowhere else: how identifiers are generated, and
how constraints are named.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, Uuid, event, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint naming. Without this, databases invent names, and an
# Alembic migration that needs to drop a constraint has nothing stable to name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware UTC. Naive datetimes are a bug waiting to happen."""
    return datetime.now(UTC)


class UUIDPrimaryKey:
    """A stable, client-side identifier.

    UUIDv4 generated in Python rather than a database sequence, deliberately:

    - The id exists the moment the object is constructed, before any INSERT. An
      orchestrator can build a whole claim/evidence/citation graph in memory and
      reference ids across it, then persist in one transaction.
    - It does not change on persist, and it does not collide across environments,
      so an id in a log line, an exported report or a benchmark table refers to
      the same row forever. An autoincrementing integer satisfies neither.
    - It carries no information. Sequential ids would leak how many runs exist.

    `sqlalchemy.Uuid` renders as native `uuid` on PostgreSQL and `CHAR(32)`
    elsewhere, so the same model works against SQLite in tests.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


# `default=uuid.uuid4` above is an *insert-time* default: SQLAlchemy applies it
# when the INSERT is emitted, so `obj.id` is None until the session flushes. That
# defeats the main reason for choosing UUIDs over a sequence, so the id is also
# assigned at construction here. The column default stays as a safety net for rows
# created by other means (a bulk insert, a migration, raw SQL).
@event.listens_for(Base, "init", propagate=True)
def _assign_id_on_construction(target, args, kwargs) -> None:  # pragma: no cover - hook
    if isinstance(target, UUIDPrimaryKey) and "id" not in kwargs:
        kwargs["id"] = uuid.uuid4()


class Timestamped:
    """Creation and update times, set by the database where possible."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


def enum_column(enum_cls: type, **kwargs) -> SAEnum:
    """A portable column for one of the `core.constants` string enums.

    Two deliberate settings:

    - `values_callable` makes the database store the member *value*
      ("model_only"), not its name ("MODEL_ONLY"), which is SQLAlchemy's default.
      The API, the frontend and the benchmark tables all speak in values, so
      without this the database would be the one place using a different
      vocabulary -- and every raw SQL query would silently disagree.
    - `native_enum=False` renders VARCHAR plus a CHECK constraint rather than a
      PostgreSQL ENUM type. A native enum can only be altered outside a
      transaction on older servers and can never have a value removed; a CHECK can
      be dropped and recreated in one migration.
    - `create_constraint=True` is **not** the default. SQLAlchemy 1.4 changed it to
      False, which means an enum column silently becomes an unconstrained VARCHAR
      and the database will accept any string at all. The Python layer still
      validates, but a data-loading script, a migration or a psql session would
      not -- exactly the gap this project closes everywhere else by enforcing an
      invariant in both places.

    Consequence worth knowing: adding a member to an enum now requires a migration
    that drops and recreates the CHECK. Alembic's autogenerate does **not** detect
    that change, so `tests/integration/test_schema_portability.py` asserts the
    constraint covers every member.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=40,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
        create_constraint=True,
        **kwargs,
    )
