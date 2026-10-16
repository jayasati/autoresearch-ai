"""
Measure the claim verifier's agreement with the manually labeled validation cases.

    backend/.venv/Scripts/python.exe scripts/validate_verifier.py

Options:
    --case ID      run only the case(s) whose case_id contains ID (repeatable)
    --json         print the full result records as JSON instead of the table

Needs a real foundation-model credential in `.env`. **This makes one LLM call per
case** (two on a validation retry), which is why it is a script you run
deliberately and not part of the test suite — the suite is fully mocked.

It runs the real production path: the versioned prompt, the structured-output
schema, the verbatim-quote enforcement and the downgrade rule, exactly as the
run-owned verifier and the arm-blind external judge apply them. Each case's
passages are presented directly as P1..Pn (the pools are small by construction;
there is nothing for a retriever to rank). Agreement means the enforced final
verdict equals the hand-authored label — or any member of the case's
`acceptable_verdicts` where the label set declares several verdicts defensible.

The outcome is written to `data/benchmarks/results/verifier-validation/`, keyed by
prompt version and validation-set version, so the measured agreement of each
prompt revision stays on the record next to the one it replaced. Passing this set
shows agreement on 21 hand-labeled cases; it is NOT a reliability certificate for
the verifier in general.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.llm import LLMResponseInvalid, LLMService  # noqa: E402
from app.services.verification.models import ClaimVerificationModel  # noqa: E402
from app.services.verification.prompts import (  # noqa: E402
    VERIFICATION_PROMPT_VERSION,
    VERIFICATION_SYSTEM,
    build_verification_prompt,
)
from app.services.verification.verifier import ClaimVerifier  # noqa: E402

VALIDATION_SET = ROOT / "data" / "benchmarks" / "verifier_validation.json"
RESULTS_DIR = ROOT / "data" / "benchmarks" / "results" / "verifier-validation"

#: Mirrors the repository downgrade rule and the external judge exactly.
_EVIDENCE_BEARING = {"supported", "partially_supported", "contradicted"}


async def judge_case(llm: LLMService, case: dict) -> dict:
    """One case through the production path: prompt, schema, enforcement, downgrade."""
    labels = {
        f"P{index + 1}": SimpleNamespace(text=passage["text"], chunk_id=uuid.uuid4())
        for index, passage in enumerate(case["passages"])
    }
    prompt = build_verification_prompt(
        case["claim"],
        [
            (label, passage["title"], passage["text"])
            for (label, _), passage in zip(labels.items(), case["passages"], strict=True)
        ],
    )
    try:
        try:
            response = await llm.generate_structured_output(
                prompt, ClaimVerificationModel, system=VERIFICATION_SYSTEM
            )
        except LLMResponseInvalid:
            response = await llm.generate_structured_output(
                prompt + "\n\nReturn ONLY the structured object, every field present.",
                ClaimVerificationModel,
                system=VERIFICATION_SYSTEM,
            )
    except LLMResponseInvalid:
        return {"case_id": case["case_id"], "verdict": None, "agree": False,
                "expected": case["expected_verdict"], "failure": "unparseable twice"}

    verification = response.data
    warnings: list[str] = []
    relations = ClaimVerifier._enforced_relations(
        verification, labels, uuid.uuid4(), warnings
    )
    anchor = ClaimVerifier._verdict_anchor(verification.verdict, relations, labels)

    verdict, downgraded = verification.verdict.value, False
    if verdict in _EVIDENCE_BEARING and anchor is None:
        verdict, downgraded = "not_enough_evidence", True

    acceptable = case.get("acceptable_verdicts", [case["expected_verdict"]])
    return {
        "case_id": case["case_id"],
        "expected": case["expected_verdict"],
        "acceptable": acceptable,
        "verdict": verdict,
        "agree": verdict in acceptable,
        "raw_verdict": verification.verdict.value,
        "downgraded": downgraded,
        "confidence": verification.confidence,
        "rationale": verification.rationale,
        "enforcement_warnings": warnings,
        "model": response.model,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--case", action="append", default=[], metavar="ID")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = json.loads(VALIDATION_SET.read_text(encoding="utf-8"))
    cases = [
        case for case in payload["cases"]
        if not args.case or any(fragment in case["case_id"] for fragment in args.case)
    ]
    if not cases:
        print("No cases match the filter.", file=sys.stderr)
        return 2

    llm = LLMService()
    try:
        records = [await judge_case(llm, case) for case in cases]
    finally:
        await llm.aclose()

    agreed = sum(1 for record in records if record["agree"])
    outcome = {
        "prompt_version": VERIFICATION_PROMPT_VERSION,
        "validation_set_version": payload["version"],
        "run_at": datetime.now(UTC).isoformat(),
        "model": next((r["model"] for r in records if r.get("model")), None),
        "agreement": f"{agreed}/{len(records)}",
        "cases": records,
    }

    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        for record in records:
            mark = "OK  " if record["agree"] else "MISS"
            print(f"{mark} {record['case_id']:<28} expected={record['expected']:<21} "
                  f"got={record['verdict']}")
        print(f"\nAgreement: {agreed}/{len(records)} "
              f"({VERIFICATION_PROMPT_VERSION}, set v{payload['version']})")

    if not args.case:  # partial runs are spot checks, not measurements
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = RESULTS_DIR / f"{stamp}-{VERIFICATION_PROMPT_VERSION}.json"
        out.write_text(json.dumps(outcome, indent=2), encoding="utf-8")
        print(f"Written: {out.relative_to(ROOT)}")
    return 0 if agreed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
