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

from app.services.vectorstore import RetrievedChunk

SYNTHESIS_PROMPT_VERSION = "synthesis-grounded-v1"

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


def format_evidence_block(label: str, chunk: RetrievedChunk) -> str:
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
