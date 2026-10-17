"""report-quality-v1 stages 1–5, pure parts: planner validation and fallback, the
mirrored merge rule, budget allocation, MMR, selection and label freezing.

A scripted fake LLM and a deterministic bag-of-words embedder; no network, no model.
"""

import uuid

import pytest

from app.core.constants import EvidenceDepth
from app.services.llm import LLMProviderUnavailable, LLMResponseInvalid
from app.services.processing import DocumentProcessor
from app.services.quality.evidence import (
    RetrievalOutcome,
    allocate_passages,
    merge_candidates,
    mmr_select,
    per_subquestion_results,
    select_evidence,
    split_sentences,
)
from app.services.quality.models import CoveragePlan
from app.services.quality.planner import CoveragePlanner, Subquestion, fallback_plan
from app.services.quality.prompts import PLANNER_PROMPT_VERSION
from tests.unit.test_orchestrator import web_candidate
from tests.unit.test_vector_store import BagOfWordsEmbedder

TOPIC = "What are the health effects of regular moderate coffee consumption in adults?"
EMBED = BagOfWordsEmbedder()


def plan_payload(*subquestions, analysis=None):
    return CoveragePlan.model_validate(
        {
            "analysis": analysis
            or {
                "question_type": "causal",
                "key_entities": ["coffee", "coffee", " "],
                "time_scope": None,
                "population_or_scope": "adults",
                "needs_quantitative_evidence": True,
                "premise_to_check": None,
            },
            "subquestions": [
                {
                    "text": text,
                    "rationale": "<why>",
                    "web_query": f"web {text}",
                    "academic_query": f"What is {text}?",
                    "priority": priority,
                }
                for text, priority in subquestions
            ],
        }
    )


class Response:
    def __init__(self, data):
        self.data = data
        self.model = "fake-planner-2026"


class FakePlannerLLM:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []

    async def generate_structured_output(self, prompt, schema, *, system=None, **_):
        assert schema is CoveragePlan
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)


def planner(llm, maximum=3, cosine=0.9):
    return CoveragePlanner(
        llm, EMBED.embed_documents, max_subquestions=maximum, duplicate_min_cosine=cosine
    )


class TestPlanner:
    async def test_a_valid_plan_is_cleaned_capped_and_keywordized(self):
        llm = FakePlannerLLM(
            plan_payload(
                ("Does coffee lower type 2 diabetes risk?", "core"),
                ("How does coffee affect blood pressure?", "core"),
                ("What are coffee's effects on sleep quality?", "supporting"),
                ("Is coffee linked to anxiety in adults?", "supporting"),
            )
        )
        result = await planner(llm, maximum=3).plan(TOPIC)
        assert not result.fallback and result.llm_calls == 1
        assert [s.alias for s in result.subquestions] == ["Q1", "Q2", "Q3"]
        assert [s.is_core for s in result.subquestions] == [True, True, False]
        assert result.subquestions[0].academic_query.startswith("coffee lower type 2")
        assert "kept the first 3" in " ".join(result.warnings)
        assert result.analysis.key_entities == ["coffee"]
        assert result.prompt_version == PLANNER_PROMPT_VERSION

    async def test_near_duplicates_are_merged_and_core_is_guaranteed(self):
        llm = FakePlannerLLM(
            plan_payload(
                ("Does coffee lower diabetes risk in adults?", "supporting"),
                ("Does coffee lower diabetes risk in adults?", "supporting"),
                ("How does coffee affect blood pressure?", "supporting"),
            )
        )
        result = await planner(llm).plan(TOPIC)
        assert len(result.subquestions) == 2
        assert all(s.is_core for s in result.subquestions)
        assert any("near-duplicate" in w for w in result.warnings)

    async def test_invalid_output_is_retried_once_then_falls_back(self):
        llm = FakePlannerLLM(LLMResponseInvalid(), LLMResponseInvalid())
        result = await planner(llm).plan(TOPIC)
        assert result.fallback and result.llm_calls == 2
        assert [s.text for s in result.subquestions] == [TOPIC]
        assert "previous output did not validate" in llm.prompts[1]

    async def test_a_retry_that_validates_is_used(self):
        llm = FakePlannerLLM(
            LLMResponseInvalid(),
            plan_payload(
                ("Does coffee lower diabetes risk?", "core"), ("Does coffee raise anxiety?", "core")
            ),
        )
        result = await planner(llm).plan(TOPIC)
        assert not result.fallback and result.llm_calls == 2

    async def test_too_few_usable_subquestions_falls_back(self):
        llm = FakePlannerLLM(plan_payload(("Does coffee lower diabetes risk?", "core")))
        result = await planner(llm).plan(TOPIC)
        assert result.fallback and result.analysis is not None
        assert result.subquestions == fallback_plan(TOPIC)

    async def test_a_transport_failure_is_not_disguised_as_a_fallback(self):
        llm = FakePlannerLLM(LLMProviderUnavailable())
        with pytest.raises(LLMProviderUnavailable):
            await planner(llm).plan(TOPIC)


def sq(alias, text, core=True):
    return Subquestion(alias, text, text, text, core, "<why>")


class TestBudgetAndSelection:
    def test_per_subquestion_results(self):
        assert per_subquestion_results(4, 3) == 2
        assert per_subquestion_results(12, 3) == 4
        assert per_subquestion_results(15, 6) == 3

    def test_allocation_gives_the_remainder_to_core_first(self):
        plan = [sq("Q1", "a", False), sq("Q2", "b"), sq("Q3", "c")]
        assert allocate_passages(plan, 8) == {"Q1": 2, "Q2": 3, "Q3": 3}
        assert allocate_passages(plan, 3) == {"Q1": 1, "Q2": 1, "Q3": 1}

    def test_mmr_prefers_relevant_then_diverse(self):
        ids = [uuid.uuid4() for _ in range(3)]
        # Three equally relevant passages, two of them identical: with lambda 0.7 the
        # second pick is the different one, not the duplicate.
        query = [1.0, 1.0, 0.0]
        candidates = [
            (ids[0], [1.0, 0.2, 0.0]),
            (ids[1], [1.0, 0.2, 0.0]),
            (ids[2], [0.2, 1.0, 0.0]),
        ]
        assert mmr_select(query, candidates, 2) == [ids[0], ids[2]]
        assert mmr_select(query, candidates, 0) == []

    def test_sentence_split_is_conservative(self):
        assert split_sentences(
            "VE was 36% (U.S. Flu VE). It rose, e.g. Smith et al. Found 54%."
        ) == ["VE was 36% (U.S. Flu VE).", "It rose, e.g. Smith et al. Found 54%."]


def candidate(index, text):
    found = web_candidate(index)
    return found.model_copy(update={"snippet": text})


class TestSelectEvidence:
    def setup(self):
        texts = {
            0: "Coffee intake lowered type 2 diabetes risk by 15% in adults. " * 4,
            1: "Coffee raised blood pressure by 2 mmHg in adults in 2019. " * 4,
            2: "Sleep quality fell with evening coffee intake in adults. " * 4,
        }
        candidates = [candidate(i, t) for i, t in texts.items()]
        documents = DocumentProcessor().process_many(candidates)
        chunks = [c for d in documents for c in d.chunks]
        fps = [c.fingerprint for c in candidates]
        retrieval = RetrievalOutcome(
            candidates=candidates,
            found_by={fps[0]: ["Q1"], fps[1]: ["Q2", "Q1"], fps[2]: ["Q2"]},
            failures=[],
            failed_subquestions={"Q3"},
        )
        plan = [
            sq("Q1", "coffee diabetes risk"),
            sq("Q2", "coffee blood pressure"),
            sq("Q3", "coffee anxiety", core=False),
        ]
        ids = {s.alias: uuid.uuid4() for s in plan}
        source_ids = {fp: uuid.uuid4() for fp in fps}
        return plan, ids, chunks, retrieval, source_ids, fps

    def test_selection_uses_own_sources_freezes_labels_and_marks_gaps(self):
        plan, ids, chunks, retrieval, source_ids, fps = self.setup()
        evidence = select_evidence(
            plan, ids, chunks, retrieval, source_ids, EMBED, total_passages=6, entities=["coffee"]
        )
        by_alias = {s.alias: s for s in evidence.per_subquestion}
        q1_sources = {evidence.aliases.passages[e].source_label for e in by_alias["Q1"].passages}
        assert q1_sources <= {evidence.labels[fps[0]], evidence.labels[fps[1]]}
        assert evidence.labels[fps[2]] not in q1_sources  # not found by Q1
        assert by_alias["Q3"].passages == [] and by_alias["Q3"].gap_reason == "provider_failure"
        assert [g.alias for g in evidence.gaps] == ["Q3"]
        # labels in first-selection order; every passage's source is labelled
        assert sorted(evidence.labels.values()) == [
            f"S{i}" for i in range(1, len(evidence.labels) + 1)
        ]
        assert set(evidence.aliases.sources) == set(evidence.labels.values())
        assert all(
            ref.source_label in evidence.aliases.sources
            for ref in evidence.aliases.passages.values()
        )
        assert set(evidence.profiles) == set(evidence.labels.values())
        assert all(
            p.evidence_depth == EvidenceDepth.SNIPPET.value for p in evidence.profiles.values()
        )

    def test_selection_is_deterministic(self):
        plan, ids, chunks, retrieval, source_ids, _ = self.setup()
        first = select_evidence(plan, ids, chunks, retrieval, source_ids, EMBED, total_passages=6)
        second = select_evidence(plan, ids, chunks, retrieval, source_ids, EMBED, total_passages=6)
        assert first.labels == second.labels
        assert first.aliases.passages == second.aliases.passages

    def test_passage_quantities_are_extracted_for_the_audit(self):
        plan, ids, chunks, retrieval, source_ids, _ = self.setup()
        evidence = select_evidence(
            plan, ids, chunks, retrieval, source_ids, EMBED, total_passages=6
        )
        units = {m.unit for mentions in evidence.quantities.values() for m in mentions}
        assert {"percent", "mmHg"} <= units
        record = evidence.as_record()
        assert record["quantity_mentions"] and "text" not in str(record["per_subquestion"])


class TestMergeMirrorsTheLegacyRule:
    """The mirrored merge must agree with ResearchOrchestrator._retrieve, exactly."""

    async def test_same_outcomes_same_result(self):
        from app.services.orchestration import ResearchOrchestrator
        from app.services.orchestration.orchestrator import DEPTH_PROFILES
        from tests.unit.test_orchestrator import (
            FakeAcademicSearch,
            FakeWebSearch,
            academic_candidate,
            settings,
            web_result,
        )
        from tests.unit.test_orchestrator import academic_result as academic_res

        web = [web_candidate(0), web_candidate(1), web_candidate(0)]
        academic = [academic_candidate(0), academic_candidate(1, abstract=None)]
        orchestrator = ResearchOrchestrator(
            settings=settings(),
            web_search=FakeWebSearch(web_result(*web)),
            academic_search=FakeAcademicSearch(academic_res(*academic)),
        )
        legacy = await orchestrator._retrieve(
            "<topic under test>", DEPTH_PROFILES["quick"], [], True
        )
        mirrored, found_by, merged = merge_candidates([("Q1", web), ("Q1", academic)])
        assert [c.fingerprint for c in mirrored] == [c.fingerprint for c in legacy]
        assert merged == 1 and found_by[web[0].fingerprint] == ["Q1"]

    def test_a_document_found_by_two_subquestions_keeps_both_owners(self):
        shared = web_candidate(5)
        _, found_by, merged = merge_candidates([("Q1", [shared]), ("Q2", [shared])])
        assert found_by[shared.fingerprint] == ["Q1", "Q2"] and merged == 1
