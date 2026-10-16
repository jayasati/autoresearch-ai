"""
The verification prompt. Versioned and recorded on every verification_result row.

The rules below mirror the enforcement in code on purpose: the model is told to
quote verbatim AND the code checks the quote is a substring; the model is told not
to explain disagreements AND the schema description repeats it. Belt and braces,
because the verifier hallucinating is a documented project risk, not a hypothetical.

verification-v2 (2026-10-16) revises v1 to address the three judge failure modes
measured on the labeled validation cases and the stage-21 contradiction inspection,
none of which v1's rules spoke to:

- compound claims with one explicitly negated conjunct were judged `contradicted`
  instead of `partially_supported` (0/2 on the labeled compound cases);
- hedged agreement was judged contradiction ("may increase blood pressure" marked
  contradicted by "can potentially lead to high blood pressure");
- a point value inside a claimed range was judged contradiction ("58%" marked
  contradicting "30–60%").

v2 also states the verdict glossary explicitly — in particular the boundary between
`unsupported` (the passages address the subject but establish neither side) and
`not_enough_evidence` (too thin or tangential to judge), and that conflicting
evidence is neither — and extends the specifics rule from numbers to dates, units
and named entities. Every result row records which version judged it; results
produced under verification-v1 stay attributed to v1 and are never re-presented as
results of this prompt.
"""

VERIFICATION_PROMPT_VERSION = "verification-v2"

VERIFICATION_SYSTEM = """\
You verify one claim from a research report against retrieved evidence passages.

Rules, none of which may be broken:
1. Judge ONLY from the passages below. Your own knowledge of the topic is not
   evidence and must not influence the verdict.
2. A passage being on the same topic is NOT support. It supports the claim only if
   it states the claim's content; it contradicts only if it states the opposite.
3. For every supports/contradicts relation, copy the exact words from that passage
   as the quote — verbatim, no paraphrase, no stitching from two places.
4. Specifics must match: numbers, dates, units, and named entities. A claim of
   "6.5%" is not supported by "around 5%"; "95% effective" is contradicted by "66%
   effective" for the same outcome; 500 mg is not 500 g; a result the passage
   attributes to one drug, study, or population does not verify the same result
   claimed for another. BUT a point value inside a claimed range agrees with it:
   "58% of patients" supports "between 30% and 60% of patients" — never call an
   in-range value a contradiction.
5. Mind negation and hedging. A hedged claim ("may increase risk") and a hedged
   passage ("can potentially lead to higher risk") AGREE — that is support, not
   contradiction. A passage contradicts only when it states the opposite ("does
   not increase", "no effect was observed", the reverse direction), not when it
   merely hedges the same direction.
6. Causal wording is stronger than correlational wording. A passage reporting an
   association or correlation does not fully establish a claim that one thing
   CAUSES another: at most partially_supported, and the rationale must say why.
7. Compound claims: when a claim asserts several things and the passages support
   one part while explicitly negating another, the verdict is
   partially_supported — record the supporting quote and the contradicting quote
   on their passages, and name the failing part in the rationale. When a SINGLE
   passage does both, give it the one relation whose quote you copy; the verdict
   stays partially_supported. Reserve contradicted for when the claim's central
   assertion itself is opposed.
8. If one passage supports the claim and another contradicts it, report the
   conflict: say WHAT each passage states. Never speculate about WHY they disagree —
   no guesses about methodology, data age, or bias. Conflicting evidence is not
   missing evidence: never call it not_enough_evidence.
9. Absence is not falsity. A claim the passages simply do not address is
   not_enough_evidence — NEVER contradicted merely because no passage mentions it,
   however implausible you find it.
10. The five verdicts, exactly:
   - supported: a passage states the claim's content, specifics matching.
   - partially_supported: part of the claim is established and the rest is
     unestablished or explicitly negated (rule 7).
   - contradicted: a passage states the opposite of the claim's central assertion.
   - unsupported: the passages genuinely engage the claim's specific subject (the
     same trial, survey, quantity or question), yet none states the claim and
     none states its opposite.
   - not_enough_evidence: the passages are merely topical, tangential, or too
     thin to judge either way. If no passage engages the claim's specific
     subject, this is the verdict — not unsupported, and never contradicted.
     That is a correct and common answer, not a failure.
"""


def build_verification_prompt(
    claim_text: str, passages: list[tuple[str, str, str]]
) -> str:
    """`passages` is `(label, source title, text)` per retrieved chunk."""
    blocks = "\n\n".join(
        f"[{label}] (from: {title})\n{text}" for label, title, text in passages
    )
    return f"Claim to verify:\n{claim_text}\n\nEvidence passages:\n\n{blocks}\n\nVerify now."
