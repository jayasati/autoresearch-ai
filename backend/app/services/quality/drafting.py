"""
The structured draft — design stage 10a (stage 31, S4).

    plan + evidence ──LLM (synthesis-structured-v1)──▶ StructuredReport
                    ──code──▶ validated: unknown sections → SYNTHESIS, claim/finding
                              aliases stripped (a draft has none), citation markers
                              and leading heading markup removed from model text,
                              missing/repeated subquestion sections warned about,
                              unknown passage aliases recorded and never rendered
                    ──code──▶ markdown: [S#] rendered from E# via the alias table,
                              Sources list appended from stored provider metadata

Failure policy (approved): invalid output is retried once, then the run fails with
`draft_invalid` — there is no fallback to the legacy markdown prompt, so a
report-quality run never contains a legacy draft. A draft citing no valid passage is
retried once with a reminder, then fails with the existing `UncitedReportError`.

The model never writes a citation marker or a source's metadata: markers come from
the passages it names, and the Sources list from the stored source rows (the legacy
formatter, reused unchanged).
"""

import re
from dataclasses import dataclass, field

from app.core.exceptions import AppError
from app.services.llm import LLMResponseInvalid
from app.services.orchestration.exceptions import UncitedReportError
from app.services.orchestration.orchestrator import ResearchOrchestrator
from app.services.quality.alignment import DraftSentence
from app.services.quality.evidence import EvidenceSet
from app.services.quality.models import ReportSectionDraft, StructuredReport
from app.services.quality.planner import PlanResult
from app.services.quality.prompts import (
    DRAFT_PROMPT_VERSION,
    DRAFT_RETRY_INVALID,
    DRAFT_RETRY_UNCITED,
    DRAFT_SYSTEM,
    build_draft_prompt,
)
from app.services.quality.rendering import render_draft

SYNTHESIS = "SYNTHESIS"


class DraftInvalid(AppError):
    """The structured draft failed schema validation twice."""

    status_code = 502
    code = "draft_invalid"
    message = "The model did not return a valid structured draft after one retry."


@dataclass
class DraftResult:
    report: StructuredReport
    markdown: str
    model: str
    llm_calls: int
    prompt_version: str = DRAFT_PROMPT_VERSION
    rejected_aliases: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def sentences(self, evidence: EvidenceSet) -> list[DraftSentence]:
        """Every draft sentence in reading order, with the S# markers rendered on it —
        the alignment's view of the draft."""
        result: list[DraftSentence] = []
        for section in self.report.sections:
            for sentence in section.sentences:
                markers = {
                    evidence.aliases.passages[alias].source_label
                    for alias in sentence.evidence_ids
                    if alias in evidence.aliases.passages
                }
                result.append(DraftSentence(len(result), sentence.text, frozenset(markers)))
        return result

    def as_record(self) -> dict:
        return {
            "prompt_version": self.prompt_version,
            "model": self.model,
            "report": self.report.model_dump(),
            "rejected_aliases": self.rejected_aliases,
            "warnings": self.warnings,
        }


def evidence_blocks(evidence: EvidenceSet) -> list[str]:
    """Each selected passage, headed by its alias and the provider-reported facts the
    model may read (never write): source label, title, type, year, evidence depth."""
    blocks = []
    for alias, chunk in evidence.chunks.items():
        label = evidence.aliases.passages[alias].source_label
        source = chunk.source
        facts = [source.provider or source.source_type.value]
        if source.year is not None:
            facts.append(str(source.year))
        title = source.title or source.url or "untitled source"
        blocks.append(
            f"[{alias}] ({label}: {title}; {', '.join(facts)}; evidence: {chunk.depth.value})"
            f"\n{chunk.text}"
        )
    return blocks


def plan_lines(plan: PlanResult, evidence: EvidenceSet) -> list[str]:
    per = {s.alias: s for s in evidence.per_subquestion}
    lines = []
    for sq in plan.subquestions:
        selected = per[sq.alias].passages
        tail = f"passages {', '.join(selected)}" if selected else "no evidence retrieved"
        role = "core" if sq.is_core else "supporting"
        lines.append(f"{sq.alias} ({role}): {sq.text} — {tail}")
    return lines


#: Anything that reads as a citation marker in model-written text ([S3], [ E2 ], [s1]).
#: Markers are rendered by code from evidence_ids only, so text must never carry one.
_MARKER = re.compile(r"\[\s*[SE]\d+\s*\]", re.IGNORECASE)


def _plain(text: str, *, sentence: bool = False) -> str:
    """Model text with citation markers removed; a sentence also loses leading '#',
    which would otherwise start a markdown heading and shift the report's sections."""
    cleaned = " ".join(_MARKER.sub(" ", text).split())
    if sentence:
        cleaned = cleaned.lstrip("#").strip()
    return cleaned


def _validated(
    report: StructuredReport, plan: PlanResult, warnings: list[str], *, allow_claims: bool = False
) -> StructuredReport:
    """Normalise model output. A draft carries no claim or finding aliases (stripped);
    a revision (stage 33, S5b) keeps its claim aliases for validation to
    check. Marker and heading sanitisation applies to both."""
    known = {sq.alias for sq in plan.subquestions}
    sanitized = 0
    sections = []
    for section in report.sections:
        subquestion = section.subquestion
        if subquestion not in known and subquestion != SYNTHESIS:
            warnings.append(
                f"Section {section.heading!r} named unknown {subquestion!r}; treated as SYNTHESIS."
            )
            subquestion = SYNTHESIS
        sentences = []
        for sentence in section.sentences:
            if not allow_claims and (sentence.claim_ids or sentence.finding_refs):
                warnings.append("A draft sentence carried claim or finding aliases; stripped.")
                sentence = sentence.model_copy(update={"claim_ids": [], "finding_refs": []})
            text = _plain(sentence.text, sentence=True)
            if text != sentence.text:
                sanitized += 1
                if not text:
                    warnings.append("A draft sentence held only markup; dropped.")
                    continue
                sentence = sentence.model_copy(update={"text": text})
            sentences.append(sentence)
        heading = _plain(section.heading) or "Findings"
        sanitized += heading != section.heading
        sections.append(
            ReportSectionDraft(subquestion=subquestion, heading=heading, sentences=sentences)
        )
    title = _plain(report.title) or "Research report"
    sanitized += title != report.title
    if sanitized:
        warnings.append(
            f"Removed citation markers or heading markup from {sanitized} piece(s) of "
            "model-written text; markers are rendered from evidence ids only."
        )
    present = [s.subquestion for s in sections if s.subquestion != SYNTHESIS]
    missing = [alias for alias in sorted(known) if alias not in present]
    repeated = sorted({alias for alias in present if present.count(alias) > 1})
    if missing:
        warnings.append(f"The draft has no section for planned subquestion(s) {missing}.")
    if repeated:
        warnings.append(f"The draft has more than one section for {repeated}.")
    return StructuredReport(title=title, sections=sections)


def _cites_valid_passage(report: StructuredReport, evidence: EvidenceSet) -> bool:
    return any(
        alias in evidence.aliases.passages
        for section in report.sections
        for sentence in section.sentences
        for alias in sentence.evidence_ids
    )


async def write_draft(
    llm, topic: str, plan: PlanResult, evidence: EvidenceSet, *, max_tokens: int
) -> DraftResult:
    warnings: list[str] = []
    prompt = build_draft_prompt(topic, plan_lines(plan, evidence), evidence_blocks(evidence))
    calls = 0
    response = None
    for suffix in ("", DRAFT_RETRY_INVALID):
        calls += 1
        try:
            response = await llm.generate_structured_output(
                prompt + suffix, StructuredReport, system=DRAFT_SYSTEM, max_tokens=max_tokens
            )
            break
        except LLMResponseInvalid as exc:
            warnings.append(f"Draft output invalid (attempt {calls}): {exc.message}")
    if response is None:
        raise DraftInvalid(details={"attempts": calls})

    report = _validated(response.data, plan, warnings)
    if not _cites_valid_passage(report, evidence):
        warnings.append("The first draft cited no valid passage; retried once.")
        calls += 1
        try:
            response = await llm.generate_structured_output(
                prompt + DRAFT_RETRY_UNCITED,
                StructuredReport,
                system=DRAFT_SYSTEM,
                max_tokens=max_tokens,
            )
        except LLMResponseInvalid as exc:
            raise DraftInvalid(details={"attempts": calls}) from exc
        report = _validated(response.data, plan, warnings)
        if not _cites_valid_passage(report, evidence):
            raise UncitedReportError(details={"attempts": calls})

    markdown, rejected = render_draft(report, evidence.aliases)
    if rejected:
        warnings.append(
            f"The draft named passages that do not exist: {sorted(set(rejected))}; "
            "they were not rendered."
        )
    meta_by_fp = {chunk.source.fingerprint: chunk.source for chunk in evidence.chunks.values()}
    markdown = ResearchOrchestrator._with_sources_section(markdown, evidence.labels, meta_by_fp)
    return DraftResult(
        report,
        markdown,
        response.model,
        calls,
        rejected_aliases=sorted(set(rejected)),
        warnings=warnings,
    )
