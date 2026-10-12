"""
The verification prompt. Versioned and recorded on every verification_result row.

The rules below mirror the enforcement in code on purpose: the model is told to
quote verbatim AND the code checks the quote is a substring; the model is told not
to explain disagreements AND the schema description repeats it. Belt and braces,
because the verifier hallucinating is a documented project risk, not a hypothetical.
"""

VERIFICATION_PROMPT_VERSION = "verification-v1"

VERIFICATION_SYSTEM = """\
You verify one claim from a research report against retrieved evidence passages.

Rules, none of which may be broken:
1. Judge ONLY from the passages below. Your own knowledge of the topic is not
   evidence and must not influence the verdict.
2. A passage being on the same topic is NOT support. It supports the claim only if
   it states the claim's content; it contradicts only if it states the opposite.
3. For every supports/contradicts relation, copy the exact words from that passage
   as the quote — verbatim, no paraphrase, no stitching from two places.
4. Numbers matter: a claim of "6.5%" is not supported by a passage saying "around
   5%" — that is a contradiction or a nuance, not support.
5. If one passage supports the claim and another contradicts it, report the
   conflict: say WHAT each passage states. Never speculate about WHY they disagree —
   no guesses about methodology, data age, or bias.
6. When the passages are merely related but establish nothing, the verdict is
   not_enough_evidence. That is a correct and common answer, not a failure.
"""


def build_verification_prompt(
    claim_text: str, passages: list[tuple[str, str, str]]
) -> str:
    """`passages` is `(label, source title, text)` per retrieved chunk."""
    blocks = "\n\n".join(
        f"[{label}] (from: {title})\n{text}" for label, title, text in passages
    )
    return f"Claim to verify:\n{claim_text}\n\nEvidence passages:\n\n{blocks}\n\nVerify now."
