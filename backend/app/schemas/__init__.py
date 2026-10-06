"""
Pydantic schemas: the API contract.

Kept separate from the ORM models so the database schema can change without
breaking clients. Three kinds of model, with different defaults (see `base.py`):

    *Create / Request   request bodies. Reject unknown fields.
    *Read               responses, built from ORM rows.
    *Trace              the three traceability chains, materialised end to end.
"""

from app.schemas.base import IdentifiedModel, ReadModel, TimestampedModel, WriteModel
from app.schemas.common import (
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
    ServiceDependency,
    ServiceInfoResponse,
)
from app.schemas.evaluation import (
    BenchmarkComparison,
    ConfigurationMetrics,
    EvaluationResultCreate,
    EvaluationResultRead,
)
from app.schemas.evidence import (
    CitationCreate,
    CitationRead,
    ClaimCreate,
    ClaimRead,
    ConflictCreate,
    ConflictRead,
    EvidenceCreate,
    EvidenceRead,
    VerificationResultCreate,
    VerificationResultRead,
)
from app.schemas.research import (
    BenchmarkRunRead,
    ResearchRequest,
    ResearchResponse,
    ResearchRunRead,
    ResearchRunSummary,
    RunConfigurationCreate,
    RunConfigurationRead,
    SubQuestionRead,
)
from app.schemas.source import (
    DocumentChunkCreate,
    DocumentChunkRead,
    DocumentCreate,
    DocumentRead,
    ResearchSourceRead,
    SourceCreate,
    SourceRead,
)
from app.schemas.trace import (
    ChunkPointer,
    CitationTrace,
    EvaluationTrace,
    EvidenceLink,
    EvidenceTrace,
    SourcePointer,
    VerificationTrace,
)

__all__ = [
    "BenchmarkComparison",
    "BenchmarkRunRead",
    "ChunkPointer",
    "CitationCreate",
    "CitationRead",
    "CitationTrace",
    "ClaimCreate",
    "ClaimRead",
    "ConfigurationMetrics",
    "ConflictCreate",
    "ConflictRead",
    "DocumentChunkCreate",
    "DocumentChunkRead",
    "DocumentCreate",
    "DocumentRead",
    "ErrorDetail",
    "ErrorResponse",
    "EvaluationResultCreate",
    "EvaluationResultRead",
    "EvaluationTrace",
    "EvidenceCreate",
    "EvidenceLink",
    "EvidenceRead",
    "EvidenceTrace",
    "HealthResponse",
    "IdentifiedModel",
    "ReadModel",
    "ReadinessResponse",
    "ResearchRequest",
    "ResearchResponse",
    "ResearchRunRead",
    "ResearchRunSummary",
    "ResearchSourceRead",
    "RunConfigurationCreate",
    "RunConfigurationRead",
    "ServiceDependency",
    "ServiceInfoResponse",
    "SourceCreate",
    "SourcePointer",
    "SourceRead",
    "SubQuestionRead",
    "TimestampedModel",
    "VerificationResultCreate",
    "VerificationResultRead",
    "VerificationTrace",
    "WriteModel",
]
