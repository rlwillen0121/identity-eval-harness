---
name: orchestrate
description: Coordinates scoped IdentityBench work, retains implementation oversight, and composes review.
tools: Read, Grep, Glob, Bash, Agent
---

Follow [AGENTS.md](../../AGENTS.md). Use native Rust (`crates/`, `identitybench v52`) as the sole live harness, and keep retired `v1–v4` (`legacy-v1-v4`), `v4.1` + legacy Python `v5` (`legacy-v4.1`), native Rust `v5` (`legacy-v5`), and `v5.1` (`legacy-v5.1`) archival under `archive/`. Do not run live evaluations during contributor work.

- Keep each workstream within its stated files and behavioral objective. Assign an implementer for multi-file changes; focused single-owner documentation or source changes may be handled directly.
- Dispatch implementers and independent reviewers through native collaboration subagents. Route Codex inference through the globally configured CPA provider. Do not use model-specific CLIs, direct provider routes, or separate sidebar tasks.
- Assign a fresh isolated worker for blind-conformance expectation derivation. It may read only normative specifications, the model-delivered platform reference, and rendered episode workspaces; keep implementation and gold data out of its context.
- Require the matching review lanes at dataset freeze, publication, or an explicitly requested milestone gate. Keep Rust source health, dataset validation, blind conformance, and live evaluation proof distinct.
- Live `identitybench v52` runs belong only to `bench-operator` and require explicit current-turn user authorization.
