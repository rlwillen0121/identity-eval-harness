//! Work queues, Phase-1/Phase-2 slice builders, and compact observation helpers.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use ib_v5_runtime::{obs_strategy_q1, obs_strategy_q2};
use ib_v5_schemas::{
    AuthorityTierV5, ChangeTicketV5, CompactTableV5, ConnectorPageV5, DeliveredSliceV5,
    EpisodeV5, ExternalTicketStatusV5, ExternalTicketSystemV5, PhaseManifestV5, PhaseWorkQueueV5,
    PrivilegeTierV5, RoleV5, SliceDescriptorV5, StrictV5Model, TableRowV5, UARCampaignV5,
    UARItemV5, UARPreserveStandingFloorOverrideV5, decode_compact_table_v5, encode_compact_table_v5,
};
use indexmap::IndexMap;
use serde_json::{Map, Value};

use crate::error::GeneratorError;
use crate::generator::planner::{V5Cell, etag, evidence, evidence_codes, replica, slice_id};
use crate::generator::world::{IdBook, family_profile};

fn sorted_keys<V>(map: &IndexMap<String, V>) -> Vec<String> {
    let mut keys: Vec<String> = map.keys().cloned().collect();
    keys.sort();
    keys
}

pub(crate) fn phase1_queue(world: &ib_v5_schemas::WorldStateV5, ids: &IdBook) -> PhaseWorkQueueV5 {
    PhaseWorkQueueV5 {
        target_connector_ids: vec![ids.target_connector.clone()],
        target_hr_mapping_ids: sorted_keys(&world.hr_source_mappings),
        target_human_account_ids: sorted_keys(&world.idp_accounts),
        target_nhi_ids: sorted_keys(&world.target_nhis),
        target_agent_ids: sorted_keys(&world.enterprise_agents),
        target_ciem_task_ids: sorted_keys(&world.cloud_ciem_tasks),
        target_jml_workflow_ids: sorted_keys(&world.jml_workflow_tasks),
        target_opt_task_ids: vec![ids.opt("p1")],
        target_cae_incident_ids: Vec::new(),
        target_uar_campaign_ids: Vec::new(),
        target_jit_request_ids: Vec::new(),
        target_agent_delegation_ids: Vec::new(),
    }
}

pub(crate) fn phase2_queue(
    world: &ib_v5_schemas::WorldStateV5,
    ids: &IdBook,
    cell: &V5Cell,
) -> Result<PhaseWorkQueueV5, GeneratorError> {
    let profile = family_profile(&cell.family_id)?;
    let mut target_uar_campaign_ids = if profile.sequential_uar {
        vec![ids.campaign("first"), ids.campaign("second")]
    } else {
        vec![ids.campaign("first")]
    };
    target_uar_campaign_ids.sort();

    let mut target_jit_request_ids = vec![
        ids.request("first"),
        ids.request("second"),
        ids.request("replay"),
    ];
    target_jit_request_ids.sort();

    let mut target_agent_delegation_ids =
        vec![ids.delegation("root"), ids.delegation("child")];
    target_agent_delegation_ids.sort();

    Ok(PhaseWorkQueueV5 {
        target_connector_ids: Vec::new(),
        target_hr_mapping_ids: Vec::new(),
        target_human_account_ids: Vec::new(),
        target_nhi_ids: Vec::new(),
        target_agent_ids: Vec::new(),
        target_ciem_task_ids: Vec::new(),
        target_jml_workflow_ids: Vec::new(),
        target_opt_task_ids: vec![ids.opt("p2")],
        target_cae_incident_ids: sorted_keys(&world.cae_incidents),
        target_uar_campaign_ids,
        target_jit_request_ids,
        target_agent_delegation_ids,
    })
}

fn delivered_slice(
    cell: &V5Cell,
    label: &str,
    rows: BTreeMap<&'static str, Vec<TableRowV5>>,
    authority: AuthorityTierV5,
    watermark_s: i64,
) -> Result<DeliveredSliceV5, GeneratorError> {
    let mut tables = Vec::new();
    for (table_name, table_rows) in rows {
        if !table_rows.is_empty() {
            tables.push(encode_compact_table_v5(table_name, &table_rows)?);
        }
    }
    Ok(DeliveredSliceV5 {
        slice_id: slice_id(cell, label)?,
        authority_tier: authority,
        watermark_s,
        tables,
    })
}

fn slice_pair(
    cell: &V5Cell,
    label: &str,
    scope_filter: &str,
    rows: BTreeMap<&'static str, Vec<TableRowV5>>,
    watermark_s: i64,
) -> Result<(SliceDescriptorV5, DeliveredSliceV5), GeneratorError> {
    let payload = delivered_slice(cell, label, rows, AuthorityTierV5::SecondaryReplica, watermark_s)?;
    let row_count: usize = payload.tables.iter().map(|t| t.rows.len()).sum();
    if row_count == 0 {
        return Err(GeneratorError::InvalidInput(format!(
            "cannot construct empty v5 slice {label}"
        )));
    }
    let mut table_names: Vec<String> = payload
        .tables
        .iter()
        .map(|t| t.table_name.clone())
        .collect();
    table_names.sort();
    let descriptor = SliceDescriptorV5 {
        slice_id: payload.slice_id.clone(),
        source_system: "src_replica".to_owned(),
        table_names,
        scope_filter: scope_filter.to_owned(),
        authority_tier: AuthorityTierV5::SecondaryReplica,
        is_complete_for_scope: false,
        watermark_s,
        row_count: i64::try_from(row_count).expect("row_count fits i64"),
        credit_cost: 1,
    };
    Ok((descriptor, payload))
}

fn primary_slice_pair(
    cell: &V5Cell,
    label: &str,
    rows: BTreeMap<&'static str, Vec<TableRowV5>>,
    watermark_s: i64,
) -> Result<(SliceDescriptorV5, DeliveredSliceV5), GeneratorError> {
    let payload = delivered_slice(
        cell,
        label,
        rows,
        AuthorityTierV5::AuthoritativePrimary,
        watermark_s,
    )?;
    let row_count: usize = payload.tables.iter().map(|t| t.rows.len()).sum();
    let mut table_names: Vec<String> = payload
        .tables
        .iter()
        .map(|t| t.table_name.clone())
        .collect();
    table_names.sort();
    let descriptor = SliceDescriptorV5 {
        slice_id: payload.slice_id.clone(),
        source_system: "src_primary".to_owned(),
        table_names,
        scope_filter: "global=*".to_owned(),
        authority_tier: AuthorityTierV5::AuthoritativePrimary,
        is_complete_for_scope: true,
        watermark_s,
        row_count: i64::try_from(row_count).expect("row_count fits i64"),
        credit_cost: 1,
    };
    Ok((descriptor, payload))
}

fn sorted_map_rows<V: Clone>(
    map: &IndexMap<String, V>,
    wrap: impl Fn(V) -> TableRowV5,
) -> Vec<TableRowV5> {
    let mut keys: Vec<&String> = map.keys().collect();
    keys.sort();
    keys.into_iter().map(|k| wrap(map[k].clone())).collect()
}

pub(crate) fn make_phase1_slices(
    cell: &V5Cell,
    world: &ib_v5_schemas::WorldStateV5,
    ids: &IdBook,
) -> Result<(PhaseManifestV5, IndexMap<String, DeliveredSliceV5>), GeneratorError> {
    let codes = evidence_codes(&cell.episode_id);
    let clock = world.clock_epoch_s;

    let mut pages = world.connector_pages.clone();
    pages.sort_by(|a, b| a.page_id.cmp(&b.page_id));

    let mut primary_rows: BTreeMap<&'static str, Vec<TableRowV5>> = BTreeMap::new();
    primary_rows.insert("hr_persons", sorted_map_rows(&world.hr_persons, TableRowV5::HrPerson));
    primary_rows.insert("roles", sorted_map_rows(&world.roles, TableRowV5::Role));
    primary_rows.insert("grants", sorted_map_rows(&world.grants, TableRowV5::Grant));
    primary_rows.insert("groups", sorted_map_rows(&world.groups, TableRowV5::Group));
    primary_rows.insert(
        "idp_accounts",
        sorted_map_rows(&world.idp_accounts, TableRowV5::IdpAccount),
    );
    primary_rows.insert(
        "transfer_bridges",
        sorted_map_rows(&world.transfer_bridges, TableRowV5::TransferBridge),
    );
    primary_rows.insert(
        "compensating_controls",
        sorted_map_rows(&world.compensating_controls, TableRowV5::CompensatingControl),
    );
    primary_rows.insert(
        "connector_schemas",
        sorted_map_rows(&world.connector_schemas, TableRowV5::ConnectorSchema),
    );
    primary_rows.insert(
        "connector_pages",
        pages.into_iter().map(TableRowV5::ConnectorPage).collect(),
    );
    primary_rows.insert(
        "hr_source_mappings",
        sorted_map_rows(&world.hr_source_mappings, TableRowV5::HrSourceMapping),
    );
    primary_rows.insert(
        "baseline_nhis",
        sorted_map_rows(&world.baseline_nhis, TableRowV5::BaselineNhi),
    );
    primary_rows.insert(
        "target_nhis",
        sorted_map_rows(&world.target_nhis, TableRowV5::TargetNhi),
    );
    primary_rows.insert(
        "baseline_agents",
        sorted_map_rows(&world.baseline_agents, TableRowV5::BaselineAgent),
    );
    primary_rows.insert(
        "enterprise_agents",
        sorted_map_rows(&world.enterprise_agents, TableRowV5::EnterpriseAgent),
    );
    primary_rows.insert(
        "cloud_ciem_tasks",
        sorted_map_rows(&world.cloud_ciem_tasks, TableRowV5::CloudCiemTask),
    );
    primary_rows.insert(
        "optimization_tasks",
        vec![TableRowV5::OptimizationTask(
            world.optimization_tasks[&ids.opt("p1")].clone(),
        )],
    );
    primary_rows.insert(
        "jml_workflow_tasks",
        sorted_map_rows(&world.jml_workflow_tasks, TableRowV5::JmlWorkflowTask),
    );

    let (core_desc, core_slice) = primary_slice_pair(cell, "p1-core", primary_rows, clock - 60)?;
    let mut payloads = IndexMap::new();
    payloads.insert(core_slice.slice_id.clone(), core_slice);
    let mut descriptors = vec![core_desc.clone()];
    let mut q2_count = 0i64;

    let main_connector = &world.connector_schemas[&ids.target_connector];
    let mut conn_rows = BTreeMap::new();
    conn_rows.insert(
        "connector_schemas",
        vec![TableRowV5::ConnectorSchema(replica(
            cell,
            main_connector,
            "p1:connector-main",
        )?)],
    );
    let (desc, payload) = slice_pair(
        cell,
        "p1-connector-main",
        &format!("connector_id={}", ids.target_connector),
        conn_rows,
        clock - 120,
    )?;
    q2_count += 1;
    descriptors.push(desc);
    payloads.insert(payload.slice_id.clone(), payload);

    let control_id = ids.control("mover");
    let bridge_id = ids.bridge("mover");
    let dept_specs: [(&str, &str, TableRowV5); 3] = [
        (
            "/d",
            "compensating_controls",
            TableRowV5::CompensatingControl(replica(
                cell,
                &world.compensating_controls[&control_id],
                "p1:dept-finance-control",
            )?),
        ),
        (
            "/d/p",
            "compensating_controls",
            TableRowV5::CompensatingControl(replica(
                cell,
                &world.compensating_controls[&control_id],
                "p1:dept-payroll-control",
            )?),
        ),
        (
            "/d/l",
            "transfer_bridges",
            TableRowV5::TransferBridge(replica(
                cell,
                &world.transfer_bridges[&bridge_id],
                "p1:dept-bridge",
            )?),
        ),
    ];
    for (index, (scope, table_name, row)) in dept_specs.into_iter().enumerate() {
        let mut rows = BTreeMap::new();
        rows.insert(table_name, vec![row]);
        let (desc, payload) = slice_pair(
            cell,
            &format!("p1-dept-{index:02}"),
            &format!("dept_prefix={scope}"),
            rows,
            clock - 130 - i64::try_from(index).expect("index fits i64"),
        )?;
        q2_count += 1;
        descriptors.push(desc);
        payloads.insert(payload.slice_id.clone(), payload);
    }

    let finance_role = &world.roles[&ids.role("review")];
    let mut fin_rows = BTreeMap::new();
    fin_rows.insert(
        "roles",
        vec![TableRowV5::Role(replica(
            cell,
            finance_role,
            "p1:app-finance",
        )?)],
    );
    let (desc, payload) = slice_pair(
        cell,
        "p1-app-finance",
        "app_id=fin",
        fin_rows,
        clock - 140,
    )?;
    q2_count += 1;
    descriptors.push(desc);
    payloads.insert(payload.slice_id.clone(), payload);

    for (name, suffix) in [("decoy_a", "a"), ("decoy_b", "b")] {
        let role_row = RoleV5 {
            role_id: ids.role(name),
            app_id: format!("u-{suffix}"),
            privilege_tier: PrivilegeTierV5::Read,
            capabilities: vec!["read".to_owned()],
            is_jit_capable_target: true,
            evidence_id: evidence(&codes, &format!("p1:app-{name}"))?,
        };
        let mut rows = BTreeMap::new();
        rows.insert("roles", vec![TableRowV5::Role(role_row)]);
        let (desc, payload) = slice_pair(
            cell,
            &format!("p1-app-unused-{name}"),
            &format!("app_id=u-{suffix}"),
            rows,
            clock - 140,
        )?;
        descriptors.push(desc);
        payloads.insert(payload.slice_id.clone(), payload);
    }

    descriptors.sort_by(|a, b| a.slice_id.cmp(&b.slice_id));
    let manifest = PhaseManifestV5 {
        phase_index: 1,
        clock_epoch_s: clock,
        query_budget: core_desc.credit_cost + q2_count,
        approval_gated: false,
        work_queue: phase1_queue(world, ids),
        source_directory: descriptors,
        invalidated_entity_ids: Vec::new(),
        prior_phase_ledger: None,
    };
    Ok((manifest, payloads))
}

pub(crate) fn make_phase2_slices(
    cell: &V5Cell,
    world: &ib_v5_schemas::WorldStateV5,
    ids: &IdBook,
    queue: &PhaseWorkQueueV5,
    visible_ledger: &ib_v5_schemas::PriorPhaseLedgerV5,
    invalidated_entity_ids: &[String],
) -> Result<(PhaseManifestV5, IndexMap<String, DeliveredSliceV5>), GeneratorError> {
    let codes = evidence_codes(&cell.episode_id);
    let clock = world.clock_epoch_s;

    let mut sessions = world.historical_sessions.clone();
    sessions.sort_by(|a, b| a.session_id.cmp(&b.session_id));

    let mut primary_rows: BTreeMap<&'static str, Vec<TableRowV5>> = BTreeMap::new();
    primary_rows.insert(
        "hr_persons",
        invalidated_entity_ids
            .iter()
            .map(|pid| TableRowV5::HrPerson(world.hr_persons[pid].clone()))
            .collect(),
    );
    primary_rows.insert(
        "cae_incidents",
        sorted_map_rows(&world.cae_incidents, TableRowV5::CaeIncident),
    );
    primary_rows.insert(
        "uar_campaigns",
        sorted_map_rows(&world.uar_campaigns, TableRowV5::UarCampaign),
    );
    primary_rows.insert(
        "historical_sessions",
        sessions
            .into_iter()
            .map(TableRowV5::HistoricalSession)
            .collect(),
    );
    primary_rows.insert(
        "jit_requests",
        sorted_map_rows(&world.jit_requests, TableRowV5::JitRequest),
    );
    primary_rows.insert(
        "agent_delegation_requests",
        sorted_map_rows(
            &world.agent_delegation_requests,
            TableRowV5::AgentDelegationRequest,
        ),
    );
    primary_rows.insert(
        "optimization_tasks",
        vec![TableRowV5::OptimizationTask(
            world.optimization_tasks[&ids.opt("p2")].clone(),
        )],
    );

    let (core_desc, core_slice) = primary_slice_pair(cell, "p2-core", primary_rows, clock - 60)?;
    let mut payloads = IndexMap::new();
    payloads.insert(core_slice.slice_id.clone(), core_slice);
    let mut descriptors = vec![core_desc.clone()];
    let mut q2_count = 0i64;

    let app_campaign = &world.uar_campaigns[&ids.campaign("first")];
    let app_delegation = &world.agent_delegation_requests[&ids.delegation("root")];
    let mut app_fin_rows = BTreeMap::new();
    app_fin_rows.insert(
        "uar_campaigns",
        vec![TableRowV5::UarCampaign(replica(
            cell,
            app_campaign,
            "p2:app-campaign",
        )?)],
    );
    app_fin_rows.insert(
        "agent_delegation_requests",
        vec![TableRowV5::AgentDelegationRequest(replica(
            cell,
            app_delegation,
            "p2:app-delegation",
        )?)],
    );
    let (desc, payload) = slice_pair(
        cell,
        "p2-app-finance",
        "app_id=fin",
        app_fin_rows,
        clock - 120,
    )?;
    q2_count += 1;
    descriptors.push(desc);
    payloads.insert(payload.slice_id.clone(), payload);

    for (name, suffix, decoy_item_name, eval_seq) in [
        ("decoy_a", "a", "third", 6i64),
        ("decoy_b", "b", "fourth", 7i64),
    ] {
        let decoy_item = UARItemV5 {
            item_id: ids.item(decoy_item_name),
            account_id: ids.account("reviewer"),
            grant_id: ids.grant("reviewer_review"),
            assigned_reviewer_account_id: ids.account("manager"),
            used_within_lookback: true,
            evidence_id: evidence(&codes, &format!("p2:decoy-item:{name}"))?,
        };
        let campaign = UARCampaignV5 {
            campaign_id: ids.campaign(name),
            app_id: format!("u-{suffix}"),
            scope_path: "/d/p".to_owned(),
            admin_role_id: ids.role("review"),
            m_standing: 1,
            m_eligible: 0,
            preserve_standing_floor_override:
                UARPreserveStandingFloorOverrideV5::UseOverlayDefault,
            backup_reviewer_account_ids: vec![ids.account("manager")],
            candidate_custodian_account_ids: Vec::new(),
            items: vec![decoy_item],
            etag: etag(cell, &format!("p2:decoy-campaign:{name}")),
            evidence_id: evidence(&codes, &format!("p2:decoy-campaign:{name}"))?,
        };
        let mut delegation = app_delegation.clone();
        delegation.request_id = ids.delegation(name);
        delegation.eval_seq = eval_seq;
        delegation.parent_delegation_request_id = None;
        delegation.delegation_hop = 1;
        delegation.requested_role_id = ids.role("read");
        delegation.target_resource = format!("u-{suffix}");
        delegation.proposed_hitl_approver_account_id = None;
        let delegation = replica(cell, &delegation, &format!("p2:app-{name}-delegation"))?;
        let campaign = replica(cell, &campaign, &format!("p2:app-{name}-campaign"))?;

        let mut rows = BTreeMap::new();
        rows.insert("uar_campaigns", vec![TableRowV5::UarCampaign(campaign)]);
        rows.insert(
            "agent_delegation_requests",
            vec![TableRowV5::AgentDelegationRequest(delegation)],
        );
        let (desc, payload) = slice_pair(
            cell,
            &format!("p2-app-unused-{name}"),
            &format!("app_id=u-{suffix}"),
            rows,
            clock - 120,
        )?;
        descriptors.push(desc);
        payloads.insert(payload.slice_id.clone(), payload);
    }

    let primary_ticket_id = ids.ticket("primary");
    let primary_ticket = world.change_tickets[&primary_ticket_id].clone();
    let mut ticket_rows = BTreeMap::new();
    ticket_rows.insert("change_tickets", vec![TableRowV5::ChangeTicket(primary_ticket.clone())]);
    let (desc, payload) = slice_pair(
        cell,
        "p2-ticket-primary",
        &format!("ticket_partition={primary_ticket_id}"),
        ticket_rows,
        clock - 130,
    )?;
    q2_count += 1;
    descriptors.push(desc);
    payloads.insert(payload.slice_id.clone(), payload);

    for (name, suffix) in [("decoy_a", "a"), ("decoy_b", "b")] {
        let ticket_id = ids.ticket(name);
        let mut ticket: ChangeTicketV5 = primary_ticket.clone();
        ticket.ticket_id = ticket_id.clone();
        ticket.external_system = if name == "decoy_a" {
            ExternalTicketSystemV5::ServicenowRitm
        } else {
            ExternalTicketSystemV5::JiraJsm
        };
        ticket.external_ticket_status = if name == "decoy_a" {
            ExternalTicketStatusV5::Cancelled
        } else {
            ExternalTicketStatusV5::ApprovedExternal
        };
        ticket.target_resource = format!("u-{suffix}");
        ticket.single_use = name == "decoy_a";

        let mut rows = BTreeMap::new();
        rows.insert(
            "change_tickets",
            vec![TableRowV5::ChangeTicket(replica(
                cell,
                &ticket,
                &format!("p2:ticket-{name}"),
            )?)],
        );
        let (desc, payload) = slice_pair(
            cell,
            &format!("p2-ticket-unused-{name}"),
            &format!("ticket_partition={ticket_id}"),
            rows,
            clock - 130,
        )?;
        descriptors.push(desc);
        payloads.insert(payload.slice_id.clone(), payload);
    }

    descriptors.sort_by(|a, b| a.slice_id.cmp(&b.slice_id));
    let manifest = PhaseManifestV5 {
        phase_index: 2,
        clock_epoch_s: clock,
        query_budget: core_desc.credit_cost + q2_count,
        approval_gated: false,
        work_queue: queue.clone(),
        source_directory: descriptors,
        invalidated_entity_ids: invalidated_entity_ids.to_vec(),
        prior_phase_ledger: Some(visible_ledger.clone()),
    };
    Ok((manifest, payloads))
}

pub(crate) fn compact_observation_source_rows(
    cell: &V5Cell,
    episode: &EpisodeV5,
) -> Result<EpisodeV5, GeneratorError> {
    let phase1_manifest = &episode.phase1_manifest;
    let phase1_q1_ids = obs_strategy_q1(phase1_manifest, &episode.policy_card, 1)?;
    let phase1_q1: Vec<DeliveredSliceV5> = phase1_q1_ids
        .iter()
        .map(|sid| episode.phase1_slices[sid].clone())
        .collect();
    let connector_id = &phase1_manifest.work_queue.target_connector_ids[0];
    let connector_scope = format!("connector_id={connector_id}");
    let connector_descriptor = phase1_manifest
        .source_directory
        .iter()
        .find(|item| item.scope_filter == connector_scope && !item.is_complete_for_scope)
        .ok_or_else(|| {
            GeneratorError::InvalidInput("missing connector descriptor in phase1_manifest".to_owned())
        })?
        .clone();

    let phase1_q2_ids = obs_strategy_q2(
        phase1_manifest,
        &phase1_q1,
        &phase1_manifest.work_queue,
        &episode.policy_card,
        1,
    )?;
    if !phase1_q2_ids.contains(&connector_descriptor.slice_id) {
        return Err(GeneratorError::InvalidInput(
            "compact observation profile lost its connector Q2 witness".to_owned(),
        ));
    }

    let mut connector_pages: Vec<ConnectorPageV5> = Vec::new();
    for delivered in &phase1_q1 {
        for table in &delivered.tables {
            if table.table_name == "connector_pages" {
                for row in decode_compact_table_v5(table)? {
                    if let TableRowV5::ConnectorPage(page) = row
                        && page.connector_id == *connector_id
                    {
                        connector_pages.push(page);
                    }
                }
            }
        }
    }
    if connector_pages.is_empty() {
        return Err(GeneratorError::InvalidInput(format!(
            "connector {connector_id} has no complete-Q1 page observation"
        )));
    }
    connector_pages.sort_by(|a, b| (a.attempt_index, &a.page_id).cmp(&(b.attempt_index, &b.page_id)));

    let q1_core_descriptor = phase1_manifest
        .source_directory
        .iter()
        .find(|item| phase1_q1_ids.contains(&item.slice_id))
        .ok_or_else(|| GeneratorError::InvalidInput("missing Q1 core descriptor".to_owned()))?
        .clone();
    let q1_core_payload = &episode.phase1_slices[&q1_core_descriptor.slice_id];
    let q1_core_tables: Vec<CompactTableV5> = q1_core_payload
        .tables
        .iter()
        .filter(|table| table.table_name != "connector_pages")
        .cloned()
        .collect();
    if q1_core_tables.len() == q1_core_payload.tables.len() {
        return Err(GeneratorError::InvalidInput(
            "complete-Q1 core slice lacks the connector page table to repartition".to_owned(),
        ));
    }

    let mut compact_q1_core_payload = q1_core_payload.clone();
    compact_q1_core_payload.tables = q1_core_tables.clone();
    let mut compact_q1_core_descriptor = q1_core_descriptor.clone();
    compact_q1_core_descriptor.table_names = q1_core_tables
        .iter()
        .map(|t| t.table_name.clone())
        .collect();
    compact_q1_core_descriptor.row_count =
        i64::try_from(q1_core_tables.iter().map(|t| t.rows.len()).sum::<usize>())
            .expect("row_count fits i64");

    let connector_rows: Vec<TableRowV5> = connector_pages
        .iter()
        .cloned()
        .map(TableRowV5::ConnectorPage)
        .collect();
    let connector_table = encode_compact_table_v5("connector_pages", &connector_rows)?;
    let mut compact_connector_payload = episode.phase1_slices[&connector_descriptor.slice_id].clone();
    compact_connector_payload.tables = vec![connector_table];
    compact_connector_payload.authority_tier = AuthorityTierV5::AuthoritativePrimary;
    compact_connector_payload.watermark_s = q1_core_descriptor.watermark_s;

    let mut compact_connector_descriptor = connector_descriptor.clone();
    compact_connector_descriptor.source_system = q1_core_descriptor.source_system;
    compact_connector_descriptor.table_names = vec!["connector_pages".to_owned()];
    compact_connector_descriptor.authority_tier = AuthorityTierV5::AuthoritativePrimary;
    compact_connector_descriptor.watermark_s = q1_core_descriptor.watermark_s;
    compact_connector_descriptor.row_count =
        i64::try_from(connector_pages.len()).expect("len fits i64");

    let dept_descriptor = phase1_manifest
        .source_directory
        .iter()
        .find(|item| {
            item.scope_filter == "dept_prefix=/d/p" && phase1_q2_ids.contains(&item.slice_id)
        })
        .ok_or_else(|| GeneratorError::InvalidInput("missing dept_prefix=/d/p descriptor".to_owned()))?
        .clone();

    let mut controls = Vec::new();
    let mut account_ids = HashSet::new();
    let mut grants = Vec::new();
    for delivered in &phase1_q1 {
        for table in &delivered.tables {
            for row in decode_compact_table_v5(table)? {
                match row {
                    TableRowV5::CompensatingControl(cc) => controls.push(cc),
                    TableRowV5::IdpAccount(acct) => {
                        account_ids.insert(acct.account_id);
                    }
                    TableRowV5::Grant(grant) => grants.push(grant),
                    _ => {}
                }
            }
        }
    }

    let monitor_account_id = controls
        .iter()
        .find(|c| account_ids.contains(&c.monitor_account_id))
        .map(|c| c.monitor_account_id.clone())
        .ok_or_else(|| GeneratorError::InvalidInput("missing monitor_account_id".to_owned()))?;
    let dept_grant = grants
        .iter()
        .find(|g| g.account_id == monitor_account_id && g.scope_path == "/d/p")
        .ok_or_else(|| {
            GeneratorError::InvalidInput(
                "compact observation profile lacks a monitor-account grant in /d/p".to_owned(),
            )
        })?;
    let parent_grant = grants
        .iter()
        .find(|g| g.account_id == monitor_account_id && g.scope_path == "/*")
        .ok_or_else(|| {
            GeneratorError::InvalidInput(
                "compact observation profile lacks a monitor-account grant in /*".to_owned(),
            )
        })?;

    let parent_descriptor = phase1_manifest
        .source_directory
        .iter()
        .find(|item| {
            item.scope_filter == "dept_prefix=/d" && phase1_q2_ids.contains(&item.slice_id)
        })
        .ok_or_else(|| GeneratorError::InvalidInput("missing dept_prefix=/d descriptor".to_owned()))?
        .clone();

    let parent_grant_replica = replica(cell, parent_grant, "p1:dept-finance-control")?;
    let parent_table =
        encode_compact_table_v5("grants", &[TableRowV5::Grant(parent_grant_replica)])?;
    let mut compact_parent_payload = episode.phase1_slices[&parent_descriptor.slice_id].clone();
    compact_parent_payload.tables = vec![parent_table];
    let mut compact_parent_descriptor = parent_descriptor.clone();
    compact_parent_descriptor.table_names = vec!["grants".to_owned()];
    compact_parent_descriptor.row_count = 1;

    let dept_grant_replica = replica(cell, dept_grant, "p1:dept-payroll-control")?;
    let dept_table = encode_compact_table_v5("grants", &[TableRowV5::Grant(dept_grant_replica)])?;
    let mut compact_dept_payload = episode.phase1_slices[&dept_descriptor.slice_id].clone();
    compact_dept_payload.tables = vec![dept_table];
    let mut compact_dept_descriptor = dept_descriptor.clone();
    compact_dept_descriptor.table_names = vec!["grants".to_owned()];
    compact_dept_descriptor.row_count = 1;

    let mut replacements = HashMap::new();
    replacements.insert(
        q1_core_descriptor.slice_id.clone(),
        compact_q1_core_descriptor,
    );
    replacements.insert(
        connector_descriptor.slice_id.clone(),
        compact_connector_descriptor,
    );
    replacements.insert(
        parent_descriptor.slice_id.clone(),
        compact_parent_descriptor,
    );
    replacements.insert(dept_descriptor.slice_id.clone(), compact_dept_descriptor);

    let mut next_phase1_manifest = phase1_manifest.clone();
    next_phase1_manifest.source_directory = phase1_manifest
        .source_directory
        .iter()
        .map(|item| replacements.get(&item.slice_id).cloned().unwrap_or_else(|| item.clone()))
        .collect();

    let mut next_phase1_slices = episode.phase1_slices.clone();
    next_phase1_slices.insert(q1_core_descriptor.slice_id, compact_q1_core_payload);
    next_phase1_slices.insert(connector_descriptor.slice_id, compact_connector_payload);
    next_phase1_slices.insert(parent_descriptor.slice_id, compact_parent_payload);
    next_phase1_slices.insert(dept_descriptor.slice_id, compact_dept_payload);

    let phase2_manifest = &episode.phase2_base_manifest;
    let phase2_q1_ids = obs_strategy_q1(phase2_manifest, &episode.policy_card, 2)?;
    let phase2_q1: Vec<DeliveredSliceV5> = phase2_q1_ids
        .iter()
        .map(|sid| episode.phase2_slices[sid].clone())
        .collect();
    let selected_q2_p2 = obs_strategy_q2(
        phase2_manifest,
        &phase2_q1,
        &phase2_manifest.work_queue,
        &episode.policy_card,
        2,
    )?;
    let app_descriptor = phase2_manifest
        .source_directory
        .iter()
        .find(|item| item.scope_filter == "app_id=fin" && selected_q2_p2.contains(&item.slice_id))
        .ok_or_else(|| GeneratorError::InvalidInput("missing phase2 app_id=fin descriptor".to_owned()))?
        .clone();
    let app_payload = &episode.phase2_slices[&app_descriptor.slice_id];
    let retained_tables: Vec<CompactTableV5> = app_payload
        .tables
        .iter()
        .filter(|table| table.table_name == "agent_delegation_requests")
        .cloned()
        .collect();
    if retained_tables.is_empty() {
        return Err(GeneratorError::InvalidInput(
            "compact observation profile lost its meaningful delegation Q2 row".to_owned(),
        ));
    }
    let mut compact_app_payload = app_payload.clone();
    compact_app_payload.tables = retained_tables.clone();
    let mut compact_app_descriptor = app_descriptor.clone();
    compact_app_descriptor.table_names = retained_tables
        .iter()
        .map(|t| t.table_name.clone())
        .collect();
    compact_app_descriptor.row_count =
        i64::try_from(retained_tables.iter().map(|t| t.rows.len()).sum::<usize>())
            .expect("row_count fits i64");

    let mut next_phase2_manifest = phase2_manifest.clone();
    next_phase2_manifest.source_directory = phase2_manifest
        .source_directory
        .iter()
        .map(|item| {
            if item.slice_id == app_descriptor.slice_id {
                compact_app_descriptor.clone()
            } else {
                item.clone()
            }
        })
        .collect();
    let mut next_phase2_slices = episode.phase2_slices.clone();
    next_phase2_slices.insert(app_descriptor.slice_id, compact_app_payload);

    let mut next_episode = episode.clone();
    next_episode.phase1_manifest = next_phase1_manifest;
    next_episode.phase1_slices = next_phase1_slices;
    next_episode.phase2_base_manifest = next_phase2_manifest;
    next_episode.phase2_slices = next_phase2_slices;
    Ok(next_episode)
}

pub(crate) fn collect_evidence_ids_in_value(value: &Value, found: &mut BTreeSet<String>) {
    match value {
        Value::Object(map) => {
            for (k, child) in map {
                if k == "evidence_id"
                    && let Some(ev) = child.as_str()
                {
                    found.insert(ev.to_owned());
                } else {
                    collect_evidence_ids_in_value(child, found);
                }
            }
        }
        Value::Array(items) => {
            for child in items {
                collect_evidence_ids_in_value(child, found);
            }
        }
        _ => {}
    }
}

pub(crate) fn collect_evidence_ids_in_slice_map(
    slices: &IndexMap<String, DeliveredSliceV5>,
    found: &mut BTreeSet<String>,
) -> Result<(), GeneratorError> {
    for slice in slices.values() {
        for table in &slice.tables {
            for row in decode_compact_table_v5(table)? {
                let v = row.to_strict_json_value()?;
                collect_evidence_ids_in_value(&v, found);
            }
        }
    }
    Ok(())
}

pub(crate) fn order_preserving_evidence_map(
    old_ids: &BTreeSet<String>,
    excluded_ids: &BTreeSet<String>,
) -> Result<HashMap<String, String>, GeneratorError> {
    let mut ordered: Vec<(u64, &str)> = Vec::new();
    for ev_id in old_ids {
        if excluded_ids.contains(ev_id) {
            continue;
        }
        let Some((prefix, suffix)) = ev_id.split_once('_') else {
            return Err(GeneratorError::InvalidInput(format!(
                "cannot compact nonnumeric evidence identifier {ev_id:?}"
            )));
        };
        if prefix != "ev" || suffix.is_empty() || !suffix.bytes().all(|b| b.is_ascii_digit()) {
            return Err(GeneratorError::InvalidInput(format!(
                "cannot compact nonnumeric evidence identifier {ev_id:?}"
            )));
        }
        let ordinal: u64 = suffix.parse().map_err(|_| {
            GeneratorError::InvalidInput(format!(
                "cannot compact nonnumeric evidence identifier {ev_id:?}"
            ))
        })?;
        ordered.push((ordinal, ev_id.as_str()));
    }
    ordered.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(b.1)));
    if ordered.len() > 999 {
        return Err(GeneratorError::InvalidInput(
            "compact observation profile supports at most 999 evidence identifiers".to_owned(),
        ));
    }
    Ok(ordered
        .into_iter()
        .enumerate()
        .map(|(index, (_, old_id))| (old_id.to_owned(), format!("ev_{:03}", index + 1)))
        .collect())
}

fn rewrite_citation(token: Value, mapping: &HashMap<String, String>) -> Value {
    let Some(s) = token.as_str() else {
        return token;
    };
    let Some((prefix, evidence_id)) = s.rsplit_once(':') else {
        return token;
    };
    if let Some(mapped) = mapping.get(evidence_id) {
        Value::String(format!("{prefix}:{mapped}"))
    } else {
        token
    }
}

pub(crate) fn rewrite_evidence_in_value(value: Value, mapping: &HashMap<String, String>) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::with_capacity(map.len());
            for (k, child) in map {
                let next = if k == "evidence_id" {
                    match child {
                        Value::String(s) => Value::String(mapping.get(&s).cloned().unwrap_or(s)),
                        other => other,
                    }
                } else if k == "cite" || k == "citations" {
                    match child {
                        Value::Array(arr) => Value::Array(
                            arr.into_iter()
                                .map(|tok| rewrite_citation(tok, mapping))
                                .collect(),
                        ),
                        other => rewrite_citation(other, mapping),
                    }
                } else {
                    rewrite_evidence_in_value(child, mapping)
                };
                out.insert(k, next);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|c| rewrite_evidence_in_value(c, mapping))
                .collect(),
        ),
        other => other,
    }
}

pub(crate) fn rewrite_model_evidence<T: StrictV5Model>(
    model: &T,
    mapping: &HashMap<String, String>,
) -> Result<T, GeneratorError> {
    let val = model.to_strict_json_value()?;
    let rewritten = rewrite_evidence_in_value(val, mapping);
    Ok(T::from_strict_json_value(&rewritten)?)
}

pub(crate) fn rewrite_slices_evidence(
    slices: &IndexMap<String, DeliveredSliceV5>,
    mapping: &HashMap<String, String>,
) -> Result<IndexMap<String, DeliveredSliceV5>, GeneratorError> {
    let mut out = IndexMap::with_capacity(slices.len());
    for (sid, slice) in slices {
        let mut next_tables = Vec::with_capacity(slice.tables.len());
        for table in &slice.tables {
            let decoded = decode_compact_table_v5(table)?;
            let mut next_rows = Vec::with_capacity(decoded.len());
            for row in decoded {
                let val = row.to_strict_json_value()?;
                let rewritten = rewrite_evidence_in_value(val, mapping);
                let next_row = match row {
                    TableRowV5::HrPerson(_) => {
                        TableRowV5::HrPerson(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::Role(_) => {
                        TableRowV5::Role(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::Grant(_) => {
                        TableRowV5::Grant(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::Group(_) => {
                        TableRowV5::Group(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::IdpAccount(_) => {
                        TableRowV5::IdpAccount(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::TransferBridge(_) => TableRowV5::TransferBridge(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::CompensatingControl(_) => TableRowV5::CompensatingControl(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::ConnectorSchema(_) => TableRowV5::ConnectorSchema(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::ConnectorPage(_) => TableRowV5::ConnectorPage(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::HrSourceMapping(_) => TableRowV5::HrSourceMapping(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::BaselineNhi(_) => {
                        TableRowV5::BaselineNhi(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::TargetNhi(_) => {
                        TableRowV5::TargetNhi(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::BaselineAgent(_) => TableRowV5::BaselineAgent(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::EnterpriseAgent(_) => TableRowV5::EnterpriseAgent(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::CloudCiemTask(_) => TableRowV5::CloudCiemTask(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::OptimizationTask(_) => TableRowV5::OptimizationTask(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::JmlWorkflowTask(_) => TableRowV5::JmlWorkflowTask(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::CaeIncident(_) => {
                        TableRowV5::CaeIncident(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::UarCampaign(_) => {
                        TableRowV5::UarCampaign(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::ChangeTicket(_) => TableRowV5::ChangeTicket(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::HistoricalSession(_) => TableRowV5::HistoricalSession(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                    TableRowV5::JitRequest(_) => {
                        TableRowV5::JitRequest(StrictV5Model::from_strict_json_value(&rewritten)?)
                    }
                    TableRowV5::AgentDelegationRequest(_) => TableRowV5::AgentDelegationRequest(
                        StrictV5Model::from_strict_json_value(&rewritten)?,
                    ),
                };
                next_rows.push(next_row);
            }
            next_tables.push(encode_compact_table_v5(&table.table_name, &next_rows)?);
        }
        let mut next_slice = slice.clone();
        next_slice.tables = next_tables;
        out.insert(sid.clone(), next_slice);
    }
    Ok(out)
}
