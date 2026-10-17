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
    PARTIAL_PREFIX,
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
        # Partly supported and not narrowed: unresolved, never qualified into the body
        # (stage 37 policy; previously Placement.QUALIFIED).
        assert (
            found[PARTIAL_PREFIX + "Green tea was associated with lower stroke risk."]
            is Placement.UNRESOLVED
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
        # The verified original is NOT restored (S5b policy): the revision rewrote this
        # claim, and putting the outdated version back would misrepresent the report.
        restored = [p for s in validated.sections for p in s.sentences if p.origin == "restored"]
        assert restored == []
        # First-pass outcome is provisional and says why; a one-finding report whose
        # only finding awaits re-verification fails safe here.
        assert validated.status == "finalization_failed"
        assert any("await re-verification" in reason for reason in validated.reasons)

    def test_reverified_support_through_an_uncited_chunk_never_reaches_the_body(self):
        """Safety invariant (S2 approval): a flagged sentence is treated as verified only
        when re-verification succeeded against the exact evidence it cites (E1 here)."""
        changed = "Moderate coffee intake may reduce type 2 diabetes risk by 25% in adults."
        elsewhere = self.revise(
            changed,
            {
                (0, 0): Reverification(
                    S.SUPPORTED_BY_CITED_SOURCE, P.SUPPORTED, frozenset({CHUNK[2]})
                )
            },
        )
        body_texts = [p.text for s in elsewhere.sections for p in s.sentences]
        assert changed not in body_texts
        [moved] = elsewhere.labelled[Placement.CITATION_NOT_CONFIRMED]
        assert (moved.text, moved.markers) == (changed, ())

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

    def test_an_important_verified_claim_the_revision_omitted_is_restored(self):
        """V7 still restores a pure omission: no revised sentence states the claim."""
        other = claim(
            "C2", "Tea intake was not linked to stroke.", S.SUPPORTED_BY_CITED_SOURCE, chunks=[3]
        )
        rep = report(
            (
                "Q1",
                "Diabetes",
                [said("Tea intake was not linked to stroke.", claims=["C2"], evidence=["E3"])],
            )
        )
        validated = run(rep, [self.verified, other])
        restored = [p for s in validated.sections for p in s.sentences if p.origin == "restored"]
        assert [(p.text, p.markers) for p in restored] == [(RESTORED_PREFIX + VERIFIED, ("S1",))]

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


class TestRun1IntegrityRegressions:
    """Stage 37: the report-integrity failures seen in the first paid development run
    (docs/REPORT_QUALITY_ERROR_ANALYSIS.md, run 1, F1-F3), reproduced in miniature."""

    OVERSTATED = (
        "Moderate coffee consumption (3 to 5 cups daily) is highly beneficial for "
        "cardiovascular health in all adults."
    )
    HEDGED = (
        "However, moderate coffee consumption (3 to 5 cups daily) is highly beneficial for "
        "cardiovascular health in all adults, albeit with some qualification regarding its "
        "benefits."
    )
    # Demonstrably narrower (stage 40): every restriction kept, an age range added.
    NARROWED = (
        "Moderate coffee consumption (3 to 5 cups daily) is highly beneficial for "
        "cardiovascular health in all adults aged 45 to 65."
    )
    # The stage-37 example: a reformulation ("highly beneficial" -> "associated with
    # better") that the fail-closed test no longer accepts as proof of narrowing.
    REFORMULATED = (
        "Moderate coffee consumption (3 to 5 cups daily) was associated with better "
        "cardiovascular health in adults aged 45 to 65."
    )
    MORTALITY = "One to three cups a day was linked to about 15% lower mortality."
    SWEETENED = "Even sweetened coffee was associated with health benefits."
    CARDIO = "Coffee was linked to lower heart disease risk."

    def partial(self):
        return claim(
            "C1",
            self.OVERSTATED,
            S.SUPPORTED_BY_CITED_SOURCE,
            pool=P.PARTIALLY_SUPPORTED,
            importance="high",
            chunks=[1],
        )

    def supported(self):
        return claim("C2", self.CARDIO, S.SUPPORTED_BY_CITED_SOURCE, chunks=[3])

    def validate(self, rep, claims, *, core=frozenset({"Q1"}), reverified=None, **kwargs):
        return validate_final(
            rep,
            aliases(),
            {c.alias: c for c in claims},
            config=CONFIG,
            core_subquestions=core,
            verification_coverage=1.0,
            reverified=reverified,
            subquestion_headings={
                "Q1": "Cardiovascular benefits?",
                "Q2": "Mental-health risks?",
                "Q3": "Variation by age?",
            },
            **kwargs,
        )

    def cardio(self, *sentences):
        return report(("Q1", "Cardiovascular benefits", list(sentences)))

    def cardio_only(self):
        return self.cardio(said(self.CARDIO, claims=["C2"], evidence=["E3"]))

    @staticmethod
    def body(validated):
        return [p for s in validated.sections for p in s.sentences]

    # --- F3: partial support -------------------------------------------------------

    def test_a_hedge_on_a_partly_supported_claim_is_not_reverified_and_not_in_the_body(self):
        rep = self.cardio(said(self.HEDGED, kind="qualification", claims=["C1"], evidence=["E1"]))
        # Even a "supported" re-verification result for this sentence cannot rescue it:
        # a hedge is never sent for re-verification, so no result is consulted.
        upgrade = {
            (0, 0): Reverification(S.SUPPORTED_BY_CITED_SOURCE, P.SUPPORTED, frozenset({CHUNK[1]}))
        }
        for reverified in (None, upgrade):
            validated = self.validate(rep, [self.partial()], reverified=reverified)
            assert validated.needs_reverification == []
            assert all(p.text != self.HEDGED for p in self.body(validated))
            [listed] = validated.labelled[Placement.UNRESOLVED]
            assert listed.text == PARTIAL_PREFIX + self.HEDGED and listed.markers == ()
            hedges = [a for a in validated.actions if a.action == "superficial_hedge"]
            assert len(hedges) == 1

    def test_an_unchanged_partly_supported_claim_is_unresolved(self):
        rep = self.cardio(said(self.OVERSTATED, claims=["C1"], evidence=["E1"]))
        validated = self.validate(rep, [self.partial()])
        assert self.body(validated) == []
        listed = validated.labelled[Placement.UNRESOLVED][0]
        assert listed.text == PARTIAL_PREFIX + self.OVERSTATED
        assert any(a.action == "partial_not_narrowed" for a in validated.actions)

    def test_a_narrowed_rewrite_reaches_the_body_only_if_reverified_as_fully_supported(self):
        rep = self.cardio(said(self.NARROWED, kind="qualification", claims=["C1"], evidence=["E1"]))
        first = self.validate(rep, [self.partial()])
        assert first.needs_reverification == [(0, 0)]  # narrowed: sent to re-verification

        def with_result(pool):
            result = Reverification(S.SUPPORTED_BY_CITED_SOURCE, pool, frozenset({CHUNK[1]}))
            return self.validate(rep, [self.partial()], reverified={(0, 0): result})

        still_partial = with_result(P.PARTIALLY_SUPPORTED)
        listed = still_partial.labelled[Placement.UNRESOLVED][0]
        assert listed.text == PARTIAL_PREFIX + self.NARROWED
        assert self.body(still_partial) == []

        upheld = with_result(P.SUPPORTED)
        assert [(p.text, p.markers, p.placement) for p in self.body(upheld)] == [
            (self.NARROWED, ("S1",), Placement.BODY)
        ]

    def test_narrowing_heuristic(self):
        from app.services.quality.validation import narrowed

        assert not narrowed(self.HEDGED, [self.OVERSTATED])  # run 1's exact rewrite
        assert narrowed(self.NARROWED, [self.OVERSTATED])  # an age restriction added
        assert not narrowed(self.REFORMULATED, [self.OVERSTATED])  # reworded: fail closed
        assert narrowed("Coffee lowers risk in women.", ["Coffee lowers risk."])  # scope added

    def test_an_omitted_important_partial_claim_is_listed_not_restored_to_the_body(self):
        validated = self.validate(self.cardio_only(), [self.partial(), self.supported()])
        assert all(self.OVERSTATED not in p.text for p in self.body(validated))
        listed = validated.labelled[Placement.UNRESOLVED][0]
        assert listed.text == PARTIAL_PREFIX + self.OVERSTATED

    # --- F1: claims stay with their question; nothing silently lost ---------------------

    def test_a_restored_finding_goes_under_its_own_question_not_the_last_section(self):
        mortality = claim(
            "C6",
            self.MORTALITY,
            S.SUPPORTED_BY_CITED_SOURCE,
            importance="high",
            sub="Q3",
            core=False,
            chunks=[4],
        )
        validated = self.validate(self.cardio_only(), [self.supported(), mortality])
        q1, q3 = validated.sections
        assert [p.text for p in q1.sentences] == [self.CARDIO]  # untouched
        assert (q3.subquestion, q3.heading) == ("Q3", "Variation by age?")
        assert [(p.text, p.markers) for p in q3.sentences] == [
            (RESTORED_PREFIX + self.MORTALITY, ("S3",))
        ]

    def test_every_claim_is_shown_or_explicitly_recorded_as_omitted(self):
        sweetened = claim(
            "C9", self.SWEETENED, S.SUPPORTED_BY_CITED_SOURCE, sub="Q3", core=False, chunks=[4]
        )  # supported, not high-importance
        validated = self.validate(self.cardio_only(), [self.supported(), sweetened])
        [recorded] = [a for a in validated.actions if a.action == "omission_recorded"]
        assert recorded.original_text == self.SWEETENED and "C9" in recorded.detail
        assert validated.metrics["claims_omitted"] == ["C9"]
        # not restored merely to look complete
        assert all(self.SWEETENED not in p.text for p in self.body(validated))

    def test_restored_sections_are_recorded(self):
        validated = self.validate(
            self.cardio_only(), [self.supported()], restored_sections=[("Q3", "Age")]
        )
        [action] = [a for a in validated.actions if a.action == "section_restored"]
        assert action.section == "Age" and "Q3" in action.detail

    # --- F2: an unanswered core question is disclosed -------------------------------

    def test_an_unanswered_core_question_is_a_gap_even_at_the_coverage_threshold(self):
        validated = self.validate(
            self.cardio_only(), [self.supported()], core=frozenset({"Q1", "Q2"})
        )
        # coverage 0.5 equals the provisional minimum: not a failure, but never complete
        assert validated.metrics["core_coverage"] == 0.5
        assert validated.unanswered_core == ["Q2"]
        assert validated.status == "degraded"
        assert any("not answered" in r and "Q2" in r for r in validated.reasons)
        markdown = render_final(
            validated, subquestion_texts={"Q2": "What are the mental-health risks?"}
        )
        gaps = markdown.split("## Evidence gaps", 1)[1]
        assert "What are the mental-health risks?" in gaps


class TestP1NarrowingRegressions:
    """Stage 40: pilot finding P1 (docs/REPORT_QUALITY_ERROR_ANALYSIS.md, Pilot). A partly
    supported claim reaches the body only if its rewrite is DEMONSTRABLY narrower and
    the citation-specific re-check fully supports that exact rewrite."""

    P1_CLAIM = (
        "High coffee consumption (≥ 3 cups/day) was inversely associated with mortality "
        "and disability."
    )
    P1_REWRITE = (
        "High coffee consumption was inversely associated with mortality and disability "
        "with some qualification."
    )
    #: Four supported findings beside the partial one, so holding one back leaves a
    #: renderable (degraded) report rather than a finalization failure.
    OTHERS = (
        ("C2", "Tea intake was not linked to stroke.", "E3", 3),
        ("C3", "Green tea drinkers reported better sleep.", "E3", 3),
        ("C4", "Decaffeinated coffee showed similar associations.", "E4", 4),
        ("C5", "Milk added to coffee did not change the association.", "E4", 4),
    )
    FULL = Reverification(S.SUPPORTED_BY_CITED_SOURCE, P.SUPPORTED, frozenset({CHUNK[1]}))

    def validate(self, claim_text, rewrite, reverified=None):
        partial = claim(
            "C1", claim_text, S.SUPPORTED_BY_CITED_SOURCE, pool=P.PARTIALLY_SUPPORTED,
            importance="high", chunks=[1],
        )  # fmt: skip
        others = [
            claim(alias, text, S.SUPPORTED_BY_CITED_SOURCE, chunks=[chunk])
            for alias, text, _, chunk in self.OTHERS
        ]
        rep = report(
            ("Q1", "Mortality", [
                said(rewrite, kind="qualification", claims=["C1"], evidence=["E1"]),
                *(said(text, claims=[alias], evidence=[passage])
                  for alias, text, passage, _ in self.OTHERS),
            ])
        )  # fmt: skip
        reverified = {(0, 0): reverified} if reverified is not None else None
        return run(rep, [partial, *others], reverified=reverified)

    @staticmethod
    def body_texts(validated):
        return [p.text for s in validated.sections for p in s.sentences]

    def assert_held_back(self, claim_text, rewrite, action="not_narrowed"):
        """Not narrower: never re-verified, never in the body, listed as unresolved —
        even when a fully supporting re-check result is on offer."""
        for offered in (None, self.FULL):
            validated = self.validate(claim_text, rewrite, offered)
            assert validated.needs_reverification == []
            assert rewrite not in " ".join(self.body_texts(validated))
            [listed] = validated.labelled[Placement.UNRESOLVED]
            assert listed.text == PARTIAL_PREFIX + rewrite and listed.markers == ()
            assert [a.action for a in validated.actions if a.action == action] == [action]
            markdown = render_final(validated)
            body, _, rest = markdown.partition("\n---\n")
            assert rewrite not in body and "[S1]" not in body
            assert PARTIAL_PREFIX + rewrite in rest.split("## Unresolved findings", 1)[1]
        return validated

    # 1 — the exact pilot case
    def test_the_p1_rewrite_dropping_the_threshold_is_not_narrowed_or_upgraded(self):
        from app.services.quality.validation import narrowing

        verdict = narrowing(self.P1_REWRITE, [self.P1_CLAIM])
        assert not verdict.narrowed and "3 cups" in verdict.reason
        validated = self.assert_held_back(self.P1_CLAIM, self.P1_REWRITE)
        detail = next(a.detail for a in validated.actions if a.action == "not_narrowed")
        assert "3 cups" in detail

    # 2 — another dropped restriction
    def test_dropping_a_population_restriction_is_not_narrowing(self):
        self.assert_held_back("Coffee lowers stroke risk in women.", "Coffee lowers stroke risk.")

    # 4 — a threshold loosened
    def test_a_loosened_threshold_is_not_narrowing(self):
        self.assert_held_back(
            self.P1_CLAIM,
            "High coffee consumption (≥ 1 cup/day) was inversely associated with mortality "
            "and disability.",
        )

    # 6 — a hedge alone
    def test_a_hedge_alone_is_not_narrowing(self):
        self.assert_held_back(
            "Coffee lowers stroke risk in women.",
            "Coffee may lower stroke risk in women.",
            action="superficial_hedge",
        )

    # 8 — ambiguous scope or number changes fail closed
    @pytest.mark.parametrize(
        ("claim_text", "rewrite"),
        [
            ("Coffee lowers stroke risk in women.", "Coffee lowers stroke risk in men."),
            ("Coffee cut stroke risk by 15%.", "Coffee cut stroke risk by 10%."),
        ],
    )
    def test_an_ambiguous_change_fails_closed(self, claim_text, rewrite):
        self.assert_held_back(claim_text, rewrite)

    # 7 — a wording-only paraphrase
    def test_a_paraphrase_is_not_narrowing(self):
        from app.services.quality.validation import narrowing

        claim_text = "Coffee lowers stroke risk in women."
        rewrite = "In women, drinking coffee reduces the risk of stroke."
        assert not narrowing(rewrite, [claim_text]).narrowed
        validated = self.validate(claim_text, rewrite, self.FULL)
        assert rewrite not in " ".join(self.body_texts(validated))
        assert validated.labelled[Placement.UNRESOLVED][0].text == PARTIAL_PREFIX + rewrite

    # 3 and 5 — a demonstrably narrower rewrite can still be upgraded
    @pytest.mark.parametrize(
        ("claim_text", "rewrite"),
        [
            ("Coffee lowers stroke risk.", "Coffee lowers stroke risk in women."),  # restriction
            (
                P1_CLAIM,
                "High coffee consumption (≥ 5 cups/day) was inversely associated with "
                "mortality and disability.",
            ),  # lower bound raised
            (
                "Up to 4 cups per day is safe in pregnancy.",
                "Up to 2 cups per day is safe in pregnancy.",
            ),  # upper bound lowered
        ],
    )  # fmt: skip
    def test_a_narrower_rewrite_is_upgraded_only_with_a_full_recheck(self, claim_text, rewrite):
        first = self.validate(claim_text, rewrite)
        assert first.needs_reverification == [(0, 0)]  # sent to the citation-specific check
        assert rewrite not in " ".join(self.body_texts(first))

        still_partial = self.validate(claim_text, rewrite, Reverification(
            S.SUPPORTED_BY_CITED_SOURCE, P.PARTIALLY_SUPPORTED, frozenset({CHUNK[1]})))  # fmt: skip
        assert rewrite not in " ".join(self.body_texts(still_partial))
        assert still_partial.labelled[Placement.UNRESOLVED][0].text == PARTIAL_PREFIX + rewrite

        upheld = self.validate(claim_text, rewrite, self.FULL)
        body = {p.text: p for s in upheld.sections for p in s.sentences}
        assert body[rewrite].markers == ("S1",) and body[rewrite].placement is Placement.BODY
        assert with_markers(rewrite, ["S1"]) in render_final(upheld).partition("\n---\n")[0]

    # 9 and 10 — failed, missing and negative re-checks keep their distinctions
    @pytest.mark.parametrize(
        ("result", "placement"),
        [
            (None, Placement.NOT_EVALUATED),  # no re-check result at all
            (Reverification(S.NOT_EVALUATED, P.NOT_EVALUATED, frozenset()),
             Placement.NOT_EVALUATED),  # technical failure / budget
            (Reverification(S.INSUFFICIENT_EVIDENCE, P.NOT_ENOUGH_EVIDENCE, frozenset()),
             Placement.UNRESOLVED),
            (Reverification(S.CONTESTED, P.CONTRADICTED, frozenset()), Placement.CONTESTED),
            (Reverification(S.SUPPORTED_BY_CITED_SOURCE, P.SUPPORTED, frozenset({CHUNK[2]})),
             Placement.CITATION_NOT_CONFIRMED),  # supported only through an uncited chunk
        ],
    )  # fmt: skip
    def test_a_narrower_rewrite_without_a_full_recheck_is_never_upgraded(self, result, placement):
        rewrite = "Coffee lowers stroke risk in women."
        validated = self.validate("Coffee lowers stroke risk.", rewrite, result)
        assert rewrite not in " ".join(self.body_texts(validated))
        [placed] = [p for p in validated.labelled[placement] if "C1" in p.claim_aliases]
        assert placed.text == rewrite and placed.markers == ()
        assert placed.status is not S.SUPPORTED_BY_CITED_SOURCE or placement is not Placement.BODY
