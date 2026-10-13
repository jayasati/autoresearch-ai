"""
The benchmark dataset: structure, coverage, and the freeze that makes the
no-modification rule real.

These tests load the actual committed files — the dataset is part of the method, so
its integrity is part of the suite. The freeze tests work on copies in tmp_path,
because a test that edited the real test set would be the exact crime the freeze
exists to catch.
"""

import json
import shutil
from pathlib import Path

import pytest

from app.core.config import Settings
from app.schemas.benchmark import (
    BenchmarkCategory,
    BenchmarkItem,
    BenchmarkSet,
    ExpectedBehavior,
    GroundTruthBasis,
    KnownClaim,
)
from app.services.benchmark import (
    SET_NAMES,
    BenchmarkDatasetError,
    FrozenSetViolation,
    benchmark_directory,
    load_freeze_manifest,
    load_set,
)

ALL_CATEGORIES = set(BenchmarkCategory)


class TestTheCommittedSets:
    @pytest.mark.parametrize("name", SET_NAMES)
    def test_every_set_loads_and_covers_every_category(self, name):
        benchmark_set = load_set(name)
        assert len(benchmark_set.items) == 8
        assert {item.category for item in benchmark_set.items} == ALL_CATEGORIES

    def test_question_ids_are_unique_across_all_sets(self):
        ids = [
            item.question_id for name in SET_NAMES for item in load_set(name).items
        ]
        assert len(ids) == len(set(ids))

    def test_questions_do_not_repeat_across_sets(self):
        """A test question leaking into the development set would let tuning see
        the exam paper."""
        questions = [
            item.question.lower() for name in SET_NAMES for item in load_set(name).items
        ]
        assert len(questions) == len(set(questions))

    def test_every_item_says_how_its_ground_truth_was_established(self):
        for name in SET_NAMES:
            for item in load_set(name).items:
                assert item.ground_truth_basis in set(GroundTruthBasis)
                assert len(item.ground_truth_notes) >= 20

    def test_behavioral_items_store_no_answers(self):
        """Current-information items deliberately record no figure or version: the
        right answer moves, so storing one would rot into wrong ground truth."""
        for name in SET_NAMES:
            for item in load_set(name).items:
                if item.category is BenchmarkCategory.CURRENT_INFORMATION:
                    assert item.expected_behavior.answer_changes_over_time
                    assert item.known_claims == []

    def test_false_premise_items_expect_contradiction(self):
        for name in SET_NAMES:
            for item in load_set(name).items:
                if item.category is BenchmarkCategory.FALSE_PREMISE:
                    assert item.expected_behavior.premise_is_false
                    assert any(
                        claim.expected_verdict == "contradicted"
                        for claim in item.known_claims
                    )

    def test_conflict_items_name_the_known_disagreement(self):
        for name in SET_NAMES:
            for item in load_set(name).items:
                if item.category is BenchmarkCategory.CONFLICTING_LITERATURE:
                    assert item.expected_behavior.expect_conflicts
                    assert item.known_conflicts

    def test_unknown_set_names_are_refused(self):
        with pytest.raises(BenchmarkDatasetError, match="Unknown benchmark set"):
            load_set("production")


class TestTheFreeze:
    @pytest.fixture
    def copied_dataset(self, tmp_path) -> Settings:
        """The real dataset files, copied where a test may safely corrupt them.

        Files only: the directory also holds `results/` subdirectories once
        experiments have run, and those are not part of the dataset.
        """
        for path in benchmark_directory().iterdir():
            if path.is_file():
                shutil.copy(path, tmp_path / path.name)
        return Settings(BENCHMARK_DATA_DIR=str(tmp_path), APP_ENV="test")

    def test_the_committed_test_set_matches_its_manifest(self):
        benchmark_set = load_set("test")  # raises FrozenSetViolation on mismatch
        manifest = load_freeze_manifest(benchmark_directory())
        assert manifest["sha256"] == benchmark_set.fingerprint
        assert manifest["version"] == benchmark_set.version
        assert "may not be modified in response to benchmark results" in manifest["policy"]

    def test_an_edited_test_set_is_refused(self, copied_dataset):
        path = Path(copied_dataset.BENCHMARK_DATA_DIR) / "test.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["items"][0]["question"] = "A quietly edited question, which must not load?"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(FrozenSetViolation):
            load_set("test", copied_dataset)

    def test_formatting_changes_do_not_break_the_freeze(self, copied_dataset):
        """The hash is canonical: re-indenting or re-ordering keys is not an edit."""
        path = Path(copied_dataset.BENCHMARK_DATA_DIR) / "test.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(payload, indent=4, sort_keys=True), encoding="utf-8")

        assert load_set("test", copied_dataset).fingerprint  # loads without violation

    def test_a_missing_manifest_is_itself_a_violation(self, copied_dataset):
        (Path(copied_dataset.BENCHMARK_DATA_DIR) / "test.freeze.json").unlink()
        with pytest.raises(FrozenSetViolation, match="no freeze manifest"):
            load_set("test", copied_dataset)

    def test_development_and_validation_are_not_frozen(self, copied_dataset):
        """Refinable sets stay refinable: editing them must not trip anything."""
        for name in ("development", "validation"):
            path = Path(copied_dataset.BENCHMARK_DATA_DIR) / f"{name}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["description"] += " (edited)"
            path.write_text(json.dumps(payload), encoding="utf-8")
            assert load_set(name, copied_dataset).items


class TestTheSchema:
    def behavior(self, **overrides) -> ExpectedBehavior:
        return ExpectedBehavior(
            summary="A behaviour long enough to satisfy the schema's floor.", **overrides
        )

    def item(self, **overrides) -> BenchmarkItem:
        values = {
            "question_id": "dev-normal_research-99",
            "question": "A question long enough to satisfy the schema?",
            "category": BenchmarkCategory.NORMAL_RESEARCH,
            "difficulty": "easy",
            "expected_behavior": self.behavior(),
            "ground_truth_basis": GroundTruthBasis.BEHAVIORAL,
            "ground_truth_notes": "Written for schema tests; asserts nothing about the world.",
        }
        return BenchmarkItem(**{**values, **overrides})

    def test_the_id_must_carry_the_category(self):
        with pytest.raises(ValueError, match="does not carry its category"):
            self.item(question_id="dev-false_premise-99")

    def test_known_claims_cannot_rest_on_author_judgment(self):
        with pytest.raises(ValueError, match="author_judgment"):
            KnownClaim(
                text="A claim that would be scored against verdicts.",
                expected_verdict="supported",
                basis=GroundTruthBasis.AUTHOR_JUDGMENT,
            )

    def test_contradictory_behavior_flags_are_rejected(self):
        with pytest.raises(ValueError, match="pick the failure"):
            self.behavior(premise_is_false=True, expect_insufficient_evidence=True)

    def test_duplicate_ids_within_a_set_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate question_id"):
            BenchmarkSet(
                name="development",
                version="1.0.0",
                created="2026-10-12",
                description="dup check",
                items=[self.item(), self.item()],
            )

    def test_items_cannot_sit_in_the_wrong_set(self):
        with pytest.raises(ValueError, match="does not belong"):
            BenchmarkSet(
                name="test",
                version="1.0.0",
                created="2026-10-12",
                description="wrong-set check",
                items=[self.item()],  # a dev- id in the test set
            )

    def test_the_fingerprint_ignores_formatting_but_not_content(self):
        a = BenchmarkSet(
            name="development", version="1.0.0", created="2026-10-12",
            description="fingerprint check", items=[self.item()],
        )
        b = BenchmarkSet.model_validate(json.loads(json.dumps(a.model_dump(mode="json"))))
        assert a.fingerprint == b.fingerprint
        c = a.model_copy(update={"description": "fingerprint check, edited"})
        assert c.fingerprint != a.fingerprint
