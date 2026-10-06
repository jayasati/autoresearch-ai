"""
Response schemas shared across the API.

These are the contract the frontend codes against. They live apart from the ORM
models so the database can change without breaking clients.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """The body of every error response, whatever its cause."""

    code: str = Field(
        description="Stable machine-readable identifier. Switch on this, not on `message`.",
        examples=["not_found"],
    )
    message: str = Field(
        description="Human-readable explanation. Wording may change; do not parse it.",
        examples=["The requested resource does not exist."],
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured context: offending fields, upstream service name, limits.",
    )
    request_id: str = Field(
        default="-",
        description="Correlation id, also returned in the X-Request-ID header.",
    )


class ErrorResponse(BaseModel):
    """Errors are always wrapped, so a client can tell an error from a payload."""

    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = Field(examples=["0.1.0"])
    environment: str = Field(examples=["development"])


class ServiceInfoResponse(BaseModel):
    """The ``GET /`` payload: what this service is and where to go next."""

    name: str
    version: str
    environment: str
    description: str
    stage: str = Field(description="Which build stage of the project is live.")
    docs_url: str
    health_url: str
    api_version_prefix: str
