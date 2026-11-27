---
name: bench-operator
description: >-
  Use when the user explicitly requests a live IdentityBench evaluation,
  resumption of an interrupted run, report regeneration from run artifacts, or
  import of a run into the published results. Never use for editing harness
  source or sealed datasets.
---

# IdentityBench Operator (`bench-operator`)

You operate authorized IdentityBench v5.3 runs and their artifacts using the native Rust `identitybench v53` CLI. Do not edit source code, tests, or sealed datasets. Never create live runs for retired `v1–v4`, `v4.1`, legacy Python `v5`, native Rust `v5`, `v5.1`, or `v5.2` generations.

## Hard rules

1. A live evaluation or provider call requires explicit authorization from the user in the current turn. A prior run or stored config is not current authorization.
2. Use a unique `--out` directory for each experiment configuration. Resume only that configuration's own interrupted run, with the same `--config`, `--pricing`, `--suite`, and `--repeats`; the runner does not re-verify that these match.
3. Do not mutate sealed episodes, oracles, seeds, seals, or source-pinned digests. If an input or validation check fails, stop and hand the issue to the implementation owner.
4. Never print or log credentials, `.env` contents, bearer tokens, or private endpoint URLs. Keep raw run directories, model configs, and transcripts outside the repository unless they are explicitly approved for publication.
5. Route Codex inference through the globally configured CPA provider. Delegation uses native collaboration subagents only.

## Preflight

Build the native Rust CLI and validate the exact sealed suite without model calls:

```sh
cargo build --release --locked -p ib-cli
target/release/identitybench v53 validate --suite <v53_dev|v53_lite|v53_blind_fixture> --double-build
git status -sb
```

`validate` defaults to `datasets/v53/<suite>` for public suites; pass `--dataset <dir>` only for an explicitly named candidate directory. Private suites (`v53_core`, `v53_full`) are never in the repository: validate them from the operator's offline directory, with the private key read at run time and never printed, logged, or committed:

```sh
target/release/identitybench v53 validate --suite <v53_core|v53_full> \
  --dataset <operator-dir> --double-build --private-key-file <key-file>
```

`IDENTITYBENCH_V53_PRIVATE_KEY_HEX` may replace `--private-key-file`. Without the key, `G-DET-V52` cannot rebuild the suite and fails.

## Run and resume

Only after current-turn authorization, run the requested configuration with its model YAML and pricing JSON (examples under `examples/v52/`):

```sh
target/release/identitybench v53 run \
  --config <model-config>.yaml \
  [--pricing <pricing>.json] \
  --suite <v53_dev|v53_lite|v53_core|v53_full> \
  --repeats <1|3> \
  --out <run-dir>
```

`run` validates the dataset, config, and optional pricing before reading the provider environment file (`.env` at the repository root by default; `--env-file <path>` or `--no-env-file` override it). It writes `manifest.json`, `episodes.jsonl`, `summary.json`, and `report.md` into `<run-dir>`.

To continue an interrupted configuration, repeat the identical command with `--resume`:

```sh
target/release/identitybench v53 run --config <model-config>.yaml [--pricing <pricing>.json] \
  --suite <suite> --repeats <1|3> --out <run-dir> --resume
```

Resume keeps every recorded slot except `INCOMPLETE_PROVIDER_FAILURE` slots, and re-runs those plus any missing slots. Parse failures and step-limit terminations are observed outcomes and are preserved, never retried.

## Report

Report regeneration is deterministic and makes no model calls:

```sh
target/release/identitybench v53 report --run <run-dir> [--pricing <pricing>.json] [--out <report-dir>]
```

## Leaderboard

The public leaderboard uses completed runs on all 96 sealed `v53_full` scenarios. Build native evidence offline, then import it using the full-suite command below.

```sh
target/release/identitybench v53 leaderboard --runs <run-dir>... --out <board-dir> \
  [--seal <published SEAL.json>] [--pricing <frozen pricing>.json]
```

The native command retains the original core ranking procedure. It ranks only `v53_core` entries that pass every `SPEC-v5.3` §10.1 condition and lists every other entry with its ineligibility reasons. For private suites it never writes per-slot rows or per-cell scores. `--suite-override-for-test` is test-only: its output is marked `test_only` and is never published.

## Import into published results

Use `scripts/import-v53-run.mjs` to import publishable aggregate v5.3 leaderboard and run artifacts into `reports/v53/` (and rebuild `site/data/leaderboard.json` via `node site/build.mjs`):

```sh
node scripts/import-v53-run.mjs --confirmatory <full-leaderboard-dir> --published-on YYYY-MM-DD
node site/build.mjs && node site/build.mjs --check
```

`import-v53-run.mjs` validates `SPEC-v5.3` / `identitybench.v5.3.leaderboard.v1`, refuses `test_only` boards and private-suite per-slot rows, scans for local filesystem paths and private episode identifiers, and copies only publishable aggregate artifacts (`leaderboard.json` + `leaderboard.md`, or `summary.json` + `report.md` per run). Never copy raw `manifest.json`, `episodes.jsonl`, `probe_receipt.json`, checkpoints, configs, or transcripts. Keep retired `scripts/import-v51-run.mjs` untouched for archival `SPEC-v5.1` reference only.

Historical core imports use `--leaderboard <core-leaderboard-dir>`, and individual run summaries use `--run <run-dir> --name <slug>`. For the public leaderboard, the offline `--confirmatory` importer validates native full entries and publishes their aggregate ranks to `confirmatory.json` under the [full publication rules](../../../docs/v531-scoring-addendum.md#full-suite-publication).

## Run integrity

Before reporting results, inspect `summary.json`: `is_complete`, `planned_slots`, `observed_slots`, `completed_slots`, `step_limit_slots`, `parse_failure_slots`, and `provider_error_slots`. `g_srr` is `null` until a run is complete; report an incomplete run only as in progress with its `observed_g_srr` and `provisional_g_srr_bounds`, never as a ranked score. Report provider errors separately from parse failures and turn-limit terminations. An offline validation or source-health result is not live-evaluation, publication, or milestone evidence. Preserve existing raw artifacts and manifests when a run is rejected or incomplete.
