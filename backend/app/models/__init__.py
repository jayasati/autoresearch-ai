"""
SQLAlchemy models.

Importing this package registers every model with `Base.metadata`. That matters:
relationships are declared with string targets ("ResearchRun"), which SQLAlchemy
resolves from its registry the first time a mapper is configured. If a module were
never imported, its class would be missing from that registry and the error would
surface far from the cause. So everything is imported here, once, and the rest of
the codebase imports from `app.models`.

Entity map -- see DATA_MODEL.md for the reasoning behind each:

    RunConfiguration     immutable, content-addressed settings snapshot
    BenchmarkRun         groups the runs of one experiment
    ResearchRun          one research request; root of the traceability graph
    SubQuestion          the plan, kept so coverage can be measured

    Source               a page or paper; global, deduplicated across runs
    ResearchSource       which run retrieved which source, and how deeply
    Document             one fetched extraction of a source, at one moment
    DocumentChunk        a span of that document, with character offsets

    Report               the generated text; at most one per run
    ReportSection        a section of it, with its span

    Claim                one atomic assertion from the report
    Evidence             a passage linked to a claim, with its stance
    Citation             what the report *said* its source was
    VerificationResult   the verdict, and the evidence it rests on
    Conflict             two passages that disagree

    EvaluationResult     one measured metric for one run
    LlmCallLog           one model call: tokens, cost, outcome
"""

from app.db.base import Base
from app.models.evaluation import EvaluationResult
from app.models.evidence import Citation, Claim, Conflict, Evidence, VerificationResult
from app.models.observability import LlmCallLog
from app.models.report import Report, ReportSection
from app.models.research import BenchmarkRun, ResearchRun, RunConfiguration, SubQuestion
from app.models.source import Document, DocumentChunk, ResearchSource, Source

__all__ = [
    "Base",
    "BenchmarkRun",
    "Citation",
    "Claim",
    "Conflict",
    "Document",
    "DocumentChunk",
    "EvaluationResult",
    "Evidence",
    "LlmCallLog",
    "Report",
    "ReportSection",
    "ResearchRun",
    "ResearchSource",
    "RunConfiguration",
    "Source",
    "SubQuestion",
    "VerificationResult",
]
