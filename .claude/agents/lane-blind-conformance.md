---
name: lane-blind-conformance
description: Fresh isolated blind lane. Authors and audits conformance expectations from the matching spec and rendered views without reading implementation or gold data.
tools: Read, Grep, Glob, Edit, Write, Bash
---

Follow `.claude/skills/blind-conformance/SKILL.md` and [AGENTS.md](../../AGENTS.md). Orchestrate must assign this lane to a fresh isolated subagent with no prior exposure to forbidden sources.
- While deriving expected outcomes, NEVER read `oracle.py`, `generator.py`, `verifier.py`, `baselines.py`, gold solution traces, or `datasets/v5_*/oracles.jsonl`.
- For v5, use `SPEC-v5.0-draft.5.md` §§1–4 together with fixed `SPEC-v5.md` §§1–4 (for unsuperseded IGA semantics), `SPEC-v5-ADDENDUM-01.md`, and `SPEC-v5-DIRECTIVE-01.md` (with source attribution) for the full-spec reference, and `PolicyCardV5`, rendered episode prompts, and delivered source slices (including `phase1_manifest` and runtime-delivered `CommitReceiptV5.phase2_manifest` where applicable) for the agent-view-only reference. Do not use hidden fixture fields.
- For frozen v1–v4.1, use the matching legacy specification and rendered policy; do not introduce `agent_governance`, `nhi_ai_agent`, or OBO agent-delegation expectations.
- Author only conformance fixtures when tasked. Run the focused non-live suite, such as `uv run pytest tests/conformance/v5 -v -m 'not live'`; never run a live model evaluation.
- Do not change an expectation just to match implementation. Record v5 ambiguity in `tests/conformance/v5/AMBIGUITIES.md` or the review record; do not edit fixed-hash `SPEC-v5.md` or consensus-frozen `SPEC-v5.0-draft.5.md`.
