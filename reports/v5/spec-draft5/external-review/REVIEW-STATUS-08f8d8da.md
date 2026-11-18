# Specification revision review checkpoint

**Candidate SHA-256:** `08f8d8da64dffb3b1b5a5bb549348b9d48f2eccb192abe64fb3062a3c62ff104`. The protected active `SPEC-v5.md` remains SHA-256 `6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43`; the candidate has not been promoted.

**Opus:** `PASS WITH NONBLOCKING NOTES`, with **0 unresolved P0 / P1 / P2**, on this exact candidate. The completed response returned `claude-opus-5-5-high`, effort `high`, tier `priority`; status completed, no incomplete details/error. [Full scoped closure](round4-claude-opus-5-5-high-08f8d8da-review.md) and [safe response provenance](round4-claude-opus-5-5-high-08f8d8da-status.json) preserve the report and its limits. The full candidate plus exact prior-to-current diff were supplied; unchanged normative sources rely on its own earlier full reads. Optional notes were not silently adopted after acceptance.

**Gemini Carbon:** no valid review verdict was obtained. Initial nonstreaming attempts produced no review text; one captured chat response ended at max_tokens with empty visible content. A full streaming attempt and three independent revision packets reached their hard overall bounds, with insufficient old-capture telemetry to identify the cause. Those historical requests remain bound to their original candidate hashes.

The final small **actual revision-clause review** used the independently corrected capture helper, exact requested `gemini-4-carbon-high` / `high` / `priority`, streaming, and the configured connection. It opened HTTP 200 with `text/event-stream`, identity encoding. At the last safe checkpoint it recorded 240 bytes, 60 raw lines, 30 comment heartbeats, **zero data events and zero visible review characters**, without DONE or a terminal event. The client stopped at its 600-second hard overall deadline. Response model, effective effort/tier, usage and upstream cause remain unavailable; provider-side cancellation is unconfirmed. [Safe outcome](carbon-small-policy-v2-08f8d8da-outcome.json), [request/status](carbon-small-policy-v2-08f8d8da-status.json), and [safe progress](carbon-small-policy-v2-08f8d8da-progress.json) contain the evidence. This distinguishes an open comment-only stream from speculative gzip/JSON parser failures; it does not prove what the upstream model was doing.

**Two-model consensus is not achieved.** No Carbon synthesis is available, and no failed, partial, empty or transport-only response counts as reviewer agreement. Additional review inference stopped after that bounded call. A routing/transport/operator resolution is needed before another Carbon request; no provider/model/effort fallback was used.

## Proof and scope

Internal read-only prose checks closed the concrete findings, including the final three Opus repairs. The helper passed independent review and 29 offline synthetic fixtures, AST parsing and focused Ruff; its authenticated dispatcher is private and excluded from the safe checkpoint. These checks do not establish source health, implementation correctness, dataset seals, empirical difficulty/calibration, G-SPEC agreement, milestone completion, publication readiness, or live benchmark results. No live IdentityBench evaluation was run.

Known raw provider token totals from the four completed Opus requests sum to 1,012,710. Two captured historical Carbon responses report 246,018 total tokens. Thus **1,258,728 reported tokens are known**, with additional usage unavailable for other attempts. These are review-only raw response totals, not normalized benchmark accounting or a complete billing total; unknown usage is never treated as zero.

## Checkpoint boundary

The final manifest lists safe evidence paths/hashes and separately lists retained private runtime/auth-loading dispatchers, caches and builders excluded from staging. No review-lane staging, commit, push, source migration, active-spec replacement, or publication occurred. Root/owner must verify hashes before the authorized local checkpoint.
