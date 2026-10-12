"""
The foundation-model boundary: prompts in, validated, provider-agnostic responses out.

| Module | Contents |
|---|---|
| `service.py` | `LLMService` — `generate_text`, `generate_structured_output` |
| `models.py` | `LLMResponse`, `StructuredLLMResponse[T]`, `TokenUsage` |
| `exceptions.py` | the `LLMError` hierarchy, mirroring retrieval's |

**This project uses an external foundation model (OpenAI); it does not train or
fine-tune one.** Nothing above this package imports the OpenAI SDK: callers depend on
these models and exceptions only, so the provider is swappable in one place.

No research generation lives here — planning, verification and report prompts belong
to the orchestration stage. This package only knows how to ask and what came back.
"""

from app.services.llm.exceptions import (
    LLMAuthenticationFailed,
    LLMError,
    LLMNotConfigured,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMRefusal,
    LLMRequestInvalid,
    LLMResponseInvalid,
    LLMTimeout,
)
from app.services.llm.models import LLMResponse, StructuredLLMResponse, TokenUsage
from app.services.llm.service import LLMService

__all__ = [
    "LLMAuthenticationFailed",
    "LLMError",
    "LLMNotConfigured",
    "LLMProviderUnavailable",
    "LLMRateLimited",
    "LLMRefusal",
    "LLMRequestInvalid",
    "LLMResponse",
    "LLMResponseInvalid",
    "LLMService",
    "LLMTimeout",
    "StructuredLLMResponse",
    "TokenUsage",
]
