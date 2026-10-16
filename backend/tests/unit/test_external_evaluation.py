"""
metrics-v2 external evaluation: the pool, its cache and hash, the arm-blind judge,
and the failure semantics.

The safeguards under test, by number (see the approval in DEVELOPMENT_LOG stage 19):
(3) one cached pool per (question, version), identical across arms, never silently
refreshed; (4) arm-blind judging with the run-owned enforcement rules; (5) explicit
unavailable results, never artificial zeros; (7) the manually labeled validation
cases exist and cover every required category, including true-but-absent-from-pool;
(8) hand-computed metric values and v1 untouched.
"""

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.constants import VerificationVerdict
from app.services.benchmark import metrics as formulas
from app.services.benchmark.external_judge import judge_claims_against_pool
from app.services.benchmark.external_pool import (
    POOL_VERSION,
    PoolUnavailable,
    load_or_build_pool,
    pool_cache_path,
)
from app.services.llm import LLMResponseInvalid
from app.services.retrieval import SearchProviderUnavailable
from app.services.vectorstore import VectorStore
from tests.unit.test_orchestrator import (
    TOPIC,
    FakeAcademicSearch,
    FakeWebSearch,
    academic_candidate,
    academic_result,
    web_candidate,
    web_result,
)
from tests.unit.test_vector_store import BagOfWordsEmbedder
from tests.unit.test_verification import WEB_QUOTE, FakeVerifierLLM, judge

QUESTION_ID = "dev-normal_research-01"


def pool_settings(tmp_path) -> Settings:
    return Settings(BENCHMARK_DATA_DIR=str(tmp_path), APP_ENV="test")


def store_factory_for(tmp_path):
    return lambda name: VectorStore(
        embedder=BagOfWordsEmbedder(),
        persist_dir=str(tmp_path / "chroma"),
        collection_name=name,
    )


class TestExternalPool:
    async def test_the_pool_is_built_once_and_cached_with_a_stable_hash(self, tmp_path):
        settings = pool_settings(tmp_path)
        web = FakeWebSearch(web_result(web_candidate()))
        academic = FakeAcademicSearch(academic_result(academic_candidate()))

        first, _ = await load_or_build_pool(
            QUESTION_ID, TOPIC, settings=settings,
            store_factory=store_factory_for(tmp_path),
            web_search=web, academic_search=academic,
        )
        assert pool_cache_path(QUESTION_ID, POOL_VERSION, settings).exists()
        assert len(web.calls) == 1

        second, _ = await load_or_build_pool(
            QUESTION_ID, TOPIC, settings=settings,
            store_factory=store_factory_for(tmp_path),
            web_search=web, academic_search=academic,
        )
        assert len(web.calls) == 1  # cache hit: providers were NOT called again
        assert second.content_hash == first.content_hash
        assert second.chunk_count == first.chunk_count

    async def test_an_edited_cache_is_refused_not_silently_accepted(self, tmp_path):
        settings = pool_settings(tmp_path)
        await load_or_build_pool(
            QUESTION_ID, TOPIC, settings=settings,
            store_factory=store_factory_for(tmp_path),
            web_search=FakeWebSearch(web_result(web_candidate())),
            academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        )
        cache = pool_cache_path(QUESTION_ID, POOL_VERSION, settings)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["chunks"][0]["text"] = payload["chunks"][0]["text"] + " quietly edited"
        cache.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(PoolUnavailable, match="hash mismatch"):
            await load_or_build_pool(
                QUESTION_ID, TOPIC, settings=settings,
                store_factory=store_factory_for(tmp_path),
            )

    async def test_both_providers_failing_is_an_explicit_unavailable(self, tmp_path):
        with pytest.raises(PoolUnavailable, match="tavily"):
            await load_or_build_pool(
                QUESTION_ID, TOPIC, settings=pool_settings(tmp_path),
                store_factory=store_factory_for(tmp_path),
                web_search=FakeWebSearch(SearchProviderUnavailable("tavily", "down")),
                academic_search=FakeAcademicSearch(
                    SearchProviderUnavailable("semantic_scholar", "down")
                ),
            )

    async def test_one_provider_failing_still_builds_a_pool(self, tmp_path):
        pool, _ = await load_or_build_pool(
            QUESTION_ID, TOPIC, settings=pool_settings(tmp_path),
            store_factory=store_factory_for(tmp_path),
            web_search=FakeWebSearch(SearchProviderUnavailable("tavily", "down")),
            academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        )
        assert pool.chunk_count >= 1


class TestExternalJudge:
    async def claims_pool(self, tmp_path):
        settings = pool_settings(tmp_path)
        _, store = await load_or_build_pool(
            QUESTION_ID, TOPIC, settings=settings,
            store_factory=store_factory_for(tmp_path),
            web_search=FakeWebSearch(web_result(web_candidate())),
            academic_search=FakeAcademicSearch(academic_result(academic_candidate())),
        )
        import uuid

        claims = [{"claim_id": uuid.uuid4(),
                   "text": "Moderate caffeine intake improved working memory scores."}]
        return claims, store

    async def test_judging_is_arm_blind_and_prompts_carry_no_arm_identifiers(self, tmp_path):
        """The judge's inputs are claim text and pool passages only — no arm, mode
        or run identifier can reach the prompt."""
        claims, store = await self.claims_pool(tmp_path)

        captured: list[str] = []

        class Recording(FakeVerifierLLM):
            async def generate_structured_output(self, prompt, schema, **kwargs):
                captured.append(prompt)
                return await super().generate_structured_output(prompt, schema, **kwargs)

        llm = Recording().on("caffeine", lambda p: judge(p, support_containing=WEB_QUOTE))
        await judge_claims_against_pool(llm, store, claims)

        assert captured
        for prompt in captured:
            for identifier in ("model_only", "hybrid", "search_grounded",
                               "autoresearch", "arm", "mode="):
                assert identifier not in prompt.lower()

    async def test_quote_enforcement_downgrades_exactly_like_run_verification(self, tmp_path):
        claims, store = await self.claims_pool(tmp_path)
        llm = FakeVerifierLLM().on(
            "caffeine",
            lambda p: judge(p, support_containing=WEB_QUOTE,
                            support_quote="words in no passage at all"),
        )
        outcome = await judge_claims_against_pool(llm, store, claims)

        [record] = outcome["claims"]
        assert record["verdict"] == "not_enough_evidence"
        assert record["downgraded"] is True

    async def test_a_twice_failing_claim_is_a_failure_not_a_verdict(self, tmp_path):
        claims, store = await self.claims_pool(tmp_path)
        llm = FakeVerifierLLM().on("caffeine", LLMResponseInvalid("never parses"))
        outcome = await judge_claims_against_pool(llm, store, claims)

        assert outcome["judging_failures"] == 1
        assert outcome["judged"] == 0
        assert outcome["verdicts"] == {}
        assert formulas.external_support_rate(outcome["verdicts"]) is None  # not 0.0

    async def test_an_empty_pool_yields_not_enough_evidence_without_llm_calls(self, tmp_path):
        import uuid

        store = store_factory_for(tmp_path)("eval_empty_pool")
        llm = FakeVerifierLLM()  # would raise if consulted (no handler)
        outcome = await judge_claims_against_pool(
            llm, store, [{"claim_id": uuid.uuid4(), "text": "A true but unpoolable claim."}]
        )
        [record] = outcome["claims"]
        assert record["verdict"] == "not_enough_evidence"
        assert record["evidence_count"] == 0
        assert llm.calls_made == 0


class TestV2Formulas:
    def test_external_support_rate_hand_computed(self):
        verdicts = {"supported": 4, "partially_supported": 1, "contradicted": 1,
                    "not_enough_evidence": 4}
        assert formulas.external_support_rate(verdicts) == 0.5  # (4+1)/10
        assert formulas.external_support_rate({}) is None

    def test_external_contradiction_rate_hand_computed(self):
        verdicts = {"supported": 4, "contradicted": 2, "not_enough_evidence": 4}
        assert formulas.external_contradiction_rate(verdicts) == 0.2  # 2/10
        assert formulas.external_contradiction_rate({}) is None

    def test_v1_remains_untouched(self):
        assert formulas.METRICS_VERSION == "metrics-v1"
        assert formulas.METRICS_V2_VERSION == "metrics-v2"
        # The v1 definitions still produce their stage-16 hand-computed values.
        assert formulas.claim_support_rate(
            {"supported": 3, "partially_supported": 1, "unsupported": 2,
             "contradicted": 1, "not_enough_evidence": 1}
        ) == 0.5

    def test_terminology_guards_are_in_code_and_docs(self):
        """Safeguard 2: supportability is not factual accuracy; contradiction is
        not all hallucinations — stated where the formulas live."""
        assert "not" in formulas.external_support_rate.__doc__
        assert "factual accuracy" in formulas.external_support_rate.__doc__
        assert "not all hallucinations" in formulas.external_contradiction_rate.__doc__
        documentation = Path("../data/benchmarks/METRICS.md").read_text(encoding="utf-8")
        assert "not ground-truth factual accuracy" in documentation
        assert "never false" in documentation


class TestValidationCases:
    def test_the_labeled_cases_cover_every_required_category(self):
        payload = json.loads(
            Path("../data/benchmarks/verifier_validation.json").read_text(encoding="utf-8")
        )
        cases = payload["cases"]
        verdicts = {case["expected_verdict"] for case in cases}
        assert {"supported", "partially_supported", "unsupported", "contradicted",
                "not_enough_evidence"} == verdicts  # all five statuses labeled
        absent = [case for case in cases if "absent" in case["case_id"]]
        assert absent, "the true-but-absent-from-pool case must exist"
        assert absent[0]["expected_verdict"] == "not_enough_evidence"
        for case in cases:
            assert case["label_basis"]
            assert len(case["passages"]) >= 1

    def test_the_v2_failure_mode_anchors_exist(self):
        """Each judge failure mode measured under verification-v1 has a labeled
        case pinning the correct behaviour, plus the conflicting-vs-insufficient
        distinction and the previously uncovered unsupported verdict."""
        payload = json.loads(
            Path("../data/benchmarks/verifier_validation.json").read_text(encoding="utf-8")
        )
        ids = {case["case_id"] for case in payload["cases"]}
        for anchor in ("hedged-agreement", "range", "unit-mismatch", "date-mismatch",
                       "entity-swap", "causal", "compound-partial", "conflicting",
                       "unsupported"):
            assert any(anchor in case_id for case_id in ids), f"missing anchor: {anchor}"

    def test_acceptable_verdicts_are_wellformed_where_present(self):
        """acceptable_verdicts always contains the expected verdict and only real
        verdict values — a typo here would silently loosen the agreement measure."""
        payload = json.loads(
            Path("../data/benchmarks/verifier_validation.json").read_text(encoding="utf-8")
        )
        valid = {verdict.value for verdict in VerificationVerdict}
        for case in payload["cases"]:
            assert case["expected_verdict"] in valid
            acceptable = case.get("acceptable_verdicts")
            if acceptable is not None:
                assert case["expected_verdict"] in acceptable
                assert set(acceptable) <= valid
