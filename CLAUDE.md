Follow [AGENTS.md](AGENTS.md) for IdentityBench domain boundaries, version scope, dataset integrity, model routing, secret hygiene, and proof gates.

- The public v4.1 benchmark remains frozen. Versions v1–v4 are archival. Native Rust is the active v5 execution path; the Python v5 code is retained for compatibility and differential checks, not live v5 runs.
- Keep structured v5 `agent_governance` out of v4.1. SOC/SIEM/EDR triage, firewall or network incident response, unstructured prompt-injection or jailbreak testing, and content moderation remain out of scope in every version.
- Keep sealed datasets and frozen seeds unchanged. Tune against development data only. Derive blind conformance expectations from normative specifications and rendered policy/episode views without implementation or gold-data access.
- Use native collaboration subagents for delegated work and route Codex inference through the configured global CPA provider. Run live model evaluations only with explicit current-turn authorization.
- For native v5, verify Rust source health and the applicable Rust validation gates; run the retained Python reference checks separately. Do not conflate source health with conformance, live evaluation, or publication proof.
