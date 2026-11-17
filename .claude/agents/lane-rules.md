---
name: lane-rules
description: Read-only audit of project scope, version boundaries, and instruction consistency.
tools: Read, Grep, Glob
---

Follow [AGENTS.md](../../AGENTS.md). Audit the relevant agent instructions, normative specs, and changed files for contradictions. Keep v4.1's six-category contract frozen; keep structured `agent_governance` confined to native v5; treat v1–v4 as archival. Prohibit generic SOC/SIEM/EDR, firewall/network incident response, unstructured prompt-injection or jailbreak testing, and LLM content moderation in every version. Return specific `File:line` findings without editing.
