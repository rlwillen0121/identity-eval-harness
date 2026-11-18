# Rust transition final audit

Audit base: `8c87f1c7239274b238244e7c838d3a29271cfc69`. This audit follows the
initial integration review and historical-run parity audit. It evaluates the native
v5 implementation, its deployment-independent command paths, and migration evidence.
Frozen v1–v4.1 behavior, the retained Python v5 reference, specifications, sealed
datasets, and historical model-run inputs remain outside the mutation scope.

Status: source remediation and local integrated verification complete. No unresolved
actionable findings remain in the reviewed migration scope. Exact-head hosted CI and
publication status are recorded on [PR #8](https://github.com/rlwillen0121/identity-eval-harness/pull/8).
This document is not a release or benchmark milestone approval.

The [Daybreak Blue follow-up](DAYBREAK.md) records the subsequent provider and
dataset file-handling repairs and the validation of the rebuilt executable. The
source and binary digests below identify the original audit at commit `0bdd288`.

## Review lanes and authority

Native collaboration workers used Sol medium for orchestration and technical review,
Terra high for repository/documentation/CI review, and Luna xhigh for implementation.
Fast service was requested in dispatch contracts; the collaboration API does not
independently report the effective service tier or per-worker transport. The session API
base URL matched the required CPA endpoint, and worker dispatch contracts required that
route. No model-specific CLI was used for delegated inference, and no live benchmark
calls were made.

Independent lanes covered repository surface and portability, provider/artifact
security, schema/oracle/runtime/generator coverage, accounting/statistical parity,
conformance execution and provenance, and isolated normative-specification review.
The fresh stimulus reviewer read the four normative specifications, 24 legally delivered
dev views, and the isolation instructions. It did not read implementation, gold outputs,
previous reference answers, held-out views, or production outcomes. Its frozen
[stimulus contract](blind-stimulus-contract.md), SHA-256
`ca9b5a5d0b327452bff227b4b4d4ec2a8be8eb4b4ee073e314bfcaf59264c642`, is review
guidance, not a new qualified blind reference or G-SPEC-01 receipt.

## Findings

| Finding | Severity | Concrete reproduction | Remediation and review |
| --- | --- | --- | --- |
| Anthropic redirects forwarded credentials and prompts to another origin | P1 | Two loopback ports and a synthetic key; HTTP 307 reached the second listener | Both HTTP adapters reject redirects; independent 302/307 checks confirm no destination request |
| Provider bodies were buffered without a byte limit | P2 | Oversized valid JSON with ignored padding was accepted | Shared incremental 20 MiB limit covers success/error, chunked and close-delimited responses; independent bounded-reader tests pass |
| A fresh run could overwrite an existing run | P1 | Two replay runs with different model identities reused one output directory | Read-only collision preflight and exclusive output lock; rejects symlinks and multiply linked markers, preserves default report directories on resume; CLI unit and executable preflight/resume tests pass |
| Resume deleted failed attempts and freshly reexecuted their slots | P1 | The loader dropped provider-incomplete and observed parse-failure rows, then rewrote the raw log before scheduling replacement attempts | Observed terminal rows are preserved and a durable, dataset/config-bound started-slot journal precedes dispatch; ambiguous or infrastructure-incomplete histories fail closed. Four executable regressions cover paid parse failures, paid failures, kill/restart and preflight controls |
| Retry eligibility treated missing evidence or error diagnostics as proof of no response | P1 | Missing `proven_unanswered` defaulted to true; HTTP status, CLI timeouts and malformed-call diagnostics could trigger fresh retries, while buffered CLI output was ignored | Explicit unanswered-request proof, disabled local retries and actual adapter checkpoint support are required; buffered usage/cost are retained; all 89 provider/runner tests pass and independent review accepts the repair; executable CLI retry/continuation preflight proof passes |
| Recovery reused stale request state and could count one physical request twice | P1 | A later error could inherit an earlier request ID/checkpoint; successful retrieval appended another physical-usage row with the same request ID | Current-response identifiers/checkpoints and single-request reconciliation preserve safe prior diagnostics without double counting; all seven redispatch branches independently reviewed, with 15 focused regressions passing |
| Category contrasts tested G-SRR instead of the selected category | P1 | Identical classification outcomes with opposite G-SRR produced a false significant difference | Explicit category selector and category vectors; independent probe now gives delta 0, p=q=1 |
| Duplicate confirmatory family names silently overwrote results | P2 | Duplicate additional-family names were accepted | Duplicate and reserved names now rejected before inference; independent probe passes |
| Large suite seeds overflowed in debug and wrapped in release | P2 | Maximum u64 seed on offset suites | Widened intermediate arithmetic; Python-reference boundary vectors pass debug/release and all four sealed datasets retain exact bytes |
| Rust conformance execution and build provenance were incomplete | P1 | Python-only source identity and production profiles; no native freeze/run/bind/verify workflow | Native prepare/freeze/run/bind/verify, trusted all-suite corpus coverage and compiled-source/executable bindings are implemented and source-reviewed; full-corpus CLI prepare/freeze/run/bind/verify/validate and mutation verification pass |
| Literal conformance submissions omitted rule-coverage accounting | P2 | A DEV typed binding-citation negative produced the expected Stage 4 diagnostic but lost the cited rule because literal plans have no derived base plan | Native-only accounting and every validator/comparator now use the same status-aware contract derived from literal public inputs; forged or missing coverage is rejected, archived results remain byte-identical, and four focused regressions pass. Full integrated verification passes |
| Blind source isolation omitted Rust implementation and archive aliases | P1 | Rust oracle declared as public input; dot-component and archive-path aliases | Protection moved to the actual provenance-read boundary with normalized and resolved checks; captured own-source bytes retain canonical guards while logical source labels remain relocatable; focused guards and independent source review pass |
| Operator docs described outdated runtime/version boundaries and omitted pricing input | P2 | Documentation/CLI comparison | Corrected version roles, reference-debt disclosure, required pricing and synthetic replay example; independent surface review passes |
| Public Python v5 run dispatch remained reachable despite the native-only designation | P2 | Existing root-CLI regression reached a mocked OpenAI-compatible provider constructor | Public command now refuses before configuration, environment or provider access; independent review and full Python source-health checks pass; frozen reference module retained for differential tests |
| YAML parser depended on an unmaintained backend | Maintenance | RustSec RUSTSEC-2024-0449 for unsafe-libyaml 0.2.11; no vulnerability advisory | Replaced with serde_norway 0.9.42; twelve actual old/new parser fixtures match and focused provider tests pass; updated dependency audit has no findings |

## Additional checks

- Coverage inspection mapped 86 noninternal Python schema classes and direct fields,
  23 scope-algebra functions, four suites, twelve coupling families, seven baselines,
  and twelve mutation variants to the Rust implementation. This supplements tests;
  it is not exhaustive equivalence proof for every possible input.
- The native execution path has no Python bindings or Python subprocess fallback.
  The installed-binary test copies the executable into a temporary root without
  sources, Cargo files or a virtual environment, clears the environment and PATH,
  and exercises build, validation, baseline scoring, report generation, and offline
  replay. All twelve replay-error attempts remain recorded; zero observed scoring
  slots and an incomplete report are expected.
- The development toolchain is Rust 1.100.0. CI now separately checks the declared
  Rust 1.90.0 minimum and runs the normal Rust tests on Linux and macOS. Pull requests
  receive one complete CI workflow; pushes to main and manual dispatch remain enabled.
- Historical input hashes and the earlier audit's binary/source bindings were
  independently verified before remediation. The earlier audit remains immutable;
  a rebuilt executable requires a new audit inventory and source binding.

## Evidence boundaries

The committed historical conformance receipts remain stale. Their four-suite ordered
diagnostics are regression-checked rather than rewritten to create a pass. Native
conformance mechanics and synthetic test fixtures do not qualify as fresh independent
blind certification. The required live calibration/economics evidence is separate.

Raw Pyright retains the documented 25 diagnostics in the frozen Python v5 reference.
The enforced baseline checks exact diagnostics, pinned checker configuration/version,
and all 27 reference source hashes. It is not a zero-error raw Pyright claim.

Original model runs remain historical Python-executed results. Native offline regrading
preserves their execution provenance and cannot fill missing provider responses. New
native evaluations, if later authorized, require separate output directories and their
own configuration, pricing, harness and dataset identities.

The historical audit verifies the records that still exist. It cannot reconstruct
attempts or physical requests discarded by an earlier runner, or prove that a historical
resume never replaced a slot. Artifact-level scoring completeness is separate from
that execution-history and accounting evidence.

Native retry and continuation behavior deliberately corrects unsafe reference behavior.
Positive request-state-machine fixtures declare synthetic no-response proof and checkpoint
support in memory; new regressions check that missing proof, ambiguous failures and unsupported
continuation stop safely. Those tests are not a claim of unmodified Python runner parity.
The frozen Python fixture bytes remain unchanged.

## Final local verification

The [source-health record](source-health.json) binds the local proof logs and exact
production sources. Composed Rust coverage is **462 passed tests across 70 successful
targets**: 457 in the successful workspace targets, then five in the corrected validation
integration targets. The initial workspace command stopped on an incorrect test
expectation; that entire failed target is excluded from the count. This is not a claim
that the initial workspace invocation exited successfully. The final full-corpus test
passes the positive gate, bound mutation failures, and actual native CLI
prepare/freeze/run/bind/verify/validate lifecycle. CI sets `IDENTITYBENCH_CLI` on both
platforms so the full executable lifecycle runs there too.

Rust formatting, clippy (with nonfatal style warnings), Rust 1.90 minimum-version
workspace checking, and all documentation-test targets pass. The final release binary
SHA-256 is `6414624e392c64afd1f714b5000bda66564584c2eda507fac17bb71b9a91583a`.
All 151 compiled production-source hashes match the current checkout.

The final binary's [historical audit](historical/AUDIT.md) independently rechecked
24 configurations and 3,456 recorded slots: all 24 JSON summaries and all 24 Markdown
reports are byte-identical to the historical Python outputs, with no stored-score or
summary deltas. Eighteen configurations are artifact/protocol-complete and six remain
provider-incomplete. The audit separately records 580 provider errors, 40 parse failures,
285 turn limits, 12 context limits, 149 output stops, and 2,390 episode completions.
All original run/report/dataset hashes remain unchanged. Independent final verification
checked 144 original run files, 48 original reports, 48 native outputs, six canonical
dataset bindings, the executable, and its complete source inventory.

The final native `v5 validate --suite v5_dev --double-build` retains the expected 19-entry
projection: 14 PASS, one stale-receipt G-SPEC-01 FAIL, and four live-evidence UNKNOWN gates.
The synthetic positive conformance fixture does not replace the stale committed receipt.
The retained Python reference, historical dataset/specification files, and main's
published v4.1 behavior were preserved.

After the public CLI cutover, all 2,736 non-live Python tests passed, together with
Ruff lint/format on 383 files, strict mypy on 75 files, and the exact Pyright reference
baseline. The post-replacement RustSec check used cargo-audit 0.23.0 against database
revision `233155b4025b854c68a4b199c0e32d7fd53291a2`: 232 dependencies, zero
vulnerabilities, and zero warnings. The final lockfile SHA-256 is
`a820b07fd7bad232e3913de6842d751a4d885969fbb0fe17a29a63d62a94b6f5`;
the repeated audit at that exact lockfile has the same clean result. The independently verified [dependency audit binding](dependency-audit.json) records tool version, database revision, before/after lockfile hashes and the audit-output digest.
Targeted executable verification passed all 14 tests across native conformance command lifecycle, output preflight, evidence-preserving resume, run dispatch and standalone installation. All 18 CLI unit tests also passed. The fake provider uses explicit blocking accepted sockets for consistent macOS/Linux behavior; all request counts and billed-cost assertions remain exact.

Gitleaks 8.30.1 scanned 298 files, including all 226 native crate files and the added
audit artifacts. It returned **507 raw generic-key findings, all independently triaged
as nonsecrets; zero actionable findings**. The raw scanner exit remains 1. Of those,
504 are canonical metric SHA-256 fixture values, one is a verified compiled-source
hash, and two are intentional dummy-credential test fixtures. No repository allowlist
or suppression was added. The [secret-scan record](secret-scan.json) preserves the raw
result digest, exact classifications, file hashes, and supplemental scans for final
changed files.

## Push verification

The shared local pre-push hook invokes raw Pyright and a retired test path. This branch uses an invocation-scoped hook with Ruff lint/format, the exact frozen-reference Pyright baseline, strict mypy, and the current v4.1 experiment-config tests. The shared hook is unchanged; verification is not disabled. Hosted CI must pass at the published head before closeout; its exact commit, check conclusions, and links are recorded in the PR rather than self-referentially changing this audit commit.
