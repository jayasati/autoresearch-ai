"""
The foundation-model boundary: prompts in, validated responses out.

    generate_text()               prompt → LLMResponse (text + model + usage)
    generate_structured_output()  prompt + Pydantic schema → StructuredLLMResponse[T]

**This project calls an external foundation model; it does not train one.** The
research pipeline's claims about grounding and verification are claims about how a
model is *used* — what it is shown, what it must cite, how its output is checked —
not about weights. Everything above this module is written against `LLMResponse` /
`StructuredLLMResponse` and this package's exceptions; no OpenAI SDK type crosses
this boundary, so swapping the provider is an edit to this package, not a migration.

Transport policy — the one deliberate divergence from retrieval: Tavily and Semantic
Scholar own their retry loop because they speak raw httpx and nothing else would do
it. Here the OpenAI SDK already implements bounded retries with Retry-After handling
and jittered backoff over the same httpx stack, so the service *configures* that
machinery (`OPENAI_MAX_RETRIES`, `OPENAI_TIMEOUT_SECONDS`) rather than reimplementing
it — duplicated retry logic is the kind of thing where a bug gets fixed in one copy
and lives on in the other. The policy is still pinned by tests: a 429 is retried, a
401 is not, over a mocked transport counting real requests.

The API key never appears in a log line or an exception raised from here:
authentication failures carry a fixed message naming the *setting*, not the secret,
and provider error bodies are truncated, never echoed in full.
"""

import logging
import time
from typing import Any, TypeVar

import openai
import pydantic
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import Settings, get_settings
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

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=pydantic.BaseModel)


class LLMService:
    """Async access to the configured foundation model. No database, no persistence.

    Accepts an injected `http_client` so tests drive the real SDK over
    `httpx.MockTransport` — the same approach the retrieval tests use, and for the
    same reason: the request assembly and error mapping being tested are the code
    that runs in production.
    """

    def __init__(self, settings: Settings | None = None, http_client: Any = None) -> None:
        self.settings = settings or get_settings()
        self._http_client = http_client
        self._client: AsyncOpenAI | None = None
        #: Cumulative across this instance's lifetime, for budget accounting.
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()

    # --- client lifecycle ------------------------------------------------------

    def _ensure_client(self) -> AsyncOpenAI:
        """The client, created on first use — after the credential check.

        Raised at call time, not at startup, so the service boots without a key and
        /api/v1/system/capabilities reports the gap: the same contract as
        SearchNotConfigured.
        """
        if self.settings.llm_provider is None:
            raise LLMNotConfigured(details={"setting": "OPENAI_API_KEY"})
        if self._client is None:
            if self.settings.llm_provider == "azure_openai":
                # AsyncAzureOpenAI subclasses AsyncOpenAI: same request pipeline,
                # same retries, same exceptions — only addressing and auth differ.
                from openai import AsyncAzureOpenAI

                self._client = AsyncAzureOpenAI(
                    azure_endpoint=self.settings.AZURE_OPENAI_ENDPOINT or "",
                    api_key=self.settings.AZURE_OPENAI_API_KEY,
                    api_version=self.settings.AZURE_OPENAI_API_VERSION,
                    timeout=float(self.settings.OPENAI_TIMEOUT_SECONDS),
                    max_retries=self.settings.OPENAI_MAX_RETRIES,
                    http_client=self._http_client,
                )
            else:
                self._client = AsyncOpenAI(
                    api_key=self.settings.OPENAI_API_KEY,
                    timeout=float(self.settings.OPENAI_TIMEOUT_SECONDS),
                    max_retries=self.settings.OPENAI_MAX_RETRIES,
                    http_client=self._http_client,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> "LLMService":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # --- public API --------------------------------------------------------------

    async def generate_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """One text completion. `model` and `temperature` fall back to settings;
        a temperature of None is *omitted* from the request, because some models
        reject the parameter outright and the provider's default is then correct."""
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            completion = await client.chat.completions.create(
                messages=self._messages(prompt, system),
                **self._request_kwargs(model, temperature, max_tokens),
            )
        except Exception as exc:
            raise self._translate(exc) from exc
        latency = time.perf_counter() - started

        choice = self._only_choice(completion)
        text = choice.message.content
        if text is None:
            raise self._no_content_error(choice)

        usage = self._record_usage(completion)
        self._log_call("generate_text", completion.model, latency, usage)
        return LLMResponse(
            text=text,
            model=completion.model,
            finish_reason=choice.finish_reason,
            usage=usage,
            latency_seconds=latency,
        )

    async def generate_structured_output(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> StructuredLLMResponse[T]:
        """One completion constrained to `schema`, validated by Pydantic.

        The provider is asked for strict JSON matching the schema, and the content is
        then validated against the actual Pydantic model — a response that does not
        validate raises `LLMResponseInvalid` and never reaches the caller. `data` on
        the result is therefore always a real, checked instance of `schema`.
        """
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            completion = await client.chat.completions.parse(
                messages=self._messages(prompt, system),
                response_format=schema,
                **self._request_kwargs(model, temperature, max_tokens),
            )
        except pydantic.ValidationError as exc:
            # Covers both not-JSON and JSON that fails the schema: pydantic's
            # model_validate_json raises ValidationError for either.
            raise LLMResponseInvalid(
                f"The model's output did not validate against {schema.__name__}: "
                f"{exc.error_count()} error(s).",
                details={"schema": schema.__name__},
            ) from exc
        except openai.LengthFinishReasonError as exc:
            raise LLMResponseInvalid(
                "The structured output was truncated by the token limit.",
                details={"schema": schema.__name__, "finish_reason": "length"},
            ) from exc
        except Exception as exc:
            raise self._translate(exc) from exc
        latency = time.perf_counter() - started

        choice = self._only_choice(completion)
        message = choice.message
        if message.refusal:
            raise LLMRefusal(details={"refusal": message.refusal[:500]})
        if message.parsed is None or message.content is None:
            raise LLMResponseInvalid(
                "The model returned no parsable content.",
                details={"schema": schema.__name__, "finish_reason": choice.finish_reason},
            )

        usage = self._record_usage(completion)
        self._log_call(f"generate_structured_output[{schema.__name__}]",
                       completion.model, latency, usage)
        return StructuredLLMResponse[schema](  # type: ignore[valid-type]
            data=message.parsed,
            text=message.content,
            model=completion.model,
            finish_reason=choice.finish_reason,
            usage=usage,
            latency_seconds=latency,
        )

    # --- request assembly ----------------------------------------------------------

    @staticmethod
    def _messages(prompt: str, system: str | None) -> list[ChatCompletionMessageParam]:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must contain text")
        messages: list[ChatCompletionMessageParam] = []
        if system and system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _request_kwargs(
        self, model: str | None, temperature: float | None, max_tokens: int | None
    ) -> dict[str, Any]:
        # On Azure, the default "model" is the deployment name.
        kwargs: dict[str, Any] = {"model": model or self.settings.llm_default_model}
        effective_temperature = (
            temperature if temperature is not None else self.settings.OPENAI_TEMPERATURE
        )
        if effective_temperature is not None:
            kwargs["temperature"] = effective_temperature
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        return kwargs

    # --- response handling ----------------------------------------------------------

    @staticmethod
    def _only_choice(completion: Any) -> Any:
        if not completion.choices:
            raise LLMResponseInvalid("The provider returned no choices.")
        return completion.choices[0]

    @staticmethod
    def _no_content_error(choice: Any) -> LLMError:
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            return LLMRefusal(details={"refusal": refusal[:500]})
        return LLMResponseInvalid(
            "The model returned no text content.",
            details={"finish_reason": choice.finish_reason},
        )

    def _record_usage(self, completion: Any) -> TokenUsage | None:
        """Usage as reported, or None when the provider sent none — absence of
        information is not zeros."""
        self.calls_made += 1
        raw = completion.usage
        if raw is None:
            return None
        usage = TokenUsage(
            prompt_tokens=raw.prompt_tokens,
            completion_tokens=raw.completion_tokens,
            total_tokens=raw.total_tokens,
        )
        self.total_usage = self.total_usage + usage
        return usage

    @staticmethod
    def _log_call(operation: str, model: str, latency: float, usage: TokenUsage | None) -> None:
        # Model, cost and time only. Never the prompt (it may quote source text we
        # have no business logging) and never anything carrying the key.
        logger.info(
            "openai %s: model=%s latency=%.2fs tokens=%s",
            operation,
            model,
            latency,
            usage.total_tokens if usage else "unreported",
        )

    # --- error mapping ----------------------------------------------------------

    def _translate(self, exc: Exception) -> Exception:
        """SDK exception to ours; anything already ours or unexpected passes through.

        Auth failures get a FIXED message naming the setting: the provider's own 401
        body quotes (part of) the key it rejected, and echoing that into logs is
        exactly what requirement 8 forbids. Other bodies are truncated.
        """
        if isinstance(exc, LLMError | ValueError):
            return exc
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return LLMAuthenticationFailed(
                details={"status": exc.status_code, "setting": "OPENAI_API_KEY"}
            )
        if isinstance(exc, openai.RateLimitError):
            return LLMRateLimited(
                f"Still rate-limited after {self.settings.OPENAI_MAX_RETRIES} retries.",
                details={"status": exc.status_code, "retries": self.settings.OPENAI_MAX_RETRIES},
            )
        if isinstance(exc, openai.APITimeoutError):  # before APIConnectionError: subclass
            return LLMTimeout(
                f"No response after {self.settings.OPENAI_TIMEOUT_SECONDS:g}s.",
                details={"timeout_seconds": self.settings.OPENAI_TIMEOUT_SECONDS},
            )
        if isinstance(exc, openai.APIConnectionError):
            return LLMProviderUnavailable(
                f"Could not reach the provider: {type(exc.__cause__ or exc).__name__}."
            )
        if isinstance(exc, openai.APIStatusError):
            detail = self._safe_detail(exc)
            if exc.status_code >= 500:
                return LLMProviderUnavailable(
                    f"The provider returned {exc.status_code}: {detail}",
                    details={"status": exc.status_code},
                )
            return LLMRequestInvalid(
                f"The provider returned {exc.status_code}: {detail}",
                details={"status": exc.status_code},
            )
        if isinstance(exc, openai.OpenAIError):
            return LLMProviderUnavailable(f"Unexpected provider failure: {type(exc).__name__}.")
        return exc  # our bug, not the provider's — let it surface as one

    @staticmethod
    def _safe_detail(exc: Any) -> str:
        """A short description of a failure body — truncated, never echoed in full,
        same rationale as retrieval's error_detail."""
        body = exc.body
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()[:200]
        return (str(body) if body is not None else "no body")[:200]
