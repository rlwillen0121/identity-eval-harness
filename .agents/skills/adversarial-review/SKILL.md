---
name: adversarial-review
description: >-
  Use for read-only multi-lane reviews at IdentityBench dataset freeze,
  publication, and explicitly requested milestone gates.
---

# Multi-Lane Adversarial Review (`review`)

Use this skill at dataset freeze and publication gates, at a requested milestone gate, or when the user asks for an adversarial review. Keep routine local iteration focused. Dispatch all lanes through native collaboration subagents; Codex inference uses the configured global CPA provider. Do not use model-specific CLIs, direct provider routes, or separate sidebar threads.

## Version scope

- **Native v5.3 (sole live harness):** Review Rust under `crates/` against [`SPEC-v5.3.md`](../../../SPEC-v5.3.md) and the sealed suites under `datasets/v53/`.
- **Retired generations (`v1–v4`, `v4.1`, legacy Python `v5`, native Rust `v5`, `v5.1`, `v5.2`):** Archival artifacts only under [`archive/`](../../../archive/README.md) (`legacy-v1-v4`, `legacy-v4.1`, `legacy-v5`, `legacy-v5.1`, and archived `archive/specs/v5.2/`). Do not revive them as live targets or import v5.2 `nhi_agent_governance` semantics into pre-v5.1 / v5.2 archived artifacts.

## Lanes

- **`lane-rules` (always):** Check agent instructions, normative specs, and changed files for scope and version contradictions. Structured NHI and AI-agent governance (`nhi_agent_governance`) is allowed only in v5.3 (inherited from the archived v5.1 and v5.2). Generic SOC/SIEM/EDR, firewall/network response, unstructured prompt-injection or jailbreak testing, and moderation are prohibited in every version.
- **`lane-security` (always):** Check secret handling, provider/process boundaries, sealed data and seed integrity, provenance, and development-only tuning. Separate observed controls from unverified claims.
- **`lane-oracle` (always):** Run read-only deterministic checks for the exact candidate: v5.3 seal (`G-SEAL-V52`), gold-replay and negative-mutation (`G-GOLD-REPLAY-V52`), anti-oracle (`G-ANTI-ORACLE-V52`), and `--double-build` determinism (`G-DET-V52`) gates via `identitybench v53 validate`.
- **`lane-blind-conformance` (for schema, generator, engine, runtime, or scoring changes):** Use a fresh isolated worker for expectations. It may read only `SPEC-v5.3.md`, the model-delivered platform reference, and rendered episode workspaces while deriving results. It must not inspect Rust implementations, gold configurations or traces, or oracle datasets during derivation. Keep full-spec and agent-view-only references separate, and compare through `G-BLIND-V52` only after the reference is frozen.
- **`lane-stats` (for scoring, calibration, bootstrap, or report changes):** Audit the statistical and execution-validity gates from raw artifacts. Keep provider errors, parse failures, and turn-limit terminations separate.

Keep hidden lane instructions private unless an audit transcript is requested. The orchestrator records each lane's exact inputs, candidate head, proof, and verdict. Report actionable findings with severity and file/line evidence. Do not claim milestone sign-off with unresolved P0/P1 findings or actionable P2 findings.
