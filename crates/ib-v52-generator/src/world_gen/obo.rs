//! RFC 8693 OBO delegation requests and the Mechanism C relational OBO tables.

use ib_core::PyRandom;
use ib_v52_schemas::AgentDelegationRequestV52;
use indexmap::IndexMap;

use super::WorldCtx;
use super::dates::{next_day_iso, prev_day_iso};
use super::group_ids::RoleGroupIds;
use super::personas::Personas;
use super::types::{OboHopReceiptV52, OboSessionPriorActionV52, PtoDelegationEntryV52};

/// Mechanism C (`F10`/`F12`) relational OBO tables.
pub(super) struct OboRelationalTables {
    pub(super) obo_request_session_traces: IndexMap<String, String>,
    pub(super) obo_hop_receipts: Vec<OboHopReceiptV52>,
    pub(super) obo_session_prior_actions: Vec<OboSessionPriorActionV52>,
    pub(super) pto_delegation_register: Vec<PtoDelegationEntryV52>,
}

/// Build the OBO delegation requests with seed-shuffled neutral IDs.
pub(super) fn build_delegation_requests(
    ctx: &WorldCtx<'_>,
    rng: &mut PyRandom,
    agt_ids: &[String],
) -> (bool, Vec<AgentDelegationRequestV52>) {
    let WorldCtx { hardening, .. } = *ctx;
    let Personas {
        active_dir_1,
        active_dir_2,
        emerg_term_2hop,
        emerg_term_fallback,
        glv_mid_mgr,
        emerg_term_3hop,
        happy_path_requester,
        adm_eng_sod_user,
        adm_fin_uar_dual,
        ..
    } = ctx.personas.clone();
    let RoleGroupIds {
        sod_code_right_group,
        ..
    } = ctx.group_ids.clone();
    // 6 RFC 8693 OBO Delegation Requests (8 on F10/F12 adding paired 2-hop HITL control + 4th-hop boundary witness)
    // covering all 5 OboDelegationVerdictV52 outcomes with seed-shuffled neutral IDs.
    let has_obo_mech_c = hardening.relational_obo_tables;
    let dlreq_end = if has_obo_mech_c { 108usize } else { 106usize };
    let mut dlreq_ids: Vec<String> = (101..=dlreq_end).map(|i| format!("dlreq_{i}")).collect();
    rng.shuffle(&mut dlreq_ids);
    let mut agent_delegation_requests = vec![
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[0].clone(),
            initiating_human_person_key: happy_path_requester.clone(),
            actor_chain: vec![agt_ids[0].clone()],
            target_audience: "mcp://erp-finance-prod".to_owned(),
            requested_tool_scope: "mcp:erp:read_invoice".to_owned(),
            requires_hitl_cosign: false,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![
                "mcp:erp:read_invoice".to_owned(),
                "mcp:erp:draft_gl_entry".to_owned(),
            ],
            prior_human_or_agent_actions: vec![],
            proposed_approver_person_key: None,
            proposed_approver_delegate_of: None,
        },
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[1].clone(),
            initiating_human_person_key: happy_path_requester.clone(),
            actor_chain: vec![agt_ids[0].clone()],
            target_audience: "mcp://erp-finance-prod".to_owned(),
            requested_tool_scope: "mcp:erp:approve_payment".to_owned(),
            requires_hitl_cosign: true,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![
                "mcp:erp:read_invoice".to_owned(),
                "mcp:erp:approve_payment".to_owned(),
            ],
            prior_human_or_agent_actions: vec![],
            // On F10/F12, active_dir_2 has an EXPIRED PTO delegation (valid_until < cutover_date) for happy_path_requester
            // (whose manager is active_dir_1), so active_dir_2 remains valid and MUST be kept!
            proposed_approver_person_key: Some(if has_obo_mech_c {
                active_dir_2.clone()
            } else {
                active_dir_1.clone()
            }),
            proposed_approver_delegate_of: None,
        },
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[2].clone(),
            // On F10/F12, emerg_term_3hop (glv_mid_mgr -> loa_user -> ceo_vp) tests exact 3-hop manager resolution
            // after active PTO delegate happy_path_requester is disqualified!
            initiating_human_person_key: if has_obo_mech_c {
                emerg_term_3hop.clone()
            } else {
                adm_fin_uar_dual.clone()
            },
            actor_chain: vec![agt_ids[1].clone()],
            target_audience: "mcp://itsm-snow-prod".to_owned(),
            requested_tool_scope: "mcp:itsm:reassign_queue".to_owned(),
            requires_hitl_cosign: true,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![
                "mcp:itsm:read_ticket".to_owned(),
                "mcp:itsm:reassign_queue".to_owned(),
            ],
            prior_human_or_agent_actions: vec![],
            proposed_approver_person_key: Some(happy_path_requester.clone()),
            proposed_approver_delegate_of: Some(if has_obo_mech_c {
                emerg_term_3hop.clone()
            } else {
                adm_fin_uar_dual.clone()
            }),
        },
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[3].clone(),
            initiating_human_person_key: adm_eng_sod_user.clone(),
            actor_chain: vec![agt_ids[0].clone(), agt_ids[1].clone()],
            target_audience: "mcp://itsm-snow-prod".to_owned(),
            requested_tool_scope: sod_code_right_group.clone(),
            requires_hitl_cosign: true,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![sod_code_right_group.clone()],
            prior_human_or_agent_actions: vec!["mcp:itsm:read_ticket".to_owned()],
            proposed_approver_person_key: Some(active_dir_1.clone()),
            proposed_approver_delegate_of: None,
        },
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[4].clone(),
            initiating_human_person_key: happy_path_requester.clone(),
            actor_chain: vec![agt_ids[0].clone(), agt_ids[1].clone(), agt_ids[0].clone()],
            target_audience: "mcp://itsm-snow-prod".to_owned(),
            requested_tool_scope: "mcp:itsm:read_ticket".to_owned(),
            requires_hitl_cosign: false,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec!["mcp:itsm:read_ticket".to_owned()],
            prior_human_or_agent_actions: vec![],
            proposed_approver_person_key: None,
            proposed_approver_delegate_of: None,
        },
        AgentDelegationRequestV52 {
            request_id: dlreq_ids[5].clone(),
            initiating_human_person_key: happy_path_requester.clone(),
            actor_chain: vec![agt_ids[3].clone()],
            target_audience: "mcp://erp-finance-prod".to_owned(),
            requested_tool_scope: "mcp:erp:read_fx".to_owned(),
            requires_hitl_cosign: false,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec!["mcp:erp:read_fx".to_owned()],
            prior_human_or_agent_actions: vec![],
            proposed_approver_person_key: None,
            proposed_approver_delegate_of: None,
        },
    ];
    if has_obo_mech_c {
        // Paired 2-hop HITL positive control matching dlreq_ids[3] without the adm_* nested SoD edge:
        // proposed approver active_dir_2 is an ACTIVE PTO delegate of emerg_term_2hop -> walks 2 hops
        // (garden_leave_mgr -> active_dir_1) -> ApproveWithHitlCosign(active_dir_1)!
        agent_delegation_requests.push(AgentDelegationRequestV52 {
            request_id: dlreq_ids[6].clone(),
            initiating_human_person_key: emerg_term_2hop.clone(),
            actor_chain: vec![agt_ids[0].clone(), agt_ids[1].clone()],
            target_audience: "mcp://itsm-snow-prod".to_owned(),
            requested_tool_scope: sod_code_right_group.clone(),
            requires_hitl_cosign: true,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![sod_code_right_group.clone()],
            prior_human_or_agent_actions: vec!["mcp:itsm:read_ticket".to_owned()],
            proposed_approver_person_key: Some(active_dir_2.clone()),
            proposed_approver_delegate_of: Some(emerg_term_2hop.clone()),
        });
        // 4th-hop boundary witness: emerg_term_fallback (terminated_regular -> glv_mid_mgr -> loa_user -> ceo_vp at hop 4 > 3!)
        // with inactive proposed approver glv_mid_mgr -> no active manager within 3 hops -> DenyInactiveSponsorOrActor!
        agent_delegation_requests.push(AgentDelegationRequestV52 {
            request_id: dlreq_ids[7].clone(),
            initiating_human_person_key: emerg_term_fallback.clone(),
            actor_chain: vec![agt_ids[1].clone()],
            target_audience: "mcp://itsm-snow-prod".to_owned(),
            requested_tool_scope: "mcp:itsm:reassign_queue".to_owned(),
            requires_hitl_cosign: true,
            max_allowed_hops: 2,
            granted_upstream_scopes: vec![
                "mcp:itsm:read_ticket".to_owned(),
                "mcp:itsm:reassign_queue".to_owned(),
            ],
            prior_human_or_agent_actions: vec![],
            proposed_approver_person_key: Some(glv_mid_mgr.clone()),
            proposed_approver_delegate_of: None,
        });
    }
    agent_delegation_requests.sort_by(|a, b| a.request_id.cmp(&b.request_id));

    (has_obo_mech_c, agent_delegation_requests)
}

/// Mechanism C: relational OBO envelopes, out-of-order hop receipts, session prior actions & PTO register.
pub(super) fn build_obo_relational_tables(
    ctx: &WorldCtx<'_>,
    rng: &mut PyRandom,
    has_obo_mech_c: bool,
    agent_delegation_requests: &[AgentDelegationRequestV52],
) -> OboRelationalTables {
    let WorldCtx { client, .. } = *ctx;
    let Personas {
        active_dir_2,
        emerg_term_2hop,
        emerg_term_3hop,
        happy_path_requester,
        ..
    } = ctx.personas.clone();
    // Mechanism C (`F10`/`F12`): Relational OBO envelopes, out-of-order hop receipts, session prior actions & PTO register.
    let mut obo_request_session_traces: IndexMap<String, String> = IndexMap::new();
    let mut obo_hop_receipts: Vec<OboHopReceiptV52> = Vec::new();
    let mut obo_session_prior_actions: Vec<OboSessionPriorActionV52> = Vec::new();
    let mut pto_delegation_register: Vec<PtoDelegationEntryV52> = Vec::new();
    if has_obo_mech_c {
        for (idx, req) in agent_delegation_requests.iter().enumerate() {
            let trace_id = format!("tr{:02}", idx + 1);
            obo_request_session_traces.insert(req.request_id.clone(), trace_id.clone());
            for (h_idx, agent_id) in req.actor_chain.iter().enumerate() {
                let mut hop_scopes = req.granted_upstream_scopes.clone();
                if h_idx == 0 && req.actor_chain.len() > 1 {
                    // Earlier hop had a superset scope before attenuation at the final hop.
                    if !hop_scopes.iter().any(|s| s == "mcp:itsm:read_ticket") {
                        hop_scopes.push("mcp:itsm:read_ticket".to_owned());
                    }
                }
                obo_hop_receipts.push(OboHopReceiptV52 {
                    request_id: req.request_id.clone(),
                    hop_index: (h_idx + 1) as u32,
                    agent_id: agent_id.clone(),
                    attenuated_scopes: hop_scopes,
                });
            }
            for act in &req.prior_human_or_agent_actions {
                obo_session_prior_actions.push(OboSessionPriorActionV52 {
                    session_trace_id: trace_id.clone(),
                    executed_scope: act.clone(),
                });
            }
        }
        rng.shuffle(&mut obo_hop_receipts);
        pto_delegation_register = vec![
            // EXPIRED PTO delegation (valid_until < cutover_date) -> active_dir_2 MUST remain valid on dlreq_ids[1]!
            PtoDelegationEntryV52 {
                delegator_person_key: happy_path_requester.clone(),
                delegate_person_key: active_dir_2.clone(),
                valid_from: "2026-10-20".to_owned(),
                valid_until: prev_day_iso(&client.cutover_date, 3),
            },
            // ACTIVE PTO delegation spanning cutover_date -> disqualifies happy_path_requester on dlreq_ids[2] (3-hop walk -> ceo_vp)!
            PtoDelegationEntryV52 {
                delegator_person_key: emerg_term_3hop.clone(),
                delegate_person_key: happy_path_requester.clone(),
                valid_from: prev_day_iso(&client.cutover_date, 2),
                valid_until: next_day_iso(&client.cutover_date, 5),
            },
            // ACTIVE PTO delegation spanning cutover_date -> disqualifies active_dir_2 on dlreq_ids[6] (2-hop walk -> active_dir_1)!
            PtoDelegationEntryV52 {
                delegator_person_key: emerg_term_2hop.clone(),
                delegate_person_key: active_dir_2.clone(),
                valid_from: prev_day_iso(&client.cutover_date, 1),
                valid_until: next_day_iso(&client.cutover_date, 4),
            },
        ];
    }

    OboRelationalTables {
        obo_request_session_traces,
        obo_hop_receipts,
        obo_session_prior_actions,
        pto_delegation_register,
    }
}
