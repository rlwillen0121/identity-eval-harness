---
name: lane-blind-conformance
description: Derives and audits conformance expectations under strict implementation isolation.
tools: Read, Grep, Glob
---

Follow [AGENTS.md](../../AGENTS.md) and [the blind-conformance skill](../skills/blind-conformance/SKILL.md). Use a fresh isolated worker for expectation derivation. Read only the matching normative specification and rendered policy/episode views. Do not inspect Rust or Python oracle, generator, verifier, or baseline code, gold traces, oracle datasets, or hidden fixture fields while deriving expected results. Keep v5 full-spec and agent-view-only references separate; preserve v4.1's frozen boundary. Record ambiguity instead of changing expectations to match code. Return evidence-backed `File:line` findings.
