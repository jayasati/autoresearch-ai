"""
Builders for structurally valid rows under test.

**These are not fixtures of research data.** Nothing here ships, nothing is seeded,
and every value is a visible placeholder: topics read `<topic under test>`, claim
text reads `<claim N under test>`, and URLs use `example.invalid` -- a TLD reserved
by RFC 2606 that can never resolve. If any of this ever escaped into a screenshot
or a log it would be unmistakable, which is the point: the schema can be exercised
without producing anything that could pass for a real research result.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.constants import (
    CitationStatus,
    EvidenceDepth,
    EvidenceRelation,
    ResearchMode,
    RunStatus,
    SourceType,
    VerificationVerdict,
)
from app.models import (
    Citation,
    Claim,
    Document,
    DocumentChunk,
    Evidence,
    Report,
    ResearchRun,
    ResearchSource,
    RunConfiguration,
    Source,
    SubQuestion,
    VerificationResult,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

# RFC 2606 reserves .invalid: it is guaranteed never to resolve.
PLACEHOLDER_HOST = "https://example.invalid"


def make_configuration(mode: ResearchMode = ResearchMode.SEARCH_GROUNDED, **overrides):
    values = {
        "mode": mode,
        "model": "<model under test>",
        "temperature": 0.0,
        "max_subquestions": 6,
        "max_sources": 25,
    }
    if mode is not ResearchMode.MODEL_ONLY:
        values |= {
            "embedding_model": "<embedding model under test>",
            "chunk_size": 900,
            "chunk_overlap": 150,
            "top_k": 6,
        }
    values |= overrides
    return RunConfiguration(
        **values,
        fingerprint=RunConfiguration.compute_fingerprint(values),
        created_at=NOW,
    )


def make_run(configuration: RunConfiguration, **overrides) -> ResearchRun:
    topic = overrides.pop("topic", "<topic under test>")
    return ResearchRun(
        topic=topic,
        normalized_topic=ResearchRun.normalize_topic(topic),
        status=overrides.pop("status", RunStatus.COMPLETED),
        configuration=configuration,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=30),
        created_at=NOW,
        updated_at=NOW,
        **overrides,
    )


def make_subquestion(run: ResearchRun, position: int = 0, **overrides) -> SubQuestion:
    return SubQuestion(
        research_run=run,
        position=position,
        text=f"<sub-question {position} under test>",
        created_at=NOW,
        **overrides,
    )


def make_source(**overrides) -> Source:
    source_type = overrides.pop("source_type", SourceType.ACADEMIC)
    defaults: dict = {"title": "<source title under test>", "first_retrieved_at": NOW}
    if source_type is SourceType.ACADEMIC:
        defaults |= {
            "doi": overrides.pop("doi", f"10.0000/invalid.{uuid.uuid4().hex[:8]}"),
            "venue": "<venue under test>",
            "publication_year": 2025,
        }
    elif source_type is SourceType.WEB:
        defaults |= {"url": overrides.pop("url", f"{PLACEHOLDER_HOST}/{uuid.uuid4().hex[:8]}")}
    defaults |= overrides
    basis = defaults.get("doi") or defaults.get("url") or uuid.uuid4().hex
    return Source(
        source_type=source_type,
        fingerprint=uuid.uuid5(uuid.NAMESPACE_URL, basis).hex,
        created_at=NOW,
        updated_at=NOW,
        **defaults,
    )


def link_source(run: ResearchRun, source: Source, label: str = "S1", **overrides):
    return ResearchSource(
        research_run=run,
        source=source,
        citation_label=label,
        evidence_depth=overrides.pop("evidence_depth", EvidenceDepth.FULL_TEXT),
        retriever="<retriever under test>",
        rank=overrides.pop("rank", 0),
        created_at=NOW,
        **overrides,
    )


def make_document(source: Source, text: str = "<document text under test>") -> Document:
    return Document(
        source=source,
        text=text,
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, text).hex,
        char_length=len(text),
        depth=EvidenceDepth.FULL_TEXT,
        extractor="<extractor under test>",
        fetched_at=NOW,
    )


def make_chunk(document: Document, position: int = 0, text: str | None = None) -> DocumentChunk:
    text = text or f"<chunk {position} under test>"
    start = position * 1000
    return DocumentChunk(
        document=document,
        position=position,
        text=text,
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, f"{position}:{text}").hex,
        start_char=start,
        end_char=start + len(text),
        created_at=NOW,
    )


def make_report(run: ResearchRun, **overrides) -> Report:
    markdown = overrides.pop("markdown", "<report markdown under test>")
    return Report(
        research_run=run,
        title="<report title under test>",
        markdown=markdown,
        word_count=len(markdown.split()),
        generated_at=NOW,
        **overrides,
    )


def make_claim(report: Report, position: int = 0, **overrides) -> Claim:
    return Claim(
        report=report,
        position=position,
        text=f"<claim {position} under test>",
        requires_citation=overrides.pop("requires_citation", True),
        extracted_at=NOW,
        **overrides,
    )


def make_evidence(
    claim: Claim,
    chunk: DocumentChunk,
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
    **overrides,
) -> Evidence:
    quoted = overrides.pop("quoted_text", "<quoted span under test>")
    return Evidence(
        claim=claim,
        document_chunk=chunk,
        relation=relation,
        similarity=overrides.pop("similarity", 0.5),
        rank=overrides.pop("rank", 0),
        quoted_text=quoted,
        quote_start_char=0,
        quote_end_char=len(quoted),
        created_at=NOW,
        **overrides,
    )


def make_citation(
    claim: Claim,
    source: Source | None,
    marker: str = "S1",
    status: CitationStatus = CitationStatus.VALID,
    chunk: DocumentChunk | None = None,
    **overrides,
) -> Citation:
    if status is CitationStatus.VALID:
        overrides.setdefault("supporting_chunk_id", chunk.id if chunk else None)
    else:
        overrides.setdefault("failure_reason", f"<reason: {status.value}>")
    return Citation(
        claim=claim,
        source=source,
        marker=marker,
        status=status,
        validated_at=NOW,
        created_at=NOW,
        **overrides,
    )


def make_verification(
    claim: Claim,
    verdict: VerificationVerdict = VerificationVerdict.SUPPORTED,
    evidence: Evidence | None = None,
    **overrides,
) -> VerificationResult:
    return VerificationResult(
        claim=claim,
        verdict=verdict,
        confidence=overrides.pop("confidence", 0.9),
        supporting_evidence=evidence,
        rationale="<rationale under test>",
        verified_at=NOW,
        **overrides,
    )
