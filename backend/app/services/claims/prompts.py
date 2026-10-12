"""
The claim-extraction prompt. Versioned, like the synthesis prompts, and recorded on
every claim row via `extractor_model` + this version on the response: two reports
whose claims were extracted under different wordings are not comparable.
"""

CLAIM_EXTRACTION_PROMPT_VERSION = "claim-extraction-v1"

CLAIM_EXTRACTION_SYSTEM = """\
You extract the individually checkable factual claims from a research report.

What counts as a claim:
- An atomic assertion: one checkable statement per claim. A sentence asserting two
  things becomes two claims, each self-contained enough to verify alone.
- Classify each claim: factual (a verifiable statement), numerical (a quantity,
  rate or measurement), comparative (compares things), methodological (how a study
  or process was done), causal (one thing causes another), historical (past events
  or developments), conclusion (the report's own summarising judgement).
- Rate importance: high (central to answering the topic), medium, low (background).

What must NOT become a claim:
- Headings, and sentences that only announce what a section will discuss.
- Opinions and recommendations ("people should consult a doctor").
- Generic transitions and framing ("several factors play a role", "research is
  ongoing", "this report explores...").
- Purely stylistic or rhetorical statements.

Citations: for each claim, list the bracketed markers (like S1) the report cites
for that specific statement — only markers written with it, never markers you think
it deserves. A claim whose sentence cites nothing gets an empty list.

Copy the claim's section heading exactly as written after the ## marker. Set
requires_citation to false only for definitions and framing that assert nothing
checkable.

Extract every claim that qualifies, in reading order. Do not invent claims the
report does not make, and do not paraphrase numbers — keep them exact.
"""


def build_claim_extraction_prompt(report_markdown: str, known_labels: list[str]) -> str:
    labels = ", ".join(known_labels) if known_labels else "(none — the report cites no sources)"
    return (
        f"Source labels that exist in this report: {labels}\n\n"
        f"Report:\n\n{report_markdown}\n\n"
        "Extract the claims now."
    )


EXTRACTION_RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous output did not validate against the required "
    "schema. Return ONLY the structured object, with every field present on every "
    "claim, claim_type and importance drawn exactly from the allowed values, and "
    "citation_labels as plain markers like \"S1\"."
)
