"""Final-report validation (V1–V7, outcome status) and rendering.

Includes the design's required regression scenario: a verified draft claim whose
number, hedge or scope changes during revision must be detected and re-evaluated —
never presented in the body with its changed meaning.
"""

import re
import uuid

import pytest

from app.services.quality.models import (
    AliasTable,
    ClaimStatus,
    PassageRef,
    Placement,
    PoolStatus,
    ReportSectionDraft,
    ReportSentence,
    StructuredReport,
)
from app.services.quality.rendering import render_draft, render_final, with_markers
from app.services.quality.validation import (
    QUALIFIER_PREFIX,
    RESTORED_PREFIX,
    ClaimInfo,
    Reverification,
    ValidationConfig,
    detect_meaning_change,
    validate_final,
)

CHUNK = {n: uuid.uuid5(uuid.NAMESPACE_URL, f"chunk-{n}") for n in range(1, 10)}
CONFIG = ValidationConfig(
    degraded_body_loss_ratio=0.10,
    fail_body_loss_ratio=0.25,
    min_core_coverage=0.5,
    meaning_change_min_cosine=0.90,
    duplicate_min_cosine=0.92,
)
S = ClaimStatus
P = PoolStatus

VERIFIED = "Moderate coffee intake may reduce type 2 diabetes risk by 15% in adults."


def aliases(n_claims: int = 9) -> AliasTable:
    return AliasTable(
        sources={f"S{i}": uuid.uuid4() for i in range(1, 4)},
        passages={
            "E1": PassageRef(CHUNK[1], "S1"),
            "E2": PassageRef(CHUNK[2], "S1"),
            "E3": PassageRef(CHUNK[3], "S2"),
            "E4": PassageRef(CHUNK[4], "S3"),
        },
        claims={f"C{i}": uuid.uuid4() for i in range(1, n_claims + 1)},
    )


def claim(
    alias, text, status, pool=P.SUPPORTED, importance="medium", sub="Q1", core=True, chunks=()
):
    return ClaimInfo(
        alias, text, importance, sub, core, status, pool, frozenset(CHUNK[c] for c in chunks)
    )


def said(text, kind="finding", claims=(), evidence=()):
    return ReportSentence(
        text=text, kind=kind, claim_ids=list(claims), evidence_ids=list(evidence), finding_refs=[]
    )


def report(*sections) -> StructuredReport:
    return StructuredReport(
        title="Coffee and health",
        sections=[
            ReportSectionDraft(subquestion=sub, heading=h, sentences=list(s))
            for sub, h, s in sections
        ],
    )


def run(rep, claims, *, core=frozenset({"Q1"}), coverage=1.0, reverified=None, table=None):
    return validate_final(
        rep,
        table or aliases(),
        {c.alias: c for c in claims},
        config=CONFIG,
        core_subquestions=core,
        verification_coverage=coverage,
        reverified=reverified,
    )


def placements(validated):
    found = {}
    for section in validated.sections:
        for p in section.sentences:
            found[p.text] = p.placement
    for placement, items in validated.labelled.items():
        for p in items:
            found[p.text] = placement
    return found


class TestPlacementAndRepairs:
    claims = [
        claim("C1", VERIFIED, S.SUPPORTED_BY_CITED_SOURCE, importance="high", chunks=[1]),
        claim(
            "C2",
            "Green tea was associated with lower stroke risk.",
            S.SUPPORTED_BY_CITED_SOURCE,
            pool=P.PARTIALLY_SUPPORTED,
            sub="Q2",
            chunks=[3],
        ),
        claim(
            "C3",
            "Caffeine improves marathon times.",
            S.INSUFFICIENT_EVIDENCE,
            pool=P.NOT_ENOUGH_EVIDENCE,
            importance="high",
            sub="Q2",
        ),
        claim(
            "C4",
            "Coffee raises blood pressure.",
            S.INSUFFICIENT_EVIDENCE,
            pool=P.UNSUPPORTED,
            importance="low",
            core=False,
        ),
        claim("C5", "Coffee lowers mortality.", S.CONTESTED, pool=P.CONTRADICTED),
        claim(
            "C6", "Decaffeinated coffee has the same effect.", S.CITATION_NOT_CONFIRMED, chunks=[9]
        ),
    ]
    rep = report(
        (
            "Q1",
            "Diabetes",
            [
                said(VERIFIED, claims=["C1"], evidence=["E1", "E2"]),
                said("Coffee raises blood pressure.", claims=["C4"], evidence=["E2"]),
                said("Coffee lowers mortality.", claims=["C5"], evidence=["E1"]),
            ],
        ),
        (
            "Q2",
            "Stroke",
            [
                said(
                    "Green tea was associated with lower stroke risk.",
                    claims=["C2"],
                    evidence=["E3"],
                ),
                said("Caffeine improves marathon times.", claims=["C3"], evidence=["E3"]),
                said("Decaffeinated coffee has the same effect.", claims=["C6"], evidence=["E4"]),
                said("Taken together, the picture is mixed.", kind="transition"),
                said("Experts agree coffee is healthy.", claims=[], evidence=["E1"]),
                said("A hidden claim.", claims=["C99"], evidence=["E99"]),
            ],
        ),
    )

    def test_every_sentence_lands_where_its_status_says(self):
        found = placements(run(self.rep, self.claims))
        assert found[with_markers(VERIFIED, [])] is Placement.BODY
        assert (
            found[QUALIFIER_PREFIX + "Green tea was associated with lower stroke risk."]
            is Placement.QUALIFIED
        )
        assert found["Caffeine improves marathon times."] is Placement.UNRESOLVED  # important
        assert found["Coffee raises blood pressure."] is Placement.UNVERIFIED_LIST  # minor
        assert found["Coffee lowers mortality."] is Placement.CONTESTED
        assert (
            found["Decaffeinated coffee has the same effect."] is Placement.CITATION_NOT_CONFIRMED
        )
        assert found["Taken together, the picture is mixed."] is Placement.BODY
        assert found["Experts agree coffee is healthy."] is Placement.UNRESOLVED  # untraceable
        assert found["A hidden claim."] is Placement.UNRESOLVED

    def test_markers_render_only_for_passages_established_as_supporting(self):
        validated = run(self.rep, self.claims)
        body = {p.text: p for s in validated.sections for p in s.sentences}
        assert body[VERIFIED].markers == ("S1",)  # E1 supports; E2 (same source) does not
        removed = [a for a in validated.actions if a.action == "marker_removed"]
        assert any("S1" in a.detail and a.original_text == VERIFIED for a in removed)

    def test_unconfirmed_citation_is_never_replaced_by_another_source(self):
        validated = run(self.rep, self.claims)
        moved = validated.labelled[Placement.CITATION_NOT_CONFIRMED]
        assert [p.markers for p in moved] == [()]
        assert all("S3" not in p.markers for s in validated.sections for p in s.sentences)

    def test_fabricated_references_are_rejected_and_recorded(self):
        validated = run(self.rep, self.claims)
        rejected = [a.detail for a in validated.actions if a.action == "reference_rejected"]
        assert "unknown claim C99" in rejected and "unknown passage E99" in rejected

    def test_nothing_is_dropped_every_move_is_recorded_with_its_original_text(self):
        validated = run(self.rep, self.claims)
        all_text = set(placements(validated))
        originals = {s.text for sec in self.rep.sections for s in sec.sentences}
        assert all(any(o in t for t in all_text) for o in originals)
        moved = {a.original_text for a in validated.actions if a.action == "moved"}
        assert "Coffee lowers mortality." in moved

    def test_too_much_lost_from_the_body_fails_safely(self):
        validated = run(self.rep, self.claims)
        assert validated.status == "finalization_failed"
        assert any("body_loss_ratio" in reason for reason in validated.reasons)
        with pytest.raises(ValueError, match="finalization failed"):
            render_final(validated)


class TestRegressionMeaningChangedDuringRevision:
    """A verified draft claim, rewritten by the revision model."""

    verified = claim("C1", VERIFIED, S.SUPPORTED_BY_CITED_SOURCE, importance="high", chunks=[1])

    def revise(self, text, reverified=None):
        rep = report(("Q1", "Diabetes", [said(text, claims=["C1"], evidence=["E1"])]))
        return run(rep, [self.verified], reverified=reverified)

    @pytest.mark.parametrize(
        ("revised", "change"),
        [
            ("Moderate coffee intake may reduce type 2 diabetes risk by 25% in adults.", "number"),
            (
                "Moderate coffee intake reduces type 2 diabetes risk by 15% in adults.",
                "qualification",
            ),
            (
                "Moderate coffee intake may reduce type 2 diabetes risk by 15% in all populations.",
                "scope",
            ),
        ],
    )
    def test_change_is_detected_and_sent_for_reverification_never_into_the_body(
        self, revised, change
    ):
        validated = self.revise(revised)
        flagged = [a for a in validated.actions if a.action == "meaning_changed"]
        assert flagged and change in flagged[0].detail
        assert validated.needs_reverification == [(0, 0)]
        body_texts = [p.text for s in validated.sections for p in s.sentences]
        assert revised not in body_texts
        assert validated.labelled[Placement.NOT_EVALUATED][0].text == revised
        # The verified original is restored to the body instead (V7), with its citation.
        restored = [p for s in validated.sections for p in s.sentences if p.origin == "restored"]
        assert [(p.text, p.markers) for p in restored] == [(RESTORED_PREFIX + VERIFIED, ("S1",))]
        # First-pass outcome is provisional and says why; restorations do not reduce
        # body_loss_ratio (design §5), so a one-finding report fails safe here.
        assert validated.status == "finalization_failed"
        assert any("await re-verification" in reason for reason in validated.reasons)

    def test_reverified_result_decides_on_the_second_pass(self):
        changed = "Moderate coffee intake may reduce type 2 diabetes risk by 25% in adults."
        failed = self.revise(
            changed,
            {(0, 0): Reverification(S.INSUFFICIENT_EVIDENCE, P.NOT_ENOUGH_EVIDENCE, frozenset())},
        )
        assert failed.needs_reverification == []
        assert failed.labelled[Placement.UNRESOLVED][0].text == changed
        upheld = self.revise(
            changed,
            {
                (0, 0): Reverification(
                    S.SUPPORTED_BY_CITED_SOURCE, P.SUPPORTED, frozenset({CHUNK[1]})
                )
            },
        )
        body = [p for s in upheld.sections for p in s.sentences]
        assert [(p.text, p.markers) for p in body] == [(changed, ("S1",))]
        assert upheld.status == "complete"

    def test_a_pure_paraphrase_is_not_reverified(self):
        paraphrase = "In adults, moderate coffee intake may cut type 2 diabetes risk by 15%."
        validated = self.revise(paraphrase)
        assert not [a for a in validated.actions if a.action == "meaning_changed"]
        assert validated.needs_reverification == []
        body = [p for s in validated.sections for p in s.sentences]
        assert [(p.text, p.markers) for p in body] == [(paraphrase, ("S1",))]
        assert validated.status == "complete"


class TestMeaningChangeSignals:
    def test_negation(self):
        assert "negation" in detect_meaning_change(
            "Coffee does not raise risk.", ["Coffee raises risk."], min_cosine=0.9
        )

    def test_wording_only_with_an_embedder(self):
        def far(texts):
            return [[1.0, 0.0], [0.0, 1.0]]

        assert detect_meaning_change("A b.", ["A b."], min_cosine=0.9) == set()
        assert detect_meaning_change("A b.", ["A b."], embed=far, min_cosine=0.9) == {"wording"}


class TestOutcomeThresholds:
    """body_loss_ratio is strictly-greater-than: exactly 0.10 is complete, exactly 0.25 is
    degraded (not failed), above 0.25 fails. All values provisional."""

    def build(self, kept: int, lost: int):
        claims, sentences = [], []
        for i in range(1, kept + 1):
            claims.append(
                claim(
                    f"C{i}", f"Finding number {i} holds.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[1]
                )
            )
            sentences.append(said(f"Finding number {i} holds.", claims=[f"C{i}"], evidence=["E1"]))
        for j in range(kept + 1, kept + lost + 1):
            claims.append(
                claim(
                    f"C{j}",
                    f"Weak point {j} holds.",
                    S.INSUFFICIENT_EVIDENCE,
                    pool=P.NOT_ENOUGH_EVIDENCE,
                    importance="low",
                    core=False,
                )
            )
            sentences.append(said(f"Weak point {j} holds.", claims=[f"C{j}"], evidence=["E2"]))
        return run(report(("Q1", "All", sentences)), claims, table=aliases(kept + lost))

    @pytest.mark.parametrize(
        ("kept", "lost", "status"),
        [(9, 1, "complete"), (3, 1, "degraded"), (7, 3, "finalization_failed"), (5, 0, "complete")],
    )
    def test_edges(self, kept, lost, status):
        validated = self.build(kept, lost)
        assert validated.status == status, validated.reasons

    def test_incomplete_verification_degrades(self):
        claims = [claim("C1", "Finding one holds.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[1])]
        rep = report(("Q1", "All", [said("Finding one holds.", claims=["C1"], evidence=["E1"])]))
        validated = run(rep, claims, coverage=0.8)
        assert validated.status == "degraded" and "verification coverage" in validated.reasons[0]

    def test_uncovered_core_subquestions_fail(self):
        claims = [claim("C1", "Finding one holds.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[1])]
        rep = report(("Q1", "All", [said("Finding one holds.", claims=["C1"], evidence=["E1"])]))
        validated = run(rep, claims, core=frozenset({"Q1", "Q2", "Q3"}))
        assert validated.status == "finalization_failed"
        assert any("core coverage" in r for r in validated.reasons)

    def test_duplicates_are_flagged_not_removed(self):
        claims = [claim("C1", "Finding one holds.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[1])]
        rep = report(
            (
                "Q1",
                "All",
                [
                    said("Finding one holds.", claims=["C1"], evidence=["E1"]),
                    said("Finding one holds.", claims=["C1"], evidence=["E1"]),
                ],
            )
        )
        validated = run(rep, claims)
        assert sum(1 for s in validated.sections for _ in s.sentences) == 2
        assert [a.action for a in validated.actions] == ["duplicate_flagged"]


class TestRendering:
    def test_markers_go_before_closing_punctuation(self):
        assert with_markers("It holds.", ["S1", "S3"]) == "It holds [S1][S3]."
        assert with_markers("It holds", ["S2"]) == "It holds [S2]"
        assert with_markers("It holds.", []) == "It holds."

    def test_draft_markers_come_from_the_alias_table_and_unknowns_are_dropped(self):
        rep = report(
            (
                "Q1",
                "## Diabetes\n",
                [
                    said("One.", evidence=["E1", "E2", "E3"]),
                    said("Two.", evidence=["E7"]),
                ],
            )
        )
        markdown, rejected = render_draft(rep, aliases())
        assert markdown == "# Coffee and health\n\n## Diabetes\n\nOne [S1][S2]. Two.\n"
        assert rejected == ["E7"]

    def test_the_model_has_no_field_that_can_carry_source_metadata(self):
        assert set(ReportSentence.model_fields) == {
            "text",
            "kind",
            "claim_ids",
            "evidence_ids",
            "finding_refs",
        }
        assert set(ReportSectionDraft.model_fields) == {"subquestion", "heading", "sentences"}
        assert set(StructuredReport.model_fields) == {"title", "sections"}

    def test_final_report_shows_sections_and_banner_but_no_audit_internals(self):
        claims = [
            claim("C1", "Finding one holds.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[1]),
            claim(
                "C2",
                "Point two is open.",
                S.INSUFFICIENT_EVIDENCE,
                pool=P.NOT_ENOUGH_EVIDENCE,
                importance="high",
            ),
        ]
        rep = report(
            (
                "Q1",
                "All",
                [
                    said("Finding one holds.", claims=["C1"], evidence=["E1"]),
                    said("Point two is open.", claims=["C2"], evidence=["E2"], kind="uncertainty"),
                ],
            )
        )
        validated = run(rep, claims)
        assert validated.status == "degraded"
        text = render_final(validated)
        assert "> **Degraded report.**" in text
        assert "Finding one holds [S1]." in text
        assert "## Unresolved findings" in text and "- Point two is open." in text
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)  # no ids from the audit
        assert "C1" not in text and "E1" not in text
