# Defensive Security Audit Report

Audit and remediation completed for `feat/v5-harness` at `618a69f318b36b5961b1c1667634c8a79fa5f45c`. Candidate diff SHA-256: `68c92ac416f6e5bbcf4b2b90c9cc1c4be0215411f43d4f91eb16f51c3ce7e216`.

## Audit Execution Metadata

- **Model / Program / Effort:** `gpt-daybreak-blue-latest` (`--cyber-access-program daybreak_blue`, `model_reasoning_effort="high"`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `-s workspace-write`)
- **Session / Auth:** Local logged-in OpenAI Codex session (no CPA routes or API keys exposed)
- **Token Usage (`turn.completed`):** `input_tokens=67,870,776` (`cached_input_tokens=67,361,792`), `output_tokens=85,204` (`reasoning_output_tokens=28,022`)

## Vulnerabilities remediated

| Severity | Finding and root cause | Defensive remediation and regression proof |
|---|---|---|
| High | Untrusted provider responses could contain oversized, cyclic, deeply nested, non-JSON, or extreme numeric values, causing CPU/memory exhaustion, serialization failures, or mutable replay-state corruption. Primary boundary: [_json.py](../../src/identitybench/providers/_json.py#L58), lines 58–615; [replay.py](../../src/identitybench/providers/replay.py#L64), lines 64–226. | Added byte, node, depth, token, string, and cumulative scan-work limits; cycle-aware JSON-safe copying; linear reasoning-tag processing; strict tool-call validation; bounded raw-usage evidence; and deep ownership isolation. API and CLI adapters now sanitize at the response boundary. Covered by [test_response_boundary_security.py](../../tests/providers/test_response_boundary_security.py#L28), lines 28–284, and [test_replay.py](../../tests/providers/test_replay.py#L89), lines 89–139. |
| High | Malformed, clamped, camelCase, or overflowed usage metadata could appear complete, corrupt accounting, overflow float totals, or emit invalid strict JSON. [v5/runner.py](../../src/identitybench/v5/runner.py#L2063), lines 2063–2113, 2432–2843, 3086–3120, and 4230–4262; [_cli.py](../../src/identitybench/providers/_cli.py#L1196), lines 1196–1225; [cost.py](../../src/identitybench/runner/cost.py#L292), lines 292–490. | Added finite saturating arithmetic, exact integer-micros attribution, strict `>` crossing semantics, bounded token validation, latency normalization, anomaly evidence, and fail-closed unavailable/partial accounting. Known billed costs remain preserved. Tests cover exact ceiling, true crossing, retries, camelCase counts, latency, and strict JSON in [test_runner.py](../../tests/v5/test_runner.py#L170), lines 170–425 and 1109–1308; [test_cost.py](../../tests/runner/test_cost.py#L279), lines 279–303. |
| High | Provenance and historical receipt paths could escape their intended trust root through absolute paths, `..`, root mismatches, or symlink components. [validation.py](../../src/identitybench/v5/validation.py#L8411), lines 8411–8688 and 9396–9483. | Added canonical root binding, explicit allowed archive roots, NUL/backslash/traversal rejection, component-by-component symlink rejection, sensitive-path checks before reads, and resolved-path propagation. Regression coverage: [test_validation.py](../../tests/v5/test_validation.py#L3261), lines 3261–3354. |
| High | Exceptions and persisted diagnostics could disclose URL userinfo, private hosts and paths, query credentials, home usernames, or sensitive environment values. Child Git inspection also inherited more environment than necessary. [_subprocess.py](../../src/identitybench/providers/_subprocess.py#L320), lines 320–440; [config.py](../../src/identitybench/runner/config.py#L298), lines 298–314; [manifest.py](../../src/identitybench/runner/manifest.py#L304), lines 304–326. | Expanded credential-name detection, redacted arbitrary diagnostic URL authority/path/query content, sanitized launch failures and nested metadata, masked home paths, made home-path warnings generic, and gave Git a minimal environment with filesystem monitoring disabled. Tests: [test_response_boundary_security.py](../../tests/providers/test_response_boundary_security.py#L232), lines 232–278; [test_boundary_security.py](../../tests/runner/test_boundary_security.py#L16), lines 16–61. |
| Medium | Untrusted report labels could break Markdown code spans or synthesize autolinks, including through currency labels. [markdown_text.py](../../src/identitybench/report/markdown_text.py#L21), lines 21–56; [v5/report.py](../../src/identitybench/v5/report.py#L226), lines 226–496 and 1878–1964. | Added dynamic code-span fencing, entity/line-break normalization, autolink neutralization, and consistent escaping of v5 labels and currency values. Tests: [test_markdown_text.py](../../tests/report/test_markdown_text.py#L8), lines 8–56; [test_report.py](../../tests/v5/test_report.py#L1546), lines 1546–1571. |

## Verification

- `uv run ruff check`: passed.
- Security-focused provider, report, runner, and v5 accounting/report suite: passed.
- Final targeted v5 path, latency, usage, and exact-cost boundary set: 11/11 passed.
- `git diff --check`: passed.
- Independent rules, security, oracle, and accounting/statistics lanes: PASS, no unresolved findings.
- Untouchable-file diff guard: clean.

The complete v5 runner module’s final rerun encountered blocked DNS while an external tokenizer cache was missing. Affected regressions were made fully offline with deterministic token counters and passed; no full source-health, milestone, publication, or live-evaluation claim is made.

## Clean or residual observations

- No `shell=True`, `os.system`, unsafe archive extraction, or exploitable option-injection path was found. CLI execution uses argument arrays and isolated environments/private call directories.
- HTML reports already use Jinja autoescaping and strict JSON; YAML loading uses safe loaders.
- Remaining low-risk observations: local operator YAML has no explicit size or duplicate-key policy, and provenance reads retain a narrow concurrent symlink-swap TOCTOU window.
- No credentials or protected configuration were inspected, no forbidden files were changed, and unrelated worktree content was preserved.