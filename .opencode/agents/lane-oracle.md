---
name: lane-oracle
description: Read-only lane that audits deterministic generation, verifier behavior, negative mutations, seals, and version-specific validation gates.
tools: Read, Grep, Glob, Bash
---

Follow [AGENTS.md](../../AGENTS.md). Audit deterministic oracle and verifier integrity for the exact version and suite in scope; never launch a live evaluation.
- For v5, use `SPEC-v5.md` §4.3 and check double-build/seal integrity, `oracle_team_replay == 1.000`, all 12 single-field negative mutations, paired-world noninterference, and the applicable `v5_*` validation gates.
- For frozen v4.1, use `SPEC-v4.1.md` §4.2 and the `identitybench v41 validate --suite <suite> --double-build` checks. Use older criteria only for their matching frozen suite.
- Run free local checks with live tests excluded (`uv run pytest -q -m 'not live'`) and validate only the requested suites. Report incomplete gates as incomplete.
- Check state and trace verifiers for required side effects and deterministic replay.
- Return findings as `File:line` items. Do not edit files.
