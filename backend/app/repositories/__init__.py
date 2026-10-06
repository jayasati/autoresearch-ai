"""
Repositories: queries for one entity type each.

The rule that defines this layer: **repositories never commit.** They stage inserts
and `flush()` where a database check must happen before the next statement, but the
transaction belongs to whoever opened the unit of work. See `base.py` for why that
matters — committing here is how a half-written claim graph reaches the database.

Services (`app/services/`) compose repositories inside one transaction.
"""

from app.repositories.base import Repository
from app.repositories.evaluation import EvaluationResultRepository, LlmCallLogRepository
from app.repositories.evidence import (
    CitationRepository,
    ClaimRepository,
    ConflictRepository,
    DocumentChunkRepository,
    DocumentRepository,
    EvidenceRepository,
    ReportRepository,
    VerificationResultRepository,
)
from app.repositories.research import (
    BenchmarkRunRepository,
    ResearchRunRepository,
    RunConfigurationRepository,
    SourceRepository,
)

__all__ = [
    "BenchmarkRunRepository",
    "CitationRepository",
    "ClaimRepository",
    "ConflictRepository",
    "DocumentChunkRepository",
    "DocumentRepository",
    "EvaluationResultRepository",
    "EvidenceRepository",
    "LlmCallLogRepository",
    "ReportRepository",
    "Repository",
    "ResearchRunRepository",
    "RunConfigurationRepository",
    "SourceRepository",
    "VerificationResultRepository",
]
