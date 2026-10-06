"""
Schema base classes.

Two deliberately different defaults:

- **Read** models are permissive about extra input (they are built from ORM rows)
  and serialise cleanly.
- **Write** models `forbid` extra fields. A request with `topik` instead of `topic`
  must be rejected, not silently accepted with a default -- otherwise a typo in a
  benchmark script would run the wrong configuration and nobody would notice.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReadModel(BaseModel):
    """Base for responses built from ORM objects."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class WriteModel(BaseModel):
    """Base for request bodies. Unknown fields are an error."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IdentifiedModel(ReadModel):
    """Anything with a stable identifier.

    Every entity carries a UUID that exists from construction, so a response can
    always be joined back to a database row -- and an id quoted in a report,
    a log line or a benchmark table means the same thing forever.
    """

    id: uuid.UUID = Field(description="Stable UUIDv4 identifier, assigned at creation.")


class TimestampedModel(IdentifiedModel):
    created_at: datetime
    updated_at: datetime
