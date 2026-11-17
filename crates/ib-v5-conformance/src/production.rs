//! Production-side v5 evaluator for the semantics-free shared protocol.
//!
//! Mirrors `identitybench.v5.conformance_production`.
//!
//! This module consumes only the public case supplied by the outer conformance
//! driver. Source closure and dataset-seal verification belong to that driver; the
//! evaluator never opens blind-reference files or reads sealed oracle artifacts.

use std::collections::{BTreeMap, BTreeSet};

use ib_core::pyjson::{PyNumber, classify_number};
use ib_v5_oracle::{
    GoldTargetTraceV5, OracleError, PhaseGoldResultV5, evaluate_phase_with_trace,
    phase2_feasibility,
};
use ib_v5_runtime::policy_card::RULE_TO_POLICY_CARD_ANCHORS_V5;
use ib_v5_runtime::{
    CoSignTuple, EvaluatePhaseOptions, GoldTraceMap, RuntimeError, RuntimeOptionsV5,
    RuntimeSnapshotV5, RuntimeV5, VerifierContextError, ViewV5, completeness_ok,
    cosign_requirements, evaluate_phase as verify_phase, induce_phase2_world, live_version,
    required_mutation_keys, safety_partition, stage_digests, structural_errors, structural_ok,
    violation_category_as_str,
};
use ib_v5_schemas::{
    AtomicMutationV5, CoSignScopeKindV5, DeliveredSliceV5, EpisodeV5, PhaseManifestV5,
    PhaseWorkQueueV5, PolicyCardV5, STAGE_ID_RE, SchemaValidationError, StageReceiptStatusV5,
    StrictV5Model, SuiteIdV5, WorldStateV5,
};
use ib_v5_scoring::{
    PhaseSnapshotSetV5, ScoringError, SnapshotAssessmentV5, gold_trace_entry_from_oracle_trace,
    snapshot_assessment_from_verification,
};
use indexmap::IndexMap;
use serde_json::{Map, Number, Value};
use thiserror::Error;

use crate::shared::{
    ConformanceProtocolError, EvaluatorResult, FrozenCase, P1_EPISODE_KEYS, PROTOCOL, PublicCase,
    SCORE_METRIC_KEYS, base64_decode_strict, canonical_json_bytes, freeze_case, sha256_bytes,
    sha256_json, strict_json_loads, validate_evaluator_result, validate_public_case,
};

pub const METRIC_KEYS: [&str; 11] = SCORE_METRIC_KEYS;

/// Error raised during production conformance evaluation.
#[derive(Debug, Error)]
pub enum ProductionEvalError {
    #[error("{0}")]
    Protocol(#[from] ConformanceProtocolError),
    #[error("{0}")]
    PatchPathAbsent(String),
    #[error("{0}")]
    Unmaterializable(String),
    #[error("{0}")]
    UnsupportedTrace(String),
    #[error("{detail}")]
    TraceContextMissing { path: String, detail: String },
    #[error("{0}")]
    Validation(String),
    #[error("{0}")]
    Oracle(#[from] OracleError),
    #[error("{0}")]
    Runtime(#[from] RuntimeError),
    #[error("{0}")]
    Verifier(#[from] VerifierContextError),
    #[error("{0}")]
    Scoring(#[from] ScoringError),
    #[error("{0}")]
    Schema(#[from] SchemaValidationError),
}

/// Frozen public fields consumed by phase transition and `RuntimeV5`.
#[derive(Debug, Clone, PartialEq)]
pub struct PublicPhaseTransitionProjectionV5 {
    pub episode_id: String,
    pub policy_card: PolicyCardV5,
    pub phase1_manifest: PhaseManifestV5,
    pub phase1_slices: IndexMap<String, DeliveredSliceV5>,
    pub phase2_base_manifest: PhaseManifestV5,
    pub phase2_slices: IndexMap<String, DeliveredSliceV5>,
    pub phase2_delta_person_ids: Vec<String>,
}

impl PublicPhaseTransitionProjectionV5 {
    /// Bridge the public runtime read-set to APIs taking [`EpisodeV5`].
    ///
    /// Note that `RuntimeV5` and `induce_phase2_world` never consult `suite_id` or `family_id`.
    #[must_use]
    pub fn to_episode_v5(&self, suite_id: SuiteIdV5) -> EpisodeV5 {
        EpisodeV5 {
            episode_id: self.episode_id.clone(),
            suite_id,
            family_id: "F01".to_owned(),
            policy_card: self.policy_card.clone(),
            phase1_manifest: self.phase1_manifest.clone(),
            phase1_slices: self.phase1_slices.clone(),
            phase2_base_manifest: self.phase2_base_manifest.clone(),
            phase2_slices: self.phase2_slices.clone(),
            phase2_delta_person_ids: self.phase2_delta_person_ids.clone(),
        }
    }
}

/// P1-only runtime inputs; future phase metadata stays absent until supplied.
#[derive(Debug, Clone, PartialEq)]
pub struct PublicPhase1RuntimeProjectionV5 {
    pub episode_id: String,
    pub policy_card: PolicyCardV5,
    pub phase1_manifest: PhaseManifestV5,
    pub phase1_slices: IndexMap<String, DeliveredSliceV5>,
}

impl PublicPhase1RuntimeProjectionV5 {
    /// Bridge the P1-only runtime read-set to `RuntimeV5` while keeping P2 fields empty
    /// and strictly guarded against P1->P2 commit transition.
    #[must_use]
    pub fn to_episode_v5(&self, suite_id: SuiteIdV5) -> EpisodeV5 {
        let placeholder_p2_manifest = PhaseManifestV5 {
            phase_index: 2,
            clock_epoch_s: self.phase1_manifest.clock_epoch_s,
            query_budget: 1,
            approval_gated: false,
            work_queue: PhaseWorkQueueV5 {
                target_connector_ids: Vec::new(),
                target_hr_mapping_ids: Vec::new(),
                target_human_account_ids: Vec::new(),
                target_nhi_ids: Vec::new(),
                target_agent_ids: Vec::new(),
                target_ciem_task_ids: Vec::new(),
                target_jml_workflow_ids: Vec::new(),
                target_opt_task_ids: Vec::new(),
                target_cae_incident_ids: Vec::new(),
                target_uar_campaign_ids: Vec::new(),
                target_jit_request_ids: Vec::new(),
                target_agent_delegation_ids: Vec::new(),
            },
            source_directory: Vec::new(),
            invalidated_entity_ids: Vec::new(),
            prior_phase_ledger: None,
        };
        EpisodeV5 {
            episode_id: self.episode_id.clone(),
            suite_id,
            family_id: "F01".to_owned(),
            policy_card: self.policy_card.clone(),
            phase1_manifest: self.phase1_manifest.clone(),
            phase1_slices: self.phase1_slices.clone(),
            phase2_base_manifest: placeholder_p2_manifest,
            phase2_slices: IndexMap::new(),
            phase2_delta_person_ids: Vec::new(),
        }
    }
}

fn suite_id_from_str(s: &str) -> Result<SuiteIdV5, ProductionEvalError> {
    match s {
        "v5_dev" => Ok(SuiteIdV5::V5Dev),
        "v5_lite" => Ok(SuiteIdV5::V5Lite),
        "v5_core" => Ok(SuiteIdV5::V5Core),
        "v5_full" => Ok(SuiteIdV5::V5Full),
        _ => Err(ProductionEvalError::Validation(format!(
            "unknown suite_id: {s:?}"
        ))),
    }
}

fn exact_i64(value: &Value) -> Option<i64> {
    let Value::Number(n) = value else {
        return None;
    };
    match classify_number(n) {
        PyNumber::Int(s) => s.parse::<i64>().ok(),
        PyNumber::Float(_) => None,
    }
}

/// Validate a public JSON value in strict JSON mode.
pub fn strict_json_validate<T: StrictV5Model>(value: &Value) -> Result<T, ProductionEvalError> {
    let encoded = canonical_json_bytes(value)?;
    let text = std::str::from_utf8(&encoded)
        .map_err(|exc| ProductionEvalError::Validation(format!("invalid UTF-8 JSON: {exc}")))?;
    Ok(T::from_strict_json_str(text)?)
}

/// Convert a dotted internal field path to an RFC 6901 JSON pointer.
#[must_use]
pub fn json_pointer(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    if path.starts_with('/') {
        return path.to_owned();
    }
    let escaped: Vec<String> = path
        .split('.')
        .map(|part| part.replace('~', "~0").replace('/', "~1"))
        .collect();
    format!("/{}", escaped.join("/"))
}

/// Build the runtime's exact public read-set without hidden episode metadata.
#[must_use]
pub fn public_transition_projection(
    episode_id: impl Into<String>,
    policy_card: PolicyCardV5,
    phase1_manifest: PhaseManifestV5,
    phase1_slices: IndexMap<String, DeliveredSliceV5>,
    phase2_base_manifest: PhaseManifestV5,
    phase2_slices: IndexMap<String, DeliveredSliceV5>,
) -> PublicPhaseTransitionProjectionV5 {
    let delta_ids = phase2_base_manifest.invalidated_entity_ids.clone();
    PublicPhaseTransitionProjectionV5 {
        episode_id: episode_id.into(),
        policy_card,
        phase1_manifest,
        phase1_slices,
        phase2_base_manifest,
        phase2_slices,
        phase2_delta_person_ids: delta_ids,
    }
}

/// Combine resolved public P2 rows with only the own P1 ledger and revision.
pub fn phase2_context_from_induced(
    public_phase2_state: &WorldStateV5,
    induced_state2: &WorldStateV5,
) -> Result<WorldStateV5, ProductionEvalError> {
    if public_phase2_state.phase_index != 2 || induced_state2.phase_index != 2 {
        return Err(ProductionEvalError::Validation(
            "phase-2 context composition requires two phase-2 states".to_owned(),
        ));
    }
    let Some(ref ledger) = induced_state2.prior_phase_ledger else {
        return Err(ProductionEvalError::Validation(
            "induced phase-2 state has no independently derived P1 ledger".to_owned(),
        ));
    };
    let mut out = public_phase2_state.clone();
    out.prior_phase_ledger = Some(ledger.clone());
    out.world_rev = induced_state2.world_rev;
    Ok(out)
}

fn empty_coverage() -> Value {
    let mut map = Map::new();
    map.insert("phase_targets".to_owned(), Value::Array(Vec::new()));
    map.insert("diagnostics".to_owned(), Value::Array(Vec::new()));
    map.insert("rules".to_owned(), Value::Array(Vec::new()));
    map.insert("safety_cases".to_owned(), Value::Array(Vec::new()));
    map.insert("snapshots".to_owned(), Value::Array(Vec::new()));
    Value::Object(map)
}

fn anchor_map() -> Value {
    let mut entries: Vec<(&str, &Vec<String>)> = RULE_TO_POLICY_CARD_ANCHORS_V5
        .iter()
        .map(|(k, v)| (k.as_str(), v))
        .collect();
    entries.sort_by_key(|(a, _)| *a);
    let mut map = Map::new();
    for (key, section_ids) in entries {
        map.insert(
            key.to_owned(),
            Value::Array(section_ids.iter().cloned().map(Value::String).collect()),
        );
    }
    Value::Object(map)
}

fn empty_result(
    case: &PublicCase,
    status: &str,
    gap_code: &str,
    path: &str,
    detail: &str,
) -> Result<Value, ProductionEvalError> {
    let payload = match case.operation.as_str() {
        "derive_phase" => {
            let mut m = Map::new();
            m.insert("mutations".to_owned(), Value::Array(Vec::new()));
            m.insert("policy_card_anchor_map".to_owned(), anchor_map());
            Value::Object(m)
        }
        "validate_submission" => {
            let mut m = Map::new();
            m.insert("first_failing_stage".to_owned(), Value::Number(Number::from(2)));
            m.insert(
                "diagnostics".to_owned(),
                Value::Array(vec![Value::String("SCHEMA_VALIDATION_ERROR".to_owned())]),
            );
            Value::Object(m)
        }
        "safety" => {
            let mut m = Map::new();
            m.insert("overgrant".to_owned(), Value::Bool(false));
            m.insert("availability_breach".to_owned(), Value::Bool(false));
            Value::Object(m)
        }
        _ => {
            let mut metrics = Map::new();
            for key in METRIC_KEYS {
                metrics.insert(key.to_owned(), Value::Number(Number::from(0)));
            }
            let mut m = Map::new();
            m.insert("metrics".to_owned(), Value::Object(metrics));
            m.insert("snapshot_derivations".to_owned(), Value::Array(Vec::new()));
            Value::Object(m)
        }
    };
    let input_sha256 = sha256_bytes(&canonical_json_bytes(&case.to_json_value())?);
    let mut gap = Map::new();
    gap.insert("code".to_owned(), Value::String(gap_code.to_owned()));
    gap.insert("operation".to_owned(), Value::String(case.operation.clone()));
    gap.insert("case_id".to_owned(), Value::String(case.case_id.clone()));
    gap.insert("path".to_owned(), Value::String(json_pointer(path)));
    gap.insert("detail".to_owned(), Value::String(detail.to_owned()));

    let mut out = Map::new();
    out.insert("protocol".to_owned(), Value::String(PROTOCOL.to_owned()));
    out.insert(
        "evaluator_id".to_owned(),
        Value::String("production".to_owned()),
    );
    out.insert("case_id".to_owned(), Value::String(case.case_id.clone()));
    out.insert("input_sha256".to_owned(), Value::String(input_sha256));
    out.insert("status".to_owned(), Value::String(status.to_owned()));
    out.insert("payload".to_owned(), payload);
    out.insert("materialization".to_owned(), Value::Null);
    out.insert("coverage".to_owned(), empty_coverage());
    out.insert("gaps".to_owned(), Value::Array(vec![Value::Object(gap)]));
    Ok(Value::Object(out))
}

fn parse_slices_map(
    raw: &Value,
    field_name: &str,
) -> Result<IndexMap<String, DeliveredSliceV5>, ProductionEvalError> {
    let obj = raw.as_object().ok_or_else(|| {
        ProductionEvalError::Validation(format!("{field_name} must be a JSON object"))
    })?;
    let mut out = IndexMap::with_capacity(obj.len());
    for (slice_id, raw_slice) in obj {
        let parsed: DeliveredSliceV5 = strict_json_validate(raw_slice)?;
        if parsed.slice_id != *slice_id {
            return Err(ProductionEvalError::Validation(format!(
                "{field_name} map key disagrees with its slice_id"
            )));
        }
        out.insert(slice_id.clone(), parsed);
    }
    Ok(out)
}

fn sorted_slices(slices: &IndexMap<String, DeliveredSliceV5>) -> Vec<DeliveredSliceV5> {
    let mut keys: Vec<&String> = slices.keys().collect();
    keys.sort();
    keys.into_iter().map(|k| slices[k].clone()).collect()
}

struct TypedPublicInputs {
    card: PolicyCardV5,
    manifest: PhaseManifestV5,
    slices: IndexMap<String, DeliveredSliceV5>,
    view: ViewV5,
    state: WorldStateV5,
}

fn typed_public_inputs(case: &PublicCase) -> Result<TypedPublicInputs, ProductionEvalError> {
    let public = &case.public_episode;
    let card: PolicyCardV5 = strict_json_validate(&public["policy_card"])?;
    if card.rendered_rules_markdown != case.rendered_card {
        return Err(ProductionEvalError::Validation(
            "rendered card disagrees with the public policy-card object".to_owned(),
        ));
    }
    let phase1_manifest: PhaseManifestV5 = strict_json_validate(&public["phase1_manifest"])?;
    let phase1_slices = parse_slices_map(&public["phase1_slices"], "phase1_slices")?;
    let p1_sorted = sorted_slices(&phase1_slices);
    let phase1_view = ViewV5::from_slices(&phase1_manifest, &p1_sorted, &card, 0, None, &[])?;
    if case.phase_index == 1 {
        if phase1_manifest.phase_index != 1 {
            return Err(ProductionEvalError::Validation(
                "phase 1 case carries a different phase manifest".to_owned(),
            ));
        }
        let state = phase1_view.to_policy_state();
        return Ok(TypedPublicInputs {
            card,
            manifest: phase1_manifest,
            slices: phase1_slices,
            view: phase1_view,
            state,
        });
    }

    let phase2_manifest: PhaseManifestV5 = strict_json_validate(&public["phase2_base_manifest"])?;
    let phase2_slices = parse_slices_map(&public["phase2_slices"], "phase2_slices")?;
    if phase2_manifest.phase_index != 2 {
        return Err(ProductionEvalError::Validation(
            "phase 2 case carries a different phase manifest".to_owned(),
        ));
    }
    let p2_sorted = sorted_slices(&phase2_slices);
    let phase2_view = ViewV5::from_slices(
        &phase2_manifest,
        &p2_sorted,
        &card,
        0,
        Some(&phase1_view),
        &phase2_manifest.invalidated_entity_ids,
    )?;
    let state = phase2_view.to_policy_state();
    Ok(TypedPublicInputs {
        card,
        manifest: phase2_manifest,
        slices: phase2_slices,
        view: phase2_view,
        state,
    })
}

struct BothPhaseInputs {
    manifest1: PhaseManifestV5,
    manifest2: PhaseManifestV5,
    phase2_view: ViewV5,
    p1_state: WorldStateV5,
    p2_state: WorldStateV5,
    projection: PublicPhaseTransitionProjectionV5,
}

fn both_phase_inputs(case: &PublicCase) -> Result<BothPhaseInputs, ProductionEvalError> {
    let public = &case.public_episode;
    let card: PolicyCardV5 = strict_json_validate(&public["policy_card"])?;
    if card.rendered_rules_markdown != case.rendered_card {
        return Err(ProductionEvalError::Validation(
            "rendered card disagrees with the public policy-card object".to_owned(),
        ));
    }
    let phase1_manifest: PhaseManifestV5 = strict_json_validate(&public["phase1_manifest"])?;
    let phase1_slices = parse_slices_map(&public["phase1_slices"], "phase1_slices")?;
    let phase2_manifest: PhaseManifestV5 = strict_json_validate(&public["phase2_base_manifest"])?;
    let phase2_slices = parse_slices_map(&public["phase2_slices"], "phase2_slices")?;
    if phase1_manifest.phase_index != 1 || phase2_manifest.phase_index != 2 {
        return Err(ProductionEvalError::Validation(
            "score trace requires ordered phase 1 and phase 2 manifests".to_owned(),
        ));
    }
    let p1_sorted = sorted_slices(&phase1_slices);
    let p2_sorted = sorted_slices(&phase2_slices);
    let phase1_view = ViewV5::from_slices(&phase1_manifest, &p1_sorted, &card, 0, None, &[])?;
    let phase2_view = ViewV5::from_slices(
        &phase2_manifest,
        &p2_sorted,
        &card,
        0,
        Some(&phase1_view),
        &phase2_manifest.invalidated_entity_ids,
    )?;
    let p1_state = phase1_view.to_policy_state();
    let p2_state = phase2_view.to_policy_state();
    let episode_id = public
        .get("episode_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ProductionEvalError::Validation("missing episode_id".to_owned()))?;
    let projection = public_transition_projection(
        episode_id,
        card,
        phase1_manifest.clone(),
        phase1_slices,
        phase2_manifest.clone(),
        phase2_slices,
    );
    Ok(BothPhaseInputs {
        manifest1: phase1_manifest,
        manifest2: phase2_manifest,
        phase2_view,
        p1_state,
        p2_state,
        projection,
    })
}

type OracleTraceMap = BTreeMap<(String, String), GoldTargetTraceV5>;

fn phase_gold(
    case: &PublicCase,
    use_induced_phase1_ledger: bool,
) -> Result<(ViewV5, PhaseGoldResultV5, OracleTraceMap), ProductionEvalError> {
    if case.phase_index == 2 {
        let inputs = both_phase_inputs(case)?;
        let suite_id = suite_id_from_str(&case.suite_id)?;
        let state2 = if use_induced_phase1_ledger {
            let gold1 =
                evaluate_phase_with_trace(&inputs.p1_state, &inputs.manifest1.work_queue, &[])?;
            let ep = inputs.projection.to_episode_v5(suite_id);
            let induced_state2 = induce_phase2_world(
                &inputs.p1_state,
                &gold1.mutations,
                &inputs.manifest1.work_queue,
                &ep,
                None,
            )?;
            phase2_context_from_induced(&inputs.p2_state, &induced_state2)?
        } else {
            inputs.p2_state
        };
        let result = evaluate_phase_with_trace(&state2, &inputs.manifest2.work_queue, &[])?;
        let traces = result.trace_by_target.clone();
        return Ok((inputs.phase2_view, result, traces));
    }
    let inputs = typed_public_inputs(case)?;
    let result = evaluate_phase_with_trace(&inputs.state, &inputs.manifest.work_queue, &[])?;
    let traces = result.trace_by_target.clone();
    Ok((inputs.view, result, traces))
}

fn rule_coverage(mutations: &[Value]) -> Vec<String> {
    let mut rules = BTreeSet::new();
    for mutation in mutations {
        if let Some(cites) = mutation.get("cite").and_then(Value::as_array) {
            for citation in cites {
                if let Some(s) = citation.as_str()
                    && let Some((prefix, _)) = s.split_once(':')
                {
                    rules.insert(prefix.to_owned());
                }
            }
        }
    }
    rules.into_iter().collect()
}

/// Extract sorted unique rule prefixes from materialized `base_plan` / `event_plans` rows.
#[must_use]
pub fn materialized_base_rule_prefixes(materialization: &Value) -> Vec<String> {
    let Some(map) = materialization.as_object() else {
        return Vec::new();
    };
    let mut rows: Vec<&Value> = Vec::new();
    if let Some(base_plan) = map.get("base_plan") {
        match base_plan {
            Value::Array(arr) => rows.extend(arr.iter()),
            Value::Object(_) => rows.push(base_plan),
            _ => {}
        }
    }
    if let Some(Value::Array(event_plans)) = map.get("event_plans") {
        for event_plan in event_plans {
            if let Some(ep_obj) = event_plan.as_object()
                && let Some(event_base_plan) = ep_obj.get("base_plan")
            {
                match event_base_plan {
                    Value::Array(arr) => rows.extend(arr.iter()),
                    Value::Object(_) => rows.push(event_base_plan),
                    _ => {}
                }
            }
        }
    }
    let mut rules = BTreeSet::new();
    for row in rows {
        if let Some(Value::Array(cites)) = row.as_object().and_then(|o| o.get("cite")) {
            for citation in cites {
                if let Some(s) = citation.as_str()
                    && let Some((prefix, _)) = s.split_once(':')
                {
                    rules.insert(prefix.to_owned());
                }
            }
        }
    }
    rules.into_iter().collect()
}

/// Decode an RFC 6901 JSON Pointer into unescaped reference tokens.
pub fn pointer_parts(path: &str) -> Result<Vec<String>, ProductionEvalError> {
    if path.is_empty() {
        return Ok(Vec::new());
    }
    if !path.starts_with('/') {
        return Err(ProductionEvalError::Unmaterializable(
            "patch path is not an RFC 6901 pointer".to_owned(),
        ));
    }
    let mut parts = Vec::new();
    for raw in path[1..].split('/') {
        let bytes = raw.as_bytes();
        let mut decoded = String::with_capacity(raw.len());
        let mut i = 0;
        while i < bytes.len() {
            if bytes[i] != b'~' {
                let ch = raw[i..].chars().next().ok_or_else(|| {
                    ProductionEvalError::Unmaterializable("invalid UTF-8 in pointer".to_owned())
                })?;
                decoded.push(ch);
                i += ch.len_utf8();
                continue;
            }
            if i + 1 >= bytes.len() || (bytes[i + 1] != b'0' && bytes[i + 1] != b'1') {
                return Err(ProductionEvalError::Unmaterializable(
                    "patch path has an invalid RFC 6901 escape".to_owned(),
                ));
            }
            decoded.push(if bytes[i + 1] == b'0' { '~' } else { '/' });
            i += 2;
        }
        parts.push(decoded);
    }
    Ok(parts)
}

fn array_index(token: &str, length: usize, allow_end: bool) -> Result<usize, ProductionEvalError> {
    if token == "-" && allow_end {
        return Ok(length);
    }
    if token.is_empty()
        || (token != "0" && token.starts_with('0'))
        || !token.is_ascii()
        || !token.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(ProductionEvalError::Unmaterializable(
            "array pointer is not a canonical index".to_owned(),
        ));
    }
    let index = token.parse::<usize>().map_err(|_| {
        ProductionEvalError::PatchPathAbsent("array index is absent".to_owned())
    })?;
    if index > length || (index == length && !allow_end) {
        return Err(ProductionEvalError::PatchPathAbsent(
            "array index is absent".to_owned(),
        ));
    }
    Ok(index)
}

fn read_pointer<'a>(
    document: &'a Value,
    parts: &[String],
) -> Result<&'a Value, ProductionEvalError> {
    let mut current = document;
    for part in parts {
        match current {
            Value::Object(map) => {
                current = map.get(part).ok_or_else(|| {
                    ProductionEvalError::PatchPathAbsent(format!(
                        "object member {part:?} is absent"
                    ))
                })?;
            }
            Value::Array(arr) => {
                let idx = array_index(part, arr.len(), false)?;
                current = &arr[idx];
            }
            _ => {
                return Err(ProductionEvalError::PatchPathAbsent(
                    "patch path traverses a scalar".to_owned(),
                ));
            }
        }
    }
    Ok(current)
}

/// Apply one RFC 6902 patch operation (`add`, `remove`, `replace`, or `copy`).
pub fn apply_rfc6902_patch(
    mut document: Value,
    patch: &Map<String, Value>,
) -> Result<Value, ProductionEvalError> {
    let op = patch.get("op").and_then(Value::as_str);
    let Some(path) = patch.get("path").and_then(Value::as_str) else {
        return Err(ProductionEvalError::Unmaterializable(
            "patch path is not text".to_owned(),
        ));
    };
    let parts = pointer_parts(path)?;
    let (effective_op, value) = match op {
        Some("copy") => {
            let Some(from_path) = patch.get("from").and_then(Value::as_str) else {
                return Err(ProductionEvalError::Unmaterializable(
                    "copy patch requires a text from pointer".to_owned(),
                ));
            };
            let from_parts = pointer_parts(from_path)?;
            let copied = read_pointer(&document, &from_parts)?.clone();
            ("add", copied)
        }
        Some("add") | Some("replace") => (
            op.expect("checked"),
            patch.get("value").cloned().unwrap_or(Value::Null),
        ),
        Some("remove") => ("remove", Value::Null),
        _ => {
            return Err(ProductionEvalError::Unmaterializable(
                "unsupported RFC 6902 operation".to_owned(),
            ));
        }
    };

    if parts.is_empty() {
        if effective_op == "remove" {
            return Err(ProductionEvalError::Unmaterializable(
                "cannot remove the document root".to_owned(),
            ));
        }
        return Ok(value);
    }

    let (parent_parts, leaf) = (&parts[..parts.len() - 1], &parts[parts.len() - 1]);
    let mut current = &mut document;
    for part in parent_parts {
        match current {
            Value::Object(map) => {
                current = map.get_mut(part).ok_or_else(|| {
                    ProductionEvalError::PatchPathAbsent(format!(
                        "object member {part:?} is absent"
                    ))
                })?;
            }
            Value::Array(arr) => {
                let idx = array_index(part, arr.len(), false)?;
                current = &mut arr[idx];
            }
            _ => {
                return Err(ProductionEvalError::PatchPathAbsent(
                    "patch path traverses a scalar".to_owned(),
                ));
            }
        }
    }

    match current {
        Value::Object(map) => {
            if matches!(effective_op, "remove" | "replace") && !map.contains_key(leaf) {
                return Err(ProductionEvalError::PatchPathAbsent(format!(
                    "object member {leaf:?} is absent"
                )));
            }
            if effective_op == "remove" {
                map.shift_remove(leaf);
            } else {
                map.insert(leaf.clone(), value);
            }
            Ok(document)
        }
        Value::Array(arr) => {
            let idx = array_index(leaf, arr.len(), effective_op == "add")?;
            if effective_op == "add" {
                arr.insert(idx, value);
            } else if effective_op == "remove" {
                arr.remove(idx);
            } else {
                arr[idx] = value;
            }
            Ok(document)
        }
        _ => Err(ProductionEvalError::PatchPathAbsent(
            "patch parent is a scalar".to_owned(),
        )),
    }
}

/// Apply an ordered RFC 6902 patch recipe to a JSON base document.
pub fn apply_recipe(base: &Value, recipe: &Map<String, Value>) -> Result<Value, ProductionEvalError> {
    let mut value = base.clone();
    let Some(patches) = recipe.get("patches").and_then(Value::as_array) else {
        return Err(ProductionEvalError::Unmaterializable(
            "recipe patches are not an ordered array".to_owned(),
        ));
    };
    for patch in patches {
        let Some(patch_obj) = patch.as_object() else {
            return Err(ProductionEvalError::Unmaterializable(
                "recipe patch is not an object".to_owned(),
            ));
        };
        value = apply_rfc6902_patch(value, patch_obj)?;
    }
    Ok(value)
}

fn selected_mutation_index(
    plan: &Value,
    selector: &Map<String, Value>,
) -> Result<usize, ProductionEvalError> {
    let Some(rows) = plan.as_array() else {
        return Err(ProductionEvalError::Unmaterializable(
            "target selector requires an array base plan".to_owned(),
        ));
    };
    let sel_kind = selector.get("mutation_kind").and_then(Value::as_str);
    let sel_id = selector.get("target_entity_id").and_then(Value::as_str);
    let (Some(sel_kind), Some(sel_id)) = (sel_kind, sel_id) else {
        return Err(ProductionEvalError::Unmaterializable(
            "target selector does not identify exactly one base mutation".to_owned(),
        ));
    };
    let mut matches = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        if let Some(obj) = row.as_object()
            && obj.get("mutation_kind").and_then(Value::as_str) == Some(sel_kind)
            && obj.get("target_entity_id").and_then(Value::as_str) == Some(sel_id)
        {
            matches.push(index);
        }
    }
    if matches.len() != 1 {
        return Err(ProductionEvalError::Unmaterializable(
            "target selector does not identify exactly one base mutation".to_owned(),
        ));
    }
    Ok(matches[0])
}

/// Materialize a literal or recipe plan source against an independently derived base plan.
pub fn materialize_candidate_source(
    case: &PublicCase,
    source: &Map<String, Value>,
    base_plan: &Value,
) -> Result<(Value, Option<Map<String, Value>>), ProductionEvalError> {
    if source.len() == 1
        && let Some(submitted) = source.get("submitted_plan")
    {
        return Ok((submitted.clone(), None));
    }
    let Some(recipe) = source.get("recipe").and_then(Value::as_object) else {
        return Err(ProductionEvalError::Unmaterializable(
            "plan source has no valid recipe".to_owned(),
        ));
    };
    let scope = recipe.get("base_scope").and_then(Value::as_str);
    let mut selected_base: Option<Value> = None;
    let submitted_plan = match scope {
        Some("target_mutation") => {
            let Some(selector) = source.get("target_selector").and_then(Value::as_object) else {
                return Err(ProductionEvalError::Unmaterializable(
                    "target_mutation recipe has no selector".to_owned(),
                ));
            };
            let selected_index = selected_mutation_index(base_plan, selector)?;
            let Some(base_rows) = base_plan.as_array() else {
                return Err(ProductionEvalError::Unmaterializable(
                    "target_mutation recipe requires a full array base".to_owned(),
                ));
            };
            let sb = base_rows[selected_index].clone();
            let selected_submission = apply_recipe(&sb, recipe)?;
            selected_base = Some(sb);
            if case.operation == "safety" {
                selected_submission
            } else {
                let mut rows = base_rows.clone();
                rows[selected_index] = selected_submission;
                Value::Array(rows)
            }
        }
        Some("phase_plan") => apply_recipe(base_plan, recipe)?,
        _ => {
            return Err(ProductionEvalError::Unmaterializable(
                "recipe base_scope is unsupported".to_owned(),
            ));
        }
    };

    if case.operation == "safety" {
        let selector = source
            .get("target_selector")
            .or_else(|| case.stimulus.get("target_selector"))
            .and_then(Value::as_object)
            .ok_or_else(|| {
                ProductionEvalError::Unmaterializable(
                    "safety recipe has no target selector".to_owned(),
                )
            })?;
        let sb = match selected_base {
            Some(sb) => sb,
            None => {
                let idx = selected_mutation_index(base_plan, selector)?;
                let base_rows = base_plan.as_array().expect("checked array");
                base_rows[idx].clone()
            }
        };
        let ss = if scope == Some("target_mutation") {
            submitted_plan.clone()
        } else {
            let idx = selected_mutation_index(&submitted_plan, selector)?;
            let sub_rows = submitted_plan.as_array().expect("checked array");
            sub_rows[idx].clone()
        };
        let mut mat = Map::new();
        mat.insert("base_plan".to_owned(), sb);
        mat.insert("submitted_plan".to_owned(), ss);
        return Ok((submitted_plan, Some(mat)));
    }

    let mut mat = Map::new();
    mat.insert("base_plan".to_owned(), base_plan.clone());
    mat.insert("submitted_plan".to_owned(), submitted_plan.clone());
    Ok((submitted_plan, Some(mat)))
}

fn sorted_json_mutations(mutations: &[AtomicMutationV5]) -> Result<Value, ProductionEvalError> {
    let mut sorted = mutations.to_vec();
    sorted.sort_by(|a, b| {
        (a.mutation_kind(), a.target_entity_id()).cmp(&(b.mutation_kind(), b.target_entity_id()))
    });
    let mut out = Vec::with_capacity(sorted.len());
    for row in &sorted {
        out.push(row.to_strict_json_value()?);
    }
    Ok(Value::Array(out))
}

fn materialize_source(
    case: &PublicCase,
    source: &Map<String, Value>,
) -> Result<(Value, Option<Map<String, Value>>), ProductionEvalError> {
    if source.len() == 1
        && let Some(submitted) = source.get("submitted_plan")
    {
        return Ok((submitted.clone(), None));
    }
    if !source.get("recipe").is_some_and(Value::is_object) {
        return Err(ProductionEvalError::Unmaterializable(
            "plan source has no valid recipe".to_owned(),
        ));
    }
    let (_view, gold, _trace) = phase_gold(case, case.operation == "score_trace")?;
    let base_plan = sorted_json_mutations(&gold.mutations)?;
    materialize_candidate_source(case, source, &base_plan)
}

/// Return `(base_plan, submitted_plan)` for one `score_trace` `stage_phase_mutations` event.
#[must_use]
pub fn score_event_plan_materialization(
    candidate: &Value,
    recipe_materialization: Option<&Map<String, Value>>,
) -> (Value, Value) {
    match recipe_materialization {
        None => (Value::Null, candidate.clone()),
        Some(mat) => (mat["base_plan"].clone(), mat["submitted_plan"].clone()),
    }
}

fn snapshot_mutation_rows(mutations: &[AtomicMutationV5]) -> Result<Value, ProductionEvalError> {
    let mut out = Vec::with_capacity(mutations.len());
    for item in mutations {
        out.push(item.to_strict_json_value()?);
    }
    Ok(Value::Array(out))
}

fn assert_stage_event_plan_bijection(
    expected: &[(i64, i64)],
    materialized: &[Value],
) -> Result<(), ProductionEvalError> {
    let mut actual = Vec::with_capacity(materialized.len());
    for row in materialized {
        let obj = row.as_object().ok_or_else(|| {
            ProductionEvalError::UnsupportedTrace(
                "stage event plans do not bijectively match supplied events".to_owned(),
            )
        })?;
        let seq = obj.get("seq").and_then(exact_i64);
        let phase_index = obj.get("phase_index").and_then(exact_i64);
        match (seq, phase_index) {
            (Some(s), Some(p)) => actual.push((s, p)),
            _ => {
                return Err(ProductionEvalError::UnsupportedTrace(
                    "stage event plans do not bijectively match supplied events".to_owned(),
                ));
            }
        }
    }
    if actual != expected {
        return Err(ProductionEvalError::UnsupportedTrace(
            "stage event plans do not bijectively match supplied events".to_owned(),
        ));
    }
    Ok(())
}

fn parse_candidate(value: &Value) -> (Option<Vec<AtomicMutationV5>>, bool, Option<&'static str>) {
    let Some(rows) = value.as_array() else {
        return (None, false, Some("submission is not an array of mutations"));
    };
    let mut parsed = Vec::with_capacity(rows.len());
    for row in rows {
        match strict_json_validate::<AtomicMutationV5>(row) {
            Ok(m) => parsed.push(m),
            Err(_) => {
                return (
                    None,
                    false,
                    Some("submission contains a row that fails AtomicMutationV5 schema validation"),
                );
            }
        }
    }
    (Some(parsed), true, None)
}

fn extract_evidence_ids(value: &Value, found: &mut BTreeSet<String>) {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                if key == "evidence_id"
                    && let Some(s) = child.as_str()
                {
                    found.insert(s.to_owned());
                } else {
                    extract_evidence_ids(child, found);
                }
            }
        }
        Value::Array(items) => {
            for child in items {
                extract_evidence_ids(child, found);
            }
        }
        _ => {}
    }
}

/// Compute the delivered read-ledger evidence IDs for `case`, evicting stale P1
/// `hr_persons` evidence on Phase 2.
#[must_use]
pub fn read_ledger(case: &PublicCase, card: &PolicyCardV5) -> BTreeSet<String> {
    let public = &case.public_episode;
    let string_vec = |val: Option<&Value>| -> Vec<String> {
        val.and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    };

    let mut p1_ids = string_vec(public.get("q1_slice_ids_p1"));
    p1_ids.extend(string_vec(public.get("q2_slice_ids_p1")));
    let p1_slices = public.get("phase1_slices").and_then(Value::as_object);

    let mut p2_ids = Vec::new();
    let mut p2_slices = None;
    let mut invalidated = BTreeSet::new();
    if case.phase_index == 2 {
        p2_ids = string_vec(public.get("q1_slice_ids_p2"));
        p2_ids.extend(string_vec(public.get("q2_slice_ids_p2")));
        p2_slices = public.get("phase2_slices").and_then(Value::as_object);
        if let Some(arr) = public
            .get("phase2_base_manifest")
            .and_then(Value::as_object)
            .and_then(|m| m.get("invalidated_entity_ids"))
            .and_then(Value::as_array)
        {
            for item in arr {
                if let Some(s) = item.as_str() {
                    invalidated.insert(s.to_owned());
                }
            }
        }
    }

    let policy_evidence: BTreeSet<String> = card
        .overlay
        .temporal_sod_rules
        .iter()
        .map(|r| r.evidence_id.clone())
        .collect();
    let mut inherited_evidence = BTreeSet::new();
    let mut refreshed_evidence = BTreeSet::new();
    let mut stale_person_evidence = BTreeSet::new();

    for (source_phase, slice_ids, source_slices, is_p1) in [
        (1_i64, &p1_ids, p1_slices, true),
        (2_i64, &p2_ids, p2_slices, false),
    ] {
        let Some(slices_map) = source_slices else {
            continue;
        };
        let evidence = if is_p1 {
            &mut inherited_evidence
        } else {
            &mut refreshed_evidence
        };
        for slice_id in slice_ids {
            let Some(delivered) = slices_map.get(slice_id).and_then(Value::as_object) else {
                continue;
            };
            let Some(tables) = delivered.get("tables").and_then(Value::as_array) else {
                continue;
            };
            for table in tables {
                let Some(table_obj) = table.as_object() else {
                    continue;
                };
                let Some(columns) = table_obj.get("columns").and_then(Value::as_array) else {
                    continue;
                };
                let Some(rows) = table_obj.get("rows").and_then(Value::as_array) else {
                    continue;
                };
                let col_names: Vec<Option<&str>> =
                    columns.iter().map(Value::as_str).collect();
                if col_names.iter().any(Option::is_none) {
                    continue;
                }
                let table_name = table_obj.get("table_name").and_then(Value::as_str);
                for row_val in rows {
                    let Some(row) = row_val.as_array() else {
                        continue;
                    };
                    if row.len() != col_names.len() {
                        continue;
                    }
                    let mut person_id: Option<&str> = None;
                    let mut top_ev_id: Option<&str> = None;
                    for (col_opt, cell) in col_names.iter().zip(row.iter()) {
                        let col = col_opt.expect("checked");
                        if col == "evidence_id"
                            && let Some(s) = cell.as_str()
                        {
                            evidence.insert(s.to_owned());
                            top_ev_id = Some(s);
                        } else {
                            extract_evidence_ids(cell, evidence);
                        }
                        if col == "person_id" {
                            person_id = cell.as_str();
                        }
                    }
                    if source_phase == 1
                        && table_name == Some("hr_persons")
                        && let (Some(pid), Some(ev_id)) = (person_id, top_ev_id)
                        && invalidated.contains(pid)
                    {
                        stale_person_evidence.insert(ev_id.to_owned());
                    }
                }
            }
        }
    }

    if case.phase_index == 1 {
        policy_evidence
            .union(&inherited_evidence)
            .cloned()
            .collect()
    } else {
        let surviving: BTreeSet<String> = inherited_evidence
            .difference(&stale_person_evidence)
            .cloned()
            .collect();
        policy_evidence
            .union(&surviving)
            .cloned()
            .chain(refreshed_evidence)
            .collect()
    }
}

fn submission_value(
    case: &PublicCase,
    stimulus: &Map<String, Value>,
) -> Result<Value, ProductionEvalError> {
    if let Some(submitted) = stimulus.get("submitted_plan") {
        return Ok(submitted.clone());
    }
    if stimulus.contains_key("recipe") {
        let mut source = Map::new();
        for key in ["recipe", "target_selector"] {
            if let Some(val) = stimulus.get(key) {
                source.insert(key.to_owned(), val.clone());
            }
        }
        return Ok(materialize_source(case, &source)?.0);
    }
    let b64 = stimulus
        .get("raw_submission_b64")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ProductionEvalError::Protocol(ConformanceProtocolError::protocol(
                "raw_submission_b64 must be text",
            ))
        })?;
    let raw = base64_decode_strict(b64)?;
    Ok(strict_json_loads(&raw)?)
}

fn convert_trace_map(
    traces: &BTreeMap<(String, String), GoldTargetTraceV5>,
) -> GoldTraceMap {
    traces
        .iter()
        .map(|(k, v)| (k.clone(), gold_trace_entry_from_oracle_trace(v)))
        .collect()
}

fn validate_submission_op(case: &PublicCase) -> Result<Value, ProductionEvalError> {
    let value = match submission_value(case, &case.stimulus) {
        Ok(v) => v,
        Err(
            ProductionEvalError::Protocol(_)
            | ProductionEvalError::PatchPathAbsent(_)
            | ProductionEvalError::Unmaterializable(_),
        ) => {
            let mut m = Map::new();
            m.insert(
                "first_failing_stage".to_owned(),
                Value::Number(Number::from(2)),
            );
            m.insert(
                "diagnostics".to_owned(),
                Value::Array(vec![Value::String("SCHEMA_VALIDATION_ERROR".to_owned())]),
            );
            return Ok(Value::Object(m));
        }
        Err(err) => return Err(err),
    };
    let (parsed, schema_ok, _reason) = parse_candidate(&value);
    let (view, _gold, traces) = phase_gold(case, false)?;
    let manifest: PhaseManifestV5 = if case.phase_index == 1 {
        strict_json_validate(&case.public_episode["phase1_manifest"])?
    } else {
        strict_json_validate(&case.public_episode["phase2_base_manifest"])?
    };

    let raw_rows = value.as_array().map(Vec::as_slice).unwrap_or(&[]);
    let required_set: BTreeSet<(String, String)> =
        required_mutation_keys(&manifest.work_queue, case.phase_index);

    let mut raw_key_count = 0usize;
    let mut parsed_keys = BTreeSet::new();
    let mut all_string_keys = true;
    for row in raw_rows {
        if let Some(obj) = row.as_object() {
            raw_key_count += 1;
            match (
                obj.get("mutation_kind").and_then(Value::as_str),
                obj.get("target_entity_id").and_then(Value::as_str),
            ) {
                (Some(mk), Some(tid)) => {
                    parsed_keys.insert((mk.to_owned(), tid.to_owned()));
                }
                _ => {
                    all_string_keys = false;
                }
            }
        }
    }
    let stage1_ok = all_string_keys
        && raw_key_count == parsed_keys.len()
        && parsed_keys == required_set
        && raw_key_count == required_set.len();
    if !stage1_ok {
        let mut m = Map::new();
        m.insert(
            "first_failing_stage".to_owned(),
            Value::Number(Number::from(1)),
        );
        m.insert(
            "diagnostics".to_owned(),
            Value::Array(vec![Value::String("COMPLETENESS_VIOLATION".to_owned())]),
        );
        return Ok(Value::Object(m));
    }
    let Some(parsed) = parsed.filter(|_| schema_ok) else {
        let mut m = Map::new();
        m.insert(
            "first_failing_stage".to_owned(),
            Value::Number(Number::from(2)),
        );
        m.insert(
            "diagnostics".to_owned(),
            Value::Array(vec![Value::String("SCHEMA_VALIDATION_ERROR".to_owned())]),
        );
        return Ok(Value::Object(m));
    };

    let errors = structural_errors(&view, case.phase_index, &parsed, &manifest.work_queue);
    let stage2_ok = errors.is_empty();
    let mut stage2_valid = BTreeMap::new();
    for key in &required_set {
        let prefix = format!("{}:", key.1);
        stage2_valid.insert(key.clone(), !errors.iter().any(|e| e.starts_with(&prefix)));
    }
    let card: PolicyCardV5 = strict_json_validate(&case.public_episode["policy_card"])?;
    let ledger = read_ledger(case, &card);
    let required_sorted: Vec<(String, String)> = required_set.into_iter().collect();
    let trace_map = convert_trace_map(&traces);
    let verification = verify_phase(
        case.phase_index,
        &required_sorted,
        &parsed,
        &trace_map,
        &ledger,
        EvaluatePhaseOptions {
            stage1_ok: true,
            stage2_ok,
            stage2_valid_by_target: Some(&stage2_valid),
            preconditions: None,
            citations: None,
        },
    )?;

    let mut diags: BTreeSet<&'static str> = BTreeSet::new();
    for cat in &verification.violation_categories {
        diags.insert(violation_category_as_str(*cat));
    }
    let mut m = Map::new();
    m.insert(
        "first_failing_stage".to_owned(),
        match verification.first_failing_stage {
            Some(s) => Value::Number(Number::from(s as u64)),
            None => Value::Null,
        },
    );
    m.insert(
        "diagnostics".to_owned(),
        Value::Array(diags.into_iter().map(|s| Value::String(s.to_owned())).collect()),
    );
    Ok(Value::Object(m))
}

fn safety_op(case: &PublicCase) -> Result<(Value, Value, Vec<String>), ProductionEvalError> {
    let selector = case.stimulus["target_selector"]
        .as_object()
        .ok_or_else(|| ProductionEvalError::Validation("missing target_selector".to_owned()))?;
    let sel_kind = selector["mutation_kind"].as_str().unwrap_or("");
    let sel_id = selector["target_entity_id"].as_str().unwrap_or("");

    let fail_pair = |notes: Vec<String>| -> (Value, Value, Vec<String>) {
        let mut p = Map::new();
        p.insert("overgrant".to_owned(), Value::Bool(false));
        p.insert("availability_breach".to_owned(), Value::Bool(false));
        (Value::Object(p), empty_coverage(), notes)
    };

    let mut value = match submission_value(case, &case.stimulus) {
        Ok(v) => v,
        Err(
            err @ (ProductionEvalError::Protocol(_)
            | ProductionEvalError::PatchPathAbsent(_)
            | ProductionEvalError::Unmaterializable(_)),
        ) => {
            let err_name = match &err {
                ProductionEvalError::Protocol(_) => "ConformanceProtocolError",
                ProductionEvalError::PatchPathAbsent(_) => "_PatchPathAbsent",
                ProductionEvalError::Unmaterializable(_) => "_Unmaterializable",
                _ => "Error",
            };
            return Ok(fail_pair(vec![format!(
                "cannot materialize or parse selected submission: {err_name}"
            )]));
        }
        Err(err) => return Err(err),
    };
    if value.is_object() {
        value = Value::Array(vec![value]);
    }
    let (parsed, schema_ok, _reason) = parse_candidate(&value);
    let Some(parsed) = parsed.filter(|_| schema_ok) else {
        return Ok(fail_pair(vec![
            "selected mutation fails AtomicMutationV5 schema validation".to_owned(),
        ]));
    };
    let selected: Vec<AtomicMutationV5> = parsed
        .into_iter()
        .filter(|item| item.mutation_kind() == sel_kind && item.target_entity_id() == sel_id)
        .collect();
    if selected.len() != 1 {
        return Ok(fail_pair(vec![
            "target selector does not identify exactly one submitted mutation".to_owned(),
        ]));
    }
    let (view, _gold, traces) = phase_gold(case, false)?;
    let key = (sel_kind.to_owned(), sel_id.to_owned());
    let Some(trace) = traces.get(&key) else {
        return Ok(fail_pair(vec![
            "target selector is absent from independently derived phase targets".to_owned(),
        ]));
    };
    let manifest: PhaseManifestV5 = if case.phase_index == 1 {
        strict_json_validate(&case.public_episode["phase1_manifest"])?
    } else {
        strict_json_validate(&case.public_episode["phase2_base_manifest"])?
    };
    let errors = structural_errors(&view, case.phase_index, &selected, &manifest.work_queue);
    if !errors.is_empty() {
        return Ok(fail_pair(vec![
            "selected mutation fails Stage-2 delivered-view validation".to_owned(),
        ]));
    }
    let gold_entry = gold_trace_entry_from_oracle_trace(trace);
    let (overgrant, availability) =
        safety_partition(&selected[0], &gold_entry.mutation, &gold_entry.context)?;

    let mut payload = Map::new();
    payload.insert("overgrant".to_owned(), Value::Bool(overgrant));
    payload.insert("availability_breach".to_owned(), Value::Bool(availability));

    let mut safety_row = Map::new();
    safety_row.insert("case_id".to_owned(), Value::String(case.case_id.clone()));
    safety_row.insert(
        "mutation_kind".to_owned(),
        Value::String(sel_kind.to_owned()),
    );
    safety_row.insert("overgrant".to_owned(), Value::Bool(overgrant));
    safety_row.insert(
        "availability_breach".to_owned(),
        Value::Bool(availability),
    );

    let mut coverage = Map::new();
    coverage.insert("phase_targets".to_owned(), Value::Array(Vec::new()));
    coverage.insert("diagnostics".to_owned(), Value::Array(Vec::new()));
    coverage.insert("rules".to_owned(), Value::Array(Vec::new()));
    coverage.insert(
        "safety_cases".to_owned(),
        Value::Array(vec![Value::Object(safety_row)]),
    );
    coverage.insert("snapshots".to_owned(), Value::Array(Vec::new()));

    Ok((Value::Object(payload), Value::Object(coverage), Vec::new()))
}

fn derive_op(case: &PublicCase) -> Result<(Value, Value), ProductionEvalError> {
    let (_view, result, _traces) = phase_gold(case, false)?;
    let mutations_val = sorted_json_mutations(&result.mutations)?;
    let mutations_arr = mutations_val.as_array().expect("sorted array");
    let episode_id = case.episode_id().to_owned();
    let mut phase_targets = Vec::with_capacity(mutations_arr.len());
    for row in mutations_arr {
        let mut pt = Map::new();
        pt.insert("suite_id".to_owned(), Value::String(case.suite_id.clone()));
        pt.insert("episode_id".to_owned(), Value::String(episode_id.clone()));
        pt.insert(
            "phase_index".to_owned(),
            Value::Number(Number::from(case.phase_index)),
        );
        pt.insert(
            "mutation_kind".to_owned(),
            row["mutation_kind"].clone(),
        );
        pt.insert(
            "target_entity_id".to_owned(),
            row["target_entity_id"].clone(),
        );
        phase_targets.push(Value::Object(pt));
    }
    let rules = rule_coverage(mutations_arr);

    let mut payload = Map::new();
    payload.insert("mutations".to_owned(), mutations_val);
    payload.insert("policy_card_anchor_map".to_owned(), anchor_map());

    let mut coverage = Map::new();
    coverage.insert("phase_targets".to_owned(), Value::Array(phase_targets));
    coverage.insert("diagnostics".to_owned(), Value::Array(Vec::new()));
    coverage.insert(
        "rules".to_owned(),
        Value::Array(rules.into_iter().map(Value::String).collect()),
    );
    coverage.insert("safety_cases".to_owned(), Value::Array(Vec::new()));
    coverage.insert("snapshots".to_owned(), Value::Array(Vec::new()));
    Ok((Value::Object(payload), Value::Object(coverage)))
}

/// Parse `(phase_index, stage_id)` from `score_trace` event arguments.
pub fn phase_stage_args(args: &Map<String, Value>) -> Result<(i64, String), ProductionEvalError> {
    let mut missing = Vec::new();
    if !args.contains_key("phase_index") {
        missing.push("phase_index");
    }
    if !args.contains_key("stage_id") {
        missing.push("stage_id");
    }
    if !missing.is_empty() {
        return Err(ProductionEvalError::UnsupportedTrace(format!(
            "score event lacks required argument(s): {}",
            missing.join(", ")
        )));
    }
    let phase_index = exact_i64(&args["phase_index"]).filter(|&p| p == 1 || p == 2);
    let stage_id = args["stage_id"].as_str();
    match (phase_index, stage_id) {
        (Some(p), Some(s)) => Ok((p, s.to_owned())),
        _ => Err(ProductionEvalError::Validation(
            "score event phase_index/stage_id has an invalid type or value".to_owned(),
        )),
    }
}

/// Compute `G-PROP` for `COUPLED` vs `ORACLE_PHASE_RESET` execution modes.
#[must_use]
pub fn g_prop_metric(
    mode: &str,
    p1_committed: bool,
    p2_committed: bool,
    phase2_feasible: Option<bool>,
    reference_p2_pass: bool,
    actual_p2_pass: Option<bool>,
) -> i64 {
    if mode == "COUPLED" {
        i64::from(
            p1_committed
                && p2_committed
                && phase2_feasible == Some(true)
                && actual_p2_pass == Some(true),
        )
    } else {
        i64::from(p2_committed && reference_p2_pass)
    }
}

fn parse_cosign_scope_kind(raw: &str) -> Result<CoSignScopeKindV5, ProductionEvalError> {
    match raw {
        "UAR_CUSTODIAN" => Ok(CoSignScopeKindV5::UarCustodian),
        "CRITICAL_JIT" => Ok(CoSignScopeKindV5::CriticalJit),
        "AGENT_DELEGATION_COSIGN" => Ok(CoSignScopeKindV5::AgentDelegationCosign),
        _ => Err(ProductionEvalError::Validation(format!(
            "unsupported co-sign scope kind: {raw:?}"
        ))),
    }
}

fn p1_commit_would_succeed_and_transition(
    runtime: &RuntimeV5,
    staged: Option<&(i64, String, Vec<AtomicMutationV5>, String)>,
    stage_id: &str,
) -> bool {
    if runtime.episode_completed() {
        return false;
    }
    if runtime
        .phase_snapshots(1)
        .is_some_and(|s| s.commit.is_some())
    {
        return false;
    }
    if runtime.phase_index() != 1 {
        return false;
    }
    let Some((staged_phase, staged_id, staged_mutations, staged_digest)) = staged else {
        return false;
    };
    if *staged_phase != 1 || staged_id != stage_id || !STAGE_ID_RE.is_match(stage_id) {
        return false;
    }
    let manifest = runtime.manifest();
    if !completeness_ok(&manifest.work_queue, 1, staged_mutations)
        || !structural_ok(runtime.view(), 1, staged_mutations, &manifest.work_queue)
    {
        return false;
    }
    let episode_id = &runtime.episode().episode_id;
    let minted: BTreeSet<CoSignTuple> = runtime
        .minted_cosigns()
        .iter()
        .filter(|entry| {
            entry.0 == *episode_id
                && entry.1 == 1
                && entry.2 == stage_id
                && entry.3 == *staged_digest
        })
        .map(|entry| entry.4.clone())
        .collect();
    let required_cosigns = cosign_requirements(staged_mutations);
    if minted != required_cosigns || manifest.approval_gated {
        return false;
    }
    let occ_failed = staged_mutations.iter().any(|m| {
        m.if_match().iter().any(|token| {
            let Some((eid, _)) = token.split_once('@') else {
                return true;
            };
            live_version(eid, runtime.view()).as_deref() != Some(token.as_str())
        })
    });
    !occ_failed
}

#[allow(clippy::too_many_lines)]
fn score_trace_op(
    case: &PublicCase,
    mut transition_gaps: Option<&mut Vec<Value>>,
) -> Result<(Value, Value, Value), ProductionEvalError> {
    let mode = case.stimulus["mode"].as_str().ok_or_else(|| {
        ProductionEvalError::Validation("score_trace mode must be a string".to_owned())
    })?;
    let raw_events = case.stimulus["events"].as_array().ok_or_else(|| {
        ProductionEvalError::Validation("score_trace events must be an array".to_owned())
    })?;
    let has_phase2_context = case.phase_index == 2;
    let suite_id = suite_id_from_str(&case.suite_id)?;

    let (
        manifest1,
        manifest2,
        initial_state,
        public_phase2_state,
        episode_v5,
        gold_phase1,
        gold_state2,
    ) = if has_phase2_context {
        let inputs = both_phase_inputs(case)?;
        let ep = inputs.projection.to_episode_v5(suite_id);
        let gold1 =
            evaluate_phase_with_trace(&inputs.p1_state, &inputs.manifest1.work_queue, &[])?;
        let induced_gold_state2 = induce_phase2_world(
            &inputs.p1_state,
            &gold1.mutations,
            &inputs.manifest1.work_queue,
            &ep,
            None,
        )?;
        let g_state2 = phase2_context_from_induced(&inputs.p2_state, &induced_gold_state2)?;
        (
            inputs.manifest1,
            Some(inputs.manifest2),
            inputs.p1_state,
            Some(inputs.p2_state),
            ep,
            gold1,
            Some(g_state2),
        )
    } else {
        let inputs = typed_public_inputs(case)?;
        let ep_id = case.episode_id().to_owned();
        let proj = PublicPhase1RuntimeProjectionV5 {
            episode_id: ep_id,
            policy_card: inputs.card,
            phase1_manifest: inputs.manifest.clone(),
            phase1_slices: inputs.slices,
        };
        let ep = proj.to_episode_v5(suite_id);
        let gold1 = evaluate_phase_with_trace(&inputs.state, &inputs.manifest.work_queue, &[])?;
        (
            inputs.manifest,
            None,
            inputs.state,
            None,
            ep,
            gold1,
            None,
        )
    };

    let options = RuntimeOptionsV5 {
        wave_execution_mode: if has_phase2_context {
            mode.to_owned()
        } else {
            "COUPLED".to_owned()
        },
        phase2_reset_state: if has_phase2_context && mode == "ORACLE_PHASE_RESET" {
            gold_state2.clone()
        } else {
            None
        },
        ..RuntimeOptionsV5::default()
    };
    let mut runtime = RuntimeV5::with_options(episode_v5.clone(), initial_state.clone(), options)?;

    let mut materialized_events: Vec<Value> = Vec::new();
    let mut stage_event_pairs: Vec<(i64, i64)> = Vec::new();
    let mut first_stage_seq: BTreeMap<i64, i64> = BTreeMap::new();
    let mut last_stage_seq: BTreeMap<i64, i64> = BTreeMap::new();
    let mut commit_seq: BTreeMap<i64, i64> = BTreeMap::new();
    let mut p1_staged_cache: Option<(i64, String, Vec<AtomicMutationV5>, String)> = None;

    let mut usage_in: i64 = 0;
    let mut usage_out: i64 = 0;

    for (event_index, event_val) in raw_events.iter().enumerate() {
        let event = event_val.as_object().ok_or_else(|| {
            ProductionEvalError::Validation("score_trace event must be an object".to_owned())
        })?;
        let seq = exact_i64(&event["seq"]).ok_or_else(|| {
            ProductionEvalError::Validation("score_trace event seq must be int".to_owned())
        })?;
        if let Some(usage) = event.get("token_usage").and_then(Value::as_object) {
            usage_in += usage.get("input_tokens").and_then(exact_i64).unwrap_or(0);
            usage_out += usage.get("output_tokens").and_then(exact_i64).unwrap_or(0);
        }
        runtime.set_usage_totals(usage_in, 0, usage_out, 0)?;

        let tool = event["tool"].as_str().ok_or_else(|| {
            ProductionEvalError::Validation("score_trace tool must be text".to_owned())
        })?;
        let args = event["args"].as_object().ok_or_else(|| {
            ProductionEvalError::Validation("score_trace args must be object".to_owned())
        })?;

        match tool {
            "query_sources" => {
                let expected_keys: BTreeSet<&str> =
                    ["phase_index", "slice_ids"].into_iter().collect();
                let actual_keys: BTreeSet<&str> = args.keys().map(String::as_str).collect();
                if actual_keys != expected_keys {
                    return Err(ProductionEvalError::UnsupportedTrace(
                        "query_sources event lacks phase_index or slice_ids".to_owned(),
                    ));
                }
                let phase_index = exact_i64(&args["phase_index"])
                    .filter(|&p| p == 1 || p == 2)
                    .ok_or_else(|| {
                        ProductionEvalError::Validation(
                            "query_sources phase_index must be 1 or 2".to_owned(),
                        )
                    })?;
                let slice_arr = args["slice_ids"].as_array().ok_or_else(|| {
                    ProductionEvalError::Validation(
                        "query_sources event does not contain a slice-id array".to_owned(),
                    )
                })?;
                let mut slice_ids = Vec::with_capacity(slice_arr.len());
                for item in slice_arr {
                    let s = item.as_str().ok_or_else(|| {
                        ProductionEvalError::Validation(
                            "query_sources event does not contain a slice-id array".to_owned(),
                        )
                    })?;
                    slice_ids.push(s.to_owned());
                }
                runtime.query_sources(phase_index, &slice_ids)?;
            }
            "stage_phase_mutations" => {
                let (phase, stage_id) = phase_stage_args(args)?;
                stage_event_pairs.push((seq, phase));
                let expected_keys: BTreeSet<&str> =
                    ["phase_index", "stage_id", "plan_source"].into_iter().collect();
                let actual_keys: BTreeSet<&str> = args.keys().map(String::as_str).collect();
                if actual_keys != expected_keys {
                    return Err(ProductionEvalError::UnsupportedTrace(
                        "stage_phase_mutations args do not match the shared protocol".to_owned(),
                    ));
                }
                let source = args["plan_source"].as_object().ok_or_else(|| {
                    ProductionEvalError::Validation("plan_source must be an object".to_owned())
                })?;
                let phase_case = if phase == 2 && case.phase_index == 1 {
                    if !(source.len() == 1 && source.contains_key("submitted_plan")) {
                        return Err(ProductionEvalError::Unmaterializable(
                            "phase-2 plan recipe requires a public phase-2 base plan".to_owned(),
                        ));
                    }
                    case.clone()
                } else {
                    let mut case_val = case.to_json_value();
                    let case_obj = case_val.as_object_mut().expect("object");
                    case_obj.insert("phase_index".to_owned(), Value::Number(Number::from(phase)));
                    if phase == 1 && case.phase_index == 2 {
                        let mut p1_ep = Map::new();
                        for k in P1_EPISODE_KEYS {
                            p1_ep.insert((*k).to_owned(), case.public_episode[k].clone());
                        }
                        case_obj.insert("public_episode".to_owned(), Value::Object(p1_ep));
                    }
                    validate_public_case(&case_val)?
                };

                let (candidate, recipe_mat) = materialize_source(&phase_case, source)?;
                let (event_base_plan, event_submitted_plan) =
                    score_event_plan_materialization(&candidate, recipe_mat.as_ref());
                let mut ev_row = Map::new();
                ev_row.insert("seq".to_owned(), Value::Number(Number::from(seq)));
                ev_row.insert("phase_index".to_owned(), Value::Number(Number::from(phase)));
                ev_row.insert("base_plan".to_owned(), event_base_plan);
                ev_row.insert("submitted_plan".to_owned(), event_submitted_plan);
                materialized_events.push(Value::Object(ev_row));

                let (parsed, schema_ok, reason) = parse_candidate(&candidate);
                let Some(parsed) = parsed.filter(|_| schema_ok) else {
                    return Err(ProductionEvalError::Validation(
                        reason
                            .unwrap_or("stage plan fails AtomicMutationV5 schema validation")
                            .to_owned(),
                    ));
                };
                let receipt = runtime.stage_phase_mutations(phase, &stage_id, &parsed)?;
                if receipt.status == StageReceiptStatusV5::Staged {
                    first_stage_seq.entry(phase).or_insert(seq);
                    last_stage_seq.insert(phase, seq);
                    if phase == 1 {
                        let (digest, _) = stage_digests(
                            phase,
                            &stage_id,
                            runtime.world().world_rev,
                            &parsed,
                        )?;
                        p1_staged_cache = Some((phase, stage_id, parsed, digest));
                    }
                }
            }
            "request_cosign" => {
                let (phase_index, stage_id) = phase_stage_args(args)?;
                let expected_keys: BTreeSet<&str> = [
                    "phase_index",
                    "stage_id",
                    "scope_kind",
                    "target_entity_id",
                    "actor_account_id",
                ]
                .into_iter()
                .collect();
                let actual_keys: BTreeSet<&str> = args.keys().map(String::as_str).collect();
                if actual_keys != expected_keys {
                    return Err(ProductionEvalError::UnsupportedTrace(
                        "request_cosign event lacks normative tool arguments".to_owned(),
                    ));
                }
                let scope_kind_str = args["scope_kind"].as_str().ok_or_else(|| {
                    ProductionEvalError::Validation("scope_kind must be a string".to_owned())
                })?;
                let scope_kind = parse_cosign_scope_kind(scope_kind_str)?;
                let target_entity_id = args["target_entity_id"].as_str().ok_or_else(|| {
                    ProductionEvalError::Validation("target_entity_id must be a string".to_owned())
                })?;
                let actor_account_id = args["actor_account_id"].as_str().ok_or_else(|| {
                    ProductionEvalError::Validation("actor_account_id must be a string".to_owned())
                })?;
                runtime.request_cosign(
                    phase_index,
                    &stage_id,
                    scope_kind,
                    target_entity_id,
                    actor_account_id,
                );
            }
            "commit_phase_mutations" => {
                let (phase_index, stage_id) = phase_stage_args(args)?;
                let expected_keys: BTreeSet<&str> = ["phase_index", "stage_id"].into_iter().collect();
                let actual_keys: BTreeSet<&str> = args.keys().map(String::as_str).collect();
                if actual_keys != expected_keys {
                    return Err(ProductionEvalError::UnsupportedTrace(
                        "commit_phase_mutations args do not match tool schema".to_owned(),
                    ));
                }

                if !has_phase2_context
                    && phase_index == 1
                    && p1_commit_would_succeed_and_transition(
                        &runtime,
                        p1_staged_cache.as_ref(),
                        &stage_id,
                    )
                {
                    let gap_path = "/public_episode/phase2_delta_person_ids";
                    let gap_detail = "successful P1 commit requires unavailable P2 transition field phase2_delta_person_ids";
                    match transition_gaps.as_deref_mut() {
                        Some(gaps) if event_index == raw_events.len() - 1 => {
                            let mut gap = Map::new();
                            gap.insert(
                                "code".to_owned(),
                                Value::String("UNDERDEFINED_PUBLIC_INPUT".to_owned()),
                            );
                            gap.insert(
                                "operation".to_owned(),
                                Value::String("score_trace".to_owned()),
                            );
                            gap.insert("case_id".to_owned(), Value::String(case.case_id.clone()));
                            gap.insert("path".to_owned(), Value::String(gap_path.to_owned()));
                            gap.insert("detail".to_owned(), Value::String(gap_detail.to_owned()));
                            gaps.push(Value::Object(gap));
                            break;
                        }
                        _ => {
                            return Err(ProductionEvalError::TraceContextMissing {
                                path: gap_path.to_owned(),
                                detail: gap_detail.to_owned(),
                            });
                        }
                    }
                }

                let before_count = runtime.snapshots().len();
                runtime.commit_phase_mutations(phase_index, &stage_id)?;
                if runtime.snapshots().len() > before_count {
                    commit_seq.insert(phase_index, seq);
                }
            }
            _ => {
                return Err(ProductionEvalError::UnsupportedTrace(format!(
                    "unsupported score_trace tool: {tool:?}"
                )));
            }
        }
    }

    assert_stage_event_plan_bijection(&stage_event_pairs, &materialized_events)?;

    let trace1 = convert_trace_map(&gold_phase1.trace_by_target);
    let p1_snapshots = runtime
        .phase_snapshots(1)
        .cloned()
        .unwrap_or_default();
    let p1_commit = p1_snapshots.commit.as_ref();
    let p1_mutations: Vec<AtomicMutationV5> =
        p1_commit.map_or_else(Vec::new, |c| c.mutations.clone());

    let mut traces: BTreeMap<i64, GoldTraceMap> = BTreeMap::new();
    traces.insert(1, trace1);
    let mut manifests: BTreeMap<i64, PhaseManifestV5> = BTreeMap::new();
    manifests.insert(1, manifest1.clone());

    let actual_state2: Option<WorldStateV5> = if has_phase2_context {
        let m2 = manifest2.as_ref().expect("has_phase2_context");
        let p2_pub = public_phase2_state.as_ref().expect("has_phase2_context");
        let g2 = gold_state2.as_ref().expect("has_phase2_context");
        let ref_trace2 = evaluate_phase_with_trace(g2, &m2.work_queue, &[])?;
        traces.insert(2, convert_trace_map(&ref_trace2.trace_by_target));
        manifests.insert(2, m2.clone());

        if p1_commit.is_some() && mode == "COUPLED" {
            let induced = induce_phase2_world(
                &initial_state,
                &p1_mutations,
                &manifest1.work_queue,
                &episode_v5,
                None,
            )?;
            Some(phase2_context_from_induced(p2_pub, &induced)?)
        } else {
            Some(g2.clone())
        }
    } else {
        None
    };

    let assess = |snapshot: &RuntimeSnapshotV5,
                  boundary: &str,
                  trace_override: Option<&GoldTraceMap>|
     -> Result<(SnapshotAssessmentV5, Value), ProductionEvalError> {
        let mutations = &snapshot.mutations;
        let phase_index = snapshot.phase_index;
        let view = &snapshot.view;
        let work_queue = &manifests[&phase_index].work_queue;
        let required = required_mutation_keys(work_queue, phase_index);
        let target_set: BTreeSet<(String, String)> = required.into_iter().collect();
        let stage1_ok = completeness_ok(work_queue, phase_index, mutations);
        let fk_errors = structural_errors(view, phase_index, mutations, work_queue);
        let stage2_ok = fk_errors.is_empty();
        let mut stage2_valid = BTreeMap::new();
        for key in &target_set {
            let prefix = format!("{}:", key.1);
            stage2_valid.insert(key.clone(), !fk_errors.iter().any(|e| e.starts_with(&prefix)));
        }
        let target_sorted: Vec<(String, String)> = target_set.into_iter().collect();
        let trace_ref = trace_override.unwrap_or(&traces[&phase_index]);
        let verification = verify_phase(
            phase_index,
            &target_sorted,
            mutations,
            trace_ref,
            &snapshot.read_ledger,
            EvaluatePhaseOptions {
                stage1_ok,
                stage2_ok,
                stage2_valid_by_target: Some(&stage2_valid),
                preconditions: None,
                citations: None,
            },
        )?;
        let json_mutations = snapshot_mutation_rows(mutations)?;
        let event_seq = match boundary {
            "FIRST_STAGE" => first_stage_seq.get(&phase_index).copied(),
            "LAST_STAGE" => last_stage_seq.get(&phase_index).copied(),
            "COMMIT" => commit_seq.get(&phase_index).copied(),
            _ => None,
        }
        .ok_or_else(|| {
            ProductionEvalError::UnsupportedTrace(
                "runtime snapshot has no supplied stage or commit event".to_owned(),
            )
        })?;

        let mut delivered_ids = snapshot.delivered_slice_ids.clone();
        delivered_ids.sort();
        let mut diags = BTreeSet::new();
        for cat in &verification.violation_categories {
            diags.insert(violation_category_as_str(*cat));
        }

        let mut token_usage = Map::new();
        token_usage.insert(
            "input_tokens".to_owned(),
            Value::Number(Number::from(snapshot.token_totals.input_tokens_actual())),
        );
        token_usage.insert(
            "output_tokens".to_owned(),
            Value::Number(Number::from(snapshot.token_totals.output_tokens_actual())),
        );

        let mut row = Map::new();
        row.insert(
            "phase_index".to_owned(),
            Value::Number(Number::from(phase_index)),
        );
        row.insert("boundary".to_owned(), Value::String(boundary.to_owned()));
        row.insert("event_seq".to_owned(), Value::Number(Number::from(event_seq)));
        row.insert("mutations".to_owned(), json_mutations.clone());
        row.insert(
            "mutation_sha256".to_owned(),
            Value::String(sha256_json(&json_mutations)?),
        );
        row.insert(
            "read_ledger".to_owned(),
            Value::Array(
                snapshot
                    .read_ledger
                    .iter()
                    .cloned()
                    .map(Value::String)
                    .collect(),
            ),
        );
        row.insert(
            "delivered_slice_ids".to_owned(),
            Value::Array(delivered_ids.into_iter().map(Value::String).collect()),
        );
        row.insert("stage1_ok".to_owned(), Value::Bool(verification.stage1_ok));
        row.insert("stage2_ok".to_owned(), Value::Bool(verification.stage2_ok));
        row.insert("stage3_ok".to_owned(), Value::Bool(verification.stage3_ok));
        row.insert("stage4_ok".to_owned(), Value::Bool(verification.stage4_ok));
        row.insert("stage5_ok".to_owned(), Value::Bool(verification.stage5_ok));
        row.insert(
            "diagnostics".to_owned(),
            Value::Array(diags.into_iter().map(|s| Value::String(s.to_owned())).collect()),
        );
        row.insert("token_usage".to_owned(), Value::Object(token_usage));
        let row_val = Value::Object(row);
        let assessment = snapshot_assessment_from_verification(&verification, &row_val, None)?;
        Ok((assessment, row_val))
    };

    let boundary_assessments = |phase_index: i64| -> Result<(PhaseSnapshotSetV5, Vec<Value>), ProductionEvalError> {
        let snapshots = runtime
            .phase_snapshots(phase_index)
            .cloned()
            .unwrap_or_default();
        let mut first = None;
        let mut last = None;
        let mut commit = None;
        let mut derivs = Vec::new();
        if let Some(snap) = &snapshots.first_stage {
            let (a, row) = assess(snap, "FIRST_STAGE", None)?;
            first = Some(a);
            derivs.push(row);
        }
        if let Some(snap) = &snapshots.last_stage {
            let (a, row) = assess(snap, "LAST_STAGE", None)?;
            last = Some(a);
            derivs.push(row);
        }
        if let Some(snap) = &snapshots.commit {
            let (a, row) = assess(snap, "COMMIT", None)?;
            commit = Some(a);
            derivs.push(row);
        }
        Ok((
            PhaseSnapshotSetV5 {
                first_stage: first,
                commit,
                last_stage: last,
            },
            derivs,
        ))
    };

    let (phase1_scores, mut derivations1) = boundary_assessments(1)?;
    let (phase2_scores, mut derivations2) = if has_phase2_context {
        boundary_assessments(2)?
    } else {
        (PhaseSnapshotSetV5::default(), Vec::new())
    };

    let p1_committed = p1_snapshots.commit.is_some();
    let p2_snapshots = runtime
        .phase_snapshots(2)
        .cloned()
        .unwrap_or_default();
    let p2_committed = p2_snapshots.commit.is_some();
    let p2_opened = runtime.phase2_opened();

    let mut state_feasible: Option<bool> = None;
    let mut actual_p2_commit_pass: Option<bool> = None;
    if p1_committed && mode == "COUPLED" && has_phase2_context {
        let act2 = actual_state2.as_ref().expect("has_phase2_context");
        let m2 = manifest2.as_ref().expect("has_phase2_context");
        let feasibility = phase2_feasibility(act2, &m2.work_queue)?;
        state_feasible = Some(feasibility.feasible);
        if feasibility.feasible
            && let Some(p2_commit_snap) = &p2_snapshots.commit
        {
            let actual_trace_res = evaluate_phase_with_trace(act2, &m2.work_queue, &[])?;
            let actual_trace2 = convert_trace_map(&actual_trace_res.trace_by_target);
            let (actual_assessment, _) = assess(p2_commit_snap, "COMMIT", Some(&actual_trace2))?;
            actual_p2_commit_pass = Some(actual_assessment.full_pass());
        }
    }

    let p1_first = phase1_scores.first_stage.as_ref();
    let p2_first = phase2_scores.first_stage.as_ref();
    let p1_last = phase1_scores.last_stage.as_ref();
    let p2_last = phase2_scores.last_stage.as_ref();
    let p1_commit_assess = phase1_scores.commit.as_ref();
    let p2_commit_assess = phase2_scores.commit.as_ref();

    let g_first: Option<i64> = if !has_phase2_context {
        None
    } else {
        Some(i64::from(
            p1_first.is_some_and(SnapshotAssessmentV5::full_pass)
                && p2_first.is_some_and(SnapshotAssessmentV5::full_pass),
        ))
    };
    let g_commit = i64::from(
        p1_committed
            && p2_committed
            && p1_commit_assess.is_some_and(SnapshotAssessmentV5::full_pass)
            && p2_commit_assess.is_some_and(SnapshotAssessmentV5::full_pass),
    );
    let sem_commit = i64::from(
        p1_committed
            && p2_committed
            && p1_commit_assess.is_some_and(SnapshotAssessmentV5::semantic_pass)
            && p2_commit_assess.is_some_and(SnapshotAssessmentV5::semantic_pass),
    );
    let both_staged = p1_last.is_some() && p2_last.is_some();
    let action_last = match (p1_last, p2_last) {
        (Some(l1), Some(l2)) => i64::from(l1.action_pass() && l2.action_pass()),
        _ => 0,
    };
    let p1_action_last = i64::from(p1_last.is_some_and(SnapshotAssessmentV5::action_pass));
    let reference_p2_commit_pass = p2_commit_assess.is_some_and(SnapshotAssessmentV5::full_pass);
    let g_prop = g_prop_metric(
        mode,
        p1_committed,
        p2_committed,
        state_feasible,
        reference_p2_commit_pass,
        actual_p2_commit_pass,
    );

    let mut metrics = Map::new();
    metrics.insert(
        "p1_first_stage_snapshot_ok".to_owned(),
        Value::Number(Number::from(i64::from(
            p1_first.is_some_and(SnapshotAssessmentV5::full_pass),
        ))),
    );
    metrics.insert(
        "G-SRR@first_stage".to_owned(),
        match g_first {
            Some(v) => Value::Number(Number::from(v)),
            None => Value::Null,
        },
    );
    metrics.insert(
        "Sem-SRR@commit".to_owned(),
        Value::Number(Number::from(sem_commit)),
    );
    metrics.insert(
        "Action-SRR@last_stage".to_owned(),
        Value::Number(Number::from(action_last)),
    );
    metrics.insert("G-PROP".to_owned(), Value::Number(Number::from(g_prop)));
    metrics.insert("G-SRR".to_owned(), Value::Number(Number::from(g_commit)));
    metrics.insert(
        "p2_observed".to_owned(),
        Value::Number(Number::from(i64::from(p2_opened))),
    );
    metrics.insert(
        "both_phases_staged".to_owned(),
        Value::Number(Number::from(i64::from(both_staged))),
    );
    metrics.insert(
        "p1_staged".to_owned(),
        Value::Number(Number::from(i64::from(p1_last.is_some()))),
    );
    metrics.insert(
        "P1-Action-SRR@last_stage".to_owned(),
        Value::Number(Number::from(p1_action_last)),
    );
    metrics.insert(
        "repair_lift".to_owned(),
        match g_first {
            Some(gf) => Value::Number(Number::from(g_commit - gf)),
            None => Value::Null,
        },
    );

    let mut snapshot_derivations = Vec::with_capacity(derivations1.len() + derivations2.len());
    snapshot_derivations.append(&mut derivations1);
    snapshot_derivations.append(&mut derivations2);
    snapshot_derivations.sort_by(|a, b| {
        let key = |row: &Value| {
            (
                row["phase_index"].as_i64().unwrap_or(0),
                row["boundary"].as_str().unwrap_or("").to_owned(),
                row["event_seq"].as_i64().unwrap_or(0),
            )
        };
        key(a).cmp(&key(b))
    });

    let snapshot_coverage: Vec<Value> = snapshot_derivations
        .iter()
        .map(|row| {
            let mut m = Map::new();
            m.insert("case_id".to_owned(), Value::String(case.case_id.clone()));
            m.insert("phase_index".to_owned(), row["phase_index"].clone());
            m.insert("boundary".to_owned(), row["boundary"].clone());
            m.insert("event_seq".to_owned(), row["event_seq"].clone());
            Value::Object(m)
        })
        .collect();

    let mut mat_map = Map::new();
    mat_map.insert(
        "event_plans".to_owned(),
        Value::Array(materialized_events),
    );
    let materialization = Value::Object(mat_map);

    let mut payload = Map::new();
    payload.insert("metrics".to_owned(), Value::Object(metrics));
    payload.insert(
        "snapshot_derivations".to_owned(),
        Value::Array(snapshot_derivations),
    );

    let rules = materialized_base_rule_prefixes(&materialization);
    let mut coverage = Map::new();
    coverage.insert("phase_targets".to_owned(), Value::Array(Vec::new()));
    coverage.insert("diagnostics".to_owned(), Value::Array(Vec::new()));
    coverage.insert(
        "rules".to_owned(),
        Value::Array(rules.into_iter().map(Value::String).collect()),
    );
    coverage.insert("safety_cases".to_owned(), Value::Array(Vec::new()));
    coverage.insert("snapshots".to_owned(), Value::Array(snapshot_coverage));

    Ok((Value::Object(payload), Value::Object(coverage), materialization))
}

enum EvalOutcome {
    Done(Value),
    Ready {
        payload: Value,
        coverage: Value,
        materialization: Value,
        gaps: Vec<Value>,
        status: &'static str,
    },
}

fn materialize_recipe_for_submission(
    validated: &PublicCase,
) -> Result<Result<Value, Value>, ProductionEvalError> {
    if !validated.stimulus.contains_key("recipe") {
        return Ok(Ok(Value::Null));
    }
    match materialize_source(validated, &validated.stimulus) {
        Ok((_cand, mat)) => Ok(Ok(mat.map(Value::Object).unwrap_or(Value::Null))),
        Err(ProductionEvalError::PatchPathAbsent(detail)) => Ok(Err(empty_result(
            validated,
            "underdefined",
            "PATCH_PATH_ABSENT",
            "/stimulus/recipe/patches",
            &detail,
        )?)),
        Err(ProductionEvalError::Unmaterializable(detail)) => Ok(Err(empty_result(
            validated,
            "underdefined",
            "UNMATERIALIZABLE_INPUT",
            "/stimulus/recipe",
            &detail,
        )?)),
        Err(err) => Err(err),
    }
}

fn evaluate_validated_case(validated: &PublicCase) -> Result<Value, ProductionEvalError> {
    let input_sha256 = sha256_bytes(&canonical_json_bytes(&validated.to_json_value())?);

    let outcome: Result<EvalOutcome, ProductionEvalError> = (|| {
        match validated.operation.as_str() {
            "derive_phase" => {
                let (payload, coverage) = derive_op(validated)?;
                Ok(EvalOutcome::Ready {
                    payload,
                    coverage,
                    materialization: Value::Null,
                    gaps: Vec::new(),
                    status: "ok",
                })
            }
            "validate_submission" => {
                let payload = validate_submission_op(validated)?;
                let materialized = match materialize_recipe_for_submission(validated)? {
                    Ok(mat) => mat,
                    Err(done) => return Ok(EvalOutcome::Done(done)),
                };
                let mut coverage = Map::new();
                coverage.insert("phase_targets".to_owned(), Value::Array(Vec::new()));
                coverage.insert("diagnostics".to_owned(), payload["diagnostics"].clone());
                let rules = materialized_base_rule_prefixes(&materialized);
                coverage.insert(
                    "rules".to_owned(),
                    Value::Array(rules.into_iter().map(Value::String).collect()),
                );
                coverage.insert("safety_cases".to_owned(), Value::Array(Vec::new()));
                coverage.insert("snapshots".to_owned(), Value::Array(Vec::new()));
                Ok(EvalOutcome::Ready {
                    payload,
                    coverage: Value::Object(coverage),
                    materialization: materialized,
                    gaps: Vec::new(),
                    status: "ok",
                })
            }
            "safety" => {
                let (payload, mut coverage, notes) = safety_op(validated)?;
                let materialized = match materialize_recipe_for_submission(validated)? {
                    Ok(mat) => mat,
                    Err(done) => return Ok(EvalOutcome::Done(done)),
                };
                let rules = materialized_base_rule_prefixes(&materialized);
                if let Some(cov_obj) = coverage.as_object_mut() {
                    cov_obj.insert(
                        "rules".to_owned(),
                        Value::Array(rules.into_iter().map(Value::String).collect()),
                    );
                }
                if let Some(first_note) = notes.first() {
                    let mut gap = Map::new();
                    gap.insert(
                        "code".to_owned(),
                        Value::String("UNDERDEFINED_PUBLIC_INPUT".to_owned()),
                    );
                    gap.insert("operation".to_owned(), Value::String("safety".to_owned()));
                    gap.insert(
                        "case_id".to_owned(),
                        Value::String(validated.case_id.clone()),
                    );
                    gap.insert("path".to_owned(), Value::String(json_pointer("stimulus")));
                    gap.insert("detail".to_owned(), Value::String(first_note.clone()));
                    Ok(EvalOutcome::Ready {
                        payload,
                        coverage,
                        materialization: materialized,
                        gaps: vec![Value::Object(gap)],
                        status: "underdefined",
                    })
                } else {
                    Ok(EvalOutcome::Ready {
                        payload,
                        coverage,
                        materialization: materialized,
                        gaps: Vec::new(),
                        status: "ok",
                    })
                }
            }
            _ => {
                let mut trace_gaps: Vec<Value> = Vec::new();
                let (payload, coverage, materialization) =
                    match score_trace_op(validated, Some(&mut trace_gaps)) {
                        Ok(res) => res,
                        Err(ProductionEvalError::TraceContextMissing { path, detail }) => {
                            return Ok(EvalOutcome::Done(empty_result(
                                validated,
                                "underdefined",
                                "UNDERDEFINED_PUBLIC_INPUT",
                                &path,
                                &detail,
                            )?));
                        }
                        Err(ProductionEvalError::Unmaterializable(detail)) => {
                            return Ok(EvalOutcome::Done(empty_result(
                                validated,
                                "underdefined",
                                "UNMATERIALIZABLE_INPUT",
                                "/stimulus/events",
                                &detail,
                            )?));
                        }
                        Err(ProductionEvalError::PatchPathAbsent(detail)) => {
                            return Ok(EvalOutcome::Done(empty_result(
                                validated,
                                "underdefined",
                                "PATCH_PATH_ABSENT",
                                "/stimulus/events",
                                &detail,
                            )?));
                        }
                        Err(ProductionEvalError::UnsupportedTrace(detail)) => {
                            return Ok(EvalOutcome::Done(empty_result(
                                validated,
                                "unsupported",
                                "UNSUPPORTED_OPERATION",
                                "/stimulus/events",
                                &detail,
                            )?));
                        }
                        Err(err) => return Err(err),
                    };

                if payload["metrics"]["G-SRR@first_stage"].is_null()
                    || payload["metrics"]["repair_lift"].is_null()
                {
                    let mut gap = Map::new();
                    gap.insert(
                        "code".to_owned(),
                        Value::String("UNDERDEFINED_PUBLIC_INPUT".to_owned()),
                    );
                    gap.insert(
                        "operation".to_owned(),
                        Value::String("score_trace".to_owned()),
                    );
                    gap.insert(
                        "case_id".to_owned(),
                        Value::String(validated.case_id.clone()),
                    );
                    gap.insert(
                        "path".to_owned(),
                        Value::String("/public_episode".to_owned()),
                    );
                    gap.insert(
                        "detail".to_owned(),
                        Value::String(
                            "first-stage scenario eligibility is unresolved by the public contract; G-SRR@first_stage and repair_lift are uncomputed without P2 context".to_owned(),
                        ),
                    );
                    trace_gaps.push(Value::Object(gap));
                }
                let status = if trace_gaps.is_empty() {
                    "ok"
                } else {
                    "underdefined"
                };
                Ok(EvalOutcome::Ready {
                    payload,
                    coverage,
                    materialization,
                    gaps: trace_gaps,
                    status,
                })
            }
        }
    })();

    let (payload, coverage, materialization, gaps, status) = match outcome {
        Ok(EvalOutcome::Done(done)) => return Ok(done),
        Ok(EvalOutcome::Ready {
            payload,
            coverage,
            materialization,
            gaps,
            status,
        }) => (payload, coverage, materialization, gaps, status),
        Err(ProductionEvalError::PatchPathAbsent(detail)) => {
            return empty_result(
                validated,
                "underdefined",
                "PATCH_PATH_ABSENT",
                "/stimulus",
                &detail,
            );
        }
        Err(ProductionEvalError::Unmaterializable(detail)) => {
            return empty_result(
                validated,
                "underdefined",
                "UNMATERIALIZABLE_INPUT",
                "/stimulus",
                &detail,
            );
        }
        Err(err) => return Err(err),
    };

    let mut out = Map::new();
    out.insert("protocol".to_owned(), Value::String(PROTOCOL.to_owned()));
    out.insert(
        "evaluator_id".to_owned(),
        Value::String("production".to_owned()),
    );
    out.insert("case_id".to_owned(), Value::String(validated.case_id.clone()));
    out.insert("input_sha256".to_owned(), Value::String(input_sha256));
    out.insert("status".to_owned(), Value::String(status.to_owned()));
    out.insert("payload".to_owned(), payload);
    out.insert("materialization".to_owned(), materialization);
    out.insert("coverage".to_owned(), coverage);
    out.insert("gaps".to_owned(), Value::Array(gaps));
    Ok(Value::Object(out))
}

/// Evaluate one canonical public case without consulting other outputs.
///
/// Exact mirror of `identitybench.v5.conformance_production.evaluate_shared_input`.
pub fn evaluate_shared_input(case: &Value) -> Result<Value, ProductionEvalError> {
    let validated = validate_public_case(case)?;
    evaluate_validated_case(&validated)
}

/// Evaluate one batch of canonical public cases (CLI-wire array entrypoint).
pub fn evaluate_shared_input_batch(cases: &[Value]) -> Result<Vec<Value>, ProductionEvalError> {
    let mut results = Vec::with_capacity(cases.len());
    for case in cases {
        results.push(evaluate_shared_input(case)?);
    }
    Ok(results)
}

/// Evaluate a pre-validated [`PublicCase`] and return a validated [`EvaluatorResult`].
pub fn evaluate_public_case(case: &PublicCase) -> Result<EvaluatorResult, ProductionEvalError> {
    let frozen = freeze_case(&case.to_json_value())?;
    evaluate_frozen_case(&frozen)
}

/// Evaluate a [`FrozenCase`] and return a validated [`EvaluatorResult`].
pub fn evaluate_frozen_case(case: &FrozenCase) -> Result<EvaluatorResult, ProductionEvalError> {
    let raw = evaluate_validated_case(&case.public_case)?;
    Ok(validate_evaluator_result(&raw, case, "production")?)
}
