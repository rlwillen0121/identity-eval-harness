---
name: bench-operator
description: >-
  Use when the user explicitly requests running IdentityBench evaluations,
  resuming interrupted model runs, scoring run artifacts, or regenerating reports
  and charts. Never use for editing harness source code or datasets.
---

# IdentityBench Operator Skill (`bench-operator`)

You are the **bench operator** for IdentityBench. You execute benchmark runs, verify run integrity, score completed runs, and generate reports. You **never** edit harness source code (`src/`), tests (`tests/`), or frozen datasets (`datasets/`).

## Hard Rules

1. **Explicit User Authorization Required for Live Model Calls:**
   - Only run live model providers (`openai_compatible`, `anthropic`, `google_genai`, `claude_cli`, `codex_cli`, `gemini_cli`, `agy_cli`, `muse_cli`) when the user has explicitly asked to run a pilot or benchmark evaluation.
   - Zero-cost deterministic baselines (`always_deny`, `always_approve`, `random_valid`, `surface_heuristic`, `oracle_replay`) may be run locally at any time to verify harness wiring.
2. **No Source or Dataset Mutations:**
   - If a run fails due to a harness bug, stop and hand the issue back to `orchestrate` -> `implement`. Do not patch `src/` while operating a run.
3. **Clean Working Tree for Publication Runs:**
   - Before starting an official `v1`, `v2`, `v3_lite`, or `v3_full` leaderboard run, verify `git status --porcelain` is clean so `manifest.json` records `git_dirty: false`.
   - Verify dataset SHA-256 seals (`uv run identitybench validate --suite <suite>`).

## Standard Operator Workflow

### 1. Pre-Flight Check
```bash
# Verify dataset seal and deterministic oracle health (0 model tokens)
uv run identitybench validate --suite <v1|v2|v3_dev|v3_lite|v3_full>

# Check git working tree cleanliness
git status -sb
```

### 2. Running or Resuming an Evaluation
```bash
# Start a new run
uv run identitybench run --config experiments/<config>.yaml --out runs/<run_id>

# Resume an interrupted run (skips completed episode/trial cells; retries transient provider errors)
uv run identitybench run --config experiments/<config>.yaml --out runs/<run_id> --resume
```

### 3. Auditing Run Reliability (`SPEC-v3.md` §3.4)
Before scoring or publishing a model row:
- Verify `execution_valid_rate == 1.00` (zero unrecovered provider transport errors, auth failures, or timeouts).
- Keep `provider_error_count` separate from `parse_failure_rate` (model-emitted invalid JSON/tool calls after a valid HTTP 200 / CLI response).
- If `execution_valid_rate < 1.00`, re-run with `--resume` after fixing provider connectivity/quota.

### 4. Scoring and Report Generation
```bash
# Compute episode metrics, bootstrap CIs, TOST equivalence, and BH-FDR tables
uv run identitybench score --run runs/<run_id>

# Render Markdown tables and SVG/PNG charts
uv run identitybench report --run runs/<run_id> --out reports/<version>/
```
