"""quant-audit-v1: extraction, the six ordered rules, and the audit over pairs.

Fixtures here are written for these tests and are deliberately NOT drawn from the
labelled case set, so passing them says nothing about the case-set measurement.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from app.services.quality.models import QuantCategory
from app.services.quality.quantities import (
    TextPair,
    audit_quantities,
    claim_pairs_by_metric,
    classify_texts,
    extract_quantities,
    mask_numbers,
)

APPROX = 0.10
C = QuantCategory


def only(text: str, entities=()):
    mentions = extract_quantities(text, entities)
    assert len(mentions) == 1, [m.raw for m in mentions]
    return mentions[0]


class TestExtraction:
    def test_plain_percent_with_year_and_country(self):
        m = only("In 2019, the poverty rate in Mexico was 41.9%.")
        assert (m.value, m.unit, m.dimension) == (41.9, "percent", "percent")
        assert m.period == {"y2019"} and m.geography == {"mexico"}
        assert m.step == pytest.approx(0.1)

    def test_range_with_unit_between_numbers(self):
        m = only("Adherence ranged from 35% to 70% across sites.")
        assert (m.low, m.high) == (35.0, 70.0)

    def test_hyphenated_range_and_per_unit_denominator(self):
        m = only("A tablet contains 2.5-5 mg of the compound per dose unit.")
        assert (m.low, m.high) == pytest.approx((0.0025, 0.005))

    def test_plus_minus_spread_is_one_interval_not_two_quantities(self):
        m = only("Participants lost 3.2 ± 1.1 kg.")
        assert m.value == pytest.approx(3200.0) and (m.low, m.high) == pytest.approx(
            (2100.0, 4300.0)
        )

    def test_confidence_interval_attaches_and_its_level_is_not_a_quantity(self):
        m = only("The hazard ratio was HR 0.81 (95% CI 0.70–0.93).")
        assert m.dimension == "ratio" and (m.low, m.high) == pytest.approx((0.70, 0.93))

    def test_negative_values_with_unicode_minus_and_hyphen(self):
        assert only("Growth was −0.4% in 2020.").value == pytest.approx(-0.4)
        assert only("Growth was -0.4% in 2020.").value == pytest.approx(-0.4)

    def test_scale_words_and_currency(self):
        m = only("Revenue in 2021 was $3.5 billion.")
        assert m.dimension == "usd" and m.value == pytest.approx(3.5e9)

    def test_unit_conversion_within_a_dimension(self):
        a = only("Throughput reached 2 Gbps.")
        b = only("Throughput reached 2000 Mbps.")
        assert a.value == pytest.approx(b.value)

    def test_citations_ages_and_years_are_not_quantities(self):
        mentions = extract_quantities(
            "Among adults aged 40–59 years in 2018, 22% reported symptoms [12,14]."
        )
        assert [m.raw for m in mentions] == ["22%"]
        assert mentions[0].population == {"adults", "age:40-59"}

    def test_denominator_alternatives(self):
        m = only("The syrup provides 30 units per about 4 cups (1 liter).")
        assert m.denominator == {(4.0, "cup"), (1.0, "liter")}
        assert only("The syrup provides 30 units per liter.").denominator == {(1.0, "liter")}

    def test_approximation_words(self):
        assert only("Roughly 12% responded.").approximate
        assert not only("Exactly 12% responded.").approximate

    def test_entities_are_masked_and_attributed(self):
        m = only("Model X2 draws 15 W at idle.", entities=["Model X2"])
        assert m.entities == {"model x2"}

    def test_sentence_initial_frame_applies_to_every_quantity(self):
        mentions = extract_quantities(
            "In 2022 in Kenya, coverage was 61% for boys and 58% for girls."
        )
        assert all(m.period == {"y2022"} and m.geography == {"kenya"} for m in mentions)
        assert mentions[0].population == {"boys"} and mentions[1].population == {"girls"}

    def test_no_quantity(self):
        assert extract_quantities("The evidence is mixed and inconclusive.") == []


@pytest.mark.parametrize(
    ("a", "qa", "b", "qb", "expected"),
    [
        # rule 1 — units and denominators
        (
            "Uptake rose by 4% from 2015 to 2020.",
            "4%",
            "Uptake rose by 4 percentage points from 2015 to 2020.",
            "4 percentage points",
            C.UNIT_OR_DENOMINATOR,
        ),
        (
            "Each serving has 200 mg of sodium.",
            "200 mg",
            "Each serving has 200 g of sodium.",
            "200 g",
            C.UNIT_OR_DENOMINATOR,
        ),
        (
            "Spending was $900 per capita in 2019.",
            "$900",
            "Spending was $900 million in 2019.",
            "$900 million",
            C.UNIT_OR_DENOMINATOR,
        ),
        (
            "The rate was 8 per 10,000 births in 2020.",
            "8",
            "The rate was 8 per 1,000 births in 2020.",
            "8",
            C.UNIT_OR_DENOMINATOR,
        ),
        # rule 2 — periods and baselines
        (
            "In 2015, coverage in Ghana was 52%.",
            "52%",
            "In 2022, coverage in Ghana was 52%.",
            "52%",
            C.BASELINE_OR_PERIOD,
        ),
        (
            "Prices rose 6% compared with 2018.",
            "6%",
            "Prices rose 11% compared with 2012.",
            "11%",
            C.BASELINE_OR_PERIOD,
        ),
        (
            "The treatment cut absolute risk by 1%.",
            "1%",
            "The treatment cut relative risk by 20%.",
            "20%",
            C.BASELINE_OR_PERIOD,
        ),
        # rule 3 — method and scope
        (
            "In 2020, the median age in Italy was 46 years.",
            "46 years",
            "In 2020, the mean age in Italy was 44 years.",
            "44 years",
            C.METHOD_OR_SCOPE,
        ),
        (
            "In 2021, 9% of adults in Chile smoked.",
            "9%",
            "In 2021, 9% of adolescents in Chile smoked.",
            "9%",
            C.METHOD_OR_SCOPE,
        ),
        (
            "In 2021, coverage in Peru was 70%.",
            "70%",
            "In 2021, coverage in Colombia was 70%.",
            "70%",
            C.METHOD_OR_SCOPE,
        ),
        # rule 4 — consistent
        (
            "About 30% of respondents agreed.",
            "30%",
            "32% of respondents agreed.",
            "32%",
            C.CONSISTENT,
        ),
        ("Turnout was 61%.", "61%", "Turnout was 60.8%.", "60.8%", C.CONSISTENT),
        ("Efficacy was 74%.", "74%", "Efficacy was 70% to 80%.", "70% to 80%", C.CONSISTENT),
        # rule 5 — a disagreement only when everything is stated and equal
        (
            "In 2020, unemployment in Greece was 16.3%.",
            "16.3%",
            "In 2020, unemployment in Greece was 24.0%.",
            "24.0%",
            C.SAME_METRIC_DISAGREEMENT,
        ),
        # rule 6 — anything not fully stated stays unresolved
        (
            "Unemployment was 16.3%.",
            "16.3%",
            "Unemployment was 24.0% in 2020.",
            "24.0%",
            C.UNRESOLVED,
        ),
        (
            "Adults in Greece: unemployment 16.3%.",
            "16.3%",
            "Unemployment in Greece was 24.0%.",
            "24.0%",
            C.UNRESOLVED,
        ),
    ],
)
def test_six_rules(a, qa, b, qb, expected):
    category, reason = classify_texts(a, qa, b, qb, approx_rel=APPROX)
    assert category is expected, reason


def test_unknown_never_equals_known_so_one_sided_scope_is_not_a_disagreement():
    category, _ = classify_texts(
        "In 2020, unemployment was 16.3%.",
        "16.3%",
        "In 2020, unemployment in Greece was 24.0%.",
        "24.0%",
        approx_rel=APPROX,
    )
    assert category is C.UNRESOLVED


def test_missing_target_is_unresolved_not_an_error():
    category, reason = classify_texts("No numbers.", "5%", "Also 5%.", "5%", approx_rel=APPROX)
    assert category is C.UNRESOLVED and "not extracted" in reason


class TestAudit:
    def test_audit_pairs_each_quantity_with_its_closest_counterpart(self):
        findings = audit_quantities(
            [
                TextPair(
                    "claim_quote",
                    "C1",
                    "In 2020, unemployment in Greece was 16.3%.",
                    "P1",
                    "In 2020, unemployment in Greece was 16.3% and inflation 1.2%.",
                )
            ],
            approx_rel=APPROX,
        )
        assert len(findings) == 1
        assert findings[0].category is C.CONSISTENT and findings[0].alias == "F1"
        assert findings[0].relation == "claim_quote"

    def test_audit_skips_pairs_without_comparable_quantities(self):
        assert (
            audit_quantities(
                [TextPair("claim_claim", "C1", "Rates fell.", "C2", "Rates fell by 3%.")],
                approx_rel=APPROX,
            )
            == []
        )

    def test_claims_are_paired_only_when_their_masked_texts_are_similar(self):
        def embed(texts):
            return [[1.0, 0.0] if "unemployment" in t.lower() else [0.0, 1.0] for t in texts]

        pairs = claim_pairs_by_metric(
            [
                ("C1", "Unemployment was 5%."),
                ("C2", "Unemployment was 9%."),
                ("C3", "Inflation was 2%."),
                ("C4", "No number here."),
            ],
            embed,
            min_cosine=0.85,
        )
        assert [(p.owner_a, p.owner_b) for p in pairs] == [("C1", "C2")]

    def test_number_masking(self):
        assert mask_numbers("rose 4.5% to $1,200") == "rose #% to #"


class TestCaseSetIntegrity:
    """The labelled set's shape — not its labels, which are author-written."""

    path = (
        Path(__file__).resolve().parents[3]
        / "data"
        / "benchmarks"
        / "quantitative_audit_cases.json"
    )

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_is_declared_author_labelled_and_not_independent(self):
        labelling = self.load()["labelling"]
        assert labelling["independent"] is False
        assert labelling["labelled_before_auditor_code"] is True

    def test_every_case_is_well_formed_and_split(self):
        document = self.load()
        categories = {c.value for c in QuantCategory}
        ids = [case["case_id"] for case in document["cases"]]
        assert len(ids) == len(set(ids)) == 82
        for case in document["cases"]:
            assert case["label"] in categories
            assert case["split"] in ("tuning", "held_out")
            assert case["quantity_a"] in case["text_a"] and case["quantity_b"] in case["text_b"]

    def test_split_is_the_recorded_seeded_assignment(self):
        document = self.load()
        seed = document["split"]["seed"]
        by_label: dict[str, list[dict]] = {}
        for case in document["cases"]:
            by_label.setdefault(case["label"], []).append(case)
        for group in by_label.values():
            ordered = sorted(
                group, key=lambda c: hashlib.sha256(f"{seed}:{c['case_id']}".encode()).hexdigest()
            )
            for position, case in enumerate(ordered):
                assert case["split"] == ("tuning" if position < len(ordered) // 2 else "held_out")

    def test_dev_cases_quote_development_pools_only_and_verbatim(self):
        root = self.path.parents[2]
        for case in self.load()["cases"]:
            if case["origin"] != "dev-pool":
                assert case["source_ref"] is None
                continue
            assert "/dev-" in case["source_ref"]
            pool = json.loads((root / case["source_ref"]).read_text(encoding="utf-8"))
            corpus = re.sub(r"\s+", " ", " ".join(chunk["text"] for chunk in pool["chunks"]))
            assert re.sub(r"\s+", " ", case["text_b"]).strip() in corpus, case["case_id"]
