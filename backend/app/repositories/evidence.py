"""
Repositories for the evidence layer.

`ClaimRepository.get_trace` and `get_verification_trace` are the reason this layer
exists rather than letting handlers write their own queries: each materialises one of
the traceability chains in a fixed number of round trips. Reconstructed ad hoc, those
chains are both slow (an N+1 per claim) and easy to get subtly wrong — and a wrong
provenance chain is worse than none, because it still looks authoritative.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.constants import CitationStatus, EvidenceRelation, VerificationVerdict
from app.db.base import utcnow
from app.models import (
    Citation,
    Claim,
    Conflict,
    Document,
    DocumentChunk,
    Evidence,
    Report,
    ResearchRun,
    Source,
    VerificationResult,
)
from app.repositories.base import Repository


class ReportRepository(Repository[Report]):
    model = Report

    def create(
        self,
        *,
        run: ResearchRun,
        title: str,
        markdown: str,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> Report:
        report = Report(
            research_run=run,
            title=title,
            markdown=markdown,
            word_count=len(markdown.split()),
            model=model,
            prompt_version=prompt_version,
            generated_at=utcnow(),
        )
        self.add(report)
        self.flush()
        return report

    def get_for_run(self, run_id: uuid.UUID) -> Report | None:
        return self.session.execute(
            select(Report).where(Report.research_run_id == run_id)
        ).scalar_one_or_none()


class DocumentRepository(Repository[Document]):
    model = Document

    def get_by_content(self, source_id: uuid.UUID, content_hash: str) -> Document | None:
        return self.session.execute(
            select(Document).where(
                Document.source_id == source_id, Document.content_hash == content_hash
            )
        ).scalar_one_or_none()

    def get_or_create(self, *, source: Source, text: str, **values) -> tuple[Document, bool]:
        """Create a document unless this exact text was already stored for the source.

        A re-fetch that changed nothing must not create a second row; a re-fetch that
        *did* change must, because a claim verified against the old text has to keep
        pointing at the words that supported it.
        """
        import hashlib

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.get_by_content(source.id, content_hash)
        if existing is not None:
            return existing, False

        document = Document(
            source=source,
            text=text,
            content_hash=content_hash,
            char_length=len(text),
            fetched_at=values.pop("fetched_at", utcnow()),
            **values,
        )
        self.add(document)
        self.flush()
        return document, True


class DocumentChunkRepository(Repository[DocumentChunk]):
    model = DocumentChunk

    def add_chunks(
        self, document: Document, spans: Sequence[tuple[int, int, str]]
    ) -> list[DocumentChunk]:
        """Persist chunks from `(start, end, text)` triples.

        Positions are assigned here so they cannot collide, and the offsets are
        stored as given — they are the foundation of every traceability claim, so
        they are never recomputed from the text.
        """
        import hashlib

        chunks = [
            DocumentChunk(
                document=document,
                position=index,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                start_char=start,
                end_char=end,
                created_at=utcnow(),
            )
            for index, (start, end, text) in enumerate(spans)
        ]
        self.session.add_all(chunks)
        return chunks

    def list_for_document(self, document_id: uuid.UUID) -> Sequence[DocumentChunk]:
        return (
            self.session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.position)
            )
            .scalars()
            .all()
        )


class ClaimRepository(Repository[Claim]):
    model = Claim

    def create(
        self,
        *,
        report: Report,
        position: int,
        text: str,
        requires_citation: bool = True,
        **values,
    ) -> Claim:
        claim = Claim(
            report=report,
            position=position,
            text=text,
            requires_citation=requires_citation,
            extracted_at=values.pop("extracted_at", utcnow()),
            **values,
        )
        self.add(claim)
        self.flush()
        return claim

    def list_for_report(self, report_id: uuid.UUID) -> Sequence[Claim]:
        return (
            self.session.execute(
                select(Claim).where(Claim.report_id == report_id).order_by(Claim.position)
            )
            .scalars()
            .all()
        )

    def get_trace(self, claim_id: uuid.UUID) -> Claim | None:
        """Chain 1, in one query plus its eager loads.

        Research -> Report -> Claim -> Citation -> Source, plus the chunk each
        citation points at and the document that chunk belongs to. Loading all of it
        eagerly is the difference between one trace being cheap and a page of traces
        being unusable.
        """
        return self.session.execute(
            select(Claim)
            .where(Claim.id == claim_id)
            .options(
                selectinload(Claim.report).selectinload(Report.research_run),
                selectinload(Claim.citations).selectinload(Citation.source),
                selectinload(Claim.evidence)
                .selectinload(Evidence.document_chunk)
                .selectinload(DocumentChunk.document)
                .selectinload(Document.source),
            )
        ).scalar_one_or_none()

    def get_verification_trace(self, claim_id: uuid.UUID) -> Claim | None:
        """Chain 2: Claim -> Evidence (all of it) -> VerificationResult.

        All evidence is loaded, not only the passage the verdict rests on. Showing
        just the supporting one would hide contradicting evidence that was weighed
        and rejected — precisely what a reader needs in order to disagree.
        """
        return self.session.execute(
            select(Claim)
            .where(Claim.id == claim_id)
            .options(
                selectinload(Claim.evidence).selectinload(Evidence.document_chunk),
                selectinload(Claim.verification_result),
            )
        ).scalar_one_or_none()

    def count_for_report(self, report_id: uuid.UUID) -> int:
        return self.session.execute(
            select(func.count()).select_from(Claim).where(Claim.report_id == report_id)
        ).scalar_one()

    def count_requiring_citation(self, report_id: uuid.UUID) -> int:
        """The denominator of citation recall.

        Separate from the total because definitions and framing sentences
        legitimately need no citation, and dividing by all claims would understate
        the result.
        """
        return self.session.execute(
            select(func.count())
            .select_from(Claim)
            .where(Claim.report_id == report_id, Claim.requires_citation.is_(True))
        ).scalar_one()


class EvidenceRepository(Repository[Evidence]):
    model = Evidence

    def create(
        self,
        *,
        claim: Claim,
        chunk: DocumentChunk,
        relation: EvidenceRelation = EvidenceRelation.NEUTRAL,
        **values,
    ) -> Evidence:
        evidence = Evidence(
            claim=claim,
            document_chunk=chunk,
            relation=relation,
            created_at=values.pop("created_at", utcnow()),
            **values,
        )
        self.add(evidence)
        self.flush()
        return evidence

    def list_for_claim(self, claim_id: uuid.UUID) -> Sequence[Evidence]:
        return (
            self.session.execute(
                select(Evidence).where(Evidence.claim_id == claim_id).order_by(Evidence.rank)
            )
            .scalars()
            .all()
        )


class CitationRepository(Repository[Citation]):
    model = Citation

    def create(self, *, claim: Claim, marker: str, status: CitationStatus, **values) -> Citation:
        citation = Citation(
            claim=claim,
            marker=marker,
            status=status,
            created_at=values.pop("created_at", utcnow()),
            **values,
        )
        self.add(citation)
        self.flush()
        return citation

    def count_by_status(self, report_id: uuid.UUID) -> dict[CitationStatus, int]:
        """Citation outcomes for a report, in one grouped query.

        The four statuses are counted separately because `fabricated` and
        `misattributed` are different defects — collapsing them would hide the more
        interesting one.
        """
        rows = self.session.execute(
            select(Citation.status, func.count())
            .join(Citation.claim)
            .where(Claim.report_id == report_id)
            .group_by(Citation.status)
        ).all()
        return {status: count for status, count in rows}


class VerificationResultRepository(Repository[VerificationResult]):
    model = VerificationResult

    def create(
        self,
        *,
        claim: Claim,
        verdict: VerificationVerdict,
        supporting_evidence: Evidence | None = None,
        **values,
    ) -> VerificationResult:
        """Record a verdict, applying the downgrade rule.

        ARCHITECTURE.md §7 records the mitigation for a hallucinating verifier: a
        verdict asserting support must quote the passage supporting it, and one
        without a quote becomes `not_enough_evidence`. Applied here as well as in
        the schema layer, because this path is reachable from a script that never
        touches HTTP — and `downgraded` is recorded rather than silently applied, so
        how often the verifier overclaims stays measurable.
        """
        from app.schemas.evidence import EVIDENCE_BEARING_VERDICTS

        downgraded = False
        if verdict in EVIDENCE_BEARING_VERDICTS and supporting_evidence is None:
            verdict = VerificationVerdict.NOT_ENOUGH_EVIDENCE
            downgraded = True

        result = VerificationResult(
            claim=claim,
            verdict=verdict,
            supporting_evidence=supporting_evidence,
            downgraded=downgraded or values.pop("downgraded", False),
            verified_at=values.pop("verified_at", utcnow()),
            **values,
        )
        self.add(result)
        self.flush()
        return result

    def get_for_claim(self, claim_id: uuid.UUID) -> VerificationResult | None:
        return self.session.execute(
            select(VerificationResult).where(VerificationResult.claim_id == claim_id)
        ).scalar_one_or_none()

    def count_by_verdict(self, report_id: uuid.UUID) -> dict[VerificationVerdict, int]:
        """Verdict distribution for a report. The input to the support and
        hallucination rates."""
        rows = self.session.execute(
            select(VerificationResult.verdict, func.count())
            .join(VerificationResult.claim)
            .where(Claim.report_id == report_id)
            .group_by(VerificationResult.verdict)
        ).all()
        return {verdict: count for verdict, count in rows}


class ConflictRepository(Repository[Conflict]):
    model = Conflict

    def create(
        self,
        *,
        run: ResearchRun,
        evidence_a: Evidence,
        evidence_b: Evidence,
        conflict_type,
        description: str,
        **values,
    ) -> Conflict:
        if evidence_a.id == evidence_b.id:
            raise ValueError("a passage cannot conflict with itself")
        conflict = Conflict(
            research_run=run,
            evidence_a_id=evidence_a.id,
            evidence_b_id=evidence_b.id,
            conflict_type=conflict_type,
            description=description,
            detected_at=values.pop("detected_at", utcnow()),
            **values,
        )
        self.add(conflict)
        self.flush()
        return conflict

    def list_for_run(self, run_id: uuid.UUID) -> Sequence[Conflict]:
        return (
            self.session.execute(select(Conflict).where(Conflict.research_run_id == run_id))
            .scalars()
            .all()
        )
