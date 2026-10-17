"""The paired draft-vs-final evaluator, `scripts/evaluate_quality_pair.py` (stage 38).

Offline only: a hand-built artifact whose every metric is computed by hand below, a
fixture pool with its own manifest, fake judges — and one artifact produced by the
real runner on fake providers. No network, no tokens, no shared database.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services.llm import LLMResponseInvalid
from tests.unit.test_quality_verification import Response, judge

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "evaluate_quality_pair", ROOT / "scripts" / "evaluate_quality_pair.py"
)
pair = importlib.util.module_from_spec(_spec)
sys.modules["evaluate_quality_pair"] = pair
_spec.loader.exec_module(pair)

QUESTION = "Does coffee affect health in adults?"
QID = "dev-fixture-01"

S0 = "Coffee lowers diabetes risk."  # Q1, cites E1+E2 (both S1): a multi-passage pair
S1 = "Coffee is good for everyone."  # Q1, cites E3; contested in ON
S2 = "Evidence on mood is limited."  # Q2, uncertainty: not an assertion
S3 = "Older adults benefit most."  # Q3 (not core), cites E4; restored in ON
S4 = "Sweetened coffee also helps."  # Q3, cites an unknown alias: no marker

POOL_VERDICTS = {
    S0: "supported",
    S1: "contradicted",
    S3: "partially_supported",
    S4: "unsupported",
}
PAIR_OUTCOMES = {  # (sentence, passage aliases) -> outcome
    (S0, ("E1", "E2")): "partially_supports",
    (S1, ("E3",)): "contradicts",
    (S3, ("E4",)): "supports",
    (S0, ("E1",)): "supports",
}
PASSAGE_TEXT = {f"E{i}": f"Passage {i} text about coffee." for i in range(1, 5)}


def payload(*, finalization="complete", body=None, placements=None, omitted=("C4",)):
    final = None
    if finalization == "complete":
        final = {
            "status": "complete",
            "reasons": [],
            "placements": placements or {"C1": "body", "C2": "contested", "C3": "body"},
            "metrics": {"claims_omitted": list(omitted), "unanswered_core": ["Q2"]},
            "body": body
            if body is not None
            else [
                {"subquestion": "Q1", "heading": "Diabetes", "text": S0, "kind": "finding",
                 "origin": "model", "placement": "body", "claim_aliases": ["C1"],
                 "citations": {"S1": ["E1"]}},
                {"subquestion": "Q3", "heading": "Age", "text": pair.RESTORED_PREFIX + S3,
                 "kind": "finding", "origin": "restored", "placement": "body",
                 "claim_aliases": ["C3"], "citations": {"S3": ["E4"]}},
            ],
            "labelled": {"contested": [{"text": S1, "kind": "finding", "claim_aliases": ["C2"]}]},
        }  # fmt: skip
    elif finalization == "failed":
        final = {"status": "finalization_failed", "reasons": ["revision not produced"]}
    return {
        "plan": {
            "subquestions": [
                {"alias": "Q1", "is_core": True},
                {"alias": "Q2", "is_core": True},
                {"alias": "Q3", "is_core": False},
            ]
        },
        "evidence": {
            "aliases": {
                "passages": {
                    "E1": {"chunk_id": "c1", "source_label": "S1"},
                    "E2": {"chunk_id": "c2", "source_label": "S1"},
                    "E3": {"chunk_id": "c3", "source_label": "S2"},
                    "E4": {"chunk_id": "c4", "source_label": "S3"},
                }
            }
        },
        "draft": {
            "report": {
                "sections": [
                    {
                        "subquestion": "Q1",
                        "sentences": [
                            {"text": S0, "kind": "finding", "evidence_ids": ["E1", "E2"]},
                            {"text": S1, "kind": "finding", "evidence_ids": ["E3"]},
                        ],
                    },
                    {
                        "subquestion": "Q2",
                        "sentences": [{"text": S2, "kind": "uncertainty", "evidence_ids": []}],
                    },
                    {
                        "subquestion": "Q3",
                        "sentences": [
                            {"text": S3, "kind": "finding", "evidence_ids": ["E4"]},
                            {"text": S4, "kind": "finding", "evidence_ids": ["E9"]},
                        ],
                    },
                ]
            }
        },  # fmt: skip
        "alignments": {
            "C1": {"status": "aligned", "sentence_index": 0},
            "C2": {"status": "aligned", "sentence_index": 1},
            "C3": {"status": "aligned", "sentence_index": 3},
            "C4": {"status": "aligned", "sentence_index": 4},
        },
        "finalization": final,
    }


def write_fixture(tmp_path, **kwargs) -> argparse.Namespace:
    artifact = tmp_path / "artifact"
    artifact.mkdir(exist_ok=True)
    run = {
        "question": QUESTION,
        "research_id": "r-1",
        "configuration": {"model": "gpt-4o-mini"},
        "persisted": {"audit": {"payload": payload(**kwargs)}},
        "stage_seconds": {"prepare_evidence": 10.0, "draft_and_align": 6.0,
                          "verify_claims": 20.0, "finalize": 4.0},
        "budget": {"stages": {
            "planning": {"attempts": 1, "measured_tokens": 1000, "measured_prompt_tokens": 800,
                         "measured_completion_tokens": 200, "estimated_tokens": 0, "seconds": 2.0},
            "drafting": {"attempts": 1, "measured_tokens": 3000, "measured_prompt_tokens": 2000,
                         "measured_completion_tokens": 1000, "estimated_tokens": 0, "seconds": 5.0},
            "extraction": {"attempts": 1, "measured_tokens": 2000,
                           "measured_prompt_tokens": 1500, "measured_completion_tokens": 500,
                           "estimated_tokens": 0, "seconds": 1.5},
            "verification": {"attempts": 4, "measured_tokens": 8000,
                             "measured_prompt_tokens": 7000, "measured_completion_tokens": 1000,
                             "estimated_tokens": 500, "seconds": 9.0},
        }},
    }  # fmt: skip
    diagnostics = {
        "passages": [
            {"alias": a, "chunk_id": f"c{a[1]}", "text": t, "source": {"title": f"Source {a}"}}
            for a, t in PASSAGE_TEXT.items()
        ]
    }
    (artifact / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (artifact / "diagnostics.json").write_text(json.dumps(diagnostics), encoding="utf-8")
    pools = tmp_path / "pools"
    pools.mkdir(exist_ok=True)
    pool = pools / f"{QID}.json"
    pool.write_text(json.dumps({"chunks": [{"text": "x" * 800}] * 3}), encoding="utf-8")
    digest = hashlib.sha256(pool.read_bytes()).hexdigest()
    (pools / "MANIFEST.sha256").write_text(f"# fixture\n{digest}  {pool.name}\n", "utf-8")
    dataset = tmp_path / "development.json"
    dataset.write_text(
        json.dumps({"items": [{"question_id": QID, "question": QUESTION}]}), encoding="utf-8"
    )
    return pair.parse_args(
        ["evaluate", str(artifact), "--question-id", QID, "--pools", str(pools),
         "--dataset", str(dataset), "--out", str(tmp_path / "eval")]
    )  # fmt: skip


def fake_judge(seen=None, *, missing=()):
    """Judges blinded items by their content only; records what it was shown."""
    by_text = {
        (text, tuple(PASSAGE_TEXT[a] for a in aliases)): outcome
        for (text, aliases), outcome in PAIR_OUTCOMES.items()
    }

    async def judge_items(items):
        judgments = []
        for item in items:
            if seen is not None:
                seen.append(item)
            if item["text"] in missing:
                judgments.append({"item_id": item["item_id"], "verdict": None, "outcome": None})
            elif item["type"] == "pool":
                judgments.append(
                    {"item_id": item["item_id"], "verdict": POOL_VERDICTS[item["text"]]}
                )
            else:
                passages = tuple(p["text"] for p in item["passages"])
                judgments.append(
                    {"item_id": item["item_id"], "outcome": by_text[(item["text"], passages)]}
                )
        return judgments

    return judge_items


def scored(tmp_path, judge_items=None, **kwargs):
    args = write_fixture(tmp_path, **kwargs)
    return pair.evaluate(args, judge=judge_items or fake_judge())["scores"]


# --- pairing and mapping ------------------------------------------------------------------


class TestArmsAndMapping:
    def test_off_is_the_draft_as_rendered_with_its_citation_passages(self):
        off = pair.build_off(payload())
        assert [s.text for s in off] == [S0, S1, S2, S3, S4]
        assert off[0].citations == {"S1": ["E1", "E2"]}  # both passages behind one marker
        assert off[1].citations == {"S2": ["E3"]}
        assert off[4].citations == {}  # unknown alias: no marker rendered
        assert [s.assertion for s in off] == [True, True, False, True, True]
        assert off[3].claim_aliases == ["C3"] and off[3].subquestion == "Q3"

    def test_on_is_the_final_body_with_prefixes_removed(self):
        on, missing = pair.build_on(payload())
        assert missing is None
        assert [(s.text, s.citations, s.subquestion) for s in on] == [
            (S0, {"S1": ["E1"]}, "Q1"),
            (S3, {"S3": ["E4"]}, "Q3"),  # "The evidence indicates:" stripped
        ]

    def test_a_failed_finalization_is_a_missing_on_result_never_the_draft(self):
        on, missing = pair.build_on(payload(finalization="failed"))
        assert on is None and missing.startswith("finalization_failed")

    def test_an_artifact_without_the_body_record_is_refused(self):
        old = payload()
        del old["finalization"]["body"]
        with pytest.raises(pair.Refused, match="body record"):
            pair.build_on(old)


# --- blinding --------------------------------------------------------------------------------


class TestBlinding:
    def test_items_carry_no_arm_and_identical_units_are_judged_once(self, tmp_path):
        seen = []
        result = pair.evaluate(write_fixture(tmp_path), judge=fake_judge(seen))
        assert result["items"] == 8  # 4 pool + 4 pair items after cross-arm dedupe
        assert {key for item in seen for key in item} <= {"item_id", "type", "text", "passages"}
        assert all("label" in p and "chunk" not in json.dumps(p) for i in seen
                   for p in i.get("passages", []))  # fmt: skip
        key = json.loads((tmp_path / "eval" / "key.json").read_text("utf-8"))
        shared = [units for units in key.values() if {u["arm"] for u in units} == {"off", "on"}]
        assert len(shared) == 3  # S0 and S3 vs the pool; (S3, E4) citation pair

    def test_ids_and_order_are_seeded(self, tmp_path):
        arms = {"off": pair.build_off(payload()), "on": pair.build_on(payload())[0]}
        passages = {a: {"chunk_id": a, "title": a, "text": t} for a, t in PASSAGE_TEXT.items()}
        first, _ = pair.build_items(arms, passages, seed=1)
        again, _ = pair.build_items(arms, passages, seed=1)
        other, _ = pair.build_items(arms, passages, seed=2)
        assert first == again
        assert [i["item_id"] for i in first] != [i["item_id"] for i in other]

    def test_judgments_are_stored_before_the_key_is_used(self, tmp_path):
        args = write_fixture(tmp_path)
        original = pair.score

        def checking_score(*a, **k):
            assert (tmp_path / "eval" / "judgments.jsonl").is_file()
            return original(*a, **k)

        pair.score = checking_score
        try:
            pair.evaluate(args, judge=fake_judge())
        finally:
            pair.score = original


# --- metrics and denominators ----------------------------------------------------------------


class TestMetrics:
    def test_strict_is_primary_and_lenient_is_the_sensitivity(self, tmp_path):
        s = scored(tmp_path)
        off, on = s["arms"]["off"]["citations"], s["arms"]["on"]["citations"]
        # OFF pairs: (S0,S1 marker) partial, (S1,S2) contradicts, (S3,S3) supports
        assert (off["pairs"], off["judged"], off["strict"], off["lenient"]) == (
            3,
            3,
            0.3333,
            0.6667,
        )
        assert off["multi_passage"] == 1 and off["contradicted"] == 0.3333
        assert (on["pairs"], on["strict"], on["lenient"]) == (2, 1.0, 1.0)

    def test_body_claims_against_the_pool_with_counts(self, tmp_path):
        s = scored(tmp_path)
        off, on = s["arms"]["off"]["pool"], s["arms"]["on"]["pool"]
        assert off["judged"] == 4 and off["counts"]["contradicted"] == 1
        assert (off["support_strict"], off["support_lenient"]) == (0.25, 0.5)
        assert off["unsupported_or_contradicted"] == 0.5 and off["contradicted"] == 0.25
        assert (on["judged"], on["support_strict"], on["support_lenient"]) == (2, 0.5, 1.0)
        assert on["unsupported_or_contradicted"] == 0.0
        assert s["arms"]["off"]["assertion_sentences"] == 4  # the uncertainty is excluded

    def test_core_question_coverage(self, tmp_path):
        s = scored(tmp_path)
        for arm in ("off", "on"):
            core = s["arms"][arm]["core"]
            assert core["core_subquestions"] == ["Q1", "Q2"]
            assert core["covered_strict"] == ["Q1"] and core["coverage_strict"] == 0.5
        assert s["paired"]["on_unanswered_core_disclosed"] == ["Q2"]

    def test_contradicted_and_supported_findings_are_followed_into_on(self, tmp_path):
        paired = scored(tmp_path)["paired"]
        assert paired["contradiction_handling"] == {"sentences": 1, "fates": {"contested": 1}}
        assert paired["supported_findings_strict"]["fates"] == {"body": 1}
        assert paired["supported_findings_lenient"]["fates"] == {"body": 2}

    def test_a_supported_finding_lost_in_revision_is_counted_by_fate(self, tmp_path):
        verdicts = dict(POOL_VERDICTS)
        POOL_VERDICTS[S4] = "supported"  # C4: recorded as omitted by the revision
        try:
            paired = scored(tmp_path, placements={"C1": "body", "C2": "contested"})["paired"]
        finally:
            POOL_VERDICTS.clear()
            POOL_VERDICTS.update(verdicts)
        # S0 -> body; S4 -> omission recorded; S3 not strict-supported
        assert paired["supported_findings_strict"]["fates"] == {
            "body": 1,
            "omission_recorded": 1,
        }

    def test_an_unaccounted_claim_is_reported_as_such(self):
        assert pair._fate(["C9"], {"placements": {}, "metrics": {"claims_omitted": []}}) == (
            "unaccounted"
        )
        assert pair._fate([], {}) == "untracked"

    def test_missing_judgments_leave_the_denominator_and_are_counted(self, tmp_path):
        s = scored(tmp_path, fake_judge(missing={S4}))
        pool = s["arms"]["off"]["pool"]
        assert (pool["judged"], pool["missing_judgments"]) == (3, 1)
        assert pool["support_strict"] == round(1 / 3, 4)

    def test_an_unresolvable_citation_counts_against_support(self, tmp_path):
        body = [
            {"subquestion": "Q1", "heading": "D", "text": S0, "kind": "finding",
             "origin": "model", "placement": "body", "claim_aliases": ["C1"],
             "citations": {"S1": ["E1"], "S9": []}},
        ]  # fmt: skip
        on = scored(tmp_path, body=body)["arms"]["on"]["citations"]
        assert (on["pairs"], on["judged"], on["unresolvable"]) == (2, 2, 1)
        assert on["strict"] == 0.5

    def test_usage_splits_off_and_on(self, tmp_path):
        usage = scored(tmp_path)["usage"]
        assert usage["off"]["tokens_measured"] == 4000 and usage["off"]["attempts"] == 2
        assert usage["on"]["tokens_measured"] == 14000 and usage["on"]["tokens_estimated"] == 500
        assert usage["off"]["latency_seconds"] == 14.5  # 10 + 6 - 1.5 extraction
        assert usage["on"]["latency_seconds"] == 40.0
        assert usage["off"]["cost_measured_usd"] == round(2800 * 0.15e-6 + 1200 * 0.60e-6, 6)


# --- failed finalization and aggregation -------------------------------------------------------


class TestMissingOnAndAggregation:
    def test_a_failed_finalization_scores_on_as_missing(self, tmp_path):
        s = scored(tmp_path, finalization="failed")
        assert s["arms"]["on"] is None and s["missing_on"].startswith("finalization_failed")
        assert s["finalization_status"] == "finalization_failed"
        assert s["paired"]["supported_findings_strict"]["fates"] == {"on_missing": 1}
        assert s["arms"]["off"]["pool"]["judged"] == 4  # OFF still scored

    def test_aggregate_primary_is_paired_and_the_sensitivity_scores_missing_as_zero(self, tmp_path):
        complete, failed = tmp_path / "a", tmp_path / "b"
        complete.mkdir()
        failed.mkdir()
        for directory, kind in ((complete, "complete"), (failed, "failed")):
            args = write_fixture(directory, finalization=kind)
            pair.evaluate(args, judge=fake_judge())
        summary = pair.aggregate([complete / "eval", failed / "eval"])
        assert (summary["questions"], summary["paired"]) == (2, 1)
        assert summary["missing_on"] == [QID]
        primary = summary["primary_paired"]["citation_support_strict"]
        assert primary["on"] == {"mean": 1.0, "n": 1}
        assert primary["off"] == {"mean": 0.3333, "n": 1}
        sensitivity = summary["sensitivity_missing_on_as_zero"]["citation_support_strict"]
        assert sensitivity["on"] == {"mean": 0.5, "n": 2}
        assert sensitivity["off"]["n"] == 2


# --- guards, modes, judges ------------------------------------------------------------------


class TestGuardsAndModes:
    def test_only_development_questions(self, tmp_path):
        args = write_fixture(tmp_path)
        args.question_id = "val-normal_research-01"
        with pytest.raises(pair.Refused, match="development"):
            pair.evaluate(args)

    def test_the_artifact_must_answer_this_item(self, tmp_path):
        args = write_fixture(tmp_path)
        run = json.loads((args.artifact / "run.json").read_text("utf-8"))
        run["question"] = "Something else entirely?"
        (args.artifact / "run.json").write_text(json.dumps(run), encoding="utf-8")
        with pytest.raises(pair.Refused, match="question"):
            pair.evaluate(args)

    def test_a_missing_or_changed_pool_is_refused_never_built(self, tmp_path):
        args = write_fixture(tmp_path)
        pool = args.pools / f"{QID}.json"
        pool.write_text(pool.read_text("utf-8") + " ", encoding="utf-8")
        with pytest.raises(pair.Refused, match="MANIFEST"):
            pair.evaluate(args)
        pool.unlink()
        with pytest.raises(pair.Refused, match="never built"):
            pair.evaluate(args)
        assert not pool.exists()

    def test_a_dry_run_judges_nothing(self, tmp_path):
        args = write_fixture(tmp_path)
        result = pair.evaluate(args)
        assert result["mode"] == "dry_run" and result["estimate"]["items"] == 8
        assert not (args.out / "judgments.jsonl").exists()
        assert (args.out / "items.jsonl").is_file() and (args.out / "manifest.json").is_file()

    def test_cli_dry_run_and_refusals(self, tmp_path, capsys):
        args = write_fixture(tmp_path)
        base = ["evaluate", str(args.artifact), "--question-id", QID, "--pools",
                str(args.pools), "--dataset", str(args.dataset),
                "--out", str(args.out)]  # fmt: skip
        assert pair.main(base) == 0
        assert '"mode": "dry_run"' in capsys.readouterr().out
        with pytest.raises(SystemExit):
            pair.main([*base, "--execute"])  # no judge model
        same = [*base, "--execute", "--judge-model", "gpt-4o-mini"]
        assert pair.main(same) == 2  # the pipeline's own model, refused before any call
        assert "pipeline's own model" in capsys.readouterr().out

    def test_the_estimate_prices_input_and_output_and_caps_with_a_margin(self, tmp_path):
        args = write_fixture(tmp_path)
        items = [json.loads(line) for line in self._items(args)]
        cost = pair.estimate(items, args.pools / f"{QID}.json", "gpt-4o")
        assert cost["output_tokens"] == 600 * len(items)
        expected = cost["input_tokens"] * 2.5e-6 + cost["output_tokens"] * 10e-6
        assert cost["cost_usd"] == round(expected, 4)
        assert cost["cost_usd_cap"] == round(expected * 1.5, 4)
        assert pair.estimate(items, args.pools / f"{QID}.json", "unlisted")["cost_usd"] is None

    def test_a_cost_cap_above_max_usd_is_refused_before_any_call(self, tmp_path, capsys):
        args = write_fixture(tmp_path)
        argv = ["evaluate", str(args.artifact), "--question-id", QID, "--pools",
                str(args.pools), "--dataset", str(args.dataset), "--out", str(args.out),
                "--execute", "--judge-model", "gpt-4o", "--max-usd", "0.0001"]  # fmt: skip
        assert pair.main(argv) == 2
        assert "exceeds --max-usd" in capsys.readouterr().out
        assert not (args.out / "judgments.jsonl").exists()

    @staticmethod
    def _items(args):
        pair.evaluate(args)
        return (args.out / "items.jsonl").read_text("utf-8").splitlines()

    def test_blinded_human_annotation_round_trip(self, tmp_path):
        args = write_fixture(tmp_path)
        args.export_annotations = True
        pool_shown = [{"label": "P1", "title": "Pool", "text": "pool passage"}]
        pair.evaluate(args, pool_hits=lambda text: pool_shown)
        sheet = args.out / "annotations.csv"
        rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
        assert len(rows) == 8 and set(rows[0]) == {
            "item_id", "type", "text", "passages", "verdict", "notes"}  # fmt: skip
        assert all("arm" not in r["passages"] for r in rows)
        for row in rows:  # the annotator labels every item; one left blank
            if row["type"] == "pool":
                row["verdict"] = POOL_VERDICTS[row["text"]]
            elif row["text"] == S1:
                row["verdict"] = ""
            else:
                row["verdict"] = "supported"
        with sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        args.export_annotations, args.annotations = False, sheet
        result = pair.evaluate(args)
        assert result["scores"]["independence"] == "human annotation"
        off = result["scores"]["arms"]["off"]["citations"]
        assert (off["judged"], off["missing_judgments"], off["strict"]) == (2, 1, 1.0)


class TestLiveJudgePathOnFakes:
    """The reused judging code (external judge for pool items; verification-v2 prompt and
    quote enforcement for pairs) driven by a fake model and a fake pool store."""

    async def test_pool_and_pair_items_through_the_reused_judges(self):
        class Hit:
            def __init__(self, text):
                self.text = text
                self.chunk_id = "00000000-0000-4000-8000-0000000000aa"
                self.source = type("Src", (), {"title": "Pool source", "url": None})()

        class Store:
            def search(self, text, k):
                return [Hit("Alpha trial: moderate coffee lowered risk in adults.")]

        class FakeLLM:
            def __init__(self):
                self.prompts = []

            async def generate_structured_output(self, prompt, schema, **kwargs):
                self.prompts.append(prompt)
                if "nonsense" in prompt:
                    raise LLMResponseInvalid()
                return Response(judge(pick="Alpha")(prompt))

        items = [
            {"item_id": "00000000-0000-4000-8000-000000000001", "type": "pool",
             "text": "Coffee lowered risk."},
            {"item_id": "00000000-0000-4000-8000-000000000002", "type": "pair",
             "text": "Coffee lowered risk.",
             "passages": [{"label": "P1", "title": "T", "text": "Alpha trial: coffee helped."}]},
            {"item_id": "00000000-0000-4000-8000-000000000003", "type": "pair",
             "text": "nonsense", "passages": [{"label": "P1", "title": "T", "text": "Beta."}]},
        ]  # fmt: skip
        llm = FakeLLM()
        judgments = {j["item_id"]: j for j in await pair.judge_items_llm(items, llm, Store())}
        assert judgments[items[0]["item_id"]]["verdict"] == "supported"
        assert judgments[items[1]["item_id"]]["outcome"] == "supports"
        assert judgments[items[1]["item_id"]]["quotes"] == ["Alpha trial:"]
        assert judgments[items[2]["item_id"]]["outcome"] is None  # invalid twice: missing
        # the model sees claim text and passages only: no arm, no item id
        assert not any("arm" in p.lower().split() or "0000-4000" in p for p in llm.prompts)


class TestOnARealRunnerArtifact:
    """End to end on fakes: the runner writes an artifact, the evaluator pairs it."""

    def test_the_runner_artifact_pairs_and_scores(self, configured, tmp_path):
        from tests.unit.test_quality_finalization import FinalLLM, exact_support
        from tests.unit.test_quality_pipeline import TOPIC
        from tests.unit.test_quality_runner import artifact, fake_pipeline, run

        run(configured, "--execute", factory=fake_pipeline(FinalLLM(pool=exact_support)))
        directory, record = artifact(configured)
        body = record["persisted"]["audit"]["payload"]["finalization"]["body"]
        assert body and all(e["citations"] for e in body if e["kind"] == "finding")
        diagnostics = json.loads((directory / "diagnostics.json").read_text("utf-8"))
        known = {p["alias"] for p in diagnostics["passages"]}
        assert all(set(p) <= known for e in body for p in e["citations"].values())

        pools = tmp_path / "pools"
        pools.mkdir()
        pool = pools / f"{QID}.json"
        pool.write_text(json.dumps({"chunks": [{"text": "y" * 500}]}), encoding="utf-8")
        (pools / "MANIFEST.sha256").write_text(
            f"{hashlib.sha256(pool.read_bytes()).hexdigest()}  {pool.name}\n", "utf-8"
        )
        dataset = tmp_path / "development.json"
        dataset.write_text(
            json.dumps({"items": [{"question_id": QID, "question": TOPIC}]}), "utf-8"
        )
        args = pair.parse_args(
            ["evaluate", str(directory), "--question-id", QID, "--pools", str(pools),
             "--dataset", str(dataset)]
        )  # fmt: skip

        async def everything_supported(items):
            return [
                {"item_id": i["item_id"], "verdict": "supported", "outcome": "supports"}
                for i in items
            ]

        scores = pair.evaluate(args, judge=everything_supported)["scores"]
        assert scores["finalization_status"] == "complete"
        assert scores["arms"]["on"]["citations"]["strict"] == 1.0
        assert scores["arms"]["off"]["body_sentences"] >= scores["arms"]["on"]["body_sentences"]
        assert scores["framing"].startswith("support relative to the frozen evidence pool")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    from tests.unit.test_quality_runner import OPENAI_SECRET, TAVILY_SECRET, runner

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT_NAME",
                "SEMANTIC_SCHOLAR_API_KEY", *runner._OVERRIDDEN):  # fmt: skip
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("TAVILY_API_KEY", TAVILY_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    return tmp_path
