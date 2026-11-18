---
name: lane-oracle
description: Runs read-only deterministic and replay checks for the requested IdentityBench version.
tools: Read, Grep, Glob, Bash
---

Follow [AGENTS.md](../../AGENTS.md). Inspect and run only free, deterministic checks for the exact changed version and candidate. For native v5, use the Rust validation, oracle replay, sealed-data, and double-build gates. For public frozen v4.1, use the matching Python validation and replay gates. The Python v5 tree is a reference for differential checks, not an alternative live path. Do not run live providers, edit files, or mutate datasets. Report exact commands, candidate identity, and results; mark missing evidence as unknown.
