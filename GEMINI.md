Follow [AGENTS.md](AGENTS.md). Same lanes, domain boundaries, and proof gates.

- **Primary Specs:**
  - v3 (Agentic IGA & Zero-Trust Harness): [SPEC-v3.md](SPEC-v3.md)
  - v1 (Core IGA Harness): [SPEC.md](SPEC.md)
  - v2 (Compositional & Multi-Turn IGA): see [reports/v2/METHOD.md](reports/v2/METHOD.md)
- **Strict Separation of Duties:**
  - `orchestrate` coordinates work and composes multi-lane reviews.
  - `implement` edits named files and runs free local verification (`pytest`, `ruff`, `mypy`, `identitybench validate --double-build`, deterministic baselines). Never triggers live model runs.
  - `lane-blind-conformance` writes/audits conformance scenarios strictly from rendered policy text without inspecting `oracle.py` or `generator.py`.
  - `bench-operator` runs live model evaluations (`identitybench run`) only when explicitly requested by the user.
- **Project Skills:** Located in `.agents/skills/` (and mirrored in `.claude/skills/`):
  - `bench-operator`
  - `blind-conformance`
  - `adversarial-review`
