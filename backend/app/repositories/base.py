"""
The repository base.

A repository translates between the domain and the database. It owns *queries*, not
transactions -- the single most important rule in this layer:

    **Repositories never commit.**

The caller that opened the unit of work commits, once, when the whole operation
succeeded. If repositories committed, a failure halfway through persisting a
report's claims would leave the earlier ones written: a run whose claim set is
silently incomplete, which every metric computed over it would then misreport. That
is the kind of corruption nobody notices until the numbers are already in a report.

Repositories do `flush()` where a database-assigned value is needed before the
transaction ends. A flush is not a commit: it sends the INSERT so constraints are
checked and relationships resolve, while the transaction remains rollback-able.
"""

import uuid
from collections.abc import Sequence
from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """Queries for one entity type."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- reads ---------------------------------------------------------------

    def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """By id, or None. For "does this exist" questions."""
        return self.session.get(self.model, entity_id)

    def get_or_raise(self, entity_id: uuid.UUID) -> ModelT:
        """By id, or raise `NotFoundError`.

        Exists so handlers do not each re-implement the same None check and
        accidentally return a 500 where a 404 belongs.
        """
        instance = self.get(entity_id)
        if instance is None:
            raise NotFoundError(
                f"No {self.model.__tablename__} with id {entity_id}.",
                details={"entity": self.model.__tablename__, "id": str(entity_id)},
            )
        return instance

    def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[ModelT]:
        """A page of rows.

        Always paginated, with a default limit. An unbounded `list()` is a query
        that works on a developer's machine and takes the service down once the
        table is large.
        """
        statement = select(self.model).limit(limit).offset(offset)
        return self.session.execute(statement).scalars().all()

    def count(self) -> int:
        return self.session.execute(
            select(func.count()).select_from(self.model)
        ).scalar_one()

    def exists(self, entity_id: uuid.UUID) -> bool:
        return self.get(entity_id) is not None

    # --- writes --------------------------------------------------------------

    def add(self, instance: ModelT) -> ModelT:
        """Stage an insert. Does not commit."""
        self.session.add(instance)
        return instance

    def add_all(self, instances: Sequence[ModelT]) -> Sequence[ModelT]:
        self.session.add_all(instances)
        return instances

    def flush(self) -> None:
        """Send pending statements without ending the transaction.

        Needed when a later insert depends on this one having been checked -- a
        foreign key, or a unique constraint we want to fail on now rather than at
        commit, where the error would be harder to attribute.
        """
        self.session.flush()

    def delete(self, instance: ModelT) -> None:
        """Stage a delete. Does not commit."""
        self.session.delete(instance)
