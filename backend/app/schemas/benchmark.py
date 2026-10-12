"""
The benchmark dataset structure.

Dataset files live in `data/benchmarks/` and are versioned in git; these schemas are
the contract every file must satisfy to load at all. Two properties matter more than
any field:

- **Ground truth is tiered, and every item names its tier.** A `verifiable_fact` can
  be checked against the record (a retracted paper, a prize that was not awarded). A
  `behavioral` expectation is definable a priori (a false premise must not be
  accepted; absent literature must yield "insufficient evidence"). An
  `established_consensus` item leans on textbook-level findings. An
  `author_judgment` hint is advisory and scored loosely or not at all. Collapsing
  these tiers would let weak ground truth masquerade as strong — the exact failure
  this project measures in models.

- **The final test set is frozen.** Its freeze manifest records a canonical content
  hash; the loader refuses a test set whose content no longer matches. Development
  runs may refine development and validation items; nothing — including benchmark
  results — may modify the test set. That rule is enforced by the hash, not by
  good intentions.
"""

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.base import ReadModel, WriteModel


class BenchmarkCategory(StrEnum):
    """The eight behaviours the benchmark probes."""

    NORMAL_RESEARCH = "normal_research"
    MULTI_PAPER_SYNTHESIS = "multi_paper_synthesis"
    CONFLICTING_LITERATURE = "conflicting_literature"
    NUMERICAL_CLAIMS = "numerical_claims"
    CITATION_VERIFICATION = "citation_verification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FALSE_PREMISE = "false_premise"
    CURRENT_INFORMATION = "current_information"


class GroundTruthBasis(StrEnum):
    """How an item's expectations were established. See the module docstring."""

    VERIFIABLE_FACT = "verifiable_fact"
    ESTABLISHED_CONSENSUS = "established_consensus"
    BEHAVIORAL = "behavioral"
    AUTHOR_JUDGMENT = "author_judgment"


class ExpectedSource(WriteModel):
    """A kind of source a good run should surface. A hint, never a requirement for
    an exact URL — search indexes move under everyone's feet."""

    kind: Literal["web", "academic", "either"]
    hint: str = Field(min_length=5, description="What the source should be about or be.")


class KnownClaim(WriteModel):
    """A claim the literature is expected to support (or refute), with the verdict a
    correct verification should reach. Only written at the two strong tiers."""

    text: str = Field(min_length=10)
    expected_verdict: Literal["supported", "contradicted", "not_enough_evidence"]
    basis: GroundTruthBasis

    @model_validator(mode="after")
    def strong_claims_need_strong_basis(self) -> "KnownClaim":
        if self.basis is GroundTruthBasis.AUTHOR_JUDGMENT:
            raise ValueError(
                "a known claim scored against verdicts needs verifiable_fact, "
                "established_consensus or behavioral basis; author_judgment hints "
                "belong in expected_evidence"
            )
        return self


class ExpectedBehavior(WriteModel):
    """What an honest system should do with the question — the part of ground truth
    that is definable a priori, before any literature is read."""

    summary: str = Field(min_length=20)
    premise_is_false: bool = False
    expect_conflicts: bool = False
    expect_insufficient_evidence: bool = False
    answer_changes_over_time: bool = False

    @model_validator(mode="after")
    def flags_must_not_contradict(self) -> "ExpectedBehavior":
        if self.premise_is_false and self.expect_insufficient_evidence:
            raise ValueError(
                "a false premise is contradicted by evidence, not starved of it; "
                "pick the failure the item actually probes"
            )
        return self


class BenchmarkItem(WriteModel):
    """One benchmark question with its ground truth, at its stated tier."""

    question_id: str = Field(pattern=r"^(dev|val|tst)-[a-z_]+-\d{2}$")
    question: str = Field(min_length=15, max_length=500)
    category: BenchmarkCategory
    difficulty: Literal["easy", "medium", "hard"]

    expected_sources: list[ExpectedSource] = Field(default_factory=list)
    expected_evidence: list[str] = Field(
        default_factory=list,
        description="Statements the retrieved evidence should contain or establish.",
    )
    known_claims: list[KnownClaim] = Field(default_factory=list)
    known_conflicts: list[str] = Field(
        default_factory=list,
        description="Disagreements the literature is known to contain, as statements "
        "of what each side reports.",
    )
    expected_behavior: ExpectedBehavior
    ground_truth_basis: GroundTruthBasis
    ground_truth_notes: str = Field(
        min_length=20,
        description="How this item's ground truth was established, in plain words.",
    )

    @model_validator(mode="after")
    def the_id_must_carry_the_category(self) -> "BenchmarkItem":
        """A mismatch here is a copy-paste error that would silently misfile every
        result computed for the item by category."""
        if f"-{self.category.value}-" not in self.question_id:
            raise ValueError(
                f"question_id {self.question_id!r} does not carry its category "
                f"{self.category.value!r}"
            )
        return self


class BenchmarkSet(ReadModel):
    """One of the three sets. `fingerprint` is the canonical content hash the freeze
    manifest pins for the test set."""

    name: Literal["development", "validation", "test"]
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    created: date
    description: str
    items: list[BenchmarkItem]

    @field_validator("items")
    @classmethod
    def ids_must_be_unique(cls, items: list[BenchmarkItem]) -> list[BenchmarkItem]:
        seen: set[str] = set()
        for item in items:
            if item.question_id in seen:
                raise ValueError(f"duplicate question_id {item.question_id!r}")
            seen.add(item.question_id)
        return items

    @model_validator(mode="after")
    def ids_must_carry_the_set_prefix(self) -> "BenchmarkSet":
        prefix = {"development": "dev", "validation": "val", "test": "tst"}[self.name]
        for item in self.items:
            if not item.question_id.startswith(f"{prefix}-"):
                raise ValueError(
                    f"{item.question_id!r} does not belong to the {self.name} set"
                )
        return self

    @property
    def fingerprint(self) -> str:
        """Canonical content hash: independent of key order, whitespace and line
        endings, so a Windows checkout and a Linux checkout agree about whether the
        set changed."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def by_category(self) -> dict[BenchmarkCategory, list[BenchmarkItem]]:
        grouped: dict[BenchmarkCategory, list[BenchmarkItem]] = {}
        for item in self.items:
            grouped.setdefault(item.category, []).append(item)
        return grouped
