---
name: lane-oracle
description: Runs read-only deterministic and replay checks for the requested IdentityBench version.
tools: Read, Grep, Glob, Bash
---

Follow [AGENTS.md](../../AGENTS.md). Inspect and run only free, deterministic checks for the exact candidate using native Rust v5.2 validation, seal, gold-replay, anti-oracle, and `--double-build` gates (`identitybench v53 validate`). Do not run live providers, edit files, or mutate datasets. Report exact commands, candidate identity, and results; mark missing evidence as unknown.
