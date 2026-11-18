# Approved v5 clarification checkpoint

- **Date:** 2026-11-13
- **Base specification SHA-256:** `6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43` (unchanged)
- **Approved addendum:** `SPEC-v5-ADDENDUM-01.md`, SHA-256 `bf0cb575a9ea20e2995164b3e34b696f1828b4e0870f198731cc7c9624c0c9e9`

The addendum adopts the six non-account Phase-1 outcome ETag preimages and serialization rules, and snapshot-local Alternative A for `G-SRR@first_stage` and `repair_lift`. A halted connector preserves its base generation and ETag. `p1_first_stage_snapshot_ok` is diagnostic only. The runtime and scoring implementations already conformed and were unchanged; the rendered PolicyCard now discloses the approved rules.

## Focused proof

- `uv run --no-env-file pytest -q tests/v5/test_runtime.py tests/v5/test_scoring.py tests/v5/test_policy_card.py` — passed.
- Scoped Ruff check across the three source files and three test files — passed.
- Bounded independent rules, stats, security, and oracle review — passed with no actionable findings.

The 3,200-token PolicyCard and 9,600-token logical-input caps remain unchanged and are not waived. `G-TOK-01` and `G-SPEC-01` remain open. This checkpoint does not claim source-health completion, milestone-gate completion, or benchmark completion. No benchmark calls or push were performed.
