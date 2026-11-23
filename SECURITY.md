# Security policy

## Supported versions

The active **IdentityBench v5.2** native Rust workspace (`crates/`, `identitybench v52`) receives security fixes. Earlier benchmark generations (`v1–v4` at `legacy-v1-v4`, `v4.1` plus legacy Python `v5` at `legacy-v4.1`, native Rust `v5` at `legacy-v5`, and `v5.1` at `legacy-v5.1`) are retired and preserved under [`archive/`](archive/README.md) as unsupported archival artifacts. See [archive/reports/v5-python-draft5/rust-final-audit/DAYBREAK.md](archive/reports/v5-python-draft5/rust-final-audit/DAYBREAK.md) and [archive/reports/v5-python-draft5/security-audit-daybreak-blue-20261115.md](archive/reports/v5-python-draft5/security-audit-daybreak-blue-20261115.md) for the defensive audit remediations carried into the shared provider adapters.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository: open the Security tab,
select Advisories, and choose Report a vulnerability. Include the affected version,
reproduction steps, expected impact, and a minimal example with secrets removed.
Do not put credentials or exploit details in a public issue.

## Threat model

| Boundary | Risk and handling |
| --- | --- |
| Adversarial model output | Treat returned text and tool arguments as untrusted. Parsing, validation, and execution restrictions are separate controls; model cooperation is not a security boundary. |
| Untrusted endpoints | Endpoints receive prompts and the credentials configured for them. Vendor host overrides require explicit `extra.allow_custom_endpoint: true`, recorded in endpoint identity. Explicit compatible endpoints remain an operator trust decision. |
| Public run artifacts and reports | Assume these files will be published. Inspect them for sensitive content before publication; diagnostic redaction is not a guarantee that all model output is safe to disclose. |
| Dataset integrity | Sealed datasets have digest checks, and run manifests pin exact dataset and source digests. A digest is useful only when its expected value is trusted. |
| Secrets and local paths | Keep credentials and private endpoints in local environment configuration. Use machine-neutral committed configs; tracked-text hygiene checks catch known private-path and endpoint patterns, not every possible disclosure. |

## Known and accepted risks

- The native-tool probe can detect observed events and file side effects. A passing receipt
  cannot establish that a vendor disabled every read or search tool. In particular, Cursor's
  [Ask mode](https://cursor.com/docs/cli/using) supports codebase search.
- The transform evaluator limits recursion depth. Referenced expressions and replacement text
  can still amplify CPU or memory consumption. An evaluator-wide budget would need a defined
  outcome for a tool call that exceeds it, since tool results enter subsequent model requests.
- Some subscription-CLI providers pass prompt text in process arguments, visible to
  other local users via the process table. Run on a single-user host. For example,
  Cursor passes the prompt as an argument, and Claude passes its system prompt this way.
- **Native Rust provider adapter bounds (`crates/ib-providers`):**
  HTTP clients refuse redirects and enforce an incremental `20 MiB` response body cap, CLI
  adapters cap auxiliary output files at `16 MiB` (`64 KiB` per stream on version probes) and
  retain bounded stdout/stderr on timeouts with process-group cleanup.
- **v5.2 dataset loading:** Both `identitybench v52 validate` and `identitybench v52 run` check
  the episode, oracle and specification digests against the supplied seal. The operator must
  establish trust in the seal and control the dataset and output directories.
- `endpoint_identity` contains a truncated, unsalted hash of the base URL and other
  endpoint settings. It is not encryption and does not prevent guessing a known URL;
  an explicit custom-endpoint opt-in also adds a readable identity prefix.
- v5.2 run artifacts are not cryptographically signed. `identitybench v52 report`
  recomputes summaries from the recorded per-slot scores in `episodes.jsonl`; it checks
  consistency of the recorded results, not the authenticity of provider output.
  Retired v5 artifacts under `archive/` were verified by raw-trajectory regrading at
  `legacy-v5`.
