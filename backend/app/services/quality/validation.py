"""
Final-report validation, checks V1–V7 and the outcome status
(docs/REPORT_QUALITY_PIPELINE.md §5). Deterministic; no model calls.

The revision model proposes sentences tagged with claim and passage aliases; this
module decides what may stand in the body, with which markers, and records every
repair, move and flag with the original text:

    V1 unknown aliases                → reference rejected (never rendered)
    V2 meaning changed vs its claims  → needs re-verification; until a re-verified
                                        result is supplied it is NOT evaluated
    V3 citation support               → a marker renders only for a passage whose
                                        chunk was established as supporting
    V4 placement by claim status      → body, qualified, or a labelled section —
                                        nothing is deleted outright
    V5 untraceable statements         → unresolved
    V6 repetition                     → flagged only
    V7 important verified omissions   → restored as a templated sentence (only claims
                                        no revised sentence states — never an
                                        outdated version of a rewritten claim)

Approved rules applied here: an unconfirmed citation is never replaced with a
different source — the sentence moves to a labelled section instead; contested
claims are presented as possible disagreements; thresholds are provisional.

Re-verification itself (S5) is not done here: V2 returns which sentences need it,
and the caller supplies the results on a second pass.
"""

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import Settings
from app.services.quality.models import (
    IN_BODY,
    AliasTable,
    ClaimStatus,
    OutcomeStatus,
    Placement,
    PoolStatus,
    ReportSentence,
    StructuredReport,
    ValidationAction,
)
from app.services.quality.quantities import context_tokens, cosine, extract_quantities

Embed = Callable[[list[str]], list[list[float]]]
SentenceKey = tuple[int, int]  # (section index, sentence index) in the model's report

_NEGATIONS = {
    "not",
    "no",
    "never",
    "none",
    "neither",
    "nor",
    "without",
    "cannot",
    "fails",
    "failed",
}
_HEDGES = {
    "may",
    "might",
    "could",
    "can",
    "suggest",
    "suggests",
    "suggested",
    "possibly",
    "potentially",
    "likely",
    "appears",
    "appear",
    "associated",
    "linked",
    "correlated",
    "indicates",
    "indicate",
}
_QUANTIFIERS = {"all", "every", "always", "most", "some", "many", "few", "only", "any"}
_WORD = re.compile(r"[a-z']+")


@dataclass(frozen=True)
class ClaimInfo:
    """What validation needs to know about one verified (or unverified) claim."""

    alias: str
    text: str
    importance: Literal["high", "medium", "low"]
    subquestion: str | None  # "Q2"
    is_core: bool
    status: ClaimStatus
    pool: PoolStatus
    supported_chunks: frozenset[uuid.UUID]  # chunks with an established supports/partial relation


@dataclass(frozen=True)
class Reverification:
    """The re-verified result for a sentence whose meaning changed (supplied by S5)."""

    status: ClaimStatus
    pool: PoolStatus
    supported_chunks: frozenset[uuid.UUID]


@dataclass(frozen=True)
class ValidationConfig:
    degraded_body_loss_ratio: float
    fail_body_loss_ratio: float
    min_core_coverage: float
    meaning_change_min_cosine: float
    duplicate_min_cosine: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "ValidationConfig":
        return cls(
            degraded_body_loss_ratio=settings.REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO,
            fail_body_loss_ratio=settings.REPORT_QUALITY_FAIL_BODY_LOSS_RATIO,
            min_core_coverage=settings.REPORT_QUALITY_MIN_CORE_COVERAGE,
            meaning_change_min_cosine=settings.REPORT_QUALITY_MEANING_CHANGE_MIN_COSINE,
            duplicate_min_cosine=settings.REPORT_QUALITY_DUPLICATE_MIN_COSINE,
        )


@dataclass(frozen=True)
class PlacedSentence:
    text: str
    kind: str
    placement: Placement
    markers: tuple[str, ...]  # rendered S# labels, body only
    claim_aliases: tuple[str, ...]
    status: ClaimStatus | None
    origin: Literal["model", "restored"] = "model"
    key: SentenceKey | None = None


@dataclass(frozen=True)
class PlacedSection:
    subquestion: str
    heading: str
    sentences: list[PlacedSentence]


@dataclass
class ValidatedReport:
    """The validated report. `status` is final only when `needs_reverification` is
    empty: until the caller re-verifies those sentences and validates again, the
    outcome is provisional (and usually worse, since they count as not in the body).
    `reasons` lists every failing and degrading condition found."""

    title: str
    sections: list[PlacedSection]  # the body, in model order (restorations appended)
    labelled: dict[Placement, list[PlacedSentence]]  # every non-body placement
    actions: list[ValidationAction]
    needs_reverification: list[SentenceKey]
    status: OutcomeStatus
    reasons: list[str]
    metrics: dict = field(default_factory=dict)


# --- V2: meaning change ------------------------------------------------------------


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower().replace("n't", " not")))


def _numbers(text: str, entities: Sequence[str]) -> set[tuple[float, str | None]]:
    return {(m.stated_number, m.unit) for m in extract_quantities(text, entities)}


def detect_meaning_change(
    sentence: str,
    claim_texts: Sequence[str],
    *,
    entities: Sequence[str] = (),
    embed: Embed | None = None,
    min_cosine: float,
) -> set[str]:
    """How a rewritten sentence departs from the claims it says it states.

    **Heuristic.** These signals catch common, surface-visible changes; they do not
    understand meaning. A sentence that passes every check (an empty result) is NOT
    proven to mean what the verified claims meant — a reworded causal claim, a
    swapped subject, or a qualifier outside the fixed word lists can pass unnoticed,
    and a high embedding cosine is similarity, not equivalence. An empty result means
    only "no change detected".

    `number`        a number (with its unit) was added, changed or dropped
    `negation`      negation words differ
    `qualification` hedges or quantifiers differ (a removed "may", an added "all")
    `scope`         a stated period, place, population, setting, method or entity differs
    `wording`       embedding cosine below `min_cosine` (only with an embedder)
    """
    joined = " ".join(claim_texts)
    changes: set[str] = set()
    if _numbers(sentence, entities) != _numbers(joined, entities):
        changes.add("number")
    sentence_words, claim_words = _words(sentence), _words(joined)
    if (sentence_words & _NEGATIONS) != (claim_words & _NEGATIONS):
        changes.add("negation")
    if bool(sentence_words & _HEDGES) != bool(claim_words & _HEDGES) or (
        sentence_words & _QUANTIFIERS
    ) != (claim_words & _QUANTIFIERS):
        changes.add("qualification")
    if context_tokens(sentence, entities) != context_tokens(joined, entities):
        changes.add("scope")
    if embed is not None:
        vectors = embed([sentence, joined])
        if cosine(vectors[0], vectors[1]) < min_cosine:
            changes.add("wording")
    return changes


# --- V4: placement -----------------------------------------------------------------

#: When one sentence states several claims, the most cautious status decides.
_CAUTION = [
    ClaimStatus.CONTESTED,
    ClaimStatus.NOT_EVALUATED,
    ClaimStatus.INSUFFICIENT_EVIDENCE,
    ClaimStatus.CITATION_NOT_CONFIRMED,
    ClaimStatus.SUPPORTED_BY_CITED_SOURCE,
]


def _most_cautious(statuses: Sequence[ClaimStatus]) -> ClaimStatus:
    return min(statuses, key=_CAUTION.index)


def place(status: ClaimStatus, *, partial: bool, important: bool) -> Placement:
    """Design §3/§5 placement. Insufficient evidence on an important claim is kept
    visible as an unresolved finding; on a minor one it joins the unverified list."""
    if status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE:
        return Placement.QUALIFIED if partial else Placement.BODY
    if status is ClaimStatus.CITATION_NOT_CONFIRMED:
        return Placement.CITATION_NOT_CONFIRMED
    if status is ClaimStatus.CONTESTED:
        return Placement.CONTESTED
    if status is ClaimStatus.INSUFFICIENT_EVIDENCE:
        return Placement.UNRESOLVED if important else Placement.UNVERIFIED_LIST
    return Placement.NOT_EVALUATED


QUALIFIER_PREFIX = "The evidence partially supports the following: "
RESTORED_PREFIX = "The evidence indicates: "


# --- the whole pass ------------------------------------------------------------------


def validate_final(
    report: StructuredReport,
    aliases: AliasTable,
    claims: Mapping[str, ClaimInfo],
    *,
    config: ValidationConfig,
    core_subquestions: frozenset[str],
    verification_coverage: float,
    entities: Sequence[str] = (),
    embed: Embed | None = None,
    reverified: Mapping[SentenceKey, Reverification] | None = None,
) -> ValidatedReport:
    reverified = reverified or {}
    actions: list[ValidationAction] = []
    needs: list[SentenceKey] = []
    labelled: dict[Placement, list[PlacedSentence]] = {}
    sections: list[PlacedSection] = []
    proposed = 0
    lost = 0

    def record(action, section, index, text, detail) -> None:
        actions.append(ValidationAction(action, section, index, text, detail))

    for s_index, section in enumerate(report.sections):
        body: list[PlacedSentence] = []
        for t_index, sentence in enumerate(section.sentences):
            key = (s_index, t_index)
            placed = _place_sentence(
                sentence,
                key,
                section.heading,
                aliases,
                claims,
                config,
                entities,
                embed,
                reverified,
                record,
                needs,
            )
            if sentence.kind in ("finding", "qualification"):
                proposed += 1
                if placed.placement not in IN_BODY:
                    lost += 1
            if placed.placement in IN_BODY:
                body.append(placed)
            else:
                labelled.setdefault(placed.placement, []).append(placed)
                record(
                    "moved",
                    section.heading,
                    t_index,
                    sentence.text,
                    f"placed in {placed.placement.value}",
                )
        sections.append(PlacedSection(section.subquestion, section.heading, body))

    # V6 — repetition, flagged only.
    _flag_duplicates(sections, config, embed, record)

    # V7 — important, verified claims the revision OMITTED are restored. A claim that
    # some revised sentence still states (wherever that sentence was placed — e.g.
    # awaiting re-verification after a meaning change) is NOT restored: that would put
    # back an outdated version of a claim the revision rewrote (S5b policy).
    referenced = {
        alias
        for section in report.sections
        for sentence in section.sentences
        for alias in sentence.claim_ids
    }
    for alias, info in claims.items():
        if (
            info.importance == "high"
            and info.status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE
            and alias not in referenced
        ):
            markers = _markers_for(info.supported_chunks, aliases)
            if not markers:
                continue
            restored = PlacedSentence(
                text=RESTORED_PREFIX + info.text,
                kind="finding",
                placement=Placement.QUALIFIED
                if info.pool is PoolStatus.PARTIALLY_SUPPORTED
                else Placement.BODY,
                markers=markers,
                claim_aliases=(alias,),
                status=info.status,
                origin="restored",
            )
            target = next((s for s in sections if s.subquestion == info.subquestion), None)
            if target is None:
                if not sections:
                    sections.append(PlacedSection(info.subquestion or "SYNTHESIS", "Findings", []))
                target = sections[-1]
            target.sentences.append(restored)
            record(
                "omission_restored",
                target.heading,
                None,
                info.text,
                f"high-importance claim {alias} was supported by its cited source but absent",
            )

    # V7b — a claim that cannot stand as a finding (contested, insufficient, citation
    # not confirmed, not evaluated) and that the revision left out entirely is still
    # LISTED, with its verified text, where its status puts it: omitting it from the
    # body is right, making it disappear is not (S5b). Nothing is deleted silently.
    for alias, info in claims.items():
        if alias in referenced or info.status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE:
            continue
        important = info.importance == "high" or info.is_core
        where = place(info.status, partial=False, important=important)
        labelled.setdefault(where, []).append(PlacedSentence(
            text=info.text, kind="finding", placement=where, markers=(),
            claim_aliases=(alias,), status=info.status, origin="restored",
        ))
        record("omission_listed", where.value, None, info.text,
               f"claim {alias} ({info.status.value}) was left out by the revision; listed")

    return _outcome(
        report.title,
        sections,
        labelled,
        actions,
        needs,
        claims,
        aliases,
        config,
        core_subquestions,
        verification_coverage,
        proposed,
        lost,
    )


def _place_sentence(
    sentence: ReportSentence,
    key,
    heading,
    aliases,
    claims,
    config,
    entities,
    embed,
    reverified,
    record,
    needs,
) -> PlacedSentence:
    t_index = key[1]
    # V1 — every alias must be in the code-owned table.
    claim_aliases = []
    for alias in sentence.claim_ids:
        if alias in claims and alias in aliases.claims:
            claim_aliases.append(alias)
        else:
            record("reference_rejected", heading, t_index, sentence.text, f"unknown claim {alias}")
    evidence = []
    for alias in sentence.evidence_ids:
        ref = aliases.passages.get(alias)
        if ref is not None and ref.source_label in aliases.sources:
            evidence.append(ref)
        else:
            record(
                "reference_rejected", heading, t_index, sentence.text, f"unknown passage {alias}"
            )

    kind = sentence.kind
    has_number = bool(extract_quantities(sentence.text, entities))
    if kind == "transition" and (has_number or claim_aliases or evidence):
        kind = "finding"  # a "transition" that asserts something is treated as a finding
    if not claim_aliases:
        if kind == "transition" or (kind == "uncertainty" and not has_number and not evidence):
            return PlacedSentence(sentence.text, kind, Placement.BODY, (), (), None, key=key)
        # V5 — an assertion that traces to no claim.
        record(
            "untraceable_statement",
            heading,
            t_index,
            sentence.text,
            "a finding with no valid claim id",
        )
        return PlacedSentence(sentence.text, kind, Placement.UNRESOLVED, (), (), None, key=key)

    infos = [claims[alias] for alias in claim_aliases]
    important = any(i.importance == "high" or i.is_core for i in infos)
    # V2 — a sentence that no longer means what was verified is re-verified first.
    changes = detect_meaning_change(
        sentence.text,
        [i.text for i in infos],
        entities=entities,
        embed=embed,
        min_cosine=config.meaning_change_min_cosine,
    )
    if changes:
        record("meaning_changed", heading, t_index, sentence.text, ",".join(sorted(changes)))
        result = reverified.get(key)
        if result is None:
            needs.append(key)
            return PlacedSentence(
                sentence.text,
                kind,
                Placement.NOT_EVALUATED,
                (),
                tuple(claim_aliases),
                ClaimStatus.NOT_EVALUATED,
                key=key,
            )
        status, partial, supported = (
            result.status,
            result.pool is PoolStatus.PARTIALLY_SUPPORTED,
            result.supported_chunks,
        )
    else:
        status = _most_cautious([i.status for i in infos])
        partial = any(i.pool is PoolStatus.PARTIALLY_SUPPORTED for i in infos)
        supported = frozenset().union(*(i.supported_chunks for i in infos))

    placement = place(status, partial=partial, important=important)
    if placement not in IN_BODY:
        return PlacedSentence(
            sentence.text, kind, placement, (), tuple(claim_aliases), status, key=key
        )

    # V3 — a marker renders only for a passage established as supporting.
    markers = []
    for ref in evidence:
        if ref.chunk_id in supported:
            if ref.source_label not in markers:
                markers.append(ref.source_label)
        else:
            record(
                "marker_removed",
                heading,
                t_index,
                sentence.text,
                f"{ref.source_label}: its passage was not established as supporting",
            )
    if not markers:
        # Supported elsewhere, but not by what this sentence cites: never substitute
        # another source — the sentence moves to the labelled section instead.
        return PlacedSentence(
            sentence.text,
            kind,
            Placement.CITATION_NOT_CONFIRMED,
            (),
            tuple(claim_aliases),
            ClaimStatus.CITATION_NOT_CONFIRMED,
            key=key,
        )
    text = sentence.text
    if placement is Placement.QUALIFIED and kind != "qualification":
        text = QUALIFIER_PREFIX + text
        record("qualifier_added", heading, t_index, sentence.text, "partially supported claim")
    return PlacedSentence(
        text, kind, placement, tuple(markers), tuple(claim_aliases), status, key=key
    )


def _markers_for(chunks: frozenset[uuid.UUID], aliases: AliasTable) -> tuple[str, ...]:
    labels: list[str] = []
    for ref in aliases.passages.values():
        if (
            ref.chunk_id in chunks
            and ref.source_label in aliases.sources
            and ref.source_label not in labels
        ):
            labels.append(ref.source_label)
    return tuple(sorted(labels, key=lambda label: int(label[1:]) if label[1:].isdigit() else 0))


def _flag_duplicates(sections, config, embed, record) -> None:
    placed = [(s.heading, p) for s in sections for p in s.sentences if p.claim_aliases]
    seen: dict[frozenset[str], str] = {}
    for heading, sentence in placed:
        ids = frozenset(sentence.claim_aliases)
        if ids in seen:
            record(
                "duplicate_flagged",
                heading,
                sentence.key[1] if sentence.key else None,
                sentence.text,
                f"states the same claims as a sentence in '{seen[ids]}'",
            )
        else:
            seen[ids] = heading
    if embed is None or len(placed) < 2:
        return
    vectors = embed([sentence.text for _, sentence in placed])
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            if frozenset(placed[i][1].claim_aliases) == frozenset(placed[j][1].claim_aliases):
                continue  # already flagged above
            if cosine(vectors[i], vectors[j]) >= config.duplicate_min_cosine:
                heading, sentence = placed[j]
                record(
                    "duplicate_flagged",
                    heading,
                    sentence.key[1] if sentence.key else None,
                    sentence.text,
                    "near-duplicate wording of an earlier body sentence",
                )


def _outcome(
    title,
    sections,
    labelled,
    actions,
    needs,
    claims,
    aliases,
    config,
    core_subquestions,
    verification_coverage,
    proposed,
    lost,
) -> ValidatedReport:
    body = [p for s in sections for p in s.sentences]
    body_loss_ratio = round(lost / proposed, 6) if proposed else 0.0
    covered = {
        s.subquestion for s in sections if any(p.markers and p.claim_aliases for p in s.sentences)
    }
    core_coverage = (
        round(len(covered & core_subquestions) / len(core_subquestions), 6)
        if core_subquestions
        else None
    )
    leaked = sorted({m for p in body for m in p.markers if m not in aliases.sources})
    supported_in_body = any(
        p.status is ClaimStatus.SUPPORTED_BY_CITED_SOURCE and p.markers for p in body
    )
    unresolved_important = sorted(
        alias
        for alias, info in claims.items()
        if info.importance == "high"
        and info.status in (ClaimStatus.INSUFFICIENT_EVIDENCE, ClaimStatus.NOT_EVALUATED)
    )

    failed: list[str] = []
    if body_loss_ratio > config.fail_body_loss_ratio:
        failed.append(f"body_loss_ratio {body_loss_ratio} > {config.fail_body_loss_ratio}")
    if not supported_in_body:
        failed.append("no body sentence is supported by its cited source")
    if core_coverage is not None and core_coverage < config.min_core_coverage:
        failed.append(f"core coverage {core_coverage} < {config.min_core_coverage}")
    if leaked:
        failed.append(f"rejected references survived rendering: {leaked}")

    degraded: list[str] = []
    if body_loss_ratio > config.degraded_body_loss_ratio:
        degraded.append(f"body_loss_ratio {body_loss_ratio} > {config.degraded_body_loss_ratio}")
    if unresolved_important:
        degraded.append(
            f"high-importance claims unresolved or not evaluated: {unresolved_important}"
        )
    if verification_coverage < 1.0:
        degraded.append(f"verification coverage {verification_coverage} < 1.0")
    if needs:
        degraded.append(f"{len(needs)} sentence(s) await re-verification")

    status: OutcomeStatus = (
        "finalization_failed" if failed else "degraded" if degraded else "complete"
    )
    return ValidatedReport(
        title=title,
        sections=sections,
        labelled=labelled,
        actions=actions,
        needs_reverification=needs,
        status=status,
        reasons=failed + degraded,
        metrics={
            "proposed_findings": proposed,
            "findings_not_in_body": lost,
            "body_loss_ratio": body_loss_ratio,
            "core_coverage": core_coverage,
            "body_sentences": len(body),
            "restored": sum(1 for p in body if p.origin == "restored"),
            "actions": {
                name: sum(1 for a in actions if a.action == name)
                for name in sorted({a.action for a in actions})
            },
        },
    )
