---
name: bench-operator
description: >-
  Use when the user explicitly requests a live IdentityBench evaluation,
  resumption of an interrupted run, scoring run artifacts, or report generation.
  Never use for editing harness source or sealed datasets.
---

# IdentityBench Operator (`bench-operator`)

You operate authorized runs and their artifacts. Do not edit source code, tests, or frozen datasets. Use the native Rust v5 CLI for v5 evaluation. Use the Python `identitybench v41` CLI only for the published, frozen v4.1 benchmark. Never run a live evaluation through the preserved Python v5 reference.

## Hard rules

1. A live evaluation or provider call requires explicit authorization from the user in the current turn. A prior run or stored config is not current authorization.
2. Use a unique `--out` directory for each experiment configuration. Resume only that configuration's own interrupted run.
3. Do not mutate sealed episodes, oracles, seeds, seals, or source-pinned digests. If an input or validation check fails, stop and hand the issue to the implementation owner.
4. Never print or log credentials, `.env` contents, bearer tokens, or private endpoint URLs.
5. Route Codex inference through the globally configured CPA provider. Delegation uses native collaboration subagents only.

## Preflight

For native Rust v5, build and validate the exact suite without model calls:

```sh
cargo build --release --locked -p ib-cli
target/release/identitybench v5 validate --suite <v5_dev|v5_lite|v5_core|v5_full> \
  --dataset datasets/v5_draft5/<suite> --double-build
git status -sb
```

For the frozen public v4.1 benchmark, use its matching Python validation command:

```sh
uv run identitybench v41 validate --suite <v41_dev|v41_lite|v41_full> --double-build
```

## Run, score, and report

Only after current-turn authorization, run the requested Rust v5 configuration:

```sh
target/release/identitybench v5 run --config experiments/<config>.yaml --out runs/<config-name>
```

Use `--resume` only for an authorized, interrupted configuration in its existing output directory. For an explicitly requested v4.1 run, use its matching `uv run identitybench v41 run` command and a separate output directory. Never substitute another version or the Python v5 reference.

Deterministic scoring and report generation do not call a model. Use the matching version's CLI and retain version-specific raw artifacts:

```sh
target/release/identitybench v5 score --suite <suite> --dataset datasets/v5_draft5/<suite> \
  --baseline oracle_team_replay --out runs/<suite>-baseline
target/release/identitybench v5 report --run runs/<run-name> --out reports/<version>/
```

## Run integrity

Before reporting results, inspect record-level outcomes and the report's completeness and planned/observed slots. Report provider errors separately from parse failures and turn-limit terminations. An offline baseline, validation, or source-health result is not live-evaluation, publication, or milestone evidence. Preserve existing raw artifacts and manifests when a run is rejected or incomplete.
