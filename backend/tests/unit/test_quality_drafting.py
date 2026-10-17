"""report-quality-v1 stage 10a, 6 and alignment (stage 31, S4).

The fake LLM answers by schema and reads the real prompt it is given: the draft is
built from the plan lines and passage aliases the pipeline sent, and the claims
from the rendered report the (unmodified) extractor sent. No network, no tokens;
throwaway SQLite.
"""

import re

import pytest
from sqlalchemy import select

from app.core.constants import ClaimImportance, ClaimType, ReportQualityStatus, RunStatus
from app.models import Report, ReportQualityAudit, ResearchRun
from app.services.claims import ClaimExtractionModel
from app.services.llm import LLMResponseInvalid
from app.services.orchestration.exceptions import UncitedReportError
from app.services.quality.drafting import DraftInvalid, write_draft
from app.services.quality.models import CoveragePlan, StructuredReport
from app.services.quality.prompts import DRAFT_PROMPT_VERSION
from app.services.research_service import ResearchService
from tests.unit.test_quality_pipeline import PLAN, pipeline, request

PLAN_LINE = re.compile(
    r"^(Q\d+) \((?:core|supporting)\): (.+?) — (?:passages (.+)|no evidence)", re.M
)


def draft_from_prompt(prompt: str, *, cite=True, extra=None) -> StructuredReport:
    sections = []
    for alias, text, passages in PLAN_LINE.findall(prompt):
        evidence = [p.strip() for p in passages.split(",")] if passages else []
        if evidence:
            sentences = [
                {
                    "text": f"The evidence on {text.rstrip('?').lower()} is reported here.",
                    "kind": "finding",
                    "claim_ids": [],
                    "finding_refs": [],
                    "evidence_ids": evidence[:1] if cite else [],
                }
            ]
        else:
            sentences = [
                {
                    "text": "The retrieved evidence does not address this.",
                    "kind": "uncertainty",
                    "claim_ids": [],
                    "finding_refs": [],
                    "evidence_ids": [],
                }
            ]
        sections.append({"subquestion": alias, "heading": f"## {text}", "sentences": sentences})
    if extra:
        sections.append(extra)
    return StructuredReport.model_validate({"title": "Coffee and health", "sections": sections})


def claims_from_prompt(prompt: str) -> ClaimExtractionModel:
    # Body lines only (headings are not claims), one claim per cited sentence — as a real
    # extractor would. (Fixed in S5b: the earlier pattern ran across heading lines.)
    report = prompt.split("Report:", 1)[1].split("Extract the claims now.")[0]
    body = " ".join(
        line
        for line in report.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-", "---"))
    )
    claims = []
    for sentence, markers in re.findall(r"([A-Z][^.\[]*?) ((?:\[S\d+\])+)\.", body):
        claims.append(
            {
                "claim_text": sentence.strip() + ".",
                "claim_type": ClaimType.FACTUAL,
                "importance": ClaimImportance.HIGH,
                "section": None,
                "citation_labels": re.findall(r"S\d+", markers),
                "requires_citation": True,
            }
        )
    return ClaimExtractionModel.model_validate({"claims": claims})


class Response:
    def __init__(self, data, text=""):
        self.data = data
        self.model = "fake-model-2026"
        self.text = text
        self.finish_reason = "stop"
        self.usage = None


class SchemaLLM:
    """Answers by schema; `draft` may be a list of outcomes (exceptions or kwargs)."""

    def __init__(self, draft=None, claims=None):
        self.draft = list(draft or [{}])
        self.claims = claims  # optional callable(prompt) -> ClaimExtractionModel | Exception
        self.calls: list[str] = []
        self.calls_made = 0
        from app.services.llm import TokenUsage

        self.total_usage = TokenUsage.zero()

    async def generate_structured_output(self, prompt, schema, *, system=None, **_):
        self.calls.append(schema.__name__)
        self.calls_made += 1
        if schema is CoveragePlan:
            return Response(PLAN)
        if schema is StructuredReport:
            outcome = self.draft.pop(0) if len(self.draft) > 1 else self.draft[0]
            if isinstance(outcome, Exception):
                raise outcome
            if callable(outcome):
                return Response(outcome(prompt))
            return Response(draft_from_prompt(prompt, **outcome))
        if schema is ClaimExtractionModel:
            if self.claims is not None:
                result = self.claims(prompt)
                if isinstance(result, Exception):
                    raise result
                return Response(result)
            return Response(claims_from_prompt(prompt))
        raise AssertionError(f"unexpected schema {schema}")


async def drafted(service_session, llm=None):
    llm = llm or SchemaLLM()
    quality = pipeline(llm=llm)
    prepared = await quality.prepare_evidence(request())
    return quality, prepared, await quality.draft_and_align(prepared)


class TestDraftAndAlign:
    async def test_draft_is_stored_rendered_from_aliases_and_aligned(self, service_session):
        _, prepared, result = await drafted(service_session)
        run = ResearchService().get_run(prepared.research_id)
        report = service_session.get(Report, result.report_id)
        assert run.status is RunStatus.DRAFTING  # verification is S5
        assert report.prompt_version == DRAFT_PROMPT_VERSION

        # every rendered marker resolves to a source this run labelled and stored
        stored = {link.citation_label for link in run.research_sources if link.citation_label}
        body = report.markdown.split("## Sources")[0]
        markers = set(re.findall(r"\[(S\d+)\]", body))
        assert markers and markers <= stored
        assert markers <= set(result.aliases.sources)

        # sections are linked to the subquestions they answer; the Sources list is not
        sections = sorted(report.sections, key=lambda s: s.position)
        assert [s.subquestion_id for s in sections[:-1]] == [
            result.aliases.subquestions[s.subquestion] for s in result.draft.report.sections
        ]
        assert sections[-1].heading == "Sources" and sections[-1].subquestion_id is None

        # the existing extractor ran, and each cited claim aligned to its sentence
        assert result.aliases.claims and result.extractor_model == "fake-model-2026"
        assert all(a.status == "aligned" for a in result.alignments.values())

    async def test_the_audit_row_holds_the_internal_record_not_the_report(self, service_session):
        _, _, result = await drafted(service_session)
        [audit] = service_session.execute(select(ReportQualityAudit)).scalars()
        assert audit.report_id == result.report_id
        assert audit.quality_status is ReportQualityStatus.RUNNING and audit.stage == "aligned"
        assert set(audit.payload) >= {"plan", "evidence", "draft", "aliases", "alignments"}
        report = service_session.get(Report, result.report_id)
        assert "alignments" not in report.markdown and "E1" not in report.markdown

    async def test_an_invalid_draft_twice_fails_the_run_without_a_legacy_fallback(
        self, service_session
    ):
        llm = SchemaLLM(draft=[LLMResponseInvalid(), LLMResponseInvalid()])
        with pytest.raises(DraftInvalid):
            await drafted(service_session, llm)
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.FAILED and run.error_code == "draft_invalid"
        assert service_session.execute(select(Report)).first() is None
        assert "StructuredReport" in llm.calls and llm.calls.count("StructuredReport") == 2

    async def test_an_uncited_draft_is_retried_then_fails(self, service_session):
        llm = SchemaLLM(draft=[{"cite": False}, {"cite": False}])
        with pytest.raises(UncitedReportError):
            await drafted(service_session, llm)
        assert llm.calls.count("StructuredReport") == 2  # exactly one retry

    async def test_a_retry_that_cites_is_used(self, service_session):
        llm = SchemaLLM(draft=[{"cite": False}, {}])
        _, _, result = await drafted(service_session, llm)
        assert any("cited no valid passage" in w for w in result.warnings)

    async def test_the_gate_also_guards_drafting(self, service_session):
        quality, prepared, _ = await drafted(service_session)
        quality.settings = quality.settings.model_copy(update={"REPORT_QUALITY_ENABLED": False})
        from app.services.quality.exceptions import ReportQualityDisabled

        with pytest.raises(ReportQualityDisabled):
            await quality.draft_and_align(prepared)


class TestDraftValidation:
    async def prepared(self, service_session):
        quality = pipeline(llm=SchemaLLM())
        return await quality.prepare_evidence(request())

    async def test_unknown_aliases_are_never_rendered_and_sections_are_normalised(
        self, service_session
    ):
        prepared = await self.prepared(service_session)
        extra = {
            "subquestion": "Q99",
            "heading": "Elsewhere",
            "sentences": [
                {
                    "text": "An invented point.",
                    "kind": "finding",
                    "claim_ids": ["C1"],
                    "finding_refs": ["F1"],
                    "evidence_ids": ["E1", "E77"],
                }
            ],
        }
        llm = SchemaLLM(draft=[{"extra": extra}])
        result = await write_draft(
            llm, prepared.topic, prepared.plan, prepared.evidence, max_tokens=1400
        )
        assert result.rejected_aliases == ["E77"]
        assert "[S" in result.markdown and "E77" not in result.markdown
        last = result.report.sections[-1]
        assert last.subquestion == "SYNTHESIS"
        assert last.sentences[0].claim_ids == [] and last.sentences[0].finding_refs == []
        assert any("unknown 'Q99'" in w for w in result.warnings)
        assert result.markdown.rstrip().split("\n")[-1].startswith("- **[S")  # Sources list

    async def test_draft_sentences_carry_their_rendered_markers(self, service_session):
        prepared = await self.prepared(service_session)
        result = await write_draft(
            SchemaLLM(), prepared.topic, prepared.plan, prepared.evidence, max_tokens=1400
        )
        sentences = result.sentences(prepared.evidence)
        assert [s.index for s in sentences] == list(range(len(sentences)))
        cited = [s for s in sentences if s.markers]
        assert cited and all(
            m in prepared.evidence.aliases.sources for s in cited for m in s.markers
        )


def hostile_draft(prompt: str) -> StructuredReport:
    """A draft whose model-written text tries to carry its own citations and headings."""
    plan = PLAN_LINE.findall(prompt)
    first_passage = plan[0][2].split(",")[0].strip()
    return StructuredReport.model_validate(
        {
            "title": "Coffee [S9] and health",
            "sections": [
                {
                    "subquestion": plan[0][0],
                    "heading": "Diabetes [S7]",
                    "sentences": [
                        {
                            "text": "## Fake heading that would shift sections",
                            "kind": "finding",
                            "claim_ids": [],
                            "finding_refs": [],
                            "evidence_ids": [first_passage],
                        },
                        {
                            "text": "An unsupported point [S1] with a fake source [s9].",
                            "kind": "finding",
                            "claim_ids": [],
                            "finding_refs": [],
                            "evidence_ids": [],
                        },
                        {
                            "text": "[S2]",
                            "kind": "transition",
                            "claim_ids": [],
                            "finding_refs": [],
                            "evidence_ids": [],
                        },
                    ],
                },
                {
                    "subquestion": plan[1][0],
                    "heading": "Blood pressure",
                    "sentences": [
                        {
                            "text": "Pressure rose slightly.",
                            "kind": "finding",
                            "claim_ids": [],
                            "finding_refs": [],
                            "evidence_ids": [plan[1][2].split(",")[0].strip()],
                        },
                    ],
                },
                {"subquestion": plan[1][0], "heading": "Blood pressure again", "sentences": []},
            ],
        }
    )


class TestModelTextCannotForgeCitationsOrStructure:
    """Regression for two S4 review findings: model-written text could (1) render its
    own citation markers, real or fake, and (2) start a markdown heading that shifted
    every section link after it."""

    async def test_only_evidence_derived_markers_render_and_sections_link_correctly(
        self, service_session
    ):
        _, prepared, result = await drafted(service_session, SchemaLLM(draft=[hostile_draft]))
        report = service_session.get(Report, result.report_id)
        body = report.markdown.split("## Sources")[0]

        # (1) no forged markers in title, heading or text; each rendered marker is the
        # label of the passage the sentence names
        assert "[S9]" not in body and "[s9]" not in body and "[S7]" not in body
        expected = {
            prepared.evidence.aliases.passages[alias].source_label
            for section in result.draft.report.sections
            for sentence in section.sentences
            for alias in sentence.evidence_ids
        }
        assert set(re.findall(r"\[(S\d+)\]", body)) == expected
        assert "An unsupported point with a fake source ." in body  # text kept, marks gone
        assert all(s.text != "" for sec in result.draft.report.sections for s in sec.sentences)

        # (2) no stray heading: stored sections are exactly the draft's, links correct
        sections = sorted(report.sections, key=lambda s: s.position)
        assert [s.heading for s in sections] == [
            *(s.heading for s in result.draft.report.sections),
            "Sources",
        ]
        assert [s.subquestion_id for s in sections[:-1]] == [
            result.aliases.subquestions[s.subquestion] for s in result.draft.report.sections
        ]

        # the extractor saw no forged citation: a fake [S9] would be stored as fabricated
        from app.core.constants import CitationStatus
        from app.models import Citation, Claim

        statuses = (
            service_session.execute(
                select(Citation.status).join(Claim).where(Claim.report_id == result.report_id)
            )
            .scalars()
            .all()
        )
        assert statuses and CitationStatus.FABRICATED not in statuses
        warnings = " ".join(result.warnings)
        assert "Removed citation markers or heading markup" in warnings
        assert "held only markup; dropped" in warnings
        assert "no section for planned subquestion(s) ['Q3']" in warnings
        assert "more than one section for ['Q2']" in warnings

    def test_the_store_refuses_to_link_sections_that_do_not_match(self, service_session):
        from app.core.exceptions import ValidationError
        from app.services.quality.store import QualityStore
        from tests.integration.builders import make_configuration, make_report, make_run

        run = make_run(make_configuration())
        report = make_report(run, markdown="# T\n\n## One\n\nx\n\n## Two\n\ny\n")
        service_session.add_all([run, report])
        service_session.commit()
        ResearchService()  # the factory is the throwaway database
        from app.models import ReportSection

        for position, heading in enumerate(["One", "Injected", "Two"]):
            service_session.add(
                ReportSection(
                    report=report,
                    position=position,
                    heading=heading,
                    start_char=position,
                    end_char=position + 1,
                )
            )
        service_session.commit()
        with pytest.raises(ValidationError, match="do not match"):
            QualityStore().link_sections(report.id, [None, None], ["One", "Two"])


class TestFailureAfterTheDraftIsStored:
    async def test_an_extraction_failure_fails_the_run_and_keeps_the_draft(self, service_session):
        llm = SchemaLLM(claims=lambda prompt: LLMResponseInvalid())
        with pytest.raises(Exception, match="extract|claim|valid"):
            await drafted(service_session, llm)
        [run] = service_session.execute(select(ResearchRun)).scalars()
        service_session.refresh(run)
        assert run.status is RunStatus.FAILED and run.error_code
        assert service_session.execute(select(Report)).first() is not None  # draft kept
        assert service_session.execute(select(ReportQualityAudit)).first() is None


class TestUnalignedClaims:
    async def test_an_unalignable_claim_is_warned_about_and_recorded(self, service_session):
        def claims(prompt):
            model = claims_from_prompt(prompt)
            orphan = model.claims[0].model_copy(
                update={
                    "claim_text": "A claim citing a source no sentence cites.",
                    "citation_labels": ["S99"],
                }
            )
            return ClaimExtractionModel(claims=[*model.claims, orphan])

        _, _, result = await drafted(service_session, SchemaLLM(claims=claims))
        unaligned = [a for a, r in result.alignments.items() if r.status == "unaligned"]
        assert unaligned and all(result.alignments[a].reason == "no_candidate" for a in unaligned)
        assert any("will not get a citation check" in w for w in result.warnings)
        [audit] = service_session.execute(select(ReportQualityAudit)).scalars()
        assert {
            a for a, r in audit.payload["alignments"].items() if r["status"] == "unaligned"
        } == set(unaligned)
