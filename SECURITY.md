# Security policy

## Supported versions

Both the published frozen **IdentityBench v4.1** Python harness (`src/identitybench/v41/`, `src/identitybench/providers/`) and the active **IdentityBench v5** native Rust workspace (`crates/`) receive security fixes. Earlier benchmark generations (v1–v4) are archived at `legacy-v1-v4` for reproducibility and are unsupported. See [reports/v5/rust-final-audit/DAYBREAK.md](reports/v5/rust-final-audit/DAYBREAK.md) and [reports/v5/security-audit-daybreak-blue-20261115.md](reports/v5/security-audit-daybreak-blue-20261115.md) for recent defensive audit remediations.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository: open the Security tab,
select Advisories, and choose Report a vulnerability. Include the affected version,
reproduction steps, expected impact, and a minimal example with secrets removed.
Do not put credentials or exploit details in a public issue.

## Threat model

| Boundary | Risk and handling |
| --- | --- |
| Adversarial model output | Treat returned text, tool arguments, and generated connector code as untrusted. Parsing, validation, and execution restrictions are separate controls; model cooperation is not a security boundary. |
| Untrusted endpoints | Endpoints receive prompts and the credentials configured for them. Vendor host overrides require explicit `extra.allow_custom_endpoint: true`, recorded in endpoint identity. Explicit compatible endpoints remain an operator trust decision. |
| Public run artifacts and reports | Assume these files will be published. Inspect them for sensitive content before publication; diagnostic redaction is not a guarantee that all model output is safe to disclose. |
| Dataset integrity | Sealed datasets have digest checks, and experiment digest pins detect mismatches. A digest is useful only when its expected value is trusted. |
| Secrets and local paths | Keep credentials and private endpoints in local environment configuration. Use machine-neutral committed configs; the tracked-text hygiene check catches known private-path and endpoint patterns, not every possible disclosure. |

## Known and accepted risks

- Some subscription-CLI providers pass prompt text in process arguments, visible to
  other local users via the process table. Run on a single-user host. For example,
  Cursor passes the prompt as an argument, and Claude passes its system prompt this way.
- **Python v4.1 vs. native Rust v5 HTTP response limits:** In the Python v4.1 HTTP
  adapters, response bodies from endpoints are buffered without a byte cap, so an
  untrusted endpoint can consume excessive memory before response validation runs.
  In the native Rust v5 adapters (`crates/ib-providers`), HTTP clients refuse redirects
  and enforce an incremental `20 MiB` response body cap, CLI adapters cap auxiliary
  output files at `16 MiB` (`64 KiB` per stream on version probes) and retain bounded
  stdout/stderr on timeouts, and the native dataset loader caps reads at `512 MiB` across
  the three sealed files with Unix regular-file descriptor checks (directory ownership and
  ancestor directory permissions remain operator responsibilities).
- `endpoint_identity` contains a truncated, unsalted hash of the base URL and other
  endpoint settings. It is not encryption and does not prevent guessing a known URL;
  an explicit custom-endpoint opt-in also adds a readable identity prefix.
- Run artifacts are verified by trace replay / raw-trajectory regrading and digest pins,
  not cryptographically signed. Verification checks consistency with recorded actions,
  not the authenticity of provider output; older artifacts may lack enough context to
  verify.
- Native Rust `v5 conformance` executes operator-supplied evaluator binaries in a clean
  child environment with read-only staged artifacts, but without OS-level network
  sandboxing; do not run untrusted evaluator executables.
