"""
Per-run budgets and accounting for report-quality-v1 (design §6; stage 32, S5a).

Every model request the pipeline makes goes through one `BudgetedLLM` per run:

- **Attempts are counted, all of them.** The OpenAI SDK's own retries are turned off
  (`OPENAI_MAX_RETRIES=0` on a settings copy, so `LLMService` is unmodified); the
  only retries left are the pipeline's own one-retry-on-invalid-output rules, and
  each of those is an attempt that must pass the budget check like any other.
- **Checked before sending.** An attempt is refused with `BudgetExhausted` when it
  would exceed the attempt limit, when the run's deadline has passed, or when its
  estimated tokens (prompt characters / 4, plus the requested output limit) would
  exceed the token limit minus the tokens reserved for later stages.
- **Tokens: measured when the provider reports them, estimated otherwise, and
  labelled as such.** A failed attempt (invalid output, truncation, transport error)
  reports no usage through `LLMService`, so its tokens are recorded as the pre-call
  estimate in `estimated_tokens`, never folded silently into `measured_tokens`.

Exhausting a budget is never support: callers record what could not run as
`not_evaluated` with reason `budget` or `deadline`.
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.llm import TokenUsage

#: Rough characters per token for the pre-call estimate. Deliberately simple; it is
#: corrected by measured usage whenever the provider reports it.
CHARS_PER_TOKEN = 4
#: Output estimate when a call passes no explicit limit.
DEFAULT_OUTPUT_ESTIMATE = 1000


class BudgetExhausted(AppError):
    """An attempt would exceed the run's budget or deadline. Not a support signal."""

    status_code = 429
    code = "report_quality_budget_exhausted"
    message = "The run's model-call budget or deadline would be exceeded."

    def __init__(self, reason: str, **kwargs) -> None:
        super().__init__(f"Budget exhausted: {reason}.", details={"reason": reason}, **kwargs)
        self.reason = reason  # "attempts" | "tokens" | "deadline"


class BudgetInsufficient(AppError):
    """The configured budget cannot fit the pipeline's minimum; refused before any call."""

    status_code = 422
    code = "report_quality_budget_insufficient"
    message = "The configured report-quality budget cannot fit a minimal run."


@dataclass
class StageAccount:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    measured_tokens: int = 0
    estimated_tokens: int = 0  # failed attempts and responses without usage
    refused: int = 0  # attempts refused by the budget before sending
    seconds: float = 0.0


@dataclass
class RunBudget:
    max_attempts: int
    max_tokens: int
    max_claims: int
    deadline_seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=-1.0)
    reserved: dict[str, int] = field(default_factory=dict)
    accounts: dict[str, StageAccount] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.started < 0:
            self.started = self.clock()

    @classmethod
    def for_depth(cls, settings: Settings, depth: str, **kwargs) -> "RunBudget":
        key = depth.upper()
        return cls(
            max_attempts=getattr(settings, f"REPORT_QUALITY_MAX_LLM_ATTEMPTS_{key}"),
            max_tokens=getattr(settings, f"REPORT_QUALITY_MAX_TOTAL_TOKENS_{key}"),
            max_claims=getattr(settings, f"REPORT_QUALITY_MAX_CLAIMS_VERIFIED_{key}"),
            deadline_seconds=getattr(settings, f"REPORT_QUALITY_RUN_DEADLINE_SECONDS_{key}"),
            **kwargs,
        )

    # --- totals -------------------------------------------------------------------

    @property
    def attempts(self) -> int:
        return sum(a.attempts for a in self.accounts.values())

    @property
    def tokens_used(self) -> int:
        return sum(a.measured_tokens + a.estimated_tokens for a in self.accounts.values())

    def account(self, stage: str) -> StageAccount:
        return self.accounts.setdefault(stage, StageAccount())

    def expired(self) -> bool:
        return self.clock() - self.started >= self.deadline_seconds

    # --- reservations -------------------------------------------------------------

    def reserve(self, name: str, tokens: int) -> None:
        """Hold tokens back for a later stage; other stages cannot spend them."""
        self.reserved[name] = max(0, tokens)

    def release(self, name: str) -> None:
        self.reserved.pop(name, None)

    def available_tokens(self, stage: str) -> int:
        held = sum(t for name, t in self.reserved.items() if name != stage)
        return self.max_tokens - held - self.tokens_used

    # --- the check ----------------------------------------------------------------

    def check(self, stage: str, estimate: int) -> None:
        if self.expired():
            reason = "deadline"
        elif self.attempts + 1 > self.max_attempts:
            reason = "attempts"
        elif estimate > self.available_tokens(stage):
            reason = "tokens"
        else:
            return
        self.account(stage).refused += 1
        raise BudgetExhausted(reason)

    def as_record(self) -> dict:
        return {
            "limits": {
                "attempts": self.max_attempts,
                "tokens": self.max_tokens,
                "claims": self.max_claims,
                "deadline_seconds": self.deadline_seconds,
            },
            "attempts": self.attempts,
            "tokens_used": self.tokens_used,
            "tokens_measured": sum(a.measured_tokens for a in self.accounts.values()),
            "tokens_estimated": sum(a.estimated_tokens for a in self.accounts.values()),
            "elapsed_seconds": round(self.clock() - self.started, 3),
            "reserved": dict(self.reserved),
            "stages": {name: asdict(account) for name, account in self.accounts.items()},
        }


def estimate_tokens(prompt: str, system: str | None, max_tokens: int | None) -> int:
    text = len(prompt) + len(system or "")
    return text // CHARS_PER_TOKEN + (max_tokens or DEFAULT_OUTPUT_ESTIMATE)


def no_sdk_retries(settings: Settings) -> Settings:
    """A settings copy whose LLM client makes exactly one HTTP attempt per call."""
    return settings.model_copy(update={"OPENAI_MAX_RETRIES": 0})


class BudgetedLLM:
    """The run's LLM, behind its budget. Duck-compatible with `LLMService` for the
    callers the pipeline composes (planner, draft, extractor, verifier): it exposes
    `generate_structured_output`, `generate_text`, `calls_made`, `total_usage` and
    `aclose`. `stage` names the account the next attempts are charged to."""

    def __init__(self, inner, budget: RunBudget, *, stage: str = "unassigned") -> None:
        self._inner = inner
        self.budget = budget
        self.stage = stage
        self.calls_made = 0
        self.total_usage = TokenUsage.zero()

    async def _attempt(self, method: str, prompt: str, *, system=None, max_tokens=None, **kwargs):
        estimate = estimate_tokens(prompt, system, max_tokens)
        self.budget.check(self.stage, estimate)
        account = self.budget.account(self.stage)
        account.attempts += 1
        started = self.budget.clock()
        try:
            response = await getattr(self._inner, method)(
                prompt, system=system, max_tokens=max_tokens, **kwargs
            )
        except Exception:
            account.failures += 1
            account.estimated_tokens += estimate
            raise
        finally:
            account.seconds += self.budget.clock() - started
        account.successes += 1
        self.calls_made += 1
        usage = getattr(response, "usage", None)
        if isinstance(usage, TokenUsage) and usage.total_tokens:
            account.measured_tokens += usage.total_tokens
            self.total_usage = self.total_usage + usage
        else:
            account.estimated_tokens += estimate
        return response

    async def generate_structured_output(
        self, prompt, schema, *, system=None, max_tokens=None, **kwargs
    ):
        return await self._attempt(
            "generate_structured_output",
            prompt,
            schema=schema,
            system=system,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def generate_text(self, prompt, *, system=None, max_tokens=None, **kwargs):
        return await self._attempt(
            "generate_text", prompt, system=system, max_tokens=max_tokens, **kwargs
        )

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
