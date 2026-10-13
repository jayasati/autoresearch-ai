"""
The arm-blind external judge: one claim, one pool, one verdict.

Reuses the verification stage's machinery wholesale — the same prompt version
(`verification-v1`), the same structured-output schema, the same verbatim-quote
enforcement and the same downgrade rule — against the external pool instead of the
run's own evidence. Arm-blindness is structural: the judge's inputs are the claim
text and pool passages, nothing else; no run id, mode or arm name can reach the
prompt.

Nothing here writes to run-owned tables. The verdicts are returned to the runner,
which stores them in the raw results next to the pool hash — separate from
self-grounding verdicts by construction.
"""

import asyncio
import logging
import uuid
from collections import Counter

from app.services.llm import LLMResponseInvalid, LLMService
from app.services.vectorstore import VectorStore
from app.services.verification.models import ClaimVerificationModel
from app.services.verification.prompts import (
    VERIFICATION_PROMPT_VERSION,
    VERIFICATION_SYSTEM,
    build_verification_prompt,
)
from app.services.verification.verifier import TOP_K_PER_CLAIM, ClaimVerifier

logger = logging.getLogger(__name__)

EXTERNAL_JUDGE_VERSION = f"external-{VERIFICATION_PROMPT_VERSION}"
MAX_CONCURRENCY = 4

#: Verdicts that assert evidence and therefore need a surviving quoted relation;
#: mirrors the repository downgrade rule exactly.
_EVIDENCE_BEARING = {"supported", "partially_supported", "contradicted"}


async def judge_claims_against_pool(
    llm: LLMService,
    store: VectorStore,
    claims: list[dict],
    *,
    top_k: int = TOP_K_PER_CLAIM,
) -> dict:
    """Judge each claim (`{"claim_id", "text"}`) against the pool.

    Returns per-claim verdict records, the verdict counts, and the judging-failure
    count. A claim whose structured output fails validation twice is recorded as a
    failure and excluded from the verdict counts — never defaulted to any verdict.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def judge_one(claim: dict) -> dict:
        async with semaphore:
            return await _judge_claim(llm, store, claim, top_k)

    records = await asyncio.gather(*(judge_one(claim) for claim in claims))
    verdicts = Counter(
        record["verdict"] for record in records if record["verdict"] is not None
    )
    failures = sum(1 for record in records if record["verdict"] is None)
    return {
        "judge_version": EXTERNAL_JUDGE_VERSION,
        "claims": records,
        "verdicts": dict(verdicts),
        "judged": sum(verdicts.values()),
        "judging_failures": failures,
    }


async def _judge_claim(
    llm: LLMService, store: VectorStore, claim: dict, top_k: int
) -> dict:
    claim_id = claim["claim_id"]
    record: dict = {"claim_id": str(claim_id), "verdict": None, "confidence": None,
                    "downgraded": False, "quote": None, "evidence_count": 0}

    hits = await asyncio.to_thread(store.search, claim["text"], top_k)
    record["evidence_count"] = len(hits)
    if not hits:
        record["verdict"] = "not_enough_evidence"
        return record

    labels = {f"P{index + 1}": hit for index, hit in enumerate(hits)}
    prompt = build_verification_prompt(
        claim["text"],
        [(label, hit.source.title or hit.source.url or "untitled", hit.text)
         for label, hit in labels.items()],
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
        logger.warning("External judging failed twice for claim %s; excluded", claim_id)
        return record  # verdict stays None: a failure, not a verdict

    verification = response.data
    # The same quote enforcement the run-owned verifier applies, reused verbatim.
    relations = ClaimVerifier._enforced_relations(
        verification, labels, uuid.UUID(str(claim_id)), warnings=[]
    )
    anchor = ClaimVerifier._verdict_anchor(verification.verdict, relations, labels)

    verdict = verification.verdict.value
    downgraded = False
    if verdict in _EVIDENCE_BEARING and anchor is None:
        # The documented downgrade: an evidence-bearing verdict with no surviving
        # quoted relation is not_enough_evidence, and the downgrade is recorded.
        verdict, downgraded = "not_enough_evidence", True

    surviving_quotes = [quote for _, quote in relations.values() if quote]
    record.update(
        verdict=verdict,
        confidence=verification.confidence,
        downgraded=downgraded,
        quote=surviving_quotes[0] if surviving_quotes else None,
    )
    return record
