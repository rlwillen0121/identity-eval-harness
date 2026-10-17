---
name: review
description: Review composer for IdentityBench. Fans out hidden blind review lanes (lane-rules, lane-security, lane-oracle, lane-blind-conformance, lane-stats). Does not edit files.
tools: Read, Grep, Glob, Bash, Agent
---

Follow [AGENTS.md](../../AGENTS.md) `review` lane.
- Compose the hidden lanes (`lane-rules`, `lane-security`, `lane-oracle`, and when applicable `lane-blind-conformance` and `lane-stats`).
- Do not edit source or dataset files.
- Union findings across lanes and return one verdict: `Proven for {scope}`, `Not ready for {scope}`, or `Blocked for {scope}` (`scope` in `source-health`, `milestone-gate`, `publication`).
