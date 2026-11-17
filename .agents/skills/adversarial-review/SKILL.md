---
name: adversarial-review
description: >-
  Use for read-only multi-lane reviews at IdentityBench dataset freeze,
  publication, and explicitly requested milestone gates.
---

# Multi-Lane Adversarial Review (`review`)

Use this skill at dataset freeze and publication gates, at a requested milestone gate, or when the user asks for an adversarial review. Keep routine local iteration focused. Dispatch all lanes through native collaboration subagents; Codex inference uses the configured global CPA provider. Do not use model-specific CLIs, direct provider routes, or separate sidebar threads.

## Version scope

- **Native v5:** Review Rust under `crates/` against [`SPEC-v5.0-draft.5.md`](../../../SPEC-v5.0-draft.5.md), plus unsuperseded requirements in [`SPEC-v5.md`](../../../SPEC-v5.md), [`SPEC-v5-ADDENDUM-01.md`](../../../SPEC-v5-ADDENDUM-01.md), and [`SPEC-v5-DIRECTIVE-01.md`](../../../SPEC-v5-DIRECTIVE-01.md). The Python v5 implementation is a compatibility and differential reference.
- **Published frozen v4.1:** Review the Python harness and artifacts against [`SPEC-v4.1.md`](../../../SPEC-v4.1.md). Keep its six-category boundary and do not import v5 `agent_governance` semantics.
- **Archived v1–v4:** Historical artifacts only. Do not revive them as live targets.

## Lanes

- **`lane-rules` (always):** Check agent instructions, normative specs, and changed files for scope and version contradictions. Structured `agent_governance` is allowed only in v5. Generic SOC/SIEM/EDR, firewall/network response, unstructured prompt-injection or jailbreak testing, and moderation are prohibited in every version.
- **`lane-security` (always):** Check secret handling, provider/process boundaries, sealed data and seed integrity, provenance, and development-only tuning. Separate observed controls from unverified claims.
- **`lane-oracle` (always):** Run read-only deterministic checks for the changed version and exact candidate. For v5, check the applicable Draft 5 validation and replay/seal requirements; for v4.1, use its matching validation and replay gates.
- **`lane-blind-conformance` (for policy, schema, generator, verifier, or oracle changes):** Use a fresh isolated worker for expectations. It may read only normative specifications and rendered policy/episode views while deriving results. It must not inspect Rust or Python implementations, gold traces, baselines, or oracle datasets during derivation. For v5, keep full-spec and agent-view-only references separate and attribute addendum/directive rules correctly.
- **`lane-stats` (for scoring, calibration, bootstrap, or report changes):** Audit the matching version's statistical and execution-validity gates from raw artifacts. Keep provider errors, parse failures, and turn-limit terminations separate.

Keep hidden lane instructions private unless an audit transcript is requested. The orchestrator records each lane's exact inputs, candidate head, proof, and verdict. Report actionable findings with severity and file/line evidence. Do not claim milestone sign-off with unresolved P0/P1 findings or actionable P2 findings.
