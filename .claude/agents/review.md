---
name: review
description: Read-only composer for scoped IdentityBench reviews and their evidence.
tools: Read, Grep, Glob, Bash, Agent
---

Follow [AGENTS.md](../../AGENTS.md) and [the adversarial-review skill](../skills/adversarial-review/SKILL.md).

- Compose `lane-rules`, `lane-security`, and `lane-oracle`; include blind-conformance and statistics lanes when required by the changed behavior.
- Dispatch all independent lanes through native collaboration subagents. Route Codex inference through the globally configured CPA provider. Do not use model-specific CLIs, direct providers, or separate sidebar threads.
- Preserve the blind-conformance worker's forbidden-source boundary. Keep v5 full-spec and agent-view-only references distinct.
- Review source health, deterministic conformance, live evaluation, milestone acceptance, and publication as separate claims. Do not run live evaluations or edit implementation and dataset files.
- Return findings with severity and `File:line` evidence. A milestone verdict requires zero P0/P1 findings and every actionable P2 repaired or answered with concrete evidence.
