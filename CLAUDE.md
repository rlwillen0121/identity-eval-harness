Follow [AGENTS.md](AGENTS.md) for IdentityBench domain boundaries, version scope, dataset integrity, model routing, secret hygiene, and proof gates.

- Native Rust (`crates/`, `identitybench v5`) is the sole live benchmark harness. Versions `v1–v4` (`legacy-v1-v4`) and `v4.1` + legacy Python `v5` (`legacy-v4.1`) are retired, with published `v4.1` and historical Python `v5_core` results preserved under [`archive/`](archive/README.md).
- SOC/SIEM/EDR triage, firewall or network incident response, unstructured prompt-injection or jailbreak testing, and content moderation remain out of scope in every version.
- Keep sealed datasets and frozen seeds unchanged. Tune against `v5_dev` only. Derive blind conformance expectations from normative specifications and rendered policy/episode views without implementation or gold-data access.
- Use native collaboration subagents for delegated work and route Codex inference through the configured global CPA provider. Run live model evaluations only with explicit current-turn authorization.
- Verify Rust source health (`cargo test --workspace --locked`, `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets --locked`) and the applicable Rust validation gates (`identitybench v5 validate ... --double-build`). Do not conflate source health with conformance, live evaluation, or publication proof.
