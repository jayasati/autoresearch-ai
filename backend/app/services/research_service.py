"""
The research service: use cases, and the transaction boundary.

This is the layer that answers "what happened, and did all of it happen". Each
public method is one atomic operation: it either completes and commits, or raises and
leaves the database exactly as it was.

Why that boundary lives here and not lower down: a research run produces a connected
graph — a report, its sections, its claims, each claim's evidence, citations and
verdict. Persisting that in pieces, each committed as it is built, means a failure
partway through leaves a run whose claim set is silently incomplete. Every metric
computed over it would then be wrong in a way that looks like a finding. One
transaction per operation makes partial state impossible rather than unlikely.

No LLM calls and no retrieval here — this service persists what those stages will
eventually produce.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.constants import (
    CitationStatus,
    EvidenceDepth,
    EvidenceRelation,
    ResearchMode,
    RunStatus,
    VerificationVerdict,
)
from app.core.exceptions import ConflictError, NotFoundError
from app.db.base import utcnow
from app.db.session import unit_of_work
from app.models import (
    Conflict,
    Document,
    DocumentChunk,
    Evidence,
    ResearchRun,
    RunConfiguration,
)
from app.models.report import ReportSection
from app.repositories import (
    BenchmarkRunRepository,
    CitationRepository,
    ClaimRepository,
    ConflictRepository,
    DocumentChunkRepository,
    DocumentRepository,
    EvidenceRepository,
    ReportRepository,
    ResearchRunRepository,
    RunConfigurationRepository,
    SourceRepository,
    VerificationResultRepository,
)
from app.schemas.research import ResearchRequest

logger = logging.getLogger(__name__)


def parse_sections(markdown: str) -> list[tuple[str, int, int]]:
    """`(heading, start_char, end_char)` for each `##` section of a report.

    A section runs from its heading line to the next `##` heading (or the end), so
    the spans tile the body and a character position maps to at most one section.
    Text before the first `##` belongs to no section, like a claim in a preamble.
    """
    sections: list[tuple[str, int, int]] = []
    offset = 0
    current: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("###"):
            if current is not None:
                sections.append((current[0], current[1], offset))
            current = (stripped[3:].strip(), offset)
        offset += len(line)
    if current is not None:
        sections.append((current[0], current[1], len(markdown)))
    return [section for section in sections if section[0]]


class ResearchService:
    """Use cases over the research graph.

    Constructed with a session when joining a caller's transaction, or without one
    to manage its own. Every write method is wrapped in `unit_of_work`, which joins
    an existing transaction rather than opening a second — so a service method is
    safe to call standalone *or* as one step of a larger operation, and in the
    second case it does not commit work the caller may still abandon.
    """

    def __init__(self, session: Session | None = None, settings: Settings | None = None) -> None:
        self._session = session
        self.settings = settings or get_settings()

    # --- configuration -------------------------------------------------------

    def configuration_values_for(self, request: ResearchRequest) -> dict:
        """Turn a request plus the current settings into a configuration.

        Retrieval settings are included only for the modes that retrieve. A chunk
        size on a `model_only` run would imply retrieval that never happened, and
        would make the fingerprint — and therefore comparability — depend on
        settings that never applied.
        """
        values: dict = {
            "mode": request.mode,
            "model": self.settings.OPENAI_MODEL,
            "temperature": 0.0,
            "max_subquestions": request.max_subquestions or self.settings.MAX_SUBQUESTIONS,
            "max_sources": request.max_sources or self.settings.MAX_SOURCES_PER_RUN,
        }
        if request.mode is not ResearchMode.MODEL_ONLY:
            values |= {
                "embedding_model": self.settings.EMBEDDING_MODEL,
                "chunk_size": self.settings.CHUNK_SIZE,
                "chunk_overlap": self.settings.CHUNK_OVERLAP,
                "top_k": self.settings.TOP_K,
            }
        return values

    def resolve_configuration(
        self, request: ResearchRequest, *, session: Session | None = None
    ) -> tuple[RunConfiguration, bool]:
        """Find or create the configuration for a request."""
        with unit_of_work(session or self._session) as active:
            return RunConfigurationRepository(active).get_or_create(
                **self.configuration_values_for(request)
            )

    # --- creating a run ------------------------------------------------------

    def create_run(self, request: ResearchRequest) -> ResearchRun:
        """Create a research run in `pending`, with its configuration and plan slot.

        One transaction covering both the configuration and the run. Committing the
        configuration separately would leave an orphan settings row behind whenever
        run creation failed — harmless individually, but it accumulates and makes
        "which configurations has this project actually used" unanswerable.
        """
        values = self.configuration_values_for(request)

        with unit_of_work(self._session) as session:
            configurations = RunConfigurationRepository(session)
            runs = ResearchRunRepository(session)

            configuration, created = configurations.get_or_create(**values)

            benchmark = None
            if request.benchmark_run_id is not None:
                benchmark = BenchmarkRunRepository(session).get(request.benchmark_run_id)
                if benchmark is None:
                    raise NotFoundError(
                        f"No benchmark_run with id {request.benchmark_run_id}.",
                        details={"benchmark_run_id": str(request.benchmark_run_id)},
                    )

            run = runs.create(
                topic=request.topic, configuration=configuration, benchmark_run=benchmark
            )

            logger.info(
                "Created research run %s (mode=%s, configuration=%s%s)",
                run.id,
                configuration.mode.value,
                configuration.fingerprint[:12],
                ", new" if created else "",
            )
            return run

    # --- reading -------------------------------------------------------------

    def get_run(self, run_id: uuid.UUID) -> ResearchRun:
        """A run with its configuration, plan, sources and report loaded."""
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_with_relations(run_id)
            if run is None:
                raise NotFoundError(
                    f"No research run with id {run_id}.", details={"run_id": str(run_id)}
                )
            return run

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> Sequence[ResearchRun]:
        with unit_of_work(self._session) as session:
            return ResearchRunRepository(session).list_recent(limit=limit, offset=offset)

    def run_summary(self, run_id: uuid.UUID) -> dict:
        """Counts for the response wrapper, computed rather than cached.

        Cached counters drift the moment anything writes outside the service; a
        count over the rows cannot.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_with_relations(run_id)
            if run is None:
                raise NotFoundError(f"No research run with id {run_id}.")
            report = ReportRepository(session).get_for_run(run_id)
            claim_count = (
                ClaimRepository(session).count_for_report(report.id) if report else 0
            )
            return {
                "report_id": report.id if report else None,
                "claim_count": claim_count,
                "source_count": len(run.research_sources),
                "conflict_count": len(run.conflicts),
            }

    # --- status transitions --------------------------------------------------

    # Which transitions are legal. A run cannot go back to pending, and a completed
    # or failed run is final: re-running a topic creates a new run, which is what
    # keeps a benchmark reproducible instead of letting a result be edited.
    ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
        RunStatus.PENDING: frozenset({RunStatus.PLANNING, RunStatus.FAILED}),
        RunStatus.PLANNING: frozenset({RunStatus.RETRIEVING, RunStatus.DRAFTING, RunStatus.FAILED}),
        RunStatus.RETRIEVING: frozenset({RunStatus.DRAFTING, RunStatus.FAILED}),
        RunStatus.DRAFTING: frozenset({RunStatus.VERIFYING, RunStatus.COMPLETED, RunStatus.FAILED}),
        RunStatus.VERIFYING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED}),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
    }

    def advance_status(self, run_id: uuid.UUID, status: RunStatus) -> ResearchRun:
        """Move a run to the next status, refusing an illegal transition.

        Enforced in the service rather than left to callers, because a run that goes
        `completed` → `retrieving` would produce a second report for the same run and
        break the one-report-per-run invariant from the far side.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)

            if status not in self.ALLOWED_TRANSITIONS[run.status]:
                raise ConflictError(
                    f"Cannot move a run from '{run.status.value}' to '{status.value}'.",
                    details={
                        "run_id": str(run_id),
                        "from": run.status.value,
                        "to": status.value,
                        "allowed": sorted(s.value for s in self.ALLOWED_TRANSITIONS[run.status]),
                    },
                )

            if run.status is RunStatus.PENDING:
                runs.mark_started(run, status)
            elif status is RunStatus.COMPLETED:
                runs.mark_completed(run)
            else:
                runs.mark_status(run, status)

            logger.info("Run %s -> %s", run_id, status.value)
            return run

    def fail_run(self, run_id: uuid.UUID, *, code: str, message: str | None = None) -> ResearchRun:
        """Mark a run failed, with its cause.

        `code` reuses the API's stable error vocabulary, so a run that failed because
        a provider was down (`external_service_error`) stays distinguishable from one
        that failed because our logic is wrong (`internal_error`). The benchmark must
        not conflate them.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)
            runs.mark_failed(run, code=code, message=message)
            logger.warning("Run %s failed: %s (%s)", run_id, code, message or "no detail")
            return run

    def record_plan(self, run_id: uuid.UUID, subquestions: Sequence[str]) -> ResearchRun:
        """Persist the plan for a run, all of it or none.

        Partially recorded sub-questions would make coverage — addressed divided by
        planned — wrong in the project's favour, since the denominator would be too
        small.
        """
        with unit_of_work(self._session) as session:
            runs = ResearchRunRepository(session)
            run = runs.get_or_raise(run_id)
            if run.subquestions:
                raise ConflictError(
                    f"Run {run_id} already has a plan.",
                    details={"run_id": str(run_id), "existing": len(run.subquestions)},
                )
            runs.add_subquestions(run, subquestions)
            return run

    # --- sources -------------------------------------------------------------

    def attach_source(
        self,
        run_id: uuid.UUID,
        *,
        fingerprint: str | None,
        citation_label: str | None = None,
        evidence_depth: EvidenceDepth = EvidenceDepth.SNIPPET,
        retriever: str | None = None,
        retrieval_query: str | None = None,
        rank: int | None = None,
        **source_values,
    ):
        """Deduplicate a source and link it to a run, in one transaction.

        Both halves must land together: a source row with no link is invisible to the
        run that found it, and a link to a source that was never created is
        impossible. Doing them in separate transactions would allow the first.

        The link carries the per-run retrieval facts — depth, retriever, query, rank —
        because they differ between runs and the association row is where they live.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            sources = SourceRepository(session)
            source, created = sources.get_or_create(fingerprint, **source_values)
            link = sources.link_to_run(
                run,
                source,
                citation_label=citation_label,
                evidence_depth=evidence_depth,
                retriever=retriever,
                retrieval_query=retrieval_query,
                rank=rank,
            )
            logger.info(
                "Attached source %s to run %s (%s)",
                source.id,
                run_id,
                "new" if created else "deduplicated",
            )
            return source, link

    def set_citation_labels(self, run_id: uuid.UUID, labels: dict[str, str]) -> int:
        """Persist the [S#] labels assigned at evidence selection, by fingerprint.

        Without this, the labels exist only in the response and the report text, and
        claim extraction could not resolve a marker back to a source row.
        """
        with unit_of_work(self._session) as session:
            ResearchRunRepository(session).get_or_raise(run_id)
            labelled = SourceRepository(session).set_citation_labels(run_id, labels)
            logger.info("Labelled %d sources for run %s", labelled, run_id)
            return labelled

    # --- the report ------------------------------------------------------------

    def record_report(
        self,
        run_id: uuid.UUID,
        *,
        title: str,
        markdown: str,
        model: str | None = None,
        prompt_version: str | None = None,
    ):
        """Persist the run's report and its sections, in one transaction.

        At most one report per run, enforced here as well as by the unique
        constraint, so the error is a clear conflict rather than an IntegrityError
        surfacing from the driver. Sections are parsed from the markdown's `##`
        headings with their character spans, so a claim can later be attributed to
        the section it sits in without duplicating any text.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            reports = ReportRepository(session)
            if reports.get_for_run(run_id) is not None:
                raise ConflictError(
                    f"Run {run_id} already has a report.", details={"run_id": str(run_id)}
                )
            report = reports.create(
                run=run, title=title, markdown=markdown, model=model, prompt_version=prompt_version
            )
            for position, (heading, start, end) in enumerate(parse_sections(markdown)):
                session.add(
                    ReportSection(
                        report=report,
                        position=position,
                        heading=heading,
                        start_char=start,
                        end_char=end,
                    )
                )
            session.flush()
            logger.info(
                "Recorded report %s for run %s (%d words, %d sections)",
                report.id,
                run_id,
                report.word_count,
                len(report.sections),
            )
            return report

    # --- documents and chunks ------------------------------------------------------

    def record_processed_documents(
        self,
        run_id: uuid.UUID,
        *,
        documents: Sequence,
        source_ids: dict[str, uuid.UUID],
    ) -> int:
        """Persist the processed documents and their chunks for a run's sources.

        Chunk rows are stored under the processing stage's content-derived UUIDv5
        ids — the same ids the vector store uses — so a vector hit addresses its
        database row directly. Documents deduplicate by (source, content_hash), and
        an already-stored document keeps its existing chunks: same content, same
        ids, nothing to add. Returns how many chunk rows were newly stored.
        """
        with unit_of_work(self._session) as session:
            ResearchRunRepository(session).get_or_raise(run_id)
            document_repository = DocumentRepository(session)
            chunk_repository = DocumentChunkRepository(session)
            sources = SourceRepository(session)

            stored = 0
            for processed in documents:
                if processed.is_empty:
                    continue
                source_id = source_ids.get(processed.source.fingerprint)
                if source_id is None:
                    continue
                source = sources.get_or_raise(source_id)
                document, created = document_repository.get_or_create(
                    source=source,
                    text=processed.text,
                    depth=processed.depth,
                    extractor=processed.extractor,
                )
                if created:
                    chunk_repository.add_chunks(
                        document,
                        processed.spans,
                        ids=[chunk.chunk_id for chunk in processed.chunks],
                    )
                    stored += len(processed.chunks)
            if stored:
                logger.info("Stored %d chunk rows for run %s", stored, run_id)
            return stored

    # --- verification ---------------------------------------------------------------

    def claims_awaiting_verification(self, run_id: uuid.UUID) -> tuple[uuid.UUID, list[dict]]:
        """The run's unverified claims, as plain rows safe to use outside a session."""
        with unit_of_work(self._session) as session:
            report = ReportRepository(session).get_for_run(run_id)
            if report is None:
                raise NotFoundError(
                    f"Run {run_id} has no report.", details={"run_id": str(run_id)}
                )
            claims = ClaimRepository(session).list_unverified(report.id)
            rows = [
                {
                    "claim_id": claim.id,
                    "text": claim.text,
                    "claim_type": claim.claim_type,
                    "importance": claim.importance,
                    "requires_citation": claim.requires_citation,
                    "citations": [
                        {
                            "citation_id": citation.id,
                            "marker": citation.marker,
                            "status": citation.status,
                            "source_id": citation.source_id,
                        }
                        for citation in claim.citations
                    ],
                }
                for claim in claims
            ]
            return report.id, rows

    def verification_summary(self, run_id: uuid.UUID) -> dict:
        """Everything the final report and its evidence audit need, in plain data.

        One read, no judgement: claims with their verdicts, rationales and citation
        outcomes; the stored conflicts with their factual descriptions; and the
        run's labelled sources. The audit is computed from these rows by code —
        never asked of a model — so it can always be recomputed from the database.
        """
        with unit_of_work(self._session) as session:
            report = ReportRepository(session).get_for_run(run_id)
            if report is None:
                raise NotFoundError(
                    f"Run {run_id} has no report.", details={"run_id": str(run_id)}
                )
            claims = ClaimRepository(session).list_for_report(report.id)
            claim_rows = []
            for claim in claims:
                result = claim.verification_result
                claim_rows.append(
                    {
                        "claim_id": claim.id,
                        "text": claim.text,
                        "claim_type": claim.claim_type.value,
                        "importance": claim.importance.value,
                        "requires_citation": claim.requires_citation,
                        "section": claim.report_section.heading if claim.report_section else None,
                        "verdict": result.verdict.value if result else None,
                        "confidence": result.confidence if result else None,
                        "downgraded": result.downgraded if result else False,
                        "rationale": result.rationale if result else None,
                        "citations": {c.marker: c.status.value for c in claim.citations},
                    }
                )

            conflicts = (
                session.execute(
                    select(Conflict).where(Conflict.research_run_id == run_id)
                )
                .scalars()
                .all()
            )
            conflict_rows = [
                {
                    "conflict_id": conflict.id,
                    "claim_id": conflict.claim_id,
                    "conflict_type": conflict.conflict_type.value,
                    "description": conflict.description,
                }
                for conflict in conflicts
            ]

            source_rows = []
            for link in SourceRepository(session).links_for_run(run_id):
                if link.citation_label:
                    source = link.source
                    source_rows.append(
                        {
                            "label": link.citation_label,
                            "title": source.title,
                            "url": source.url,
                            "doi": source.doi,
                            "venue": source.venue,
                            "year": source.publication_year,
                            "source_type": source.source_type.value,
                        }
                    )

            return {
                "report_id": report.id,
                "draft_markdown": report.markdown,
                "finalized": report.final_markdown is not None,
                "topic": ResearchRunRepository(session).get_or_raise(run_id).topic,
                "claims": claim_rows,
                "conflicts": conflict_rows,
                "sources": source_rows,
            }

    def record_final_report(
        self, run_id: uuid.UUID, *, final_markdown: str, model: str | None
    ) -> uuid.UUID:
        """Store the verification-aware final report on the run's report row.

        One per run: finalizing twice would quietly re-roll a text the user may
        already have cited, so it is refused like every other re-roll here.
        """
        with unit_of_work(self._session) as session:
            report = ReportRepository(session).get_for_run(run_id)
            if report is None:
                raise NotFoundError(
                    f"Run {run_id} has no report.", details={"run_id": str(run_id)}
                )
            if report.final_markdown is not None:
                raise ConflictError(
                    f"Run {run_id} already has a final report.",
                    details={"run_id": str(run_id)},
                )
            report.final_markdown = final_markdown
            report.final_model = model
            report.finalized_at = utcnow()
            session.flush()
            logger.info("Recorded final report for run %s (%d chars)", run_id, len(final_markdown))
            return report.id

    def citation_status_summary(self, run_id: uuid.UUID) -> dict[str, int]:
        """Citation outcomes for a run's report, grouped by status."""
        with unit_of_work(self._session) as session:
            report = ReportRepository(session).get_for_run(run_id)
            if report is None:
                return {}
            counts = CitationRepository(session).count_by_status(report.id)
            return {status.value: count for status, count in counts.items()}

    def record_claim_verification(
        self,
        run_id: uuid.UUID,
        claim_id: uuid.UUID,
        *,
        passages: Sequence[dict],
        verdict: VerificationVerdict,
        confidence: float | None,
        rationale: str | None,
        supporting_passage_chunk_id: uuid.UUID | None,
        conflict: dict | None = None,
        verifier_model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict:
        """Persist one claim's verification: evidence, verdict, citation outcomes and
        any conflict — in a single transaction, so a claim is verified entirely or
        not at all.

        Citation validation happens here, structurally: a citation is `valid` only
        when a **supporting** evidence passage belongs to the **cited** source — the
        URL existing, the paper existing, or the source being topical count for
        nothing, which is the spec's rule. A real source whose retrieved passages do
        not support the claim becomes `misattributed`, with the reason stored.

        A reported conflict is stored between the two evidence rows it names; the
        description records what each passage says, never a guessed explanation of
        the disagreement.
        """
        with unit_of_work(self._session) as session:
            run = ResearchRunRepository(session).get_or_raise(run_id)
            claim = ClaimRepository(session).get_or_raise(claim_id)
            if claim.verification_result is not None:
                raise ConflictError(
                    f"Claim {claim_id} is already verified.",
                    details={"claim_id": str(claim_id)},
                )

            evidence_repository = EvidenceRepository(session)
            evidence_by_chunk: dict[uuid.UUID, Evidence] = {}
            for passage in passages:
                chunk = DocumentChunkRepository(session).get_or_raise(passage["chunk_id"])
                evidence_by_chunk[chunk.id] = evidence_repository.create(
                    claim=claim,
                    chunk=chunk,
                    relation=passage["relation"],
                    similarity=passage.get("similarity"),
                    rank=passage.get("rank"),
                    quoted_text=passage.get("quoted_text"),
                    quote_start_char=passage.get("quote_start_char"),
                    quote_end_char=passage.get("quote_end_char"),
                )

            supporting_evidence = (
                evidence_by_chunk.get(supporting_passage_chunk_id)
                if supporting_passage_chunk_id is not None
                else None
            )
            result = VerificationResultRepository(session).create(
                claim=claim,
                verdict=verdict,
                supporting_evidence=supporting_evidence,
                confidence=confidence,
                rationale=rationale,
                verifier_model=verifier_model,
                prompt_version=prompt_version,
            )

            # Which source does each evidence chunk belong to? Needed to decide
            # whether a citation's own source did any supporting.
            chunk_sources: dict[uuid.UUID, uuid.UUID] = dict(
                session.execute(
                    select(DocumentChunk.id, Document.source_id)
                    .join(Document, DocumentChunk.document_id == Document.id)
                    .where(DocumentChunk.id.in_(evidence_by_chunk.keys()))
                ).all()
            )
            supporting_chunks_by_source: dict[uuid.UUID, uuid.UUID] = {}
            for chunk_id, evidence in evidence_by_chunk.items():
                if evidence.relation is EvidenceRelation.SUPPORTS:
                    source = chunk_sources.get(chunk_id)
                    if source is not None:
                        supporting_chunks_by_source.setdefault(source, chunk_id)

            citation_outcomes: dict[str, str] = {}
            for citation in claim.citations:
                if citation.status is not CitationStatus.UNVALIDATED:
                    citation_outcomes[citation.marker] = citation.status.value
                    continue
                supporting_chunk = (
                    supporting_chunks_by_source.get(citation.source_id)
                    if citation.source_id is not None
                    else None
                )
                if supporting_chunk is not None:
                    citation.status = CitationStatus.VALID
                    citation.supporting_chunk_id = supporting_chunk
                else:
                    citation.status = CitationStatus.MISATTRIBUTED
                    citation.failure_reason = (
                        "The cited source is real, but none of its retrieved passages "
                        "supports this claim."
                    )
                citation.validated_at = utcnow()
                citation_outcomes[citation.marker] = citation.status.value

            conflict_id: uuid.UUID | None = None
            if conflict is not None:
                evidence_a = evidence_by_chunk.get(conflict["supporting_chunk_id"])
                evidence_b = evidence_by_chunk.get(conflict["contradicting_chunk_id"])
                if evidence_a is not None and evidence_b is not None:
                    stored_conflict = ConflictRepository(session).create(
                        run=run,
                        evidence_a=evidence_a,
                        evidence_b=evidence_b,
                        conflict_type=conflict["conflict_type"],
                        description=conflict["description"],
                        claim_id=claim.id,
                        confidence=conflict.get("confidence"),
                        detector_model=verifier_model,
                    )
                    conflict_id = stored_conflict.id

            session.flush()
            return {
                "claim_id": claim.id,
                "verdict": result.verdict,
                "confidence": result.confidence,
                "downgraded": result.downgraded,
                "rationale": result.rationale,
                "evidence_count": len(evidence_by_chunk),
                "citations": citation_outcomes,
                "conflict_id": conflict_id,
            }

    # --- claims ------------------------------------------------------------------

    def record_claims(
        self,
        run_id: uuid.UUID,
        *,
        claims: Sequence[dict],
        extractor_model: str | None = None,
    ) -> tuple[uuid.UUID, list[dict]]:
        """Persist extracted claims and their citations, all of them or none.

        Each entry carries `text`, `claim_type`, `importance`, `requires_citation`,
        an optional `section` heading, and `citation_labels` — the [S#] markers the
        report used. Markers are resolved here, against the run's own labelled
        sources: a marker that resolves becomes an `unvalidated` citation pointing
        at the source row, and a marker that resolves to nothing becomes a
        `fabricated` one, because the report cited a source the run never had.
        That distinction is structural, not a judgement call, which is why it can
        be made at extraction time.
        """
        with unit_of_work(self._session) as session:
            ResearchRunRepository(session).get_or_raise(run_id)
            report = ReportRepository(session).get_for_run(run_id)
            if report is None:
                raise NotFoundError(
                    f"Run {run_id} has no report to extract claims from.",
                    details={"run_id": str(run_id)},
                )
            claim_repository = ClaimRepository(session)
            if claim_repository.count_for_report(report.id) > 0:
                raise ConflictError(
                    f"Report {report.id} already has claims.",
                    details={"report_id": str(report.id)},
                )

            label_to_source: dict[str, uuid.UUID] = {}
            for link in SourceRepository(session).links_for_run(run_id):
                if link.citation_label:
                    label_to_source[link.citation_label] = link.source_id
            sections = {section.heading.strip().lower(): section for section in report.sections}

            citation_repository = CitationRepository(session)
            rows: list[dict] = []
            citation_total = 0
            for position, entry in enumerate(claims):
                section = sections.get((entry.get("section") or "").strip().lower())
                claim = claim_repository.create(
                    report=report,
                    position=position,
                    text=entry["text"],
                    claim_type=entry["claim_type"],
                    importance=entry["importance"],
                    requires_citation=entry.get("requires_citation", True),
                    report_section=section,
                    extractor_model=extractor_model,
                )
                citation_ids: list[uuid.UUID] = []
                cited_source_ids: list[uuid.UUID] = []
                markers = list(dict.fromkeys(entry.get("citation_labels", [])))
                for marker in markers:
                    source_id = label_to_source.get(marker)
                    if source_id is not None:
                        citation = citation_repository.create(
                            claim=claim,
                            marker=marker,
                            status=CitationStatus.UNVALIDATED,
                            source_id=source_id,
                            raw_reference=f"[{marker}]",
                        )
                        cited_source_ids.append(source_id)
                    else:
                        citation = citation_repository.create(
                            claim=claim,
                            marker=marker,
                            status=CitationStatus.FABRICATED,
                            source_id=None,
                            raw_reference=f"[{marker}]",
                            failure_reason=(
                                f"The report cites [{marker}], but the run provided "
                                "no source under that label."
                            ),
                        )
                    citation_ids.append(citation.id)
                citation_total += len(citation_ids)

                # Response rows are built inside the session, so nothing is read off
                # a detached ORM object later.
                rows.append(
                    {
                        "id": claim.id,
                        "claim_id": claim.id,
                        "research_id": run_id,
                        "claim_text": claim.text,
                        "claim_type": claim.claim_type,
                        "importance": claim.importance,
                        "section": section.heading if section else None,
                        "citation_ids": citation_ids,
                        "position": position,
                        "requires_citation": claim.requires_citation,
                        "markers": markers,
                        "cited_source_ids": cited_source_ids,
                    }
                )

            logger.info(
                "Recorded %d claims (%d citations) for report %s",
                len(rows),
                citation_total,
                report.id,
            )
            return report.id, rows
