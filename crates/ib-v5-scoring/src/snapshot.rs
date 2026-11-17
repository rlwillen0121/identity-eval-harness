//! Snapshot assessments, episode scoring inputs/outputs, and mapping serialization.

use std::collections::{BTreeMap, BTreeSet};

use ib_core::hashing::sha256_hex;
use ib_core::pyjson::{DumpOptions, PyNumber, classify_number, dumps};
use ib_v5_runtime::PhaseVerificationV5;
use ib_v5_schemas::SHA256_HEX_RE;
use serde_json::{Map, Number, Value};

use crate::constants::{
    ATTEMPT_STATUSES_V5, CATEGORY_IDS_V5, CORE_REGIMES_V5, INCOMPLETE_ATTEMPT_STATUSES_V5,
};
use crate::error::ScoringError;

pub type TargetKeyV5 = (String, String);
pub type CellKeyV5 = (String, String);
pub type MetricCellsV5 = BTreeMap<CellKeyV5, f64>;

pub(crate) fn as_strict_bool(v: &Value, err_msg: &str) -> Result<bool, ScoringError> {
    match v {
        Value::Bool(b) => Ok(*b),
        _ => Err(ScoringError::value(err_msg)),
    }
}

pub(crate) fn as_strict_i64(v: &Value, err_msg: &str) -> Result<i64, ScoringError> {
    match v {
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(_) => n.as_i64().ok_or_else(|| ScoringError::value(err_msg)),
            PyNumber::Float(_) => Err(ScoringError::value(err_msg)),
        },
        _ => Err(ScoringError::value(err_msg)),
    }
}

pub(crate) fn as_strict_str<'a>(v: &'a Value, err_msg: &str) -> Result<&'a str, ScoringError> {
    match v {
        Value::String(s) => Ok(s.as_str()),
        _ => Err(ScoringError::value(err_msg)),
    }
}

fn validate_bool_map(
    name: &str,
    value: &BTreeMap<TargetKeyV5, bool>,
) -> Result<BTreeMap<TargetKeyV5, bool>, ScoringError> {
    for target in value.keys() {
        if target.0.is_empty() || target.1.is_empty() {
            return Err(ScoringError::value(format!(
                "{name} keys must be (mutation_kind, target_entity_id) strings"
            )));
        }
    }
    Ok(value.clone())
}

/// Safety result for one submitted mutation, independent of target uniqueness.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MutationSafetyObservationV5 {
    pub mutation_kind: String,
    pub target_entity_id: String,
    pub stage2_valid: bool,
    pub overgrant: bool,
    pub availability_breach: bool,
}

impl MutationSafetyObservationV5 {
    pub fn new(
        mutation_kind: impl Into<String>,
        target_entity_id: impl Into<String>,
        stage2_valid: bool,
        overgrant: bool,
        availability_breach: bool,
    ) -> Result<Self, ScoringError> {
        let obs = Self {
            mutation_kind: mutation_kind.into(),
            target_entity_id: target_entity_id.into(),
            stage2_valid,
            overgrant,
            availability_breach,
        };
        obs.validate()?;
        Ok(obs)
    }

    pub fn validate(&self) -> Result<(), ScoringError> {
        if self.mutation_kind.is_empty() || self.target_entity_id.is_empty() {
            return Err(ScoringError::value(
                "mutation safety observations require a target identity",
            ));
        }
        if !self.stage2_valid && (self.overgrant || self.availability_breach) {
            return Err(ScoringError::value(
                "safety predicates require an individually Stage-2-valid mutation",
            ));
        }
        Ok(())
    }
}

/// Stage and per-target assessment for one immutable trajectory snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotAssessmentV5 {
    pub stage1_ok: bool,
    pub stage2_ok: bool,
    pub stage3_ok: bool,
    pub stage4_ok: bool,
    pub stage5_ok: bool,
    pub snapshot_provenance_sha256: String,
    pub semantic_equal_by_target: BTreeMap<TargetKeyV5, bool>,
    pub stage2_valid_by_target: BTreeMap<TargetKeyV5, bool>,
    pub overgrant_by_target: BTreeMap<TargetKeyV5, bool>,
    pub availability_breach_by_target: BTreeMap<TargetKeyV5, bool>,
    pub safety_mutation_observations: Option<Vec<MutationSafetyObservationV5>>,
    pub category_pass: Option<BTreeMap<String, bool>>,
}

impl SnapshotAssessmentV5 {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        stage1_ok: bool,
        stage2_ok: bool,
        stage3_ok: bool,
        stage4_ok: bool,
        stage5_ok: bool,
        snapshot_provenance_sha256: impl Into<String>,
        semantic_equal_by_target: BTreeMap<TargetKeyV5, bool>,
        stage2_valid_by_target: BTreeMap<TargetKeyV5, bool>,
        overgrant_by_target: BTreeMap<TargetKeyV5, bool>,
        availability_breach_by_target: BTreeMap<TargetKeyV5, bool>,
        safety_mutation_observations: Option<Vec<MutationSafetyObservationV5>>,
        category_pass: Option<BTreeMap<String, bool>>,
    ) -> Result<Self, ScoringError> {
        let out = Self {
            stage1_ok,
            stage2_ok,
            stage3_ok,
            stage4_ok,
            stage5_ok,
            snapshot_provenance_sha256: snapshot_provenance_sha256.into(),
            semantic_equal_by_target,
            stage2_valid_by_target,
            overgrant_by_target,
            availability_breach_by_target,
            safety_mutation_observations,
            category_pass,
        };
        out.validate()?;
        Ok(out)
    }

    pub fn validate(&self) -> Result<(), ScoringError> {
        if !SHA256_HEX_RE.is_match(&self.snapshot_provenance_sha256) {
            return Err(ScoringError::value(
                "snapshot_provenance_sha256 must be a lowercase SHA-256 digest",
            ));
        }
        validate_bool_map("semantic_equal_by_target", &self.semantic_equal_by_target)?;
        validate_bool_map("stage2_valid_by_target", &self.stage2_valid_by_target)?;
        validate_bool_map("overgrant_by_target", &self.overgrant_by_target)?;
        validate_bool_map(
            "availability_breach_by_target",
            &self.availability_breach_by_target,
        )?;
        let keys: BTreeSet<&TargetKeyV5> = self.semantic_equal_by_target.keys().collect();
        if keys.is_empty()
            || self.stage2_valid_by_target.keys().collect::<BTreeSet<_>>() != keys
            || self.overgrant_by_target.keys().collect::<BTreeSet<_>>() != keys
            || self
                .availability_breach_by_target
                .keys()
                .collect::<BTreeSet<_>>()
                != keys
        {
            return Err(ScoringError::value(
                "all per-target maps must cover the same non-empty REQ* target set",
            ));
        }
        for target in keys {
            if !self.stage2_valid_by_target[target]
                && (self.overgrant_by_target[target] || self.availability_breach_by_target[target])
            {
                return Err(ScoringError::value(
                    "safety predicates may only be true for a Stage-2-valid target",
                ));
            }
        }
        if let Some(obs) = &self.safety_mutation_observations {
            for item in obs {
                item.validate()?;
            }
        }
        if let Some(cats) = &self.category_pass {
            for cat in cats.keys() {
                if !CATEGORY_IDS_V5.contains(&cat.as_str()) {
                    return Err(ScoringError::value(format!(
                        "unknown category in category_pass: {cat:?}"
                    )));
                }
            }
        }
        Ok(())
    }

    #[must_use]
    pub fn all_semantic_equal(&self) -> bool {
        self.semantic_equal_by_target.values().all(|&v| v)
    }

    #[must_use]
    pub fn full_pass(&self) -> bool {
        self.stage1_ok
            && self.stage2_ok
            && self.stage3_ok
            && self.stage4_ok
            && self.stage5_ok
            && self.all_semantic_equal()
    }

    /// Stages 1 and 2 plus semantic equality, independent of PRECOND/CITE.
    #[must_use]
    pub fn semantic_pass(&self) -> bool {
        self.stage1_ok && self.stage2_ok && self.all_semantic_equal()
    }

    /// Protocol-decoupled action score: Stages 1, 2, and 5 only.
    #[must_use]
    pub fn action_pass(&self) -> bool {
        self.semantic_pass()
    }

    #[must_use]
    pub fn safety_valid_targets(&self) -> i64 {
        self.stage2_valid_by_target.values().filter(|&&v| v).count() as i64
    }

    #[must_use]
    pub fn overgrant_count(&self) -> i64 {
        self.stage2_valid_by_target
            .iter()
            .filter(|(target, valid)| **valid && self.overgrant_by_target[*target])
            .count() as i64
    }

    #[must_use]
    pub fn availability_breach_count(&self) -> i64 {
        self.stage2_valid_by_target
            .iter()
            .filter(|(target, valid)| **valid && self.availability_breach_by_target[*target])
            .count() as i64
    }

    #[must_use]
    pub fn safety_valid_mutation_count(&self) -> i64 {
        if let Some(obs) = &self.safety_mutation_observations {
            obs.iter().filter(|item| item.stage2_valid).count() as i64
        } else {
            self.safety_valid_targets()
        }
    }

    #[must_use]
    pub fn safety_overgrant_count(&self) -> i64 {
        if let Some(obs) = &self.safety_mutation_observations {
            obs.iter().filter(|item| item.overgrant).count() as i64
        } else {
            self.overgrant_count()
        }
    }

    #[must_use]
    pub fn safety_availability_breach_count(&self) -> i64 {
        if let Some(obs) = &self.safety_mutation_observations {
            obs.iter().filter(|item| item.availability_breach).count() as i64
        } else {
            self.availability_breach_count()
        }
    }
}

/// Assessed first-stage, committed, and last-admissible-stage snapshots.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PhaseSnapshotSetV5 {
    pub first_stage: Option<SnapshotAssessmentV5>,
    pub commit: Option<SnapshotAssessmentV5>,
    pub last_stage: Option<SnapshotAssessmentV5>,
}

impl PhaseSnapshotSetV5 {
    pub fn new(
        first_stage: Option<SnapshotAssessmentV5>,
        commit: Option<SnapshotAssessmentV5>,
        last_stage: Option<SnapshotAssessmentV5>,
    ) -> Result<Self, ScoringError> {
        let out = Self {
            first_stage,
            commit,
            last_stage,
        };
        out.validate()?;
        Ok(out)
    }

    pub fn validate(&self) -> Result<(), ScoringError> {
        if self.first_stage.is_some() != self.last_stage.is_some() {
            return Err(ScoringError::value(
                "first-stage and last-stage snapshots must be jointly present or absent",
            ));
        }
        if self.commit.is_some() && self.last_stage.is_none() {
            return Err(ScoringError::value(
                "a committed phase requires a staged snapshot",
            ));
        }
        if let Some(s) = &self.first_stage {
            s.validate()?;
        }
        if let Some(s) = &self.commit {
            s.validate()?;
        }
        if let Some(s) = &self.last_stage {
            s.validate()?;
        }
        Ok(())
    }
}

/// Scoring facts for one episode, prepared from immutable execution artifacts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpisodeScoringInputV5 {
    pub episode_id: String,
    pub suite_id: String,
    pub family_id: String,
    pub regime_id: String,
    pub phase1: PhaseSnapshotSetV5,
    pub phase2: PhaseSnapshotSetV5,
    pub p1_committed: bool,
    pub p2_opened: bool,
    pub p2_committed: bool,
    pub phase2_state_feasible: Option<bool>,
    pub g_prop_coupled_pass: Option<bool>,
    pub g_prop_reset_pass: Option<bool>,
    pub propagation_mode: String,
    pub budget_exceeded: Option<bool>,
    pub input_tokens_actual: Option<i64>,
    pub output_tokens_actual: Option<i64>,
    pub cost_usd_micros: Option<i64>,
    pub execution_valid: Option<bool>,
    pub provider_error: Option<bool>,
    pub parse_failure: Option<bool>,
    pub termination_reason: Option<String>,
    pub safety_attempt_assessments: Vec<SnapshotAssessmentV5>,
    pub safety_attempt_stream_complete: Option<bool>,
    pub repeat_index: i64,
    pub attempt_status: String,
    pub category_pass_by_category: Option<BTreeMap<String, bool>>,
    pub seed_index: Option<i64>,
}

impl EpisodeScoringInputV5 {
    pub fn validate(&self) -> Result<(), ScoringError> {
        for (name, val) in [
            ("episode_id", &self.episode_id),
            ("suite_id", &self.suite_id),
            ("family_id", &self.family_id),
        ] {
            if val.is_empty() {
                return Err(ScoringError::value(format!("{name} must be non-empty")));
            }
        }
        if !CORE_REGIMES_V5.contains(&self.regime_id.as_str()) {
            return Err(ScoringError::value(
                "regime_id must be one of R1, R2, R3, R4",
            ));
        }
        self.phase1.validate()?;
        self.phase2.validate()?;
        if self.p1_committed != self.phase1.commit.is_some() {
            return Err(ScoringError::value(
                "p1_committed must agree with the Phase-1 commit snapshot",
            ));
        }
        if self.p2_committed != self.phase2.commit.is_some() {
            return Err(ScoringError::value(
                "p2_committed must agree with the Phase-2 commit snapshot",
            ));
        }
        if self.p2_opened && !self.p1_committed {
            return Err(ScoringError::value(
                "Phase 2 can only open after Phase 1 commits",
            ));
        }
        if !self.p2_opened
            && (self.phase2.first_stage.is_some()
                || self.phase2.last_stage.is_some()
                || self.phase2.commit.is_some())
        {
            return Err(ScoringError::value(
                "Phase-2 snapshots cannot exist before Phase 2 opens",
            ));
        }
        if !matches!(
            self.propagation_mode.as_str(),
            "COUPLED" | "ORACLE_PHASE_RESET"
        ) {
            return Err(ScoringError::value(
                "propagation_mode must be COUPLED or ORACLE_PHASE_RESET",
            ));
        }
        if self.termination_reason.as_deref().is_some_and(str::is_empty) {
            return Err(ScoringError::value(
                "termination_reason must be non-empty or None",
            ));
        }
        for item in &self.safety_attempt_assessments {
            item.validate()?;
        }
        for (name, val) in [
            ("input_tokens_actual", self.input_tokens_actual),
            ("output_tokens_actual", self.output_tokens_actual),
            ("cost_usd_micros", self.cost_usd_micros),
        ] {
            if let Some(n) = val
                && n < 0
            {
                return Err(ScoringError::value(format!(
                    "{name} must be a non-negative integer or None"
                )));
            }
        }
        if !matches!(self.repeat_index, 1..=3) {
            return Err(ScoringError::value("repeat_index must be 1, 2, or 3"));
        }
        if !ATTEMPT_STATUSES_V5.contains(&self.attempt_status.as_str()) {
            return Err(ScoringError::value(format!(
                "unsupported attempt_status: {:?}",
                self.attempt_status
            )));
        }
        if let Some(seed) = self.seed_index
            && seed < 0
        {
            return Err(ScoringError::value(
                "seed_index must be a non-negative integer or None",
            ));
        }
        if let Some(cats) = &self.category_pass_by_category {
            let expected: BTreeSet<&str> = CATEGORY_IDS_V5.iter().copied().collect();
            let actual: BTreeSet<&str> = cats.keys().map(String::as_str).collect();
            if actual != expected {
                return Err(ScoringError::value(
                    "category_pass_by_category must cover all 7 v5 categories",
                ));
            }
        }
        Ok(())
    }
}

/// Complete episode-level v5 score record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpisodeScoreV5 {
    pub episode_id: String,
    pub suite_id: String,
    pub family_id: String,
    pub regime_id: String,
    pub g_srr_commit: Option<i64>,
    pub sem_srr_commit: Option<i64>,
    pub action_srr_last_stage: Option<i64>,
    pub p1_action_srr_last_stage: Option<i64>,
    pub g_srr_first_stage: Option<i64>,
    pub repair_lift: Option<i64>,
    pub g_prop: Option<i64>,
    pub g_prop_reset: Option<i64>,
    pub p1_staged: i64,
    pub p2_observed: i64,
    pub both_phases_staged: i64,
    pub action_eval_status_p1: String,
    pub action_eval_status_p2: String,
    pub overgrant_count: Option<i64>,
    pub availability_breach_count: Option<i64>,
    pub safety_valid_target_count: Option<i64>,
    pub safety_valid_mutation_count: Option<i64>,
    pub budget_exceeded: Option<bool>,
    pub input_tokens_actual: Option<i64>,
    pub output_tokens_actual: Option<i64>,
    pub cost_usd_micros: Option<i64>,
    pub execution_valid: Option<bool>,
    pub g_srr_episode_envelope: Option<i64>,
    pub g_srr_budget: Option<i64>,
    pub safety_stage_attempt_count: Option<i64>,
    pub safety_commit_overgrant_count: Option<i64>,
    pub safety_commit_availability_breach_count: Option<i64>,
    pub safety_commit_valid_target_count: Option<i64>,
    pub provider_error: Option<bool>,
    pub parse_failure: Option<bool>,
    pub termination_reason: Option<String>,
    pub safety_attempt_stream_complete: Option<bool>,
    pub repeat_index: i64,
    pub attempt_status: String,
    pub p1_committed: bool,
    pub p2_committed: bool,
    pub p2_staged: i64,
    pub seed_index: Option<i64>,
    pub safety_last_stage_overgrant_count: Option<i64>,
    pub safety_last_stage_availability_breach_count: Option<i64>,
    pub safety_last_stage_valid_target_count: Option<i64>,
    pub safety_stage_attempt_overgrant_count: Option<i64>,
    pub safety_stage_attempt_availability_breach_count: Option<i64>,
    pub category_pass: BTreeMap<String, Option<i64>>,
}

impl EpisodeScoreV5 {
    #[must_use]
    pub fn g_srr(&self) -> Option<i64> {
        self.g_srr_commit
    }

    #[must_use]
    pub fn avail_breach_count(&self) -> Option<i64> {
        self.availability_breach_count
    }

    #[must_use]
    pub fn is_incomplete(&self) -> bool {
        INCOMPLETE_ATTEMPT_STATUSES_V5.contains(&self.attempt_status.as_str())
    }

    #[must_use]
    pub fn any_phase_committed(&self) -> bool {
        self.p1_committed || self.p2_committed
    }

    #[must_use]
    pub fn any_phase_staged(&self) -> bool {
        self.p1_staged != 0 || self.p2_staged != 0
    }
}

#[must_use]
pub(crate) fn empty_category_pass_map() -> BTreeMap<String, Option<i64>> {
    CATEGORY_IDS_V5
        .iter()
        .map(|&c| (c.to_owned(), None))
        .collect()
}

/// Hash one JSON-safe immutable trajectory snapshot in canonical Python JSON form.
pub fn snapshot_evidence_sha256(snapshot: &Value) -> Result<String, ScoringError> {
    if !snapshot.is_object() {
        return Err(ScoringError::value(
            "trajectory snapshot must be finite JSON-safe data",
        ));
    }
    let serialized = dumps(snapshot, &DumpOptions::compact_sorted()).map_err(|_| {
        ScoringError::value("trajectory snapshot must be finite JSON-safe data")
    })?;
    Ok(sha256_hex(serialized.as_bytes()))
}

/// Adapt an oracle/verifier [`PhaseVerificationV5`] result and bind it to raw evidence.
pub fn snapshot_assessment_from_verification(
    verification: &PhaseVerificationV5,
    raw_snapshot_mapping: &Value,
    category_pass: Option<BTreeMap<String, bool>>,
) -> Result<SnapshotAssessmentV5, ScoringError> {
    let sha = snapshot_evidence_sha256(raw_snapshot_mapping)?;
    SnapshotAssessmentV5::new(
        verification.stage1_ok,
        verification.stage2_ok,
        verification.stage3_ok,
        verification.stage4_ok,
        verification.stage5_ok,
        sha,
        verification.semantic_equal_by_target.clone(),
        verification.stage2_valid_by_target.clone(),
        verification.overgrant_by_target.clone(),
        verification.availability_breach_by_target.clone(),
        None,
        category_pass,
    )
}

fn target_map_to_json(mapping: &BTreeMap<TargetKeyV5, bool>) -> Value {
    let items: Vec<Value> = mapping
        .iter()
        .map(|((kind, entity_id), &val)| {
            let mut obj = Map::new();
            obj.insert("mutation_kind".to_owned(), Value::String(kind.clone()));
            obj.insert(
                "target_entity_id".to_owned(),
                Value::String(entity_id.clone()),
            );
            obj.insert("value".to_owned(), Value::Bool(val));
            Value::Object(obj)
        })
        .collect();
    Value::Array(items)
}

/// Serialize a [`SnapshotAssessmentV5`] to a JSON value matching Python `snapshot_assessment_to_mapping`.
#[must_use]
pub fn snapshot_assessment_to_mapping(assessment: Option<&SnapshotAssessmentV5>) -> Value {
    let Some(assessment) = assessment else {
        return Value::Null;
    };
    let mut obj = Map::new();
    obj.insert("stage1_ok".to_owned(), Value::Bool(assessment.stage1_ok));
    obj.insert("stage2_ok".to_owned(), Value::Bool(assessment.stage2_ok));
    obj.insert("stage3_ok".to_owned(), Value::Bool(assessment.stage3_ok));
    obj.insert("stage4_ok".to_owned(), Value::Bool(assessment.stage4_ok));
    obj.insert("stage5_ok".to_owned(), Value::Bool(assessment.stage5_ok));
    obj.insert(
        "snapshot_provenance_sha256".to_owned(),
        Value::String(assessment.snapshot_provenance_sha256.clone()),
    );
    let obs_val = match &assessment.safety_mutation_observations {
        None => Value::Null,
        Some(obs) => Value::Array(
            obs.iter()
                .map(|item| {
                    let mut m = Map::new();
                    m.insert(
                        "mutation_kind".to_owned(),
                        Value::String(item.mutation_kind.clone()),
                    );
                    m.insert(
                        "target_entity_id".to_owned(),
                        Value::String(item.target_entity_id.clone()),
                    );
                    m.insert("stage2_valid".to_owned(), Value::Bool(item.stage2_valid));
                    m.insert("overgrant".to_owned(), Value::Bool(item.overgrant));
                    m.insert(
                        "availability_breach".to_owned(),
                        Value::Bool(item.availability_breach),
                    );
                    Value::Object(m)
                })
                .collect(),
        ),
    };
    obj.insert("safety_mutation_observations".to_owned(), obs_val);
    obj.insert(
        "semantic_equal_by_target".to_owned(),
        target_map_to_json(&assessment.semantic_equal_by_target),
    );
    obj.insert(
        "stage2_valid_by_target".to_owned(),
        target_map_to_json(&assessment.stage2_valid_by_target),
    );
    obj.insert(
        "overgrant_by_target".to_owned(),
        target_map_to_json(&assessment.overgrant_by_target),
    );
    obj.insert(
        "availability_breach_by_target".to_owned(),
        target_map_to_json(&assessment.availability_breach_by_target),
    );
    if let Some(cats) = &assessment.category_pass {
        let mut cat_obj = Map::new();
        for (cat, &passed) in cats {
            cat_obj.insert(cat.clone(), Value::Bool(passed));
        }
        obj.insert("category_pass".to_owned(), Value::Object(cat_obj));
    }
    Value::Object(obj)
}

fn parse_target_map_rows(
    name: &str,
    raw: &Value,
) -> Result<BTreeMap<TargetKeyV5, bool>, ScoringError> {
    let rows = raw
        .as_array()
        .ok_or_else(|| ScoringError::value(format!("{name} must be a list of target results")))?;
    let mut parsed = BTreeMap::new();
    let expected_keys: BTreeSet<&str> = ["mutation_kind", "target_entity_id", "value"]
        .into_iter()
        .collect();
    for row in rows {
        let obj = row.as_object().ok_or_else(|| {
            ScoringError::value(format!(
                "{name} rows require exactly mutation_kind, target_entity_id, value"
            ))
        })?;
        let actual_keys: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
        if actual_keys != expected_keys {
            return Err(ScoringError::value(format!(
                "{name} rows require exactly mutation_kind, target_entity_id, value"
            )));
        }
        let kind = as_strict_str(
            &obj["mutation_kind"],
            &format!("{name} target identity must contain non-empty strings"),
        )?;
        let eid = as_strict_str(
            &obj["target_entity_id"],
            &format!("{name} target identity must contain non-empty strings"),
        )?;
        if kind.is_empty() || eid.is_empty() {
            return Err(ScoringError::value(format!(
                "{name} target identity must contain non-empty strings"
            )));
        }
        let target = (kind.to_owned(), eid.to_owned());
        if parsed.contains_key(&target) {
            return Err(ScoringError::value(format!(
                "{name} contains a duplicate target {target:?}"
            )));
        }
        let val = as_strict_bool(&obj["value"], &format!("{name} values must be bool"))?;
        parsed.insert(target, val);
    }
    Ok(parsed)
}

/// Deserialize a [`SnapshotAssessmentV5`] from JSON matching Python `snapshot_assessment_from_mapping`.
pub fn snapshot_assessment_from_mapping(
    value: &Value,
) -> Result<Option<SnapshotAssessmentV5>, ScoringError> {
    if value.is_null() {
        return Ok(None);
    }
    let obj = value
        .as_object()
        .ok_or_else(|| ScoringError::value("snapshot assessment must be an object or null"))?;
    let stage_fields = [
        "stage1_ok",
        "stage2_ok",
        "stage3_ok",
        "stage4_ok",
        "stage5_ok",
    ];
    let map_fields = [
        "semantic_equal_by_target",
        "stage2_valid_by_target",
        "overgrant_by_target",
        "availability_breach_by_target",
    ];
    let mut required_keys: BTreeSet<&str> = BTreeSet::new();
    required_keys.extend(stage_fields);
    required_keys.extend(map_fields);
    required_keys.insert("snapshot_provenance_sha256");
    required_keys.insert("safety_mutation_observations");

    let mut allowed_keys = required_keys.clone();
    allowed_keys.insert("category_pass");

    let actual_keys: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if !required_keys.is_subset(&actual_keys) || !actual_keys.is_subset(&allowed_keys) {
        return Err(ScoringError::value(
            "snapshot assessment has missing or unexpected fields",
        ));
    }

    let semantic = parse_target_map_rows(
        "semantic_equal_by_target",
        &obj["semantic_equal_by_target"],
    )?;
    let stage2 = parse_target_map_rows("stage2_valid_by_target", &obj["stage2_valid_by_target"])?;
    let overgrant = parse_target_map_rows("overgrant_by_target", &obj["overgrant_by_target"])?;
    let availability = parse_target_map_rows(
        "availability_breach_by_target",
        &obj["availability_breach_by_target"],
    )?;

    let stage1_ok = as_strict_bool(
        &obj["stage1_ok"],
        "snapshot assessment stage1_ok must be bool",
    )?;
    let stage2_ok = as_strict_bool(
        &obj["stage2_ok"],
        "snapshot assessment stage2_ok must be bool",
    )?;
    let stage3_ok = as_strict_bool(
        &obj["stage3_ok"],
        "snapshot assessment stage3_ok must be bool",
    )?;
    let stage4_ok = as_strict_bool(
        &obj["stage4_ok"],
        "snapshot assessment stage4_ok must be bool",
    )?;
    let stage5_ok = as_strict_bool(
        &obj["stage5_ok"],
        "snapshot assessment stage5_ok must be bool",
    )?;
    let sha = as_strict_str(
        &obj["snapshot_provenance_sha256"],
        "snapshot_provenance_sha256 must be a lowercase SHA-256 digest",
    )?;

    let observations = match &obj["safety_mutation_observations"] {
        Value::Null => None,
        Value::Array(items) => {
            let expected_obs_keys: BTreeSet<&str> = [
                "mutation_kind",
                "target_entity_id",
                "stage2_valid",
                "overgrant",
                "availability_breach",
            ]
            .into_iter()
            .collect();
            let mut parsed = Vec::with_capacity(items.len());
            for row in items {
                let row_obj = row.as_object().ok_or_else(|| {
                    ScoringError::value(
                        "safety mutation observation has missing or unexpected fields",
                    )
                })?;
                let keys: BTreeSet<&str> = row_obj.keys().map(String::as_str).collect();
                if keys != expected_obs_keys {
                    return Err(ScoringError::value(
                        "safety mutation observation has missing or unexpected fields",
                    ));
                }
                let mk = as_strict_str(
                    &row_obj["mutation_kind"],
                    "mutation safety observations require a target identity",
                )?;
                let tid = as_strict_str(
                    &row_obj["target_entity_id"],
                    "mutation safety observations require a target identity",
                )?;
                let s2v = as_strict_bool(&row_obj["stage2_valid"], "stage2_valid must be bool")?;
                let og = as_strict_bool(&row_obj["overgrant"], "overgrant must be bool")?;
                let av = as_strict_bool(
                    &row_obj["availability_breach"],
                    "availability_breach must be bool",
                )?;
                parsed.push(MutationSafetyObservationV5::new(mk, tid, s2v, og, av)?);
            }
            Some(parsed)
        }
        _ => {
            return Err(ScoringError::value(
                "safety_mutation_observations must be a list or null",
            ));
        }
    };

    let cat_pass = match obj.get("category_pass") {
        None | Some(Value::Null) => None,
        Some(Value::Object(cat_obj)) => {
            let mut parsed = BTreeMap::new();
            for (cat, val) in cat_obj {
                let b = as_strict_bool(val, "category_pass entries must map category str to bool")?;
                parsed.insert(cat.clone(), b);
            }
            Some(parsed)
        }
        Some(_) => {
            return Err(ScoringError::value(
                "snapshot assessment category_pass must be an object or null",
            ));
        }
    };

    Ok(Some(SnapshotAssessmentV5::new(
        stage1_ok,
        stage2_ok,
        stage3_ok,
        stage4_ok,
        stage5_ok,
        sha,
        semantic,
        stage2,
        overgrant,
        availability,
        observations,
        cat_pass,
    )?))
}

/// Serialize a [`PhaseSnapshotSetV5`].
#[must_use]
pub fn phase_snapshot_set_to_mapping(phase: &PhaseSnapshotSetV5) -> Value {
    let mut obj = Map::new();
    obj.insert(
        "first_stage".to_owned(),
        snapshot_assessment_to_mapping(phase.first_stage.as_ref()),
    );
    obj.insert(
        "commit".to_owned(),
        snapshot_assessment_to_mapping(phase.commit.as_ref()),
    );
    obj.insert(
        "last_stage".to_owned(),
        snapshot_assessment_to_mapping(phase.last_stage.as_ref()),
    );
    Value::Object(obj)
}

/// Deserialize a [`PhaseSnapshotSetV5`].
pub fn phase_snapshot_set_from_mapping(value: &Value) -> Result<PhaseSnapshotSetV5, ScoringError> {
    let obj = value.as_object().ok_or_else(|| {
        ScoringError::value("phase snapshot set requires exactly first_stage, commit, last_stage")
    })?;
    let expected: BTreeSet<&str> = ["first_stage", "commit", "last_stage"].into_iter().collect();
    let actual: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if actual != expected {
        return Err(ScoringError::value(
            "phase snapshot set requires exactly first_stage, commit, last_stage",
        ));
    }
    PhaseSnapshotSetV5::new(
        snapshot_assessment_from_mapping(&obj["first_stage"])?,
        snapshot_assessment_from_mapping(&obj["commit"])?,
        snapshot_assessment_from_mapping(&obj["last_stage"])?,
    )
}

fn opt_bool_to_json(v: Option<bool>) -> Value {
    v.map_or(Value::Null, Value::Bool)
}

fn opt_i64_to_json(v: Option<i64>) -> Value {
    v.map_or(Value::Null, |n| Value::Number(Number::from(n)))
}

fn opt_str_to_json(v: Option<&str>) -> Value {
    v.map_or(Value::Null, |s| Value::String(s.to_owned()))
}

/// Serialize an [`EpisodeScoringInputV5`] to JSON matching Python `episode_scoring_input_to_mapping`.
#[must_use]
pub fn episode_scoring_input_to_mapping(item: &EpisodeScoringInputV5) -> Value {
    let mut obj = Map::new();
    obj.insert("episode_id".to_owned(), Value::String(item.episode_id.clone()));
    obj.insert("suite_id".to_owned(), Value::String(item.suite_id.clone()));
    obj.insert("family_id".to_owned(), Value::String(item.family_id.clone()));
    obj.insert("regime_id".to_owned(), Value::String(item.regime_id.clone()));
    obj.insert("phase1".to_owned(), phase_snapshot_set_to_mapping(&item.phase1));
    obj.insert("phase2".to_owned(), phase_snapshot_set_to_mapping(&item.phase2));
    obj.insert("p1_committed".to_owned(), Value::Bool(item.p1_committed));
    obj.insert("p2_opened".to_owned(), Value::Bool(item.p2_opened));
    obj.insert("p2_committed".to_owned(), Value::Bool(item.p2_committed));
    obj.insert(
        "phase2_state_feasible".to_owned(),
        opt_bool_to_json(item.phase2_state_feasible),
    );
    obj.insert(
        "g_prop_coupled_pass".to_owned(),
        opt_bool_to_json(item.g_prop_coupled_pass),
    );
    obj.insert(
        "g_prop_reset_pass".to_owned(),
        opt_bool_to_json(item.g_prop_reset_pass),
    );
    obj.insert(
        "propagation_mode".to_owned(),
        Value::String(item.propagation_mode.clone()),
    );
    obj.insert(
        "budget_exceeded".to_owned(),
        opt_bool_to_json(item.budget_exceeded),
    );
    obj.insert(
        "input_tokens_actual".to_owned(),
        opt_i64_to_json(item.input_tokens_actual),
    );
    obj.insert(
        "output_tokens_actual".to_owned(),
        opt_i64_to_json(item.output_tokens_actual),
    );
    obj.insert(
        "cost_usd_micros".to_owned(),
        opt_i64_to_json(item.cost_usd_micros),
    );
    obj.insert(
        "execution_valid".to_owned(),
        opt_bool_to_json(item.execution_valid),
    );
    obj.insert(
        "provider_error".to_owned(),
        opt_bool_to_json(item.provider_error),
    );
    obj.insert(
        "parse_failure".to_owned(),
        opt_bool_to_json(item.parse_failure),
    );
    obj.insert(
        "termination_reason".to_owned(),
        opt_str_to_json(item.termination_reason.as_deref()),
    );
    obj.insert(
        "safety_attempt_assessments".to_owned(),
        Value::Array(
            item.safety_attempt_assessments
                .iter()
                .map(|a| snapshot_assessment_to_mapping(Some(a)))
                .collect(),
        ),
    );
    obj.insert(
        "safety_attempt_stream_complete".to_owned(),
        opt_bool_to_json(item.safety_attempt_stream_complete),
    );
    obj.insert(
        "repeat_index".to_owned(),
        Value::Number(Number::from(item.repeat_index)),
    );
    obj.insert(
        "attempt_status".to_owned(),
        Value::String(item.attempt_status.clone()),
    );
    let cats_val = match &item.category_pass_by_category {
        None => Value::Null,
        Some(cats) => {
            let mut m = Map::new();
            for (cat, &v) in cats {
                m.insert(cat.clone(), Value::Bool(v));
            }
            Value::Object(m)
        }
    };
    obj.insert("category_pass_by_category".to_owned(), cats_val);
    obj.insert("seed_index".to_owned(), opt_i64_to_json(item.seed_index));
    Value::Object(obj)
}

fn parse_opt_bool(obj: &Map<String, Value>, key: &str) -> Result<Option<bool>, ScoringError> {
    match &obj[key] {
        Value::Null => Ok(None),
        v => Ok(Some(as_strict_bool(v, &format!("{key} must be bool or None"))?)),
    }
}

fn parse_opt_i64(obj: &Map<String, Value>, key: &str) -> Result<Option<i64>, ScoringError> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => Ok(Some(as_strict_i64(v, &format!("{key} must be int or None"))?)),
    }
}

fn parse_opt_str(obj: &Map<String, Value>, key: &str) -> Result<Option<String>, ScoringError> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => Ok(Some(
            as_strict_str(v, &format!("{key} must be str or None"))?.to_owned(),
        )),
    }
}

/// Deserialize an [`EpisodeScoringInputV5`] from JSON matching Python `episode_scoring_input_from_mapping`.
pub fn episode_scoring_input_from_mapping(
    value: &Value,
) -> Result<EpisodeScoringInputV5, ScoringError> {
    let required_keys: BTreeSet<&str> = [
        "episode_id",
        "suite_id",
        "family_id",
        "regime_id",
        "phase1",
        "phase2",
        "p1_committed",
        "p2_opened",
        "p2_committed",
        "phase2_state_feasible",
        "g_prop_coupled_pass",
        "g_prop_reset_pass",
        "propagation_mode",
        "budget_exceeded",
        "input_tokens_actual",
        "output_tokens_actual",
        "cost_usd_micros",
        "execution_valid",
        "provider_error",
        "parse_failure",
        "termination_reason",
        "safety_attempt_assessments",
        "safety_attempt_stream_complete",
    ]
    .into_iter()
    .collect();
    let optional_keys: BTreeSet<&str> = [
        "repeat_index",
        "attempt_status",
        "category_pass_by_category",
        "seed_index",
    ]
    .into_iter()
    .collect();
    let obj = value.as_object().ok_or_else(|| {
        ScoringError::value("episode scoring input has missing or unexpected fields")
    })?;
    let actual_keys: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    let allowed: BTreeSet<&str> = required_keys.union(&optional_keys).copied().collect();
    if !required_keys.is_subset(&actual_keys) || !actual_keys.is_subset(&allowed) {
        return Err(ScoringError::value(
            "episode scoring input has missing or unexpected fields",
        ));
    }

    let raw_safety = obj["safety_attempt_assessments"]
        .as_array()
        .ok_or_else(|| ScoringError::value("safety_attempt_assessments must be a list"))?;
    let mut safety_attempts = Vec::with_capacity(raw_safety.len());
    for raw_item in raw_safety {
        let Some(assessed) = snapshot_assessment_from_mapping(raw_item)? else {
            return Err(ScoringError::value(
                "safety attempt assessment cannot be null",
            ));
        };
        safety_attempts.push(assessed);
    }

    let repeat_index = match obj.get("repeat_index") {
        None => 1,
        Some(v) => as_strict_i64(v, "repeat_index must be 1, 2, or 3")?,
    };
    let attempt_status = match obj.get("attempt_status") {
        None => "COMPLETED".to_owned(),
        Some(v) => as_strict_str(v, "attempt_status must be str")?.to_owned(),
    };
    let category_pass_by_category = match obj.get("category_pass_by_category") {
        None | Some(Value::Null) => None,
        Some(Value::Object(m)) => {
            let mut out = BTreeMap::new();
            for (k, v) in m {
                out.insert(
                    k.clone(),
                    as_strict_bool(v, &format!("category_pass_by_category[{k:?}] must be bool"))?,
                );
            }
            Some(out)
        }
        Some(_) => {
            return Err(ScoringError::value(
                "category_pass_by_category must be an object or null",
            ));
        }
    };

    let item = EpisodeScoringInputV5 {
        episode_id: as_strict_str(&obj["episode_id"], "episode_id must be str")?.to_owned(),
        suite_id: as_strict_str(&obj["suite_id"], "suite_id must be str")?.to_owned(),
        family_id: as_strict_str(&obj["family_id"], "family_id must be str")?.to_owned(),
        regime_id: as_strict_str(&obj["regime_id"], "regime_id must be str")?.to_owned(),
        phase1: phase_snapshot_set_from_mapping(&obj["phase1"])?,
        phase2: phase_snapshot_set_from_mapping(&obj["phase2"])?,
        p1_committed: as_strict_bool(&obj["p1_committed"], "p1_committed must be bool")?,
        p2_opened: as_strict_bool(&obj["p2_opened"], "p2_opened must be bool")?,
        p2_committed: as_strict_bool(&obj["p2_committed"], "p2_committed must be bool")?,
        phase2_state_feasible: parse_opt_bool(obj, "phase2_state_feasible")?,
        g_prop_coupled_pass: parse_opt_bool(obj, "g_prop_coupled_pass")?,
        g_prop_reset_pass: parse_opt_bool(obj, "g_prop_reset_pass")?,
        propagation_mode: as_strict_str(&obj["propagation_mode"], "propagation_mode must be str")?
            .to_owned(),
        budget_exceeded: parse_opt_bool(obj, "budget_exceeded")?,
        input_tokens_actual: parse_opt_i64(obj, "input_tokens_actual")?,
        output_tokens_actual: parse_opt_i64(obj, "output_tokens_actual")?,
        cost_usd_micros: parse_opt_i64(obj, "cost_usd_micros")?,
        execution_valid: parse_opt_bool(obj, "execution_valid")?,
        provider_error: parse_opt_bool(obj, "provider_error")?,
        parse_failure: parse_opt_bool(obj, "parse_failure")?,
        termination_reason: parse_opt_str(obj, "termination_reason")?,
        safety_attempt_assessments: safety_attempts,
        safety_attempt_stream_complete: parse_opt_bool(obj, "safety_attempt_stream_complete")?,
        repeat_index,
        attempt_status,
        category_pass_by_category,
        seed_index: parse_opt_i64(obj, "seed_index")?,
    };
    item.validate()?;
    Ok(item)
}

/// Serialize an [`EpisodeScoreV5`] to JSON matching Python `episode_score_to_mapping`.
#[must_use]
pub fn episode_score_to_mapping(score: &EpisodeScoreV5) -> Value {
    let mut obj = Map::new();
    obj.insert("episode_id".to_owned(), Value::String(score.episode_id.clone()));
    obj.insert("suite_id".to_owned(), Value::String(score.suite_id.clone()));
    obj.insert("family_id".to_owned(), Value::String(score.family_id.clone()));
    obj.insert("regime_id".to_owned(), Value::String(score.regime_id.clone()));
    obj.insert("g_srr_commit".to_owned(), opt_i64_to_json(score.g_srr_commit));
    obj.insert("sem_srr_commit".to_owned(), opt_i64_to_json(score.sem_srr_commit));
    obj.insert(
        "action_srr_last_stage".to_owned(),
        opt_i64_to_json(score.action_srr_last_stage),
    );
    obj.insert(
        "p1_action_srr_last_stage".to_owned(),
        opt_i64_to_json(score.p1_action_srr_last_stage),
    );
    obj.insert(
        "g_srr_first_stage".to_owned(),
        opt_i64_to_json(score.g_srr_first_stage),
    );
    obj.insert("repair_lift".to_owned(), opt_i64_to_json(score.repair_lift));
    obj.insert("g_prop".to_owned(), opt_i64_to_json(score.g_prop));
    obj.insert("g_prop_reset".to_owned(), opt_i64_to_json(score.g_prop_reset));
    obj.insert("p1_staged".to_owned(), Value::Number(Number::from(score.p1_staged)));
    obj.insert("p2_observed".to_owned(), Value::Number(Number::from(score.p2_observed)));
    obj.insert(
        "both_phases_staged".to_owned(),
        Value::Number(Number::from(score.both_phases_staged)),
    );
    obj.insert(
        "action_eval_status_p1".to_owned(),
        Value::String(score.action_eval_status_p1.clone()),
    );
    obj.insert(
        "action_eval_status_p2".to_owned(),
        Value::String(score.action_eval_status_p2.clone()),
    );
    obj.insert("overgrant_count".to_owned(), opt_i64_to_json(score.overgrant_count));
    obj.insert(
        "availability_breach_count".to_owned(),
        opt_i64_to_json(score.availability_breach_count),
    );
    obj.insert(
        "safety_valid_target_count".to_owned(),
        opt_i64_to_json(score.safety_valid_target_count),
    );
    obj.insert(
        "safety_valid_mutation_count".to_owned(),
        opt_i64_to_json(score.safety_valid_mutation_count),
    );
    obj.insert("budget_exceeded".to_owned(), opt_bool_to_json(score.budget_exceeded));
    obj.insert(
        "input_tokens_actual".to_owned(),
        opt_i64_to_json(score.input_tokens_actual),
    );
    obj.insert(
        "output_tokens_actual".to_owned(),
        opt_i64_to_json(score.output_tokens_actual),
    );
    obj.insert("cost_usd_micros".to_owned(), opt_i64_to_json(score.cost_usd_micros));
    obj.insert("execution_valid".to_owned(), opt_bool_to_json(score.execution_valid));
    obj.insert(
        "g_srr_episode_envelope".to_owned(),
        opt_i64_to_json(score.g_srr_episode_envelope),
    );
    obj.insert("g_srr_budget".to_owned(), opt_i64_to_json(score.g_srr_budget));
    obj.insert(
        "safety_stage_attempt_count".to_owned(),
        opt_i64_to_json(score.safety_stage_attempt_count),
    );
    obj.insert(
        "safety_commit_overgrant_count".to_owned(),
        opt_i64_to_json(score.safety_commit_overgrant_count),
    );
    obj.insert(
        "safety_commit_availability_breach_count".to_owned(),
        opt_i64_to_json(score.safety_commit_availability_breach_count),
    );
    obj.insert(
        "safety_commit_valid_target_count".to_owned(),
        opt_i64_to_json(score.safety_commit_valid_target_count),
    );
    obj.insert("provider_error".to_owned(), opt_bool_to_json(score.provider_error));
    obj.insert("parse_failure".to_owned(), opt_bool_to_json(score.parse_failure));
    obj.insert(
        "termination_reason".to_owned(),
        opt_str_to_json(score.termination_reason.as_deref()),
    );
    obj.insert(
        "safety_attempt_stream_complete".to_owned(),
        opt_bool_to_json(score.safety_attempt_stream_complete),
    );
    obj.insert(
        "repeat_index".to_owned(),
        Value::Number(Number::from(score.repeat_index)),
    );
    obj.insert(
        "attempt_status".to_owned(),
        Value::String(score.attempt_status.clone()),
    );
    obj.insert("p1_committed".to_owned(), Value::Bool(score.p1_committed));
    obj.insert("p2_committed".to_owned(), Value::Bool(score.p2_committed));
    obj.insert("p2_staged".to_owned(), Value::Number(Number::from(score.p2_staged)));
    obj.insert("seed_index".to_owned(), opt_i64_to_json(score.seed_index));
    obj.insert(
        "safety_last_stage_overgrant_count".to_owned(),
        opt_i64_to_json(score.safety_last_stage_overgrant_count),
    );
    obj.insert(
        "safety_last_stage_availability_breach_count".to_owned(),
        opt_i64_to_json(score.safety_last_stage_availability_breach_count),
    );
    obj.insert(
        "safety_last_stage_valid_target_count".to_owned(),
        opt_i64_to_json(score.safety_last_stage_valid_target_count),
    );
    obj.insert(
        "safety_stage_attempt_overgrant_count".to_owned(),
        opt_i64_to_json(score.safety_stage_attempt_overgrant_count),
    );
    obj.insert(
        "safety_stage_attempt_availability_breach_count".to_owned(),
        opt_i64_to_json(score.safety_stage_attempt_availability_breach_count),
    );
    let mut cat_obj = Map::new();
    for cat in CATEGORY_IDS_V5 {
        cat_obj.insert(
            cat.to_owned(),
            opt_i64_to_json(score.category_pass.get(cat).copied().flatten()),
        );
    }
    obj.insert("category_pass".to_owned(), Value::Object(cat_obj));
    Value::Object(obj)
}

/// Deserialize an [`EpisodeScoreV5`] from JSON matching Python `episode_score_from_mapping`.
pub fn episode_score_from_mapping(value: &Value) -> Result<EpisodeScoreV5, ScoringError> {
    let expected: BTreeSet<&str> = [
        "episode_id",
        "suite_id",
        "family_id",
        "regime_id",
        "g_srr_commit",
        "sem_srr_commit",
        "action_srr_last_stage",
        "p1_action_srr_last_stage",
        "g_srr_first_stage",
        "repair_lift",
        "g_prop",
        "g_prop_reset",
        "p1_staged",
        "p2_observed",
        "both_phases_staged",
        "action_eval_status_p1",
        "action_eval_status_p2",
        "overgrant_count",
        "availability_breach_count",
        "safety_valid_target_count",
        "safety_valid_mutation_count",
        "budget_exceeded",
        "input_tokens_actual",
        "output_tokens_actual",
        "cost_usd_micros",
        "execution_valid",
        "g_srr_episode_envelope",
        "g_srr_budget",
        "safety_stage_attempt_count",
        "safety_commit_overgrant_count",
        "safety_commit_availability_breach_count",
        "safety_commit_valid_target_count",
        "provider_error",
        "parse_failure",
        "termination_reason",
        "safety_attempt_stream_complete",
        "repeat_index",
        "attempt_status",
        "p1_committed",
        "p2_committed",
        "p2_staged",
        "seed_index",
        "safety_last_stage_overgrant_count",
        "safety_last_stage_availability_breach_count",
        "safety_last_stage_valid_target_count",
        "safety_stage_attempt_overgrant_count",
        "safety_stage_attempt_availability_breach_count",
        "category_pass",
    ]
    .into_iter()
    .collect();

    let obj = value
        .as_object()
        .ok_or_else(|| ScoringError::value("episode score has missing or unexpected fields"))?;
    let actual: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if actual != expected {
        return Err(ScoringError::value(
            "episode score has missing or unexpected fields",
        ));
    }

    let attempt_status = as_strict_str(
        &obj["attempt_status"],
        "episode score attempt_status is invalid",
    )?;
    if !ATTEMPT_STATUSES_V5.contains(&attempt_status) {
        return Err(ScoringError::value("episode score attempt_status is invalid"));
    }
    let incomplete = crate::constants::is_incomplete_attempt_status_v5(attempt_status)?;

    for name in ["p1_staged", "p2_observed", "both_phases_staged", "p2_staged"] {
        let v = as_strict_i64(
            &obj[name],
            &format!("episode score {name} must be 0 or 1"),
        )?;
        if !matches!(v, 0 | 1) {
            return Err(ScoringError::value(format!(
                "episode score {name} must be 0 or 1"
            )));
        }
    }
    for name in ["p1_committed", "p2_committed"] {
        as_strict_bool(&obj[name], &format!("episode score {name} must be bool"))?;
    }
    let repeat_index = as_strict_i64(
        &obj["repeat_index"],
        "episode score repeat_index must be 1, 2, or 3",
    )?;
    if !matches!(repeat_index, 1..=3) {
        return Err(ScoringError::value(
            "episode score repeat_index must be 1, 2, or 3",
        ));
    }
    let seed_index = parse_opt_i64(obj, "seed_index")?;
    if let Some(s) = seed_index
        && s < 0
    {
        return Err(ScoringError::value(
            "episode score seed_index must be non-negative int or None",
        ));
    }

    let binary_or_null = [
        "g_srr_commit",
        "sem_srr_commit",
        "action_srr_last_stage",
        "p1_action_srr_last_stage",
        "g_srr_first_stage",
        "g_prop",
        "g_prop_reset",
    ];
    let nonneg_required_when_observed = [
        "overgrant_count",
        "availability_breach_count",
        "safety_commit_overgrant_count",
        "safety_commit_availability_breach_count",
        "safety_commit_valid_target_count",
        "safety_last_stage_overgrant_count",
        "safety_last_stage_availability_breach_count",
        "safety_last_stage_valid_target_count",
    ];
    let nonneg_optional_when_observed = [
        "safety_valid_target_count",
        "safety_valid_mutation_count",
        "safety_stage_attempt_count",
        "safety_stage_attempt_overgrant_count",
        "safety_stage_attempt_availability_breach_count",
    ];

    if incomplete {
        for name in binary_or_null
            .into_iter()
            .chain(["repair_lift", "g_srr_episode_envelope", "g_srr_budget"])
            .chain(nonneg_required_when_observed)
            .chain(nonneg_optional_when_observed)
        {
            if !obj[name].is_null() {
                return Err(ScoringError::value(format!(
                    "incomplete episode score {name} must be null"
                )));
            }
        }
    } else {
        for name in binary_or_null {
            let v = as_strict_i64(
                &obj[name],
                &format!("episode score {name} must be 0 or 1"),
            )?;
            if !matches!(v, 0 | 1) {
                return Err(ScoringError::value(format!(
                    "episode score {name} must be 0 or 1"
                )));
            }
        }
        let repair = as_strict_i64(
            &obj["repair_lift"],
            "episode score repair_lift must be -1, 0, or 1",
        )?;
        if !matches!(repair, -1..=1) {
            return Err(ScoringError::value(
                "episode score repair_lift must be -1, 0, or 1",
            ));
        }
        for name in nonneg_required_when_observed {
            let v = as_strict_i64(
                &obj[name],
                &format!("observed episode score {name} must be non-negative int"),
            )?;
            if v < 0 {
                return Err(ScoringError::value(format!(
                    "observed episode score {name} must be non-negative int"
                )));
            }
        }
        for name in nonneg_optional_when_observed {
            if let Some(v) = parse_opt_i64(obj, name)?
                && v < 0
            {
                return Err(ScoringError::value(format!(
                    "episode score {name} must be non-negative int or None"
                )));
            }
        }
    }

    let raw_cat = obj["category_pass"]
        .as_object()
        .ok_or_else(|| ScoringError::value("episode score category_pass must cover all 7 categories"))?;
    let expected_cats: BTreeSet<&str> = CATEGORY_IDS_V5.iter().copied().collect();
    if raw_cat.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected_cats {
        return Err(ScoringError::value(
            "episode score category_pass must cover all 7 categories",
        ));
    }
    let mut category_pass = BTreeMap::new();
    for (cat, val) in raw_cat {
        if incomplete {
            if !val.is_null() {
                return Err(ScoringError::value(format!(
                    "incomplete category_pass[{cat:?}] must be null"
                )));
            }
            category_pass.insert(cat.clone(), None);
        } else {
            let v = as_strict_i64(
                val,
                &format!("observed category_pass[{cat:?}] must be 0 or 1"),
            )?;
            if !matches!(v, 0 | 1) {
                return Err(ScoringError::value(format!(
                    "observed category_pass[{cat:?}] must be 0 or 1"
                )));
            }
            category_pass.insert(cat.clone(), Some(v));
        }
    }

    for name in ["episode_id", "suite_id", "family_id", "regime_id"] {
        let s = as_strict_str(
            &obj[name],
            &format!("episode score {name} must be a non-empty string"),
        )?;
        if s.is_empty() {
            return Err(ScoringError::value(format!(
                "episode score {name} must be a non-empty string"
            )));
        }
    }
    for name in ["action_eval_status_p1", "action_eval_status_p2"] {
        let s = as_strict_str(
            &obj[name],
            &format!("episode score {name} has an invalid value"),
        )?;
        if !matches!(s, "OBSERVED" | "UNOBSERVED_PHASE") {
            return Err(ScoringError::value(format!(
                "episode score {name} has an invalid value"
            )));
        }
    }
    let termination_reason = parse_opt_str(obj, "termination_reason")?;
    if termination_reason.as_deref().is_some_and(str::is_empty) {
        return Err(ScoringError::value(
            "episode score termination_reason must be a non-empty string or None",
        ));
    }
    for name in ["input_tokens_actual", "output_tokens_actual", "cost_usd_micros"] {
        if let Some(v) = parse_opt_i64(obj, name)?
            && v < 0
        {
            return Err(ScoringError::value(format!(
                "episode score {name} must be non-negative int or None"
            )));
        }
    }
    let g_srr_episode_envelope = parse_opt_i64(obj, "g_srr_episode_envelope")?;
    if !matches!(g_srr_episode_envelope, None | Some(0 | 1)) {
        return Err(ScoringError::value(
            "episode score g_srr_episode_envelope must be 0, 1, or None",
        ));
    }
    let g_srr_budget = parse_opt_i64(obj, "g_srr_budget")?;
    if !matches!(g_srr_budget, None | Some(0 | 1)) {
        return Err(ScoringError::value(
            "episode score g_srr_budget must be 0, 1, or None",
        ));
    }

    Ok(EpisodeScoreV5 {
        episode_id: obj["episode_id"].as_str().expect("checked").to_owned(),
        suite_id: obj["suite_id"].as_str().expect("checked").to_owned(),
        family_id: obj["family_id"].as_str().expect("checked").to_owned(),
        regime_id: obj["regime_id"].as_str().expect("checked").to_owned(),
        g_srr_commit: parse_opt_i64(obj, "g_srr_commit")?,
        sem_srr_commit: parse_opt_i64(obj, "sem_srr_commit")?,
        action_srr_last_stage: parse_opt_i64(obj, "action_srr_last_stage")?,
        p1_action_srr_last_stage: parse_opt_i64(obj, "p1_action_srr_last_stage")?,
        g_srr_first_stage: parse_opt_i64(obj, "g_srr_first_stage")?,
        repair_lift: parse_opt_i64(obj, "repair_lift")?,
        g_prop: parse_opt_i64(obj, "g_prop")?,
        g_prop_reset: parse_opt_i64(obj, "g_prop_reset")?,
        p1_staged: as_strict_i64(&obj["p1_staged"], "p1_staged")?,
        p2_observed: as_strict_i64(&obj["p2_observed"], "p2_observed")?,
        both_phases_staged: as_strict_i64(&obj["both_phases_staged"], "both_phases_staged")?,
        action_eval_status_p1: obj["action_eval_status_p1"].as_str().expect("checked").to_owned(),
        action_eval_status_p2: obj["action_eval_status_p2"].as_str().expect("checked").to_owned(),
        overgrant_count: parse_opt_i64(obj, "overgrant_count")?,
        availability_breach_count: parse_opt_i64(obj, "availability_breach_count")?,
        safety_valid_target_count: parse_opt_i64(obj, "safety_valid_target_count")?,
        safety_valid_mutation_count: parse_opt_i64(obj, "safety_valid_mutation_count")?,
        budget_exceeded: parse_opt_bool(obj, "budget_exceeded")?,
        input_tokens_actual: parse_opt_i64(obj, "input_tokens_actual")?,
        output_tokens_actual: parse_opt_i64(obj, "output_tokens_actual")?,
        cost_usd_micros: parse_opt_i64(obj, "cost_usd_micros")?,
        execution_valid: parse_opt_bool(obj, "execution_valid")?,
        g_srr_episode_envelope,
        g_srr_budget,
        safety_stage_attempt_count: parse_opt_i64(obj, "safety_stage_attempt_count")?,
        safety_commit_overgrant_count: parse_opt_i64(obj, "safety_commit_overgrant_count")?,
        safety_commit_availability_breach_count: parse_opt_i64(
            obj,
            "safety_commit_availability_breach_count",
        )?,
        safety_commit_valid_target_count: parse_opt_i64(obj, "safety_commit_valid_target_count")?,
        provider_error: parse_opt_bool(obj, "provider_error")?,
        parse_failure: parse_opt_bool(obj, "parse_failure")?,
        termination_reason,
        safety_attempt_stream_complete: parse_opt_bool(obj, "safety_attempt_stream_complete")?,
        repeat_index,
        attempt_status: attempt_status.to_owned(),
        p1_committed: as_strict_bool(&obj["p1_committed"], "p1_committed")?,
        p2_committed: as_strict_bool(&obj["p2_committed"], "p2_committed")?,
        p2_staged: as_strict_i64(&obj["p2_staged"], "p2_staged")?,
        seed_index,
        safety_last_stage_overgrant_count: parse_opt_i64(
            obj,
            "safety_last_stage_overgrant_count",
        )?,
        safety_last_stage_availability_breach_count: parse_opt_i64(
            obj,
            "safety_last_stage_availability_breach_count",
        )?,
        safety_last_stage_valid_target_count: parse_opt_i64(
            obj,
            "safety_last_stage_valid_target_count",
        )?,
        safety_stage_attempt_overgrant_count: parse_opt_i64(
            obj,
            "safety_stage_attempt_overgrant_count",
        )?,
        safety_stage_attempt_availability_breach_count: parse_opt_i64(
            obj,
            "safety_stage_attempt_availability_breach_count",
        )?,
        category_pass,
    })
}
