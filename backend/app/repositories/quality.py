"""
Repositories for the report-quality-v1 tables (stage 29, S2).

Queries only — the transaction belongs to `QualityStore` (and, like every
repository here, these never commit). Nothing in the legacy pipeline uses them.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.models import CitationCheck, ClaimAudit, FinalClaimCheck, ReportQualityAudit
from app.repositories.base import Repository


class ReportQualityAuditRepository(Repository[ReportQualityAudit]):
    model = ReportQualityAudit

    def for_report(self, report_id: uuid.UUID) -> ReportQualityAudit | None:
        return self.session.execute(
            select(ReportQualityAudit).where(ReportQualityAudit.report_id == report_id)
        ).scalar_one_or_none()


class ClaimAuditRepository(Repository[ClaimAudit]):
    model = ClaimAudit

    def for_claim(self, claim_id: uuid.UUID) -> ClaimAudit | None:
        return self.session.execute(
            select(ClaimAudit).where(ClaimAudit.claim_id == claim_id)
        ).scalar_one_or_none()

    def for_claims(self, claim_ids: Sequence[uuid.UUID]) -> list[ClaimAudit]:
        if not claim_ids:
            return []
        return list(
            self.session.execute(
                select(ClaimAudit).where(ClaimAudit.claim_id.in_(claim_ids))
            ).scalars()
        )


class CitationCheckRepository(Repository[CitationCheck]):
    model = CitationCheck

    def find(self, claim_id: uuid.UUID, passage_alias: str) -> CitationCheck | None:
        return self.session.execute(
            select(CitationCheck).where(
                CitationCheck.claim_id == claim_id, CitationCheck.passage_alias == passage_alias
            )
        ).scalar_one_or_none()

    def for_claims(self, claim_ids: Sequence[uuid.UUID]) -> list[CitationCheck]:
        if not claim_ids:
            return []
        return list(
            self.session.execute(
                select(CitationCheck)
                .where(CitationCheck.claim_id.in_(claim_ids))
                .order_by(CitationCheck.claim_id, CitationCheck.passage_alias)
            ).scalars()
        )


class FinalClaimCheckRepository(Repository[FinalClaimCheck]):
    model = FinalClaimCheck

    def find(
        self, report_id: uuid.UUID, section_index: int, sentence_index: int
    ) -> FinalClaimCheck | None:
        return self.session.execute(
            select(FinalClaimCheck).where(
                FinalClaimCheck.report_id == report_id,
                FinalClaimCheck.section_index == section_index,
                FinalClaimCheck.sentence_index == sentence_index,
            )
        ).scalar_one_or_none()

    def for_report(self, report_id: uuid.UUID) -> list[FinalClaimCheck]:
        return list(
            self.session.execute(
                select(FinalClaimCheck)
                .where(FinalClaimCheck.report_id == report_id)
                .order_by(FinalClaimCheck.section_index, FinalClaimCheck.sentence_index)
            ).scalars()
        )
