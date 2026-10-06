# Evaluation protocol

*Stub — filled in during stage 6.*

## Metrics to define precisely before measuring anything

| Metric | Rough intent |
|---|---|
| Claim support rate | fraction of extracted claims verified as `supported` |
| Hallucination rate | fraction `unsupported` or `contradicted` |
| Citation precision | of citations present, fraction that are `valid` |
| Citation recall | of claims needing a citation, fraction that have a valid one |
| Fabrication rate | fraction of citations that are `fabricated` |
| Coverage | fraction of planned sub-questions actually addressed |
| Source diversity | distinct domains / distinct venues per run |
| Evidence depth | abstract-only vs. full-text grounding per source |
| Cost | tokens and USD per run, from `llm_call_log` |

Every metric needs a written definition **before** the first benchmark run, so
the numbers cannot be reverse-engineered to look good.

## Topic set
Lives in `data/benchmarks/`. Requirements: a mix of settled and contested
topics, some where recent developments matter (to expose stale parametric
knowledge), and some narrow enough that fabricated citations are easy to detect.
