# IdentityBench Agent Rules

## 1. Product and Version Boundaries

- **Active v5 implementation (sole live harness):** The native Rust workspace in `crates/` is the sole live benchmark harness (`identitybench v5`). Its normative contract is [`SPEC-v5.0-draft.5.md`](SPEC-v5.0-draft.5.md) together with the unsuperseded requirements of [`SPEC-v5.md`](SPEC-v5.md), [`SPEC-v5-ADDENDUM-01.md`](SPEC-v5-ADDENDUM-01.md), and [`SPEC-v5-DIRECTIVE-01.md`](SPEC-v5-DIRECTIVE-01.md).
- **Version-specific agent governance:** Structured, deterministic `agent_governance` (including `EnterpriseAgentV5`, `agt_*`, `dlreq_*`, and the specified delegation controls) belongs only to v5.
- **Retired versions:** `v1–v4` (reproducible at `git tag legacy-v1-v4`) and `v4.1` + legacy Python `v5` (reproducible at `git tag legacy-v4.1`) are retired. Published `v4.1` specifications, sealed datasets, and reports—along with historical Python `v5` Draft 5 audit/preview results—are preserved as archival artifacts under [`archive/`](archive/README.md). Do not restore Python runners or create new live runs for retired versions.
- **Prohibited in every version:** Generic SOC/SIEM/EDR alert triage, firewall or network incident response, unstructured prompt-injection or jailbreak testing, and LLM content moderation.

## 2. Dataset Integrity and Blind Conformance

1. Sealed dataset files and seeds are immutable. Never hand-edit `episodes.jsonl`, `oracles.jsonl`, `SEAL.json`, or a source-pinned digest under `datasets/v5_draft5/`, `datasets/v5_*`, or `archive/datasets/v41_*`.
2. Tune v5 prompts, schemas, and verifier tolerances on `v5_dev` only. Never tune against held-out `v5_lite`, `v5_core`, or `v5_full` results.
3. Derive conformance expectations in a fresh, isolated native-collaboration worker using only the matching normative specification and the rendered episode and policy views. Before expectations are fixed, do not read or search Rust oracle, generator, verifier, or baseline implementations, gold traces, or `oracles.jsonl`. For v5 agent-view expectations, use only `PolicyCardV5` and information legally delivered to the model; for full-spec expectations, attribute rules to the correct specification, addendum, or directive. Compare the completed blind reference with implementation behavior only after derivation is complete.

## 3. Live Spend, Model Routing, and Secret Hygiene

- Run benchmark evaluations or benchmark workload provider calls only when the user explicitly authorizes them in the current turn (`identitybench v5 run`). Use a distinct `--out` directory for each experiment configuration.
- Route Codex inference, including delegated work, through the globally configured CPA provider using protected local configuration. Use native collaboration subagents for delegation. Do not invoke model-specific CLIs, direct provider routes, or separate sidebar threads unless the user explicitly directs otherwise.
- Never print, log, commit, or include in a prompt or handoff credentials, bearer or OAuth tokens, `.env` values, or private endpoint URLs.

## 4. Work and Proof Boundaries

- Make focused edits directly for single-owner changes. Use native collaboration subagents for independent parallel work, required blind-conformance isolation, and milestone or publication reviews. Keep reviewer lanes read-only.
- **Rust v5 source health:** `cargo test --workspace --locked`, `cargo fmt --all -- --check`, and `cargo clippy --workspace --all-targets --locked`; build the executable with `cargo build --release --locked -p ib-cli` when executable verification is needed. Keep `git ls-files '*.py'` empty (`test -z "$(git ls-files '*.py')"`), and verify static site JS with `node --test "site/tests/*.test.mjs"`.
- When v5 generator, oracle, verifier, schema, or dataset behavior changes, validate the matching sealed Draft 5 suite with `./target/release/identitybench v5 validate --suite <v5_dev|v5_lite|v5_core|v5_full> --double-build`.
- Keep source health, dataset validation, blind conformance, live evaluation, milestone acceptance, publication, and deployment as separate claims. Do not describe offline checks as live evidence or claim a benchmark milestone without its specified gates and review verdict. Report provider errors separately from parse failures and turn-limit terminations.
- Multi-lane review is required at dataset freeze and publication gates, and when explicitly requested. Apply the matching normative gates and record the exact inputs reviewed.

## 5. Tool Configuration

Shared agent configuration lives in `AGENTS.md`, `GEMINI.md`, `.agents/skills/`, `CLAUDE.md`, `.claude/agents/`, `.claude/skills/`, `opencode.json`, and `.opencode/agents/`. Keep mirrored role and skill instructions consistent with these rules.
