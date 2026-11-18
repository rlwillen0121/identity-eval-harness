# Proposed Carbon revision-review coverage plan

Status: PREPARATION ONLY. No packet calls authorized by this file. Use only if the active monolithic review ends without a valid terminal review and after the new candidate is READY/frozen. Preserve the same advertised Carbon-high model, high effort, priority request, configured connection, and documented streaming transport. This is review of the methodology revision, not a fresh full-spec, implementation, G-SPEC, or milestone audit.

## Immutable inputs and mechanical coverage

1. Snapshot the final candidate after writer READY; independently verify its SHA-256. Snapshot the fixed base, approved Addendum/Directive, and the exact full unified base-to-candidate diff. No local configuration, credentials, opaque provider state, or other reviewers' opinions enter inputs.
2. Split each changed line/hunk by normative section. Every addition, replacement, and deletion must have at least one packet owner in a machine-readable ledger. Pure historical review appendices remain accounted for as removed/superseded historical text. No hunk is silently dropped as irrelevant.
3. Each packet states the same full-candidate hash, exact source paths and line ranges, excerpt hashes, scope/limitations, source precedence, and neighboring unchanged context sufficient to interpret the changed clause. Removed clauses have exact base excerpts. Repeated context is explicit.
4. Mechanical diff evidence identifies unchanged IGA sections; it does not prove those sections correct. Any changed IGA semantic clause is supplied in full with its controlling approved source and old text.

## Packet allocation

| Packet | Owned changed clauses | Required context and task |
| :--- | :--- | :--- |
| A: execution and provider fairness | Header effort/execution/resource rows; source precedence for replaced execution rules; new evaluation/provenance schema classes in §2.2; §3.3 complete-policy rendering; §4.2 configuration/effort/headroom, response dispatch/disposition, segment and recovery continuation, accounting; G-TOK/G-ECON/G-TURN and effort disclosure | Full relevant tool allowlist/receipts and state-transition contract; per-agent T0/T5 profile boundaries; approved co-sign lifecycle excerpt. Audit dynamic highest-valid numeric budget, complete evidence, canonical primary prompt/tool bundle, positional tool IDs, strict decoding, causal effects, terminal latch, null-vs-failure classification, per-physical-request/whole-logical-response usage, safe opaque continuation handling. |
| B: design, scoring, inference and hygiene | Header/core design; §0.2 precedents and domain review; §0.3 suites/seeds/lite; §4.1 metrics/category reporting; §4.2 cap-sensitivity/missingness/inference/replication/hypotheses; G-DET/G-QUERY/G-SPEC/G-BASE/G-IGA/G-CAL; constructive-witness and superseded-method appendices | Full exact estimands and relevant runtime termination/slot-observability definitions from A. Audit three-repeat pass@1, complete-denominator inference, partial bounds, multiplicity, 12-family target, nested seeds, all held-out exposure paths, same-trajectory prefixes, external precedent limits. |
| C: preserved IGA and normative migration boundary | Full changed normative portions of §§0.4–0.5 and §§1.1–1.6, plus any otherwise-unassigned schema/policy/fixture edits; exact removed/replaced clauses from fixed base; complete Addendum/Directive | Derive preservation from the exact before/after excerpts, including mandatory NOT_GOVERNED ledger rows, available-mint snapshot then permanent consumption, ETag preimages/serialization, first-stage observed-slot scope, source-credit/ledger/semantic-equivalence invariants and mandatory joint governance promotion. Read the full hunk ledger; report any uncovered or insufficiently contextualized change. Unchanged §§2.1/2.3/2.4/3.x are acknowledged only to the extent supported by mechanical diff, not independently signed off. |

Any hunk crossing packet boundaries is included in both and explicitly cross-referenced. The final mapping will be generated from the actual frozen candidate, not assumed from these draft section labels. If a packet remains too large, split along complete normative subsection boundaries and revise the mapping before dispatch.

## Independent reviewer reports and synthesis

Each packet receives neutral instructions and its own inputs only, without Opus/internal verdicts or proposed reviewer agreement. Require exact candidate hash acknowledgment, explicit coverage and limitations, P0/P1/P2 clauses/counterexamples/repairs, and a terminal complete visible report. Transport statuses/usage remain separately recorded; no reasoning trace is retained.

After every packet completes, send Carbon a bounded synthesis packet containing the machine coverage ledger, input hashes/ranges, its own packet findings, and any author repairs with before/after exact-hash excerpts. Do not send peer opinions. Require it to identify conflicts/coverage gaps and give a scoped revision verdict. A packet or synthesis failure remains unresolved; no whole-spec consensus is inferred from partial coverage. Opus independently reviews the same final full-candidate hash.

No new provider/model route, effort substitution, direct-provider call, benchmark trial, or source implementation is included. Existing failed monolithic attempts and partial usage remain in history.
