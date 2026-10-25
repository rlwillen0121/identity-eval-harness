---
name: implement
description: IdentityBench contributor implementer. Edits named files and runs free local verification (pytest, ruff, mypy, validate --double-build). Never calls paid/live models.
tools: Read, Grep, Glob, Edit, Write, Bash
---

Follow [AGENTS.md](../../AGENTS.md) `implement` lane.
- Edit only the files named in your task.
- Run free local verification (`uv run pytest`, `uv run ruff check .`, `uv run mypy --strict src`, `uv run identitybench v41 validate --suite <suite> --double-build`, deterministic baselines).
- NEVER run `identitybench v41 run` (or legacy `v3 run`) against live model providers (`anthropic`, `openai_compatible`, `google_genai`, `claude_cli`, `codex_cli`, `gemini_cli`, `agy_cli`, `muse_cli`).
- Never hand-edit frozen dataset JSONLs or mutate `SEAL.json`.
- Never log or commit secrets or private endpoint URLs.
