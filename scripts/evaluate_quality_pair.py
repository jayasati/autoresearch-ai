"""
Paired evaluation of one report-quality-v1 run (docs/VERIFICATION_COMPARISON_PROTOCOL.md).

    OFF = the run's structured draft      ON = the same run's verified, revised final report

Both arms come from ONE run artifact (`scripts/run_quality_pipeline.py --execute`), so
question, plan, sources, passages and draft are identical; the comparison estimates
the COMBINED effect of verification plus verification-informed revision. Support is
judged relative to the frozen external evidence pool — not absolute factual accuracy.

    backend/.venv/Scripts/python.exe scripts/evaluate_quality_pair.py evaluate ARTIFACT_DIR \\
        --question-id dev-normal_research-01                      # dry run: no calls
    ... evaluate ARTIFACT_DIR --question-id ID --export-annotations   # blinded sheet, no calls
    ... evaluate ARTIFACT_DIR --question-id ID --annotations FILE.csv # score human labels
    ... evaluate ARTIFACT_DIR --question-id ID --execute --judge-model gpt-4o   # PAID
    ... aggregate EVAL_DIR [EVAL_DIR ...]

Blinding: every judged unit becomes an *item* (body sentence vs pool; or rendered
citation pair: sentence vs the passages behind one marker). Items carry random ids
from a seeded generator, are deduplicated across arms (an identical unit is judged
once and credited to every arm that has it) and shuffled. The judge — model or human
— sees items only; the arm key is a separate file joined after judgments are stored.

Judges: `--execute` uses the verification-v2 prompt and verbatim-quote enforcement
(the pool judge is the existing external judge, reused) with `--judge-model`; it
refuses the pipeline's own model unless `--allow-same-model` (then the evaluation is
labelled not independent). `--annotations` scores blinded human labels instead.

Development data only: the question id must be a `dev-*` item, the pool must be the
existing frozen pool-v1 file matching its manifest — a missing or changed pool is a
refusal, never a rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.core.constants import CitationOutcome, PoolStatus  # noqa: E402
from app.services.benchmark.metrics import MODEL_PRICES_PER_TOKEN  # noqa: E402
from app.services.llm import LLMResponseInvalid  # noqa: E402
from app.services.quality.validation import QUALIFIER_PREFIX, RESTORED_PREFIX  # noqa: E402
from app.services.quality.verification import RETRY_SUFFIX, passage_outcome  # noqa: E402
from app.services.verification.models import ClaimVerificationModel  # noqa: E402
from app.services.verification.prompts import (  # noqa: E402
    VERIFICATION_PROMPT_VERSION,
    VERIFICATION_SYSTEM,
    build_verification_prompt,
)
from app.services.verification.verifier import TOP_K_PER_CLAIM, ClaimVerifier  # noqa: E402

EVALUATOR_VERSION = "pair-eval-v1"
POOLS = ROOT / "data" / "benchmarks" / "pools" / "pool-v1"
DATASET = ROOT / "data" / "benchmarks" / "development.json"
ARMS = ("off", "on")
ASSERTION_KINDS = {"finding", "qualification"}
POOL_VERDICTS = (
    "supported",
    "partially_supported",
    "unsupported",
    "contradicted",
    "not_enough_evidence",
)
_HUMAN_TO_OUTCOME = {
    "supported": "supports",
    "partially_supported": "partially_supports",
    "contradicted": "contradicts",
    "unsupported": "does_not_establish",
    "not_enough_evidence": "does_not_establish",
}
_BODY = {"body", "qualified"}
_PREFIXES = (RESTORED_PREFIX, QUALIFIER_PREFIX)


class Refused(RuntimeError):
    """An input the protocol does not allow; nothing was called or written."""


# --- the two arms ------------------------------------------------------------------------


@dataclass
class Sentence:
    arm: str
    index: int  # reading order within the arm's body
    subquestion: str
    text: str  # as judged: templated prefixes removed
    kind: str
    citations: dict[str, list[str]]  # rendered marker -> the E# passages behind it
    claim_aliases: list[str] = field(default_factory=list)

    @property
    def assertion(self) -> bool:
        return self.kind in ASSERTION_KINDS or bool(self.citations)


def _strip(text: str) -> str:
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def build_off(payload: dict) -> list[Sentence]:
    """The draft as rendered: each sentence's markers are the source labels of its known
    passage aliases (`render_draft`'s rule), each marker backed by those passages."""
    passages = payload["evidence"]["aliases"]["passages"]
    aligned: dict[int, list[str]] = {}
    for alias, alignment in payload["alignments"].items():
        if alignment["status"] == "aligned" and alignment["sentence_index"] is not None:
            aligned.setdefault(alignment["sentence_index"], []).append(alias)
    sentences, index = [], 0
    for section in payload["draft"]["report"]["sections"]:
        for sentence in section["sentences"]:
            citations: dict[str, list[str]] = {}
            for alias in sentence["evidence_ids"]:
                if alias in passages:
                    citations.setdefault(passages[alias]["source_label"], []).append(alias)
            sentences.append(
                Sentence(
                    "off",
                    index,
                    section["subquestion"],
                    sentence["text"],
                    sentence["kind"],
                    citations,
                    sorted(aligned.get(index, [])),
                )
            )
            index += 1
    return sentences


def build_on(payload: dict) -> tuple[list[Sentence] | None, str | None]:
    """The final report body, or (None, why) — a failed finalization is a MISSING ON
    result, never replaced by the draft."""
    finalization = payload.get("finalization")
    if finalization is None:
        return None, "run did not reach finalization"
    if finalization["status"] == "finalization_failed":
        return None, "finalization_failed: " + "; ".join(finalization["reasons"])
    if "body" not in finalization:
        raise Refused("artifact predates the stage-38 body record; it cannot be evaluated")
    return [
        Sentence(
            "on",
            index,
            entry["subquestion"],
            _strip(entry["text"]),
            entry["kind"],
            entry["citations"],
            entry["claim_aliases"],
        )
        for index, entry in enumerate(finalization["body"])
    ], None


# --- blinded items -------------------------------------------------------------------------


def build_items(
    arms: dict[str, list[Sentence] | None], passages: dict[str, dict], seed: int
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Blinded, deduplicated, shuffled items plus the arm key. A pool item is an assertion
    sentence; a pair item is (sentence, the passages behind one rendered marker)."""
    units: dict[tuple, list[dict]] = {}
    contents: dict[tuple, dict] = {}
    for arm, sentences in arms.items():
        for sentence in sentences or []:
            if sentence.assertion:
                key: tuple = ("pool", sentence.text)
                contents.setdefault(key, {"type": "pool", "text": sentence.text})
                units.setdefault(key, []).append({"arm": arm, "sentence": sentence.index})
            for marker, cited in sentence.citations.items():
                if not cited:
                    continue  # unresolvable marker: scored without a judgment (see score)
                key = ("pair", sentence.text, tuple(passages[a]["chunk_id"] for a in cited))
                contents.setdefault(
                    key,
                    {
                        "type": "pair",
                        "text": sentence.text,
                        "passages": [
                            {
                                "label": f"P{i + 1}",
                                "title": passages[a]["title"],
                                "text": passages[a]["text"],
                            }
                            for i, a in enumerate(cited)
                        ],
                    },
                )
                units.setdefault(key, []).append(
                    {"arm": arm, "sentence": sentence.index, "marker": marker}
                )
    rng = random.Random(seed)
    order = sorted(contents, key=repr)  # canonical before the seeded shuffle
    rng.shuffle(order)
    items, arm_key = [], {}
    for key in order:
        item_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        items.append({"item_id": item_id, **contents[key]})
        arm_key[item_id] = units[key]
    return items, arm_key


# --- the runtime dollar ledger (stage 39) ---------------------------------------------------

#: Hard output limit sent with EVERY judge request (as max_completion_tokens).
OUTPUT_TOKEN_LIMIT = 800
#: Chat-format and response-format tokens beyond the text itself (messages, roles, the
#: structured-output wrapper) — a generous constant.
REQUEST_OVERHEAD_TOKENS = 256
#: The provider renders the response schema into the prompt in its own format; the
#: bound allows twice the JSON schema the SDK sends.
SCHEMA_BOUND_FACTOR = 2

#: List prices, USD per token, by model snapshot. Settlement prices the snapshot the
#: provider reports; a reservation prices the most expensive snapshot of the requested
#: model's family; an unlisted snapshot is charged at that maximum.
SNAPSHOT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-2024-05-13": (5.00e-6, 15.00e-6),
    "gpt-4o-2024-08-06": (2.50e-6, 10.00e-6),
    "gpt-4o-2024-11-20": (2.50e-6, 10.00e-6),
    "gpt-4o-mini-2024-07-18": (0.15e-6, 0.60e-6),
}
MODEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "gpt-4o": ("gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20"),
    "gpt-4o-mini": ("gpt-4o-mini-2024-07-18",),
}


class BudgetRefused(LLMResponseInvalid):
    """A judge request whose worst-case cost does not fit the remaining budget. Never
    sent; the item becomes a missing judgment."""


class JudgeCallFailed(LLMResponseInvalid):
    """A sent judge request that produced no usable judgment (truncated, malformed,
    timeout, provider error). Charged conservatively; a missing judgment."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"judge call failed: {reason}", details={"reason": reason})
        self.reason = reason


def worst_prices(model: str) -> tuple[float, float] | None:
    """The highest input and output price among the snapshots `model` may resolve to."""
    snapshots = MODEL_FAMILIES.get(model) or ((model,) if model in SNAPSHOT_PRICES else ())
    if not snapshots:
        return None
    return (
        max(SNAPSHOT_PRICES[s][0] for s in snapshots),
        max(SNAPSHOT_PRICES[s][1] for s in snapshots),
    )


def input_token_bound(prompt: str, system: str | None, schema) -> int:
    """An upper bound on the request's input tokens: byte-level BPE tokens are at least
    one byte each, so tokens <= UTF-8 bytes; plus the rendered schema (bounded at
    SCHEMA_BOUND_FACTOR x its JSON) and a constant overhead."""
    from openai.lib._pydantic import to_strict_json_schema

    schema_bytes = len(json.dumps(to_strict_json_schema(schema)).encode("utf-8"))
    text_bytes = len(prompt.encode("utf-8")) + len((system or "").encode("utf-8"))
    return text_bytes + SCHEMA_BOUND_FACTOR * schema_bytes + REQUEST_OVERHEAD_TOKENS


class DollarLedger:
    """One budget across every judge request and retry of an evaluation.

    reserve: before a request, its worst-case cost (input bound x the family's highest
    input price + the output limit x the highest output price) must fit
    `limit - spent - open reservations`, or it is refused and never sent.
    settle: with reported usage, the request is charged its measured tokens at the
    reported snapshot's price and the rest of the reservation is released; without
    usage (any failure), it is charged the full reservation — never refunded. If a
    measured charge ever exceeds its reservation (a bound assumption failed), the
    ledger closes: every later request is refused."""

    def __init__(self, limit_usd: float, model: str) -> None:
        prices = worst_prices(model)
        if prices is None:
            raise Refused(f"no listed price for judge model {model}; refusing to judge")
        self.limit_usd = limit_usd
        self.model = model
        self.reserve_prices = prices
        self.spent = 0.0
        self.open: dict[int, float] = {}
        self.entries: list[dict] = []
        self.closed: str | None = None

    @property
    def remaining(self) -> float:
        return self.limit_usd - self.spent - sum(self.open.values())

    def reserve(self, item_id: str | None, input_bound: int, output_limit: int) -> int:
        worst = input_bound * self.reserve_prices[0] + output_limit * self.reserve_prices[1]
        attempt = sum(1 for e in self.entries if e["item_id"] == item_id) + 1
        rid = len(self.entries)
        entry: dict = {
            "id": rid,
            "item_id": item_id,
            "attempt": attempt,
            "input_token_bound": input_bound,
            "output_token_limit": output_limit,
            "reserved_usd": round(worst, 8),
            "remaining_before_usd": round(self.remaining, 8),
        }
        self.entries.append(entry)
        if self.closed is not None or worst > self.remaining:
            entry.update(status="refused", charged_usd=0.0, reason=self.closed or "budget")
            raise BudgetRefused(
                f"worst-case cost {worst:.6f} exceeds the remaining {self.remaining:.6f}"
            )
        entry["status"] = "open"
        self.open[rid] = worst
        return rid

    def settle(self, rid: int, *, usage=None, model: str | None = None, reason=None) -> None:
        entry = self.entries[rid]
        reserved = self.open.pop(rid)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if prompt is not None and completion is not None:
            price = SNAPSHOT_PRICES.get(model or "", self.reserve_prices)
            charge = prompt * price[0] + completion * price[1]
            entry.update(
                charge_basis="measured",
                prompt_tokens=prompt,
                completion_tokens=completion,
                model=model,
                price_per_token=list(price),
            )
            if charge > reserved + 1e-12 or prompt > entry["input_token_bound"]:
                self.closed = f"bound exceeded on request {rid}; ledger closed"
                entry["bound_exceeded"] = True
        else:
            charge = reserved
            entry.update(charge_basis="reserved (usage unknown)")
        self.spent += charge
        entry.update(
            status="failed" if reason else "ok",
            reason=reason,
            charged_usd=round(charge, 8),
            remaining_after_usd=round(self.remaining, 8),
        )

    def as_record(self) -> dict:
        statuses = [e["status"] for e in self.entries]
        return {
            "limit_usd": self.limit_usd,
            "judge_model": self.model,
            "reservation_prices_per_token": list(self.reserve_prices),
            "output_token_limit": OUTPUT_TOKEN_LIMIT,
            "input_bound": "UTF-8 bytes of system + prompt + "
            f"{SCHEMA_BOUND_FACTOR}x strict JSON schema + {REQUEST_OVERHEAD_TOKENS}",
            "spent_usd": round(self.spent, 8),
            "open_reservations_usd": round(sum(self.open.values()), 8),
            "remaining_usd": round(self.remaining, 8),
            "requests": len(self.entries),
            "sent": sum(s in ("ok", "failed") for s in statuses),
            "ok": statuses.count("ok"),
            "failed": statuses.count("failed"),
            "refused": statuses.count("refused"),
            "retries": sum(1 for e in self.entries if e["attempt"] > 1),
            "charged_at_reservation": sum(
                1 for e in self.entries if e.get("charge_basis", "").startswith("reserved")
            ),
            "closed": self.closed,
            "entries": self.entries,
        }


class LedgeredLLM:
    """The judge client behind the ledger: every request carries the output limit and is
    reserved first; every failure is charged and surfaces as a missing judgment."""

    def __init__(self, inner, ledger: DollarLedger) -> None:
        self._inner = inner
        self.ledger = ledger
        self.item_id: str | None = None

    async def generate_structured_output(self, prompt, schema, *, system=None, **kwargs):
        kwargs["max_tokens"] = min(kwargs.get("max_tokens") or OUTPUT_TOKEN_LIMIT,
                                   OUTPUT_TOKEN_LIMIT)  # fmt: skip
        rid = self.ledger.reserve(
            self.item_id, input_token_bound(prompt, system, schema), kwargs["max_tokens"]
        )
        try:
            response = await self._inner.generate_structured_output(
                prompt, schema, system=system, **kwargs
            )
        except LLMResponseInvalid as exc:
            finish = (getattr(exc, "details", None) or {}).get("finish_reason")
            reason = "truncated" if finish == "length" else "malformed"
            self.ledger.settle(rid, reason=reason)
            raise JudgeCallFailed(reason) from exc
        except Exception as exc:  # noqa: BLE001 - timeout, provider error: charged, missing
            self.ledger.settle(rid, reason=type(exc).__name__)
            raise JudgeCallFailed(type(exc).__name__) from exc
        self.ledger.settle(
            rid, usage=getattr(response, "usage", None), model=getattr(response, "model", None)
        )
        return response

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


def missing_reason(item_id: str, ledger: DollarLedger | None) -> str:
    """Why an item has no judgment: its last request's outcome."""
    if ledger is None:
        return "no_judgment"
    attempts = [e for e in ledger.entries if e["item_id"] == item_id]
    if not attempts:
        return "no_request"
    last = attempts[-1]
    if last["status"] == "refused":
        return "budget_refused"
    return last.get("reason") or "invalid_output"


# --- judges ----------------------------------------------------------------------------------


async def judge_pair_llm(llm, item: dict) -> dict:
    """Sentence vs the passages behind one marker: verification-v2 prompt, schema and
    verbatim-quote enforcement; the pair's outcome is the best passage outcome."""
    labels = {p["label"]: _Passage(p["text"]) for p in item["passages"]}
    prompt = build_verification_prompt(
        item["text"], [(p["label"], p["title"] or "untitled", p["text"]) for p in item["passages"]]
    )
    try:
        try:
            response = await llm.generate_structured_output(
                prompt, ClaimVerificationModel, system=VERIFICATION_SYSTEM
            )
        except LLMResponseInvalid:
            response = await llm.generate_structured_output(
                prompt + RETRY_SUFFIX, ClaimVerificationModel, system=VERIFICATION_SYSTEM
            )
    except LLMResponseInvalid:
        return {"item_id": item["item_id"], "outcome": None, "source": "llm"}
    data = response.data
    relations = ClaimVerifier._enforced_relations(
        data,
        labels,  # type: ignore[arg-type]  # only .text is read
        uuid.UUID(item["item_id"]),
        [],
    )
    verdict = PoolStatus(data.verdict.value)
    outcomes = {passage_outcome(relation, verdict) for relation, _ in relations.values()}
    return {
        "item_id": item["item_id"],
        "outcome": _best(outcomes).value,
        "verdict": verdict.value,
        "quotes": [quote for _, quote in relations.values() if quote],
        "source": "llm",
    }


@dataclass
class _Passage:
    text: str


def _best(outcomes: set[CitationOutcome]) -> CitationOutcome:
    for outcome in (
        CitationOutcome.SUPPORTS,
        CitationOutcome.PARTIALLY_SUPPORTS,
        CitationOutcome.CONTRADICTS,
    ):
        if outcome in outcomes:
            return outcome
    return CitationOutcome.DOES_NOT_ESTABLISH


async def judge_items_llm(items: list[dict], llm, pool_store) -> list[dict]:
    """Every item, one at a time: pool items through the existing external judge's
    per-claim function, pair items as above. Sequential so each request is tagged with
    its item (for the ledger's audit and the missing-judgment reasons)."""
    from app.services.benchmark.external_judge import _judge_claim

    judgments = []
    for item in items:
        if isinstance(llm, LedgeredLLM):
            llm.item_id = item["item_id"]
        if item["type"] == "pool":
            record = await _judge_claim(
                llm, pool_store, {"claim_id": item["item_id"], "text": item["text"]},
                TOP_K_PER_CLAIM,
            )  # fmt: skip
            judgments.append(
                {"item_id": item["item_id"], "verdict": record["verdict"],
                 "quote": record["quote"], "source": "llm"}
            )  # fmt: skip
        else:
            judgments.append(await judge_pair_llm(llm, item))
    ledger = llm.ledger if isinstance(llm, LedgeredLLM) else None
    for judgment in judgments:
        if judgment.get("verdict") is None and judgment.get("outcome") is None:
            judgment["missing_reason"] = missing_reason(judgment["item_id"], ledger)
    return judgments


def export_annotations(items: list[dict], pool_hits, path: Path) -> None:
    """A blinded sheet: item id, type, text, the passages to judge against (for a pool
    item, the pool's top passages), and empty `verdict` / `notes` columns."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item_id", "type", "text", "passages", "verdict", "notes"])
        for item in items:
            shown = item.get("passages") or pool_hits(item["text"])
            writer.writerow(
                [
                    item["item_id"],
                    item["type"],
                    item["text"],
                    "\n\n".join(f"[{p['label']}] ({p['title']}) {p['text']}" for p in shown),
                    "",
                    "",
                ]
            )


def read_annotations(path: Path, items: list[dict]) -> list[dict]:
    """Human labels -> judgments. A blank or unknown verdict is a missing judgment."""
    types = {item["item_id"]: item["type"] for item in items}
    judgments = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["item_id"] not in types:
                raise Refused(f"annotation for unknown item {row['item_id']}")
            verdict = row["verdict"].strip().lower() or None
            if verdict not in POOL_VERDICTS:
                verdict = None
            judgment = {"item_id": row["item_id"], "source": "human"}
            if types[row["item_id"]] == "pair":
                judgment["outcome"] = _HUMAN_TO_OUTCOME.get(verdict) if verdict else None
            else:
                judgment["verdict"] = verdict
            judgments.append(judgment)
    return judgments


# --- scoring ---------------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _fate(aliases: list[str], finalization: dict) -> str:
    """Where a draft sentence's claims ended in ON."""
    if not aliases:
        return "untracked"
    placements = finalization.get("placements", {})
    omitted = set(finalization.get("metrics", {}).get("claims_omitted", []))
    fates = []
    for alias in aliases:
        where = placements.get(alias)
        if where in _BODY:
            fates.append("body")
        elif where == "contested":
            fates.append("contested")
        elif where is not None:
            fates.append("listed")
        elif alias in omitted:
            fates.append("omission_recorded")
        else:
            fates.append("unaccounted")
    for fate in ("body", "contested", "listed", "omission_recorded"):
        if fate in fates:
            return fate
    return "unaccounted"


def score(
    arms: dict[str, list[Sentence] | None],
    arm_key: dict[str, list[dict]],
    judgments: list[dict],
    payload: dict,
    missing_on_reason: str | None,
) -> dict:
    """Per-arm metrics and paired comparisons, every rate with its counts."""
    pool_verdict: dict[tuple[str, int], str | None] = {}
    pair_outcome: dict[tuple[str, int, str], str | None] = {}
    for judgment in judgments:
        for unit in arm_key[judgment["item_id"]]:
            if "marker" in unit:
                pair_outcome[(unit["arm"], unit["sentence"], unit["marker"])] = judgment.get(
                    "outcome"
                )
            else:
                pool_verdict[(unit["arm"], unit["sentence"])] = judgment.get("verdict")
    core = [s["alias"] for s in payload["plan"]["subquestions"] if s["is_core"]]
    result: dict = {"arms": {}, "missing_on": missing_on_reason}

    for arm in ARMS:
        sentences = arms.get(arm)
        if sentences is None:
            result["arms"][arm] = None
            continue
        assertions = [s for s in sentences if s.assertion]
        verdicts = [pool_verdict.get((arm, s.index)) for s in assertions]
        judged = [v for v in verdicts if v is not None]
        counts = {v: judged.count(v) for v in POOL_VERDICTS}
        pairs = [(s, m, cited) for s in sentences for m, cited in s.citations.items()]
        outcomes = [
            "unresolvable" if not cited else pair_outcome.get((arm, s.index, m))
            for s, m, cited in pairs
        ]
        scored = [o for o in outcomes if o is not None]
        strict_ok = {"supported"}
        lenient_ok = {"supported", "partially_supported"}

        def covered(ok: set[str], arm=arm, assertions=assertions) -> list[str]:
            return [
                alias
                for alias in core
                if any(
                    s.subquestion == alias and pool_verdict.get((arm, s.index)) in ok
                    for s in assertions
                )
            ]

        result["arms"][arm] = {
            "body_sentences": len(sentences),
            "assertion_sentences": len(assertions),
            "pool": {
                "judged": len(judged),
                "missing_judgments": len(verdicts) - len(judged),
                "counts": counts,
                "support_strict": _rate(counts["supported"], len(judged)),
                "support_lenient": _rate(
                    counts["supported"] + counts["partially_supported"], len(judged)
                ),
                "unsupported_or_contradicted": _rate(
                    counts["unsupported"] + counts["contradicted"], len(judged)
                ),
                "contradicted": _rate(counts["contradicted"], len(judged)),
                "not_enough_evidence": _rate(counts["not_enough_evidence"], len(judged)),
            },
            "citations": {
                "pairs": len(pairs),
                "judged": len(scored),
                "missing_judgments": len(outcomes) - len(scored),
                "unresolvable": outcomes.count("unresolvable"),
                "multi_passage": sum(1 for _, _, cited in pairs if len(cited) > 1),
                "counts": {o: scored.count(o) for o in sorted(set(scored))},
                "strict": _rate(scored.count("supports"), len(scored)),
                "lenient": _rate(
                    scored.count("supports") + scored.count("partially_supports"), len(scored)
                ),
                "contradicted": _rate(scored.count("contradicts"), len(scored)),
            },
            "core": {
                "core_subquestions": core,
                "covered_strict": covered(strict_ok),
                "covered_lenient": covered(lenient_ok),
                "coverage_strict": _rate(len(covered(strict_ok)), len(core)),
                "coverage_lenient": _rate(len(covered(lenient_ok)), len(core)),
            },
        }

    off, finalization = arms["off"] or [], payload.get("finalization") or {}

    def fates(ok: set[str]) -> dict:
        tracked = [s for s in off if s.assertion and pool_verdict.get(("off", s.index)) in ok]
        if arms.get("on") is None:
            return {"sentences": len(tracked), "fates": {"on_missing": len(tracked)}}
        found = [_fate(s.claim_aliases, finalization) for s in tracked]
        return {"sentences": len(tracked), "fates": {f: found.count(f) for f in sorted(set(found))}}

    result["paired"] = {
        # P3: draft sentences the pool contradicts, and what ON did with their claims
        "contradiction_handling": fates({"contradicted"}),
        # supported draft findings and where they ended (lost = anything but body)
        "supported_findings_strict": fates({"supported"}),
        "supported_findings_lenient": fates({"supported", "partially_supported"}),
        "on_unanswered_core_disclosed": finalization.get("metrics", {}).get("unanswered_core"),
    }
    return result


def usage(run: dict) -> dict:
    """Per-arm latency, tokens and cost from the run's own accounts. OFF = planning and
    drafting; ON = the whole run. OFF latency is approximate: claim extraction runs
    inside draft_and_align and its model time is subtracted."""
    budget = run.get("budget") or {"stages": {}}
    stages = budget["stages"]
    seconds = run.get("stage_seconds", {})
    price = _price(run.get("configuration", {}).get("model"))

    def account(names) -> dict:
        picked = [stages[n] for n in names if n in stages]
        prompt = sum(s.get("measured_prompt_tokens", 0) for s in picked)
        completion = sum(s.get("measured_completion_tokens", 0) for s in picked)
        estimated = sum(s["estimated_tokens"] for s in picked)
        return {
            "attempts": sum(s["attempts"] for s in picked),
            "tokens_measured": sum(s["measured_tokens"] for s in picked),
            "tokens_estimated": estimated,
            "cost_measured_usd": None
            if price is None
            else round(prompt * price[0] + completion * price[1], 6),
            "cost_estimated_upper_usd": None if price is None else round(estimated * price[1], 6),
        }

    off_seconds = (
        seconds.get("prepare_evidence", 0)
        + seconds.get("draft_and_align", 0)
        - stages.get("extraction", {}).get("seconds", 0)
    )
    return {
        "off": {**account(["planning", "drafting"]), "latency_seconds": round(off_seconds, 3)},
        "on": {**account(list(stages)), "latency_seconds": round(sum(seconds.values()), 3)},
    }


def _price(model: str | None) -> tuple[float, float] | None:
    match = max(
        (p for p in MODEL_PRICES_PER_TOKEN if model and model.startswith(p)), key=len, default=None
    )
    return MODEL_PRICES_PER_TOKEN[match] if match else None


# --- inputs and their guards ---------------------------------------------------------------


def load_artifact(directory: Path) -> tuple[dict, dict]:
    run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((directory / "diagnostics.json").read_text(encoding="utf-8"))
    return run, diagnostics


def check_question(run: dict, question_id: str, dataset: Path) -> None:
    if not question_id.startswith("dev-"):
        raise Refused(f"{question_id}: only development questions may be evaluated")
    items = {i["question_id"]: i for i in json.loads(dataset.read_text("utf-8"))["items"]}
    if question_id not in items:
        raise Refused(f"{question_id} is not in {dataset.name}")
    if items[question_id]["question"] != run["question"]:
        raise Refused("the artifact's question is not this item's question")


def check_pool(question_id: str, pools: Path) -> Path:
    """The frozen pool file, verified against its manifest. Never built here."""
    path = pools / f"{question_id}.json"
    if not path.is_file():
        raise Refused(f"no frozen pool for {question_id}; pools are never built here")
    manifest = {}
    for line in (pools / "MANIFEST.sha256").read_text(encoding="utf-8-sig").splitlines():
        if line and not line.startswith("#"):
            digest, name = line.split(maxsplit=1)
            manifest[name.strip()] = digest
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest.get(path.name) != actual:
        raise Refused(f"{path.name} does not match MANIFEST.sha256; refusing to use it")
    return path


#: Output tokens assumed per judged item for the expected-cost figure.
OUTPUT_TOKENS_PER_ITEM = 600


def estimate(items: list[dict], pool_path: Path, judge_model: str | None) -> dict:
    """Pre-call figures, both at the RESERVATION prices (the judge family's highest):
    `cost_usd` expected (input characters / 4, 600 output per item) and
    `worst_case_usd` (every item's request and one retry at its full reservation:
    the byte-bounded input and the 800-token output limit). The runtime ledger, not
    this estimate, is what enforces `--max-usd`."""
    chunks = json.loads(pool_path.read_text(encoding="utf-8"))["chunks"]
    mean_chunk = mean(len(c["text"]) for c in chunks) if chunks else 0
    sample = "x" * int(TOP_K_PER_CLAIM * mean_chunk)
    input_tokens, worst_tokens = 0, 0
    for item in items:
        context = "".join(p["text"] for p in item["passages"]) if item["type"] == "pair" else sample
        prompt = item["text"] + context
        input_tokens += int((len(VERIFICATION_SYSTEM) + len(prompt)) / 4)
        worst_tokens += input_token_bound(prompt, VERIFICATION_SYSTEM, ClaimVerificationModel)
    output_tokens = OUTPUT_TOKENS_PER_ITEM * len(items)
    price = worst_prices(judge_model) if judge_model else None
    cost = worst = None
    if price is not None:
        cost = input_tokens * price[0] + output_tokens * price[1]
        worst = 2 * (worst_tokens * price[0] + OUTPUT_TOKEN_LIMIT * len(items) * price[1])
    return {
        "items": len(items),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens": input_tokens + output_tokens,
        "cost_usd": None if cost is None else round(cost, 4),
        "worst_case_usd": None if worst is None else round(worst, 4),
    }


# --- the command -----------------------------------------------------------------------------


def evaluate(args: argparse.Namespace, *, judge=None, pool_store=None, pool_hits=None) -> dict:
    """Build arms and blinded items; then (by mode) stop, export, or judge and score.
    `judge`, `pool_store` and `pool_hits` are injectable for offline tests."""
    run, diagnostics = load_artifact(args.artifact)
    check_question(run, args.question_id, args.dataset)
    pool_path = check_pool(args.question_id, args.pools)
    payload = run["persisted"]["audit"]["payload"]
    passages = {
        p["alias"]: {
            "chunk_id": p["chunk_id"],
            "title": (p.get("source") or {}).get("title") or "untitled",
            "text": p["text"],
        }
        for p in diagnostics["passages"]
    }
    on, missing_on = build_on(payload)
    arms = {"off": build_off(payload), "on": on}
    items, arm_key = build_items(arms, passages, args.seed)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "items.jsonl", items, lines=True)  # what any judge may see
    _write(out / "key.json", arm_key)  # joined only after judgments are stored
    cost = estimate(items, pool_path, args.judge_model)
    manifest = {
        "evaluator_version": EVALUATOR_VERSION,
        "judge_prompt_version": VERIFICATION_PROMPT_VERSION,
        "artifact": str(args.artifact),
        "research_id": run.get("research_id"),
        "question_id": args.question_id,
        "pool": {
            "file": pool_path.name,
            "sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest(),
        },
        "seed": args.seed,
        "pipeline_model": run.get("configuration", {}).get("model"),
        "judge_model": args.judge_model,
        "missing_on": missing_on,
        "estimate": cost,
    }
    _write(out / "manifest.json", manifest)
    if args.export_annotations:
        export_annotations(items, pool_hits or (lambda text: []), out / "annotations.csv")
        return {"mode": "exported", "items": len(items), "estimate": cost}
    if args.annotations:
        judgments = read_annotations(args.annotations, items)
        independence = "human annotation"
    elif args.execute or judge is not None:
        if judge is None:
            raise Refused("--execute needs a live judge; see main()")
        judgments = asyncio.run(judge(items))
        same = args.judge_model == manifest["pipeline_model"]
        independence = "NOT independent (same model as the pipeline)" if same else "different model"
    else:
        return {"mode": "dry_run", "items": len(items), "estimate": cost}
    _write(out / "judgments.jsonl", judgments, lines=True)  # stored before unblinding
    ledger = getattr(judge, "ledger", None)
    if ledger is not None:
        _write(out / "budget.json", ledger.as_record())
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    scores = score(arms, key, judgments, payload, missing_on)
    scores.update(
        question_id=args.question_id,
        evaluator_version=EVALUATOR_VERSION,
        independence=independence,
        usage=usage(run),
        finalization_status=(payload.get("finalization") or {}).get("status"),
        framing="support relative to the frozen evidence pool, not absolute factual accuracy",
        missing_judgments_by_reason=_reasons(judgments),
        evaluation_cost=None
        if ledger is None
        else {k: v for k, v in ledger.as_record().items() if k != "entries"},
    )
    _write(out / "scores.json", scores)
    return {"mode": "scored", "items": len(items), "scores": scores}


def _reasons(judgments: list[dict]) -> dict[str, int]:
    found = [j.get("missing_reason", "no_judgment") for j in judgments
             if j.get("verdict") is None and j.get("outcome") is None]  # fmt: skip
    return {reason: found.count(reason) for reason in sorted(set(found))}


def aggregate(directories: list[Path]) -> dict:
    """Macro means over questions with both arms (primary), plus the missing-ON
    sensitivity view (a missing ON scores 0 support, 0 core coverage), with counts."""
    scores = [json.loads((d / "scores.json").read_text(encoding="utf-8")) for d in directories]
    paired = [s for s in scores if s["arms"]["on"] is not None]

    def macro(rows, arm, path, *, missing_as=None):
        values = []
        for row in rows:
            node = row["arms"][arm]
            if node is None:
                if missing_as is not None:
                    values.append(missing_as)
                continue
            for part in path:
                node = node[part]
            if node is not None:
                values.append(node)
        return {"mean": round(mean(values), 4) if values else None, "n": len(values)}

    metrics = {
        "citation_support_strict": ("citations", "strict"),
        "citation_support_lenient": ("citations", "lenient"),
        "pool_support_strict": ("pool", "support_strict"),
        "pool_unsupported_or_contradicted": ("pool", "unsupported_or_contradicted"),
        "pool_contradicted": ("pool", "contradicted"),
        "core_coverage_strict": ("core", "coverage_strict"),
    }
    zero_if_missing = {"citation_support_strict", "citation_support_lenient",
                       "pool_support_strict", "core_coverage_strict"}  # fmt: skip
    return {
        "questions": len(scores),
        "paired": len(paired),
        "missing_on": [s["question_id"] for s in scores if s["arms"]["on"] is None],
        "primary_paired": {
            name: {arm: macro(paired, arm, path) for arm in ARMS} for name, path in metrics.items()
        },
        "sensitivity_missing_on_as_zero": {
            name: {
                "off": macro(scores, "off", path),
                "on": macro(scores, "on", path, missing_as=0.0),
            }
            for name, path in metrics.items()
            if name in zero_if_missing
        },
    }


def _write(path: Path, value, *, lines: bool = False) -> None:
    if lines:
        text = "".join(json.dumps(v, ensure_ascii=False, default=str) + "\n" for v in value)
    else:
        text = json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n"
    path.write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paired draft-vs-final evaluation.")
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("evaluate")
    ev.add_argument("artifact", type=Path)
    ev.add_argument("--question-id", required=True)
    ev.add_argument("--out", type=Path)
    ev.add_argument("--seed", type=int, default=20261017)
    ev.add_argument("--judge-model")
    mode = ev.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="PAID: judge with --judge-model")
    mode.add_argument("--export-annotations", action="store_true")
    mode.add_argument("--annotations", type=Path)
    ev.add_argument("--allow-same-model", action="store_true")
    ev.add_argument("--max-usd", type=float, default=0.50)
    ev.add_argument("--pools", type=Path, default=POOLS)
    ev.add_argument("--dataset", type=Path, default=DATASET)
    ag = sub.add_parser("aggregate")
    ag.add_argument("evaluations", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "evaluate":
        args.out = args.out or args.artifact / "evaluation"
        if args.execute and not args.judge_model:
            parser.error("--execute requires --judge-model")
    return args


def _pool_store(args, settings: Settings, pool_path: Path):
    """The frozen pool indexed (local embeddings, no paid call) in a store inside the
    evaluation directory — never the project vector store. Retrieval cannot run."""
    from app.services.benchmark.external_pool import load_or_build_pool
    from app.services.vectorstore import VectorStore, get_default_embedder

    class _Refuse:  # the pool exists and was verified; building one must never happen
        async def search(self, *a, **k):
            raise Refused("pool retrieval is not allowed")

        async def aclose(self):
            return None

    pool_settings = settings.model_copy(update={"BENCHMARK_DATA_DIR": str(pool_path.parents[2])})

    async def load():
        _, store = await load_or_build_pool(
            args.question_id,
            "",
            settings=pool_settings,
            store_factory=lambda name: VectorStore(
                embedder=get_default_embedder(),
                persist_dir=str(args.out / "vectorstore"),
                collection_name=name,
            ),
            web_search=_Refuse(),
            academic_search=_Refuse(),
        )
        return store

    return load


def _pool_hits(store):
    def hits(text: str) -> list[dict]:
        return [
            {"label": f"P{i + 1}", "title": h.source.title or h.source.url or "untitled",
             "text": h.text}
            for i, h in enumerate(store.search(text, TOP_K_PER_CLAIM))
        ]  # fmt: skip

    return hits


def _live_judge(args, settings: Settings, pool_path: Path):
    """The paid path: the judge model behind the dollar ledger (`--max-usd`), SDK
    retries off (each attempt is exactly one request), over the frozen pool."""
    from app.services.llm import LLMService
    from app.services.quality.budget import no_sdk_retries

    judge_settings = no_sdk_retries(settings).model_copy(update={"OPENAI_MODEL": args.judge_model})
    load_store = _pool_store(args, settings, pool_path)
    ledger = DollarLedger(args.max_usd, args.judge_model)

    async def judge(items):
        store = await load_store()
        llm = LedgeredLLM(LLMService(settings=judge_settings), ledger)
        try:
            return await judge_items_llm(items, llm, store)
        finally:
            await llm.aclose()

    judge.ledger = ledger  # type: ignore[attr-defined]
    return judge


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "aggregate":
            print(json.dumps(aggregate(args.evaluations), indent=2))
            return 0
        if args.execute:
            run, _ = load_artifact(args.artifact)
            pipeline_model = run.get("configuration", {}).get("model")
            if args.judge_model == pipeline_model and not args.allow_same_model:
                raise Refused(
                    f"judge model {args.judge_model} is the pipeline's own model; pass "
                    "--allow-same-model to run anyway (labelled not independent)"
                )
            check_question(run, args.question_id, args.dataset)
            pool_path = check_pool(args.question_id, args.pools)
            dry = evaluate(argparse.Namespace(**{**vars(args), "execute": False}))
            cost = dry["estimate"]
            if cost["cost_usd"] is None or cost["cost_usd"] > args.max_usd:
                raise Refused(
                    f"expected cost {cost['cost_usd']} exceeds --max-usd {args.max_usd} "
                    "(or the judge model has no listed price)"
                )
            settings = Settings()
            if settings.llm_provider != "openai":
                raise Refused("the judge needs the OpenAI provider (listed prices); none set")
            result = evaluate(args, judge=_live_judge(args, settings, pool_path))
        elif args.export_annotations:
            run, _ = load_artifact(args.artifact)
            check_question(run, args.question_id, args.dataset)
            pool_path = check_pool(args.question_id, args.pools)
            store = asyncio.run(_pool_store(args, Settings(), pool_path)())
            result = evaluate(args, pool_hits=_pool_hits(store))
        else:
            result = evaluate(args)
    except Refused as exc:
        print(f"Refused: {exc}")
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "scores"}, indent=2))
    if result.get("mode") == "scored":
        print(f"Scores: {args.out / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
