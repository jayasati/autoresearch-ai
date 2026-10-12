"""
The LLM service, against a mocked transport.

Every test drives the **real OpenAI SDK** over `httpx.MockTransport` — the same
approach as the retrieval tests, for the same reason: the request assembly, retry
configuration and error mapping under test are the code that runs in production.
Mocking the SDK itself would test the mock.

No test contacts the network, and no real key exists anywhere in this file: the
placeholder key is obviously fake, and the assertions in `TestNoKeyLeaks` are about
it never appearing in logs or exceptions even so.
"""

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.services.llm import (
    LLMAuthenticationFailed,
    LLMNotConfigured,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMRefusal,
    LLMRequestInvalid,
    LLMResponse,
    LLMResponseInvalid,
    LLMService,
    LLMTimeout,
    StructuredLLMResponse,
    TokenUsage,
)

API_KEY = "sk-test-key-that-must-never-leak"
PROMPT = "Summarise the evidence."


def settings(**overrides) -> Settings:
    base = {
        "OPENAI_API_KEY": API_KEY,
        "OPENAI_MODEL": "gpt-4o-mini",
        "OPENAI_MAX_RETRIES": 2,
        "OPENAI_TIMEOUT_SECONDS": 5,
        # Explicitly no Azure: Settings also reads the developer's real .env, and
        # these tests must pin the plain-OpenAI path regardless of what it holds.
        "AZURE_OPENAI_ENDPOINT": None,
        "AZURE_OPENAI_API_KEY": None,
        "AZURE_OPENAI_DEPLOYMENT_NAME": None,
        "APP_ENV": "test",
    }
    return Settings(**{**base, **overrides})


AZURE_SETTINGS = {
    "OPENAI_API_KEY": None,
    "AZURE_OPENAI_ENDPOINT": "https://unit-test.openai.azure.invalid/",
    "AZURE_OPENAI_API_KEY": "azure-test-key-that-must-never-leak",
    "AZURE_OPENAI_API_VERSION": "2024-12-01-preview",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o-deployed",
}


def completion_payload(
    content: str = "<generated text under test>",
    *,
    model: str = "gpt-4o-mini-2024-07-18",
    finish_reason: str = "stop",
    usage: dict | None | type[Ellipsis] = ...,
    refusal: str | None = None,
) -> dict:
    if usage is ...:  # the default; None means "the provider reported nothing"
        usage = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
    message: dict = {"role": "assistant", "content": content, "refusal": refusal}
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1760000000,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def error_payload(message: str, *, error_type: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": error_type, "code": None, "param": None}}


class Recorder:
    """A MockTransport handler that records requests and replays queued responses.

    The last queued response repeats, so "retry until it works" and "fail every
    time" are both expressible. An exception in the queue is raised instead.
    """

    def __init__(self, *responses):
        self.queued = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queued:
            raise AssertionError("more requests than queued responses")
        response = self.queued.pop(0) if len(self.queued) > 1 else self.queued[0]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def service(handler, **setting_overrides) -> LLMService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMService(settings=settings(**setting_overrides), http_client=client)


def ok(content: str = "<generated text under test>", **kwargs) -> Recorder:
    return Recorder(httpx.Response(200, json=completion_payload(content, **kwargs)))


class Claim(BaseModel):
    """A small schema for structured-output tests."""

    statement: str
    confidence: float = Field(ge=0.0, le=1.0)


def claim_json(statement: str = "<claim under test>", confidence: float = 0.4) -> str:
    return json.dumps({"statement": statement, "confidence": confidence})


# --------------------------------------------------------------------------- #
# generate_text
# --------------------------------------------------------------------------- #


class TestGenerateText:
    async def test_returns_a_structured_response(self):
        llm = service(ok("The evidence is mixed."))
        response = await llm.generate_text(PROMPT)

        assert isinstance(response, LLMResponse)
        assert response.text == "The evidence is mixed."
        # The model the provider ANSWERED with — the dated snapshot, not our alias.
        assert response.model == "gpt-4o-mini-2024-07-18"
        assert response.finish_reason == "stop"
        assert response.usage == TokenUsage(prompt_tokens=12, completion_tokens=7, total_tokens=19)
        assert response.latency_seconds >= 0

    async def test_sends_the_configured_model_and_the_prompt(self):
        recorder = ok()
        llm = service(recorder)
        await llm.generate_text(PROMPT)

        body = recorder.body()
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [{"role": "user", "content": PROMPT}]

    async def test_system_prompt_and_model_override_are_sent(self):
        recorder = ok()
        llm = service(recorder)
        await llm.generate_text(PROMPT, system="You verify claims.", model="gpt-4o")

        body = recorder.body()
        assert body["model"] == "gpt-4o"
        assert body["messages"][0] == {"role": "system", "content": "You verify claims."}

    async def test_temperature_is_omitted_unless_configured(self):
        """Some models reject the parameter, so None means 'do not send it' —
        not 'send a default'."""
        recorder = ok()
        await service(recorder).generate_text(PROMPT)
        assert "temperature" not in recorder.body()

    async def test_temperature_from_settings_and_per_call_override(self):
        recorder = ok()
        await service(recorder, OPENAI_TEMPERATURE=0.0).generate_text(PROMPT)
        assert recorder.body()["temperature"] == 0.0

        recorder = ok()
        await service(recorder, OPENAI_TEMPERATURE=0.0).generate_text(PROMPT, temperature=0.7)
        assert recorder.body()["temperature"] == 0.7

    async def test_max_tokens_is_passed_through_when_given(self):
        recorder = ok()
        await service(recorder).generate_text(PROMPT, max_tokens=256)
        assert recorder.body()["max_completion_tokens"] == 256

    async def test_a_blank_prompt_is_rejected_without_a_request(self):
        recorder = ok()
        llm = service(recorder)
        with pytest.raises(ValueError, match="prompt"):
            await llm.generate_text("   ")
        assert recorder.call_count == 0

    async def test_truncation_is_reported_not_hidden(self):
        llm = service(ok("A partial ans", finish_reason="length"))
        response = await llm.generate_text(PROMPT)
        assert response.finish_reason == "length"


# --------------------------------------------------------------------------- #
# generate_structured_output
# --------------------------------------------------------------------------- #


class TestStructuredOutput:
    async def test_valid_json_becomes_a_validated_instance(self):
        llm = service(ok(claim_json("Coffee may aid memory.", 0.6)))
        response = await llm.generate_structured_output(PROMPT, Claim)

        assert isinstance(response, StructuredLLMResponse)
        assert isinstance(response.data, Claim)
        assert response.data.statement == "Coffee may aid memory."
        assert response.data.confidence == 0.6
        assert response.text == claim_json("Coffee may aid memory.", 0.6)  # the raw bytes
        assert response.usage is not None and response.usage.total_tokens == 19

    async def test_the_request_asks_for_strict_json_schema(self):
        recorder = ok(claim_json())
        await service(recorder).generate_structured_output(PROMPT, Claim)

        response_format = recorder.body()["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "Claim"
        assert response_format["json_schema"]["strict"] is True
        assert "statement" in response_format["json_schema"]["schema"]["properties"]

    async def test_content_that_is_not_json_raises(self):
        llm = service(ok("I would rate this claim as fairly confident."))
        with pytest.raises(LLMResponseInvalid, match="Claim"):
            await llm.generate_structured_output(PROMPT, Claim)

    async def test_json_that_fails_the_schema_raises(self):
        llm = service(ok(json.dumps({"statement": "ok", "confidence": 3.5})))  # > 1.0
        with pytest.raises(LLMResponseInvalid, match="Claim"):
            await llm.generate_structured_output(PROMPT, Claim)

    async def test_a_refusal_is_its_own_error_with_the_reason(self):
        llm = service(ok(None, refusal="I can't help with that."))
        with pytest.raises(LLMRefusal) as excinfo:
            await llm.generate_structured_output(PROMPT, Claim)
        assert excinfo.value.details["refusal"] == "I can't help with that."


# --------------------------------------------------------------------------- #
# configuration and authentication
# --------------------------------------------------------------------------- #


class TestConfiguration:
    async def test_no_key_raises_without_any_request(self):
        recorder = ok()
        llm = service(recorder, OPENAI_API_KEY=None)
        with pytest.raises(LLMNotConfigured) as excinfo:
            await llm.generate_text(PROMPT)
        assert recorder.call_count == 0
        assert excinfo.value.details["setting"] == "OPENAI_API_KEY"

    async def test_the_env_example_placeholder_is_not_a_key(self):
        llm = service(ok(), OPENAI_API_KEY="sk-replace-me")
        with pytest.raises(LLMNotConfigured):
            await llm.generate_text(PROMPT)

    async def test_the_key_is_sent_as_the_auth_header(self):
        recorder = ok()
        await service(recorder).generate_text(PROMPT)
        assert recorder.requests[0].headers["Authorization"] == f"Bearer {API_KEY}"

    async def test_a_rejected_key_is_not_retried(self):
        recorder = Recorder(httpx.Response(401, json=error_payload("Incorrect API key")))
        llm = service(recorder)
        with pytest.raises(LLMAuthenticationFailed):
            await llm.generate_text(PROMPT)
        assert recorder.call_count == 1  # wrong now, wrong in two seconds


# --------------------------------------------------------------------------- #
# Azure OpenAI
# --------------------------------------------------------------------------- #


class TestAzureProvider:
    """The same service against an Azure resource: only addressing and auth differ."""

    async def test_requests_go_to_the_deployment_url_with_the_azure_header(self):
        recorder = ok()
        await service(recorder, **AZURE_SETTINGS).generate_text(PROMPT)

        request = recorder.requests[0]
        assert request.url.host == "unit-test.openai.azure.invalid"
        assert "/openai/deployments/gpt-4o-deployed/chat/completions" in request.url.path
        assert request.headers["api-key"] == "azure-test-key-that-must-never-leak"

    async def test_the_default_model_is_the_deployment_name(self):
        recorder = ok()
        await service(recorder, **AZURE_SETTINGS).generate_text(PROMPT)
        assert recorder.body()["model"] == "gpt-4o-deployed"

    async def test_azure_wins_when_both_providers_are_configured(self):
        """Three deliberate values beat one; the request must not go to OpenAI."""
        recorder = ok()
        await service(recorder, **{**AZURE_SETTINGS, "OPENAI_API_KEY": API_KEY}).generate_text(
            PROMPT
        )
        assert recorder.requests[0].url.host == "unit-test.openai.azure.invalid"

    async def test_an_incomplete_azure_setup_is_not_configured(self):
        """Endpoint and key but no deployment: nothing to address a request to."""
        recorder = ok()
        llm = service(
            recorder, **{**AZURE_SETTINGS, "AZURE_OPENAI_DEPLOYMENT_NAME": None}
        )
        with pytest.raises(LLMNotConfigured):
            await llm.generate_text(PROMPT)
        assert recorder.call_count == 0

    async def test_structured_output_works_on_azure_too(self):
        llm = service(ok(claim_json("From Azure.", 0.5)), **AZURE_SETTINGS)
        response = await llm.generate_structured_output(PROMPT, Claim)
        assert response.data.statement == "From Azure."


# --------------------------------------------------------------------------- #
# retries, timeouts, provider failures
# --------------------------------------------------------------------------- #


class TestRetries:
    async def test_a_rate_limit_is_retried_and_can_succeed(self):
        recorder = Recorder(
            httpx.Response(
                429, json=error_payload("slow down"), headers={"Retry-After": "0"}
            ),
            httpx.Response(200, json=completion_payload("made it")),
        )
        response = await service(recorder).generate_text(PROMPT)
        assert response.text == "made it"
        assert recorder.call_count == 2

    async def test_a_persistent_rate_limit_exhausts_the_retries(self):
        rate_limited = httpx.Response(
            429, json=error_payload("slow down"), headers={"Retry-After": "0"}
        )
        recorder = Recorder(rate_limited)
        with pytest.raises(LLMRateLimited):
            await service(recorder).generate_text(PROMPT)
        assert recorder.call_count == 3  # one try + OPENAI_MAX_RETRIES

    async def test_a_server_error_is_retried_and_can_succeed(self):
        recorder = Recorder(
            httpx.Response(500, json=error_payload("boom"), headers={"Retry-After": "0"}),
            httpx.Response(200, json=completion_payload("recovered")),
        )
        response = await service(recorder).generate_text(PROMPT)
        assert response.text == "recovered"
        assert recorder.call_count == 2

    async def test_a_persistent_server_error_surfaces_as_unavailable(self):
        recorder = Recorder(
            httpx.Response(503, json=error_payload("down"), headers={"Retry-After": "0"})
        )
        with pytest.raises(LLMProviderUnavailable) as excinfo:
            await service(recorder).generate_text(PROMPT)
        assert recorder.call_count == 3
        assert excinfo.value.details["status"] == 503

    async def test_a_timeout_surfaces_as_llm_timeout(self):
        recorder = Recorder(httpx.ConnectTimeout("too slow"))
        llm = service(recorder, OPENAI_MAX_RETRIES=0)
        with pytest.raises(LLMTimeout, match="5s"):
            await llm.generate_text(PROMPT)

    async def test_a_bad_request_is_not_retried(self):
        recorder = Recorder(
            httpx.Response(
                400, json=error_payload("Unsupported parameter: 'temperature'")
            )
        )
        with pytest.raises(LLMRequestInvalid, match="temperature"):
            await service(recorder).generate_text(PROMPT, temperature=0.5)
        assert recorder.call_count == 1


# --------------------------------------------------------------------------- #
# usage tracking
# --------------------------------------------------------------------------- #


class TestUsageTracking:
    async def test_usage_accumulates_across_calls(self):
        llm = service(ok())
        await llm.generate_text(PROMPT)
        await llm.generate_text(PROMPT)

        assert llm.calls_made == 2
        assert llm.total_usage == TokenUsage(
            prompt_tokens=24, completion_tokens=14, total_tokens=38
        )

    async def test_absent_usage_is_none_not_zeros(self):
        llm = service(ok(usage=None))
        response = await llm.generate_text(PROMPT)
        assert response.usage is None
        assert llm.calls_made == 1
        assert llm.total_usage == TokenUsage.zero()  # nothing invented


# --------------------------------------------------------------------------- #
# the key never leaks
# --------------------------------------------------------------------------- #


class TestNoKeyLeaks:
    async def test_the_key_is_not_in_the_auth_exception_or_logs(self, caplog):
        """Requirement 8. The provider's own 401 body quotes the key it rejected —
        the mapped exception must not echo it, and nothing may log it."""
        body = error_payload(f"Incorrect API key provided: {API_KEY}")
        recorder = Recorder(httpx.Response(401, json=body))
        llm = service(recorder)

        with caplog.at_level("DEBUG"), pytest.raises(LLMAuthenticationFailed) as excinfo:
            await llm.generate_text(PROMPT)

        for leak_surface in (str(excinfo.value), repr(excinfo.value), str(excinfo.value.details)):
            assert API_KEY not in leak_surface
        assert API_KEY not in caplog.text

    async def test_successful_call_logs_never_carry_the_key(self, caplog):
        llm = service(ok())
        with caplog.at_level("DEBUG"):
            await llm.generate_text(PROMPT)
        assert API_KEY not in caplog.text
