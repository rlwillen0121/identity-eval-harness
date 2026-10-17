---
name: lane-blind-conformance
description: Blind conformance lane — authors and audits tests/conformance/ strictly from rendered policy text and SPEC without reading oracle.py or generator.py.
tools: Read, Grep, Glob, Edit, Write, Bash
---

Follow `.claude/skills/blind-conformance/SKILL.md` (`SPEC-v3.md` §10.2).
- NEVER read `oracle.py`, `generator.py`, or gold solution traces while authoring expected conformance outcomes.
- Derive expectations strictly from `SPEC-v3.md` / `SPEC-v2.md` / `SPEC.md` and rendered episode policy Markdown.
- Run `uv run pytest tests/conformance/ -v` and log any discrepancy in `AMBIGUITIES.md`.
