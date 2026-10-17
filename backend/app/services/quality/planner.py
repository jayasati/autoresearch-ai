"""
Question analysis and coverage plan — design stages 1–2 (stage 30, S3).

    topic ──LLM (planner-v1, one structured call)──▶ CoveragePlan
          ──code──▶ validated plan: empty items dropped, near-duplicates merged,
                    capped at the depth's maximum, academic queries keywordized,
                    at least one core subquestion

Nothing the model returns is trusted as is. Invalid output is retried once; a second
failure, or a plan with fewer than two usable subquestions after validation, falls
back to a single subquestion — the topic itself — with `fallback=True` and a warning.
The run continues; the fallback is recorded, never hidden. A transport or
configuration failure (no key, provider down) is NOT a fallback: it propagates and
the run fails honestly, as in the legacy pipeline.

This call is not yet budgeted — the budget wrapper is S5, and nothing can reach
this code until the pipeline is wired behind the gate.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.services.llm import LLMResponseInvalid
from app.services.quality.models import CoveragePlan, QuestionAnalysis
from app.services.quality.prompts import (
    PLANNER_PROMPT_VERSION,
    PLANNER_RETRY_REMINDER,
    PLANNER_SYSTEM,
    build_planner_prompt,
)
from app.services.quality.quantities import cosine
from app.services.retrieval.semantic_scholar_service import keywordize_academic_query

Embed = Callable[[list[str]], list[list[float]]]

#: A plan needs at least this many usable subquestions to count as a decomposition.
MIN_SUBQUESTIONS = 2


@dataclass(frozen=True)
class Subquestion:
    alias: str  # "Q1"
    text: str
    web_query: str
    academic_query: str
    is_core: bool
    rationale: str


@dataclass(frozen=True)
class PlanResult:
    subquestions: list[Subquestion]
    analysis: QuestionAnalysis | None
    fallback: bool
    prompt_version: str
    model: str | None
    llm_calls: int
    warnings: list[str] = field(default_factory=list)

    def as_record(self) -> dict:
        return {
            "fallback": self.fallback,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "analysis": self.analysis.model_dump() if self.analysis else None,
            "subquestions": [
                {
                    "alias": s.alias,
                    "text": s.text,
                    "web_query": s.web_query,
                    "academic_query": s.academic_query,
                    "is_core": s.is_core,
                    "rationale": s.rationale,
                }
                for s in self.subquestions
            ],
            "warnings": list(self.warnings),
        }


def fallback_plan(topic: str) -> list[Subquestion]:
    """The topic as its only subquestion: no decomposition, but a usable plan."""
    text = " ".join(topic.split())
    return [
        Subquestion(
            "Q1",
            text,
            text,
            keywordize_academic_query(text),
            True,
            "fallback: the planner produced no usable plan",
        )
    ]


class CoveragePlanner:
    def __init__(self, llm, embed: Embed, *, max_subquestions: int, duplicate_min_cosine: float):
        self._llm = llm
        self._embed = embed
        self.max_subquestions = max_subquestions
        self.duplicate_min_cosine = duplicate_min_cosine

    async def plan(self, topic: str) -> PlanResult:
        warnings: list[str] = []
        prompt = build_planner_prompt(topic, self.max_subquestions)
        calls = 0
        response = None
        for attempt in (prompt, prompt + PLANNER_RETRY_REMINDER):
            calls += 1
            try:
                response = await self._llm.generate_structured_output(
                    attempt, CoveragePlan, system=PLANNER_SYSTEM
                )
                break
            except LLMResponseInvalid as exc:
                warnings.append(f"Planner output invalid (attempt {calls}): {exc.message}")
        if response is None:
            warnings.append(
                "The planner failed twice; falling back to the topic as one subquestion."
            )
            return PlanResult(
                fallback_plan(topic), None, True, PLANNER_PROMPT_VERSION, None, calls, warnings
            )

        plan: CoveragePlan = response.data
        subquestions = self._validated(plan, warnings)
        if len(subquestions) < MIN_SUBQUESTIONS:
            warnings.append(
                f"Only {len(subquestions)} usable subquestion(s) after validation; "
                "falling back to the topic as one subquestion."
            )
            return PlanResult(
                fallback_plan(topic),
                plan.analysis,
                True,
                PLANNER_PROMPT_VERSION,
                response.model,
                calls,
                warnings,
            )
        return PlanResult(
            subquestions,
            plan.analysis,
            False,
            PLANNER_PROMPT_VERSION,
            response.model,
            calls,
            warnings,
        )

    def _validated(self, plan: CoveragePlan, warnings: list[str]) -> list[Subquestion]:
        items = [item for item in plan.subquestions if item.text.strip()]
        if len(items) < len(plan.subquestions):
            warnings.append(f"Dropped {len(plan.subquestions) - len(items)} empty subquestion(s).")
        kept = []
        if items:
            vectors = self._embed([item.text for item in items])
            kept_vectors: list[list[float]] = []
            for item, vector in zip(items, vectors, strict=True):
                if any(
                    cosine(vector, other) >= self.duplicate_min_cosine for other in kept_vectors
                ):
                    warnings.append(f"Merged a near-duplicate subquestion: {item.text!r}")
                    continue
                kept.append(item)
                kept_vectors.append(vector)
        if len(kept) > self.max_subquestions:
            warnings.append(
                f"Plan had {len(kept)} subquestions; kept the first {self.max_subquestions}."
            )
            kept = kept[: self.max_subquestions]
        priorities: list[Literal["core", "supporting"]] = [item.priority for item in kept]
        if kept and "core" not in priorities:
            warnings.append("No subquestion was marked core; all are treated as core.")
            priorities = ["core"] * len(kept)
        return [
            Subquestion(
                alias=f"Q{index + 1}",
                text=item.text,
                web_query=item.web_query or item.text,
                academic_query=keywordize_academic_query(item.academic_query or item.text),
                is_core=priority == "core",
                rationale=item.rationale,
            )
            for index, (item, priority) in enumerate(zip(kept, priorities, strict=True))
        ]
