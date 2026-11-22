# IdentityBench Agent Rules

## 1. Product and Version Boundaries

- **Active v5.1 implementation (sole live benchmark):** The native Rust workspace in `crates/` is the sole live benchmark harness (`identitybench v51`). Its normative contract is the frozen [`SPEC-v5.1.md`](SPEC-v5.1.md) (`v5.1.0`), with sealed suites under `datasets/v51/` and imported run artifacts under `reports/v51/runs/`.
- **Version-specific agent governance:** Structured, deterministic NHI and AI-agent governance (the `nhi_agent_governance` category, including `agt_*` agent registration and `dlreq_*` `RFC 8693` delegation decisions) belongs only to v5.1. Do not fold it into archived generations.
- **Retired versions:** `v1–v4` (reproducible at `git tag legacy-v1-v4`), `v4.1` + legacy Python `v5` (reproducible at `git tag legacy-v4.1`), and native Rust `v5` (`identitybench v5`, Draft 5; reproducible at `git tag legacy-v5`) are retired. Their specifications, sealed datasets, and reports—including the published native `v5_core` leaderboard and historical Python `v5` Draft 5 audit/preview results—are preserved as archival artifacts under [`archive/`](archive/README.md). Do not restore retired runners or create new live runs for retired versions.
- **Prohibited in every version:** Generic SOC/SIEM/EDR alert triage, firewall or network incident response, unstructured prompt-injection or jailbreak testing, and LLM content moderation.

## 2. Dataset Integrity and Blind Conformance

1. Sealed dataset files and seeds are immutable. Never hand-edit `episodes.jsonl`, `oracles.jsonl`, `SEAL.json`, or a source-pinned digest under `datasets/v51/` or `archive/datasets/`.
2. Tune v5.1 prompts, schemas, engine behavior, and verifier tolerances on `v51_dev` only. Never inspect or tune against held-out `v51_lite`, `v51_core`, or `v51_full` results (`SPEC-v5.1.md` §2 anti-leakage rule).
3. Derive conformance expectations in a fresh, isolated native-collaboration worker using only [`SPEC-v5.1.md`](SPEC-v5.1.md), the model-delivered platform reference (`platform_reference_v51.md`), and the rendered public episode workspace. Before expectations are fixed, do not read or search Rust engine, generator, runtime, or schema implementations, gold configurations or traces, or `oracles.jsonl`. Compare the completed blind reference with implementation behavior (`identitybench v51 validate --blind-configs`, gate `G-BLIND-V51`) only after derivation is complete.

## 3. Live Spend, Model Routing, and Secret Hygiene

- Run benchmark evaluations or benchmark workload provider calls only when the user explicitly authorizes them in the current turn (`identitybench v51 run`). Use a distinct `--out` directory for each experiment configuration.
- Route Codex inference, including delegated work, through the globally configured CPA provider using protected local configuration. Use native collaboration subagents for delegation. Do not invoke model-specific CLIs, direct provider routes, or separate sidebar threads unless the user explicitly directs otherwise.
- Never print, log, commit, or include in a prompt or handoff credentials, bearer or OAuth tokens, `.env` values, or private endpoint URLs.

## 4. Work and Proof Boundaries

- Make focused edits directly for single-owner changes. Use native collaboration subagents for independent parallel work, required blind-conformance isolation, and milestone or publication reviews. Keep reviewer lanes read-only.
- **Rust source health:** `cargo test --workspace --locked`, `cargo fmt --all -- --check`, and `cargo clippy --workspace --all-targets --locked`; build the executable with `cargo build --release --locked -p ib-cli` when executable verification is needed. Keep `git ls-files '*.py'` empty (`test -z "$(git ls-files '*.py')"`), and verify static site JS with `node --test "site/tests/*.test.mjs"`.
- When v5.1 generator, engine, runtime, schema, or dataset behavior changes, validate the matching sealed suite with `./target/release/identitybench v51 validate --suite <v51_dev|v51_lite|v51_core|v51_full> --double-build`.
- Keep source health, dataset validation, blind conformance, live evaluation, milestone acceptance, publication, and deployment as separate claims. Do not describe offline checks as live evidence or claim a benchmark milestone without its specified gates and review verdict. Report provider errors separately from parse failures and turn-limit terminations.
- Multi-lane review is required at dataset freeze and publication gates, and when explicitly requested. Apply the matching normative gates and record the exact inputs reviewed.

## 5. Tool Configuration

Shared agent configuration lives in `AGENTS.md`, `GEMINI.md`, `.agents/skills/`, `CLAUDE.md`, `.claude/agents/`, `.claude/skills/`, `opencode.json`, and `.opencode/agents/`. Keep mirrored role and skill instructions consistent with these rules.
