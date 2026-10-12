"""
The synthesis prompt: evidence in, a grounded, cited report out.

Versioned, because the prompt is part of what produced a report: two runs under
different prompt wordings are not comparable, and the version string is recorded on
the `report` row for exactly that reason. Change the wording, bump the version.

The system prompt forbids exactly the failures the benchmark will later measure:
uncited sentences, invented sources, overstated evidence. It also forbids the model
writing its own bibliography — the Sources section is appended by code, from the
actual retrieved sources, so a URL in the report can never be a hallucination.
"""

from app.services.processing import TextChunk
from app.services.vectorstore import RetrievedChunk

#: Anything with text, a source and a depth can be an evidence passage. HYBRID cites
#: processing chunks directly (provider order); SEARCH_GROUNDED cites retrieved ones.
EvidencePassage = RetrievedChunk | TextChunk

SYNTHESIS_PROMPT_VERSION = "synthesis-grounded-v1"
MODEL_ONLY_PROMPT_VERSION = "synthesis-model-only-v1"

SYNTHESIS_SYSTEM = """\
You write research reports grounded strictly in the evidence passages you are given.

Rules, none of which may be broken:
1. Use ONLY the evidence passages below. No outside knowledge, nothing from memory.
2. Every sentence that states a fact must cite its evidence with the bracketed label
   of the passage it came from, like [S1], or [S1][S3] when two passages support it.
3. Cite only labels that appear in the evidence. Never invent a label, a source,
   a quotation, a statistic, or a URL.
4. Where the evidence is thin, conflicting, or silent on part of the topic, say so
   plainly instead of filling the gap. "The retrieved evidence does not address X"
   is a correct finding.
5. The passages are web snippets and paper abstracts - excerpts, not full documents.
   Do not overstate what an excerpt establishes.
6. Format: Markdown. Start with a single `#` title. Then 2 to 5 `##` sections.
   Do NOT write a sources, references or bibliography section - the system appends
   the real one.
"""


def format_evidence_block(label: str, chunk: EvidencePassage) -> str:
    """One passage, headed by everything the model may cite it as."""
    source = chunk.source
    provenance = [source.provider or source.source_type.value]
    if source.year is not None:
        provenance.append(str(source.year))
    header = f"[{label}] {source.title or source.url or source.doi or 'untitled source'}"
    header += f" ({', '.join(provenance)}; evidence: {chunk.depth.value})"
    return f"{header}\n{chunk.text}"


def build_synthesis_prompt(topic: str, evidence_blocks: list[str]) -> str:
    passages = "\n\n".join(evidence_blocks)
    return (
        f"Topic: {topic}\n\n"
        f"Evidence passages:\n\n{passages}\n\n"
        "Write the grounded research report now."
    )


RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous draft contained no citations. Every factual "
    "sentence must end with the bracketed label of the passage supporting it, "
    "for example: 'Caffeine improved recall in adults [S2].'"
)


FINAL_REPORT_PROMPT_VERSION = "final-report-v1"

FINAL_REPORT_SYSTEM = """\
You rewrite a draft research report into its final form, using the verification
results for every claim. The verification is ground truth here; the draft is not.

Rules, none of which may be broken:
1. Present SUPPORTED claims as findings, keeping their bracketed citations [S#] —
   but only markers listed as valid for that claim.
2. PARTIALLY SUPPORTED claims must be explicitly qualified in the text, e.g. "The
   evidence partially supports ..." or "One source suggests ..., though the evidence
   does not establish ...". Keep their valid citations.
3. Claims whose verdict is unsupported, contradicted, or not_enough_evidence must
   NOT appear as findings. Do not state them, softened or otherwise. (They are not
   lost: the system appends a section listing every one of them with its reason.)
4. Where the verification found conflicting evidence, say so in the body where the
   topic arises — state what the sources disagree about, never why they might.
5. Add no new facts, no new numbers, no new citations. Everything in the final
   report must come from the draft's supported or partially supported content.
6. Format: Markdown. A single `#` title, then 2 to 5 `##` sections. Do NOT write
   sections for unverified claims, conflicting evidence or references — the system
   appends those from the stored records.
"""


def build_final_report_prompt(
    topic: str,
    draft_markdown: str,
    claim_lines: list[str],
    conflict_lines: list[str],
) -> str:
    conflicts = "\n".join(conflict_lines) if conflict_lines else "(none detected)"
    return (
        f"Topic: {topic}\n\n"
        f"Draft report:\n\n{draft_markdown}\n\n"
        f"Verification results, one line per claim:\n\n" + "\n".join(claim_lines) + "\n\n"
        f"Conflicts detected between sources:\n\n{conflicts}\n\n"
        "Write the final report now."
    )


MODEL_ONLY_SYSTEM = """\
You write research reports from your own knowledge. No evidence is provided and no
retrieval has happened — this is a deliberately ungrounded baseline configuration.

Rules, none of which may be broken:
1. Do NOT cite anything. No bracketed labels like [S1], no footnotes, and no
   references, sources or bibliography section. There are no sources; inventing one
   would be fabrication.
2. Do NOT state specific URLs, DOIs, paper titles, author names or publication
   venues. If you cannot name it without risking invention, do not name it.
3. State uncertainty plainly. Where your knowledge may be incomplete, dated, or
   contested, say so in the text.
4. Format: Markdown. Start with a single `#` title. Then 2 to 5 `##` sections.
"""


def build_model_only_prompt(topic: str) -> str:
    return f"Topic: {topic}\n\nWrite the research report now."
