//! Public blueprint, client, group, and Appendix C side-table types.

use ib_v52_schemas::{
    AffiliationTypeV52, BrownfieldGroupRecordV52, Day2EventV52, EnterpriseBrownfieldWorldV52,
    GroupDispositionV52, SpokeEnvKindV52,
};
use indexmap::IndexMap;

use crate::archetypes::FamilyRegimeSpecV52;
use crate::cells::V52Cell;

/// Client-specific randomized naming, domain, lookup tables, and target API quirks.
#[derive(Debug, Clone)]
pub struct ClientParametersV52 {
    pub company_name: String,
    pub company_short: String,
    pub primary_domain: String,
    pub contractor_domain: String,
    pub vendor_domain: String,
    pub student_domain: String,
    pub acquired_domain: String,
    pub ad_domain_dn: String,
    pub cutover_date: String,
    pub cutover_epoch_s: i64,
    pub badge_prefix: String,
    pub cost_center_prefix: String,
    pub fallback_governance_mailbox: String,
    pub security_legal_queue: String,
    pub fallback_vms_pmo_queue: String,
    pub itsm_leaver_tasks: Vec<String>,
    pub webhook_endpoint_id: String,
    pub webhook_hmac_header: String,
    pub sap_cua_master_sid: String,
    pub ad_max_page_size: usize,
    pub ad_cursor_param: String,
    pub entra_max_page_size: usize,
    pub sap_max_page_size: usize,
    pub sap_offset_param: String,
    pub aws_max_page_size: usize,
    pub custom_rest_cursor_param: String,
    pub custom_rest_cursor_response: String,
    pub custom_rest_page_size: usize,
    pub custom_rest_injects_429: bool,
    pub custom_rest_injects_207: bool,
    pub custom_rest_account_id_field: String,
    pub custom_rest_status_field: String,
    pub custom_rest_entitlements_field: String,
    pub custom_rest_verb_prefix: String,
    pub custom_rest_disconnected_prefixes: Vec<String>,
    pub custom_rest_itsm_queue: String,
    pub dept_lookup: IndexMap<String, String>,
    pub office_lookup: IndexMap<String, String>,
    pub cost_center_lookup: IndexMap<String, String>,
    pub hr_status_lookup: IndexMap<String, String>,
    pub leave_type_lookup: IndexMap<String, String>,
    pub workday_aff_lookup: IndexMap<String, String>,
    pub secondary_aff_lookup: IndexMap<String, String>,
    pub vms_aff_lookup: IndexMap<String, String>,
    pub ad_active_ou_by_affiliation: IndexMap<AffiliationTypeV52, String>,
    pub ad_disabled_ou: String,
}

/// Group metadata linking the world's `BrownfieldGroupRecordV52` to its authoritative gold classification.
#[derive(Debug, Clone)]
pub struct GroupBlueprintV52 {
    pub record: BrownfieldGroupRecordV52,
    pub disposition: GroupDispositionV52,
    pub environment: SpokeEnvKindV52,
    pub role_tag: GroupRoleTagV52,
}

/// Semantic tag used to assemble clean birthright/requestable roles and SoD policies.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GroupRoleTagV52 {
    AllEmployeeBase,
    AllEmployeeBaseEntra,
    FinanceBirthright,
    FinanceBirthrightSpoke,
    EngineeringBirthright,
    EngineeringBirthrightSpoke,
    ItBirthright,
    ItBirthrightSpoke,
    ClinicalBirthright,
    AcademicBirthright,
    LegalBirthright,
    StudentBase,
    ContingentBase,
    SodApPostLeft,
    SodVendorMaintRight,
    SodDeployApproveLeft,
    SodCodeCommitRight,
    RequestableAppCleanA,
    RequestableAppCleanB,
    RequestableAppCleanC,
    NearThresholdDecoyAllEmp,
    NearThresholdDecoyFinance,
    NearThresholdDecoyEngineering,
    NearThresholdDecoyIt,
    NearThresholdDecoyClinical,
    NearThresholdDecoyAcademic,
    NearThresholdDecoyLegal,
    NonProdDevNoise,
    PrivilegedAdmin,
    Orphaned,
    ServiceAccountOnly,
    NestedEngSquad,
    NestedEngPlatform,
    NestedFinSquad,
    NestedFinTeam,
    UarCoreVaultTarget,
    UarSharedCollab,
    UarAdmBridge,
    UarSplitChildTarget,
    UarDeptBundleLocked,
    UarExcTemp,
    UarExcValid,
    UarPrivAudit,
    NestedAdmSodDeployLeft,
    /// `F06-CR`: PROD group of a CMDB-`RETIRED` application whose memberships were never cleaned up
    /// (held by every active Finance FTE, so naive `>= 80%` mining bundles it).
    RetiredAppPopulated,
    /// A live Finance group whose nesting still reaches the retired app.
    RetiredAppBridge,
}

/// `F06-CR`: application ID of the CMDB-`RETIRED` application behind [`GroupRoleTagV52::RetiredAppPopulated`].
pub const F06_RETIRED_APP_ID_V52: &str = "app_fin_expense_classic";

/// Effective-dated governance ledger entry rendered inside `sources/nhi_and_agents.json` on `F05`/`F12`.
#[derive(Debug, Clone)]
pub struct GovernanceLedgerEntryV52 {
    pub event_id: String,
    pub entity_id: String,
    pub event_type: String,
    pub approval_status: String,
    pub effective_timestamp: String,
    pub payload: IndexMap<String, String>,
}

/// Out-of-order OBO actor hop receipt rendered inside `sources/nhi_and_agents.json` on `F10`/`F12`.
#[derive(Debug, Clone)]
pub struct OboHopReceiptV52 {
    pub request_id: String,
    pub hop_index: u32,
    pub agent_id: String,
    pub attenuated_scopes: Vec<String>,
}

/// Prior session action audit entry rendered inside `sources/nhi_and_agents.json` on `F10`/`F12`.
#[derive(Debug, Clone)]
pub struct OboSessionPriorActionV52 {
    pub session_trace_id: String,
    pub executed_scope: String,
}

/// Effective-dated PTO delegation register entry rendered inside `sources/nhi_and_agents.json` on `F10`/`F12`.
#[derive(Debug, Clone)]
pub struct PtoDelegationEntryV52 {
    pub delegator_person_key: String,
    pub delegate_person_key: String,
    pub valid_from: String,
    pub valid_until: String,
}

/// Signed cloud workload deployment revision rendered inside `sources/cloud_iam_graph.json` on `F03`/`F12`.
#[derive(Debug, Clone)]
pub struct CloudWorkloadDeploymentRevisionV52 {
    pub revision_id: String,
    pub principal_id: String,
    pub status: String,
    pub effective_date: String,
    pub capability_codes: Vec<String>,
}

/// Cloud principal 30-day activity telemetry rendered inside `sources/cloud_iam_graph.json` on `F03`/`F12`.
#[derive(Debug, Clone)]
pub struct CloudActivityTelemetryV52 {
    pub principal_id: String,
    pub days_since_last_authenticated: u32,
    pub observed_30d_permissions: Vec<String>,
}

/// Remediation ticket history entry rendered inside `sources/uar_campaigns.json` on `F08`/`F10`/`F11`.
#[derive(Debug, Clone)]
pub struct UarRemediationTicketV52 {
    pub ticket_id: String,
    pub item_id: String,
    pub spoke_id: String,
    pub account_id: String,
    pub entitlement_id: String,
    pub status: String,
    pub closed_at: Option<String>,
}

/// Spoke aggregation watermark rendered inside `sources/uar_campaigns.json` on `F08`/`F10`/`F11`.
#[derive(Debug, Clone)]
pub struct SpokeReadbackWatermarkV52 {
    pub spoke_id: String,
    pub last_complete_aggregation_at: String,
}

/// Signed `(department, job_code)` entitlement matrix row rendered inside `sources/uar_campaigns.json` on `F08`/`F10`/`F11`.
#[derive(Debug, Clone)]
pub struct SignedRoleBundleEntryV52 {
    pub department: String,
    pub job_code: String,
    pub required_shared_group_id: Option<String>,
    pub justified_entitlement_ids: Vec<String>,
}

/// Complete scenario blueprint produced from a [`V52Cell`].
#[derive(Debug, Clone)]
pub struct ScenarioBlueprintV52 {
    pub cell: V52Cell,
    pub spec: FamilyRegimeSpecV52,
    pub client: ClientParametersV52,
    pub active_spokes: Vec<String>,
    pub groups: Vec<GroupBlueprintV52>,
    pub world: EnterpriseBrownfieldWorldV52,
    pub sealed_day2_events: Vec<Day2EventV52>,
    pub stakeholder_names: StakeholderNamesV52,
    /// Person keys for key named test scenarios (used in tests and verification, NEVER leaked to workspace).
    pub key_persons: KeyPersonHandlesV52,
    /// Factual business justification notes rendered into `sources/uar_campaigns.json`.
    pub uar_review_notes: IndexMap<String, String>,
    /// Factual PTO-delegate relationship (`item_id` -> `subject_id` when assigned reviewer is PTO delegate).
    pub uar_assigned_reviewer_delegate_of: IndexMap<String, String>,
    /// Internal generator metadata: UAR items where the subject requires the colleague-locked
    /// shared department parent bundle for core job duties (never rendered into workspace files).
    pub uar_owner_split_item_ids: Vec<String>,
    /// Mechanism B (`F05`/`F12`): effective-dated governance ledger entries & pre-ledger baseline overrides.
    pub governance_event_ledger: Vec<GovernanceLedgerEntryV52>,
    pub nhi_baseline_owner_overrides: IndexMap<String, Option<String>>,
    pub agent_baseline_sponsor_overrides: IndexMap<String, Option<String>>,
    /// Mechanism C (`F10`/`F12`): relational OBO tables (replacing pre-joined `agent_delegation_requests`).
    pub obo_request_session_traces: IndexMap<String, String>,
    pub obo_hop_receipts: Vec<OboHopReceiptV52>,
    pub obo_session_prior_actions: Vec<OboSessionPriorActionV52>,
    pub pto_delegation_register: Vec<PtoDelegationEntryV52>,
    /// Mechanism D (`F03`/`F12`): multi-table cloud CIEM workload revisions, dictionary & 30d telemetry.
    pub cloud_principal_display_names: IndexMap<String, String>,
    pub cloud_deployment_revisions: Vec<CloudWorkloadDeploymentRevisionV52>,
    pub cloud_capability_dictionary: IndexMap<String, String>,
    pub cloud_activity_telemetry: Vec<CloudActivityTelemetryV52>,
    /// Mechanism E (`F08`/`F10`/`F11`): UAR remediation ticket log, spoke readback watermarks & role bundle matrix.
    pub uar_remediation_ticket_log: Vec<UarRemediationTicketV52>,
    pub spoke_readback_watermarks: Vec<SpokeReadbackWatermarkV52>,
    pub signed_role_bundle_matrix: Vec<SignedRoleBundleEntryV52>,
}

#[derive(Debug, Clone)]
pub struct StakeholderNamesV52 {
    pub hr_lead: String,
    pub ad_admin: String,
    pub app_owner: String,
    pub business_sponsor: String,
    pub ciso_lead: String,
}

#[derive(Debug, Clone)]
pub struct KeyPersonHandlesV52 {
    pub ceo_vp: String,
    pub active_dir_1: String,
    pub garden_leave_mgr: String,
    pub emerg_term_2hop: String,
    pub emerg_term_3hop: String,
    pub emerg_term_fallback: String,
    pub legal_hold_user: String,
    pub diacritic_newhire: String,
    pub longname_collision_newhire: String,
    pub tombstone_collision_newhire: String,
    pub legalhold_collision_newhire: String,
    pub contingent_day1_grace: String,
    pub contingent_day2_exhausted: String,
    pub furlough_user: String,
    pub student_worker_ended: String,
    pub finance_sod_requester: String,
    pub eng_sod_requester: String,
    pub contractor_priv_requester: String,
    pub b2b_guest_1: String,
    pub b2b_guest_2: String,
    pub adm_eng_sod_user: String,
    pub adm_fin_uar_dual: String,
    pub uar_exp_exc_user: String,
}
