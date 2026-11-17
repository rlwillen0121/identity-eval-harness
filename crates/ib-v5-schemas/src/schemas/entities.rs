//! Entity, task, and sub-record models for IdentityBench v5 world state.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::common::{
    ACCT_ID_RE, AGT_ID_RE, ATG_ID_RE, AffiliationKindV5, BAGT_ID_RE, BNHI_ID_RE, BRG_ID_RE,
    BRULE_ID_RE, CAMP_ID_RE, CAND_ID_RE, CC_ID_RE, CIEM_ID_RE, CONN_ID_RE, CPRIN_ID_RE,
    CUSTODIAN_GRANT_ID_RE, DLREQ_ID_RE, DRIFT_TOK_RE, ENG_ID_RE, ETAG_HEX_RE, EV_ID_RE,
    EngagementStatusV5, GRANT_ID_RE, GRP_ID_RE, HFEED_ID_RE, HRMAP_ID_RE, HumanSubkindV5,
    INC_ID_RE, ITEM_ID_RE, ITKT_ID_RE, JEV_ID_RE, NHI_ID_RE, NHISubkindV5, OPT_ID_RE, PAGE_ID_RE,
    PERS_ID_RE, PrivilegeTierV5, REQ_ID_RE, ROLE_ID_RE, SESS_ID_RE, SIG_ID_RE,
    SchemaValidationError, StrictV5Model, TKT_ID_RE, TOK_ID_RE, WFL_ID_RE, check_opt_re,
    check_re, check_re_any, check_slice_re, has_all_unique, validate_finite_json_tree,
};

fn default_true() -> bool {
    true
}
fn default_one() -> i64 {
    1
}
fn default_two() -> i64 {
    2
}
fn default_thousand() -> i64 {
    1000
}
fn default_root_scope() -> String {
    "/*".to_owned()
}
fn default_gcp_env() -> String {
    "prod".to_owned()
}

// ---------------------------------------------------------------------------
// HR / Core IGA Entities
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngagementV5 {
    pub eng_id: String,
    pub affiliation: AffiliationKindV5,
    pub eng_status: EngagementStatusV5,
    pub dept_path: String,
    #[serde(default)]
    pub prior_dept_path: Option<String>,
    #[serde(default)]
    pub dept_transfer_effective_at_s: Option<i64>,
    #[serde(default)]
    pub manager_person_id: Option<String>,
    pub start_s: i64,
    #[serde(default)]
    pub end_s: Option<i64>,
    #[serde(default)]
    pub conversion_start_s: Option<i64>,
    #[serde(default)]
    pub conversion_grace_deadline_s: Option<i64>,
    #[serde(default)]
    pub retro_terminated_effective_s: Option<i64>,
}

impl StrictV5Model for EngagementV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.eng_id, &ENG_ID_RE, "eng_id", "EngagementV5")?;
        check_opt_re(
            self.manager_person_id.as_deref(),
            &PERS_ID_RE,
            "manager_person_id",
            "EngagementV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HRPersonV5 {
    pub person_id: String,
    pub emp_immutable_id: String,
    pub primary_email: String,
    #[serde(default)]
    pub historical_emails: Vec<String>,
    pub engagements: Vec<EngagementV5>,
    #[serde(default = "default_one")]
    pub lifecycle_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl HRPersonV5 {
    #[must_use]
    pub fn engagements_by_id(&self) -> HashMap<&str, &EngagementV5> {
        self.engagements
            .iter()
            .map(|e| (e.eng_id.as_str(), e))
            .collect()
    }
}

impl StrictV5Model for HRPersonV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.person_id, &PERS_ID_RE, "person_id", "HRPersonV5")?;
        if self.emp_immutable_id.is_empty() {
            return Err(SchemaValidationError::validation(
                "emp_immutable_id must be non-empty",
            ));
        }
        if self.engagements.is_empty() {
            return Err(SchemaValidationError::validation(
                "HRPersonV5.engagements must have length >= 1",
            ));
        }
        for e in &self.engagements {
            e.validate()?;
        }
        if !has_all_unique(self.engagements.iter().map(|e| e.eng_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "engagements must have unique eng_id per HRPersonV5",
            ));
        }
        if self.lifecycle_gen < 1 {
            return Err(SchemaValidationError::validation(
                "HRPersonV5.lifecycle_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "HRPersonV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "HRPersonV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoleV5 {
    pub role_id: String,
    pub app_id: String,
    pub privilege_tier: PrivilegeTierV5,
    pub capabilities: Vec<String>,
    #[serde(default = "default_true")]
    pub is_jit_capable_target: bool,
    pub evidence_id: String,
}

impl StrictV5Model for RoleV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.role_id, &ROLE_ID_RE, "role_id", "RoleV5")?;
        if self.capabilities.is_empty() {
            return Err(SchemaValidationError::validation(
                "RoleV5.capabilities must have length >= 1",
            ));
        }
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "RoleV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GrantModeV5 {
    Standing,
    Eligible,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantV5 {
    pub grant_id: String,
    pub account_id: String,
    pub role_id: String,
    pub grant_mode: GrantModeV5,
    pub scope_path: String,
    pub sod_prune_priority: i64,
    pub evidence_id: String,
}

impl StrictV5Model for GrantV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re_any(
            &self.grant_id,
            &[&GRANT_ID_RE, &CUSTODIAN_GRANT_ID_RE],
            "grant_id",
            "GrantV5",
        )?;
        check_re(&self.account_id, &ACCT_ID_RE, "account_id", "GrantV5")?;
        check_re(&self.role_id, &ROLE_ID_RE, "role_id", "GrantV5")?;
        if self.sod_prune_priority < 1 {
            return Err(SchemaValidationError::validation(
                "GrantV5.sod_prune_priority must be >= 1",
            ));
        }
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "GrantV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum GroupEffectV5 {
    Allow,
    Deny,
}

fn default_group_effect() -> GroupEffectV5 {
    GroupEffectV5::Allow
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GroupV5 {
    pub group_id: String,
    #[serde(default)]
    pub member_of_group_ids: Vec<String>,
    #[serde(default = "default_group_effect")]
    pub effect: GroupEffectV5,
    #[serde(default = "default_root_scope")]
    pub scope_path: String,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default)]
    pub granted_role_ids: Vec<String>,
    pub evidence_id: String,
}

impl StrictV5Model for GroupV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.group_id, &GRP_ID_RE, "group_id", "GroupV5")?;
        check_slice_re(
            &self.member_of_group_ids,
            &GRP_ID_RE,
            "member_of_group_ids",
            "GroupV5",
        )?;
        check_slice_re(
            &self.granted_role_ids,
            &ROLE_ID_RE,
            "granted_role_ids",
            "GroupV5",
        )?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "GroupV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum ActiveDisabledStatusV5 {
    Active,
    Disabled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IdPAccountV5 {
    pub account_id: String,
    pub subkind: HumanSubkindV5,
    pub login_email: String,
    #[serde(default)]
    pub immutable_person_ref: Option<String>,
    #[serde(default)]
    pub bound_engagement_ref: Option<String>,
    pub status: ActiveDisabledStatusV5,
    #[serde(default)]
    pub group_ids: Vec<String>,
    #[serde(default)]
    pub direct_grant_ids: Vec<String>,
    #[serde(default = "default_one")]
    pub account_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for IdPAccountV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.account_id, &ACCT_ID_RE, "account_id", "IdPAccountV5")?;
        check_opt_re(
            self.immutable_person_ref.as_deref(),
            &PERS_ID_RE,
            "immutable_person_ref",
            "IdPAccountV5",
        )?;
        check_opt_re(
            self.bound_engagement_ref.as_deref(),
            &ENG_ID_RE,
            "bound_engagement_ref",
            "IdPAccountV5",
        )?;
        check_slice_re(&self.group_ids, &GRP_ID_RE, "group_ids", "IdPAccountV5")?;
        check_slice_re(
            &self.direct_grant_ids,
            &GRANT_ID_RE,
            "direct_grant_ids",
            "IdPAccountV5",
        )?;
        if self.account_gen < 1 {
            return Err(SchemaValidationError::validation(
                "IdPAccountV5.account_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "IdPAccountV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "IdPAccountV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransferBridgeV5 {
    pub bridge_id: String,
    pub account_id: String,
    pub grant_id: String,
    pub from_dept_path: String,
    pub to_dept_path: String,
    pub target_resource: String,
    pub approved_by_account_id: String,
    pub approved_at_s: i64,
    pub expires_at_s: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for TransferBridgeV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.bridge_id, &BRG_ID_RE, "bridge_id", "TransferBridgeV5")?;
        check_re(
            &self.account_id,
            &ACCT_ID_RE,
            "account_id",
            "TransferBridgeV5",
        )?;
        check_re(&self.grant_id, &GRANT_ID_RE, "grant_id", "TransferBridgeV5")?;
        check_re(
            &self.approved_by_account_id,
            &ACCT_ID_RE,
            "approved_by_account_id",
            "TransferBridgeV5",
        )?;
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "TransferBridgeV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "TransferBridgeV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompensatingControlV5 {
    pub cc_id: String,
    pub monitored_person_id: String,
    pub cap_a: String,
    pub cap_b: String,
    pub covered_scope_path: String,
    pub monitor_account_id: String,
    pub required_monitor_capability: String,
    pub audit_connector_id: String,
    #[serde(default = "default_true")]
    pub preserve_if_live: bool,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for CompensatingControlV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.cc_id, &CC_ID_RE, "cc_id", "CompensatingControlV5")?;
        check_re(
            &self.monitored_person_id,
            &PERS_ID_RE,
            "monitored_person_id",
            "CompensatingControlV5",
        )?;
        check_re(
            &self.monitor_account_id,
            &ACCT_ID_RE,
            "monitor_account_id",
            "CompensatingControlV5",
        )?;
        check_re(
            &self.audit_connector_id,
            &CONN_ID_RE,
            "audit_connector_id",
            "CompensatingControlV5",
        )?;
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "CompensatingControlV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "CompensatingControlV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Connectors & HR Source Mapping
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ITSMTicketStatusV5 {
    Open,
    InProgress,
    ClosedComplete,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ITSMTicketRecordV5 {
    pub itkt_id: String,
    pub drift_token: String,
    pub ticket_status: ITSMTicketStatusV5,
    #[serde(default)]
    pub closed_at_s: Option<i64>,
    pub sla_due_s: i64,
    pub assigned_to_account_id: String,
    pub evidence_id: String,
}

impl StrictV5Model for ITSMTicketRecordV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.itkt_id, &ITKT_ID_RE, "itkt_id", "ITSMTicketRecordV5")?;
        check_re(
            &self.drift_token,
            &DRIFT_TOK_RE,
            "drift_token",
            "ITSMTicketRecordV5",
        )?;
        if self.closed_at_s.is_some() != (self.ticket_status == ITSMTicketStatusV5::ClosedComplete)
        {
            return Err(SchemaValidationError::validation(
                "closed_at_s must be non-null iff ticket_status == 'CLOSED_COMPLETE'",
            ));
        }
        check_re(
            &self.assigned_to_account_id,
            &ACCT_ID_RE,
            "assigned_to_account_id",
            "ITSMTicketRecordV5",
        )?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "ITSMTicketRecordV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ITSMTicketActionKindV5 {
    DispatchNewTicket,
    KeepPendingInSla,
    EscalateSlaOrCoi,
    AwaitNextAggregation,
    ReopenPhantomClosure,
    EscalateItsmUnavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ITSMTicketActionV5 {
    pub drift_token: String,
    pub ticket_action: ITSMTicketActionKindV5,
    #[serde(default)]
    pub assignee_account_id: Option<String>,
}

impl StrictV5Model for ITSMTicketActionV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_opt_re(
            self.assignee_account_id.as_deref(),
            &ACCT_ID_RE,
            "assignee_account_id",
            "ITSMTicketActionV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectorTransportKindV5 {
    RestWebService,
    JdbcSql,
    FlatFileSftp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectorProvisioningModeV5 {
    AutomatedWrite,
    DisconnectedItsmTicket,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectorSyncModeV5 {
    FullSnapshot,
    DeltaCursor,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectorAuthStateV5 {
    AuthOk,
    OauthScopeMissing,
    MtlsCertExpired,
    JdbcViewDenied,
    SftpSignatureInvalid,
}

fn default_auth_ok() -> ConnectorAuthStateV5 {
    ConnectorAuthStateV5::AuthOk
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UnmappedPolicyOverrideV5 {
    UseOverlayDefault,
    HaltConnector,
    QuarantineDrift,
}

fn default_unmapped_policy_override() -> UnmappedPolicyOverrideV5 {
    UnmappedPolicyOverrideV5::UseOverlayDefault
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorSchemaV5 {
    pub connector_id: String,
    pub app_id: String,
    pub transport_kind: ConnectorTransportKindV5,
    pub provisioning_mode: ConnectorProvisioningModeV5,
    pub sync_mode: ConnectorSyncModeV5,
    #[serde(default = "default_auth_ok")]
    pub auth_state: ConnectorAuthStateV5,
    #[serde(default = "default_true")]
    pub has_delete_audit_view: bool,
    #[serde(default = "default_thousand")]
    pub max_delete_threshold_bps: i64,
    #[serde(default)]
    pub expected_file_row_count: Option<i64>,
    pub required_fields: Vec<String>,
    #[serde(default)]
    pub mapping_rules: Vec<(String, String)>,
    #[serde(default = "default_unmapped_policy_override")]
    pub unmapped_policy_override: UnmappedPolicyOverrideV5,
    #[serde(default)]
    pub local_checkpoint_etag: Option<String>,
    #[serde(default)]
    pub baseline_reconciled_account_ids: Vec<String>,
    pub audit_watermark_s: i64,
    #[serde(default = "default_true")]
    pub itsm_bridge_healthy: bool,
    #[serde(default)]
    pub itsm_primary_fulfiller_account_id: Option<String>,
    #[serde(default)]
    pub itsm_backup_fulfiller_account_id: Option<String>,
    #[serde(default)]
    pub open_itsm_tickets: Vec<ITSMTicketRecordV5>,
    #[serde(default = "default_one")]
    pub sync_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for ConnectorSchemaV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.connector_id,
            &CONN_ID_RE,
            "connector_id",
            "ConnectorSchemaV5",
        )?;
        if !(0..=10000).contains(&self.max_delete_threshold_bps) {
            return Err(SchemaValidationError::validation(
                "ConnectorSchemaV5.max_delete_threshold_bps must be in 0..=10000",
            ));
        }
        if self.required_fields.is_empty() {
            return Err(SchemaValidationError::validation(
                "ConnectorSchemaV5.required_fields must have length >= 1",
            ));
        }
        if self.expected_file_row_count.is_some()
            != (self.transport_kind == ConnectorTransportKindV5::FlatFileSftp)
        {
            return Err(SchemaValidationError::validation(
                "expected_file_row_count must be non-null iff transport_kind == 'FLAT_FILE_SFTP'",
            ));
        }
        check_opt_re(
            self.local_checkpoint_etag.as_deref(),
            &ETAG_HEX_RE,
            "local_checkpoint_etag",
            "ConnectorSchemaV5",
        )?;
        check_slice_re(
            &self.baseline_reconciled_account_ids,
            &ACCT_ID_RE,
            "baseline_reconciled_account_ids",
            "ConnectorSchemaV5",
        )?;
        check_opt_re(
            self.itsm_primary_fulfiller_account_id.as_deref(),
            &ACCT_ID_RE,
            "itsm_primary_fulfiller_account_id",
            "ConnectorSchemaV5",
        )?;
        check_opt_re(
            self.itsm_backup_fulfiller_account_id.as_deref(),
            &ACCT_ID_RE,
            "itsm_backup_fulfiller_account_id",
            "ConnectorSchemaV5",
        )?;
        for t in &self.open_itsm_tickets {
            t.validate()?;
        }
        if !has_all_unique(self.open_itsm_tickets.iter().map(|t| t.itkt_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "open_itsm_tickets must have unique itkt_id per connector",
            ));
        }
        if !has_all_unique(self.open_itsm_tickets.iter().map(|t| t.drift_token.as_str())) {
            return Err(SchemaValidationError::validation(
                "open_itsm_tickets must have unique drift_token per connector",
            ));
        }
        if !has_all_unique(self.mapping_rules.iter().map(|(k, _)| k.as_str())) {
            return Err(SchemaValidationError::validation(
                "mapping_rules must have unique raw entitlement keys",
            ));
        }
        if self.sync_gen < 1 {
            return Err(SchemaValidationError::validation(
                "ConnectorSchemaV5.sync_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "ConnectorSchemaV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "ConnectorSchemaV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorPageV5 {
    pub page_id: String,
    pub connector_id: String,
    pub cursor_in: String,
    pub attempt_index: i64,
    pub http_status: i64,
    #[serde(default)]
    pub next_cursor: Option<String>,
    #[serde(default)]
    pub base_checkpoint_etag: Option<String>,
    #[serde(default)]
    pub records: Vec<Vec<(String, Value)>>,
    pub evidence_id: String,
}

impl StrictV5Model for ConnectorPageV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.page_id, &PAGE_ID_RE, "page_id", "ConnectorPageV5")?;
        check_re(
            &self.connector_id,
            &CONN_ID_RE,
            "connector_id",
            "ConnectorPageV5",
        )?;
        if self.attempt_index < 0 {
            return Err(SchemaValidationError::validation(
                "ConnectorPageV5.attempt_index must be >= 0",
            ));
        }
        if !matches!(self.http_status, 200 | 429 | 500) {
            return Err(SchemaValidationError::validation(format!(
                "ConnectorPageV5.http_status must be 200, 429, or 500, got {}",
                self.http_status
            )));
        }
        check_opt_re(
            self.base_checkpoint_etag.as_deref(),
            &ETAG_HEX_RE,
            "base_checkpoint_etag",
            "ConnectorPageV5",
        )?;
        for (row_idx, row) in self.records.iter().enumerate() {
            for (cell_idx, (_, cell)) in row.iter().enumerate() {
                validate_finite_json_tree(cell, &format!("$.records[{row_idx}][{cell_idx}][1]"))?;
            }
        }
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "ConnectorPageV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawHRFeedAssertionV5 {
    pub person_id: String,
    #[serde(default)]
    pub emp_immutable_id: Option<String>,
    pub email: String,
    pub affiliation: AffiliationKindV5,
    pub raw_status_code: String,
}

impl StrictV5Model for RawHRFeedAssertionV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.person_id,
            &PERS_ID_RE,
            "person_id",
            "RawHRFeedAssertionV5",
        )?;
        if let Some(emp) = &self.emp_immutable_id
            && emp.is_empty()
        {
            return Err(SchemaValidationError::validation(
                "emp_immutable_id must be non-empty",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum HRSourceKindV5 {
    WorkdayHris,
    VmsVendor,
    LegacyCsvBackup,
    TargetAppSpoke,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum HRCorrelationStrategyV5 {
    ImmutableEmpIdThenEmail,
    MutableEmailOnly,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HRSourceFeedSpecV5 {
    pub feed_id: String,
    pub connector_id: String,
    pub source_kind: HRSourceKindV5,
    pub authorized_affiliations: Vec<AffiliationKindV5>,
    pub precedence_rank: i64,
    pub correlation_strategy: HRCorrelationStrategyV5,
    #[serde(default)]
    pub status_code_map: Vec<(String, EngagementStatusV5)>,
    #[serde(default)]
    pub assertions: Vec<RawHRFeedAssertionV5>,
    pub evidence_id: String,
}

impl StrictV5Model for HRSourceFeedSpecV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.feed_id, &HFEED_ID_RE, "feed_id", "HRSourceFeedSpecV5")?;
        check_re(
            &self.connector_id,
            &CONN_ID_RE,
            "connector_id",
            "HRSourceFeedSpecV5",
        )?;
        if self.authorized_affiliations.is_empty() {
            return Err(SchemaValidationError::validation(
                "HRSourceFeedSpecV5.authorized_affiliations must have length >= 1",
            ));
        }
        if self.precedence_rank < 1 {
            return Err(SchemaValidationError::validation(
                "HRSourceFeedSpecV5.precedence_rank must be >= 1",
            ));
        }
        if !has_all_unique(self.status_code_map.iter().map(|(k, _)| k.as_str())) {
            return Err(SchemaValidationError::validation(
                "status_code_map must have unique raw_status_code keys",
            ));
        }
        for a in &self.assertions {
            a.validate()?;
        }
        if !has_all_unique(self.assertions.iter().map(|a| a.person_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "assertions must have unique (feed_id, person_id) keys per feed",
            ));
        }
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "HRSourceFeedSpecV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HRSourceMappingTaskV5 {
    pub mapping_task_id: String,
    pub feeds: Vec<HRSourceFeedSpecV5>,
    #[serde(default = "default_one")]
    pub mapping_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for HRSourceMappingTaskV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.mapping_task_id,
            &HRMAP_ID_RE,
            "mapping_task_id",
            "HRSourceMappingTaskV5",
        )?;
        if self.feeds.is_empty() {
            return Err(SchemaValidationError::validation(
                "HRSourceMappingTaskV5.feeds must have length >= 1",
            ));
        }
        for f in &self.feeds {
            f.validate()?;
        }
        if !has_all_unique(self.feeds.iter().map(|f| f.feed_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "feeds must have unique feed_id per HRSourceMappingTaskV5",
            ));
        }
        if self.mapping_gen < 1 {
            return Err(SchemaValidationError::validation(
                "HRSourceMappingTaskV5.mapping_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "HRSourceMappingTaskV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "HRSourceMappingTaskV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// NHI & Agent Governance Entities
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EnvironmentKindV5 {
    Prod,
    Staging,
    Dev,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaselineNHIV5 {
    pub bnhi_id: String,
    pub owner_person_id: String,
    pub environment: EnvironmentKindV5,
    pub status: ActiveDisabledStatusV5,
    pub last_rotated_at_s: i64,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default = "default_root_scope")]
    pub scope_path: String,
    #[serde(default = "default_one")]
    pub baseline_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for BaselineNHIV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.bnhi_id, &BNHI_ID_RE, "bnhi_id", "BaselineNHIV5")?;
        check_re(
            &self.owner_person_id,
            &PERS_ID_RE,
            "owner_person_id",
            "BaselineNHIV5",
        )?;
        if self.baseline_gen < 1 {
            return Err(SchemaValidationError::validation(
                "BaselineNHIV5.baseline_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "BaselineNHIV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "BaselineNHIV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TelemetrySignalSourceV5 {
    IacStateAttestation,
    VaultRotatorPrincipal,
    ParentNhiCustody,
    CicdDeployProvenance,
    CmdbServiceOwner,
    StaleGroupTag,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TelemetrySignalV5 {
    pub signal_id: String,
    pub source: TelemetrySignalSourceV5,
    #[serde(default)]
    pub candidate_person_id: Option<String>,
    #[serde(default)]
    pub rotator_account_id: Option<String>,
    #[serde(default)]
    pub parent_nhi_id: Option<String>,
    pub synced_at_s: i64,
    pub evidence_id: String,
}

impl StrictV5Model for TelemetrySignalV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.signal_id, &SIG_ID_RE, "signal_id", "TelemetrySignalV5")?;
        check_opt_re(
            self.candidate_person_id.as_deref(),
            &PERS_ID_RE,
            "candidate_person_id",
            "TelemetrySignalV5",
        )?;
        check_opt_re(
            self.rotator_account_id.as_deref(),
            &ACCT_ID_RE,
            "rotator_account_id",
            "TelemetrySignalV5",
        )?;
        check_opt_re(
            self.parent_nhi_id.as_deref(),
            &BNHI_ID_RE,
            "parent_nhi_id",
            "TelemetrySignalV5",
        )?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "TelemetrySignalV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TargetNHIV5 {
    pub nhi_id: String,
    pub subkind: NHISubkindV5,
    pub environment: EnvironmentKindV5,
    pub capabilities: Vec<String>,
    pub scope_path: String,
    #[serde(default)]
    pub signals: Vec<TelemetrySignalV5>,
    #[serde(default = "default_one")]
    pub nhi_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for TargetNHIV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.nhi_id, &NHI_ID_RE, "nhi_id", "TargetNHIV5")?;
        if self.capabilities.is_empty() {
            return Err(SchemaValidationError::validation(
                "TargetNHIV5.capabilities must have length >= 1",
            ));
        }
        for s in &self.signals {
            s.validate()?;
        }
        if !has_all_unique(self.signals.iter().map(|s| s.signal_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "signals must have unique signal_id per TargetNHIV5",
            ));
        }
        if self.nhi_gen < 1 {
            return Err(SchemaValidationError::validation(
                "TargetNHIV5.nhi_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "TargetNHIV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "TargetNHIV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum BaselineAgentStatusV5 {
    Active,
    Suspended,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaselineAgentV5 {
    pub bagt_id: String,
    pub sponsor_person_id: String,
    pub environment: EnvironmentKindV5,
    pub status: BaselineAgentStatusV5,
    #[serde(default)]
    pub autonomous_capabilities: Vec<String>,
    #[serde(default = "default_root_scope")]
    pub scope_path: String,
    pub evidence_id: String,
}

impl StrictV5Model for BaselineAgentV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.bagt_id, &BAGT_ID_RE, "bagt_id", "BaselineAgentV5")?;
        check_re(
            &self.sponsor_person_id,
            &PERS_ID_RE,
            "sponsor_person_id",
            "BaselineAgentV5",
        )?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "BaselineAgentV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AgentToolModeV5 {
    AutonomousBounded,
    HitlRequired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentToolBindingV5 {
    pub binding_id: String,
    pub tool_connector_id: String,
    pub requested_mode: AgentToolModeV5,
    pub privilege_tier: PrivilegeTierV5,
    pub capabilities: Vec<String>,
    pub scope_path: String,
    pub evidence_id: String,
}

impl StrictV5Model for AgentToolBindingV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.binding_id,
            &ATG_ID_RE,
            "binding_id",
            "AgentToolBindingV5",
        )?;
        check_re(
            &self.tool_connector_id,
            &CONN_ID_RE,
            "tool_connector_id",
            "AgentToolBindingV5",
        )?;
        if self.capabilities.is_empty() {
            return Err(SchemaValidationError::validation(
                "AgentToolBindingV5.capabilities must have length >= 1",
            ));
        }
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "AgentToolBindingV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnterpriseAgentV5 {
    pub agent_id: String,
    pub environment: EnvironmentKindV5,
    pub manifest_digest_etag: String,
    pub approved_registry_digest_etag: String,
    pub attestation_synced_at_s: i64,
    pub candidate_sponsor_person_id: String,
    pub tool_bindings: Vec<AgentToolBindingV5>,
    #[serde(default = "default_one")]
    pub agent_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for EnterpriseAgentV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.agent_id, &AGT_ID_RE, "agent_id", "EnterpriseAgentV5")?;
        check_re(
            &self.manifest_digest_etag,
            &ETAG_HEX_RE,
            "manifest_digest_etag",
            "EnterpriseAgentV5",
        )?;
        check_re(
            &self.approved_registry_digest_etag,
            &ETAG_HEX_RE,
            "approved_registry_digest_etag",
            "EnterpriseAgentV5",
        )?;
        check_re(
            &self.candidate_sponsor_person_id,
            &PERS_ID_RE,
            "candidate_sponsor_person_id",
            "EnterpriseAgentV5",
        )?;
        if self.tool_bindings.is_empty() {
            return Err(SchemaValidationError::validation(
                "EnterpriseAgentV5.tool_bindings must have length >= 1",
            ));
        }
        for b in &self.tool_bindings {
            b.validate()?;
        }
        if !has_all_unique(self.tool_bindings.iter().map(|b| b.binding_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "tool_bindings must have unique binding_id per EnterpriseAgentV5",
            ));
        }
        if self.agent_gen < 1 {
            return Err(SchemaValidationError::validation(
                "EnterpriseAgentV5.agent_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "EnterpriseAgentV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "EnterpriseAgentV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Cloud CIEM
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum CloudProviderV5 {
    Aws,
    Azure,
    Gcp,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloudPrincipalBindingV5 {
    pub cprin_id: String,
    pub cloud_provider: CloudProviderV5,
    pub linked_principal_id: String,
    #[serde(default = "default_true")]
    pub aws_same_account_resource: bool,
    #[serde(default)]
    pub aws_identity_allow: Vec<String>,
    #[serde(default)]
    pub aws_resource_policy_allow: Vec<String>,
    #[serde(default)]
    pub aws_permission_boundary_allow: Option<Vec<String>>,
    #[serde(default)]
    pub aws_scp_allow: Vec<String>,
    #[serde(default)]
    pub aws_explicit_deny: Vec<String>,
    #[serde(default)]
    pub aws_passrole_target_actions: Vec<String>,
    #[serde(default)]
    pub aws_assumerole_chain_actions: Vec<String>,
    #[serde(default)]
    pub aws_trust_policy_valid: bool,
    #[serde(default)]
    pub az_role_assignments: Vec<(Vec<String>, Vec<String>)>,
    #[serde(default)]
    pub az_deny_assignment_actions: Vec<String>,
    #[serde(default)]
    pub az_exempt_from_deny_assignment: bool,
    #[serde(default)]
    pub az_unconstrained_role_assign_write_target_actions: Vec<String>,
    #[serde(default)]
    pub az_abac_delegation_constrained: bool,
    #[serde(default)]
    pub az_entra_app_cred_target_actions: Vec<String>,
    #[serde(default = "default_gcp_env")]
    pub gcp_resource_env_tag: String,
    #[serde(default)]
    pub gcp_allow_bindings: Vec<(String, Vec<String>)>,
    #[serde(default)]
    pub gcp_v2_deny_actions: Vec<String>,
    #[serde(default)]
    pub gcp_sa_impersonation_target_actions: Vec<String>,
    #[serde(default)]
    pub gcp_wif_unscoped_repo_claim: bool,
}

impl StrictV5Model for CloudPrincipalBindingV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.cprin_id,
            &CPRIN_ID_RE,
            "cprin_id",
            "CloudPrincipalBindingV5",
        )?;
        check_re_any(
            &self.linked_principal_id,
            &[&ACCT_ID_RE, &NHI_ID_RE, &AGT_ID_RE],
            "linked_principal_id",
            "CloudPrincipalBindingV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CIEMTaskV5 {
    pub ciem_task_id: String,
    pub forbidden_standing_cloud_actions: Vec<String>,
    pub principals: Vec<CloudPrincipalBindingV5>,
    #[serde(default = "default_one")]
    pub ciem_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for CIEMTaskV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.ciem_task_id, &CIEM_ID_RE, "ciem_task_id", "CIEMTaskV5")?;
        if self.forbidden_standing_cloud_actions.is_empty() {
            return Err(SchemaValidationError::validation(
                "CIEMTaskV5.forbidden_standing_cloud_actions must have length >= 1",
            ));
        }
        if self.principals.len() < 3 {
            return Err(SchemaValidationError::validation(
                "CIEMTaskV5.principals must have length >= 3",
            ));
        }
        for p in &self.principals {
            p.validate()?;
        }
        if !has_all_unique(self.principals.iter().map(|p| p.cprin_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "principals must have unique cprin_id per CIEMTaskV5",
            ));
        }
        if self.ciem_gen < 1 {
            return Err(SchemaValidationError::validation(
                "CIEMTaskV5.ciem_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "CIEMTaskV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "CIEMTaskV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CIEMEscalationVectorV5 {
    None,
    AwsPassroleExec,
    AwsAssumeroleChain,
    AzureRoleAssignWrite,
    AzureEntraAppCred,
    GcpSaImpersonation,
    GcpWifUnscopedClaim,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CIEMRemediationActionV5 {
    KeepCompliant,
    AttachBoundaryOrDeny,
    StripEscalationEdge,
    LockWifAttributeCondition,
    RevokeOrphanCloudPrincipal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CIEMPrincipalFindingV5 {
    pub cprin_id: String,
    #[serde(default)]
    pub effective_forbidden_actions: Vec<String>,
    pub escalation_vector: CIEMEscalationVectorV5,
    pub remediation_action: CIEMRemediationActionV5,
}

impl StrictV5Model for CIEMPrincipalFindingV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.cprin_id,
            &CPRIN_ID_RE,
            "cprin_id",
            "CIEMPrincipalFindingV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Optimization
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OptActionKindV5 {
    ConvertGrantToEligible,
    AssignRoleStanding,
    SelectReviewerAccount,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptCandidateActionV5 {
    pub candidate_id: String,
    pub action_kind: OptActionKindV5,
    #[serde(default)]
    pub target_person_id: Option<String>,
    #[serde(default)]
    pub target_grant_id: Option<String>,
    #[serde(default)]
    pub role_id: Option<String>,
    #[serde(default)]
    pub scope_path: Option<String>,
    #[serde(default)]
    pub candidate_account_id: Option<String>,
    #[serde(default)]
    pub covered_scopes: Vec<String>,
    pub action_cost: i64,
}

impl StrictV5Model for OptCandidateActionV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.candidate_id,
            &CAND_ID_RE,
            "candidate_id",
            "OptCandidateActionV5",
        )?;
        check_opt_re(
            self.target_person_id.as_deref(),
            &PERS_ID_RE,
            "target_person_id",
            "OptCandidateActionV5",
        )?;
        check_opt_re(
            self.target_grant_id.as_deref(),
            &GRANT_ID_RE,
            "target_grant_id",
            "OptCandidateActionV5",
        )?;
        check_opt_re(
            self.role_id.as_deref(),
            &ROLE_ID_RE,
            "role_id",
            "OptCandidateActionV5",
        )?;
        check_opt_re(
            self.candidate_account_id.as_deref(),
            &ACCT_ID_RE,
            "candidate_account_id",
            "OptCandidateActionV5",
        )?;
        if self.action_cost < 0 {
            return Err(SchemaValidationError::validation(
                "OptCandidateActionV5.action_cost must be >= 0",
            ));
        }
        match self.action_kind {
            OptActionKindV5::ConvertGrantToEligible => {
                if self.target_person_id.is_none() || self.target_grant_id.is_none() {
                    return Err(SchemaValidationError::validation(
                        "CONVERT_GRANT_TO_ELIGIBLE requires target_person_id and target_grant_id",
                    ));
                }
                if self.role_id.is_some()
                    || self.scope_path.is_some()
                    || self.candidate_account_id.is_some()
                    || !self.covered_scopes.is_empty()
                {
                    return Err(SchemaValidationError::validation(
                        "CONVERT_GRANT_TO_ELIGIBLE forbids role_id/scope_path/candidate_account_id/covered_scopes",
                    ));
                }
            }
            OptActionKindV5::AssignRoleStanding => {
                if self.target_person_id.is_none()
                    || self.role_id.is_none()
                    || self.scope_path.is_none()
                {
                    return Err(SchemaValidationError::validation(
                        "ASSIGN_ROLE_STANDING requires target_person_id, role_id, and scope_path",
                    ));
                }
                if self.target_grant_id.is_some()
                    || self.candidate_account_id.is_some()
                    || !self.covered_scopes.is_empty()
                {
                    return Err(SchemaValidationError::validation(
                        "ASSIGN_ROLE_STANDING forbids target_grant_id/candidate_account_id/covered_scopes",
                    ));
                }
            }
            OptActionKindV5::SelectReviewerAccount => {
                if self.candidate_account_id.is_none()
                    || self.role_id.is_none()
                    || self.covered_scopes.is_empty()
                {
                    return Err(SchemaValidationError::validation(
                        "SELECT_REVIEWER_ACCOUNT requires candidate_account_id, role_id, and non-empty covered_scopes",
                    ));
                }
                if self.target_person_id.is_some()
                    || self.target_grant_id.is_some()
                    || self.scope_path.is_some()
                {
                    return Err(SchemaValidationError::validation(
                        "SELECT_REVIEWER_ACCOUNT forbids target_person_id/target_grant_id/scope_path",
                    ));
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OptimizationPhaseKindV5 {
    #[serde(rename = "P1_ZSP_REFACTOR")]
    P1ZspRefactor,
    #[serde(rename = "P2_REVIEWER_ASSIGNMENT")]
    P2ReviewerAssignment,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptimizationTaskV5 {
    pub task_id: String,
    pub phase_kind: OptimizationPhaseKindV5,
    #[serde(default)]
    pub required_person_caps: Vec<(String, String, String)>,
    #[serde(default)]
    pub required_review_scopes: Vec<String>,
    #[serde(default)]
    pub excluded_conflict_person_ids: Vec<String>,
    #[serde(default = "default_two")]
    pub max_assignments_per_person: i64,
    pub candidates: Vec<OptCandidateActionV5>,
    pub etag: String,
    pub evidence_id: String,
}

impl OptimizationTaskV5 {
    #[must_use]
    pub fn candidate_by_id(&self) -> HashMap<&str, &OptCandidateActionV5> {
        self.candidates
            .iter()
            .map(|c| (c.candidate_id.as_str(), c))
            .collect()
    }
}

impl StrictV5Model for OptimizationTaskV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.task_id, &OPT_ID_RE, "task_id", "OptimizationTaskV5")?;
        check_slice_re(
            &self.excluded_conflict_person_ids,
            &PERS_ID_RE,
            "excluded_conflict_person_ids",
            "OptimizationTaskV5",
        )?;
        if self.max_assignments_per_person < 1 {
            return Err(SchemaValidationError::validation(
                "OptimizationTaskV5.max_assignments_per_person must be >= 1",
            ));
        }
        if !(1..=10).contains(&self.candidates.len()) {
            return Err(SchemaValidationError::validation(
                "OptimizationTaskV5.candidates length must be in 1..=10",
            ));
        }
        for c in &self.candidates {
            c.validate()?;
        }
        if !has_all_unique(self.candidates.iter().map(|c| c.candidate_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "candidates must have unique candidate_id per OptimizationTaskV5",
            ));
        }
        match self.phase_kind {
            OptimizationPhaseKindV5::P1ZspRefactor => {
                if self
                    .candidates
                    .iter()
                    .any(|c| c.action_kind == OptActionKindV5::SelectReviewerAccount)
                {
                    return Err(SchemaValidationError::validation(
                        "P1_ZSP_REFACTOR forbids SELECT_REVIEWER_ACCOUNT candidates",
                    ));
                }
            }
            OptimizationPhaseKindV5::P2ReviewerAssignment => {
                if self
                    .candidates
                    .iter()
                    .any(|c| c.action_kind != OptActionKindV5::SelectReviewerAccount)
                {
                    return Err(SchemaValidationError::validation(
                        "P2_REVIEWER_ASSIGNMENT requires SELECT_REVIEWER_ACCOUNT candidates",
                    ));
                }
            }
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "OptimizationTaskV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "OptimizationTaskV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// JML Workflow
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BirthrightRuleV5 {
    pub rule_id: String,
    pub dept_prefix: String,
    pub allowed_affiliations: Vec<AffiliationKindV5>,
    pub role_id: String,
    pub grant_mode: GrantModeV5,
    pub scope_path: String,
    pub evidence_id: String,
}

impl StrictV5Model for BirthrightRuleV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.rule_id, &BRULE_ID_RE, "rule_id", "BirthrightRuleV5")?;
        if self.allowed_affiliations.is_empty() {
            return Err(SchemaValidationError::validation(
                "BirthrightRuleV5.allowed_affiliations must have length >= 1",
            ));
        }
        check_re(&self.role_id, &ROLE_ID_RE, "role_id", "BirthrightRuleV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "BirthrightRuleV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JMLTraceEventKindV5 {
    Joiner,
    NoShowCancel,
    Mover,
    Leaver,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JMLTraceEventV5 {
    pub event_id: String,
    pub event_kind: JMLTraceEventKindV5,
    pub affiliation: AffiliationKindV5,
    #[serde(default)]
    pub under_legal_hold: bool,
    #[serde(default)]
    pub owns_prod_nhi_or_agent: bool,
    #[serde(default = "default_true")]
    pub has_active_tokens: bool,
    #[serde(default)]
    pub spoke_transient_error: bool,
    #[serde(default)]
    pub target_is_disconnected_itsm: bool,
    pub evidence_id: String,
}

impl StrictV5Model for JMLTraceEventV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.event_id, &JEV_ID_RE, "event_id", "JMLTraceEventV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "JMLTraceEventV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JoinerStepV5 {
    VerifyHrAuthAndEffectiveDate,
    CorrelateImmutableIdOrCreateAccount,
    ProvisionBirthrightAccess,
    BindTelemetryAndMfaPolicy,
    EmitJoinerAuditReceipt,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MoverStepV5 {
    SnapshotPriorGrantsAndBridges,
    RevokeOutOfScopeNonBridgedGrants,
    ProvisionNewDeptBirthrights,
    EnforcePostMoverSodAndRebindOwnership,
    TriggerTargetedMoverCertification,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LeaverStepV5 {
    DisableIdpAndSsoSessions,
    RevokeRefreshAndOboTrees,
    TransferOwnedNhiAndAgentCustody,
    DeprovisionSpokeEntitlementsOrDispatchItsm,
    ApplyMailboxArchiveOrLegalHoldRetention,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JoinerContractorSponsorPolicyV5 {
    RequireActiveFteSponsorPreProvision,
    AllowInternalConversionCarryoverSponsor,
    SkipSponsorVerification,
}

fn default_joiner_sponsor_policy() -> JoinerContractorSponsorPolicyV5 {
    JoinerContractorSponsorPolicyV5::RequireActiveFteSponsorPreProvision
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum NoShowDispositionV5 {
    FullRevokeAndDisableNeverStarted,
    LeaveDormant,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LegalHoldDispositionV5 {
    SuspendAuthRevokeEntitlementsPreserveHoldData,
    SuspendAuthIsolateForensicSnapshot,
    HardPurgeAllRecords,
    KeepAccountActiveForCounsel,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrphanHandoffPolicyV5 {
    ReassignToActiveManagerOrEscrow,
    ImmediateSuspendAllOwnedEntities,
    DropUnassigned,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SpokeErrorPolicyV5 {
    FailClosedRetryQueueAndAlert,
    IgnoreAndMarkComplete,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DisconnectedTargetPolicyV5 {
    DispatchItsmAndVerifyPostCloseAggregation,
    NotApplicableAutomatedOnly,
    TrustTicketClosedStatusImmediately,
    SkipDisconnectedSpokes,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JMLWorkflowSpecV5 {
    pub joiner_steps: Vec<JoinerStepV5>,
    pub mover_steps: Vec<MoverStepV5>,
    pub leaver_steps: Vec<LeaverStepV5>,
    #[serde(default = "default_joiner_sponsor_policy")]
    pub joiner_contractor_sponsor_policy: JoinerContractorSponsorPolicyV5,
    pub no_show_disposition: NoShowDispositionV5,
    pub legal_hold_disposition: LegalHoldDispositionV5,
    pub orphan_handoff_policy: OrphanHandoffPolicyV5,
    pub spoke_error_policy: SpokeErrorPolicyV5,
    pub disconnected_target_policy: DisconnectedTargetPolicyV5,
}

impl StrictV5Model for JMLWorkflowSpecV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        let j_unique: std::collections::BTreeSet<_> = self.joiner_steps.iter().collect();
        let m_unique: std::collections::BTreeSet<_> = self.mover_steps.iter().collect();
        let l_unique: std::collections::BTreeSet<_> = self.leaver_steps.iter().collect();
        if self.joiner_steps.len() != 5
            || self.mover_steps.len() != 5
            || self.leaver_steps.len() != 5
            || j_unique.len() != 5
            || m_unique.len() != 5
            || l_unique.len() != 5
        {
            return Err(SchemaValidationError::validation(
                "joiner_steps, mover_steps, and leaver_steps must each contain all 5 distinct steps",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LegalHoldScopeModeV5 {
    SuspendPreserveArchive,
    IsolateForensicSnapshot,
}

fn default_legal_hold_scope_mode() -> LegalHoldScopeModeV5 {
    LegalHoldScopeModeV5::SuspendPreserveArchive
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrphanCustodyModeV5 {
    HandoffToManagerElseSuspend,
    ImmediateSuspendAll,
}

fn default_orphan_custody_mode() -> OrphanCustodyModeV5 {
    OrphanCustodyModeV5::HandoffToManagerElseSuspend
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ContractorSponsorCheckModeV5 {
    StrictPreProvision,
    ExemptInternalConversion,
}

fn default_contractor_sponsor_check_mode() -> ContractorSponsorCheckModeV5 {
    ContractorSponsorCheckModeV5::StrictPreProvision
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JMLWorkflowTaskV5 {
    pub workflow_task_id: String,
    #[serde(default = "default_legal_hold_scope_mode")]
    pub legal_hold_scope_mode: LegalHoldScopeModeV5,
    #[serde(default = "default_orphan_custody_mode")]
    pub orphan_custody_mode: OrphanCustodyModeV5,
    #[serde(default = "default_true")]
    pub has_disconnected_spokes: bool,
    #[serde(default = "default_contractor_sponsor_check_mode")]
    pub contractor_sponsor_check_mode: ContractorSponsorCheckModeV5,
    pub birthright_catalog: Vec<BirthrightRuleV5>,
    pub visible_trace_events: Vec<JMLTraceEventV5>,
    pub draft_spec: JMLWorkflowSpecV5,
    #[serde(default = "default_one")]
    pub workflow_gen: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for JMLWorkflowTaskV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.workflow_task_id,
            &WFL_ID_RE,
            "workflow_task_id",
            "JMLWorkflowTaskV5",
        )?;
        if self.birthright_catalog.is_empty() {
            return Err(SchemaValidationError::validation(
                "JMLWorkflowTaskV5.birthright_catalog must have length >= 1",
            ));
        }
        for b in &self.birthright_catalog {
            b.validate()?;
        }
        if !has_all_unique(self.birthright_catalog.iter().map(|b| b.rule_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "birthright_catalog must have unique rule_id per JMLWorkflowTaskV5",
            ));
        }
        if self.visible_trace_events.len() < 4 {
            return Err(SchemaValidationError::validation(
                "JMLWorkflowTaskV5.visible_trace_events must have length >= 4",
            ));
        }
        for e in &self.visible_trace_events {
            e.validate()?;
        }
        if !has_all_unique(
            self.visible_trace_events
                .iter()
                .map(|e| e.event_id.as_str()),
        ) {
            return Err(SchemaValidationError::validation(
                "visible_trace_events must have unique event_id per JMLWorkflowTaskV5",
            ));
        }
        self.draft_spec.validate()?;
        if self.workflow_gen < 1 {
            return Err(SchemaValidationError::validation(
                "JMLWorkflowTaskV5.workflow_gen must be >= 1",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "JMLWorkflowTaskV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "JMLWorkflowTaskV5",
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Phase-2 Entities: CAE, UAR, Tickets, Sessions, JIT, Agent Delegation
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CAETokenKindV5 {
    Refresh,
    Access,
    OboAgent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CAETokenBindingKindV5 {
    BoundDpop,
    BearerUnbound,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CAETokenV5 {
    pub token_id: String,
    pub account_id: String,
    pub token_kind: CAETokenKindV5,
    #[serde(default)]
    pub parent_token_id: Option<String>,
    pub binding_kind: CAETokenBindingKindV5,
    pub cae_capable_spoke: bool,
    pub issued_at_s: i64,
    pub token_exp_s: i64,
    #[serde(default)]
    pub revoke_ack_at_s: Option<i64>,
    pub next_spoke_poll_sync_at_s: i64,
    #[serde(default)]
    pub agent_id: Option<String>,
    pub evidence_id: String,
}

impl StrictV5Model for CAETokenV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.token_id, &TOK_ID_RE, "token_id", "CAETokenV5")?;
        check_re(&self.account_id, &ACCT_ID_RE, "account_id", "CAETokenV5")?;
        check_opt_re(
            self.parent_token_id.as_deref(),
            &TOK_ID_RE,
            "parent_token_id",
            "CAETokenV5",
        )?;
        check_opt_re(self.agent_id.as_deref(), &AGT_ID_RE, "agent_id", "CAETokenV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "CAETokenV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CAETriggerKindV5 {
    HrTerminationEvent,
    CredentialCompromise,
    BenignSessionRenewal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CAEIncidentV5 {
    pub incident_id: String,
    pub person_id: String,
    pub trigger_kind: CAETriggerKindV5,
    pub event_ts_s: i64,
    pub tokens: Vec<CAETokenV5>,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for CAEIncidentV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.incident_id, &INC_ID_RE, "incident_id", "CAEIncidentV5")?;
        check_re(&self.person_id, &PERS_ID_RE, "person_id", "CAEIncidentV5")?;
        if self.tokens.is_empty() {
            return Err(SchemaValidationError::validation(
                "CAEIncidentV5.tokens must have length >= 1",
            ));
        }
        for t in &self.tokens {
            t.validate()?;
        }
        if !has_all_unique(self.tokens.iter().map(|t| t.token_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "tokens must have unique token_id per CAEIncidentV5",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "CAEIncidentV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "CAEIncidentV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UARItemV5 {
    pub item_id: String,
    pub account_id: String,
    pub grant_id: String,
    pub assigned_reviewer_account_id: String,
    pub used_within_lookback: bool,
    pub evidence_id: String,
}

impl StrictV5Model for UARItemV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.item_id, &ITEM_ID_RE, "item_id", "UARItemV5")?;
        check_re(&self.account_id, &ACCT_ID_RE, "account_id", "UARItemV5")?;
        check_re(&self.grant_id, &GRANT_ID_RE, "grant_id", "UARItemV5")?;
        check_re(
            &self.assigned_reviewer_account_id,
            &ACCT_ID_RE,
            "assigned_reviewer_account_id",
            "UARItemV5",
        )?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "UARItemV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UARPreserveStandingFloorOverrideV5 {
    UseOverlayDefault,
    True,
    False,
}

fn default_uar_override() -> UARPreserveStandingFloorOverrideV5 {
    UARPreserveStandingFloorOverrideV5::UseOverlayDefault
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UARCampaignV5 {
    pub campaign_id: String,
    pub app_id: String,
    pub scope_path: String,
    pub admin_role_id: String,
    pub m_standing: i64,
    pub m_eligible: i64,
    #[serde(default = "default_uar_override")]
    pub preserve_standing_floor_override: UARPreserveStandingFloorOverrideV5,
    #[serde(default)]
    pub backup_reviewer_account_ids: Vec<String>,
    #[serde(default)]
    pub candidate_custodian_account_ids: Vec<String>,
    pub items: Vec<UARItemV5>,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for UARCampaignV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.campaign_id, &CAMP_ID_RE, "campaign_id", "UARCampaignV5")?;
        check_re(
            &self.admin_role_id,
            &ROLE_ID_RE,
            "admin_role_id",
            "UARCampaignV5",
        )?;
        if self.m_standing < 0 {
            return Err(SchemaValidationError::validation(
                "UARCampaignV5.m_standing must be >= 0",
            ));
        }
        if self.m_eligible < 0 {
            return Err(SchemaValidationError::validation(
                "UARCampaignV5.m_eligible must be >= 0",
            ));
        }
        check_slice_re(
            &self.backup_reviewer_account_ids,
            &ACCT_ID_RE,
            "backup_reviewer_account_ids",
            "UARCampaignV5",
        )?;
        check_slice_re(
            &self.candidate_custodian_account_ids,
            &ACCT_ID_RE,
            "candidate_custodian_account_ids",
            "UARCampaignV5",
        )?;
        if self.items.is_empty() {
            return Err(SchemaValidationError::validation(
                "UARCampaignV5.items must have length >= 1",
            ));
        }
        for it in &self.items {
            it.validate()?;
        }
        if !has_all_unique(self.items.iter().map(|it| it.item_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "items must have unique item_id per UARCampaignV5",
            ));
        }
        if !has_all_unique(self.items.iter().map(|it| it.grant_id.as_str())) {
            return Err(SchemaValidationError::validation(
                "items must have unique grant_id per UARCampaignV5",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "UARCampaignV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "UARCampaignV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ExternalTicketSystemV5 {
    ServicenowRitm,
    JiraJsm,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ExternalTicketStatusV5 {
    ApprovedExternal,
    OpenUnapproved,
    Cancelled,
    ClosedConsumed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChangeTicketV5 {
    pub ticket_id: String,
    pub external_system: ExternalTicketSystemV5,
    pub external_ticket_status: ExternalTicketStatusV5,
    pub bound_requester_account_id: String,
    pub approved_role_id: String,
    pub approved_scope_path: String,
    pub target_resource: String,
    #[serde(default = "default_true")]
    pub single_use: bool,
    pub approved_by_account_id: String,
    pub approved_at_s: i64,
    pub expires_at_s: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for ChangeTicketV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.ticket_id, &TKT_ID_RE, "ticket_id", "ChangeTicketV5")?;
        check_re(
            &self.bound_requester_account_id,
            &ACCT_ID_RE,
            "bound_requester_account_id",
            "ChangeTicketV5",
        )?;
        check_re(
            &self.approved_role_id,
            &ROLE_ID_RE,
            "approved_role_id",
            "ChangeTicketV5",
        )?;
        check_re(
            &self.approved_by_account_id,
            &ACCT_ID_RE,
            "approved_by_account_id",
            "ChangeTicketV5",
        )?;
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "ChangeTicketV5")?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "ChangeTicketV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum HistoricalSessionKindV5 {
    PersonalJit,
    SharedBreakGlass,
    AgentObo,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoricalSessionV5 {
    pub session_id: String,
    pub account_id: String,
    pub role_id: String,
    pub scope_path: String,
    pub session_kind: HistoricalSessionKindV5,
    pub start_s: i64,
    pub scheduled_end_s: i64,
    #[serde(default)]
    pub revoked_at_s: Option<i64>,
    pub audit_events_count: i64,
    #[serde(default)]
    pub last_audit_event_s: Option<i64>,
    pub evidence_id: String,
}

impl StrictV5Model for HistoricalSessionV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.session_id,
            &SESS_ID_RE,
            "session_id",
            "HistoricalSessionV5",
        )?;
        check_re(
            &self.account_id,
            &ACCT_ID_RE,
            "account_id",
            "HistoricalSessionV5",
        )?;
        check_re(&self.role_id, &ROLE_ID_RE, "role_id", "HistoricalSessionV5")?;
        if self.audit_events_count < 0 {
            return Err(SchemaValidationError::validation(
                "HistoricalSessionV5.audit_events_count must be >= 0",
            ));
        }
        if self.start_s >= self.scheduled_end_s {
            return Err(SchemaValidationError::validation(
                "start_s must be strictly less than scheduled_end_s",
            ));
        }
        if (self.audit_events_count > 0) != self.last_audit_event_s.is_some() {
            return Err(SchemaValidationError::validation(
                "audit_events_count > 0 iff last_audit_event_s is not None",
            ));
        }
        if let Some(last) = self.last_audit_event_s
            && last < self.start_s
        {
            return Err(SchemaValidationError::validation(
                "last_audit_event_s must be >= start_s",
            ));
        }
        if let Some(rev) = self.revoked_at_s
            && rev < self.start_s
        {
            return Err(SchemaValidationError::validation(
                "revoked_at_s must be >= start_s",
            ));
        }
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "HistoricalSessionV5",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JITRequestChannelV5 {
    StandardTicket,
    EmergencyBreakGlass,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JITRequestV5 {
    pub request_id: String,
    pub eval_seq: i64,
    pub requester_account_id: String,
    pub requested_role_id: String,
    pub requested_scope_path: String,
    pub target_resource: String,
    pub request_channel: JITRequestChannelV5,
    #[serde(default)]
    pub change_ticket_id: Option<String>,
    pub proposed_approver_account_id: String,
    pub requested_ttl_s: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for JITRequestV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(&self.request_id, &REQ_ID_RE, "request_id", "JITRequestV5")?;
        if self.eval_seq < 1 {
            return Err(SchemaValidationError::validation(
                "JITRequestV5.eval_seq must be >= 1",
            ));
        }
        check_re(
            &self.requester_account_id,
            &ACCT_ID_RE,
            "requester_account_id",
            "JITRequestV5",
        )?;
        check_re(
            &self.requested_role_id,
            &ROLE_ID_RE,
            "requested_role_id",
            "JITRequestV5",
        )?;
        check_opt_re(
            self.change_ticket_id.as_deref(),
            &TKT_ID_RE,
            "change_ticket_id",
            "JITRequestV5",
        )?;
        check_re(
            &self.proposed_approver_account_id,
            &ACCT_ID_RE,
            "proposed_approver_account_id",
            "JITRequestV5",
        )?;
        if self.requested_ttl_s < 60 {
            return Err(SchemaValidationError::validation(
                "JITRequestV5.requested_ttl_s must be >= 60",
            ));
        }
        check_re(&self.etag, &ETAG_HEX_RE, "etag", "JITRequestV5")?;
        check_re(&self.evidence_id, &EV_ID_RE, "evidence_id", "JITRequestV5")?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentDelegationRequestV5 {
    pub request_id: String,
    pub eval_seq: i64,
    pub workflow_id: String,
    pub agent_id: String,
    pub delegator_account_id: String,
    #[serde(default)]
    pub parent_delegation_request_id: Option<String>,
    pub delegation_hop: i64,
    pub requested_role_id: String,
    pub requested_scope_path: String,
    pub target_resource: String,
    #[serde(default)]
    pub proposed_hitl_approver_account_id: Option<String>,
    pub requested_ttl_s: i64,
    pub etag: String,
    pub evidence_id: String,
}

impl StrictV5Model for AgentDelegationRequestV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.request_id,
            &DLREQ_ID_RE,
            "request_id",
            "AgentDelegationRequestV5",
        )?;
        if self.eval_seq < 1 {
            return Err(SchemaValidationError::validation(
                "AgentDelegationRequestV5.eval_seq must be >= 1",
            ));
        }
        check_re(
            &self.agent_id,
            &AGT_ID_RE,
            "agent_id",
            "AgentDelegationRequestV5",
        )?;
        check_re(
            &self.delegator_account_id,
            &ACCT_ID_RE,
            "delegator_account_id",
            "AgentDelegationRequestV5",
        )?;
        check_opt_re(
            self.parent_delegation_request_id.as_deref(),
            &DLREQ_ID_RE,
            "parent_delegation_request_id",
            "AgentDelegationRequestV5",
        )?;
        if self.delegation_hop < 1 {
            return Err(SchemaValidationError::validation(
                "AgentDelegationRequestV5.delegation_hop must be >= 1",
            ));
        }
        check_re(
            &self.requested_role_id,
            &ROLE_ID_RE,
            "requested_role_id",
            "AgentDelegationRequestV5",
        )?;
        check_opt_re(
            self.proposed_hitl_approver_account_id.as_deref(),
            &ACCT_ID_RE,
            "proposed_hitl_approver_account_id",
            "AgentDelegationRequestV5",
        )?;
        if self.requested_ttl_s < 60 {
            return Err(SchemaValidationError::validation(
                "AgentDelegationRequestV5.requested_ttl_s must be >= 60",
            ));
        }
        check_re(
            &self.etag,
            &ETAG_HEX_RE,
            "etag",
            "AgentDelegationRequestV5",
        )?;
        check_re(
            &self.evidence_id,
            &EV_ID_RE,
            "evidence_id",
            "AgentDelegationRequestV5",
        )?;
        Ok(())
    }
}
