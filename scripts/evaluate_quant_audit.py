"""
Evaluate quant-audit-v1 against the labelled case set — locally, no provider calls.

    backend/.venv/Scripts/python.exe scripts/evaluate_quant_audit.py --split tuning
    backend/.venv/Scripts/python.exe scripts/evaluate_quant_audit.py --split held_out --write

Reports per-category precision and recall with Wilson 95% intervals, the confusion
matrix, and every misclassified case with the rule that fired. `--write` stores the
result under `data/benchmarks/results/quant-audit/` (a new directory per run;
nothing existing is touched).

The case set is AUTHOR-LABELLED (see its `labelling` block): the agent that wrote
the auditor also wrote the labels, so these numbers are not an independent
measurement and not the design's M3 result. Discipline: rule changes are made
against the tuning split only; the held-out split is reported once per auditor
version, and a later rule change means a new auditor version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.services.quality.models import QuantCategory  # noqa: E402
from app.services.quality.quantities import (  # noqa: E402
    QUANT_AUDIT_VERSION,
    classify_texts,
)

CASES = ROOT / "data" / "benchmarks" / "quantitative_audit_cases.json"
RESULTS = ROOT / "data" / "benchmarks" / "results" / "quant-audit"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)


def evaluate(split: str, approx_rel: float) -> dict:
    raw = CASES.read_bytes()
    document = json.loads(raw)
    cases = [c for c in document["cases"] if split == "all" or c["split"] == split]
    rows = []
    for case in cases:
        predicted, reason = classify_texts(
            case["text_a"],
            case["quantity_a"],
            case["text_b"],
            case["quantity_b"],
            entities=case["entities"],
            approx_rel=approx_rel,
        )
        rows.append(
            {
                "case_id": case["case_id"],
                "origin": case["origin"],
                "label": case["label"],
                "predicted": predicted.value,
                "reason": reason,
                "correct": predicted.value == case["label"],
            }
        )

    per_category = {}
    for category in QuantCategory:
        name = category.value
        tp = sum(1 for r in rows if r["predicted"] == name and r["label"] == name)
        predicted = sum(1 for r in rows if r["predicted"] == name)
        actual = sum(1 for r in rows if r["label"] == name)
        per_category[name] = {
            "tp": tp,
            "predicted": predicted,
            "actual": actual,
            "precision": round(tp / predicted, 3) if predicted else None,
            "precision_wilson95": wilson(tp, predicted),
            "recall": round(tp / actual, 3) if actual else None,
            "recall_wilson95": wilson(tp, actual),
        }
    confusion = Counter((r["label"], r["predicted"]) for r in rows)
    return {
        "auditor_version": QUANT_AUDIT_VERSION,
        "case_set": {
            "version": document["version"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "labelling": document["labelling"],
        },
        "split": split,
        "n": len(rows),
        "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3) if rows else None,
        "approx_rel_tolerance": approx_rel,
        "per_category": per_category,
        "confusion": [
            {"label": a, "predicted": b, "count": n} for (a, b), n in sorted(confusion.items())
        ],
        "rows": rows,
        "status": "author-labelled; provisional; not an independent measurement",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--split", choices=("tuning", "held_out", "all"), required=True)
    parser.add_argument("--write", action="store_true", help="store the result as an artifact")
    args = parser.parse_args()

    approx_rel = Settings(_env_file=None).REPORT_QUALITY_QUANT_APPROX_REL_TOLERANCE
    result = evaluate(args.split, approx_rel)

    print(
        f"{result['auditor_version']} on {args.split}: n={result['n']} "
        f"accuracy={result['accuracy']} (case set sha256 {result['case_set']['sha256'][:12]})"
    )
    for name, stats in result["per_category"].items():
        print(
            f"  {name:45s} P={stats['precision']} {stats['precision_wilson95']} "
            f"R={stats['recall']} {stats['recall_wilson95']} "
            f"(tp {stats['tp']}, predicted {stats['predicted']}, actual {stats['actual']})"
        )
    for row in result["rows"]:
        if not row["correct"]:
            print(
                f"  MISS {row['case_id']} [{row['origin']}] label={row['label']} "
                f"predicted={row['predicted']} — {row['reason']}"
            )

    if args.write:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        directory = RESULTS / f"{stamp}-{args.split}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {directory.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
