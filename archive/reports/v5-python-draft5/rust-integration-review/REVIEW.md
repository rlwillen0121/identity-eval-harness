# Rust v5 integration review

Status: independent scoped reviews closed. Exact-commit hosted CI and final full-suite readbacks are recorded in the PR. No benchmark milestone sign-off.

Checkpoint: `6e0a22496990278e0fe6d24b85942504f751b359`. Integration target: `7c1406445456db202e1feaf414c5bacb9bff7d1e` (`main`). The integration was prepared in the original owner worktree. Frozen specifications, Draft 5 datasets, historical model runs, and public v4.1 results must remain unchanged.

## Dispatch and evidence boundaries

Native collaboration dispatch requested Sol medium for orchestration/independent technical review, Terra high for integration/rules inspection, and Luna xhigh for implementation/correction. Native dispatch accepted these model selections. Fast service was requested in the task contracts; the collaboration API does not independently expose the effective per-call service tier. The runtime OpenAI base URL was checked against the required global CPA connection without printing credentials. No direct provider route or live benchmark execution was used.

The independent blind specification review was freshly isolated and read only the four matching normative specifications and the blind-conformance skill. It derived boundaries across seven categories and twelve mutation kinds, Stage 1–5 ordering, OBO attenuation, permanent co-sign consumption, immutable first-stage evidence, and incomplete scoring. Implementation inspection then checked selected high-risk expectations. This bounded review is not a new G-SPEC-01 receipt or certification.

## Findings and closure

| Finding | Severity | Reproduction | Status |
| --- | --- | --- | --- |
| Vendor credentials follow custom endpoint overrides without explicit opt-in | P1 | Isolated constructor configuration, no real key/network | Resolved; independent re-review passed |
| Additional confirmatory tracks accept wrong suite/seal/repeat eligibility | P1 | Synthetic dev artifact incorrectly accepted as confirmatory | Resolved; independent re-review passed |
| Contradictory provider-error and COMPLETED status receives a score | P1 | Synthetic raw trajectory, no attached score/attempt | Resolved; independent re-review passed |
| Report output follows existing symlink/hardlink leaves | P2 | Offline report replaces external sentinel | Resolved; independent re-review passed |
| Typed runtime staging bypasses mutation schema validation | P2 | Invalid dev Optimization mutation stages and commits despite validate error | Resolved; independent re-review passed |
| Documentation claims retired v1–v4 harnesses remain live | P2 | Comparison with current main retirement contract | Resolved; independent re-review passed |
| Historical documentation recognizer permits appended proxy values | P2 | Synthetic appended key/endpoint assignments | Resolved with exact-line recognition; independent re-review passed |

Receipt diagnostics depend on source and allowed-input provenance. Rust and the current Python reference reproduce the same 12 ordered diagnostics for each Draft 5 suite, including newly stale shared-CLI and blind-skill bindings. Frozen receipts were not rewritten to force a pass. The diagnostic fixture was updated to the verified current Python result while retaining fail-closed receipt validation.

## Historical evaluation audit

The pre-integration audit in `../rust-migration-audit/` regraded all 24 manifested Draft 5 core configurations offline: 24/24 summaries and Markdown reports matched Python byte for byte. All original input hashes remained unchanged. Eighteen configurations are protocol-complete; six retain 580 provider-failed slots. Parse failures and within-protocol limits remain observed outcomes. The initial audit binds its frozen binary hash. The final integrated-binary audit is complete and separately binds all 144 compiled source digests; all match the final working-tree source. See `../rust-migration-audit/integrated/`.

## Integrated local proof

- All 362 Rust tests pass; final Rust formatting and clippy pass (lint warnings remain). After fixture portability edits, all four pilot parity tests pass with an explicit real historical-run root, and all 15 affected provider fixture tests pass. Independent negative checks verify an explicit missing pilot root fails instead of silently skipping.
- The pre-repair full non-live Python run passed 2,723 tests and failed only the historical-text hygiene check. That finding is closed; the combined hygiene and Pyright-gate regression suite now passes all 46 tests. The final full-suite rerun is recorded in the PR alongside exact-commit CI.
- Strict mypy passes all 75 retained Python source files. Ruff lint and formatter checks pass with explicitly enumerated formatter-only exemptions for frozen, hash-bound reference inputs.
- Raw Pyright exits 1 with 25 frozen-v5-reference diagnostics. The enforced baseline compares the exact current messages, locations, rule/severity, analyzer version/configuration and all 27 source hashes. Pre-integration comparison has 24 identical messages and one documented `str`/`StrictStr` alias display difference at the same rule/location; no normalization is applied by the gate. The gate passes and finds no diagnostics elsewhere. This is bounded debt enforcement, not zero-error raw Pyright.
- All 19 native Draft 5 dev validation entries were checked without collapsing duplicate gate IDs: 14 PASS, G-SPEC-01 FAIL on unchanged stale receipts, four live-evidence gates UNKNOWN. Python and Rust both reproduce the exact 12 ordered receipt diagnostics for all four sealed Draft 5 suites.
- Final release-binary audit: all 24 core summaries and Markdown reports remain byte-identical to the original Python reports. All original inputs remain unchanged. See `../rust-migration-audit/integrated/`; binary identity and all 144 compiled-source digests are recorded separately from historical execution provenance.

## Publication hygiene scope

The checked-in baseline permits only absolute-home-path metadata in exactly 46 reviewed immutable proof files at their recorded SHA-256 digests. The files are byte-identical to the checkpoint. A different hash or path receives no exemption, and matching hashes never suppress private-IP, hostname or proxy-value findings. Exact policy constants and environment-variable-name documentation have narrow contextual recognition with appended-value regressions. Mutable test paths use repository-local defaults or the explicit `IDENTITYBENCH_REAL_PILOT_ROOT` override.

Independent branch-history inspection reviewed 56 commits, 1,626 new objects and 1,026 eligible text blobs. Flagged token/network forms were synthetic fixtures, security policy constants or documented names; no actual credential or private-endpoint disclosure was demonstrated. Safe classification metadata is retained alongside this review. This is bounded evidence: the inherited tracked-text scanner excludes top-level `tests/`, binary files and files larger than 5 MiB and is not a general credential detector. A separate parent token/private-key shape scan of all currently tracked text found only the existing synthetic redaction fixtures.

No live evaluations, dataset resealing, public leaderboard changes, or production deployment occurred. Exact-commit hosted CI and PR publication status will be recorded in the PR; the review does not establish benchmark publication readiness.
