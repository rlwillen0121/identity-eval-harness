---
name: adversarial-review
description: >-
  Use when running multi-lane blind reviews (lane-rules, lane-security,
  lane-oracle, lane-blind-conformance, lane-stats) or cross-model adversarial
  reviews (e.g., Gemini Argon <-> Codex Astra xhigh / Claude) at milestone gates.
---

# Multi-Lane & Cross-Model Adversarial Review Skill (`review`)

Use this skill at milestone boundaries (`M3.1` through `M3.6`) and before any dataset freeze or publication claim.

## 1. Internal Multi-Lane Review Composition

The `review` lane fans out independent read-only subagents across the hidden lanes defined in `AGENTS.md`:

- **`lane-rules` (Always-On):** Checks `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `SPEC-v3.md`, and modified files for domain boundary violations (no SOC/SIEM drift, no `nhi_ai_agent` governance) or rule contradictions.
- **`lane-security` (Always-On):** Audits subprocess sandbox isolation (network disabled, temp dir isolation, resource caps in `connectors`), secret scrubbing, dataset `SEAL.json` SHA-256 integrity, and holdout split (`r2`, `r3`) hygiene.
- **`lane-oracle` (Always-On):** Runs `uv run pytest`, `uv run identitybench validate --double-build`, verifies `oracle_replay == 1.00` and negative-mutation pass rate `== 0.00`, and checks state/trace verifier determinism.
- **`lane-blind-conformance` (When policy/generator/verifier changes):** Verifies `tests/conformance/` passes 100% and `AMBIGUITIES.md` has zero unresolved items.
- **`lane-stats` (When scoring/bootstrap/reporting changes):** Audits paired cluster bootstrap (`B = 10,000`), TOST equivalence (`Delta = 0.05`), Benjamini-Hochberg FDR (`q = 0.05`), and `execution_valid_rate == 1.00` enforcement.

## 2. Cross-Model Adversarial Review (`Argon` <-> `Codex Astra xhigh`)

When requested by the user or required by a milestone gate (`M3.1`–`M3.6`), run an external adversarial review round using Codex CLI (`gpt-6-astra` with `model_reasoning_effort="xhigh"`):

```bash
codex exec -m gpt-6-astra -c 'model_reasoning_effort="xhigh"' -s read-only -C /Users/ryanwillen/orca/projects/identity-eval-harness "<adversarial review prompt>"
```

### Adversarial Review Protocol
1. Ask the reviewer to hunt specifically for:
   - Shortcut leaks (can a shallow heuristic pass without true IGA reasoning?)
   - Oracle/verifier bugs or unvalidated side effects
   - Edge-case ambiguities in policy text vs. verifier logic
   - Statistical or experimental hygiene flaws
2. Classify every finding by severity (`P0` blocker, `P1` high, `P2` medium, `P3` minor).
3. Address all `P0`/`P1`/`P2` findings and re-verify in a follow-up round until the reviewer returns an explicit sign-off verdict:
   - `Proven for source-health`
   - `Proven for milestone-gate`
   - `Proven for publication`
