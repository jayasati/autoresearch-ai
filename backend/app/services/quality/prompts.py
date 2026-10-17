"""
report-quality-v1 prompts. Versioned like every prompt in the project: a changed
wording is a changed configuration, and the version is recorded on the run's
configuration (`planner_prompt_version`).

Stage 30 (S3) adds only the planner. The drafting and revision prompts arrive with
their stages (S4, S5).
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
