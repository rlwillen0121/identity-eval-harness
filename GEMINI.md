Follow [AGENTS.md](AGENTS.md). Same lanes, domain boundaries, and proof gates.

- **Primary Specs:**
  - v4.1 (current; Combinatorial IGA, Coupling & Provenance): [SPEC-v4.1.md](SPEC-v4.1.md)
- **Strict Separation of Duties:**
  - `orchestrate` coordinates work and composes multi-lane reviews.
  - `implement` edits named files and runs free local verification (`pytest`, `ruff`, `mypy`, `identitybench v41 validate --double-build`, `identitybench v41 verify --run <dir>`, deterministic baselines). Never triggers live model runs.
  - `lane-blind-conformance` writes/audits conformance scenarios strictly from rendered policy text without inspecting `oracle.py` or `generator.py`.
  - `bench-operator` runs live model evaluations (`identitybench v41 run`) only when explicitly requested by the user.
- **Project Skills:** Located in `.agents/skills/` (and mirrored in `.claude/skills/`):
  - `bench-operator`
  - `blind-conformance`
  - `adversarial-review`
