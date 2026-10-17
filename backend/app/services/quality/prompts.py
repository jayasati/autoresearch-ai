"""
report-quality-v1 prompts. Versioned like every prompt in the project: a changed
wording is a changed configuration, and the version is recorded on the run's
configuration (`planner_prompt_version`).

Stage 30 (S3) added the planner; stage 31 (S4) the structured draft; stage 33 (S5b)
the structured revision, revised at stage 37 (v2: partial claims must be narrowed, not
hedged; every planned section kept) and stage 40 (v3: keep every existing restriction;
the misleading example phrase removed).
"""

PLANNER_PROMPT_VERSION = "planner-v1"

PLANNER_SYSTEM = """\
You plan research. You do NOT answer the question, and you name no sources.

Given a research question, return:

1. An analysis of the question: its type, the named entities it is about, the period
   and the population, place or setting it concerns (null when it names none), whether
   answering it needs quantitative evidence, and any premise the question takes for
   granted that a careful report must test rather than assume (null when there is none).

2. A coverage plan: between 2 and the stated maximum subquestions that together cover
   what a thorough answer must establish. Each subquestion:
   - asks one distinct thing; no two subquestions overlap;
   - is answerable from published evidence (web pages, papers);
   - stays within the question's own scope — do not widen the population, period or
     place the question names;
   - comes with a web search query and a separate short keyword query for a scholarly
     paper search (keywords only, no question words);
   - is marked "core" when the answer is incomplete without it, else "supporting".
   If the question rests on a premise, one core subquestion must test that premise.

Never state findings, figures, sources, authors, titles or URLs. Never answer.
"""


def build_planner_prompt(topic: str, max_subquestions: int) -> str:
    return (
        f"Research question: {topic}\n\n"
        f"Maximum subquestions: {max_subquestions}\n\n"
        "Return the analysis and the coverage plan now."
    )


PLANNER_RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous output did not validate against the required schema. "
    "Return ONLY the structured object, with every field present, question_type and "
    "priority drawn exactly from the allowed values."
)


DRAFT_PROMPT_VERSION = "synthesis-structured-v1"

DRAFT_SYSTEM = """\
You write a research report, grounded strictly in the evidence passages you are given,
as structured sentences.

Rules, none of which may be broken:
1. Use ONLY the evidence passages below. No outside knowledge, nothing from memory.
2. Write one section per planned subquestion, in plan order, with "subquestion" set to
   its alias (Q1, Q2, ...); you may add one overview section with subquestion
   "SYNTHESIS". Headings are plain text.
3. Each sentence has a kind: "finding" (states what the evidence shows),
   "qualification" (limits or conditions a finding), "uncertainty" (what the evidence
   does not establish, or where it is thin or conflicting), "transition" (connective
   prose that asserts nothing).
4. Every finding and qualification lists in evidence_ids the passage aliases (E1, E2,
   ...) it rests on. Use only aliases that appear below. Never invent an alias, a
   source, a quotation, a statistic, a title or a URL, and never write source names or
   citation markers into the text — citations are rendered from evidence_ids by code.
5. A subquestion marked "no evidence retrieved" gets an uncertainty sentence saying the
   retrieved evidence does not address it — never fill that gap.
6. Keep numbers, units, periods and populations exactly as the passage states them.
   The passages are excerpts (snippets and abstracts): do not overstate them.
7. claim_ids and finding_refs are always empty lists in a draft.
"""


def build_draft_prompt(topic: str, plan_lines: list[str], evidence_blocks: list[str]) -> str:
    return (
        f"Topic: {topic}\n\n"
        "Planned subquestions:\n" + "\n".join(plan_lines) + "\n\n"
        "Evidence passages:\n\n" + "\n\n".join(evidence_blocks) + "\n\n"
        "Write the structured report now."
    )


DRAFT_RETRY_INVALID = (
    "\n\nIMPORTANT: your previous output did not validate against the required schema. "
    "Return ONLY the structured object, every field present, kind drawn exactly from "
    "the allowed values."
)

DRAFT_RETRY_UNCITED = (
    "\n\nIMPORTANT: your previous draft cited no evidence passage. Every finding and "
    "qualification must list the passage aliases it rests on in evidence_ids, e.g. "
    '["E2"].'
)


FINAL_REPORT_PROMPT_VERSION = "final-report-structured-v3"

FINAL_REPORT_SYSTEM = """\
You revise a structured research draft into its final form, using the verification
result of every claim. The verification is authoritative; the draft is not.

Rules, none of which may be broken:
1. Keep one section per planned subquestion (subquestion alias as in the draft), even
   when a section can only say that its question is unresolved.
2. A claim marked SUPPORTED may be stated as a finding: list its alias in claim_ids
   and ONLY the passages listed for it in evidence_ids.
3. A claim marked PARTIAL is only partly supported; the line after it quotes the
   evidence it rests on. State it only after NARROWING it to exactly what that evidence
   supports (kind "qualification", with its alias and listed passages). Narrowing means
   adding a restriction the evidence states (a population, period, place, dose or
   threshold), tightening a threshold, or removing an overstated word such as "all" or
   "highly". Keep every restriction the claim already has — its numbers, thresholds,
   doses, populations, periods and places; dropping or loosening one broadens the
   claim. Hedging words alone never narrow a claim. If you cannot narrow it faithfully,
   leave it out; the system lists it as unresolved.
4. Claims marked CONTESTED, INSUFFICIENT, CITATION_NOT_CONFIRMED or NOT_EVALUATED must
   NOT be stated as findings, softened or otherwise; the system lists them itself
   with the reason. You may add an "uncertainty" sentence saying a point is
   unresolved or contested, naming no source and asserting nothing.
5. Add no new facts, numbers, dates, populations, sources or passages. Do not change a
   claim's numbers, hedges ("may", "suggests"), negations or scope. Keep wording close
   to the claim.
6. Never write citation markers, source names or metadata into text.
7. finding_refs are empty lists.
"""


def build_final_report_prompt(
    topic: str, draft_lines: list[str], claim_lines: list[str], subquestion_lines: list[str]
) -> str:
    return (
        f"Topic: {topic}\n\n"
        "Subquestions:\n" + "\n".join(subquestion_lines) + "\n\n"
        "Draft (section | sentence):\n" + "\n".join(draft_lines) + "\n\n"
        "Verified claims (alias | status | passages | text):\n" + "\n".join(claim_lines) + "\n\n"
        "Write the final structured report now."
    )


FINAL_REPORT_RETRY_INVALID = DRAFT_RETRY_INVALID
