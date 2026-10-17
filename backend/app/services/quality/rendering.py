"""
Rendering structured reports to markdown (docs/REPORT_QUALITY_PIPELINE.md §2, §5).

Code renders every citation marker; the model never writes one. A marker is the
frozen `[S#]` of a passage's source, taken from the code-owned alias table — so the
report's visible format (and `CITATION_PATTERN`) is unchanged while the passage it
rests on is known. No title, URL, DOI, year, venue or author is ever taken from a
model field; the References section is appended later by the existing finalizer
helpers, from stored source rows.

The audit stays out of the text: what is rendered here is user-facing prose plus
code-templated sections. Repairs, alias tables and metrics go to the audit record.
"""

from collections.abc import Mapping, Sequence

from app.services.quality.assessment import CONFLICT_FRAMING
from app.services.quality.models import (
    AliasTable,
    ConflictContext,
    Placement,
    StructuredReport,
    SubquestionAssessment,
)
from app.services.quality.validation import PlacedSentence, ValidatedReport

_SECTION_TEXT: dict[Placement, tuple[str, str]] = {
    Placement.CITATION_NOT_CONFIRMED: (
        "Supported by other evidence — citation not confirmed",
        "Verification found support for these statements in the retrieved evidence, but "
        "not in the passage each one cited. They are listed without a citation rather "
        "than attributed to a different source.",
    ),
    Placement.CONTESTED: ("Contested — sources disagree", CONFLICT_FRAMING),
    Placement.UNRESOLVED: (
        "Unresolved findings",
        "These statements bear on the question, but the retrieved evidence could not "
        "establish them. They are reported as open, not as findings.",
    ),
    Placement.UNVERIFIED_LIST: (
        "Other unverified statements",
        "The evidence retrieved neither established nor addressed these statements.",
    ),
    Placement.NOT_EVALUATED: (
        "Not evaluated",
        "These statements were not checked — a budget or deadline limit, a technical "
        "failure, or a rewrite still awaiting re-verification. They are listed, not "
        "presented as findings.",
    ),
}
_SECTION_ORDER = [
    Placement.CONTESTED,
    Placement.CITATION_NOT_CONFIRMED,
    Placement.UNRESOLVED,
    Placement.UNVERIFIED_LIST,
    Placement.NOT_EVALUATED,
]


def with_markers(text: str, markers: Sequence[str]) -> str:
    """`text [S1][S3].` — markers before the closing punctuation, as legacy reports do."""
    if not markers:
        return text
    tags = "".join(f"[{marker}]" for marker in markers)
    stripped = text.rstrip()
    if stripped and stripped[-1] in ".!?":
        return f"{stripped[:-1]} {tags}{stripped[-1]}"
    return f"{stripped} {tags}"


def render_draft(report: StructuredReport, aliases: AliasTable) -> tuple[str, list[str]]:
    """The structured draft (stage 10a) as markdown, plus the passage aliases it cited
    that are not in the table (dropped, never rendered — returned for the audit)."""
    rejected: list[str] = []
    lines = [f"# {report.title}"]
    for section in report.sections:
        lines += ["", f"## {section.heading}", ""]
        paragraph = []
        for sentence in section.sentences:
            markers: list[str] = []
            for alias in sentence.evidence_ids:
                ref = aliases.passages.get(alias)
                if ref is None or ref.source_label not in aliases.sources:
                    rejected.append(alias)
                elif ref.source_label not in markers:
                    markers.append(ref.source_label)
            paragraph.append(with_markers(sentence.text, markers))
        lines.append(" ".join(paragraph))
    return "\n".join(lines).rstrip() + "\n", rejected


def _bullets(sentences: Sequence[PlacedSentence]) -> list[str]:
    return [f"- {sentence.text}" for sentence in sentences]


def render_final(
    validated: ValidatedReport,
    *,
    assessments: Sequence[SubquestionAssessment] = (),
    subquestion_texts: Mapping[str, str] | None = None,
    conflicts: Sequence[ConflictContext] = (),
) -> str:
    """The user-facing final report body and its code-built sections.

    Refuses a report whose finalization failed: that run keeps its draft instead.
    """
    if validated.status == "finalization_failed":
        raise ValueError("finalization failed; there is no final report to render")
    subquestion_texts = subquestion_texts or {}
    lines = [f"# {validated.title}"]
    if validated.status == "degraded":
        lines += [
            "",
            "> **Degraded report.** Some checks could not be completed, so parts of "
            "this report are listed rather than presented as findings: "
            + "; ".join(validated.reasons)
            + ".",
        ]
    for section in validated.sections:
        if not section.sentences:
            continue
        lines += ["", f"## {section.heading}", ""]
        lines.append(" ".join(with_markers(s.text, s.markers) for s in section.sentences))

    for placement in _SECTION_ORDER:
        sentences = validated.labelled.get(placement, [])
        extra = conflicts if placement is Placement.CONTESTED else ()
        if not sentences and not extra:
            continue
        heading, intro = _SECTION_TEXT[placement]
        lines += ["", "---", "", f"## {heading}", "", intro, ""]
        lines += _bullets(sentences)
        for conflict in extra:
            sides = []
            for name, side in (
                ("supporting", conflict.side_supporting),
                ("contradicting", conflict.side_contradicting),
            ):
                if side is not None:
                    detail = ", ".join(
                        str(v) for v in (side.source_type, side.year, side.venue) if v
                    )
                    sides.append(f"{name}: [{side.label}] ({detail})")
            suffix = f" — {'; '.join(sides)}" if sides else ""
            lines.append(
                f"- ({conflict.conflict_type.replace('_', ' ')}) {conflict.description}{suffix}"
            )

    # Retrieval gaps, then every core subquestion the report leaves unanswered (V9)
    # whatever the reason — a coverage threshold never hides one.
    reasons: dict[str, str] = {}
    for gap in assessments:
        if gap.status == "evidence_gap":
            reasons[gap.alias] = (
                "the search providers failed"
                if gap.gap_reason == "provider_failure"
                else "no relevant evidence was found"
            )
    for alias in validated.unanswered_core:
        reasons.setdefault(
            alias, "a core part of the question: the report states no supported finding on it"
        )
    if reasons:
        lines += [
            "",
            "---",
            "",
            "## Evidence gaps",
            "",
            "The report does not answer these parts of the question.",
            "",
        ]
        for alias, why in reasons.items():
            lines.append(f"- {subquestion_texts.get(alias, alias)} — {why}.")
    return "\n".join(lines).rstrip() + "\n"
