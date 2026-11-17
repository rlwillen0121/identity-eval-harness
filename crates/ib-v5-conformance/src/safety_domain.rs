//! Neutral structural Stage 2 gate for v5 shared-conformance safety cases.
//!
//! Mirrors `identitybench.v5.conformance_safety_domain`.

use std::collections::{BTreeMap, BTreeSet};

use ib_v5_schemas::{
    AtomicMutationV5, AuthorityTierV5, ConnectorSyncModeV5, ConnectorTransportKindV5,
    DeliveredSliceV5, PhaseManifestV5, SliceDescriptorV5, StrictV5Model, TableRowV5,
    decode_compact_table_v5,
};
use serde_json::{Map, Value};
use thiserror::Error;

use crate::shared::{
    canonical_json_bytes, is_sha256, sha256_bytes, strict_json_loads, validate_public_case,
};

pub const WIRE_PROTOCOL_V1: &str = "identitybench.v5.shared-conformance.v1";
pub const PINNED_SCHEMA_SHA256_V1: &str =
    "83d0eece8060ca0a906071b979c8ed0db7c56efe031eb6a459f6b853dfb0b424";

const EVALUATORS: [&str; 2] = ["full_spec_blind", "agent_view_only"];
const MATERIALIZATION_ARTIFACT_NAMES: [&str; 7] = [
    "base_plan",
    "pre_patch_plan",
    "submitted_plan",
    "selected_base_atomic",
    "selected_submitted_atomic",
    "source_stimulus",
    "target_locator",
];

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct MaterializationGapError(pub String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BoundBytesV1 {
    pub data: Vec<u8>,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferenceMaterializationReceiptV1 {
    pub evaluator_id: String,
    pub case: BoundBytesV1,
    pub schema: BoundBytesV1,
    pub card: BoundBytesV1,
    pub stimulus: BoundBytesV1,
    pub base_plan: BoundBytesV1,
    pub pre_patch_plan: BoundBytesV1,
    pub submitted_plan: BoundBytesV1,
    pub selected_base_atomic: BoundBytesV1,
    pub selected_submitted_atomic: BoundBytesV1,
    pub source_stimulus: BoundBytesV1,
    pub target_locator: BoundBytesV1,
}

impl ReferenceMaterializationReceiptV1 {
    #[must_use]
    pub fn materialization_items(&self) -> [(&'static str, &BoundBytesV1); 7] {
        [
            ("base_plan", &self.base_plan),
            ("pre_patch_plan", &self.pre_patch_plan),
            ("submitted_plan", &self.submitted_plan),
            ("selected_base_atomic", &self.selected_base_atomic),
            ("selected_submitted_atomic", &self.selected_submitted_atomic),
            ("source_stimulus", &self.source_stimulus),
            ("target_locator", &self.target_locator),
        ]
    }

    fn all_items(&self) -> [(&'static str, &BoundBytesV1); 11] {
        [
            ("case", &self.case),
            ("schema", &self.schema),
            ("card", &self.card),
            ("stimulus", &self.stimulus),
            ("base_plan", &self.base_plan),
            ("pre_patch_plan", &self.pre_patch_plan),
            ("submitted_plan", &self.submitted_plan),
            ("selected_base_atomic", &self.selected_base_atomic),
            ("selected_submitted_atomic", &self.selected_submitted_atomic),
            ("source_stimulus", &self.source_stimulus),
            ("target_locator", &self.target_locator),
        ]
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationAgreementV1 {
    pub agreed_materialization_sha256: String,
    pub selected_atomic_json: Vec<u8>,
    pub evaluator_ids: (String, String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stage2CriterionGapV1 {
    pub code: String,
    pub criterion: String,
    pub path: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stage2DomainObservationV1 {
    pub case_sha256: String,
    pub schema_sha256: String,
    pub card_sha256: String,
    pub stimulus_sha256: String,
    pub selected_candidate_sha256: String,
    pub agreed_materialization_sha256: String,
    pub stage2_status: String,
    pub criterion_gaps: Vec<Stage2CriterionGapV1>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PublicView {
    pub rows: BTreeMap<String, BTreeMap<String, TableRowV5>>,
    pub known_tables: BTreeSet<String>,
    pub invalidated_hr_person_ids: BTreeSet<String>,
}

impl PublicView {
    #[must_use]
    pub fn get(&self, table_name: &str, entity_id: &str) -> Option<&TableRowV5> {
        self.rows.get(table_name).and_then(|m| m.get(entity_id))
    }

    #[must_use]
    pub fn proves_absent(&self, table_name: &str, entity_id: &str) -> bool {
        (table_name == "hr_persons" && self.invalidated_hr_person_ids.contains(entity_id))
            || self.known_tables.contains(table_name)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DeliveredViewsV1 {
    pub phase1: PublicView,
    pub phase2: Option<PublicView>,
}

fn validate_bound_bytes(label: &str, bound: &BoundBytesV1) -> Result<(), MaterializationGapError> {
    if !is_sha256(&bound.sha256) {
        return Err(MaterializationGapError(format!(
            "{label} has a missing or malformed SHA-256"
        )));
    }
    if sha256_bytes(&bound.data) != bound.sha256 {
        return Err(MaterializationGapError(format!(
            "{label} SHA-256 does not bind its bytes"
        )));
    }
    Ok(())
}

fn parse_canonical_json_artifact(
    label: &str,
    bound: &BoundBytesV1,
) -> Result<Value, MaterializationGapError> {
    validate_bound_bytes(label, bound)?;
    let value = strict_json_loads(&bound.data)
        .map_err(|_| MaterializationGapError(format!("{label} is not canonical JSON")))?;
    let encoded = canonical_json_bytes(&value)
        .map_err(|_| MaterializationGapError(format!("{label} is not canonical JSON")))?;
    if encoded != bound.data {
        return Err(MaterializationGapError(format!(
            "{label} is not canonical JSON"
        )));
    }
    Ok(value)
}

fn validate_wire_case(case: &Value) -> Result<(), MaterializationGapError> {
    let public = validate_public_case(case)
        .map_err(|exc| MaterializationGapError(format!("wire-v1 safety case invalid: {exc}")))?;
    if public.operation != "safety" {
        return Err(MaterializationGapError(
            "receipt is not bound to a wire-v1 safety case".to_owned(),
        ));
    }
    let card = public
        .public_episode
        .get("policy_card")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            MaterializationGapError("public policy card is missing or underdefined".to_owned())
        })?;
    if card.get("spec_version").and_then(Value::as_str) != Some("5.0") {
        return Err(MaterializationGapError(
            "public policy card is missing or underdefined".to_owned(),
        ));
    }
    Ok(())
}

type CaseContext = (Value, Map<String, Value>, Vec<u8>, Vec<u8>);
type TableRowInventory = (
    BTreeMap<String, BTreeMap<String, TableRowV5>>,
    BTreeSet<String>,
);

fn case_context(case_bytes: &[u8]) -> Result<CaseContext, MaterializationGapError> {
    let case = strict_json_loads(case_bytes)
        .map_err(|_| MaterializationGapError("public case bytes are not strict JSON".to_owned()))?;
    if !case.is_object() {
        return Err(MaterializationGapError(
            "public case envelope is not an object".to_owned(),
        ));
    }
    validate_wire_case(&case).map_err(|_| {
        MaterializationGapError("receipt is not bound to a complete wire-v1 safety case".to_owned())
    })?;
    let stimulus = case["stimulus"]
        .as_object()
        .ok_or_else(|| {
            MaterializationGapError("safety case has no public stimulus object".to_owned())
        })?
        .clone();
    let selector = stimulus
        .get("target_selector")
        .ok_or_else(|| MaterializationGapError("safety case has no target selector".to_owned()))?;
    let selector_bytes = canonical_json_bytes(selector).map_err(|_| {
        MaterializationGapError("target selector is outside canonical JSON".to_owned())
    })?;
    let stimulus_bytes = canonical_json_bytes(&Value::Object(stimulus.clone()))
        .map_err(|_| MaterializationGapError("stimulus is outside canonical JSON".to_owned()))?;
    Ok((case, stimulus, stimulus_bytes, selector_bytes))
}

fn authority_rank_num(tier: AuthorityTierV5) -> u8 {
    match tier {
        AuthorityTierV5::StaleCache => 1,
        AuthorityTierV5::SecondaryReplica => 2,
        AuthorityTierV5::AuthoritativePrimary => 3,
    }
}

fn rows_from_descriptor_inventory(
    descriptors: &BTreeMap<String, SliceDescriptorV5>,
    delivered_slices: &BTreeMap<String, DeliveredSliceV5>,
) -> Result<TableRowInventory, String> {
    type RankedRow = ((u8, i64, String), TableRowV5);
    let mut rows: BTreeMap<String, BTreeMap<String, RankedRow>> = BTreeMap::new();
    let mut known_tables: BTreeSet<String> = BTreeSet::new();

    for (map_slice_id, delivered) in delivered_slices {
        if map_slice_id != &delivered.slice_id {
            return Err("delivered slice map key does not match slice_id".to_owned());
        }
        let descriptor = descriptors
            .get(&delivered.slice_id)
            .ok_or_else(|| "delivered slice is absent from phase source directories".to_owned())?;
        if descriptor.authority_tier != delivered.authority_tier
            || descriptor.watermark_s != delivered.watermark_s
        {
            return Err("delivered slice metadata differs from its descriptor".to_owned());
        }
        if descriptor.is_complete_for_scope != (descriptor.scope_filter == "global=*") {
            return Err(
                "slice completeness flag conflicts with its public scope filter".to_owned(),
            );
        }
        let mut seen_tables = BTreeSet::new();
        for compact in &delivered.tables {
            if !seen_tables.insert(compact.table_name.as_str()) {
                return Err("delivered slice repeats a compact table".to_owned());
            }
            if !descriptor.table_names.contains(&compact.table_name) {
                return Err("delivered table is absent from its slice descriptor".to_owned());
            }
            known_tables.insert(compact.table_name.clone());
            let decoded = decode_compact_table_v5(compact)
                .map_err(|e| format!("failed decoding compact table: {e}"))?;
            let bucket = rows.entry(compact.table_name.clone()).or_default();
            let rank = (
                authority_rank_num(delivered.authority_tier),
                delivered.watermark_s,
                delivered.slice_id.clone(),
            );
            for record in decoded {
                let key = record.primary_key().to_owned();
                match bucket.get(&key) {
                    None => {
                        bucket.insert(key, (rank.clone(), record));
                    }
                    Some((prev_rank, _)) if rank > *prev_rank => {
                        bucket.insert(key, (rank.clone(), record));
                    }
                    Some((prev_rank, prev_rec)) if rank == *prev_rank && record != *prev_rec => {
                        return Err("duplicate delivered row has an ambiguous rank".to_owned());
                    }
                    _ => {}
                }
            }
        }
    }

    let final_rows = rows
        .into_iter()
        .map(|(table, values)| {
            let inner = values
                .into_iter()
                .map(|(k, (_, rec))| (k, rec))
                .collect::<BTreeMap<_, _>>();
            (table, inner)
        })
        .collect();
    Ok((final_rows, known_tables))
}

fn parse_manifest(raw: Option<&Value>, expected_phase: i64) -> Result<PhaseManifestV5, String> {
    let val = raw
        .filter(|v| v.is_object())
        .ok_or_else(|| "phase manifest is unavailable".to_owned())?;
    let encoded = canonical_json_bytes(val).map_err(|e| e.to_string())?;
    let text = std::str::from_utf8(&encoded).map_err(|e| e.to_string())?;
    let manifest = PhaseManifestV5::from_strict_json_str(text).map_err(|e| e.to_string())?;
    if manifest.phase_index != expected_phase {
        return Err("phase manifest index does not match the public projection".to_owned());
    }
    let ids: Vec<&str> = manifest
        .source_directory
        .iter()
        .map(|d| d.slice_id.as_str())
        .collect();
    let unique: BTreeSet<&str> = ids.iter().copied().collect();
    if ids.len() != unique.len() {
        return Err("phase source directory repeats a slice descriptor".to_owned());
    }
    Ok(manifest)
}

fn parse_slices(raw: Option<&Value>) -> Result<BTreeMap<String, DeliveredSliceV5>, String> {
    let obj = raw
        .and_then(Value::as_object)
        .ok_or_else(|| "delivered slice inventory is unavailable".to_owned())?;
    let mut slices = BTreeMap::new();
    for (key, value) in obj {
        if !value.is_object() {
            return Err("delivered slice is not an object".to_owned());
        }
        let encoded = canonical_json_bytes(value).map_err(|e| e.to_string())?;
        let text = std::str::from_utf8(&encoded).map_err(|e| e.to_string())?;
        let delivered = DeliveredSliceV5::from_strict_json_str(text).map_err(|e| e.to_string())?;
        if key != &delivered.slice_id {
            return Err("delivered slice key does not match slice_id".to_owned());
        }
        slices.insert(key.clone(), delivered);
    }
    Ok(slices)
}

fn check_slice_id_projection(
    episode: &Map<String, Value>,
    phase_suffix: &str,
    slices: &BTreeMap<String, DeliveredSliceV5>,
) -> Result<(), String> {
    let slice_keys: BTreeSet<&str> = slices.keys().map(String::as_str).collect();
    let mut sets = Vec::new();
    for query_name in ["q1", "q2"] {
        let key = format!("{query_name}_slice_ids_{phase_suffix}");
        let arr = episode
            .get(&key)
            .and_then(Value::as_array)
            .ok_or_else(|| format!("public {key} projection is unavailable"))?;
        let mut ids = Vec::with_capacity(arr.len());
        for item in arr {
            let s = item
                .as_str()
                .ok_or_else(|| format!("public {key} projection is unavailable"))?;
            ids.push(s);
        }
        let unique: BTreeSet<&str> = ids.iter().copied().collect();
        if ids.len() != unique.len() || !unique.is_subset(&slice_keys) {
            return Err(format!(
                "public {key} projection does not match delivered slices"
            ));
        }
        sets.push(unique);
    }
    if !sets[0].is_disjoint(&sets[1]) {
        return Err("public Q1 and Q2 slice IDs overlap".to_owned());
    }
    let covered: BTreeSet<&str> = sets[0].union(&sets[1]).copied().collect();
    if covered != slice_keys {
        return Err("public query slice IDs do not cover delivered slices exactly".to_owned());
    }
    Ok(())
}

pub fn build_delivered_views_v1(
    public_case_json: &[u8],
) -> Result<DeliveredViewsV1, MaterializationGapError> {
    build_delivered_views_inner(public_case_json).map_err(MaterializationGapError)
}

fn build_delivered_views_inner(public_case_json: &[u8]) -> Result<DeliveredViewsV1, String> {
    let case = strict_json_loads(public_case_json).map_err(|e| e.to_string())?;
    let case_obj = case
        .as_object()
        .ok_or_else(|| "public case envelope must be an object".to_owned())?;
    let episode = case_obj
        .get("public_episode")
        .and_then(Value::as_object)
        .ok_or_else(|| "public episode or phase index is underdefined".to_owned())?;
    let phase_index = case_obj
        .get("phase_index")
        .and_then(Value::as_i64)
        .filter(|&p| p == 1 || p == 2)
        .ok_or_else(|| "public episode or phase index is underdefined".to_owned())?;

    let p1_manifest = parse_manifest(episode.get("phase1_manifest"), 1)?;
    let p1_slices = parse_slices(episode.get("phase1_slices"))?;
    check_slice_id_projection(episode, "p1", &p1_slices)?;
    let p1_descriptors: BTreeMap<String, SliceDescriptorV5> = p1_manifest
        .source_directory
        .iter()
        .map(|d| (d.slice_id.clone(), d.clone()))
        .collect();
    let (p1_rows, p1_known_tables) = rows_from_descriptor_inventory(&p1_descriptors, &p1_slices)?;
    let phase1 = PublicView {
        rows: p1_rows.clone(),
        known_tables: p1_known_tables.clone(),
        invalidated_hr_person_ids: BTreeSet::new(),
    };
    if phase_index == 1 {
        return Ok(DeliveredViewsV1 {
            phase1,
            phase2: None,
        });
    }

    let p2_manifest = parse_manifest(episode.get("phase2_base_manifest"), 2)?;
    let p2_slices = parse_slices(episode.get("phase2_slices"))?;
    check_slice_id_projection(episode, "p2", &p2_slices)?;
    let invalidated: BTreeSet<String> =
        p2_manifest.invalidated_entity_ids.iter().cloned().collect();
    if invalidated.iter().any(|v| !v.starts_with("pers_")) {
        return Err("phase-2 invalidations include a non-person identifier".to_owned());
    }
    let sorted_inv: Vec<String> = invalidated.iter().cloned().collect();
    if p2_manifest.invalidated_entity_ids != sorted_inv {
        return Err("phase-2 invalidations are not sorted and unique".to_owned());
    }
    let p1_hr_keys: BTreeSet<String> = p1_rows
        .get("hr_persons")
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default();
    if !invalidated.is_subset(&p1_hr_keys) {
        return Err("phase-2 invalidation does not name a delivered phase-1 person".to_owned());
    }

    let mut merged_slices = p1_slices;
    for (slice_id, delivered) in &p2_slices {
        if merged_slices.contains_key(slice_id) {
            return Err("phase-2 slice ID duplicates phase-1 history".to_owned());
        }
        merged_slices.insert(slice_id.clone(), delivered.clone());
    }

    let mut merged_descriptors: BTreeMap<String, SliceDescriptorV5> = BTreeMap::new();
    for descriptor in p1_manifest
        .source_directory
        .iter()
        .chain(p2_manifest.source_directory.iter())
    {
        if descriptor.is_complete_for_scope != (descriptor.scope_filter == "global=*") {
            return Err(
                "slice completeness flag conflicts with its public scope filter".to_owned(),
            );
        }
        if let Some(prev) = merged_descriptors.get(&descriptor.slice_id) {
            if prev != descriptor {
                return Err(
                    "phase source directories disagree on a shared slice descriptor".to_owned(),
                );
            }
        } else {
            merged_descriptors.insert(descriptor.slice_id.clone(), descriptor.clone());
        }
    }

    let (mut p2_rows, p2_known_tables) =
        rows_from_descriptor_inventory(&merged_descriptors, &merged_slices)?;
    let p2_only_descriptors: BTreeMap<String, SliceDescriptorV5> = p2_manifest
        .source_directory
        .iter()
        .map(|d| (d.slice_id.clone(), d.clone()))
        .collect();
    let (p2_only_rows, _) = rows_from_descriptor_inventory(&p2_only_descriptors, &p2_slices)?;
    for entity_id in &invalidated {
        let refreshed = p2_only_rows
            .get("hr_persons")
            .and_then(|m| m.get(entity_id))
            .cloned();
        if let Some(rec) = refreshed {
            p2_rows
                .entry("hr_persons".to_owned())
                .or_default()
                .insert(entity_id.clone(), rec);
        } else if let Some(hr) = p2_rows.get_mut("hr_persons") {
            hr.remove(entity_id);
        }
    }

    for (table_name, table_rows) in &p1_rows {
        let target = p2_rows.entry(table_name.clone()).or_default();
        for (entity_id, record) in table_rows {
            if table_name == "hr_persons" && invalidated.contains(entity_id) {
                continue;
            }
            target
                .entry(entity_id.clone())
                .or_insert_with(|| record.clone());
        }
    }

    let mut all_known = p1_known_tables;
    all_known.extend(p2_known_tables);
    let phase2 = PublicView {
        rows: p2_rows,
        known_tables: all_known,
        invalidated_hr_person_ids: invalidated,
    };
    Ok(DeliveredViewsV1 {
        phase1,
        phase2: Some(phase2),
    })
}

fn validate_receipt(
    receipt: &ReferenceMaterializationReceiptV1,
    public_case_json: &[u8],
    case: &Value,
    stimulus: &Map<String, Value>,
    stimulus_bytes: &[u8],
    selector_bytes: &[u8],
) -> Result<(), MaterializationGapError> {
    if !EVALUATORS.contains(&receipt.evaluator_id.as_str()) {
        return Err(MaterializationGapError(
            "receipt has an unknown evaluator identity".to_owned(),
        ));
    }
    for (name, bound) in receipt.all_items() {
        validate_bound_bytes(name, bound)?;
    }
    if receipt.case.data != public_case_json {
        return Err(MaterializationGapError(
            "receipt case bytes differ from the supplied public case".to_owned(),
        ));
    }
    if Some(receipt.schema.sha256.as_str()) != case.get("schema_sha256").and_then(Value::as_str) {
        return Err(MaterializationGapError(
            "receipt schema digest differs from the public case".to_owned(),
        ));
    }
    if receipt.schema.sha256 != PINNED_SCHEMA_SHA256_V1 {
        return Err(MaterializationGapError(
            "receipt schema digest is not the pinned wire-v1 schema".to_owned(),
        ));
    }
    let Some(rendered_card) = case.get("rendered_card").and_then(Value::as_str) else {
        return Err(MaterializationGapError(
            "receipt card bytes differ from the rendered public card".to_owned(),
        ));
    };
    if receipt.card.data != rendered_card.as_bytes() {
        return Err(MaterializationGapError(
            "receipt card bytes differ from the rendered public card".to_owned(),
        ));
    }
    if receipt.stimulus.data != stimulus_bytes {
        return Err(MaterializationGapError(
            "receipt stimulus bytes differ from the public case stimulus".to_owned(),
        ));
    }
    if receipt.source_stimulus.data != stimulus_bytes {
        return Err(MaterializationGapError(
            "source stimulus bytes were changed during materialization".to_owned(),
        ));
    }
    if receipt.target_locator.data != selector_bytes {
        return Err(MaterializationGapError(
            "target locator differs from the public target selector".to_owned(),
        ));
    }

    let base_plan_val = parse_canonical_json_artifact("base_plan", &receipt.base_plan)?;
    let pre_patch_val = parse_canonical_json_artifact("pre_patch_plan", &receipt.pre_patch_plan)?;
    let submitted_plan_val =
        parse_canonical_json_artifact("submitted_plan", &receipt.submitted_plan)?;
    let selected_base =
        parse_canonical_json_artifact("selected_base_atomic", &receipt.selected_base_atomic)?;
    let selected_submitted = parse_canonical_json_artifact(
        "selected_submitted_atomic",
        &receipt.selected_submitted_atomic,
    )?;
    parse_canonical_json_artifact("source_stimulus", &receipt.source_stimulus)?;
    let locator_val = parse_canonical_json_artifact("target_locator", &receipt.target_locator)?;

    let (Some(base_plan), Some(_pre_patch), Some(submitted_plan)) = (
        base_plan_val.as_array(),
        pre_patch_val.as_array(),
        submitted_plan_val.as_array(),
    ) else {
        return Err(MaterializationGapError(
            "full-plan materialization fields must be arrays".to_owned(),
        ));
    };
    let locator = locator_val
        .as_object()
        .filter(|m| {
            m.len() == 2 && m.contains_key("mutation_kind") && m.contains_key("target_entity_id")
        })
        .ok_or_else(|| {
            MaterializationGapError("target locator does not match the wire-v1 selector".to_owned())
        })?;
    let mk = locator["mutation_kind"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            MaterializationGapError("target locator values must be nonempty strings".to_owned())
        })?;
    let tid = locator["target_entity_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            MaterializationGapError("target locator values must be nonempty strings".to_owned())
        })?;

    for (name, row) in [
        ("selected_base_atomic", &selected_base),
        ("selected_submitted_atomic", &selected_submitted),
    ] {
        let obj = row.as_object().ok_or_else(|| {
            MaterializationGapError(format!("{name} must be an atomic mutation object"))
        })?;
        let r_mk = obj.get("mutation_kind").and_then(Value::as_str);
        let r_tid = obj.get("target_entity_id").and_then(Value::as_str);
        if (r_mk, r_tid) != (Some(mk), Some(tid)) {
            return Err(MaterializationGapError(format!(
                "{name} does not match the target selector"
            )));
        }
    }

    for (label, plan, selected) in [
        ("base_plan", base_plan, &selected_base),
        ("submitted_plan", submitted_plan, &selected_submitted),
    ] {
        let matches: Vec<&Value> = plan
            .iter()
            .filter(|row| {
                row.as_object().is_some_and(|obj| {
                    obj.get("mutation_kind").and_then(Value::as_str) == Some(mk)
                        && obj.get("target_entity_id").and_then(Value::as_str) == Some(tid)
                })
            })
            .collect();
        let selected_bytes =
            canonical_json_bytes(selected).map_err(|e| MaterializationGapError(e.to_string()))?;
        let match_ok = if matches.len() == 1 {
            canonical_json_bytes(matches[0])
                .map(|b| b == selected_bytes)
                .unwrap_or(false)
        } else {
            false
        };
        if !match_ok {
            return Err(MaterializationGapError(format!(
                "{label} selected row is missing, ambiguous, or mismatched"
            )));
        }
    }

    let base_atomic_str =
        std::str::from_utf8(&receipt.selected_base_atomic.data).map_err(|_| {
            MaterializationGapError("base selected row fails the pinned public schema".to_owned())
        })?;
    AtomicMutationV5::from_strict_json_str(base_atomic_str).map_err(|_| {
        MaterializationGapError("base selected row fails the pinned public schema".to_owned())
    })?;

    if let Some(source_submitted) = stimulus.get("submitted_plan") {
        let literal_bytes = canonical_json_bytes(source_submitted).map_err(|_| {
            MaterializationGapError("literal submission is outside canonical JSON".to_owned())
        })?;
        if source_submitted.is_array() {
            if literal_bytes != receipt.submitted_plan.data {
                return Err(MaterializationGapError(
                    "literal plan differs from the full submitted-plan materialization".to_owned(),
                ));
            }
        } else if literal_bytes != receipt.selected_submitted_atomic.data {
            return Err(MaterializationGapError(
                "literal candidate differs from materialized row".to_owned(),
            ));
        }
    }
    Ok(())
}

pub fn agree_reference_materializations_v1(
    public_case_json: &[u8],
    receipts: &[ReferenceMaterializationReceiptV1],
) -> Result<MaterializationAgreementV1, MaterializationGapError> {
    if receipts.len() != 2 {
        return Err(MaterializationGapError(
            "exactly two independent reference receipts are required".to_owned(),
        ));
    }
    let (case, stimulus, stimulus_bytes, selector_bytes) = case_context(public_case_json)
        .map_err(|_| MaterializationGapError("public case context is underdefined".to_owned()))?;
    let evaluator_ids: BTreeSet<&str> = receipts.iter().map(|r| r.evaluator_id.as_str()).collect();
    if evaluator_ids != EVALUATOR_IDS_SET() {
        return Err(MaterializationGapError(
            "receipts do not contain both independent reference lanes".to_owned(),
        ));
    }
    for receipt in receipts {
        validate_receipt(
            receipt,
            public_case_json,
            &case,
            &stimulus,
            &stimulus_bytes,
            &selector_bytes,
        )?;
    }
    let left = &receipts[0];
    let right = &receipts[1];
    for ((name, lb), (_, rb)) in left.all_items().iter().zip(right.all_items().iter()) {
        if lb.data != rb.data {
            return Err(MaterializationGapError(format!(
                "independent references disagree on {name} bytes"
            )));
        }
    }
    let mut digest_inputs = Map::new();
    for (name, bound) in left.all_items() {
        digest_inputs.insert((*name).to_owned(), Value::String(bound.sha256.clone()));
    }
    let _ = MATERIALIZATION_ARTIFACT_NAMES;
    let agreed_bytes = canonical_json_bytes(&Value::Object(digest_inputs))
        .map_err(|e| MaterializationGapError(e.to_string()))?;
    Ok(MaterializationAgreementV1 {
        agreed_materialization_sha256: sha256_bytes(&agreed_bytes),
        selected_atomic_json: left.selected_submitted_atomic.data.clone(),
        evaluator_ids: (left.evaluator_id.clone(), right.evaluator_id.clone()),
    })
}

#[allow(non_snake_case)]
fn EVALUATOR_IDS_SET() -> BTreeSet<&'static str> {
    EVALUATORS.into_iter().collect()
}

fn json_pointer(parts: &[&str]) -> String {
    if parts.is_empty() {
        String::new()
    } else {
        let escaped: Vec<String> = parts
            .iter()
            .map(|p| p.replace('~', "~0").replace('/', "~1"))
            .collect();
        format!("/{}", escaped.join("/"))
    }
}

struct Stage2Checks<'a> {
    case: &'a Map<String, Value>,
    views: &'a DeliveredViewsV1,
    gaps: Vec<Stage2CriterionGapV1>,
}

impl<'a> Stage2Checks<'a> {
    fn new(case: &'a Map<String, Value>, views: &'a DeliveredViewsV1) -> Self {
        Self {
            case,
            views,
            gaps: Vec::new(),
        }
    }

    fn fail(&mut self, criterion: &str, path: String, detail: impl Into<String>) {
        self.gaps.push(Stage2CriterionGapV1 {
            code: "FK_MISSING".to_owned(),
            criterion: criterion.to_owned(),
            path,
            detail: detail.into(),
        });
    }

    fn unknown(&mut self, criterion: &str, path: String, detail: impl Into<String>) {
        self.gaps.push(Stage2CriterionGapV1 {
            code: "CARD_GAP".to_owned(),
            criterion: criterion.to_owned(),
            path,
            detail: detail.into(),
        });
    }

    fn require(
        &mut self,
        view: Option<&'a PublicView>,
        table: &str,
        entity_id: &str,
        path: String,
        criterion: &str,
    ) -> Option<&'a TableRowV5> {
        let Some(v) = view else {
            self.unknown(
                criterion,
                path,
                "required phase view is absent from the public case",
            );
            return None;
        };
        if let Some(record) = v.get(table, entity_id) {
            return Some(record);
        }
        if v.proves_absent(table, entity_id) {
            self.fail(
                criterion,
                path,
                format!("{entity_id} is absent from the complete delivered {table} view"),
            );
        } else {
            self.unknown(
                criterion,
                path,
                format!("delivered {table} context does not prove whether {entity_id} exists"),
            );
        }
        None
    }

    fn require_any(
        &mut self,
        view: Option<&'a PublicView>,
        tables: &[&str],
        entity_id: &str,
        path: String,
        criterion: &str,
    ) -> bool {
        let Some(v) = view else {
            self.unknown(
                criterion,
                path,
                "required phase view is absent from the public case",
            );
            return false;
        };
        if tables.iter().any(|t| v.get(t, entity_id).is_some()) {
            return true;
        }
        if tables.iter().all(|t| v.proves_absent(t, entity_id)) {
            self.fail(
                criterion,
                path,
                format!("{entity_id} is absent from all complete delivered principal views"),
            );
        } else {
            self.unknown(
                criterion,
                path,
                format!("delivered principal views do not determine whether {entity_id} exists"),
            );
        }
        false
    }

    fn require_in_set(
        &mut self,
        view: Option<&'a PublicView>,
        table: &str,
        value: &str,
        allowed: &BTreeSet<String>,
        path: String,
        criterion: &str,
    ) {
        if allowed.contains(value) {
            return;
        }
        let Some(v) = view else {
            self.unknown(
                criterion,
                path,
                "required phase view is absent from the public case",
            );
            return;
        };
        if v.proves_absent(table, value) {
            self.fail(
                criterion,
                path,
                format!("{value} is not a member of the complete delivered {table} view"),
            );
        } else {
            self.unknown(
                criterion,
                path,
                format!("delivered {table} context is incomplete for {value}"),
            );
        }
    }

    fn inspect(mut self, mutation: &AtomicMutationV5) -> Vec<Stage2CriterionGapV1> {
        let target_path = json_pointer(&["target_entity_id"]);
        let entity_id = mutation.target_entity_id();

        match mutation {
            AtomicMutationV5::ConnectorSync(m) => {
                let view = Some(&self.views.phase1);
                let conn_row = self.require(
                    view,
                    "connector_schemas",
                    entity_id,
                    target_path.clone(),
                    "connector_exists",
                );
                if let Some(TableRowV5::ConnectorSchema(conn)) = conn_row {
                    let mut admitted = BTreeSet::new();
                    if let Some(page_rows) = self.views.phase1.rows.get("connector_pages") {
                        for row in page_rows.values() {
                            if let TableRowV5::ConnectorPage(page) = row
                                && page.connector_id == conn.connector_id
                            {
                                for record in &page.records {
                                    for (k, v) in record {
                                        if k == "account_id"
                                            && let Some(acc) = v.as_str()
                                        {
                                            admitted.insert(acc.to_owned());
                                        }
                                    }
                                }
                            }
                        }
                    }
                    if conn.sync_mode == ConnectorSyncModeV5::DeltaCursor
                        || conn.transport_kind == ConnectorTransportKindV5::FlatFileSftp
                    {
                        admitted.extend(conn.baseline_reconciled_account_ids.iter().cloned());
                    }
                    for (index, account_id) in m.reconciled_account_ids.iter().enumerate() {
                        let idx_s = index.to_string();
                        self.require_in_set(
                            view,
                            "connector_pages",
                            account_id,
                            &admitted,
                            json_pointer(&["reconciled_account_ids", &idx_s]),
                            "connector_reconciled_account_admitted",
                        );
                    }
                    let mapped_roles: BTreeSet<&str> =
                        conn.mapping_rules.iter().map(|(_, r)| r.as_str()).collect();
                    for (index, drift_token) in m.remediated_spoke_drift.iter().enumerate() {
                        if let Some((account_id, suffix)) = drift_token.split_once(':') {
                            let idx_s = index.to_string();
                            self.require_in_set(
                                view,
                                "connector_pages",
                                account_id,
                                &admitted,
                                json_pointer(&["remediated_spoke_drift", &idx_s]),
                                "connector_drift_account_admitted",
                            );
                            if suffix.starts_with("role_") && !mapped_roles.contains(suffix) {
                                self.require(
                                    view,
                                    "roles",
                                    suffix,
                                    json_pointer(&["remediated_spoke_drift", &idx_s]),
                                    "connector_drift_role_exists",
                                );
                            }
                        }
                    }
                }
                let drift_set: BTreeSet<&str> = m
                    .remediated_spoke_drift
                    .iter()
                    .map(String::as_str)
                    .collect();
                for (index, action) in m.itsm_ticket_actions.iter().enumerate() {
                    let idx_s = index.to_string();
                    let path = json_pointer(&["itsm_ticket_actions", &idx_s, "drift_token"]);
                    if !drift_set.contains(action.drift_token.as_str()) {
                        self.fail(
                            "itsm_action_refers_to_remediated_drift",
                            path,
                            "ITSM action token is absent from the submitted remediation tuple",
                        );
                    }
                    if let Some(assignee) = &action.assignee_account_id {
                        self.require(
                            view,
                            "idp_accounts",
                            assignee,
                            json_pointer(&["itsm_ticket_actions", &idx_s, "assignee_account_id"]),
                            "itsm_assignee_exists",
                        );
                    }
                }
            }
            AtomicMutationV5::HrSourceMapping(m) => {
                let view = Some(&self.views.phase1);
                if let Some(TableRowV5::HrSourceMapping(task)) = self.require(
                    view,
                    "hr_source_mappings",
                    entity_id,
                    target_path,
                    "hr_mapping_task_exists",
                ) {
                    let feed_ids: BTreeSet<&str> =
                        task.feeds.iter().map(|f| f.feed_id.as_str()).collect();
                    let covered_feeds: BTreeSet<&str> = m
                        .accepted_feed_ids
                        .iter()
                        .chain(m.rejected_feed_ids.iter())
                        .map(String::as_str)
                        .collect();
                    if covered_feeds != feed_ids {
                        self.fail(
                            "feed_partition_matches_task",
                            json_pointer(&["accepted_feed_ids"]),
                            "accepted and rejected feed IDs do not cover the delivered task feed IDs",
                        );
                    }
                    let assertions: BTreeSet<&str> = task
                        .feeds
                        .iter()
                        .flat_map(|f| f.assertions.iter().map(|a| a.person_id.as_str()))
                        .collect();
                    for (index, (person_id, _)) in m.resolved_person_statuses.iter().enumerate() {
                        if !assertions.contains(person_id.as_str()) {
                            let idx_s = index.to_string();
                            self.fail(
                                "resolved_person_has_delivered_assertion",
                                json_pointer(&["resolved_person_statuses", &idx_s, "0"]),
                                "resolved person is absent from delivered feed assertions",
                            );
                        }
                    }
                    let assertion_keys: BTreeSet<String> = task
                        .feeds
                        .iter()
                        .flat_map(|f| {
                            f.assertions
                                .iter()
                                .map(move |a| format!("{}:{}", f.feed_id, a.person_id))
                        })
                        .collect();
                    for (index, assertion_key) in m.quarantined_assertion_keys.iter().enumerate() {
                        if !assertion_keys.contains(assertion_key) {
                            let idx_s = index.to_string();
                            self.fail(
                                "quarantined_key_has_delivered_assertion",
                                json_pointer(&["quarantined_assertion_keys", &idx_s]),
                                "quarantined key is absent from delivered feed assertions",
                            );
                        }
                    }
                }
            }
            AtomicMutationV5::AccountRecon(m) => {
                let view = Some(&self.views.phase1);
                let account_row = self.require(
                    view,
                    "idp_accounts",
                    entity_id,
                    target_path,
                    "account_exists",
                );
                if let Some(bound_person) = &m.bound_person_id {
                    let person_row = self.require(
                        view,
                        "hr_persons",
                        bound_person,
                        json_pointer(&["bound_person_id"]),
                        "bound_person_exists",
                    );
                    if let Some(TableRowV5::HrPerson(person)) = person_row {
                        let eng_ids: BTreeSet<&str> = person
                            .engagements
                            .iter()
                            .map(|e| e.eng_id.as_str())
                            .collect();
                        let eng_ok = m
                            .bound_engagement_id
                            .as_deref()
                            .is_some_and(|e| eng_ids.contains(e));
                        if !eng_ok {
                            self.fail(
                                "bound_engagement_belongs_to_person",
                                json_pointer(&["bound_engagement_id"]),
                                "bound engagement is absent from the delivered person",
                            );
                        }
                    }
                }
                if let Some(TableRowV5::IdpAccount(account)) = account_row {
                    let direct: BTreeSet<&str> = account
                        .direct_grant_ids
                        .iter()
                        .map(String::as_str)
                        .collect();
                    for (index, grant_id) in m.retained_grant_ids.iter().enumerate() {
                        let idx_s = index.to_string();
                        let path = json_pointer(&["retained_grant_ids", &idx_s]);
                        if !direct.contains(grant_id.as_str()) {
                            self.fail(
                                "retained_grant_is_direct",
                                path.clone(),
                                "retained grant is absent from the delivered account direct grants",
                            );
                        }
                        self.require(view, "grants", grant_id, path, "retained_grant_exists");
                    }
                } else {
                    for (index, grant_id) in m.retained_grant_ids.iter().enumerate() {
                        let idx_s = index.to_string();
                        self.require(
                            view,
                            "grants",
                            grant_id,
                            json_pointer(&["retained_grant_ids", &idx_s]),
                            "retained_grant_exists",
                        );
                    }
                }
            }
            AtomicMutationV5::NhiGovernance(m) => {
                let view = Some(&self.views.phase1);
                self.require(
                    view,
                    "target_nhis",
                    entity_id,
                    target_path,
                    "target_nhi_exists",
                );
                if let Some(owner_id) = &m.resolved_owner_person_id {
                    self.require(
                        view,
                        "hr_persons",
                        owner_id,
                        json_pointer(&["resolved_owner_person_id"]),
                        "nhi_owner_person_exists",
                    );
                }
            }
            AtomicMutationV5::AgentRegistry(m) => {
                let view = Some(&self.views.phase1);
                let agent_row = self.require(
                    view,
                    "enterprise_agents",
                    entity_id,
                    target_path,
                    "enterprise_agent_exists",
                );
                if let Some(sponsor_id) = &m.resolved_sponsor_person_id {
                    self.require(
                        view,
                        "hr_persons",
                        sponsor_id,
                        json_pointer(&["resolved_sponsor_person_id"]),
                        "agent_sponsor_exists",
                    );
                }
                if let Some(TableRowV5::EnterpriseAgent(agent)) = agent_row {
                    let binding_ids: BTreeSet<&str> = agent
                        .tool_bindings
                        .iter()
                        .map(|b| b.binding_id.as_str())
                        .collect();
                    for (field_name, ids) in [
                        (
                            "active_autonomous_binding_ids",
                            &m.active_autonomous_binding_ids,
                        ),
                        ("active_hitl_binding_ids", &m.active_hitl_binding_ids),
                    ] {
                        for (index, binding_id) in ids.iter().enumerate() {
                            if !binding_ids.contains(binding_id.as_str()) {
                                let idx_s = index.to_string();
                                self.fail(
                                    "active_binding_belongs_to_agent",
                                    json_pointer(&[field_name, &idx_s]),
                                    "active binding is absent from the delivered agent",
                                );
                            }
                        }
                    }
                }
            }
            AtomicMutationV5::CloudCiem(m) => {
                let view = Some(&self.views.phase1);
                if let Some(TableRowV5::CloudCiemTask(task)) = self.require(
                    view,
                    "cloud_ciem_tasks",
                    entity_id,
                    target_path,
                    "ciem_task_exists",
                ) {
                    let task_principals: BTreeSet<&str> = task
                        .principals
                        .iter()
                        .map(|p| p.cprin_id.as_str())
                        .collect();
                    let finding_ids: BTreeSet<&str> =
                        m.findings.iter().map(|f| f.cprin_id.as_str()).collect();
                    if finding_ids != task_principals {
                        self.fail(
                            "finding_ids_match_task_principals",
                            json_pointer(&["findings"]),
                            "finding IDs differ from delivered task principal IDs",
                        );
                    }
                    for principal in &task.principals {
                        self.require_any(
                            view,
                            &["idp_accounts", "target_nhis", "enterprise_agents"],
                            &principal.linked_principal_id,
                            json_pointer(&["target_entity_id"]),
                            "ciem_linked_principal_exists",
                        );
                    }
                    let forbidden: BTreeSet<&str> = task
                        .forbidden_standing_cloud_actions
                        .iter()
                        .map(String::as_str)
                        .collect();
                    for (index, finding) in m.findings.iter().enumerate() {
                        let eff: BTreeSet<&str> = finding
                            .effective_forbidden_actions
                            .iter()
                            .map(String::as_str)
                            .collect();
                        if !eff.is_subset(&forbidden) {
                            let idx_s = index.to_string();
                            self.fail(
                                "effective_actions_subset_of_task",
                                json_pointer(&["findings", &idx_s, "effective_forbidden_actions"]),
                                "finding action is outside the delivered task forbidden-action set",
                            );
                        }
                    }
                }
            }
            AtomicMutationV5::Optimization(m) => {
                let phase_index = self.case.get("phase_index").and_then(Value::as_i64);
                let view = if phase_index == Some(1) {
                    Some(&self.views.phase1)
                } else {
                    self.views.phase2.as_ref()
                };
                if let Some(TableRowV5::OptimizationTask(task)) = self.require(
                    view,
                    "optimization_tasks",
                    entity_id,
                    target_path,
                    "optimization_task_exists",
                ) {
                    let selectable: BTreeSet<&str> = task
                        .candidates
                        .iter()
                        .map(|c| c.candidate_id.as_str())
                        .collect();
                    for (index, candidate_id) in m.selected_ids.iter().enumerate() {
                        if !selectable.contains(candidate_id.as_str()) {
                            let idx_s = index.to_string();
                            self.fail(
                                "selected_id_is_selectable",
                                json_pointer(&["selected_ids", &idx_s]),
                                "selected ID is absent from the delivered optimization task",
                            );
                        }
                    }
                }
            }
            AtomicMutationV5::JmlWorkflow(m) => {
                let view = Some(&self.views.phase1);
                if let Some(TableRowV5::JmlWorkflowTask(workflow)) = self.require(
                    view,
                    "jml_workflow_tasks",
                    entity_id,
                    target_path,
                    "jml_workflow_task_exists",
                ) {
                    let expected: BTreeSet<&str> = workflow
                        .birthright_catalog
                        .iter()
                        .map(|r| r.rule_id.as_str())
                        .collect();
                    let selected: BTreeSet<&str> = m
                        .active_birthright_rule_ids
                        .iter()
                        .chain(m.converted_eligible_brule_ids.iter())
                        .chain(m.quarantined_birthright_rule_ids.iter())
                        .map(String::as_str)
                        .collect();
                    if selected != expected {
                        self.fail(
                            "birthright_partitions_match_catalog",
                            json_pointer(&["active_birthright_rule_ids"]),
                            "birthright rule partitions differ from the delivered workflow catalog",
                        );
                    }
                }
            }
            AtomicMutationV5::CaeContainment(m) => {
                let view = self.views.phase2.as_ref();
                self.require(
                    view,
                    "cae_incidents",
                    entity_id,
                    target_path,
                    "cae_incident_exists",
                );
                for (index, ticket_id) in m.retro_invalidated_ids.iter().enumerate() {
                    let idx_s = index.to_string();
                    self.require_any(
                        view,
                        &["change_tickets", "transfer_bridges"],
                        ticket_id,
                        json_pointer(&["retro_invalidated_ids", &idx_s]),
                        "retro_invalidated_entity_exists",
                    );
                }
            }
            AtomicMutationV5::UarCampaign(m) => {
                let view = self.views.phase2.as_ref();
                let campaign_row = self.require(
                    view,
                    "uar_campaigns",
                    entity_id,
                    target_path,
                    "uar_campaign_exists",
                );
                for (index, (_, reviewer_id)) in m.reviewer_overrides.iter().enumerate() {
                    let idx_s = index.to_string();
                    self.require(
                        view,
                        "idp_accounts",
                        reviewer_id,
                        json_pointer(&["reviewer_overrides", &idx_s, "1"]),
                        "override_reviewer_exists",
                    );
                }
                if let Some(custodian_id) = &m.emergency_custodian_account_id {
                    self.require(
                        view,
                        "idp_accounts",
                        custodian_id,
                        json_pointer(&["emergency_custodian_account_id"]),
                        "emergency_custodian_exists",
                    );
                }
                if let Some(TableRowV5::UarCampaign(campaign)) = campaign_row {
                    let items: BTreeSet<&str> =
                        campaign.items.iter().map(|i| i.item_id.as_str()).collect();
                    let decision_ids: BTreeSet<&str> =
                        m.uar_decisions.iter().map(|(id, _)| id.as_str()).collect();
                    if decision_ids != items {
                        self.fail(
                            "decision_ids_match_campaign_items",
                            json_pointer(&["uar_decisions"]),
                            "decision IDs differ from delivered campaign items",
                        );
                    }
                    let backups: BTreeSet<&str> = campaign
                        .backup_reviewer_account_ids
                        .iter()
                        .map(String::as_str)
                        .collect();
                    for (index, (item_id, reviewer_id)) in m.reviewer_overrides.iter().enumerate() {
                        let idx_s = index.to_string();
                        if !items.contains(item_id.as_str()) {
                            self.fail(
                                "override_item_belongs_to_campaign",
                                json_pointer(&["reviewer_overrides", &idx_s, "0"]),
                                "override item is absent from delivered campaign",
                            );
                        }
                        if !backups.contains(reviewer_id.as_str()) {
                            self.fail(
                                "override_reviewer_is_campaign_backup",
                                json_pointer(&["reviewer_overrides", &idx_s, "1"]),
                                "override reviewer is absent from delivered campaign backup reviewers",
                            );
                        }
                    }
                    if let Some(custodian_id) = &m.emergency_custodian_account_id
                        && !campaign
                            .candidate_custodian_account_ids
                            .contains(custodian_id)
                    {
                        self.fail(
                            "emergency_custodian_is_candidate",
                            json_pointer(&["emergency_custodian_account_id"]),
                            "custodian is absent from delivered campaign candidates",
                        );
                    }
                }
            }
            AtomicMutationV5::JitDecision(m) => {
                let view = self.views.phase2.as_ref();
                self.require(
                    view,
                    "jit_requests",
                    entity_id,
                    target_path,
                    "jit_request_exists",
                );
                if let Some(approver_id) = &m.approver_account_id {
                    self.require(
                        view,
                        "idp_accounts",
                        approver_id,
                        json_pointer(&["approver_account_id"]),
                        "jit_approver_exists",
                    );
                }
            }
            AtomicMutationV5::AgentDelegation(m) => {
                let view = self.views.phase2.as_ref();
                self.require(
                    view,
                    "agent_delegation_requests",
                    entity_id,
                    target_path,
                    "delegation_request_exists",
                );
                if let Some(cosigner_id) = &m.hitl_cosigner_account_id {
                    self.require(
                        view,
                        "idp_accounts",
                        cosigner_id,
                        json_pointer(&["hitl_cosigner_account_id"]),
                        "delegation_cosigner_exists",
                    );
                }
            }
        }
        self.gaps
    }
}

fn unknown_observation(
    public_case_json: &[u8],
    candidate_atomic_json: &[u8],
    agreed_materialization_sha256: &str,
    criterion: &str,
    detail: &str,
) -> Stage2DomainObservationV1 {
    let case = strict_json_loads(public_case_json)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    let rendered_card = case.get("rendered_card").and_then(Value::as_str);
    let stimulus = case.get("stimulus");
    let schema_sha256 = case
        .get("schema_sha256")
        .map(|v| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    let stimulus_sha256 = match stimulus {
        Some(v) if v.is_object() || v.is_array() => canonical_json_bytes(v)
            .map(|b| sha256_bytes(&b))
            .unwrap_or_default(),
        _ => String::new(),
    };
    Stage2DomainObservationV1 {
        case_sha256: sha256_bytes(public_case_json),
        schema_sha256,
        card_sha256: rendered_card
            .map(|c| sha256_bytes(c.as_bytes()))
            .unwrap_or_default(),
        stimulus_sha256,
        selected_candidate_sha256: sha256_bytes(candidate_atomic_json),
        agreed_materialization_sha256: agreed_materialization_sha256.to_owned(),
        stage2_status: "UNKNOWN".to_owned(),
        criterion_gaps: vec![Stage2CriterionGapV1 {
            code: "CARD_GAP".to_owned(),
            criterion: criterion.to_owned(),
            path: String::new(),
            detail: detail.to_owned(),
        }],
    }
}

pub fn inspect_stage2_domain_v1(
    public_case_json: &[u8],
    candidate_atomic_json: &[u8],
    agreed_materialization_sha256: &str,
) -> Result<Stage2DomainObservationV1, MaterializationGapError> {
    if !is_sha256(agreed_materialization_sha256) {
        return Err(MaterializationGapError(
            "checker invocation is not bound to a valid agreement digest".to_owned(),
        ));
    }
    let Ok(case_val) = strict_json_loads(public_case_json) else {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "public_case",
            "public case bytes are not strict JSON",
        ));
    };
    let Some(case) = case_val.as_object() else {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "public_case",
            "public case envelope is not an object",
        ));
    };

    let schema_hash = case
        .get("schema_sha256")
        .map(|v| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    let rendered_card = case.get("rendered_card").and_then(Value::as_str);
    let stimulus = case.get("stimulus");
    let case_sha256 = sha256_bytes(public_case_json);
    let card_sha256 = rendered_card
        .map(|c| sha256_bytes(c.as_bytes()))
        .unwrap_or_default();
    let stimulus_sha256 = match stimulus {
        Some(v) if v.is_object() => canonical_json_bytes(v)
            .map(|b| sha256_bytes(&b))
            .unwrap_or_default(),
        _ => String::new(),
    };
    let selected_candidate_sha256 = sha256_bytes(candidate_atomic_json);

    if schema_hash != PINNED_SCHEMA_SHA256_V1 {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "schema_context",
            "public schema digest is missing or differs from the pinned wire-v1 schema",
        ));
    }

    let candidate_raw = strict_json_loads(candidate_atomic_json).ok();
    let Some(candidate_val) = candidate_raw else {
        return Ok(Stage2DomainObservationV1 {
            case_sha256,
            schema_sha256: schema_hash,
            card_sha256,
            stimulus_sha256,
            selected_candidate_sha256,
            agreed_materialization_sha256: agreed_materialization_sha256.to_owned(),
            stage2_status: "INELIGIBLE".to_owned(),
            criterion_gaps: vec![Stage2CriterionGapV1 {
                code: "SCHEMA_VALIDATION_ERROR".to_owned(),
                criterion: "atomic_schema".to_owned(),
                path: String::new(),
                detail: "selected candidate fails strict AtomicMutationV5 JSON validation"
                    .to_owned(),
            }],
        });
    };
    let canonical_candidate =
        canonical_json_bytes(&candidate_val).map_err(|e| MaterializationGapError(e.to_string()))?;
    if canonical_candidate != candidate_atomic_json {
        return Err(MaterializationGapError(
            "selected candidate bytes are not canonical-json-v1".to_owned(),
        ));
    }
    let Ok(mutation) = std::str::from_utf8(candidate_atomic_json)
        .map_err(|_| ())
        .and_then(|s| AtomicMutationV5::from_strict_json_str(s).map_err(|_| ()))
    else {
        return Ok(Stage2DomainObservationV1 {
            case_sha256,
            schema_sha256: schema_hash,
            card_sha256,
            stimulus_sha256,
            selected_candidate_sha256,
            agreed_materialization_sha256: agreed_materialization_sha256.to_owned(),
            stage2_status: "INELIGIBLE".to_owned(),
            criterion_gaps: vec![Stage2CriterionGapV1 {
                code: "SCHEMA_VALIDATION_ERROR".to_owned(),
                criterion: "atomic_schema".to_owned(),
                path: String::new(),
                detail: "selected candidate fails strict AtomicMutationV5 JSON validation"
                    .to_owned(),
            }],
        });
    };

    let Ok((_, _, _, selector_bytes)) = case_context(public_case_json) else {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "public_case",
            "public safety case envelope is malformed or underdefined",
        ));
    };
    let Ok(selector) = strict_json_loads(&selector_bytes) else {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "public_case",
            "public safety case envelope is malformed or underdefined",
        ));
    };
    let sel_mk = selector.get("mutation_kind").and_then(Value::as_str);
    let sel_tid = selector.get("target_entity_id").and_then(Value::as_str);
    if (sel_mk, sel_tid)
        != (
            Some(mutation.mutation_kind()),
            Some(mutation.target_entity_id()),
        )
    {
        return Err(MaterializationGapError(
            "selected row does not match the safety case target selector".to_owned(),
        ));
    }

    let phase_ok = case
        .get("phase_index")
        .and_then(Value::as_i64)
        .is_some_and(|p| p == 1 || p == 2);
    let views = if phase_ok {
        build_delivered_views_v1(public_case_json).ok()
    } else {
        None
    };
    let Some(views) = views else {
        return Ok(unknown_observation(
            public_case_json,
            candidate_atomic_json,
            agreed_materialization_sha256,
            "delivered_view",
            "required public phase context is missing, malformed, or underdefined",
        ));
    };

    let checks = Stage2Checks::new(case, &views);
    let gaps = checks.inspect(&mutation);
    let status = if gaps
        .iter()
        .any(|g| g.code == "SCHEMA_VALIDATION_ERROR" || g.code == "FK_MISSING")
    {
        "INELIGIBLE"
    } else if !gaps.is_empty() {
        "UNKNOWN"
    } else {
        "ELIGIBLE"
    };
    Ok(Stage2DomainObservationV1 {
        case_sha256,
        schema_sha256: schema_hash,
        card_sha256,
        stimulus_sha256,
        selected_candidate_sha256,
        agreed_materialization_sha256: agreed_materialization_sha256.to_owned(),
        stage2_status: status.to_owned(),
        criterion_gaps: gaps,
    })
}

pub fn inspect_agreed_stage2_domain_v1(
    public_case_json: &[u8],
    receipts: &[ReferenceMaterializationReceiptV1],
) -> Result<Stage2DomainObservationV1, MaterializationGapError> {
    let agreement = agree_reference_materializations_v1(public_case_json, receipts)?;
    inspect_stage2_domain_v1(
        public_case_json,
        &agreement.selected_atomic_json,
        &agreement.agreed_materialization_sha256,
    )
}
