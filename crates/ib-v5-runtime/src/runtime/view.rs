//! Delivered-view projection, version tokens, completeness, and Stage-2 structural checks.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use ib_v5_schemas::{
    AgentDelegationRequestV5, AtomicMutationV5, AuthorityTierV5, BaselineAgentV5, BaselineNHIV5,
    CAEIncidentV5, CIEMTaskV5, ChangeTicketV5, CompensatingControlV5, ConnectorPageV5,
    ConnectorSchemaV5, ConnectorSyncModeV5, ConnectorTransportKindV5, DeliveredSliceV5,
    EnterpriseAgentV5, GrantV5, GroupV5, HRPersonV5, HRSourceMappingTaskV5, HistoricalSessionV5,
    IdPAccountV5, JITRequestV5, JMLWorkflowTaskV5, OptimizationTaskV5, PhaseManifestV5,
    PhaseWorkQueueV5, PolicyCardV5, PriorPhaseLedgerV5, RoleV5, SchemaValidationError, TableRowV5,
    TargetNHIV5, TransferBridgeV5, UARCampaignV5, WorldStateV5, decode_compact_table_v5,
};
use indexmap::IndexMap;
use serde_json::{Value, json};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum RuntimeError {
    #[error("{0}")]
    InvalidArgument(String),
    #[error("Schema validation failed: {0}")]
    Schema(#[from] SchemaValidationError),
    #[error("JSON error: {0}")]
    Json(#[from] ib_core::JsonError),
    #[error("Diagnostic service error: {0}")]
    Diagnostic(#[from] crate::verifier::VerifierContextError),
}

fn authority_rank(tier: AuthorityTierV5) -> u8 {
    match tier {
        AuthorityTierV5::StaleCache => 1,
        AuthorityTierV5::SecondaryReplica => 2,
        AuthorityTierV5::AuthoritativePrimary => 3,
    }
}

#[derive(Debug, Clone, PartialEq)]
struct RowCandidate {
    table_name: String,
    primary_key: String,
    rank: (u8, i64, String),
    record: TableRowV5,
}

/// Order-independent projection of only the delivered compact-table rows.
#[derive(Debug, Clone, PartialEq)]
pub struct ViewV5 {
    pub manifest: PhaseManifestV5,
    pub policy_card: PolicyCardV5,
    pub world_rev: i64,
    pub invalidated_entity_ids: Vec<String>,
    phase_slices: Vec<DeliveredSliceV5>,
    inherited_slices: Vec<DeliveredSliceV5>,
    candidates: Vec<RowCandidate>,
    resolved: BTreeMap<(String, String), TableRowV5>,
}

/// Private derived data emitted only in execution snapshots for Python artifact parity.
///
/// These values are not part of the policy-visible view projection.
#[derive(Debug, Clone, PartialEq)]
pub struct ViewSnapshotSerializationV5 {
    pub candidates: Value,
    pub resolved: Value,
    pub phase_slice_payloads: Value,
    pub inherited_slice_payloads: Value,
}

impl ViewV5 {
    /// Construct a delivered-only [`ViewV5`] from slices and an optional inherited Phase-1 view.
    pub fn from_slices(
        manifest: &PhaseManifestV5,
        delivered_slices: &[DeliveredSliceV5],
        policy_card: &PolicyCardV5,
        world_rev: i64,
        base_view: Option<&ViewV5>,
        invalidated_entity_ids: &[String],
    ) -> Result<Self, RuntimeError> {
        let invalidated: Vec<String> = invalidated_entity_ids
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let invalidated_set: BTreeSet<&str> = invalidated.iter().map(String::as_str).collect();

        let mut candidates: Vec<RowCandidate> = Vec::new();
        let mut inherited: Vec<DeliveredSliceV5> = Vec::new();
        if let Some(base) = base_view {
            inherited.extend(base.inherited_slices.iter().cloned());
            inherited.extend(base.phase_slices.iter().cloned());
            for cand in &base.candidates {
                if !(cand.table_name == "hr_persons"
                    && invalidated_set.contains(cand.primary_key.as_str()))
                {
                    candidates.push(cand.clone());
                }
            }
        }

        let current_slices: Vec<DeliveredSliceV5> = delivered_slices.to_vec();
        for delivered in &current_slices {
            let rank = (
                authority_rank(delivered.authority_tier),
                delivered.watermark_s,
                delivered.slice_id.clone(),
            );
            for table in &delivered.tables {
                for record in decode_compact_table_v5(table)? {
                    let key = record.primary_key().to_owned();
                    candidates.push(RowCandidate {
                        table_name: table.table_name.clone(),
                        primary_key: key,
                        rank: rank.clone(),
                        record,
                    });
                }
            }
        }

        let mut winners: BTreeMap<(String, String), RowCandidate> = BTreeMap::new();
        for candidate in &candidates {
            let key = (candidate.table_name.clone(), candidate.primary_key.clone());
            match winners.get(&key) {
                Some(prior) if candidate.rank <= prior.rank => {}
                _ => {
                    winners.insert(key, candidate.clone());
                }
            }
        }

        let resolved: BTreeMap<(String, String), TableRowV5> =
            winners.into_iter().map(|(k, c)| (k, c.record)).collect();

        Ok(Self {
            manifest: manifest.clone(),
            policy_card: policy_card.clone(),
            world_rev,
            invalidated_entity_ids: invalidated,
            phase_slices: current_slices,
            inherited_slices: inherited,
            candidates,
            resolved,
        })
    }

    #[must_use]
    pub fn phase_index(&self) -> i64 {
        self.manifest.phase_index
    }

    #[must_use]
    pub fn phase_delivered_slices(&self) -> &[DeliveredSliceV5] {
        &self.phase_slices
    }

    #[must_use]
    pub fn inherited_delivered_slices(&self) -> &[DeliveredSliceV5] {
        &self.inherited_slices
    }

    /// Serialize private runtime snapshot fields without adding them to the policy-visible view.
    ///
    /// The Python artifact serializer includes these dataclass fields: candidate and resolved
    /// records preserve their runtime order, and the slice payloads are Python `repr(bytes)`
    /// strings generated from compact, UTF-8 JSON payloads.
    pub fn snapshot_serialization_v5(&self) -> Result<ViewSnapshotSerializationV5, RuntimeError> {
        let candidates = self
            .candidates
            .iter()
            .map(|candidate| {
                Ok(json!({
                    "table_name": candidate.table_name,
                    "primary_key": candidate.primary_key,
                    "rank": [candidate.rank.0, candidate.rank.1, candidate.rank.2],
                    "record": candidate.record.to_strict_json_value()?,
                }))
            })
            .collect::<Result<Vec<_>, RuntimeError>>()?;
        let resolved = self
            .resolved
            .iter()
            .map(|((table_name, primary_key), record)| {
                Ok(json!([
                    table_name,
                    primary_key,
                    record.to_strict_json_value()?,
                ]))
            })
            .collect::<Result<Vec<_>, RuntimeError>>()?;
        let phase_slice_payloads = self
            .phase_slices
            .iter()
            .map(slice_payload_python_repr)
            .collect::<Result<Vec<_>, RuntimeError>>()?;
        let inherited_slice_payloads = self
            .inherited_slices
            .iter()
            .map(slice_payload_python_repr)
            .collect::<Result<Vec<_>, RuntimeError>>()?;

        Ok(ViewSnapshotSerializationV5 {
            candidates: Value::Array(candidates),
            resolved: Value::Array(resolved),
            phase_slice_payloads: Value::Array(
                phase_slice_payloads
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            ),
            inherited_slice_payloads: Value::Array(
                inherited_slice_payloads
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            ),
        })
    }

    #[must_use]
    pub fn prior_phase_ledger(&self) -> Option<&PriorPhaseLedgerV5> {
        self.manifest.prior_phase_ledger.as_ref()
    }

    #[must_use]
    pub fn all_delivered_slices(&self) -> Vec<DeliveredSliceV5> {
        let mut all = Vec::with_capacity(self.inherited_slices.len() + self.phase_slices.len());
        all.extend(self.inherited_slices.iter().cloned());
        all.extend(self.phase_slices.iter().cloned());
        all
    }

    #[must_use]
    pub fn resolve(&self, table_name: &str, primary_key: &str) -> Option<&TableRowV5> {
        self.resolved
            .get(&(table_name.to_owned(), primary_key.to_owned()))
    }

    #[must_use]
    pub fn has_row(&self, table_name: &str, primary_key: &str) -> bool {
        self.resolve(table_name, primary_key).is_some()
    }

    #[must_use]
    pub fn entity_ids(&self, table_name: &str) -> Vec<String> {
        self.resolved
            .keys()
            .filter(|(t, _)| t == table_name)
            .map(|(_, pk)| pk.clone())
            .collect()
    }

    #[must_use]
    pub fn table_rows(&self, table_name: &str) -> BTreeMap<String, TableRowV5> {
        self.resolved
            .iter()
            .filter(|((t, _), _)| t == table_name)
            .map(|((_, pk), row)| (pk.clone(), row.clone()))
            .collect()
    }

    #[must_use]
    pub fn resolve_hr_person(&self, pk: &str) -> Option<&HRPersonV5> {
        match self.resolve("hr_persons", pk) {
            Some(TableRowV5::HrPerson(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_role(&self, pk: &str) -> Option<&RoleV5> {
        match self.resolve("roles", pk) {
            Some(TableRowV5::Role(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_grant(&self, pk: &str) -> Option<&GrantV5> {
        match self.resolve("grants", pk) {
            Some(TableRowV5::Grant(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_group(&self, pk: &str) -> Option<&GroupV5> {
        match self.resolve("groups", pk) {
            Some(TableRowV5::Group(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_idp_account(&self, pk: &str) -> Option<&IdPAccountV5> {
        match self.resolve("idp_accounts", pk) {
            Some(TableRowV5::IdpAccount(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_transfer_bridge(&self, pk: &str) -> Option<&TransferBridgeV5> {
        match self.resolve("transfer_bridges", pk) {
            Some(TableRowV5::TransferBridge(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_compensating_control(&self, pk: &str) -> Option<&CompensatingControlV5> {
        match self.resolve("compensating_controls", pk) {
            Some(TableRowV5::CompensatingControl(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_connector_schema(&self, pk: &str) -> Option<&ConnectorSchemaV5> {
        match self.resolve("connector_schemas", pk) {
            Some(TableRowV5::ConnectorSchema(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_connector_page(&self, pk: &str) -> Option<&ConnectorPageV5> {
        match self.resolve("connector_pages", pk) {
            Some(TableRowV5::ConnectorPage(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_hr_source_mapping(&self, pk: &str) -> Option<&HRSourceMappingTaskV5> {
        match self.resolve("hr_source_mappings", pk) {
            Some(TableRowV5::HrSourceMapping(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_baseline_nhi(&self, pk: &str) -> Option<&BaselineNHIV5> {
        match self.resolve("baseline_nhis", pk) {
            Some(TableRowV5::BaselineNhi(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_target_nhi(&self, pk: &str) -> Option<&TargetNHIV5> {
        match self.resolve("target_nhis", pk) {
            Some(TableRowV5::TargetNhi(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_baseline_agent(&self, pk: &str) -> Option<&BaselineAgentV5> {
        match self.resolve("baseline_agents", pk) {
            Some(TableRowV5::BaselineAgent(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_enterprise_agent(&self, pk: &str) -> Option<&EnterpriseAgentV5> {
        match self.resolve("enterprise_agents", pk) {
            Some(TableRowV5::EnterpriseAgent(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_cloud_ciem_task(&self, pk: &str) -> Option<&CIEMTaskV5> {
        match self.resolve("cloud_ciem_tasks", pk) {
            Some(TableRowV5::CloudCiemTask(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_optimization_task(&self, pk: &str) -> Option<&OptimizationTaskV5> {
        match self.resolve("optimization_tasks", pk) {
            Some(TableRowV5::OptimizationTask(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_jml_workflow_task(&self, pk: &str) -> Option<&JMLWorkflowTaskV5> {
        match self.resolve("jml_workflow_tasks", pk) {
            Some(TableRowV5::JmlWorkflowTask(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_cae_incident(&self, pk: &str) -> Option<&CAEIncidentV5> {
        match self.resolve("cae_incidents", pk) {
            Some(TableRowV5::CaeIncident(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_uar_campaign(&self, pk: &str) -> Option<&UARCampaignV5> {
        match self.resolve("uar_campaigns", pk) {
            Some(TableRowV5::UarCampaign(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_change_ticket(&self, pk: &str) -> Option<&ChangeTicketV5> {
        match self.resolve("change_tickets", pk) {
            Some(TableRowV5::ChangeTicket(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_historical_session(&self, pk: &str) -> Option<&HistoricalSessionV5> {
        match self.resolve("historical_sessions", pk) {
            Some(TableRowV5::HistoricalSession(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_jit_request(&self, pk: &str) -> Option<&JITRequestV5> {
        match self.resolve("jit_requests", pk) {
            Some(TableRowV5::JitRequest(r)) => Some(r),
            _ => None,
        }
    }

    #[must_use]
    pub fn resolve_agent_delegation_request(&self, pk: &str) -> Option<&AgentDelegationRequestV5> {
        match self.resolve("agent_delegation_requests", pk) {
            Some(TableRowV5::AgentDelegationRequest(r)) => Some(r),
            _ => None,
        }
    }

    /// Build a sparse policy input from delivered rows only.
    #[must_use]
    pub fn to_policy_state(&self) -> WorldStateV5 {
        let mut hr_persons = IndexMap::new();
        let mut roles = IndexMap::new();
        let mut grants = IndexMap::new();
        let mut groups = IndexMap::new();
        let mut idp_accounts = IndexMap::new();
        let mut transfer_bridges = IndexMap::new();
        let mut compensating_controls = IndexMap::new();
        let mut connector_schemas = IndexMap::new();
        let mut connector_pages = Vec::new();
        let mut hr_source_mappings = IndexMap::new();
        let mut baseline_nhis = IndexMap::new();
        let mut target_nhis = IndexMap::new();
        let mut baseline_agents = IndexMap::new();
        let mut enterprise_agents = IndexMap::new();
        let mut cloud_ciem_tasks = IndexMap::new();
        let mut optimization_tasks = IndexMap::new();
        let mut jml_workflow_tasks = IndexMap::new();
        let mut cae_incidents = IndexMap::new();
        let mut uar_campaigns = IndexMap::new();
        let mut change_tickets = IndexMap::new();
        let mut historical_sessions = Vec::new();
        let mut jit_requests = IndexMap::new();
        let mut agent_delegation_requests = IndexMap::new();

        for ((_, pk), row) in &self.resolved {
            match row {
                TableRowV5::HrPerson(r) => {
                    hr_persons.insert(pk.clone(), r.clone());
                }
                TableRowV5::Role(r) => {
                    roles.insert(pk.clone(), r.clone());
                }
                TableRowV5::Grant(r) => {
                    grants.insert(pk.clone(), r.clone());
                }
                TableRowV5::Group(r) => {
                    groups.insert(pk.clone(), r.clone());
                }
                TableRowV5::IdpAccount(r) => {
                    idp_accounts.insert(pk.clone(), r.clone());
                }
                TableRowV5::TransferBridge(r) => {
                    transfer_bridges.insert(pk.clone(), r.clone());
                }
                TableRowV5::CompensatingControl(r) => {
                    compensating_controls.insert(pk.clone(), r.clone());
                }
                TableRowV5::ConnectorSchema(r) => {
                    connector_schemas.insert(pk.clone(), r.clone());
                }
                TableRowV5::ConnectorPage(r) => {
                    connector_pages.push(r.clone());
                }
                TableRowV5::HrSourceMapping(r) => {
                    hr_source_mappings.insert(pk.clone(), r.clone());
                }
                TableRowV5::BaselineNhi(r) => {
                    baseline_nhis.insert(pk.clone(), r.clone());
                }
                TableRowV5::TargetNhi(r) => {
                    target_nhis.insert(pk.clone(), r.clone());
                }
                TableRowV5::BaselineAgent(r) => {
                    baseline_agents.insert(pk.clone(), r.clone());
                }
                TableRowV5::EnterpriseAgent(r) => {
                    enterprise_agents.insert(pk.clone(), r.clone());
                }
                TableRowV5::CloudCiemTask(r) => {
                    cloud_ciem_tasks.insert(pk.clone(), r.clone());
                }
                TableRowV5::OptimizationTask(r) => {
                    optimization_tasks.insert(pk.clone(), r.clone());
                }
                TableRowV5::JmlWorkflowTask(r) => {
                    jml_workflow_tasks.insert(pk.clone(), r.clone());
                }
                TableRowV5::CaeIncident(r) => {
                    cae_incidents.insert(pk.clone(), r.clone());
                }
                TableRowV5::UarCampaign(r) => {
                    uar_campaigns.insert(pk.clone(), r.clone());
                }
                TableRowV5::ChangeTicket(r) => {
                    change_tickets.insert(pk.clone(), r.clone());
                }
                TableRowV5::HistoricalSession(r) => {
                    historical_sessions.push(r.clone());
                }
                TableRowV5::JitRequest(r) => {
                    jit_requests.insert(pk.clone(), r.clone());
                }
                TableRowV5::AgentDelegationRequest(r) => {
                    agent_delegation_requests.insert(pk.clone(), r.clone());
                }
            }
        }

        WorldStateV5 {
            phase_index: self.phase_index(),
            world_rev: self.world_rev,
            clock_epoch_s: self.manifest.clock_epoch_s,
            scope_catalog: self.policy_card.scope_catalog.clone(),
            overlay: self.policy_card.overlay.clone(),
            hr_persons,
            roles,
            grants,
            groups,
            idp_accounts,
            transfer_bridges,
            compensating_controls,
            connector_schemas,
            connector_pages,
            hr_source_mappings,
            baseline_nhis,
            target_nhis,
            baseline_agents,
            enterprise_agents,
            cloud_ciem_tasks,
            optimization_tasks,
            jml_workflow_tasks,
            cae_incidents,
            uar_campaigns,
            change_tickets,
            historical_sessions,
            jit_requests,
            agent_delegation_requests,
            prior_phase_ledger: self.manifest.prior_phase_ledger.clone(),
        }
    }
}

fn slice_payload_python_repr(slice: &DeliveredSliceV5) -> Result<String, RuntimeError> {
    let bytes = serde_json::to_vec(slice)
        .map_err(|_| RuntimeError::InvalidArgument("slice payload serialization failed".into()))?;
    Ok(python_bytes_repr(&bytes))
}

fn python_bytes_repr(bytes: &[u8]) -> String {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut repr = String::with_capacity(bytes.len() + 3);
    repr.push('b');
    repr.push(char::from(quote));
    for byte in bytes {
        match *byte {
            b'\\' => repr.push_str("\\\\"),
            b'\t' => repr.push_str("\\t"),
            b'\n' => repr.push_str("\\n"),
            b'\r' => repr.push_str("\\r"),
            b if b == quote => {
                repr.push('\\');
                repr.push(char::from(b));
            }
            0x20..=0x7e => repr.push(char::from(*byte)),
            other => repr.push_str(&format!("\\x{other:02x}")),
        }
    }
    repr.push(char::from(quote));
    repr
}

/// Public factory shared by runtime, fixture validation, and baselines.
pub fn build_view_v5(
    manifest: &PhaseManifestV5,
    delivered_slices: &[DeliveredSliceV5],
    policy_card: &PolicyCardV5,
    world_rev: i64,
    base_view: Option<&ViewV5>,
    invalidated_entity_ids: &[String],
) -> Result<ViewV5, RuntimeError> {
    ViewV5::from_slices(
        manifest,
        delivered_slices,
        policy_card,
        world_rev,
        base_view,
        invalidated_entity_ids,
    )
}

/// Return the total version token for an entity visible in `view`.
#[must_use]
pub fn live_version(entity_id: &str, view: &ViewV5) -> Option<String> {
    let (prefix, _) = entity_id.split_once('_')?;
    let ledger = view.prior_phase_ledger();
    match prefix {
        "bnhi" => view
            .resolve_baseline_nhi(entity_id)
            .map(|b| format!("{entity_id}@{}:{}", b.etag, b.baseline_gen)),
        "pers" => view
            .resolve_hr_person(entity_id)
            .map(|p| format!("{entity_id}@{}:{}", p.etag, p.lifecycle_gen)),
        "acct" => {
            let account = view.resolve_idp_account(entity_id)?;
            let outcome = ledger.and_then(|l| {
                l.account_outcomes
                    .iter()
                    .find(|o| o.account_id == entity_id)
            });
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", account.etag, account.account_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_account_gen),
            })
        }
        "nhi" => {
            let nhi = view.resolve_target_nhi(entity_id)?;
            let outcome =
                ledger.and_then(|l| l.nhi_outcomes.iter().find(|o| o.nhi_id == entity_id));
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", nhi.etag, nhi.nhi_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_nhi_gen),
            })
        }
        "agt" => {
            let agent = view.resolve_enterprise_agent(entity_id)?;
            let outcome =
                ledger.and_then(|l| l.agent_outcomes.iter().find(|o| o.agent_id == entity_id));
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", agent.etag, agent.agent_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_agent_gen),
            })
        }
        "conn" => {
            let conn = view.resolve_connector_schema(entity_id)?;
            let outcome = ledger.and_then(|l| {
                l.connector_outcomes
                    .iter()
                    .find(|o| o.connector_id == entity_id)
            });
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", conn.etag, conn.sync_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_sync_gen),
            })
        }
        "hrmap" => {
            let mapping = view.resolve_hr_source_mapping(entity_id)?;
            let outcome = ledger.and_then(|l| {
                l.hr_mapping_outcomes
                    .iter()
                    .find(|o| o.mapping_task_id == entity_id)
            });
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", mapping.etag, mapping.mapping_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_mapping_gen),
            })
        }
        "ciem" => {
            let task = view.resolve_cloud_ciem_task(entity_id)?;
            let outcome =
                ledger.and_then(|l| l.ciem_outcomes.iter().find(|o| o.ciem_task_id == entity_id));
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", task.etag, task.ciem_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_ciem_gen),
            })
        }
        "wfl" => {
            let task = view.resolve_jml_workflow_task(entity_id)?;
            let outcome = ledger.and_then(|l| {
                l.workflow_outcomes
                    .iter()
                    .find(|o| o.workflow_task_id == entity_id)
            });
            Some(match outcome {
                None => format!("{entity_id}@{}:{}", task.etag, task.workflow_gen),
                Some(o) => format!("{entity_id}@{}:{}", o.current_etag, o.current_workflow_gen),
            })
        }
        "opt" => view
            .resolve_optimization_task(entity_id)
            .map(|t| format!("{entity_id}@{}:1", t.etag)),
        "inc" => view
            .resolve_cae_incident(entity_id)
            .map(|i| format!("{entity_id}@{}:1", i.etag)),
        "camp" => view
            .resolve_uar_campaign(entity_id)
            .map(|c| format!("{entity_id}@{}:1", c.etag)),
        "req" => view
            .resolve_jit_request(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "dlreq" => view
            .resolve_agent_delegation_request(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "brg" => view
            .resolve_transfer_bridge(entity_id)
            .map(|b| format!("{entity_id}@{}:1", b.etag)),
        "cc" => view
            .resolve_compensating_control(entity_id)
            .map(|c| format!("{entity_id}@{}:1", c.etag)),
        "tkt" => view
            .resolve_change_ticket(entity_id)
            .map(|t| format!("{entity_id}@{}:1", t.etag)),
        _ => None,
    }
}

/// Extract all top-level and nested `evidence_id` values from a [`DeliveredSliceV5`].
#[must_use]
pub fn all_evidence_ids(slice: &DeliveredSliceV5) -> BTreeSet<String> {
    fn walk_value(val: &Value, found: &mut BTreeSet<String>) {
        match val {
            Value::Array(items) => {
                for item in items {
                    walk_value(item, found);
                }
            }
            Value::Object(map) => {
                for (k, child) in map {
                    if k == "evidence_id" {
                        if let Some(s) = child.as_str() {
                            found.insert(s.to_owned());
                        }
                    } else {
                        walk_value(child, found);
                    }
                }
            }
            _ => {}
        }
    }

    let mut found = BTreeSet::new();
    for table in &slice.tables {
        for row in &table.rows {
            for (col, cell) in table.columns.iter().zip(row.iter()) {
                if col == "evidence_id" {
                    if let Some(s) = cell.as_str() {
                        found.insert(s.to_owned());
                    }
                } else {
                    walk_value(cell, &mut found);
                }
            }
        }
    }
    found
}

/// Build the exact `REQ*(k)` target set from the phase manifest.
#[must_use]
pub fn required_mutation_keys(
    work_queue: &PhaseWorkQueueV5,
    phase_index: i64,
) -> BTreeSet<(String, String)> {
    let mut required = BTreeSet::new();
    let mut add = |kind: &str, ids: &[String]| {
        for entity_id in ids {
            required.insert((kind.to_owned(), entity_id.clone()));
        }
    };
    if phase_index == 1 {
        add("CONNECTOR_SYNC_MUTATION", &work_queue.target_connector_ids);
        add(
            "HR_SOURCE_MAPPING_MUTATION",
            &work_queue.target_hr_mapping_ids,
        );
        add(
            "ACCOUNT_RECON_MUTATION",
            &work_queue.target_human_account_ids,
        );
        add("NHI_GOVERNANCE_MUTATION", &work_queue.target_nhi_ids);
        add("AGENT_REGISTRY_MUTATION", &work_queue.target_agent_ids);
        add("CLOUD_CIEM_MUTATION", &work_queue.target_ciem_task_ids);
        add("JML_WORKFLOW_MUTATION", &work_queue.target_jml_workflow_ids);
    } else if phase_index == 2 {
        add(
            "CAE_CONTAINMENT_MUTATION",
            &work_queue.target_cae_incident_ids,
        );
        add("UAR_CAMPAIGN_MUTATION", &work_queue.target_uar_campaign_ids);
        add("JIT_DECISION_MUTATION", &work_queue.target_jit_request_ids);
        add(
            "AGENT_DELEGATION_MUTATION",
            &work_queue.target_agent_delegation_ids,
        );
    }
    if matches!(phase_index, 1 | 2) {
        add("OPTIMIZATION_MUTATION", &work_queue.target_opt_task_ids);
    }
    required
}

#[must_use]
pub fn mutation_key(mutation: &AtomicMutationV5) -> (String, String) {
    (
        mutation.mutation_kind().to_owned(),
        mutation.target_entity_id().to_owned(),
    )
}

/// Return whether `mutations` covers `required_mutation_keys` without duplicates.
#[must_use]
pub fn completeness_ok(
    work_queue: &PhaseWorkQueueV5,
    phase_index: i64,
    mutations: &[AtomicMutationV5],
) -> bool {
    let keys: Vec<(String, String)> = mutations.iter().map(mutation_key).collect();
    let unique: BTreeSet<(String, String)> = keys.iter().cloned().collect();
    keys.len() == unique.len() && unique == required_mutation_keys(work_queue, phase_index)
}

/// Return Stage-2 delivered-view structural/FK failures in stable sorted order.
#[must_use]
pub fn structural_errors(
    view: &ViewV5,
    _phase_index: i64,
    mutations: &[AtomicMutationV5],
    _work_queue: &PhaseWorkQueueV5,
) -> Vec<String> {
    let mut errors = BTreeSet::new();
    for mutation in mutations {
        let entity_id = mutation.target_entity_id();
        match mutation {
            AtomicMutationV5::ConnectorSync(m) => {
                let Some(conn) = view.resolve_connector_schema(entity_id) else {
                    errors.insert(format!("{entity_id}:connector_missing"));
                    continue;
                };
                let mut page_aids: BTreeSet<String> = BTreeSet::new();
                for ((t, _), row) in &view.resolved {
                    if t != "connector_pages" {
                        continue;
                    }
                    let TableRowV5::ConnectorPage(page) = row else {
                        continue;
                    };
                    if page.connector_id != conn.connector_id {
                        continue;
                    }
                    for raw_row in &page.records {
                        let mut account_id: Option<&str> = None;
                        for (k, v) in raw_row {
                            if k == "account_id"
                                && let Some(s) = v.as_str()
                            {
                                account_id = Some(s);
                            }
                        }
                        if let Some(aid) = account_id {
                            page_aids.insert(aid.to_owned());
                        }
                    }
                }
                let mut admitted = page_aids;
                if conn.sync_mode == ConnectorSyncModeV5::DeltaCursor
                    || conn.transport_kind == ConnectorTransportKindV5::FlatFileSftp
                {
                    admitted.extend(conn.baseline_reconciled_account_ids.iter().cloned());
                }
                if !m
                    .reconciled_account_ids
                    .iter()
                    .all(|id| admitted.contains(id))
                {
                    errors.insert(format!("{entity_id}:reconciled_account_fk"));
                }
                let mapping_roles: BTreeSet<&str> =
                    conn.mapping_rules.iter().map(|(_, v)| v.as_str()).collect();
                for drift in &m.remediated_spoke_drift {
                    let Some((acct, rest)) = drift.split_once(':') else {
                        errors.insert(format!("{entity_id}:drift_account_fk"));
                        break;
                    };
                    if !admitted.contains(acct) {
                        errors.insert(format!("{entity_id}:drift_account_fk"));
                        break;
                    }
                    if rest.starts_with("role_")
                        && !view.has_row("roles", rest)
                        && !mapping_roles.contains(rest)
                    {
                        errors.insert(format!("{entity_id}:drift_role_fk"));
                        break;
                    }
                }
                let remediated_set: BTreeSet<&str> = m
                    .remediated_spoke_drift
                    .iter()
                    .map(String::as_str)
                    .collect();
                for action in &m.itsm_ticket_actions {
                    if !remediated_set.contains(action.drift_token.as_str()) {
                        errors.insert(format!("{entity_id}:itsm_drift_fk"));
                        break;
                    }
                    if let Some(assignee) = &action.assignee_account_id
                        && view.resolve_idp_account(assignee).is_none()
                    {
                        errors.insert(format!("{entity_id}:itsm_assignee_fk"));
                        break;
                    }
                }
            }
            AtomicMutationV5::HrSourceMapping(m) => {
                let Some(mapping_task) = view.resolve_hr_source_mapping(entity_id) else {
                    errors.insert(format!("{entity_id}:hr_mapping_missing"));
                    continue;
                };
                let feeds: BTreeSet<&str> = mapping_task
                    .feeds
                    .iter()
                    .map(|f| f.feed_id.as_str())
                    .collect();
                let assertion_people: BTreeSet<&str> = mapping_task
                    .feeds
                    .iter()
                    .flat_map(|f| f.assertions.iter().map(|a| a.person_id.as_str()))
                    .collect();
                let assertion_keys: BTreeSet<String> = mapping_task
                    .feeds
                    .iter()
                    .flat_map(|f| {
                        f.assertions
                            .iter()
                            .map(move |a| format!("{}:{}", f.feed_id, a.person_id))
                    })
                    .collect();
                let acc: BTreeSet<&str> = m.accepted_feed_ids.iter().map(String::as_str).collect();
                let rej: BTreeSet<&str> = m.rejected_feed_ids.iter().map(String::as_str).collect();
                let union: BTreeSet<&str> = acc.union(&rej).copied().collect();
                if acc.intersection(&rej).next().is_some() || union != feeds {
                    errors.insert(format!("{entity_id}:feed_partition"));
                }
                if !m
                    .resolved_person_statuses
                    .iter()
                    .all(|(pid, _)| assertion_people.contains(pid.as_str()))
                {
                    errors.insert(format!("{entity_id}:resolved_person_fk"));
                }
                if !m
                    .quarantined_assertion_keys
                    .iter()
                    .all(|k| assertion_keys.contains(k))
                {
                    errors.insert(format!("{entity_id}:quarantine_assertion_fk"));
                }
            }
            AtomicMutationV5::AccountRecon(m) => {
                let Some(account) = view.resolve_idp_account(entity_id) else {
                    errors.insert(format!("{entity_id}:account_missing"));
                    continue;
                };
                if let Some(pid) = &m.bound_person_id {
                    match view.resolve_hr_person(pid) {
                        None => {
                            errors.insert(format!("{entity_id}:person_missing"));
                        }
                        Some(person) => {
                            let has_eng = m.bound_engagement_id.as_deref().is_some_and(|eid| {
                                person.engagements.iter().any(|e| e.eng_id == eid)
                            });
                            if !has_eng {
                                errors.insert(format!("{entity_id}:engagement_missing"));
                            }
                        }
                    }
                }
                let direct: BTreeSet<&str> = account
                    .direct_grant_ids
                    .iter()
                    .map(String::as_str)
                    .collect();
                if !m
                    .retained_grant_ids
                    .iter()
                    .all(|gid| direct.contains(gid.as_str()) && view.has_row("grants", gid))
                {
                    errors.insert(format!("{entity_id}:retained_grant_fk"));
                }
            }
            AtomicMutationV5::NhiGovernance(m) => {
                if view.resolve_target_nhi(entity_id).is_none() {
                    errors.insert(format!("{entity_id}:nhi_missing"));
                }
                if let Some(owner) = &m.resolved_owner_person_id
                    && view.resolve_hr_person(owner).is_none()
                {
                    errors.insert(format!("{entity_id}:owner_person_fk"));
                }
            }
            AtomicMutationV5::AgentRegistry(m) => {
                let Some(agent) = view.resolve_enterprise_agent(entity_id) else {
                    errors.insert(format!("{entity_id}:agent_missing"));
                    continue;
                };
                if let Some(sponsor) = &m.resolved_sponsor_person_id
                    && view.resolve_hr_person(sponsor).is_none()
                {
                    errors.insert(format!("{entity_id}:sponsor_person_fk"));
                }
                let binding_ids: BTreeSet<&str> = agent
                    .tool_bindings
                    .iter()
                    .map(|b| b.binding_id.as_str())
                    .collect();
                let all_ok = m
                    .active_autonomous_binding_ids
                    .iter()
                    .chain(m.active_hitl_binding_ids.iter())
                    .all(|bid| binding_ids.contains(bid.as_str()));
                if !all_ok {
                    errors.insert(format!("{entity_id}:binding_fk"));
                }
            }
            AtomicMutationV5::CloudCiem(m) => {
                let Some(ciem_task) = view.resolve_cloud_ciem_task(entity_id) else {
                    errors.insert(format!("{entity_id}:ciem_missing"));
                    continue;
                };
                let principals: BTreeSet<&str> = ciem_task
                    .principals
                    .iter()
                    .map(|p| p.cprin_id.as_str())
                    .collect();
                let findings: BTreeSet<&str> =
                    m.findings.iter().map(|f| f.cprin_id.as_str()).collect();
                if findings != principals {
                    errors.insert(format!("{entity_id}:ciem_principal_partition"));
                }
                for principal in &ciem_task.principals {
                    let linked = &principal.linked_principal_id;
                    if view.resolve_idp_account(linked).is_none()
                        && view.resolve_target_nhi(linked).is_none()
                        && view.resolve_enterprise_agent(linked).is_none()
                    {
                        errors.insert(format!("{entity_id}:linked_principal_fk"));
                        break;
                    }
                }
                let allowed_forb: BTreeSet<&str> = ciem_task
                    .forbidden_standing_cloud_actions
                    .iter()
                    .map(String::as_str)
                    .collect();
                for finding in &m.findings {
                    if !finding
                        .effective_forbidden_actions
                        .iter()
                        .all(|a| allowed_forb.contains(a.as_str()))
                    {
                        errors.insert(format!("{entity_id}:ciem_action_fk"));
                        break;
                    }
                }
            }
            AtomicMutationV5::Optimization(m) => {
                let Some(opt_task) = view.resolve_optimization_task(entity_id) else {
                    errors.insert(format!("{entity_id}:optimization_missing"));
                    continue;
                };
                let cands: BTreeSet<&str> = opt_task
                    .candidates
                    .iter()
                    .map(|c| c.candidate_id.as_str())
                    .collect();
                if !m
                    .selected_ids
                    .iter()
                    .all(|cid| cands.contains(cid.as_str()))
                {
                    errors.insert(format!("{entity_id}:optimization_candidate_fk"));
                }
            }
            AtomicMutationV5::JmlWorkflow(m) => {
                let Some(wfl_task) = view.resolve_jml_workflow_task(entity_id) else {
                    errors.insert(format!("{entity_id}:workflow_missing"));
                    continue;
                };
                let catalog_ids: BTreeSet<&str> = wfl_task
                    .birthright_catalog
                    .iter()
                    .map(|r| r.rule_id.as_str())
                    .collect();
                let partition: BTreeSet<&str> = m
                    .active_birthright_rule_ids
                    .iter()
                    .chain(m.converted_eligible_brule_ids.iter())
                    .chain(m.quarantined_birthright_rule_ids.iter())
                    .map(String::as_str)
                    .collect();
                if partition != catalog_ids {
                    errors.insert(format!("{entity_id}:birthright_partition"));
                }
            }
            AtomicMutationV5::CaeContainment(m) => {
                if view.resolve_cae_incident(entity_id).is_none() {
                    errors.insert(format!("{entity_id}:incident_missing"));
                }
                if !m.retro_invalidated_ids.iter().all(|id| {
                    view.has_row("change_tickets", id) || view.has_row("transfer_bridges", id)
                }) {
                    errors.insert(format!("{entity_id}:retro_invalidated_fk"));
                }
            }
            AtomicMutationV5::UarCampaign(m) => {
                let Some(campaign) = view.resolve_uar_campaign(entity_id) else {
                    errors.insert(format!("{entity_id}:campaign_missing"));
                    continue;
                };
                let item_ids: BTreeSet<&str> =
                    campaign.items.iter().map(|i| i.item_id.as_str()).collect();
                let decision_ids: BTreeSet<&str> =
                    m.uar_decisions.iter().map(|(i, _)| i.as_str()).collect();
                let override_ids: BTreeSet<&str> = m
                    .reviewer_overrides
                    .iter()
                    .map(|(i, _)| i.as_str())
                    .collect();
                let backup_set: BTreeSet<&str> = campaign
                    .backup_reviewer_account_ids
                    .iter()
                    .map(String::as_str)
                    .collect();
                if decision_ids != item_ids {
                    errors.insert(format!("{entity_id}:decision_item_partition"));
                }
                if !override_ids.is_subset(&item_ids) {
                    errors.insert(format!("{entity_id}:reviewer_override_item_fk"));
                }
                if !m.reviewer_overrides.iter().all(|(_, acct)| {
                    backup_set.contains(acct.as_str()) && view.has_row("idp_accounts", acct)
                }) {
                    errors.insert(format!("{entity_id}:reviewer_override_account_fk"));
                }
                if let Some(custodian) = &m.emergency_custodian_account_id {
                    let cand_ok = campaign
                        .candidate_custodian_account_ids
                        .iter()
                        .any(|c| c == custodian)
                        && view.has_row("idp_accounts", custodian);
                    if !cand_ok {
                        errors.insert(format!("{entity_id}:custodian_account_fk"));
                    }
                }
            }
            AtomicMutationV5::JitDecision(m) => {
                if view.resolve_jit_request(entity_id).is_none() {
                    errors.insert(format!("{entity_id}:jit_request_missing"));
                }
                if let Some(approver) = &m.approver_account_id
                    && view.resolve_idp_account(approver).is_none()
                {
                    errors.insert(format!("{entity_id}:approver_account_fk"));
                }
            }
            AtomicMutationV5::AgentDelegation(m) => {
                if view.resolve_agent_delegation_request(entity_id).is_none() {
                    errors.insert(format!("{entity_id}:delegation_request_missing"));
                }
                if let Some(cosigner) = &m.hitl_cosigner_account_id
                    && view.resolve_idp_account(cosigner).is_none()
                {
                    errors.insert(format!("{entity_id}:cosigner_account_fk"));
                }
            }
        }
    }
    errors.into_iter().collect()
}

/// Return `true` iff [`structural_errors`] is empty.
#[must_use]
pub fn structural_ok(
    view: &ViewV5,
    phase_index: i64,
    mutations: &[AtomicMutationV5],
    work_queue: &PhaseWorkQueueV5,
) -> bool {
    structural_errors(view, phase_index, mutations, work_queue).is_empty()
}

/// Build a lookup map from `(mutation_kind, target_entity_id)` to mutation.
#[must_use]
pub fn mutation_map(
    mutations: &[AtomicMutationV5],
) -> HashMap<(String, String), &AtomicMutationV5> {
    mutations.iter().map(|m| (mutation_key(m), m)).collect()
}

#[cfg(test)]
mod snapshot_serialization_tests {
    use super::python_bytes_repr;

    #[test]
    fn byte_repr_matches_python_quote_and_escape_rules() {
        assert_eq!(python_bytes_repr(b""), "b''");
        assert_eq!(python_bytes_repr(b"ascii"), "b'ascii'");
        assert_eq!(python_bytes_repr(b"one'quote"), "b\"one'quote\"");
        assert_eq!(python_bytes_repr(b"both'\"quotes"), "b'both\\'\"quotes'");
        assert_eq!(
            python_bytes_repr(b"line\n\t\\\xc3\xa9"),
            "b'line\\n\\t\\\\\\xc3\\xa9'"
        );
    }
}
