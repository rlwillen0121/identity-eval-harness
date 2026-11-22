# Public structural clarification for the v5 shared-reference wire contract

**Status:** Public structural clarification. This supplement applies to
`identitybench.v5.shared-conformance.v1`, the existing [author wire
contract](author-wire-contract.v1.md), and its [auxiliary
supplement](author-wire-contract-auxiliary-supplement.v1.md). It adds no
protocol version, case field, payload field, metric, proof dimension, or
semantic expected value. The case shape and the existing
`identitybench.v5.materialization-contract.v2` and
`identitybench.v5.score-metric-nullability.v1` contracts remain unchanged.

This text clarifies wire structure only. It does not use production oracle,
generator, verifier, baseline, or reference implementation behavior, another
reference's output, comparison results, or run results. It does not provide a
private ETag or resolve first-stage metric eligibility, including
`G-SRR@first_stage`.

## Result and gap structure

Every result retains the existing exact result-envelope keys. `payload` is a
JSON object and `gaps` is an array of objects, each with exactly
`code`, `operation`, `case_id`, `path`, and `detail`. An `ok` result has an
empty `gaps` array. An `unsupported` or `underdefined` result has at least one
gap. `code` remains one of `UNSUPPORTED_OPERATION`,
`UNDERDEFINED_PUBLIC_INPUT`, `UNMATERIALIZABLE_INPUT`, `PATCH_PATH_ABSENT`, or
`OTHER`; `path` is an RFC 6901 JSON Pointer (the empty pointer is allowed),
`detail` is text, and the gap binds to the current operation and case.

Status does not relax an operation's payload, coverage, or materialization
shape. Do not replace any of them with a gap object, string, omission, or
generic null fallback. Preserve null only where the existing operation
contract specifies it: for example, `derive_phase` materialization and literal
or raw `validate_submission` and `safety` materialization are null; a non-ok
`score_trace` may use null only for an uncomputed metric, while retaining all
11 metric keys and an explicit gap. Literal score-trace rows retain their
`event_plans` row with a null `base_plan`. Snapshot rows remain non-null.

The exact payload keys remain operation-specific:

| Operation | Exact payload keys |
| --- | --- |
| `derive_phase` | `mutations`, `policy_card_anchor_map` |
| `validate_submission` | `first_failing_stage`, `diagnostics` |
| `safety` | `overgrant`, `availability_breach` |
| `score_trace` | `metrics`, `snapshot_derivations` |

`derive_phase.mutations` remains an array of unique full public
`AtomicMutationV5` objects sorted by `(mutation_kind, target_entity_id)`; its
anchor map has the existing 26 rule labels and 13 `BOUNDARY:` labels with
string-array values. `validate_submission` diagnostics remain sorted unique
strings and `first_failing_stage` remains null or an integer from 1 through 5.
The two `safety` values remain booleans.

The `score_trace.metrics` object retains exactly the 11 keys in the main wire
contract. Each score snapshot derivation retains exactly these 14 keys, with
no `case_id`: `phase_index`, `boundary`, `event_seq`, `mutations`,
`mutation_sha256`, `read_ledger`, `delivered_slice_ids`, `stage1_ok`,
`stage2_ok`, `stage3_ok`, `stage4_ok`, `stage5_ok`, `diagnostics`, and
`token_usage`.

## Operation-scoped coverage

Coverage remains exactly `phase_targets`, `diagnostics`, `rules`,
`safety_cases`, and `snapshots`. Empty arrays are required where an operation
does not own that coverage:

| Operation | Required coverage |
| --- | --- |
| `derive_phase` | `phase_targets` has one row per payload mutation, with the enclosing suite, episode, and phase identity; `diagnostics`, `safety_cases`, and `snapshots` are empty. |
| `validate_submission` | `diagnostics` equals `payload.diagnostics`; `phase_targets`, `safety_cases`, and `snapshots` are empty. |
| `safety` | `safety_cases` has exactly one row with `case_id`, selector `mutation_kind`, `overgrant`, and `availability_breach`; `phase_targets`, `diagnostics`, and `snapshots` are empty. |
| `score_trace` | `snapshots` projects the payload snapshot identities and adds `case_id`; `phase_targets`, `diagnostics`, and `safety_cases` are empty. |

Rule coverage records cited rule prefixes from evaluator-derived mutations or
base plans only. Submitted-candidate citations do not count, and the anchor
map does not replace rule coverage.

## Materialization shapes

`derive_phase` materialization is null. For `validate_submission` and
`safety`, literal or raw submissions also have null materialization.

For a recipe, `validate_submission` materialization is exactly
`{base_plan, submitted_plan}`, with both values full plan arrays, including
for `target_mutation` scope. For a recipe, `safety` materialization has the
same two keys but each value is one selected mutation object, not a plan
array. Base rows or objects are evaluator-derived and checked against the
pinned public schema. Submitted values preserve the raw candidate and are
not normalized.

`score_trace` materialization is exactly `{event_plans}`. It has one row for
every public `stage_phase_mutations` request, in request order, including
unsuccessful requests. This is a bijection between those requests and
`event_plans`; each row has exactly `seq`, `phase_index`, `base_plan`, and
`submitted_plan`. A literal row has null `base_plan` and the exact public
literal as `submitted_plan`. A recipe row has full derived plan arrays for
both fields. With no stage requests, `event_plans` is empty.

## Score-trace phase origin and snapshot order

Every `score_trace` replays a new episode beginning in phase 1, even when the
case envelope has `phase_index` 2. An event argument's `phase_index` requests
a phase; it does not advance the environment. Phase 2 opens through a
successful phase 1 commit. In `ORACLE_PHASE_RESET` mode, the environment
resets after that commit. A phase-2-only continuation would require a new
public initial-state, provenance, and metric contract; it is not inferred by
this protocol.

Within a score result, `snapshot_derivations` are sorted by
`(phase_index, boundary, event_seq)`. `coverage.snapshots` carries those same
identities with `case_id` and follows its existing identity sort. Snapshot
ordering does not select or resolve any score-eligibility rule.

## Unrepresentable required values and prior results

The public contract approves no representation for an unavailable required
output or an unavailable/unmaterializable recipe. Record the operation and
public input path in the author's ambiguity note; do not fabricate a value,
including a placeholder, omission, or fallback. This note does not authorize
changing the contract or guessing an answer.

This clarification does not alter, re-score, or retroactively certify prior
results. Existing recorded results remain as recorded; this text makes no new
passing claim.
