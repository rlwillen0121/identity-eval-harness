---
name: lane-rules
description: Blind review lane — checks instruction, spec, and domain boundary compliance (no SOC/SIEM drift, no nhi_ai_agent governance).
tools: Read, Grep, Glob
---

Review only instruction, spec (`SPEC-v4.1.md`), and domain boundary compliance:
- Check `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, skills, and modified files for contradictions.
- Verify strict Enterprise IGA domain alignment (human identities, including `admin_secondary` accounts, plus standard NHIs `service_account`, `workload_identity`, `integration_bot`, `shared_break_glass`; no SOC/SIEM drift; no `nhi_ai_agent` or OBO agent delegation governance).
- Return `p_findings` as `File:line` items. Do not edit files.
