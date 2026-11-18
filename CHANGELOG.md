# Changelog

Notable changes are recorded here in Keep a Changelog format. Dates below come from repository history. Historical entries summarize selected commits, not a reconstructed version-by-version release ledger.

## [Unreleased]

### Added

- Merged PR #8 (`0ab2f9a`, 2026-11-17): Native Rust v5 workspace (`crates/`, 14 crates, `ib-cli` `identitybench v5` executable) implementing v5 Draft 5 generation, oracle evaluation, runtime, providers, accounting, scoring, reporting, validation, and conformance (`prepare`/`freeze`/`run`/`bind`/`verify`), without Python runtime bindings or subprocess fallback.
- Synthesized the audited preliminary v5 Draft 5 (`v5_core`) preview results bundle under [reports/v5/preliminary-core/](reports/v5/preliminary-core/report.md) from the offline native Rust audit ([AUDIT.md](reports/v5/rust-final-audit/historical/AUDIT.md), `24` configurations / `3,456` slots, `24/24` byte-identical summaries and Markdown reports, `18` `protocol_complete` vs. `6` `provider_incomplete`).
- A canonical [dataset card](datasets/README.md) covering both published `v4.1` and Draft 5 `v5` splits, contamination notice, software citation metadata, and architecture, Rust, conformance, and release instructions.
- Offline `identitybench v41 verify --run <dir>` for tool-trace replay and score recomputation before publication.

### Changed

- Established dual-path version boundary: Python `identitybench v41` remains the formal published, frozen benchmark; native Rust `identitybench v5` is the active v5 execution path; Python `src/identitybench/v5/` is preserved as a hash-bound compatibility and differential reference (`scripts/check_reference_pyright.py`), with public `uv run identitybench v5 run` refusing live execution and directing runs to the Rust CLI.
- `v41 run` rejects datasets that do not match the pinned release digests. `--allow-unpinned-dataset` explicitly permits a non-release dataset and is recorded in the manifest.
- `v41 validate` requires a sealed directory by default; pass `--in-memory` for a generator check without a seal.
- `v41 verify` must exit 0 with no mismatches before publication. `--allow-unverifiable` is permitted only when every unverifiable record and its reason are listed in the report `METHOD.md`; all remaining records must replay and match recomputed scores.
- Resume revalidates cached records and archives rejected records to `*.rejected.jsonl` before rerunning missing or invalid cells. Native Rust `v5` resume adds a durable `started_slots.jsonl` journal and fails closed on ambiguous or infrastructure-incomplete history rather than replacing slots.

### Security

- Completed Daybreak Blue security and hardening follow-up on the native Rust v5 workspace (`83ee2b5`, [DAYBREAK.md](reports/v5/rust-final-audit/DAYBREAK.md)): refused HTTP redirects and added incremental `20 MiB` response body caps in HTTP adapters; capped auxiliary CLI output reads at `16 MiB` and CLI version probe streams at `64 KiB` with Unix fd identity checks; bounded timed-out stdout/stderr capture with process-group cleanup; enforced `512 MiB` native dataset read cap with regular-file/symlink/atomic-copy checks; enforced strict single-request accounting and explicit unanswered-request/checkpoint proof on retry/continuation; and replaced the unmaintained YAML backend with `serde_norway 0.9.42` (`RUSTSEC-2024-0449`).
- Fixed the v3 connector sandbox's match-pattern guard bypass before retirement (`f6cd9cf`, 2026-11-11).
- Hardened Python provider response validation and parsing boundaries, secret and path redaction, and report output escaping (`f8df32e`, `2ea5d50`, 2026-11-11; plus Daybreak Blue v5 Python reference hardening in `feat/v5-harness`).
- Pinned released dataset episode and oracle digests independently of `SEAL.json`, hardened run artifacts and resume validation (`02c0fec`, 2026-11-11).
- Added run-artifact trace verification and explicit opt-in for vendor endpoint overrides (`5ce4932`, 2026-11-11). Replay checks consistency with recorded actions, not provider-output authenticity.

### Removed

- Retired benchmark generations v1, v2, v3, and v4 from the executable tree. The exact reproduction tag is `legacy-v1-v4`; specifications, notes, and published outputs are retained under [archive/](archive/README.md).
- Removed legacy v1–v4 commands, datasets, experiment configurations, report-generation scripts, and connector sandbox code. Python `v4.1` remains the formal published benchmark alongside active native Rust `v5`.

## 2026-10-26

### Added

- Published v4.1 Full results for 180 scenarios and 1,080 category tasks (`0688b23`).

## 2026-10-19

### Added

- Published v4.1 Lite results, charts, provenance, and documentation (`0cad3d2`).

## 2026-10-18

### Added

- Implemented v4 multi-agent orchestration, skills, steering, and sealed suites (`b638748`).

## 2026-10-17

### Added

- Implemented the v3 environment, oracle, verifier, scoring, multi-turn runner, and sealed suites (`5ffd245`, `6574efc`).

## 2026-10-15

### Added

- Added v2 multi-source reconciliation, compound lifecycle planning, and interactive clarification (`779bbb3`, `2c62d07`, `c3ad3e6`).

## 2026-10-11

### Added

- Froze the v1 dataset and published the initial interim leaderboard (`37f5e6f`, `83401a3`).
