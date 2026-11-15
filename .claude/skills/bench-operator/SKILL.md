---
name: bench-operator
description: >-
  Use when the user explicitly requests an IdentityBench live evaluation,
  resuming an interrupted model run, scoring run artifacts, or regenerating
  reports and charts. Never use for editing harness source code or datasets.
---

# IdentityBench Operator Skill (`bench-operator`)

You execute authorized benchmark runs, verify run integrity, score completed runs, and generate reports. You never edit harness source (`src/`), tests (`tests/`), or frozen datasets (`datasets/`).

## Hard Rules

1. **Explicit authorization for live model calls:** Run a live evaluation only when the user explicitly requests a pilot, benchmark, or report regeneration. Deterministic local baselines and validations make no model calls.
2. **Version-scoped behavior:** Use [`SPEC-v5.0-draft.5.md`](../../../SPEC-v5.0-draft.5.md) (`08f8d8da64dffb3b1b5a5bb549348b9d48f2eccb192abe64fb3062a3c62ff104`, superseding §0.4 sections) together with fixed [`SPEC-v5.md`](../../../SPEC-v5.md) (`6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43`) for unsuperseded IGA semantics, approved [`SPEC-v5-ADDENDUM-01.md`](../../../SPEC-v5-ADDENDUM-01.md) (`bf0cb575a9ea20e2995164b3e34b696f1828b4e0870f198731cc7c9624c0c9e9`), and [`SPEC-v5-DIRECTIVE-01.md`](../../../SPEC-v5-DIRECTIVE-01.md) (`d3b01715cf277730de027ef66ec2ea66f304b5ff388ebc9cb138aedd0cc98405`), with `identitybench v5` for `v5_dev`, `v5_lite`, `v5_core`, and `v5_full`. Use the matching older specification and CLI for frozen suites, including [`SPEC-v4.1.md`](../../../SPEC-v4.1.md) and `identitybench v41` for `v41_*`. Never apply v5 rules or agent tasks to frozen legacy suites.
3. **No source or dataset mutations:** If a run reveals a harness issue, stop and hand it to `orchestrate` → `implement`. Do not patch code or datasets as the operator.
4. **Clean provenance for publication runs:** Before an official held-out run, verify the worktree and matching dataset seals so its manifest records accurate provenance. Do not inspect or print `.env` files, credentials, or private endpoints.
5. **One run directory per config:** Give every experiment config a distinct `--out` directory because each run rewrites its manifest, summary, and report.
6. **Codex model routing:** When a benchmark configuration evaluates a Codex model, route it through the global CPA provider. Do not bypass CPA with a model-specific CLI or direct provider unless the user explicitly requests that in the current turn.

## Standard Operator Workflow

### 1. Pre-flight

Validate the exact requested suite without making live model calls, then inspect repository status:

```bash
uv run identitybench v5 validate --suite <v5_dev|v5_lite|v5_core|v5_full> --double-build
git status -sb
```

For a frozen v4.1 suite, use its matching validator:

```bash
uv run identitybench v41 validate --suite <v41_dev|v41_lite|v41_full> --double-build
```

### 2. Run or Resume

Only after the user has explicitly requested a live evaluation:

```bash
uv run identitybench v5 run --config experiments/<config>.yaml --out runs/<config-name>
uv run identitybench v5 run --config experiments/<config>.yaml --out runs/<config-name> --resume
```

Use the matching `v41` command for a requested legacy evaluation. Resume only the interrupted config's own output directory.

### 3. Audit Run Reliability

Before scoring or publishing a model row:

- Verify `execution_valid_rate == 1.00` where required by the matching specification.
- Report provider errors separately from model parse failures and `MAX_TURNS_EXCEEDED` terminations.
- Report the `termination_reason` mix per model. Parse failures after a valid provider response are not provider transport failures.
- If provider failures make the run incomplete, resolve connectivity/quota issues and resume only after authorization remains applicable.

### 4. Score and Report

```bash
uv run identitybench v5 score --suite <suite> --out runs/<suite>-baselines
uv run identitybench v5 report --run runs/<config-name> --out reports/<version>/
```

Use matching `v41` commands for legacy results. Generate version-specific charts and statistics from the raw run artifacts required by the matching specification. Do not present a source-health or baseline check as a live evaluation result.
