---
name: adversarial-review
description: >-
  Use when running multi-lane blind reviews (lane-rules, lane-security,
  lane-oracle, lane-blind-conformance, lane-stats) or cross-model adversarial
  reviews (Codex gpt-6-astra xhigh, read-only) at milestone gates.
---

# Multi-Lane & Cross-Model Adversarial Review Skill (`review`)

Use this skill at milestone boundaries (`M4.1-Hard`; legacy `M3.1` through `M3.6`) and before any dataset freeze or publication claim.

## 1. Internal Multi-Lane Review Composition

The `review` lane fans out independent read-only subagents across the hidden lanes defined in `AGENTS.md`:

- **`lane-rules` (Always-On):** Checks `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `SPEC-v4.1.md`, and modified files for domain boundary violations (no SOC/SIEM drift, no `nhi_ai_agent` governance) or rule contradictions.
- **`lane-security` (Always-On):** Audits subprocess sandbox isolation (network disabled, temp dir isolation, resource caps in `connectors`), secret scrubbing, dataset `SEAL.json` integrity, and dev-only tuning hygiene.
- **`lane-oracle` (Always-On):** Runs `uv run pytest`, `uv run identitybench v41 validate --suite <suite> --double-build`, verifies `oracle_team_replay == 1.00` and negative-mutation pass rate `== 0.00`, and checks state/trace verifier determinism.
- **`lane-blind-conformance` (When policy/generator/verifier changes):** Verifies `tests/conformance/` passes 100% (`G-SPEC-01`) and the version's `AMBIGUITIES.md` has zero unresolved items.
- **`lane-stats` (When scoring/bootstrap/reporting changes):** Audits v4.1 `G-SRR` scoring and cluster bootstrap CIs (legacy v3: `B = 10,000`, TOST, BH-FDR) and `execution_valid_rate == 1.00` enforcement.

## 2. Cross-Model Adversarial Review (`Codex Astra xhigh`)

When requested by the user or required by a milestone gate (`M4.1-Hard`), run an external adversarial review round using Codex CLI (`gpt-6-astra` with `model_reasoning_effort="xhigh"`):

```bash
codex exec -m gpt-6-astra -c 'model_reasoning_effort="xhigh"' -s read-only -C . --json -o <out.md> - < <prompt.md>
# later rounds: codex exec resume <session id from the thread.started event>
```

Always pass `-s read-only`. Cap the loop at about 4 rounds and record any disagreement still open in the spec's open questions. Gemini Argon reviews are not part of the default loop; run them only when the user asks.

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
