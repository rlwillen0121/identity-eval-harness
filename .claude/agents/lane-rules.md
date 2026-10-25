---
name: lane-rules
description: Blind review lane — checks instruction, spec, and domain boundary compliance (no SOC/SIEM drift, no nhi_ai_agent governance).
tools: Read, Grep, Glob
---

Review only instruction, spec (`SPEC-v4.1.md` current; `SPEC-v4.md`, `SPEC-v3.md`, `SPEC.md` legacy), and domain boundary compliance:
- Check `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, skills, and modified files for contradictions.
- Verify strict Enterprise IGA domain alignment (humans + standard NHIs `service_account`, `workload_identity`, `integration_bot`, `admin_secondary`, `shared_break_glass`; no SOC/SIEM drift; no `nhi_ai_agent` or OBO agent delegation governance).
- Return `p_findings` as `File:line` items. Do not edit files.
