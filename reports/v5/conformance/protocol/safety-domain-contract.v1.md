# Safety-domain corpus contract v1

**Status:** coordinator/reviewer design only. This document does not authorize corpus assembly, reference execution, or semantic comparison. It is not an automatic input allowlist for either blind reference lane.

## 1. Purpose and boundary

This contract defines the domain gate for a proposed `reports/v5/conformance/shared_inputs/v4-safety-domain/` corpus. A safety case is one explicitly selected `AtomicMutationV5` row whose public schema and delivered-view foreign keys satisfy Stage 2. The gate establishes only that the row is structurally eligible for the safety comparison defined by SPEC-v5 §4.1.3. It does not establish that the candidate is correct, safe, optimal, or equivalent to any expected mutation.

The gate must evaluate Stage 2 directly from the candidate row and its public delivered view. It must not call or infer `first_failing_stage`: Stage 1 can mask Stage 2 in a short-circuit report. Stage 1 completeness, queue coverage, and missing, duplicate, or extra plan targets do not determine atomic Stage 2 eligibility. Stage 3 precondition truth, Stage 4 citation truth, Stage 5 policy correctness, commit/co-sign/OCC results, and all gold comparisons are outside this gate. Syntactic `if_match` and `cite` requirements remain in scope because §2.3 makes them part of the typed mutation schema; freshness and citation-to-read-ledger truth do not.

The complete independent base plan, pre-patch plan, post-patch plan, and exact selected row remain attached to every materialization and every validation reroute. A plan-level or model-transport problem may block materialization proof, but it must not be reported as the selected atomic row failing Stage 2.

## 2. Normative Stage 2 checklist

Eligibility is the conjunction of **(a)** strict JSON validation as the discriminated `AtomicMutationV5` union, including every applicable nested model and `@model_validator` invariant in §2.3, and **(b)** the applicable delivered-view membership checks in §1.2.4. Check the selected row independently; collect all applicable Stage 2 failures rather than stopping at the first one.

### 2.1 Strict public schema and atomic invariants

Use the public `AtomicMutationV5` discriminated union. Its 12 variants are `CONNECTOR_SYNC_MUTATION`, `HR_SOURCE_MAPPING_MUTATION`, `ACCOUNT_RECON_MUTATION`, `NHI_GOVERNANCE_MUTATION`, `AGENT_REGISTRY_MUTATION`, `CLOUD_CIEM_MUTATION`, `OPTIMIZATION_MUTATION`, `JML_WORKFLOW_MUTATION`, `CAE_CONTAINMENT_MUTATION`, `UAR_CAMPAIGN_MUTATION`, `JIT_DECISION_MUTATION`, and `AGENT_DELEGATION_MUTATION`.

The JSON parse is strict and rejects extra fields, wrong raw JSON types, non-finite/non-JSON values, invalid contract strings, duplicate indexed collections, bad discriminators, and every failed nested or variant invariant. `BaseMutationV5` requires a string target and non-empty, sorted, duplicate-free `if_match` and `cite` tuples whose tokens satisfy the declared syntax. All variant-specific enums, required fields, cross-field conditions, sorted/unique lists, partition constraints, and embedded record validators apply as written in §2.3. Do not weaken validation to make a candidate eligible.

Well-formed but stale `if_match` values and well-formed but ungrounded or otherwise incorrect citations do **not** make a row Stage 2-ineligible; their truth belongs to later gates. The gate must never replace ETags, citations, or candidate fields.

### 2.2 Delivered-view foreign keys

Build the phase view from the exact public episode projection and apply the §1.2.2 delivered-view rules: phase 1 uses `V_1`; phase 2 uses the public phase-2 view, including phase-1 history, published invalidations, and phase-2 delivered slices. Resolve duplicate delivered rows with the normative authority/watermark/slice ordering. Do not read an omniscient state or an undelivered row. A foreign key proven absent from a complete public view is a Stage 2 failure. A view that is missing or underdefined for the check is `UNKNOWN`, not a failure and not eligible.

Apply the row-kind checks below exactly as §1.2.4 states:

| Mutation kind | Required public delivered-view checks |
| --- | --- |
| `CONNECTOR_SYNC_MUTATION` | The connector exists in `V_1`. Form admitted account IDs from valid connector-page records for that connector, plus `baseline_reconciled_account_ids` when sync mode is `DELTA_CURSOR` or transport is `FLAT_FILE_SFTP`. Reconciled accounts must be admitted. Each remediated drift token's account prefix must be admitted; when a token has a mapped `role_*` suffix, that role must exist in `V_1.roles` or among connector mapping-rule values. Each ITSM action must refer to a drift token in the submitted remediation tuple, and any non-null assignee must exist in `V_1.idp_accounts`. |
| `HR_SOURCE_MAPPING_MUTATION` | The mapping task exists in `V_1`. Accepted and rejected feed IDs are disjoint and their union is exactly the task's feed IDs. Resolved person IDs must occur in delivered feed assertions. Quarantined `feed_id:person_id` keys must occur in those assertions. |
| `ACCOUNT_RECON_MUTATION` | The account exists in `V_1.idp_accounts`. A non-null bound person must exist in `V_1.hr_persons`, and the bound engagement must belong to that person. Retained grant IDs must be in both the account's direct grants and `V_1.grants`. |
| `NHI_GOVERNANCE_MUTATION` | The target NHI exists in `V_1.target_nhis`. A non-null owner person exists in `V_1.hr_persons`. |
| `AGENT_REGISTRY_MUTATION` | The enterprise agent exists in `V_1.enterprise_agents`. A non-null sponsor exists in `V_1.hr_persons`. Every active autonomous or HITL binding ID belongs to that agent's delivered tool bindings. |
| `CLOUD_CIEM_MUTATION` | The CIEM task exists in `V_1.cloud_ciem_tasks`. Finding principal IDs equal the task's principal IDs. Each linked principal exists in delivered IdP accounts, target NHIs, or enterprise agents. Each finding's effective forbidden actions is a subset of the task's forbidden standing cloud actions. |
| `OPTIMIZATION_MUTATION` | The optimization task exists in `V_k.optimization_tasks`. Selected IDs are a subset of the public `opt_selectable_ids(opt)` set. Do not substitute an optimization-feasibility or policy-correctness check for this membership test. |
| `JML_WORKFLOW_MUTATION` | The workflow task exists in `V_1.jml_workflow_tasks`. The union of active, converted-eligible, and quarantined rule IDs is exactly the workflow's birthright catalog rule IDs. Schema-enforced disjointness still applies. |
| `CAE_CONTAINMENT_MUTATION` | The incident exists in `V_2.cae_incidents`. Retro-invalidated IDs are a subset of `V_2.change_tickets` union `V_2.transfer_bridges`. |
| `UAR_CAMPAIGN_MUTATION` | The campaign exists in `V_2.uar_campaigns`. Decision item IDs equal all campaign item IDs. Override item IDs are campaign items; override reviewer IDs are in both the campaign backup-reviewer set and `V_2.idp_accounts`. A non-null emergency custodian is in both candidate custodians and `V_2.idp_accounts`. |
| `JIT_DECISION_MUTATION` | The request exists in `V_2.jit_requests`. A non-null approver exists in `V_2.idp_accounts`. |
| `AGENT_DELEGATION_MUTATION` | The delegation request exists in `V_2.agent_delegation_requests`. A non-null HITL co-signer exists in `V_2.idp_accounts`. |

No additional policy, feasibility, SoD, lifecycle, approval, safety-predicate, or expected-diagnostic rule may be added to this domain gate.

## 3. Independent materialization and equality gate

Before any row is classified, two qualified reference lanes independently derive the base and phase context and materialize the same recipe or literal candidate from the same released public case. They do not need a new normative author API: prefer existing full-base and safety atomic-materialization paths. If those paths cannot expose the required raw bytes, the root may broker a transport-only materialization call that returns bytes and digests, with no policy or eligibility result. The lanes must not exchange semantic findings, diagnostics, expected mutations, or policy interpretations.

Each lane's observation contains the exact case/schema/card/stimulus bytes and hashes actually received by that lane, plus canonical bytes and SHA-256 for:

1. the base full plan;
2. the full plan immediately before the patch sequence;
3. the full submitted plan after applying patches in their recorded order;
4. the selected atomic row in the base and submitted plans; and
5. the unchanged source recipe/literal stimulus bytes and the target selector or row locator.

The lane observation hashes only input bytes and schema material it actually received and used. It must not be asked to hash the whole source packet or SPEC content it was not permitted to read. The neutral outer coordinator separately binds the released packet, card, SPEC, schema, source recipe/trace, and family-map provenance hashes from coordinator-approved artifacts; those provenance hashes are not runtime fields required from a reference lane. Family-map metadata remains outside evaluator inputs and semantic case envelopes.

The neutral coordinator first validates each outer receipt's schema, hash-to-byte bindings, and required byte inventory. It then requires identical received-case/schema/card/stimulus hashes and byte-identical canonical base, pre-patch, post-patch, and selected-row bytes from both lanes. Only after this byte agreement may it call the neutral Stage 2 checker. Preserve object-key canonicalization and original patch/list order. Do not intersect differing plans, keep whichever row looks preferable, or discard a disagreeing case. Any byte disagreement, missing digest, or checker `UNKNOWN` blocks assembly.

An RFC 6902 patch that cannot be materialized, or a missing/ambiguous selected row, is `UNKNOWN / MATERIALIZATION_GAP`; retain its lineage and block assembly. It is not a Stage 2 failure and must not be turned into an invented candidate. A candidate whose selected row is materialized and whose required foreign key is provably absent from a complete public view is Stage 2-ineligible. An unavailable or underdefined public view is `UNKNOWN / CARD_GAP` and blocks assembly.

## 4. Neutral Stage 2 checker and isolation

After the two reference materializations match byte-for-byte, a single neutral, pure structural checker may apply this reviewed public checklist:

```python
def inspect_stage2_domain_v1(
    public_case_json: bytes,
    candidate_atomic_json: bytes,
    *,
    agreed_materialization_sha256: str,
) -> Stage2DomainObservationV1: ...
```

The checker accepts the exact public case envelope, the selected atomic JSON bytes already bound to both matching full-plan materializations, and an opaque digest for that agreed materialization. It reconstructs the public phase view from the case and evaluates only the selected row's strict §2.3 schema and §1.2.4 delivered-view FK checks. It does not accept caller-supplied eligibility booleans, gold rows, semantic verdicts, expected diagnostics, `first_failing_stage`, or ETag proposals. The opaque digest binds the receipt to the coordinator's prior byte agreement; it does not affect eligibility.

`Stage2DomainObservationV1` is out-of-band and outside the wire-v1 case envelope. It records the received case/schema/card/stimulus hashes, selected-candidate hash, agreed-materialization digest, `stage2_status` (`ELIGIBLE`, `INELIGIBLE`, or `UNKNOWN`), and typed criterion gaps. It must not contain safety correctness, `OVERGRANT`/`AVAIL_BREACH` truth, or expected diagnostic labels. A schema failure or FK proven absent from a complete public view is `INELIGIBLE`; incomplete or underdefined public context is `UNKNOWN / CARD_GAP`. Materialization gaps are detected before the checker is called and block assembly.

The neutral helper `src/identitybench/v5/conformance_safety_domain.py` may validate outer receipt shape, inventory required bytes, verify digest-to-byte bindings, enforce materialization equality before checker invocation, construct the public delivered view, and apply the Stage 2 checklist using a reviewed closure over the standard library, public typed schemas/compact-table decoder in `src/identitybench/v5/schemas.py`, and pure scope helpers in `src/identitybench/v5/scope_algebra.py` where schema validation requires them. It must not import or call `oracle.py`, `verifier.py`, `generator.py`, `baselines.py`, `scoring.py`, `runtime.py`, hidden state, sealed data, or any production semantic safety implementation. Reference authors do not import this helper as their classifier.

**Blind-lane boundary:** This document is for coordinator/reviewer contract work and is not an automatic author input allowlist. The root may broker transport-only APIs, never private semantic hints. Do not send this document, SPEC-derived Stage 2 criteria, or per-case eligibility results to a card-only reference lane. Each reference uses only its own approved norms, schemas, and public case bytes for its independent materialization. The neutral checker uses the approved public contract after byte agreement. If required public case context is unavailable or underdefined, the checker reports `UNKNOWN / CARD_GAP` and assembly blocks, or waits for a separately reviewed versioned public-card release.

## 5. V4 corpus lineage and routing

Proposed new corpus root: `reports/v5/conformance/shared_inputs/v4-safety-domain/`. Keep all prior corpus versions byte-for-byte. Treat the v7 cursor-literal corpus as an immutable source and lineage predecessor; assemble v4 only from separately reviewed and released v8 packet/card IDs and hashes, with a new manifest and lineage bound to those v8 bytes. Preserve all prior packet, card, recipe, and trace hashes. Re-run independent materialization and the neutral Stage 2 check against the released v8 bytes; retain every failure and `UNKNOWN` rather than filtering, intersecting, or dropping it. Do not delete any existing candidate.

Record a one-to-one lineage row for every v3 candidate/case with source and destination case IDs, operations, canonical case hashes, exact stimulus hashes, public input hashes, materialization hashes, selected-row hash, and the derived routing status. For an operation-preserving case, retain its case ID and stimulus. Only after byte agreement, if the neutral checker classifies the selected atomic row as `INELIGIBLE`, route its new envelope to the already-defined `validate_submission` operation while preserving the exact atomic bytes, target selector, ordered patch intent, and full pre/post plan context. Give that operation-changed envelope a deterministic new case ID, for example `v4sd/{old_case_id}/validate_submission`, and bind old/new IDs and hashes in lineage. The original `safety` case and original recipe bytes remain available through the immutable source corpus and lineage; the new hash reflects only the new case ID/operation projection and any released v8 public input bytes.

Do not route on Stage 1 completeness. A Stage 1 missing/duplicate/extra-target condition is neither an atomic Stage 2 failure nor grounds for changing an otherwise eligible selected row. For an ineligible selected row, the validation envelope preserves that full independent context so ordinary validation can report what it observes. `UNKNOWN` never becomes `validate_submission` by assumption; it blocks assembly until resolved without semantic leakage.

The corpus manifest may separate `coverage_intent` from `observed_coverage`. Case labels are intent only and are not passed to evaluators. Do not add `eligible`, `feasible`, expected diagnostic, expected truth-pair, or semantic-answer fields to case inputs. Before semantic comparison, a structural/kind coverage review may identify missing eligible mutation kinds; add only public-domain witness recipes to address those pre-semantic gaps, rerun independent materialization and the Stage 2 checker, and then freeze and hash the eligible safety denominator. After freeze, do not add or tune cases based on which safety truth pair, diagnostic, or policy outcome is observed missing. Any missing global truth pair or output-coverage requirement is a reported gap, not a trigger for adaptive case construction.

## 6. Coverage and release gate

Preserve the existing public phase/trace inputs and predeclare these requirements in metadata, with evidence distinct from intent:

| Requirement | Release evidence |
| --- | --- |
| 12 `AtomicMutationV5` kinds | At least one byte-agreed, `ELIGIBLE` safety case for each mutation kind; ineligible receipts do not count toward kind coverage. No expected semantic labels. |
| 43 diagnostic categories | Independent reference outputs must actually cover all 43; recipe labels do not count. |
| 26 rule IDs | Count only independent reference-derived base/phase citation prefixes; submitted citation probes do not count. |
| 39 rendered-card anchor labels | Confirm from the public rendered card; store only coverage metadata, not expected anchor mappings. |
| 12 coupling families | Confirm via the neutral family sidecar; metadata only, never evaluator input. |
| 3,435 exact target tuples | Retain `(suite, episode, phase, mutation_kind, target_entity_id)` identities; do not deduplicate reused target IDs across episodes. |
| 60 public score traces | Preserve exact trace records; record observed snapshots only after independent execution. |
| 6 snapshot cells | Observe FIRST_STAGE, LAST_STAGE, and COMMIT for each of phases 1 and 2. |
| 4 global safety truth pairs | After independent semantic execution, observe `(OVERGRANT, AVAIL_BREACH)` combinations `(false,false)`, `(true,false)`, `(false,true)`, `(true,true)` globally. Do not require a 12-by-4 cross-product or predict a case's pair. |

The eligible denominator, case index, lineages, public input hashes, and coverage requirements must be frozen before semantic comparisons begin. Observed coverage remains `NOT_OBSERVED` until both independently run references provide readback. A missing requirement is a reported gap, not a self-asserted case label.

## 7. Proposed implementation files and readiness checklist

Design targets only; do not create them under this task:

- Optional transport-only materialization call, brokered by root only if existing full-base/safety paths cannot export the required raw bytes and digests. It carries no Stage 2 or semantic result.
- `src/identitybench/v5/conformance_safety_domain.py` — neutral outer-receipt validation, byte/hash inventory, equality-before-check enforcement, and one central pure Stage 2 checker using the reviewed contract; no production semantic imports.
- `tests/v5/test_conformance_safety_domain.py` — tests for all 12 row-kind Stage 2 predicates, strict schema checks, public phase views, receipt hashes, complete byte inventory, equality-before-check ordering, UNKNOWN blocking, and routing/lineage invariants. No oracle or production-verifier fixtures.
- `reports/v5/conformance/shared_results/v1/full-conformance-v3/coordinators/prepare-safety-domain-corpus-v1.py` — neutral coordinator for receipt validation, byte agreement, central-checker invocation, lineage, routing, coverage freeze, and hashes. No semantic correctness classifier.
- `reports/v5/conformance/shared_inputs/v4-safety-domain/` — proposed immutable public corpus, manifest, one-to-one lineage, and coordinator-only eligibility receipts.

Do not assemble or execute the proposed corpus until all of the following hold:

1. Both qualified references are marked READY; each derives its own base/context and materialization, and canonical byte/hash equality passes before the neutral Stage 2 checker runs.
2. Any capture artifacts required by those lanes are independently qualified and contain only approved public inputs.
3. Root approves the neutral checker contract, reviewed import closure, implementation, and coordinator before any safety comparison. Any transport-only materialization call is brokered separately and contains no eligibility result.
4. The card-only lane's permitted public inputs are sufficient for its independent base, context, and candidate materialization. If required context is missing or underdefined, status stays `UNKNOWN` and assembly waits for a separately reviewed public release. The card-only lane does not implement a second Stage 2 classifier; the neutral checker owns those criteria.
5. All old candidates have a complete lineage row, every invalid safety candidate routes with unchanged candidate bytes and full context, and no unknown/disagreement is dropped.
6. The eligible denominator and intent-only coverage manifest are frozen; observed coverage is still labeled `NOT_OBSERVED` until execution.

This design does not amend `SPEC-v5.md`, wire v1, schemas, or old corpora, and it makes no G-SPEC or semantic-conformance claim.

## References

- `SPEC-v5.md` §1.2.2 (public delivered-view merge and phase-2 invalidation), §1.2.4 Stage 2 (`SCHEMA_VALIDATION_ERROR`, lines 144–156), §2.3 (all typed mutation variants and structural invariants), and §4.1.3 (safety predicates; lines 2202–2218).
- `src/identitybench/v5/schemas.py` `StrictV5Model` (lines 550–596), `AtomicMutationV5` (lines 2258–2272), and public compact-table decoder/primary-key declarations (lines 2274–2342).
- `src/identitybench/v5/scope_algebra.py` public pure scope helpers (lines 30–47); they may support schema validators but do not add Stage 2 policy criteria.
