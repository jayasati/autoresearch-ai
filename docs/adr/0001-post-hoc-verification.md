# ADR 0001 — Verify claims after generation, not during

- **Status:** accepted
- **Date:** 2026-10-06

## Context
The system must both (a) produce grounded reports and (b) *measure* how grounded
they are. These pull in opposite directions. If groundedness is enforced inside
the writing step, the system looks good by construction and we learn nothing
about how often the model fabricates.

## Decision
Generation and verification are separate stages. The synthesizer writes; a
distinct claim-extraction and verification pass then audits the finished report
against retrieved evidence.

## Alternatives considered
- **Constrained decoding / cite-as-you-write.** Rejected: the writer's own
  judgement of support becomes both the mechanism and the measurement, so the
  hallucination rate it reports is unfalsifiable.
- **Human-only evaluation.** Rejected: does not scale to a benchmark across
  three modes and many topics, and is not reproducible by a grader.

## Consequences
- Hallucination becomes a measurable quantity, and the verifier is free to
  disagree with the writer — that disagreement is the project's main signal.
- Costs more LLM calls per run; mitigated by per-run caps in `core/config.py`.
- The verifier can itself be wrong, so it must quote a supporting span; a verdict
  with no quote is downgraded to `not_enough_evidence`.
