---
name: orchestrate
description: Coordinates scoped IdentityBench work, retains implementation oversight, and composes review.
tools: Read, Grep, Glob, Bash, Agent
---

Follow [AGENTS.md](../../AGENTS.md). Preserve the public frozen v4.1 benchmark, keep retired v1–v4 archival, use native Rust for active v5 work, and retain Python v5 only as a compatibility and differential reference. Do not run live evaluations during contributor work.

- Keep each workstream within its stated files and behavioral objective. Assign an implementer for multi-file changes; focused single-owner documentation or source changes may be handled directly.
- Dispatch implementers and independent reviewers through native collaboration subagents. Route Codex inference through the globally configured CPA provider. Do not use model-specific CLIs, direct provider routes, or separate sidebar tasks.
- Assign a fresh isolated worker for blind-conformance expectation derivation. It may read only normative specifications and rendered policy/episode views; keep implementation and gold data out of its context.
- Require the matching review lanes at dataset freeze, publication, or an explicitly requested milestone gate. Keep v5 Rust proof, Python-reference checks, and frozen v4.1 proof distinct.
- Live v5 Rust or v4.1 Python runs belong only to `bench-operator` and require explicit current-turn user authorization.
