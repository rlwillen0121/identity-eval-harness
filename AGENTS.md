# IdentityBench Agent Rules

## 1. Product and Version Boundaries

- **Active v5.2 implementation (sole live benchmark):** The native Rust workspace in `crates/` is the sole live benchmark harness (`identitybench v52`). Its normative contract is [`SPEC-v5.2.md`](SPEC-v5.2.md) (`v5.2.0`, still in Draft), with provisionally sealed suites under `datasets/v52/`. No v5.2 results are published yet; the published v5.1 run artifacts under `reports/v51/runs/` and the site built from them stay in place until v5.2 results exist.
- **Version-specific agent governance:** Structured, deterministic NHI and AI-agent governance (the `nhi_agent_governance` category, including `agt_*` agent registration and `dlreq_*` `RFC 8693` delegation decisions) belongs only to v5.2 (inherited from the archived v5.1). Do not fold it into earlier archived generations.
- **Retired versions:** `v1–v4` (reproducible at `git tag legacy-v1-v4`), `v4.1` + legacy Python `v5` (reproducible at `git tag legacy-v4.1`), native Rust `v5` (`identitybench v5`, Draft 5; reproducible at `git tag legacy-v5`), and native Rust `v5.1` (`identitybench v51`, `SPEC-v5.1`; reproducible at `git tag legacy-v5.1`) are retired. Their specifications, sealed datasets, and reports—including the published native `v5_core` leaderboard, the v5.1 specification and sealed suites, and historical Python `v5` Draft 5 audit/preview results—are preserved as archival artifacts under [`archive/`](archive/README.md). Do not restore retired runners or create new live runs for retired versions.
- **Prohibited in every version:** Generic SOC/SIEM/EDR alert triage, firewall or network incident response, unstructured prompt-injection or jailbreak testing, and LLM content moderation.

## 2. Dataset Integrity and Blind Conformance

1. Sealed dataset files and seeds are immutable. Never hand-edit `episodes.jsonl`, `oracles.jsonl`, `SEAL.json`, or a source-pinned digest under `datasets/v52/` or `archive/datasets/`.
2. Tune v5.2 prompts, schemas, engine behavior, and verifier tolerances on `v52_dev` only. Never inspect or tune against held-out `v52_lite`, `v52_core`, or `v52_full` results (`SPEC-v5.2.md` §2 anti-leakage rule).
3. Derive conformance expectations in a fresh, isolated native-collaboration worker using only [`SPEC-v5.2.md`](SPEC-v5.2.md), the model-delivered platform reference (`platform_reference_v52.md`), and the rendered public episode workspace. Before expectations are fixed, do not read or search Rust engine, generator, runtime, or schema implementations, gold configurations or traces, or `oracles.jsonl`. Compare the completed blind reference with implementation behavior (`identitybench v52 validate --blind-configs`, gate `G-BLIND-V52`) only after derivation is complete.

## 3. Live Spend, Model Routing, and Secret Hygiene

- Run benchmark evaluations or benchmark workload provider calls only when the user explicitly authorizes them in the current turn (`identitybench v52 run`). Use a distinct `--out` directory for each experiment configuration.
- Route Codex inference, including delegated work, through the globally configured CPA provider using protected local configuration. Use native collaboration subagents for delegation. Do not invoke model-specific CLIs, direct provider routes, or separate sidebar threads unless the user explicitly directs otherwise.
- Never print, log, commit, or include in a prompt or handoff credentials, bearer or OAuth tokens, `.env` values, or private endpoint URLs.

## 4. Work and Proof Boundaries

- Make focused edits directly for single-owner changes. Use native collaboration subagents for independent parallel work, required blind-conformance isolation, and milestone or publication reviews. Keep reviewer lanes read-only.
- **Rust source health:** `cargo test --workspace --locked`, `cargo fmt --all -- --check`, and `cargo clippy --workspace --all-targets --locked`; build the executable with `cargo build --release --locked -p ib-cli` when executable verification is needed. Keep `git ls-files '*.py'` empty (`test -z "$(git ls-files '*.py')"`), and verify static site JS with `node --test "site/tests/*.test.mjs"`.
- When v5.2 generator, engine, runtime, schema, or dataset behavior changes, validate the matching sealed suite with `./target/release/identitybench v52 validate --suite <v52_dev|v52_lite|v52_blind_fixture> --double-build`. Private suites are never in the repository: validate them only from the operator's offline directory with the private key read at run time, `./target/release/identitybench v52 validate --suite <v52_core|v52_full> --dataset <operator-dir> --double-build --private-key-file <key-file>` (or `IDENTITYBENCH_V52_PRIVATE_KEY_HEX`); never print, log, or commit the key.
- Keep source health, dataset validation, blind conformance, live evaluation, milestone acceptance, publication, and deployment as separate claims. Do not describe offline checks as live evidence or claim a benchmark milestone without its specified gates and review verdict. Report provider errors separately from parse failures and turn-limit terminations.
- Multi-lane review is required at dataset freeze and publication gates, and when explicitly requested. Apply the matching normative gates and record the exact inputs reviewed.

## 5. Tool Configuration

Shared agent configuration lives in `AGENTS.md`, `GEMINI.md`, `.agents/skills/`, `CLAUDE.md`, `.claude/agents/`, `.claude/skills/`, `opencode.json`, and `.opencode/agents/`. Keep mirrored role and skill instructions consistent with these rules.
