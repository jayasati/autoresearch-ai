"""
LLM failures.

The same shape as retrieval's hierarchy, for the same reasons: every failure is an
`ExternalServiceError`, so it maps to the API's stable error codes and stays
distinguishable from our own bugs — a run that failed because OpenAI was down is not
the same result as a run that failed because our logic is wrong.

`retryable` is a property of the class, as in retrieval. The transport-level retrying
itself is delegated to the OpenAI SDK (see service.py for why), so here the flag is
not driving a loop of ours; it is kept because callers and the orchestrator still need
the distinction — a budget should not be spent re-asking a model that just said the
request was invalid.
"""

from typing import Any

from app.core.exceptions import ExternalServiceError

SERVICE_NAME = "openai"


class LLMError(ExternalServiceError):
    """Base for every foundation-model failure."""

    #: Whether another attempt could plausibly succeed.
    retryable: bool = False

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(SERVICE_NAME, message, **kwargs)


class LLMNotConfigured(LLMError):
    """No usable API key.

    A configuration error, not an upstream one: 500, never retried, and raised at
    call time rather than at startup so the service still boots and reports the gap
    through /api/v1/system/capabilities — the same contract as SearchNotConfigured.
    """

    status_code = 500
    code = "configuration_error"
    message = "The foundation model provider is not configured. Set OPENAI_API_KEY."
    retryable = False


class LLMAuthenticationFailed(LLMError):
    """The provider rejected the key (401/403). A wrong key stays wrong; never retried."""

    status_code = 502
    code = "external_service_error"
    message = "The foundation model provider rejected the API key."
    retryable = False


class LLMRateLimited(LLMError):
    """429. Retryable; the SDK has already obeyed any Retry-After before this surfaces."""

    status_code = 429
    code = "rate_limited"
    message = "The foundation model provider rate-limited the request."
    retryable = True


class LLMTimeout(LLMError):
    """The request did not complete inside OPENAI_TIMEOUT_SECONDS."""

    status_code = 504
    code = "external_service_error"
    message = "The foundation model did not respond in time."
    retryable = True


class LLMProviderUnavailable(LLMError):
    """5xx from the provider, or an unreachable host. Their side, transient."""

    status_code = 502
    code = "external_service_error"
    message = "The foundation model provider is unavailable."
    retryable = True


class LLMRequestInvalid(LLMError):
    """4xx that is not auth or rate limiting: we sent something the provider refused.

    Usually *our* bug — a bad parameter, a model that does not support temperature, a
    context overflow. Refused identically next time, so never retried.
    """

    status_code = 502
    code = "external_service_error"
    message = "The foundation model provider rejected the request."
    retryable = False


class LLMResponseInvalid(LLMError):
    """The model answered, but not with what was asked for.

    For structured output this means content that is not valid JSON, JSON that fails
    the Pydantic schema, or output truncated by the token limit. Not retried here:
    whether re-asking is worth a run's budget is the orchestrator's decision, not a
    transport policy.
    """

    status_code = 502
    code = "external_service_error"
    message = "The foundation model returned a response that could not be used."
    retryable = False


class LLMRefusal(LLMError):
    """The model explicitly declined to answer.

    Kept distinct from an unusable response: a refusal is the model exercising its
    own policy, and re-sending the same prompt is pointless. The refusal text is in
    `details["refusal"]` so the run can record *why*.
    """

    status_code = 502
    code = "external_service_error"
    message = "The foundation model declined to produce the requested output."
    retryable = False
