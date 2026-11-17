//! Shared result, trace, feasibility, and internal context types for `ib-v5-oracle`.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::Arc;

use ib_v5_schemas::{
    AtomicMutationV5, EngagementStatusV5, NHIGovernanceMutationV5, NHISubkindV5,
    PhaseWorkQueueV5, WorldStateV5,
};
use ib_v5_schemas::scope_algebra::CapabilityFootprint;

/// `(mutation_kind, target_entity_id)` target key.
pub type TargetKey = (String, String);

/// Detailed diagnostics returned inside [`OptimizationFeasibilityV5`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OptimizationDetailsV5 {
    pub unknown_candidate_ids: Vec<String>,
    pub touched_person_ids: Vec<String>,
    pub post_live_control_ids: Vec<String>,
    pub post_footprints: BTreeMap<String, CapabilityFootprint>,
    pub post_account_footprints: BTreeMap<String, CapabilityFootprint>,
    pub conversion_ids_by_person: BTreeMap<String, Vec<String>>,
    pub action_valid: bool,
    pub candidate_people: Vec<String>,
    pub coverage_missing: Vec<String>,
}

/// Feasibility assessment returned by P1/P2 optimization feasibility closures.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptimizationFeasibilityV5 {
    pub feasible: bool,
    pub coverage_ok: bool,
    pub policy_ok: bool,
    pub details: OptimizationDetailsV5,
}

/// Feasibility verdict for Phase-2 quorum, compensating-control, and reviewer gates.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Phase2FeasibilityV5 {
    pub feasible: bool,
    pub reason: Option<String>,
}

/// Closure checking whether a candidate subset is feasible for an optimization task.
pub type OptimizationFeasibilityFn =
    Arc<dyn Fn(&[String]) -> OptimizationFeasibilityV5 + Send + Sync>;

/// Closure checking whether an NHI mutation relied on stale or non-live custody signals.
pub type NhiStalePredicateFn = Arc<dyn Fn(&NHIGovernanceMutationV5) -> bool + Send + Sync>;

/// Per-signal telemetry evaluation row recorded in NHI trace context.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NhiSignalRowTraceV5 {
    pub signal_id: String,
    pub source: String,
    pub raw_person: Option<String>,
    pub fresh: bool,
    pub custody_live: bool,
    pub person: Option<String>,
    pub evidence_id: String,
}

/// Typed Stage-5 verification & diagnostic context attached to each [`GoldTargetTraceV5`].
#[derive(Clone, Default)]
pub struct TraceContextV5 {
    // Account recon
    pub pruned_conversion_grant_ids: Vec<String>,
    pub cc_sensitive_grant_ids: Vec<String>,
    pub gold_engagement_status: Option<EngagementStatusV5>,
    pub binding_person_ids: Vec<String>,
    pub mover_pruned_grant_ids: Vec<String>,
    pub x_mover_grant_ids: Vec<String>,

    // Connector sync
    pub inspected_page_ids: Vec<String>,
    pub consulted_fulfiller_account_ids: Vec<String>,
    pub matched_ticket_ids: Vec<String>,

    // HR mapping
    pub accepted_feed_ids: Vec<String>,
    pub quarantined_assertion_keys: Vec<String>,
    pub consulted_person_ids: Vec<String>,

    // NHI governance
    pub target_subkind: Option<NHISubkindV5>,
    pub valid_custodian_ids: Vec<String>,
    pub signal_rows: Vec<NhiSignalRowTraceV5>,
    pub eligible_owner_person_ids: Vec<String>,
    pub eligible_sources_by_rank: BTreeMap<i64, Vec<String>>,
    pub nhi_stale_support_predicate: Option<NhiStalePredicateFn>,

    // Agent registry
    pub eligible_sponsor: Option<bool>,
    pub sponsor_person_id: Option<String>,
    pub auto_binding_ids: Vec<String>,
    pub hitl_binding_ids: Vec<String>,

    // Cloud CIEM
    pub principal_live: BTreeMap<String, bool>,

    // JML workflow
    pub active_birthright_rule_ids: Vec<String>,
    pub converted_eligible_brule_ids: Vec<String>,
    pub quarantined_birthright_rule_ids: Vec<String>,
    pub simulated_event_ids: Vec<String>,
    pub has_disconnected_spokes: Option<bool>,
    pub conversion_carryover_sponsor_allowed: Option<bool>,

    // Optimization (P1 & P2)
    pub feasibility: Option<OptimizationFeasibilityFn>,
    pub selected_feasibility: Option<OptimizationFeasibilityV5>,
    pub touched_person_ids: Vec<String>,
    pub relevant_control_ids: Vec<String>,

    // UAR campaign
    pub surviving_item_ids: Vec<String>,
    pub valid_reviewers: BTreeMap<String, Vec<String>>,
    pub locked_item_ids: Vec<String>,

    // JIT & OBO delegation
    pub failing_predicate: Option<String>,
    pub predicate_results: BTreeMap<String, bool>,
    pub needs_hitl: Option<bool>,
}

impl fmt::Debug for TraceContextV5 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TraceContextV5")
            .field("failing_predicate", &self.failing_predicate)
            .field("gold_engagement_status", &self.gold_engagement_status)
            .field("target_subkind", &self.target_subkind)
            .field("has_feasibility", &self.feasibility.is_some())
            .field(
                "has_nhi_stale_support_predicate",
                &self.nhi_stale_support_predicate.is_some(),
            )
            .finish_non_exhaustive()
    }
}

/// Verifier-consumable trace entry for a single gold mutation target.
#[derive(Debug, Clone)]
pub struct GoldTargetTraceV5 {
    pub mutation: AtomicMutationV5,
    pub preconditions: Vec<String>,
    pub citations: Vec<String>,
    pub context: TraceContextV5,
}

/// Complete gold evaluation result for one phase.
#[derive(Debug, Clone)]
pub struct PhaseGoldResultV5 {
    pub phase_index: i64,
    pub work_queue: PhaseWorkQueueV5,
    pub mutations: Vec<AtomicMutationV5>,
    pub trace_by_target: BTreeMap<TargetKey, GoldTargetTraceV5>,
}

/// Internal per-account plan produced in P1.2–P1.4a and shared with P1.4b–P1.9.
#[derive(Debug, Clone)]
pub struct AccountPlanV5 {
    pub account_action: ib_v5_schemas::AccountReconActionV5,
    pub bound_person_id: Option<String>,
    pub bound_engagement_id: Option<String>,
    pub engagement: Option<ib_v5_schemas::EngagementV5>,
    pub binding_people: Vec<String>,
    pub conversion_pruned_grant_ids: BTreeSet<String>,
    pub after_conversion_grant_ids: BTreeSet<String>,
    pub mover_candidate_grant_ids: BTreeSet<String>,
    pub mover_pruned_grant_ids: BTreeSet<String>,
    pub x_mover_grant_ids: BTreeSet<String>,
    pub retained_grant_ids: BTreeSet<String>,
    pub sod_pruned_grant_ids: BTreeSet<String>,
    pub sod_pruned_by_group_ids: BTreeMap<String, Vec<String>>,
    pub sod_pruned_by_prior_grant_ids: BTreeMap<String, Vec<String>>,
    pub sod_witnesses_by_grant: BTreeMap<String, BTreeSet<ib_v5_schemas::scope_algebra::SoDWitness>>,
    pub k_before_by_grant: BTreeMap<String, CapabilityFootprint>,
    pub cc_sensitive_grant_ids: BTreeSet<String>,
    pub gold_engagement_status: Option<EngagementStatusV5>,
}

/// Internal connector evaluation outcome shared across P1 steps.
#[derive(Debug, Clone)]
pub struct ConnectorOutcomeV5 {
    pub connector: ib_v5_schemas::ConnectorSchemaV5,
    pub halted_fail_closed: bool,
    pub halt_reason: ib_v5_schemas::ConnectorHaltReasonV5,
    pub reconciled_account_ids: Vec<String>,
    pub remediated_spoke_drift: Vec<String>,
    pub itsm_ticket_actions: Vec<ib_v5_schemas::ITSMTicketActionV5>,
    pub inspected_pages: Vec<ib_v5_schemas::ConnectorPageV5>,
    pub candidate_ids: Vec<String>,
    pub drift_ids: Vec<String>,
    pub audit_sink_healthy: bool,
}

/// Internal NHI outcome shared with P1.6–P1.7.
#[derive(Debug, Clone)]
pub struct NhiOutcomeV5 {
    pub nhi_action: ib_v5_schemas::NHIGovernanceActionV5,
    pub resolved_owner_person_id: Option<String>,
    pub owner_resolution_source: Option<ib_v5_schemas::NHIOwnerResolutionSourceV5>,
}

/// Internal agent outcome shared with P1.6–P1.7.
#[derive(Debug, Clone)]
pub struct AgentOutcomeV5 {
    pub agent_action: ib_v5_schemas::AgentRegistryActionV5,
    pub resolved_sponsor_person_id: Option<String>,
    pub active_autonomous_binding_ids: Vec<String>,
    pub active_hitl_binding_ids: Vec<String>,
}

/// Internal cross-stage cache inside [`PhaseContext`].
#[derive(Debug, Clone, Default)]
pub struct PhaseSemanticCache {
    pub p1_connectors: BTreeMap<String, ConnectorOutcomeV5>,
    pub p1_account_outcomes: BTreeMap<String, AccountPlanV5>,
    pub p1_account_pbind: BTreeMap<String, Vec<String>>,
    pub p1_live_controls: Vec<String>,
    pub p1_k_kept_person: BTreeMap<String, CapabilityFootprint>,
    pub p1_k_standing_person: BTreeMap<String, CapabilityFootprint>,
    pub p1_nhi_outcomes: BTreeMap<String, NhiOutcomeV5>,
    pub p1_f_nhi: BTreeMap<String, CapabilityFootprint>,
    pub p1_agent_outcomes_partial: BTreeMap<String, AgentOutcomeV5>,
    pub p1_agent_outcomes: BTreeMap<String, AgentOutcomeV5>,
    pub p1_k_spons_auto: BTreeMap<String, CapabilityFootprint>,
}

/// Mutable evaluation context mirroring `_PhaseContext` in `oracle.py`.
#[derive(Debug, Clone)]
pub struct PhaseContext {
    pub state: WorldStateV5,
    pub queue: PhaseWorkQueueV5,
    pub phase: i64,
    pub trace: BTreeMap<TargetKey, GoldTargetTraceV5>,
    pub semantic: PhaseSemanticCache,
}

impl PhaseContext {
    /// Construct a new phase evaluation context.
    #[must_use]
    pub fn new(state: WorldStateV5, queue: PhaseWorkQueueV5, phase: i64) -> Self {
        Self {
            state,
            queue,
            phase,
            trace: BTreeMap::new(),
            semantic: PhaseSemanticCache::default(),
        }
    }

    #[must_use]
    pub fn clock(&self) -> i64 {
        self.state.clock_epoch_s
    }

    #[must_use]
    pub fn overlay(&self) -> &ib_v5_schemas::PolicyOverlayV5 {
        &self.state.overlay
    }

    #[must_use]
    pub fn catalog(&self) -> &ib_v5_schemas::ScopeCatalogV5 {
        &self.state.scope_catalog
    }

    #[must_use]
    pub fn sod_rules(&self) -> &[ib_v5_schemas::ScopedSoDRuleV5] {
        &self.state.overlay.forbidden_sod_rules
    }
}
