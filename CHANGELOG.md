# Changelog

Notable changes are recorded here in Keep a Changelog format. Dates below come from repository history. Historical entries summarize selected commits, not a reconstructed version-by-version release ledger.

## [Unreleased]

### Removed

- Retired benchmark generations v1, v2, v3, and v4 from the executable tree. The exact reproduction tag is `legacy-v1-v4`; specifications, notes, and published outputs are retained under [archive/](archive/README.md).
- Removed legacy commands, datasets, experiment configurations, report-generation scripts, and connector sandbox code. Only v4.1 remains live.

### Added

- A canonical [dataset card](datasets/README.md), contamination notice, software citation metadata, and v4.1-only architecture and release instructions.
- Offline `identitybench v41 verify --run <dir>` for tool-trace replay and score recomputation before publication.

### Security

- Fixed the v3 connector sandbox's match-pattern guard bypass before retirement (`f6cd9cf`, 2026-11-11).
- Hardened provider response validation and parsing boundaries, secret and path redaction, and report output escaping (`f8df32e`, `2ea5d50`, 2026-11-11).
- Pinned released dataset episode and oracle digests independently of `SEAL.json`, hardened run artifacts and resume validation (`02c0fec`, 2026-11-11).
- Added run-artifact trace verification and explicit opt-in for vendor endpoint overrides (`5ce4932`, 2026-11-11). Replay checks consistency with recorded actions, not provider-output authenticity.

### Changed

- `v41 run` rejects datasets that do not match the pinned release digests. `--allow-unpinned-dataset` explicitly permits a non-release dataset and is recorded in the manifest.
- `v41 validate` requires a sealed directory by default; pass `--in-memory` for a generator check without a seal.
- `v41 verify` must pass before publication; older artifacts without enough replay context can remain unverifiable.
- Resume revalidates cached records and archives rejected records to `*.rejected.jsonl` before rerunning missing or invalid cells. Rejected sidecars are not accepted trial records.

## 2026-10-27

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
