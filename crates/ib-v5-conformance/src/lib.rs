//! IdentityBench v5 shared conformance transport, safety-domain Stage 2 gate,
//! bounded process verification, and production-side shared evaluator.
//!
//! Mirrors `identitybench.v5.{conformance_shared,conformance_safety_domain,conformance_process,conformance_production}`.

pub mod process;
pub mod production;
pub mod safety_domain;
pub mod shared;

pub use process::{SharedRuntimeError, VerifiedBlindFreeze, verify_blind_freeze_receipt};
pub use production::{
    METRIC_KEYS, ProductionEvalError, PublicPhase1RuntimeProjectionV5,
    PublicPhaseTransitionProjectionV5, apply_recipe, apply_rfc6902_patch, evaluate_frozen_case,
    evaluate_public_case, evaluate_shared_input, evaluate_shared_input_batch, g_prop_metric,
    json_pointer, materialize_candidate_source, materialized_base_rule_prefixes, phase_stage_args,
    phase2_context_from_induced, pointer_parts, public_transition_projection, read_ledger,
    score_event_plan_materialization, strict_json_validate,
};
pub use safety_domain::{
    BoundBytesV1, DeliveredViewsV1, MaterializationAgreementV1, MaterializationGapError,
    ReferenceMaterializationReceiptV1, Stage2CriterionGapV1, Stage2DomainObservationV1,
    agree_reference_materializations_v1, build_delivered_views_v1, inspect_agreed_stage2_domain_v1,
    inspect_stage2_domain_v1,
};
pub use shared::{
    ConformanceProtocolError, Coverage, CoverageRequirement, EVALUATOR_WIRE_ID_BY_SLOT,
    EvaluatorClosure, EvaluatorReceipt, EvaluatorResult, FROZEN_SPEC_SHA256, FrozenCase,
    FrozenClosure, FrozenRun, Gap, HISTORICAL_PINNED_PUBLIC_SCHEMA_SHA256,
    MATERIALIZATION_CONTRACT_ID, PINNED_PUBLIC_SCHEMA_SHA256, PROTOCOL, PairedComparison,
    PublicCase, SCORE_METRIC_NULLABILITY_CONTRACT_ID, SourceFile, TargetSelector, bind_receipt,
    canonical_json_bytes, compare_production_result, compare_result_pair, freeze_case, freeze_run,
    freeze_source_closure, json_diff_paths, load_family_map, materialization_hash,
    serialize_recipe_case, sha256_bytes, sha256_json, strict_json_loads, strict_jsonl_loads,
    validate_case_bijection, validate_evaluator_result, validate_public_case, verify_case_index,
    verify_complete_coverage, verify_frozen_run,
};
