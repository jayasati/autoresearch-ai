"""
What the LLM service takes in and gives back.

The response models are this package's public surface: callers receive these and
never an OpenAI SDK object, so the rest of the system depends on this module's five
field names rather than on a vendor's. If the provider is ever swapped, these shapes
are the contract the replacement has to honour.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.schemas.base import ReadModel

T = TypeVar("T", bound=BaseModel)


class TokenUsage(ReadModel):
    """Tokens one call consumed, as the provider reported them.

    Kept verbatim rather than estimated: these numbers are what the provider bills
    and what MAX_LLM_CALLS_PER_RUN-style guardrails will be audited against. When the
    provider sends no usage block, the response carries `usage=None` — absence of
    information, never zeros pretending to be information.
    """

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    @classmethod
    def zero(cls) -> "TokenUsage":
        return cls(prompt_tokens=0, completion_tokens=0, total_tokens=0)


class LLMResponse(ReadModel):
    """One text generation: what came back, from which model, at what cost."""

    text: str
    model: str = Field(
        description=(
            "The model that actually answered, as the provider named it in the "
            "response — not the alias we asked for. 'gpt-4o-mini' may resolve to a "
            "dated snapshot, and a benchmark must record which."
        )
    )
    finish_reason: str | None = Field(
        default=None,
        description=(
            "Why generation stopped: 'stop' is a completed answer; 'length' means the "
            "text was TRUNCATED by the token limit and must not be treated as complete."
        ),
    )
    usage: TokenUsage | None = None
    latency_seconds: float = Field(ge=0)


class StructuredLLMResponse(LLMResponse, Generic[T]):
    """One structured generation: the validated object, plus the same provenance.

    `data` has passed the caller's Pydantic schema — that validation is the service's
    job and has already happened; a `StructuredLLMResponse` whose data did not
    validate does not exist. `text` keeps the raw JSON the model actually produced,
    because when something looks odd downstream, the evidence is the original bytes,
    not our parse of them.
    """

    data: T
