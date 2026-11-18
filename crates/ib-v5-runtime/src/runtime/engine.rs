//! Stateful delivered-view runtime state machine (`RuntimeV5`) and tool semantics.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use ib_v5_schemas::{
    AtomicMutationV5, CheckerReceiptStatusV5, CheckerReceiptV5, CoSignReceiptStatusV5,
    CoSignReceiptV5, CoSignScopeKindV5, CommitReceiptStatusV5, CommitReceiptV5, DeliveredSliceV5,
    EpisodeV5, PhaseManifestV5, QuerySourcesResponseV5, QuerySourcesStatusV5, STAGE_ID_RE,
    StageReceiptStatusV5, StageReceiptV5, SteerProcessDirectiveV5, SteerReceiptStatusV5,
    SteerReceiptV5, SteerTriggerReasonV5, TerminalWorldStateV5, ViolationCategoryV5, WorldStateV5,
};
use indexmap::IndexMap;
use serde_json::{Map, Value};

use crate::verifier::VerifierContextError;

use super::ledger::{build_visible_prior_ledger, jcs_sha256_value};
use super::transitions::{apply_phase1_with_queue, apply_phase2, apply_phase2_environment_delta};
use super::view::{
    RuntimeError, ViewV5, all_evidence_ids, completeness_ok, mutation_key, structural_errors,
    structural_ok,
};

const TOPOLOGY_IDS: [&str; 8] = [
    "T0_SINGLE_MONOLITH",
    "T1_DOMAIN_SPECIALISTS",
    "T2_REVIEWER_COUNCIL",
    "T3_MAKER_CHECKER_GUARDRAIL",
    "T4_PARALLEL_SWARM_FENCED",
    "T5_HIERARCHICAL_PIPELINE",
    "SINGLE_AGENT_UNIFIED",
    "MULTI_AGENT_SPECIALIZED",
];

const STEERING_MODES: [&str; 3] = ["UNSTEERED", "CHECKPOINT_STEERED", "FULL_LIVE_STEERED"];

pub type CoSignTuple = (CoSignScopeKindV5, String, String);
pub type CoSignMintKey = (String, i64, String, String, CoSignTuple);
pub type SteerMintKey = (String, i64, String, String);

/// Token usage totals recorded on runtime snapshots.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TokenTotalsV5 {
    pub input_tokens_uncached: i64,
    pub cached_input_tokens: i64,
    pub output_tokens: i64,
    pub reasoning_tokens: i64,
}

impl TokenTotalsV5 {
    #[must_use]
    pub const fn input_tokens(&self) -> i64 {
        self.input_tokens_uncached + self.cached_input_tokens
    }

    #[must_use]
    pub const fn input_tokens_actual(&self) -> i64 {
        self.input_tokens_uncached + self.cached_input_tokens
    }

    #[must_use]
    pub const fn output_tokens_actual(&self) -> i64 {
        self.output_tokens + self.reasoning_tokens
    }
}

/// Immutable snapshot frozen at a `STAGE_ATTEMPT` or `COMMIT_ATTEMPT` boundary.
#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeSnapshotV5 {
    pub event: String,
    pub phase_index: i64,
    pub stage_id: String,
    pub mutations: Vec<AtomicMutationV5>,
    pub preconditions: Vec<String>,
    pub citations: Vec<String>,
    pub stage_digest: String,
    pub dependency_digest: String,
    pub view: ViewV5,
    pub delivered_slices: Vec<DeliveredSliceV5>,
    pub delivered_slice_ids: Vec<String>,
    pub read_ledger: BTreeSet<String>,
    pub remaining_query_budget: i64,
    pub world_rev: i64,
    pub world_rev_before: i64,
    pub world_rev_after: i64,
    pub token_totals: TokenTotalsV5,
    pub receipt_status: String,
}

/// Per-phase snapshot bundle (`first_stage`, `last_stage`, `commit`, and all `stage_attempts`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PhaseSnapshotSetV5 {
    pub first_stage: Option<RuntimeSnapshotV5>,
    pub last_stage: Option<RuntimeSnapshotV5>,
    pub commit: Option<RuntimeSnapshotV5>,
    pub stage_attempts: Vec<RuntimeSnapshotV5>,
}

/// Diagnostic backend trait injected into `RuntimeV5` for gated checker and steer calls.
pub trait DiagnosticServiceV5: Send + Sync {
    #[allow(clippy::too_many_arguments)]
    fn diagnose(
        &self,
        phase_index: i64,
        stage_id: &str,
        view: &ViewV5,
        mutations: &[AtomicMutationV5],
        preconditions: &[String],
        citations: &[String],
        read_ledger: &BTreeSet<String>,
        stage1_ok: bool,
        stage2_ok: bool,
    ) -> Result<(Option<usize>, Vec<ViolationCategoryV5>), VerifierContextError>;

    fn has_unread_gold_evidence(
        &self,
        phase_index: i64,
        view: &ViewV5,
        read_ledger: &BTreeSet<String>,
    ) -> Result<bool, VerifierContextError>;
}

#[derive(Debug, Clone)]
struct StagedBuffer {
    phase_index: i64,
    stage_id: String,
    mutations: Vec<AtomicMutationV5>,
    stage_digest: String,
    dependency_digest: String,
}

/// Configuration options for [`RuntimeV5::with_options`].
#[derive(Clone)]
pub struct RuntimeOptionsV5 {
    pub topology: String,
    pub steering_mode: String,
    pub wave_execution_mode: String,
    pub phase2_reset_state: Option<WorldStateV5>,
    pub phase2_environment_state: Option<WorldStateV5>,
    pub diagnostic_service: Option<Arc<dyn DiagnosticServiceV5>>,
}

impl Default for RuntimeOptionsV5 {
    fn default() -> Self {
        Self {
            topology: "T0_SINGLE_MONOLITH".to_owned(),
            steering_mode: "UNSTEERED".to_owned(),
            wave_execution_mode: "COUPLED".to_owned(),
            phase2_reset_state: None,
            phase2_environment_state: None,
            diagnostic_service: None,
        }
    }
}

/// Compute canonical `(stage_digest, dependency_digest)` using RFC 8785 JCS SHA-256.
pub fn stage_digests(
    phase_index: i64,
    stage_id: &str,
    world_rev: i64,
    mutations: &[AtomicMutationV5],
) -> Result<(String, String), RuntimeError> {
    let mut sorted_muts: Vec<&AtomicMutationV5> = mutations.iter().collect();
    sorted_muts.sort_by_key(|m| mutation_key(m));
    let mut sorted_values = Vec::with_capacity(sorted_muts.len());
    for m in sorted_muts {
        sorted_values
            .push(serde_json::to_value(m).map_err(|e| {
                RuntimeError::Schema(ib_v5_schemas::SchemaValidationError::from(e))
            })?);
    }

    let union_if_match: BTreeSet<&str> = mutations
        .iter()
        .flat_map(|m| m.if_match().iter().map(String::as_str))
        .collect();
    let union_vec: Vec<Value> = union_if_match
        .into_iter()
        .map(|s| Value::String(s.to_owned()))
        .collect();

    let mut stage_obj = Map::new();
    stage_obj.insert("phase_index".to_owned(), Value::Number(phase_index.into()));
    stage_obj.insert("stage_id".to_owned(), Value::String(stage_id.to_owned()));
    stage_obj.insert("mutations".to_owned(), Value::Array(sorted_values));
    let stage_digest = jcs_sha256_value(&Value::Object(stage_obj))?;

    let mut dep_obj = Map::new();
    dep_obj.insert("phase_index".to_owned(), Value::Number(phase_index.into()));
    dep_obj.insert("world_rev".to_owned(), Value::Number(world_rev.into()));
    dep_obj.insert("if_match".to_owned(), Value::Array(union_vec));
    let dependency_digest = jcs_sha256_value(&Value::Object(dep_obj))?;

    Ok((stage_digest, dependency_digest))
}

/// Compute exact required co-sign tuples `COSIGN_REQ(M_k)` for a mutation batch.
#[must_use]
pub fn cosign_requirements(mutations: &[AtomicMutationV5]) -> BTreeSet<CoSignTuple> {
    let mut required = BTreeSet::new();
    for mutation in mutations {
        match mutation {
            AtomicMutationV5::UarCampaign(m) => {
                if let Some(custodian) = &m.emergency_custodian_account_id {
                    required.insert((
                        CoSignScopeKindV5::UarCustodian,
                        m.target_entity_id.clone(),
                        custodian.clone(),
                    ));
                }
            }
            AtomicMutationV5::JitDecision(m) => {
                if m.jit_decision == ib_v5_schemas::JITDecisionKindV5::ApproveJitCosign
                    && let Some(approver) = &m.approver_account_id
                {
                    required.insert((
                        CoSignScopeKindV5::CriticalJit,
                        m.target_entity_id.clone(),
                        approver.clone(),
                    ));
                }
            }
            AtomicMutationV5::AgentDelegation(m) => {
                if m.delegation_decision
                    == ib_v5_schemas::AgentDelegationDecisionKindV5::ApproveDelegationCosign
                    && let Some(cosigner) = &m.hitl_cosigner_account_id
                {
                    required.insert((
                        CoSignScopeKindV5::AgentDelegationCosign,
                        m.target_entity_id.clone(),
                        cosigner.clone(),
                    ));
                }
            }
            _ => {}
        }
    }
    required
}

fn dedup_preserve_order(values: &[String]) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut out = Vec::with_capacity(values.len());
    for v in values {
        if seen.insert(v.as_str()) {
            out.push(v.clone());
        }
    }
    out
}

/// Stateful delivered-view tool surface for a single v5 episode.
pub struct RuntimeV5 {
    episode: EpisodeV5,
    world: WorldStateV5,
    pub topology: String,
    pub steering_mode: String,
    pub wave_execution_mode: String,
    phase2_reset_state: Option<WorldStateV5>,
    phase2_environment_state: Option<WorldStateV5>,
    diagnostic_service: Option<Arc<dyn DiagnosticServiceV5>>,
    phase_index: i64,
    manifest: PhaseManifestV5,
    phase_slice_payloads: IndexMap<String, DeliveredSliceV5>,
    phase_delivered: IndexMap<String, DeliveredSliceV5>,
    phase1_delivered: IndexMap<String, DeliveredSliceV5>,
    read_ledger: BTreeSet<String>,
    remaining_query_budget: i64,
    view: ViewV5,
    phase1_view: Option<ViewV5>,
    staged: Option<StagedBuffer>,
    committed_phases: BTreeSet<i64>,
    episode_completed: bool,
    minted_cosigns: BTreeSet<CoSignMintKey>,
    consumed_cosigns: BTreeSet<CoSignMintKey>,
    minted_steers: BTreeSet<SteerMintKey>,
    checker_calls: BTreeMap<i64, i64>,
    steer_calls: BTreeMap<i64, i64>,
    phase_snapshots: BTreeMap<i64, PhaseSnapshotSetV5>,
    snapshots: Vec<RuntimeSnapshotV5>,
    token_totals: TokenTotalsV5,
    committed_p1_digest: String,
    terminal_world: Option<TerminalWorldStateV5>,
}

impl RuntimeV5 {
    /// Construct a default `T0_SINGLE_MONOLITH` / `UNSTEERED` / `COUPLED` runtime.
    pub fn new(episode: EpisodeV5, world: WorldStateV5) -> Result<Self, RuntimeError> {
        Self::with_options(episode, world, RuntimeOptionsV5::default())
    }

    /// Construct a configured runtime for an episode and initial Phase-1 world.
    pub fn with_options(
        episode: EpisodeV5,
        world: WorldStateV5,
        options: RuntimeOptionsV5,
    ) -> Result<Self, RuntimeError> {
        if world.phase_index != 1 {
            return Err(RuntimeError::InvalidArgument(
                "RuntimeV5 must open from a phase-1 world".to_owned(),
            ));
        }
        if !TOPOLOGY_IDS.contains(&options.topology.as_str()) {
            return Err(RuntimeError::InvalidArgument(format!(
                "unsupported v5 topology: {:?}",
                options.topology
            )));
        }
        if !STEERING_MODES.contains(&options.steering_mode.as_str()) {
            return Err(RuntimeError::InvalidArgument(format!(
                "unsupported v5 steering mode: {:?}",
                options.steering_mode
            )));
        }
        if !matches!(
            options.wave_execution_mode.as_str(),
            "COUPLED" | "ORACLE_PHASE_RESET"
        ) {
            return Err(RuntimeError::InvalidArgument(
                "wave_execution_mode must be COUPLED or ORACLE_PHASE_RESET".to_owned(),
            ));
        }
        if options.wave_execution_mode == "ORACLE_PHASE_RESET"
            && options.phase2_reset_state.is_none()
        {
            return Err(RuntimeError::InvalidArgument(
                "ORACLE_PHASE_RESET requires an explicit phase2_reset_state".to_owned(),
            ));
        }

        let manifest = episode.phase1_manifest.clone();
        let phase_slice_payloads = episode.phase1_slices.clone();
        let read_ledger: BTreeSet<String> = episode
            .policy_card
            .overlay
            .temporal_sod_rules
            .iter()
            .map(|r| r.evidence_id.clone())
            .collect();
        let remaining_query_budget = manifest.query_budget;
        let view = ViewV5::from_slices(
            &manifest,
            &[],
            &episode.policy_card,
            world.world_rev,
            None,
            &[],
        )?;

        let mut phase_snapshots = BTreeMap::new();
        phase_snapshots.insert(1, PhaseSnapshotSetV5::default());
        phase_snapshots.insert(2, PhaseSnapshotSetV5::default());

        Ok(Self {
            episode,
            world,
            topology: options.topology,
            steering_mode: options.steering_mode,
            wave_execution_mode: options.wave_execution_mode,
            phase2_reset_state: options.phase2_reset_state,
            phase2_environment_state: options.phase2_environment_state,
            diagnostic_service: options.diagnostic_service,
            phase_index: 1,
            manifest,
            phase_slice_payloads,
            phase_delivered: IndexMap::new(),
            phase1_delivered: IndexMap::new(),
            read_ledger,
            remaining_query_budget,
            view,
            phase1_view: None,
            staged: None,
            committed_phases: BTreeSet::new(),
            episode_completed: false,
            minted_cosigns: BTreeSet::new(),
            consumed_cosigns: BTreeSet::new(),
            minted_steers: BTreeSet::new(),
            checker_calls: BTreeMap::new(),
            steer_calls: BTreeMap::new(),
            phase_snapshots,
            snapshots: Vec::new(),
            token_totals: TokenTotalsV5::default(),
            committed_p1_digest: String::new(),
            terminal_world: None,
        })
    }

    #[must_use]
    pub fn phase_index(&self) -> i64 {
        self.phase_index
    }

    #[must_use]
    pub fn episode(&self) -> &EpisodeV5 {
        &self.episode
    }

    #[must_use]
    pub fn world(&self) -> &WorldStateV5 {
        &self.world
    }

    #[must_use]
    pub fn manifest(&self) -> &PhaseManifestV5 {
        &self.manifest
    }

    #[must_use]
    pub fn view(&self) -> &ViewV5 {
        &self.view
    }

    #[must_use]
    pub fn read_ledger(&self) -> &BTreeSet<String> {
        &self.read_ledger
    }

    #[must_use]
    pub fn remaining_query_budget(&self) -> i64 {
        self.remaining_query_budget
    }

    #[must_use]
    pub fn active_stage_id(&self) -> Option<&str> {
        self.staged.as_ref().map(|s| s.stage_id.as_str())
    }

    #[must_use]
    pub fn active_stage_digest(&self) -> &str {
        self.staged.as_ref().map_or("", |s| s.stage_digest.as_str())
    }

    #[must_use]
    pub fn terminal_world_state(&self) -> Option<&TerminalWorldStateV5> {
        self.terminal_world.as_ref()
    }

    #[must_use]
    pub fn episode_completed(&self) -> bool {
        self.episode_completed
    }

    #[must_use]
    pub fn phase2_opened(&self) -> bool {
        self.phase_index == 2
    }

    #[must_use]
    pub fn phase_snapshots(&self, phase_index: i64) -> Option<&PhaseSnapshotSetV5> {
        self.phase_snapshots.get(&phase_index)
    }

    #[must_use]
    pub fn snapshots(&self) -> &[RuntimeSnapshotV5] {
        &self.snapshots
    }

    #[must_use]
    pub fn minted_cosigns(&self) -> &BTreeSet<CoSignMintKey> {
        &self.minted_cosigns
    }

    #[must_use]
    pub fn consumed_cosigns(&self) -> &BTreeSet<CoSignMintKey> {
        &self.consumed_cosigns
    }

    /// Test helper to inject a minted co-sign entry (e.g., to test Gate 3 extra-mint burn).
    pub fn inject_minted_cosign_for_test(&mut self, key: CoSignMintKey) {
        self.minted_cosigns.insert(key);
    }

    /// Set cumulative provider token totals before the next tool boundary.
    pub fn set_usage_totals(
        &mut self,
        input_tokens_uncached: i64,
        cached_input_tokens: i64,
        output_tokens: i64,
        reasoning_tokens: i64,
    ) -> Result<(), RuntimeError> {
        if input_tokens_uncached < 0
            || cached_input_tokens < 0
            || output_tokens < 0
            || reasoning_tokens < 0
        {
            return Err(RuntimeError::InvalidArgument(
                "token totals must be non-negative integers".to_owned(),
            ));
        }
        self.token_totals = TokenTotalsV5 {
            input_tokens_uncached,
            cached_input_tokens,
            output_tokens,
            reasoning_tokens,
        };
        Ok(())
    }

    pub fn query_sources(
        &mut self,
        phase_index: i64,
        slice_ids: &[String],
    ) -> Result<QuerySourcesResponseV5, RuntimeError> {
        if phase_index != self.phase_index || self.episode_completed {
            return Ok(QuerySourcesResponseV5 {
                status: QuerySourcesStatusV5::PhaseNotOpen,
                phase_index,
                remaining_query_budget: self.remaining_query_budget,
                already_delivered_ids: Vec::new(),
                slices: Vec::new(),
            });
        }

        let requested = dedup_preserve_order(slice_ids);
        let directory: BTreeMap<&str, _> = self
            .manifest
            .source_directory
            .iter()
            .map(|d| (d.slice_id.as_str(), d))
            .collect();
        if requested.is_empty()
            || requested
                .iter()
                .any(|id| !directory.contains_key(id.as_str()))
        {
            return Ok(QuerySourcesResponseV5 {
                status: QuerySourcesStatusV5::RejectedUnknownSlice,
                phase_index,
                remaining_query_budget: self.remaining_query_budget,
                already_delivered_ids: Vec::new(),
                slices: Vec::new(),
            });
        }

        let already: Vec<String> = requested
            .iter()
            .filter(|id| self.phase_delivered.contains_key(id.as_str()))
            .cloned()
            .collect();
        let new_ids: Vec<String> = requested
            .iter()
            .filter(|id| !self.phase_delivered.contains_key(id.as_str()))
            .cloned()
            .collect();

        let cost: i64 = new_ids
            .iter()
            .map(|id| directory[id.as_str()].credit_cost)
            .sum();
        if cost > self.remaining_query_budget {
            return Ok(QuerySourcesResponseV5 {
                status: QuerySourcesStatusV5::RejectedBudgetExceeded,
                phase_index,
                remaining_query_budget: self.remaining_query_budget,
                already_delivered_ids: Vec::new(),
                slices: Vec::new(),
            });
        }

        let new_slices: Vec<DeliveredSliceV5> = new_ids
            .iter()
            .map(|id| {
                self.phase_slice_payloads.get(id).cloned().ok_or_else(|| {
                    RuntimeError::InvalidArgument(format!("missing slice payload for {id:?}"))
                })
            })
            .collect::<Result<Vec<_>, _>>()?;

        self.remaining_query_budget -= cost;
        for slice in &new_slices {
            self.phase_delivered
                .insert(slice.slice_id.clone(), slice.clone());
            if phase_index == 1 {
                self.phase1_delivered
                    .insert(slice.slice_id.clone(), slice.clone());
            }
            self.read_ledger.extend(all_evidence_ids(slice));
        }

        let delivered_vec: Vec<DeliveredSliceV5> = self.phase_delivered.values().cloned().collect();
        let empty_invalidated: Vec<String> = Vec::new();
        let invalidated = if phase_index == 2 {
            &self.manifest.invalidated_entity_ids
        } else {
            &empty_invalidated
        };
        let base_view = if phase_index == 2 {
            self.phase1_view.as_ref()
        } else {
            None
        };
        self.view = ViewV5::from_slices(
            &self.manifest,
            &delivered_vec,
            &self.episode.policy_card,
            self.world.world_rev,
            base_view,
            invalidated,
        )?;

        Ok(QuerySourcesResponseV5 {
            status: QuerySourcesStatusV5::Ok,
            phase_index,
            remaining_query_budget: self.remaining_query_budget,
            already_delivered_ids: already,
            slices: new_slices,
        })
    }

    pub fn stage_phase_mutations(
        &mut self,
        phase_index: i64,
        stage_id: &str,
        mutations: &[AtomicMutationV5],
    ) -> Result<StageReceiptV5, RuntimeError> {
        if phase_index != self.phase_index
            || !STAGE_ID_RE.is_match(stage_id)
            || !completeness_ok(&self.manifest.work_queue, phase_index, mutations)
        {
            return Ok(self.stage_rejected(
                phase_index,
                stage_id,
                mutations,
                "phase_handle_or_completeness",
            ));
        }
        let errors = structural_errors(
            &self.view,
            phase_index,
            mutations,
            &self.manifest.work_queue,
        );
        if let Some(first_err) = errors.first() {
            return Ok(self.stage_rejected(phase_index, stage_id, mutations, first_err));
        }

        let (stage_digest, dependency_digest) =
            stage_digests(phase_index, stage_id, self.world.world_rev, mutations)?;
        self.staged = Some(StagedBuffer {
            phase_index,
            stage_id: stage_id.to_owned(),
            mutations: mutations.to_vec(),
            stage_digest: stage_digest.clone(),
            dependency_digest: dependency_digest.clone(),
        });

        let receipt = StageReceiptV5 {
            status: StageReceiptStatusV5::Staged,
            phase_index,
            stage_id: stage_id.to_owned(),
            mutation_count: i64::try_from(mutations.len()).unwrap_or(i64::MAX),
            stage_digest: stage_digest.clone(),
            dependency_digest: dependency_digest.clone(),
            error_detail: None,
        };
        let snapshot = self.build_snapshot(
            "STAGE_ATTEMPT",
            phase_index,
            stage_id,
            mutations,
            &stage_digest,
            &dependency_digest,
            "STAGED",
            self.world.world_rev,
            self.world.world_rev,
        );
        self.snapshots.push(snapshot.clone());
        if let Some(phase) = self.phase_snapshots.get_mut(&phase_index) {
            phase.stage_attempts.push(snapshot.clone());
            if phase.first_stage.is_none() {
                phase.first_stage = Some(snapshot.clone());
            }
            phase.last_stage = Some(snapshot);
        }
        Ok(receipt)
    }

    fn stage_rejected(
        &mut self,
        phase_index: i64,
        stage_id: &str,
        mutations: &[AtomicMutationV5],
        reason: &str,
    ) -> StageReceiptV5 {
        let receipt = StageReceiptV5 {
            status: StageReceiptStatusV5::RejectedStructural,
            phase_index,
            stage_id: stage_id.to_owned(),
            mutation_count: 0,
            stage_digest: String::new(),
            dependency_digest: String::new(),
            error_detail: Some(reason.to_owned()),
        };
        let snapshot = self.build_snapshot(
            "STAGE_ATTEMPT",
            phase_index,
            stage_id,
            mutations,
            "",
            "",
            "REJECTED_STRUCTURAL",
            self.world.world_rev,
            self.world.world_rev,
        );
        self.snapshots.push(snapshot.clone());
        if let Some(phase) = self.phase_snapshots.get_mut(&phase_index) {
            phase.stage_attempts.push(snapshot);
        }
        receipt
    }

    pub fn request_cosign(
        &mut self,
        phase_index: i64,
        stage_id: &str,
        scope_kind: CoSignScopeKindV5,
        target_entity_id: &str,
        actor_account_id: &str,
    ) -> CoSignReceiptV5 {
        let request: CoSignTuple = (
            scope_kind,
            target_entity_id.to_owned(),
            actor_account_id.to_owned(),
        );
        let mint_key: Option<CoSignMintKey> = self.staged.as_ref().map(|staged| {
            (
                self.episode.episode_id.clone(),
                phase_index,
                stage_id.to_owned(),
                staged.stage_digest.clone(),
                request.clone(),
            )
        });
        let valid = phase_index == self.phase_index
            && STAGE_ID_RE.is_match(stage_id)
            && self.staged.as_ref().is_some_and(|staged| {
                staged.phase_index == phase_index && staged.stage_id == stage_id
            })
            && self.cosign_allowed(scope_kind, target_entity_id, actor_account_id)
            && mint_key
                .as_ref()
                .is_some_and(|k| !self.consumed_cosigns.contains(k));

        let status = if valid {
            if let Some(k) = mint_key {
                self.minted_cosigns.insert(k);
            }
            CoSignReceiptStatusV5::CoSigned
        } else {
            CoSignReceiptStatusV5::RejectedStructural
        };

        CoSignReceiptV5 {
            status,
            phase_index,
            stage_id: stage_id.to_owned(),
            scope_kind,
            target_entity_id: target_entity_id.to_owned(),
            actor_account_id: actor_account_id.to_owned(),
        }
    }

    fn cosign_allowed(
        &self,
        scope_kind: CoSignScopeKindV5,
        target_entity_id: &str,
        actor_account_id: &str,
    ) -> bool {
        let Some(staged) = &self.staged else {
            return false;
        };
        let required = cosign_requirements(&staged.mutations);
        let request = (
            scope_kind,
            target_entity_id.to_owned(),
            actor_account_id.to_owned(),
        );
        if !required.contains(&request) {
            return false;
        }
        if scope_kind == CoSignScopeKindV5::UarCustodian {
            let Some(campaign) = self.view.resolve_uar_campaign(target_entity_id) else {
                return false;
            };
            return campaign
                .candidate_custodian_account_ids
                .iter()
                .any(|id| id == actor_account_id)
                && self.view.resolve_idp_account(actor_account_id).is_some();
        }
        self.view.resolve_idp_account(actor_account_id).is_some()
    }

    pub fn commit_phase_mutations(
        &mut self,
        phase_index: i64,
        stage_id: &str,
    ) -> Result<CommitReceiptV5, RuntimeError> {
        if self.episode_completed || self.committed_phases.contains(&phase_index) {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::PhaseAlreadyCommitted,
                phase_index,
                stage_id,
            ));
        }
        if phase_index != self.phase_index {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::PhaseNotOpen,
                phase_index,
                stage_id,
            ));
        }
        let Some(staged) = self.staged.clone() else {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::CommitBlockedStructural,
                phase_index,
                stage_id,
            ));
        };
        if staged.phase_index != phase_index
            || staged.stage_id != stage_id
            || !STAGE_ID_RE.is_match(stage_id)
        {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::CommitBlockedStructural,
                phase_index,
                stage_id,
            ));
        }

        // Gate 2: Stages 1-2
        if !completeness_ok(&self.manifest.work_queue, phase_index, &staged.mutations)
            || !structural_ok(
                &self.view,
                phase_index,
                &staged.mutations,
                &self.manifest.work_queue,
            )
        {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::CommitBlockedStructural,
                phase_index,
                stage_id,
            ));
        }

        // Gate 3: Directive 01 permanent co-sign burn + token check
        let mut minted: BTreeSet<CoSignTuple> = BTreeSet::new();
        let mut matching_mints: Vec<CoSignMintKey> = Vec::new();
        for entry in &self.minted_cosigns {
            if entry.0 == self.episode.episode_id
                && entry.1 == phase_index
                && entry.2 == stage_id
                && entry.3 == staged.stage_digest
            {
                minted.insert(entry.4.clone());
                matching_mints.push(entry.clone());
            }
        }
        for entry in matching_mints {
            self.minted_cosigns.remove(&entry);
            self.consumed_cosigns.insert(entry);
        }
        let steer_key: SteerMintKey = (
            self.episode.episode_id.clone(),
            phase_index,
            stage_id.to_owned(),
            staged.stage_digest.clone(),
        );
        let has_steer = self.minted_steers.remove(&steer_key);
        let required_cosigns = cosign_requirements(&staged.mutations);
        if minted != required_cosigns || (self.manifest.approval_gated && !has_steer) {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::CommitBlockedToken,
                phase_index,
                stage_id,
            ));
        }

        // Gate 4: OCC precondition check
        let occ_failed = staged.mutations.iter().any(|m| {
            m.if_match().iter().any(|token| {
                let Some((eid, _)) = token.split_once('@') else {
                    return true;
                };
                super::view::live_version(eid, &self.view).as_deref() != Some(token.as_str())
            })
        });
        if occ_failed {
            return Ok(self.commit_result(
                CommitReceiptStatusV5::CommitBlockedPrecondition,
                phase_index,
                stage_id,
            ));
        }

        let before = self.world.world_rev;
        let committed_snapshot = self.build_snapshot(
            "COMMIT_ATTEMPT",
            phase_index,
            stage_id,
            &staged.mutations,
            &staged.stage_digest,
            &staged.dependency_digest,
            "COMMITTED",
            before,
            before + 1,
        );
        self.snapshots.push(committed_snapshot.clone());
        self.committed_phases.insert(phase_index);

        let result = if phase_index == 1 {
            self.committed_p1_digest = staged.stage_digest.clone();
            let visible_ledger = build_visible_prior_ledger(
                &self.view,
                &staged.mutations,
                &self.manifest.work_queue,
            )?;
            let changed_people: BTreeSet<&str> = self
                .episode
                .phase2_delta_person_ids
                .iter()
                .map(String::as_str)
                .collect();
            let previously_delivered: BTreeSet<String> =
                self.view.entity_ids("hr_persons").into_iter().collect();
            let invalidated: Vec<String> = previously_delivered
                .into_iter()
                .filter(|id| changed_people.contains(id.as_str()))
                .collect();

            let mut phase2_manifest = self.episode.phase2_base_manifest.clone();
            phase2_manifest.invalidated_entity_ids = invalidated.clone();
            phase2_manifest.prior_phase_ledger = Some(visible_ledger);

            let p1_world =
                apply_phase1_with_queue(&self.world, &staged.mutations, &self.manifest.work_queue)?;
            self.world = apply_phase2_environment_delta(
                &p1_world,
                &self.episode,
                self.phase2_environment_state.as_ref(),
            )?;
            if self.wave_execution_mode == "ORACLE_PHASE_RESET" {
                self.world = self
                    .phase2_reset_state
                    .clone()
                    .expect("ORACLE_PHASE_RESET validated non-null reset state");
            }

            self.phase_index = 2;
            self.manifest = phase2_manifest.clone();
            self.phase_slice_payloads = self.episode.phase2_slices.clone();
            self.phase_delivered.clear();
            self.remaining_query_budget = self.manifest.query_budget;

            for pid in &invalidated {
                if let Some(person) = self.view.resolve_hr_person(pid) {
                    self.read_ledger.remove(&person.evidence_id);
                }
            }
            let p1_view = self.view.clone();
            self.phase1_view = Some(p1_view);
            self.view = ViewV5::from_slices(
                &self.manifest,
                &[],
                &self.episode.policy_card,
                self.world.world_rev,
                self.phase1_view.as_ref(),
                &invalidated,
            )?;

            CommitReceiptV5 {
                status: CommitReceiptStatusV5::CommittedAndAdvanced,
                phase_index,
                stage_id: stage_id.to_owned(),
                world_rev: self.world.world_rev,
                phase2_manifest: Some(phase2_manifest),
            }
        } else {
            let terminal = apply_phase2(
                &self.world,
                &staged.mutations,
                &staged.stage_digest,
                &self.committed_p1_digest,
            )?;
            self.world.world_rev = terminal.world_rev;
            self.terminal_world = Some(terminal);
            self.episode_completed = true;
            CommitReceiptV5 {
                status: CommitReceiptStatusV5::EpisodeCompleted,
                phase_index,
                stage_id: stage_id.to_owned(),
                world_rev: self.world.world_rev,
                phase2_manifest: None,
            }
        };

        if let Some(phase) = self.phase_snapshots.get_mut(&phase_index) {
            phase.commit = Some(committed_snapshot);
        }
        self.staged = None;
        Ok(result)
    }

    fn commit_result(
        &self,
        status: CommitReceiptStatusV5,
        phase_index: i64,
        stage_id: &str,
    ) -> CommitReceiptV5 {
        CommitReceiptV5 {
            status,
            phase_index,
            stage_id: stage_id.to_owned(),
            world_rev: self.world.world_rev,
            phase2_manifest: None,
        }
    }

    pub fn run_phase_checker(
        &mut self,
        phase_index: i64,
        stage_id: &str,
    ) -> Result<CheckerReceiptV5, RuntimeError> {
        if !matches!(
            self.topology.as_str(),
            "T3_MAKER_CHECKER_GUARDRAIL" | "T5_HIERARCHICAL_PIPELINE"
        ) {
            return Ok(CheckerReceiptV5 {
                status: CheckerReceiptStatusV5::ToolNotPermittedInTopology,
                phase_index,
                stage_id: stage_id.to_owned(),
                stage_digest: String::new(),
                violation_categories: Vec::new(),
            });
        }
        let Some(staged) = self.staged.clone() else {
            return Ok(CheckerReceiptV5 {
                status: CheckerReceiptStatusV5::RejectedStructural,
                phase_index,
                stage_id: stage_id.to_owned(),
                stage_digest: String::new(),
                violation_categories: Vec::new(),
            });
        };
        if phase_index != self.phase_index
            || staged.stage_id != stage_id
            || staged.phase_index != phase_index
            || !STAGE_ID_RE.is_match(stage_id)
        {
            return Ok(CheckerReceiptV5 {
                status: CheckerReceiptStatusV5::RejectedStructural,
                phase_index,
                stage_id: stage_id.to_owned(),
                stage_digest: String::new(),
                violation_categories: Vec::new(),
            });
        }

        let calls = self.checker_calls.get(&phase_index).copied().unwrap_or(0);
        self.checker_calls.insert(phase_index, calls + 1);
        if calls >= 1 {
            return Ok(CheckerReceiptV5 {
                status: CheckerReceiptStatusV5::DiagnosticLimitReached,
                phase_index,
                stage_id: stage_id.to_owned(),
                stage_digest: String::new(),
                violation_categories: Vec::new(),
            });
        }

        let stage1_ok = completeness_ok(&self.manifest.work_queue, phase_index, &staged.mutations);
        let stage2_ok = structural_ok(
            &self.view,
            phase_index,
            &staged.mutations,
            &self.manifest.work_queue,
        );
        let (first_stage, categories) = self.diagnose(&staged, stage1_ok, stage2_ok)?;
        let passed = first_stage.is_none() && categories.is_empty();
        let mut sorted_cats: BTreeMap<&'static str, ViolationCategoryV5> = BTreeMap::new();
        for cat in categories {
            sorted_cats.insert(crate::verifier::violation_category_as_str(cat), cat);
        }
        Ok(CheckerReceiptV5 {
            status: if passed {
                CheckerReceiptStatusV5::Pass
            } else {
                CheckerReceiptStatusV5::RejectViolation
            },
            phase_index,
            stage_id: stage_id.to_owned(),
            stage_digest: if passed {
                staged.stage_digest
            } else {
                String::new()
            },
            violation_categories: sorted_cats.into_values().collect(),
        })
    }

    pub fn request_team_steer(
        &mut self,
        phase_index: i64,
        stage_id: &str,
        _trigger_reason: SteerTriggerReasonV5,
    ) -> Result<SteerReceiptV5, RuntimeError> {
        if self.steering_mode == "UNSTEERED" {
            return Ok(SteerReceiptV5 {
                status: SteerReceiptStatusV5::SteeringDisabled,
                phase_index,
                stage_id: stage_id.to_owned(),
                process_directive: None,
            });
        }
        let Some(staged) = self.staged.clone() else {
            return Ok(SteerReceiptV5 {
                status: SteerReceiptStatusV5::RejectedStructural,
                phase_index,
                stage_id: stage_id.to_owned(),
                process_directive: None,
            });
        };
        if phase_index != self.phase_index
            || staged.stage_id != stage_id
            || staged.phase_index != phase_index
            || !STAGE_ID_RE.is_match(stage_id)
        {
            return Ok(SteerReceiptV5 {
                status: SteerReceiptStatusV5::RejectedStructural,
                phase_index,
                stage_id: stage_id.to_owned(),
                process_directive: None,
            });
        }

        let steer_key: SteerMintKey = (
            self.episode.episode_id.clone(),
            phase_index,
            stage_id.to_owned(),
            staged.stage_digest.clone(),
        );
        self.minted_steers.insert(steer_key);

        let calls = self.steer_calls.get(&phase_index).copied().unwrap_or(0);
        self.steer_calls.insert(phase_index, calls + 1);
        if calls >= 1 {
            return Ok(SteerReceiptV5 {
                status: SteerReceiptStatusV5::DiagnosticLimitReached,
                phase_index,
                stage_id: stage_id.to_owned(),
                process_directive: None,
            });
        }

        let stage1_ok = completeness_ok(&self.manifest.work_queue, phase_index, &staged.mutations);
        let stage2_ok = structural_ok(
            &self.view,
            phase_index,
            &staged.mutations,
            &self.manifest.work_queue,
        );
        let (first_stage, _categories) = self.diagnose(&staged, stage1_ok, stage2_ok)?;
        let Some(failing_stage) = first_stage else {
            return Ok(SteerReceiptV5 {
                status: SteerReceiptStatusV5::ApprovedWithToken,
                phase_index,
                stage_id: stage_id.to_owned(),
                process_directive: Some(SteerProcessDirectiveV5::ProceedToCommit),
            });
        };

        let directive = match failing_stage {
            1..=3 => SteerProcessDirectiveV5::RecheckPreconditions,
            4 => {
                let cited_unread = staged
                    .mutations
                    .iter()
                    .flat_map(|m| m.cite().iter())
                    .filter_map(|c| c.split_once(':').map(|(_, ev)| ev))
                    .any(|ev| !self.read_ledger.contains(ev));
                let service = self.diagnostic_service.as_ref().ok_or_else(|| {
                    RuntimeError::InvalidArgument(
                        "a SemanticDiagnosticServiceV5 must be injected for checker/steer use"
                            .to_owned(),
                    )
                })?;
                let gold_unread =
                    service.has_unread_gold_evidence(phase_index, &self.view, &self.read_ledger)?;
                if cited_unread || gold_unread {
                    SteerProcessDirectiveV5::QueryMissingSlices
                } else {
                    SteerProcessDirectiveV5::RereadEvidence
                }
            }
            5 => {
                if phase_index == 2 {
                    SteerProcessDirectiveV5::RecheckPriorPhaseLedger
                } else {
                    SteerProcessDirectiveV5::RereadEvidence
                }
            }
            _ => SteerProcessDirectiveV5::RereadEvidence,
        };

        Ok(SteerReceiptV5 {
            status: SteerReceiptStatusV5::CorrectiveRedirect,
            phase_index,
            stage_id: stage_id.to_owned(),
            process_directive: Some(directive),
        })
    }

    fn diagnose(
        &self,
        staged: &StagedBuffer,
        stage1_ok: bool,
        stage2_ok: bool,
    ) -> Result<(Option<usize>, Vec<ViolationCategoryV5>), RuntimeError> {
        let service = self.diagnostic_service.as_ref().ok_or_else(|| {
            RuntimeError::InvalidArgument(
                "a SemanticDiagnosticServiceV5 must be injected for checker/steer use".to_owned(),
            )
        })?;
        let preconditions: Vec<String> = staged
            .mutations
            .iter()
            .flat_map(|m| m.if_match().iter().cloned())
            .collect();
        let citations: Vec<String> = staged
            .mutations
            .iter()
            .flat_map(|m| m.cite().iter().cloned())
            .collect();
        Ok(service.diagnose(
            staged.phase_index,
            &staged.stage_id,
            &self.view,
            &staged.mutations,
            &preconditions,
            &citations,
            &self.read_ledger,
            stage1_ok,
            stage2_ok,
        )?)
    }

    #[allow(clippy::too_many_arguments)]
    fn build_snapshot(
        &self,
        event: &str,
        phase_index: i64,
        stage_id: &str,
        mutations: &[AtomicMutationV5],
        stage_digest: &str,
        dependency_digest: &str,
        receipt_status: &str,
        world_rev_before: i64,
        world_rev_after: i64,
    ) -> RuntimeSnapshotV5 {
        RuntimeSnapshotV5 {
            event: event.to_owned(),
            phase_index,
            stage_id: stage_id.to_owned(),
            mutations: mutations.to_vec(),
            preconditions: mutations
                .iter()
                .flat_map(|m| m.if_match().iter().cloned())
                .collect(),
            citations: mutations
                .iter()
                .flat_map(|m| m.cite().iter().cloned())
                .collect(),
            stage_digest: stage_digest.to_owned(),
            dependency_digest: dependency_digest.to_owned(),
            view: self.view.clone(),
            delivered_slices: self.phase_delivered.values().cloned().collect(),
            delivered_slice_ids: self.phase_delivered.keys().cloned().collect(),
            read_ledger: self.read_ledger.clone(),
            remaining_query_budget: self.remaining_query_budget,
            world_rev: world_rev_after,
            world_rev_before,
            world_rev_after,
            token_totals: self.token_totals,
            receipt_status: receipt_status.to_owned(),
        }
    }
}
