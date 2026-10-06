# agents/ — Orchestration layer

The agentic loop. Each agent is a single-responsibility step; the orchestrator
sequences them and enforces the budget limits from `core/config.py`.

Planned modules:
- `orchestrator.py` — runs a research job end to end, emits status transitions
- `planner.py` — topic → sub-questions → search queries
- `retriever_agent.py` — decides *which* retrieval backend to call per sub-question
- `synthesizer.py` — evidence bundle → report section, with inline citation markers
- `critic.py` — self-review pass: flags unsupported statements before verification
- `state.py` — the run state object passed between agents (no DB coupling)
