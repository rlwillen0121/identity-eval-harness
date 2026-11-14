---
name: adversarial-review
description: >-
  Use for multi-lane blind reviews (lane-rules, lane-security, lane-oracle,
  lane-blind-conformance, lane-stats) and requested adversarial reviews at
  IdentityBench milestone gates.
---

# Multi-Lane Adversarial Review Skill (`review`)

Use this skill at the `M5.0-Core` milestone and before any v5 dataset freeze or publication claim. For frozen v4.1 work, use its `M4.1-Hard` milestone rules; earlier milestone records remain version-scoped. Review v5 against [`SPEC-v5.md`](../../../SPEC-v5.md), the approved [`SPEC-v5-ADDENDUM-01.md`](../../../SPEC-v5-ADDENDUM-01.md), and the original user directive recorded in [`SPEC-v5-DIRECTIVE-01.md`](../../../SPEC-v5-DIRECTIVE-01.md). Attribute each rule to its source; do not treat directive-derived co-sign lifecycle as base-SPEC text. Review legacy work against its matching specification.

## 1. Native Review Composition

The `review` lane composes independent, read-only subagents for the hidden lanes in `AGENTS.md`. Dispatch all delegated reviews through the current native collaboration subagent tools. Do not invoke `codex exec`, model-specific review CLIs, or separate sidebar threads.

- **`lane-rules` (always on):** Check `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, the matching normative spec, and changed files for contradictions or scope drift. Preserve the six-category boundary for v1–v4.1, including the prohibition on `nhi_ai_agent`, `EnterpriseAgentV5`, and OBO agent-delegation tasks; allow structured `agent_governance` only in v5; prohibit SOC/SIEM/EDR, firewall/network incident response, unstructured prompt injection, jailbreak, and moderation work across every version.
- **`lane-security` (always on):** Review relevant sandbox isolation, secret handling, dataset seals, and development-only tuning hygiene. Follow the matching version's actual implementation and spec; report unverified controls as unverified.
- **`lane-oracle` (always on):** Run read-only tests and deterministic validation for the exact version and suites in scope. For v5, check `SPEC-v5.md` §4.3 requirements such as `oracle_team_replay`, the 12 mutation kinds, paired-world noninterference, and seal/double-build integrity, applying Addendum 01 and Directive 01 where their ETag, first-stage, and Gate 3 co-sign rules are relevant. Use the v4.1 gates only for v4.1 changes.
- **`lane-blind-conformance` (when policy, verifier, or generator behavior changes):** Assign a fresh isolated subagent that has not seen forbidden implementation or gold data. Verify the matching version's blind references and conformance suite. For v5, derive the full-spec reference from `SPEC-v5.md` §§1–4, approved Addendum 01, and the original directive recorded in Directive 01, with each rule attributed to its source; derive the agent-view-only reference from `PolicyCardV5` plus delivered slices only. Assess both under `G-SPEC-01` without oracle, generator, verifier, baseline, or oracle-dataset leakage.
- **`lane-stats` (when scoring, bootstrap, equivalence, or reporting changes):** Audit the version-specific scoring, calibration, baseline, uncertainty, and execution-validity requirements. For v5, use `SPEC-v5.md` §4.3 with applicable Addendum 01 or Directive 01 rules and raw artifacts; for v4.1, use its matching spec and report.

Keep hidden lanes hidden unless the user asks for an audit transcript. The orchestrator composes the lane verdicts and records the exact head reviewed.

## 2. Requested Adversarial Review

When the user requests an independent adversarial review, dispatch it through the native collaboration tools using the model, effort, and service tier required by `AGENTS.md` and current task instructions. Record the selected route and task basis in the handoff or review record. Do not substitute a model CLI or direct provider route.

Ask reviewers to hunt for:

- Shortcut leaks that let a shallow heuristic pass without IGA reasoning.
- Oracle/verifier mismatches, unvalidated side effects, or policy/state ambiguity.
- Domain boundary leaks, including unstructured agent-security tasks in v5.
- Statistical or evaluation-hygiene flaws.

Classify findings by severity (`P0` blocker, `P1` high, `P2` medium, `P3` minor). A milestone verdict requires zero P0/P1 findings and every actionable P2 addressed by a repair with focused proof or a concrete evidence-based explanation for no change. For v5, keep ambiguities in `tests/conformance/v5/AMBIGUITIES.md` or the review record; do not edit the fixed-hash `SPEC-v5.md`. Do not claim sign-off while any required finding remains unresolved.
