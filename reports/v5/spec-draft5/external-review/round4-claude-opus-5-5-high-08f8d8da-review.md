# IdentityBench v5 Methodology Revision Review, Round 4 (focused exact-hash closure)

## Packet identity

- **Candidate:** SHA-256 `08f8d8da64dffb3b1b5a5bb549348b9d48f2eccb192abe64fb3062a3c62ff104`, as supplied. The dispatcher states it verified the full bytes. I did not compute it.
- **Status string:** `Draft 5.0-draft.5 — REVIEW_PENDING`.
- **Packet contents:**
  - the complete current candidate;
  - a mechanically verified unified diff from `c7410b68…` (my round-3 hash) to `08f8d8da…`;
  - unchanged-source provenance for `SPEC-v5.md` (`6bc0adfa…6e43`), Addendum 01 (`bf0cb575…`) and Directive 01 (`d3b01715…`). These were reviewed in my rounds 2–3; this is not a new full-base read.
  - my round-3 report;
  - the author's three proposed dispositions, which I treated as claims, not acceptance.
- **No peer, Gemini or internal opinions** were supplied or used.

## Scope, covered hunks, limits

- **Hunk IDs.** No hunk IDs were supplied. I label the six diff hunks by their unified-diff headers and covered all six:
  - **H1** `@@ -39,11 +39,11 @@`: §0.2 precedent item 2.
  - **H2** `@@ -1684,10 +1684,12 @@`: `ProviderRequestUsageV5` gains `provider_http_status` and `provider_error_code`.
  - **H3** `@@ -2443,19 +2445,23 @@`: §4.2 item 1 (preflight and freeze order) and item 2 (base output ceiling).
  - **H4** `@@ -2463,11 +2469,11 @@`: §4.2 item 3 (request-level block adds no generation).
  - **H5** `@@ -2483,15 +2489,17 @@`: §4.2 item 5 (request-level policy rejection paragraph; post-freeze incompatibility).
  - **H6** `@@ -2514,19 +2522,19 @@`: §4.3 `G-TOK-01`, `G-TURN-01`, `G-CAL-01`.
- **Cross-references checked:**
  - §0.4;
  - §2.2 schemas: `RunConfigurationV5` validator, `ProviderRequestUsageV5`, `EpisodeAttemptV5`;
  - §4.2 items 3–7 dispatch, cap and inference rules;
  - §4.3.
- **Limits:**
  - Unchanged regions rely on my prior rounds.
  - External links are not verified.
  - This is a specification/methods review only. It is not an implementation audit, unchanged-whole-spec qualification, empirical difficulty evidence, G-SPEC evidence, or milestone or publication proof.

## Verdict: PASS WITH NONBLOCKING NOTES

### Round-3 P2 verification

| Round-3 finding | Status |
| :--- | :--- |
| **P2-1** Request-level policy/safety rejection | **Resolved (H2, H4, H5, H6).** A documented content-policy rejection under the preregistered provider map becomes an observed `WITHIN_PROTOCOL_REFUSAL` with `G-SRR=0`. It carries a separate cause, `PROVIDER_REQUEST_POLICY_REJECTION`, distinct from `STRUCTURED_ASSISTANT_REFUSAL`. It is terminal and never retried or recovered, so the null-slot recovery loop is removed. It records `NO_RESPONSE` and adds zero generations, which is consistent with item 3's counting rule and `G-TURN-01`. Account/auth/quota/transport/schema/framing errors keep their infrastructure classes. HTTP-only, prose, repeated-failure and missing-output heuristics are excluded, which removes the operator discretion. The new schema fields support the evidence requirement. |
| **P2-2** Preflight vs. family freeze | **Resolved (H3, H5, H6).** The order is now explicit: outcome-blind all-split preflight → capability-only exclusions disclosed → eligible set and dev-cap plan frozen → dev sensitivity → common cap → final configurations and families frozen before held-out execution. The optional "if retained" wording is gone. Post-freeze incompatibility stays in its original family as incomplete and cannot be dropped or replaced. Preflight is defined as mechanical, with no generation and no tuning leak. Count-endpoint retention is governed by declared policy. |
| **P2-3** Witness-sized base ceiling | **Resolved (H1, H3, H6).** The base is the largest valid provider/transport **total** logical-output setting, independent of witness length. The witness is used only as a per-request feasibility check: `min(base, provider_max, context − input − overhead − headroom) ≥ effort_min`. Early responses keep their full allowance, and later ones shrink deterministically under the existing per-response formula. The segment maximum is explicitly not the ceiling. §0.2 and `G-TOK-01` are aligned with this. |

### Regression check

I found no introduced regressions.

- The `MAXIMUM_VALID_NATIVE_BUDGET` upper-cap policy is still described as preregistered and not operator-adjustable, with no equal-compute claim.
- Ordinal latching and the "earliest terminal outcome is final" rule interact correctly with the new terminal refusal. An unresolved lower ordinal still blocks.
- Addendum 01 and Directive 01, the IGA, domain and legacy contracts, the source-credit vs. provider-resource distinction, and the no-oracle boundary are untouched.
- No token, dollar or model-band gate is reintroduced.
- Complete required evidence is still required, and silent effort or provider substitution is still prohibited.

## Nonblocking notes (clarity/optional, not substantive defects)

1. **"Deterministic" in H5.** "provider map for deterministic … content-policy rejection" could be read as requiring the provider's classifier to be deterministic. Under that reading, typical classifier blocks would fall back to "unexplained rejection" and recoverable incompleteness.
   - **Repair:** say the *classification* is deterministic from the documented status and error code, whether or not the provider's filter itself is deterministic.
2. **Semantics of `provider_max_output_tokens` (§2.2 / item 2).** This field is used in the validator and in the allowance formula. If it is filled with a per-segment maximum, it silently caps the base at the segment boundary, which the new text forbids.
   - **Repair:** define it as the provider's total logical-output maximum.
   - **Optional:** validate `base ≤ transport_max_logical_output_tokens` when that field is non-null.
3. **Reopening the freeze sequence (H3).** "No post-preflight dev change … may bypass renewed outcome-blind preflight" does not state that a change made after the eligibility freeze also voids and reruns the dev-cap plan and cap selection.
   - **Repair:** state that the whole sequence reopens.
   - **Also:** state whether final family membership must equal the frozen eligible set, or whether any dev-based removal is disclosed.
4. **Term mismatch.** Item 7 says "before held-out exposure", while item 1 says "before any held-out model execution".
   - **Repair:** harmonize on the latter, since preflight renders held-out inputs.
5. **Provenance fields.** Name where the request-policy map version and hash live, for example inside `recovery_policy_json`. Optionally make `terminal_cause` a `Literal`.
6. **Carried-over round-3 notes, still open:**
   - the §0.4 "§4.1 resource-qualified score variants" mis-citation;
   - no restatement for the internal ledger that it does not pre-apply retro-bridge stripping;
   - the §0.5 vs. base wording on changes during review;
   - `native_numeric_budget_min_tokens ge=0` (no disable sentinel);
   - the role of the fixed-depth feasibility bound;
   - kind-to-index validators;
   - `G-IGA-REVIEW-01` covering `v5_full`;
   - zero-retention terms and canary strings.

## Summary

- **Unresolved severity counts:** P0 = 0, P1 = 0, P2 = 0.
- **Exact-hash recheck:** Not required for `08f8d8da…`. Any further edit, including adopting the optional notes above, produces a new hash and warrants at least a diff-scoped exact-hash recheck.
