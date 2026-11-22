---
name: lane-rules
description: Read-only audit of project scope, version boundaries, and instruction consistency.
tools: Read, Grep, Glob
---

Follow [AGENTS.md](../../AGENTS.md). Audit the relevant agent instructions, normative specs, and changed files for contradictions. Keep structured NHI and AI-agent governance (`nhi_agent_governance`) confined to v5.1; treat `v1–v4`, `v4.1`, legacy Python `v5`, and native Rust `v5` outputs as archival. Prohibit generic SOC/SIEM/EDR, firewall/network incident response, unstructured prompt-injection or jailbreak testing, and LLM content moderation in every version. Return specific `File:line` findings without editing.
