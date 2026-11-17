//! Atomic mutation models and tagged union codec for IdentityBench v5.
//!
//! Note: Workspace `serde_json` enables `arbitrary_precision`, so `#[serde(tag = ...)]` and
//! `#[serde(flatten)]` are avoided on structs/enums containing numbers. `AtomicMutationV5`
//! implements custom `Serialize` and `Deserialize` by inspecting `"mutation_kind"`.

use std::collections::BTreeSet;

use serde::de::Error as DeError;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;

use super::common::{
    ACCT_ID_RE, AGT_ID_RE, ATG_ID_RE, BRG_ID_RE, BRULE_ID_RE, CAMP_ID_RE, CAND_ID_RE, CIEM_ID_RE,
    CITE_RE, CONN_ID_RE, CPRIN_ID_RE, DLREQ_ID_RE, DRIFT_TOK_RE, ENG_ID_RE, EngagementStatusV5,
    GRANT_ID_RE, HFEED_ID_RE, HRMAP_ID_RE, INC_ID_RE, ITEM_ID_RE, NHI_ID_RE, OPT_ID_RE, PERS_ID_RE,
    REQ_ID_RE, SchemaValidationError, StrictV5Model, TKT_ID_RE, VERSION_TOK_RE, WFL_ID_RE,
    check_opt_re, check_re, check_slice_re, check_slice_re_any, check_sorted_unique,
};
use super::entities::{
    CIEMPrincipalFindingV5, ITSMTicketActionKindV5, ITSMTicketActionV5, JMLWorkflowSpecV5,
};

fn validate_base_mutation(
    target_entity_id: &str,
    if_match: &[String],
    cite: &[String],
) -> Result<(), SchemaValidationError> {
    let _ = target_entity_id;
    if if_match.is_empty() {
        return Err(SchemaValidationError::validation(
            "if_match must have length >= 1",
        ));
    }
    check_sorted_unique(if_match, "if_match")?;
    for tok in if_match {
        if !VERSION_TOK_RE.is_match(tok) {
            return Err(SchemaValidationError::validation(format!(
                "Invalid version token in if_match: {tok:?}"
            )));
        }
    }
    if cite.is_empty() {
        return Err(SchemaValidationError::validation(
            "cite must have length >= 1",
        ));
    }
    check_sorted_unique(cite, "cite")?;
    for c in cite {
        if !CITE_RE.is_match(c) {
            return Err(SchemaValidationError::validation(format!(
                "Invalid citation token in cite: {c:?}"
            )));
        }
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaseMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
}

impl StrictV5Model for BaseMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)
    }
}

// ---------------------------------------------------------------------------
// Per-variant mutation_kind literal enums (single-variant for strict serde)
// ---------------------------------------------------------------------------

macro_rules! define_kind_tag {
    ($name:ident, $variant:ident, $wire:literal) => {
        #[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
        pub enum $name {
            #[default]
            #[serde(rename = $wire)]
            $variant,
        }
    };
}

define_kind_tag!(
    ConnectorSyncMutationKind,
    ConnectorSyncMutation,
    "CONNECTOR_SYNC_MUTATION"
);
define_kind_tag!(
    HRSourceMappingMutationKind,
    HRSourceMappingMutation,
    "HR_SOURCE_MAPPING_MUTATION"
);
define_kind_tag!(
    AccountReconMutationKind,
    AccountReconMutation,
    "ACCOUNT_RECON_MUTATION"
);
define_kind_tag!(
    NHIGovernanceMutationKind,
    NHIGovernanceMutation,
    "NHI_GOVERNANCE_MUTATION"
);
define_kind_tag!(
    AgentRegistryMutationKind,
    AgentRegistryMutation,
    "AGENT_REGISTRY_MUTATION"
);
define_kind_tag!(
    CloudCIEMMutationKind,
    CloudCIEMMutation,
    "CLOUD_CIEM_MUTATION"
);
define_kind_tag!(
    OptimizationMutationKind,
    OptimizationMutation,
    "OPTIMIZATION_MUTATION"
);
define_kind_tag!(
    JMLWorkflowMutationKind,
    JMLWorkflowMutation,
    "JML_WORKFLOW_MUTATION"
);
define_kind_tag!(
    CAEContainmentMutationKind,
    CAEContainmentMutation,
    "CAE_CONTAINMENT_MUTATION"
);
define_kind_tag!(
    UARCampaignMutationKind,
    UARCampaignMutation,
    "UAR_CAMPAIGN_MUTATION"
);
define_kind_tag!(
    JITDecisionMutationKind,
    JITDecisionMutation,
    "JIT_DECISION_MUTATION"
);
define_kind_tag!(
    AgentDelegationMutationKind,
    AgentDelegationMutation,
    "AGENT_DELEGATION_MUTATION"
);

// ---------------------------------------------------------------------------
// 1. ConnectorSyncMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectorHaltReasonV5 {
    None,
    HaltAuthOrTls,
    HaltCursorOrHttp,
    HaltCheckpointDrift,
    HaltSchemaOrTrailer,
    HaltMassDeleteGuard,
    HaltUnmappedStrict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorSyncMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: ConnectorSyncMutationKind,
    pub halted_fail_closed: bool,
    pub halt_reason: ConnectorHaltReasonV5,
    #[serde(default)]
    pub reconciled_account_ids: Vec<String>,
    #[serde(default)]
    pub remediated_spoke_drift: Vec<String>,
    #[serde(default)]
    pub itsm_ticket_actions: Vec<ITSMTicketActionV5>,
}

impl StrictV5Model for ConnectorSyncMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &CONN_ID_RE,
            "target_entity_id",
            "ConnectorSyncMutationV5",
        )?;
        if self.halted_fail_closed != (self.halt_reason != ConnectorHaltReasonV5::None) {
            return Err(SchemaValidationError::validation(
                "halted_fail_closed must equal (halt_reason != 'NONE')",
            ));
        }
        check_sorted_unique(&self.reconciled_account_ids, "reconciled_account_ids")?;
        check_slice_re(
            &self.reconciled_account_ids,
            &ACCT_ID_RE,
            "reconciled_account_ids",
            "ConnectorSyncMutationV5",
        )?;
        check_sorted_unique(&self.remediated_spoke_drift, "remediated_spoke_drift")?;
        for d in &self.remediated_spoke_drift {
            if !DRIFT_TOK_RE.is_match(d) {
                return Err(SchemaValidationError::validation(format!(
                    "Invalid drift token in remediated_spoke_drift: {d:?}"
                )));
            }
        }
        let toks: Vec<String> = self
            .itsm_ticket_actions
            .iter()
            .map(|a| a.drift_token.clone())
            .collect();
        check_sorted_unique(&toks, "itsm_ticket_actions")?;
        if self.halted_fail_closed
            && (!self.remediated_spoke_drift.is_empty() || !self.itsm_ticket_actions.is_empty())
        {
            return Err(SchemaValidationError::validation(
                "Halted connector cannot emit remediated_spoke_drift or itsm_ticket_actions",
            ));
        }
        for act in &self.itsm_ticket_actions {
            act.validate()?;
            if !DRIFT_TOK_RE.is_match(&act.drift_token) {
                return Err(SchemaValidationError::validation(format!(
                    "Invalid drift_token in itsm_ticket_actions: {:?}",
                    act.drift_token
                )));
            }
            let needs_null = matches!(
                act.ticket_action,
                ITSMTicketActionKindV5::AwaitNextAggregation
                    | ITSMTicketActionKindV5::EscalateItsmUnavailable
            );
            if needs_null != act.assignee_account_id.is_none() {
                return Err(SchemaValidationError::validation(
                    "assignee_account_id nullability contradicts ticket_action",
                ));
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 2. HRSourceMappingMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HRSourceMappingMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: HRSourceMappingMutationKind,
    #[serde(default)]
    pub accepted_feed_ids: Vec<String>,
    #[serde(default)]
    pub rejected_feed_ids: Vec<String>,
    #[serde(default)]
    pub resolved_person_statuses: Vec<(String, EngagementStatusV5)>,
    #[serde(default)]
    pub quarantined_assertion_keys: Vec<String>,
}

impl StrictV5Model for HRSourceMappingMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &HRMAP_ID_RE,
            "target_entity_id",
            "HRSourceMappingMutationV5",
        )?;
        check_sorted_unique(&self.accepted_feed_ids, "accepted_feed_ids")?;
        check_slice_re(
            &self.accepted_feed_ids,
            &HFEED_ID_RE,
            "accepted_feed_ids",
            "HRSourceMappingMutationV5",
        )?;
        check_sorted_unique(&self.rejected_feed_ids, "rejected_feed_ids")?;
        check_slice_re(
            &self.rejected_feed_ids,
            &HFEED_ID_RE,
            "rejected_feed_ids",
            "HRSourceMappingMutationV5",
        )?;
        let accepted_set: BTreeSet<&str> =
            self.accepted_feed_ids.iter().map(String::as_str).collect();
        if self
            .rejected_feed_ids
            .iter()
            .any(|f| accepted_set.contains(f.as_str()))
        {
            return Err(SchemaValidationError::validation(
                "accepted_feed_ids and rejected_feed_ids must be disjoint",
            ));
        }
        let pids: Vec<String> = self
            .resolved_person_statuses
            .iter()
            .map(|(pid, _)| pid.clone())
            .collect();
        check_sorted_unique(&pids, "resolved_person_statuses")?;
        check_slice_re(
            &pids,
            &PERS_ID_RE,
            "resolved_person_statuses",
            "HRSourceMappingMutationV5",
        )?;
        check_sorted_unique(&self.quarantined_assertion_keys, "quarantined_assertion_keys")?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 3. AccountReconMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AccountReconActionV5 {
    KeepActive,
    DisableAccount,
    EscalateBindingConflict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountReconMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: AccountReconMutationKind,
    pub account_action: AccountReconActionV5,
    #[serde(default)]
    pub bound_person_id: Option<String>,
    #[serde(default)]
    pub bound_engagement_id: Option<String>,
    #[serde(default)]
    pub retained_grant_ids: Vec<String>,
}

impl StrictV5Model for AccountReconMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &ACCT_ID_RE,
            "target_entity_id",
            "AccountReconMutationV5",
        )?;
        check_opt_re(
            self.bound_person_id.as_deref(),
            &PERS_ID_RE,
            "bound_person_id",
            "AccountReconMutationV5",
        )?;
        check_opt_re(
            self.bound_engagement_id.as_deref(),
            &ENG_ID_RE,
            "bound_engagement_id",
            "AccountReconMutationV5",
        )?;
        check_sorted_unique(&self.retained_grant_ids, "retained_grant_ids")?;
        check_slice_re(
            &self.retained_grant_ids,
            &GRANT_ID_RE,
            "retained_grant_ids",
            "AccountReconMutationV5",
        )?;
        if self.account_action == AccountReconActionV5::EscalateBindingConflict {
            if self.bound_person_id.is_some() || self.bound_engagement_id.is_some() {
                return Err(SchemaValidationError::validation(
                    "ESCALATE_BINDING_CONFLICT requires null bound_person_id & bound_engagement_id",
                ));
            }
        } else if self.bound_person_id.is_none() || self.bound_engagement_id.is_none() {
            return Err(SchemaValidationError::validation(
                "Bound account requires non-null bound_person_id & bound_engagement_id",
            ));
        }
        if self.account_action == AccountReconActionV5::DisableAccount
            && !self.retained_grant_ids.is_empty()
        {
            return Err(SchemaValidationError::validation(
                "DISABLE_ACCOUNT requires retained_grant_ids == ()",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 4. NHIGovernanceMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum NHIGovernanceActionV5 {
    AttributeOwner,
    FlagSharedException,
    DisableOrphan,
    Escalate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NHIOwnerResolutionSourceV5 {
    IacStateAttestation,
    VaultRotatorPrincipal,
    ParentNhiCustody,
    CicdDeployProvenance,
    CmdbServiceOwner,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NHIGovernanceMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: NHIGovernanceMutationKind,
    pub nhi_action: NHIGovernanceActionV5,
    #[serde(default)]
    pub resolved_owner_person_id: Option<String>,
    #[serde(default)]
    pub owner_resolution_source: Option<NHIOwnerResolutionSourceV5>,
}

impl StrictV5Model for NHIGovernanceMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &NHI_ID_RE,
            "target_entity_id",
            "NHIGovernanceMutationV5",
        )?;
        check_opt_re(
            self.resolved_owner_person_id.as_deref(),
            &PERS_ID_RE,
            "resolved_owner_person_id",
            "NHIGovernanceMutationV5",
        )?;
        let is_attr = self.nhi_action == NHIGovernanceActionV5::AttributeOwner;
        if is_attr != self.resolved_owner_person_id.is_some() {
            return Err(SchemaValidationError::validation(
                "resolved_owner_person_id non-null iff nhi_action == 'ATTRIBUTE_OWNER'",
            ));
        }
        if is_attr != self.owner_resolution_source.is_some() {
            return Err(SchemaValidationError::validation(
                "owner_resolution_source non-null iff nhi_action == 'ATTRIBUTE_OWNER'",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 5. AgentRegistryMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AgentRegistryActionV5 {
    RegisterActive,
    SuspendUnattestedOrStale,
    SuspendSponsorInvalid,
    SuspendNoActiveTools,
    QuarantineSodViolation,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentRegistryMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: AgentRegistryMutationKind,
    pub agent_action: AgentRegistryActionV5,
    #[serde(default)]
    pub resolved_sponsor_person_id: Option<String>,
    #[serde(default)]
    pub active_autonomous_binding_ids: Vec<String>,
    #[serde(default)]
    pub active_hitl_binding_ids: Vec<String>,
}

impl StrictV5Model for AgentRegistryMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &AGT_ID_RE,
            "target_entity_id",
            "AgentRegistryMutationV5",
        )?;
        check_opt_re(
            self.resolved_sponsor_person_id.as_deref(),
            &PERS_ID_RE,
            "resolved_sponsor_person_id",
            "AgentRegistryMutationV5",
        )?;
        check_sorted_unique(
            &self.active_autonomous_binding_ids,
            "active_autonomous_binding_ids",
        )?;
        check_slice_re(
            &self.active_autonomous_binding_ids,
            &ATG_ID_RE,
            "active_autonomous_binding_ids",
            "AgentRegistryMutationV5",
        )?;
        check_sorted_unique(&self.active_hitl_binding_ids, "active_hitl_binding_ids")?;
        check_slice_re(
            &self.active_hitl_binding_ids,
            &ATG_ID_RE,
            "active_hitl_binding_ids",
            "AgentRegistryMutationV5",
        )?;
        let auto_set: BTreeSet<&str> = self
            .active_autonomous_binding_ids
            .iter()
            .map(String::as_str)
            .collect();
        if self
            .active_hitl_binding_ids
            .iter()
            .any(|b| auto_set.contains(b.as_str()))
        {
            return Err(SchemaValidationError::validation(
                "autonomous and HITL binding tuples must be disjoint",
            ));
        }
        if self.agent_action == AgentRegistryActionV5::RegisterActive {
            if self.resolved_sponsor_person_id.is_none() {
                return Err(SchemaValidationError::validation(
                    "REGISTER_ACTIVE requires non-null resolved_sponsor_person_id",
                ));
            }
            if self.active_autonomous_binding_ids.is_empty()
                && self.active_hitl_binding_ids.is_empty()
            {
                return Err(SchemaValidationError::validation(
                    "REGISTER_ACTIVE requires at least one active tool binding",
                ));
            }
        } else {
            if !self.active_autonomous_binding_ids.is_empty()
                || !self.active_hitl_binding_ids.is_empty()
            {
                return Err(SchemaValidationError::validation(
                    "Non-active agent must have empty active tool bindings",
                ));
            }
            if matches!(
                self.agent_action,
                AgentRegistryActionV5::SuspendUnattestedOrStale
                    | AgentRegistryActionV5::SuspendSponsorInvalid
            ) && self.resolved_sponsor_person_id.is_some()
            {
                return Err(SchemaValidationError::validation(
                    "Suspended unattested/invalid-sponsor agent must have null sponsor",
                ));
            }
            if matches!(
                self.agent_action,
                AgentRegistryActionV5::QuarantineSodViolation
                    | AgentRegistryActionV5::SuspendNoActiveTools
            ) && self.resolved_sponsor_person_id.is_none()
            {
                return Err(SchemaValidationError::validation(
                    "QUARANTINE_SOD_VIOLATION / SUSPEND_NO_ACTIVE_TOOLS requires non-null sponsor",
                ));
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 6. CloudCIEMMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloudCIEMMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: CloudCIEMMutationKind,
    pub findings: Vec<CIEMPrincipalFindingV5>,
}

impl StrictV5Model for CloudCIEMMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &CIEM_ID_RE,
            "target_entity_id",
            "CloudCIEMMutationV5",
        )?;
        if self.findings.is_empty() {
            return Err(SchemaValidationError::validation(
                "CloudCIEMMutationV5.findings must have length >= 1",
            ));
        }
        let cids: Vec<String> = self.findings.iter().map(|f| f.cprin_id.clone()).collect();
        check_sorted_unique(&cids, "findings")?;
        for f in &self.findings {
            f.validate()?;
            if !CPRIN_ID_RE.is_match(&f.cprin_id) {
                return Err(SchemaValidationError::validation(format!(
                    "Invalid cprin_id: {:?}",
                    f.cprin_id
                )));
            }
            check_sorted_unique(&f.effective_forbidden_actions, "effective_forbidden_actions")?;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 7. OptimizationMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptimizationMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: OptimizationMutationKind,
    #[serde(default)]
    pub selected_ids: Vec<String>,
    pub objective_value: i64,
}

impl StrictV5Model for OptimizationMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &OPT_ID_RE,
            "target_entity_id",
            "OptimizationMutationV5",
        )?;
        check_sorted_unique(&self.selected_ids, "selected_ids")?;
        check_slice_re(
            &self.selected_ids,
            &CAND_ID_RE,
            "selected_ids",
            "OptimizationMutationV5",
        )?;
        if self.objective_value < 0 {
            return Err(SchemaValidationError::validation(
                "OptimizationMutationV5.objective_value must be >= 0",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 8. JMLWorkflowMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JMLWorkflowMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: JMLWorkflowMutationKind,
    #[serde(default)]
    pub active_birthright_rule_ids: Vec<String>,
    #[serde(default)]
    pub converted_eligible_brule_ids: Vec<String>,
    #[serde(default)]
    pub quarantined_birthright_rule_ids: Vec<String>,
    pub authored_spec: JMLWorkflowSpecV5,
}

impl StrictV5Model for JMLWorkflowMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &WFL_ID_RE,
            "target_entity_id",
            "JMLWorkflowMutationV5",
        )?;
        check_sorted_unique(
            &self.active_birthright_rule_ids,
            "active_birthright_rule_ids",
        )?;
        check_sorted_unique(
            &self.converted_eligible_brule_ids,
            "converted_eligible_brule_ids",
        )?;
        check_sorted_unique(
            &self.quarantined_birthright_rule_ids,
            "quarantined_birthright_rule_ids",
        )?;
        check_slice_re(
            &self.active_birthright_rule_ids,
            &BRULE_ID_RE,
            "active_birthright_rule_ids",
            "JMLWorkflowMutationV5",
        )?;
        check_slice_re(
            &self.converted_eligible_brule_ids,
            &BRULE_ID_RE,
            "converted_eligible_brule_ids",
            "JMLWorkflowMutationV5",
        )?;
        check_slice_re(
            &self.quarantined_birthright_rule_ids,
            &BRULE_ID_RE,
            "quarantined_birthright_rule_ids",
            "JMLWorkflowMutationV5",
        )?;
        let a: BTreeSet<&str> = self
            .active_birthright_rule_ids
            .iter()
            .map(String::as_str)
            .collect();
        let c: BTreeSet<&str> = self
            .converted_eligible_brule_ids
            .iter()
            .map(String::as_str)
            .collect();
        let q: BTreeSet<&str> = self
            .quarantined_birthright_rule_ids
            .iter()
            .map(String::as_str)
            .collect();
        if a.intersection(&c).next().is_some()
            || a.intersection(&q).next().is_some()
            || c.intersection(&q).next().is_some()
        {
            return Err(SchemaValidationError::validation(
                "Birthright rule partitions must be pairwise disjoint",
            ));
        }
        self.authored_spec.validate()?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 9. CAEContainmentMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CAEContainmentMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: CAEContainmentMutationKind,
    pub cae_revoke_sessions: bool,
    pub cae_computed_exposure_s: i64,
    pub cae_sla_breached: bool,
    #[serde(default)]
    pub retro_invalidated_ids: Vec<String>,
}

impl StrictV5Model for CAEContainmentMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &INC_ID_RE,
            "target_entity_id",
            "CAEContainmentMutationV5",
        )?;
        if self.cae_computed_exposure_s < 0 {
            return Err(SchemaValidationError::validation(
                "CAEContainmentMutationV5.cae_computed_exposure_s must be >= 0",
            ));
        }
        check_sorted_unique(&self.retro_invalidated_ids, "retro_invalidated_ids")?;
        check_slice_re_any(
            &self.retro_invalidated_ids,
            &[&TKT_ID_RE, &BRG_ID_RE],
            "retro_invalidated_ids",
            "CAEContainmentMutationV5",
        )?;
        if !self.cae_revoke_sessions && (self.cae_computed_exposure_s != 0 || self.cae_sla_breached)
        {
            return Err(SchemaValidationError::validation(
                "Benign non-revoked CAE incident must have 0 exposure and False breach",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 10. UARCampaignMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UARDecisionKindV5 {
    Certify,
    ConvertToJit,
    Revoke,
    Escalate,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UARCampaignMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: UARCampaignMutationKind,
    pub uar_decisions: Vec<(String, UARDecisionKindV5)>,
    #[serde(default)]
    pub reviewer_overrides: Vec<(String, String)>,
    #[serde(default)]
    pub emergency_custodian_account_id: Option<String>,
}

impl StrictV5Model for UARCampaignMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &CAMP_ID_RE,
            "target_entity_id",
            "UARCampaignMutationV5",
        )?;
        if self.uar_decisions.is_empty() {
            return Err(SchemaValidationError::validation(
                "UARCampaignMutationV5.uar_decisions must have length >= 1",
            ));
        }
        let iids: Vec<String> = self.uar_decisions.iter().map(|(i, _)| i.clone()).collect();
        check_sorted_unique(&iids, "uar_decisions")?;
        check_slice_re(
            &iids,
            &ITEM_ID_RE,
            "uar_decisions",
            "UARCampaignMutationV5",
        )?;
        let ov_iids: Vec<String> = self
            .reviewer_overrides
            .iter()
            .map(|(i, _)| i.clone())
            .collect();
        check_sorted_unique(&ov_iids, "reviewer_overrides")?;
        check_slice_re(
            &ov_iids,
            &ITEM_ID_RE,
            "reviewer_overrides",
            "UARCampaignMutationV5",
        )?;
        for (_, acct_id) in &self.reviewer_overrides {
            check_re(
                acct_id,
                &ACCT_ID_RE,
                "reviewer_overrides",
                "UARCampaignMutationV5",
            )?;
        }
        check_opt_re(
            self.emergency_custodian_account_id.as_deref(),
            &ACCT_ID_RE,
            "emergency_custodian_account_id",
            "UARCampaignMutationV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 11. JITDecisionMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JITDecisionKindV5 {
    ApproveJit,
    ApproveJitCosign,
    Deny,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JITExternalSyncStatusV5 {
    SyncGranted,
    SyncGrantedCosign,
    SyncRejectedPolicy,
    NoExternalTicket,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JITDecisionMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: JITDecisionMutationKind,
    pub jit_decision: JITDecisionKindV5,
    #[serde(default)]
    pub approver_account_id: Option<String>,
    pub approved_ttl_s: i64,
    pub external_sync_status: JITExternalSyncStatusV5,
}

impl StrictV5Model for JITDecisionMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &REQ_ID_RE,
            "target_entity_id",
            "JITDecisionMutationV5",
        )?;
        check_opt_re(
            self.approver_account_id.as_deref(),
            &ACCT_ID_RE,
            "approver_account_id",
            "JITDecisionMutationV5",
        )?;
        if self.approved_ttl_s < 0 {
            return Err(SchemaValidationError::validation(
                "JITDecisionMutationV5.approved_ttl_s must be >= 0",
            ));
        }
        let approved = matches!(
            self.jit_decision,
            JITDecisionKindV5::ApproveJit | JITDecisionKindV5::ApproveJitCosign
        );
        if approved != self.approver_account_id.is_some() {
            return Err(SchemaValidationError::validation(
                "approver_account_id non-null iff JIT request is approved",
            ));
        }
        if approved != (self.approved_ttl_s > 0) {
            return Err(SchemaValidationError::validation(
                "approved_ttl_s > 0 iff JIT request is approved",
            ));
        }
        if !approved
            && matches!(
                self.external_sync_status,
                JITExternalSyncStatusV5::SyncGranted | JITExternalSyncStatusV5::SyncGrantedCosign
            )
        {
            return Err(SchemaValidationError::validation(
                "Denied JIT cannot emit SYNC_GRANTED*",
            ));
        }
        if self.jit_decision == JITDecisionKindV5::ApproveJit
            && self.external_sync_status == JITExternalSyncStatusV5::SyncGrantedCosign
        {
            return Err(SchemaValidationError::validation(
                "APPROVE_JIT cannot emit SYNC_GRANTED_COSIGN",
            ));
        }
        if self.jit_decision == JITDecisionKindV5::ApproveJitCosign
            && self.external_sync_status == JITExternalSyncStatusV5::SyncGranted
        {
            return Err(SchemaValidationError::validation(
                "APPROVE_JIT_COSIGN cannot emit SYNC_GRANTED",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 12. AgentDelegationMutationV5
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AgentDelegationDecisionKindV5 {
    ApproveDelegation,
    ApproveDelegationCosign,
    DenyDelegation,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentDelegationMutationV5 {
    pub target_entity_id: String,
    pub if_match: Vec<String>,
    pub cite: Vec<String>,
    pub mutation_kind: AgentDelegationMutationKind,
    pub delegation_decision: AgentDelegationDecisionKindV5,
    #[serde(default)]
    pub hitl_cosigner_account_id: Option<String>,
    pub approved_ttl_s: i64,
}

impl StrictV5Model for AgentDelegationMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        validate_base_mutation(&self.target_entity_id, &self.if_match, &self.cite)?;
        check_re(
            &self.target_entity_id,
            &DLREQ_ID_RE,
            "target_entity_id",
            "AgentDelegationMutationV5",
        )?;
        check_opt_re(
            self.hitl_cosigner_account_id.as_deref(),
            &ACCT_ID_RE,
            "hitl_cosigner_account_id",
            "AgentDelegationMutationV5",
        )?;
        if self.approved_ttl_s < 0 {
            return Err(SchemaValidationError::validation(
                "AgentDelegationMutationV5.approved_ttl_s must be >= 0",
            ));
        }
        let approved = matches!(
            self.delegation_decision,
            AgentDelegationDecisionKindV5::ApproveDelegation
                | AgentDelegationDecisionKindV5::ApproveDelegationCosign
        );
        if approved != (self.approved_ttl_s > 0) {
            return Err(SchemaValidationError::validation(
                "approved_ttl_s > 0 iff delegation is approved",
            ));
        }
        let needs_hitl =
            self.delegation_decision == AgentDelegationDecisionKindV5::ApproveDelegationCosign;
        if needs_hitl != self.hitl_cosigner_account_id.is_some() {
            return Err(SchemaValidationError::validation(
                "hitl_cosigner_account_id non-null iff APPROVE_DELEGATION_COSIGN",
            ));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// AtomicMutationV5 tagged union (custom Serialize / Deserialize)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AtomicMutationV5 {
    ConnectorSync(ConnectorSyncMutationV5),
    HrSourceMapping(HRSourceMappingMutationV5),
    AccountRecon(AccountReconMutationV5),
    NhiGovernance(NHIGovernanceMutationV5),
    AgentRegistry(AgentRegistryMutationV5),
    CloudCiem(CloudCIEMMutationV5),
    Optimization(OptimizationMutationV5),
    JmlWorkflow(JMLWorkflowMutationV5),
    CaeContainment(CAEContainmentMutationV5),
    UarCampaign(UARCampaignMutationV5),
    JitDecision(JITDecisionMutationV5),
    AgentDelegation(AgentDelegationMutationV5),
}

impl AtomicMutationV5 {
    #[must_use]
    pub fn mutation_kind(&self) -> &'static str {
        match self {
            Self::ConnectorSync(_) => "CONNECTOR_SYNC_MUTATION",
            Self::HrSourceMapping(_) => "HR_SOURCE_MAPPING_MUTATION",
            Self::AccountRecon(_) => "ACCOUNT_RECON_MUTATION",
            Self::NhiGovernance(_) => "NHI_GOVERNANCE_MUTATION",
            Self::AgentRegistry(_) => "AGENT_REGISTRY_MUTATION",
            Self::CloudCiem(_) => "CLOUD_CIEM_MUTATION",
            Self::Optimization(_) => "OPTIMIZATION_MUTATION",
            Self::JmlWorkflow(_) => "JML_WORKFLOW_MUTATION",
            Self::CaeContainment(_) => "CAE_CONTAINMENT_MUTATION",
            Self::UarCampaign(_) => "UAR_CAMPAIGN_MUTATION",
            Self::JitDecision(_) => "JIT_DECISION_MUTATION",
            Self::AgentDelegation(_) => "AGENT_DELEGATION_MUTATION",
        }
    }

    #[must_use]
    pub fn target_entity_id(&self) -> &str {
        match self {
            Self::ConnectorSync(m) => &m.target_entity_id,
            Self::HrSourceMapping(m) => &m.target_entity_id,
            Self::AccountRecon(m) => &m.target_entity_id,
            Self::NhiGovernance(m) => &m.target_entity_id,
            Self::AgentRegistry(m) => &m.target_entity_id,
            Self::CloudCiem(m) => &m.target_entity_id,
            Self::Optimization(m) => &m.target_entity_id,
            Self::JmlWorkflow(m) => &m.target_entity_id,
            Self::CaeContainment(m) => &m.target_entity_id,
            Self::UarCampaign(m) => &m.target_entity_id,
            Self::JitDecision(m) => &m.target_entity_id,
            Self::AgentDelegation(m) => &m.target_entity_id,
        }
    }

    #[must_use]
    pub fn if_match(&self) -> &[String] {
        match self {
            Self::ConnectorSync(m) => &m.if_match,
            Self::HrSourceMapping(m) => &m.if_match,
            Self::AccountRecon(m) => &m.if_match,
            Self::NhiGovernance(m) => &m.if_match,
            Self::AgentRegistry(m) => &m.if_match,
            Self::CloudCiem(m) => &m.if_match,
            Self::Optimization(m) => &m.if_match,
            Self::JmlWorkflow(m) => &m.if_match,
            Self::CaeContainment(m) => &m.if_match,
            Self::UarCampaign(m) => &m.if_match,
            Self::JitDecision(m) => &m.if_match,
            Self::AgentDelegation(m) => &m.if_match,
        }
    }

    #[must_use]
    pub fn cite(&self) -> &[String] {
        match self {
            Self::ConnectorSync(m) => &m.cite,
            Self::HrSourceMapping(m) => &m.cite,
            Self::AccountRecon(m) => &m.cite,
            Self::NhiGovernance(m) => &m.cite,
            Self::AgentRegistry(m) => &m.cite,
            Self::CloudCiem(m) => &m.cite,
            Self::Optimization(m) => &m.cite,
            Self::JmlWorkflow(m) => &m.cite,
            Self::CaeContainment(m) => &m.cite,
            Self::UarCampaign(m) => &m.cite,
            Self::JitDecision(m) => &m.cite,
            Self::AgentDelegation(m) => &m.cite,
        }
    }
}

impl Serialize for AtomicMutationV5 {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::ConnectorSync(m) => m.serialize(serializer),
            Self::HrSourceMapping(m) => m.serialize(serializer),
            Self::AccountRecon(m) => m.serialize(serializer),
            Self::NhiGovernance(m) => m.serialize(serializer),
            Self::AgentRegistry(m) => m.serialize(serializer),
            Self::CloudCiem(m) => m.serialize(serializer),
            Self::Optimization(m) => m.serialize(serializer),
            Self::JmlWorkflow(m) => m.serialize(serializer),
            Self::CaeContainment(m) => m.serialize(serializer),
            Self::UarCampaign(m) => m.serialize(serializer),
            Self::JitDecision(m) => m.serialize(serializer),
            Self::AgentDelegation(m) => m.serialize(serializer),
        }
    }
}

impl<'de> Deserialize<'de> for AtomicMutationV5 {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        let obj = value
            .as_object()
            .ok_or_else(|| D::Error::custom("AtomicMutationV5 must be a JSON object"))?;
        let kind = obj
            .get("mutation_kind")
            .and_then(Value::as_str)
            .ok_or_else(|| D::Error::custom("AtomicMutationV5 missing string mutation_kind"))?;
        let parsed = match kind {
            "CONNECTOR_SYNC_MUTATION" => {
                Self::ConnectorSync(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "HR_SOURCE_MAPPING_MUTATION" => {
                Self::HrSourceMapping(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "ACCOUNT_RECON_MUTATION" => {
                Self::AccountRecon(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "NHI_GOVERNANCE_MUTATION" => {
                Self::NhiGovernance(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "AGENT_REGISTRY_MUTATION" => {
                Self::AgentRegistry(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "CLOUD_CIEM_MUTATION" => {
                Self::CloudCiem(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "OPTIMIZATION_MUTATION" => {
                Self::Optimization(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "JML_WORKFLOW_MUTATION" => {
                Self::JmlWorkflow(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "CAE_CONTAINMENT_MUTATION" => {
                Self::CaeContainment(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "UAR_CAMPAIGN_MUTATION" => {
                Self::UarCampaign(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "JIT_DECISION_MUTATION" => {
                Self::JitDecision(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            "AGENT_DELEGATION_MUTATION" => {
                Self::AgentDelegation(serde_json::from_value(value).map_err(D::Error::custom)?)
            }
            other => {
                return Err(D::Error::custom(format!(
                    "unknown AtomicMutationV5 mutation_kind: {other:?}"
                )));
            }
        };
        parsed.validate().map_err(D::Error::custom)?;
        Ok(parsed)
    }
}

impl StrictV5Model for AtomicMutationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        match self {
            Self::ConnectorSync(m) => m.validate(),
            Self::HrSourceMapping(m) => m.validate(),
            Self::AccountRecon(m) => m.validate(),
            Self::NhiGovernance(m) => m.validate(),
            Self::AgentRegistry(m) => m.validate(),
            Self::CloudCiem(m) => m.validate(),
            Self::Optimization(m) => m.validate(),
            Self::JmlWorkflow(m) => m.validate(),
            Self::CaeContainment(m) => m.validate(),
            Self::UarCampaign(m) => m.validate(),
            Self::JitDecision(m) => m.validate(),
            Self::AgentDelegation(m) => m.validate(),
        }
    }
}
