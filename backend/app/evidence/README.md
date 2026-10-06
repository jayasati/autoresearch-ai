# evidence/ — Claims, verification, citations, conflicts

This is the core research contribution of the project. Everything here turns a
generated report into a set of auditable assertions.

Planned modules:
- `claim_extractor.py` — report → atomic, checkable claims (req. 4)
- `evidence_linker.py` — claim → candidate supporting passages via RAG
- `verifier.py` — per-claim NLI-style judgement → `VerificationVerdict` (req. 5)
- `citation_validator.py` — does the cited source exist, resolve, and actually
  support the claim? Detects fabricated and misattributed citations (req. 6)
- `conflict_detector.py` — pairwise disagreement between sources (req. 7)
- `traceability.py` — claim → evidence → chunk → source → URL chain (req. 8)
