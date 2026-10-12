# llm/ — The foundation-model boundary

Prompts in, validated and provider-agnostic responses out:

```python
from app.services.llm import LLMService

llm = LLMService()                                  # OPENAI_* settings
reply = await llm.generate_text("Summarise this.", system="You are terse.")
reply.text, reply.model, reply.usage                # LLMResponse

class Verdict(BaseModel):
    supported: bool
    reasoning: str

result = await llm.generate_structured_output("Is X supported by Y?", Verdict)
result.data                                         # a validated Verdict instance
```

## An external foundation model, not a trained one

**This project calls OpenAI's hosted models; it does not train or fine-tune a model.**
The research pipeline's claims — grounding, citation, verification — are claims about
how a model is *used*: what evidence it is shown, what it must cite, how its output is
checked. None of that requires owning weights, and pretending otherwise would spend
the project's budget reproducing what a provider already does well.

The boundary is enforced, not aspirational: nothing outside this package imports the
OpenAI SDK. Callers see `LLMResponse` / `StructuredLLMResponse[T]` / `TokenUsage` and
the `LLMError` hierarchy, so swapping the provider is an edit to this package alone.

## What the service guarantees

- **Key from the environment, never in logs.** `OPENAI_API_KEY` comes from settings;
  a missing or placeholder key raises `LLMNotConfigured` at call time (the service
  boots without one, same contract as retrieval). Authentication failures carry a
  fixed message naming the *setting* — the provider's own 401 body quotes part of
  the key, and is deliberately not echoed. Log lines carry model, latency and token
  counts only: never the prompt, never a header. Pinned by `TestNoKeyLeaks`.
- **Structured outputs are validated, not trusted.** The provider is asked for
  strict-schema JSON and the content is validated against the caller's Pydantic
  model; what fails validation raises `LLMResponseInvalid` and never reaches the
  caller. `data` is always a real instance of the schema; `text` keeps the raw JSON
  for auditing.
- **Retry handling is configured, not reimplemented.** The OpenAI SDK already does
  bounded retries with `Retry-After` handling and jittered backoff over httpx —
  duplicating that loop is how divergent bugs are born. `OPENAI_MAX_RETRIES` and
  `OPENAI_TIMEOUT_SECONDS` configure it; the tests pin the policy over a mocked
  transport (429 and 5xx retried, 401 and 400 not, timeout mapped to `LLMTimeout`).
- **Usage is reported verbatim or not at all.** `usage` is `None` when the provider
  sent none — absence of information, never zeros. The service also accumulates
  `calls_made` / `total_usage` across its lifetime for budget accounting.
- **A refusal is not a malformed response.** `LLMRefusal` carries the model's stated
  reason; `LLMResponseInvalid` means unusable output. The orchestrator will want to
  treat "declined" and "broken" differently.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | plain OpenAI key (`sk-...`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | default model; overridable per call |
| `OPENAI_TEMPERATURE` | `None` | `None` = parameter not sent (some models reject it) |
| `OPENAI_TIMEOUT_SECONDS` | 90 | per-request timeout (both providers) |
| `OPENAI_MAX_RETRIES` | 3 | SDK retries after the first attempt (both providers) |
| `AZURE_OPENAI_ENDPOINT` | — | `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_API_KEY` | — | Azure key (opaque, no `sk-` prefix) |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | Azure API version |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | — | the deployment; what "model" means on Azure |

**Azure OpenAI is supported as an alternative provider.** When endpoint, key and
deployment name are all set, the service talks to that Azure resource
(`AsyncAzureOpenAI` — same request pipeline, retries and exceptions; only addressing
and auth differ) and the deployment name becomes the default model. Azure wins when
both providers are configured. A partial Azure setup counts as *not configured*,
never as "half works".

## What is deliberately not here

No research generation: planning, verification and report prompts belong to the
orchestration stage. No streaming, no tool calling, no conversation state — nothing
needs them yet. `MAX_LLM_CALLS_PER_RUN` is enforced by the orchestrator that owns a
run, not here; this service counts, the owner budgets.
