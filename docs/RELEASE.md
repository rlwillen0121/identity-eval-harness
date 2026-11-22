# IdentityBench release and proof procedure

This release checklist covers the active native Rust **IdentityBench v5.1** benchmark (`crates/`, `identitybench v51`), specified by the frozen [SPEC-v5.1.md](../SPEC-v5.1.md). See the [Rust guide](RUST.md) for commands and gate semantics. Follow [AGENTS.md](../AGENTS.md) for contributor/operator separation and required review lanes. Any live model call requires explicit operator authorization in the current turn; checks below are offline unless identified as a live evaluation.

## 1. Prepare the release

Record the intended source revision, version, and dataset identity. Use a clean working tree for official runs. Update [CHANGELOG.md](../CHANGELOG.md) and [CITATION.cff](../CITATION.cff) for the release. Keep credentials, private endpoints, local paths, and unreviewed raw outputs out of committed artifacts.

Retired `v1–v4` (`legacy-v1-v4`), `v4.1` + legacy Python `v5` (`legacy-v4.1`), and native Rust `v5` (`legacy-v5`) artifacts live under [`archive/`](../archive/README.md). Do not recreate retired runners or compatibility shims; follow the archive reproduction instructions when historical execution is needed.

## 2. Generate, seal, and verify datasets

`datasets/v51/v51_dev`, `v51_lite`, `v51_core`, and `v51_full` are sealed. Do not regenerate over them, hand-edit JSONL, or mutate their seeds. Tune prompts, schemas, engine behavior, and verifier tolerances on `v51_dev` only before a new dataset freeze.

Validate a sealed suite with:

```bash
cargo build --release --locked -p ib-cli
./target/release/identitybench v51 validate --suite v51_dev --double-build
```

The command exits `0` only if every gate passes (`G-SEAL-V51`, `G-GOLD-REPLAY-V51`, `G-ANTI-ORACLE-V51`, and with `--double-build` `G-DET-V51`). For a freeze that requires blind determinability, add `--blind-configs <frozen-blind-configs>.json` (`G-BLIND-V51`) using a reference derived under the [blind-conformance skill](../.agents/skills/blind-conformance/SKILL.md).

For an intentionally new candidate dataset release, build into a separate directory with a documented seed (`--freeze` for held-out suites) and validate it:

```bash
./target/release/identitybench v51 build --suite v51_dev --seed <release-seed> --out <candidate-directory>
./target/release/identitybench v51 validate --suite v51_dev --dataset <candidate-directory> --double-build
```

## 3. Establish source health and review

Run the native Rust workspace and repository hygiene checks:

```bash
test -z "$(git ls-files '*.py')"
cargo test --workspace --locked
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked
cargo +1.90.0 check --workspace --locked
node --test "site/tests/*.test.mjs"
```

Require successful seal, gold-replay, negative-mutation, anti-oracle, and double-build validation. Compose the required `lane-rules`, `lane-security`, and `lane-oracle` reviews; add blind conformance review for schema/generator/engine/runtime changes and statistics review for scoring/reporting changes. Milestone sign-off requires applicable review verdicts and explicit pass/fail evidence for the specification's gates (`SPEC-v5.1.md` §6.2); see [AGENTS.md](../AGENTS.md) for the current work and proof boundaries.

These source-health commands do not establish blind conformance (`G-BLIND-V51`), live calibration (`G-CALIBRATION-V51`), or benchmark milestone acceptance.

## 4. Run or resume an explicitly authorized v5.1 evaluation

Only `bench-operator` may run live models, and only after explicit current-turn user authorization. Use one output directory per experiment configuration and supply both a model YAML config and a pricing JSON (examples under `examples/v51/`):

```bash
./target/release/identitybench v51 run \
  --config <model-config>.yaml \
  --pricing <pricing>.json \
  --suite <v51_dev|v51_lite|v51_core|v51_full> \
  --repeats <1|3> \
  --out <run-dir>
```

Use `--resume` only for an authorized, interrupted configuration in its existing output directory, with otherwise identical arguments. Resume keeps every recorded slot except provider failures and re-runs those plus missing slots. Preserve provider errors separately from parse failures and turn-limit terminations.

## 5. Report and import publication artifacts

```bash
./target/release/identitybench v51 report --run <run-dir>
node scripts/import-v51-run.mjs <run-dir> [<name>]
```

`report` recomputes `summary.json` and `report.md` from the run's `episodes.jsonl` without model calls. The importer copies `summary.json`, `report.md`, and `manifest.json` into `reports/v51/runs/<name>/` with a compact per-slot `episodes.json`; it does not copy configs, transcripts, or tool traces. Check that every published model row and chart agrees with the imported summary, that only complete runs (`is_complete: true`) are ranked, and that incomplete runs are labeled in progress.

## 6. Publish and tag

- Verify `SEAL.json` and pinned digests again (`identitybench v51 validate`); confirm sealed files are unchanged and the release worktree is clean.
- Require all publication proof gates, applicable review verdicts, and exact-revision checks. Inspect publishable artifacts for secrets and private paths; run relative link/image checks.
- Confirm the dataset card's contamination notice is present.
- Finalize the changelog entry with the actual release date and version. Keep citation metadata aligned with the software release.
- Tag the reviewed source revision with the release version and publish the approved artifacts under the maintainer's release authority. Preserve `legacy-v1-v4`, `legacy-v4.1`, and `legacy-v5`; do not move or reuse them.
