use ib_v5_generator::{
    DEFAULT_V5_SEED, V5_SUITE_COUNTS, V5Cell, V5Suite, build_v5_episode, plan_v5_cells,
};
use ib_v5_oracle::{
    PhaseContext, evaluate_phase_with_trace, initialize_working_state, phase2_feasibility,
};
use ib_v5_schemas::{
    AgentDelegationDecisionKindV5, AgentRegistryActionV5, AtomicMutationV5, JITDecisionKindV5,
    UARDecisionKindV5,
};

fn dev_cell(family_id: &str) -> V5Cell {
    plan_v5_cells(V5Suite::V5Dev, DEFAULT_V5_SEED)
        .into_iter()
        .find(|c| c.family_id == family_id)
        .expect("family cell exists")
}

#[test]
fn test_f07_retro_shock_taints_ticket_and_strips_exact_bridge() {
    let cell = dev_cell("F07");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F07");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;
    let cae = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::CaeContainment(c) => Some(c),
            _ => None,
        })
        .expect("CAE mutation");
    assert_eq!(cae.retro_invalidated_ids.len(), 2);

    let working = initialize_working_state(PhaseContext::new(world.clone(), queue.clone(), 2))
        .expect("init working");
    assert!(!working.postshock_stripped_ids.is_empty());

    let mut no_shock_world = world.clone();
    for person in no_shock_world.hr_persons.values_mut() {
        for eng in &mut person.engagements {
            eng.retro_terminated_effective_s = None;
        }
    }
    let no_shock = evaluate_phase_with_trace(&no_shock_world, queue, &[]).expect("no shock");
    let no_shock_work =
        initialize_working_state(PhaseContext::new(no_shock_world, queue.clone(), 2))
            .expect("no shock work");
    assert!(no_shock_work.postshock_stripped_ids.is_empty());

    let first_req_id = world
        .jit_requests
        .values()
        .find(|r| r.eval_seq == 1)
        .map(|r| r.request_id.clone())
        .expect("first req");
    let key = ("JIT_DECISION_MUTATION".to_owned(), first_req_id.clone());
    let AtomicMutationV5::JitDecision(ticketed) = oracle
        .gold_p2_mutations
        .iter()
        .find(|m| {
            m.mutation_kind() == "JIT_DECISION_MUTATION" && m.target_entity_id() == first_req_id
        })
        .expect("ticketed")
    else {
        panic!("expected JitDecision");
    };
    let AtomicMutationV5::JitDecision(ticketed_no_shock) = &no_shock.trace_by_target[&key].mutation
    else {
        panic!("expected JitDecision");
    };
    assert_eq!(ticketed.jit_decision, JITDecisionKindV5::Deny);
    assert_ne!(ticketed_no_shock.jit_decision, JITDecisionKindV5::Deny);
}

#[test]
fn test_f08_recursive_ack_binding_and_half_open_mint_cutoff() {
    let cell = dev_cell("F08");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F08");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;
    let baseline = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::CaeContainment(c) => Some(c),
            _ => None,
        })
        .expect("baseline CAE");
    assert_eq!(baseline.cae_computed_exposure_s, 100);

    let incident_id = world.cae_incidents.keys().next().expect("inc").clone();
    let mut no_ack_world = world.clone();
    for tok in &mut no_ack_world
        .cae_incidents
        .get_mut(&incident_id)
        .expect("inc")
        .tokens
    {
        tok.revoke_ack_at_s = None;
    }
    let no_ack = evaluate_phase_with_trace(&no_ack_world, queue, &[]).expect("no ack");
    let no_ack_mut = no_ack
        .mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::CaeContainment(c) => Some(c),
            _ => None,
        })
        .expect("no ack CAE");
    assert!(no_ack_mut.cae_computed_exposure_s > baseline.cae_computed_exposure_s);
}

#[test]
fn test_f09_deny_group_changes_sequential_standing_and_eligible_quorums() {
    let cell = dev_cell("F09");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F09");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;
    assert_eq!(queue.target_uar_campaign_ids.len(), 2);
    let early_id = &queue.target_uar_campaign_ids[0];
    let late_id = &queue.target_uar_campaign_ids[1];

    let early = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::UarCampaign(u) if u.target_entity_id == *early_id => Some(u),
            _ => None,
        })
        .expect("early UAR");
    let late = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::UarCampaign(u) if u.target_entity_id == *late_id => Some(u),
            _ => None,
        })
        .expect("late UAR");
    assert!(early.emergency_custodian_account_id.is_some());
    assert!(late.emergency_custodian_account_id.is_none());

    let mut late_only_queue = queue.clone();
    late_only_queue.target_uar_campaign_ids = vec![late_id.clone()];
    let late_only = evaluate_phase_with_trace(world, &late_only_queue, &[]).expect("late only");
    let late_only_mut = late_only
        .mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::UarCampaign(u) if u.target_entity_id == *late_id => Some(u),
            _ => None,
        })
        .expect("late only mut");
    assert_eq!(late_only_mut.uar_decisions[0].1, UARDecisionKindV5::Revoke);
    assert!(late_only_mut.emergency_custodian_account_id.is_some());
}

#[test]
fn test_f10_inherited_lock_requires_nonconflicting_emergency_custodian() {
    let cell = dev_cell("F10");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F10");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;
    let campaign_id = &queue.target_uar_campaign_ids[0];
    let uar_mut = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::UarCampaign(u) if u.target_entity_id == *campaign_id => Some(u),
            _ => None,
        })
        .expect("F10 UAR");
    assert!(uar_mut.emergency_custodian_account_id.is_some());
    assert!(
        uar_mut
            .uar_decisions
            .iter()
            .any(|(_, d)| *d == UARDecisionKindV5::Escalate)
    );

    let mut no_cand_world = world.clone();
    no_cand_world
        .uar_campaigns
        .get_mut(campaign_id)
        .expect("campaign")
        .candidate_custodian_account_ids
        .clear();
    let feas = phase2_feasibility(&no_cand_world, queue).expect("feas check");
    assert!(!feas.feasible);
}

#[test]
fn test_f11_shared_break_glass_cooldown_isolated_from_static_sod() {
    let cell = dev_cell("F11");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F11");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;

    let mut no_history_world = world.clone();
    no_history_world.historical_sessions.clear();
    let no_history = evaluate_phase_with_trace(&no_history_world, queue, &[]).expect("no history");
    let replay_req_id = world
        .jit_requests
        .values()
        .find(|r| r.eval_seq == 3)
        .map(|r| r.request_id.clone())
        .expect("replay req");
    let baseline = oracle
        .gold_p2_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::JitDecision(j) if j.target_entity_id == replay_req_id => Some(j),
            _ => None,
        })
        .expect("baseline replay");
    assert_eq!(baseline.jit_decision, JITDecisionKindV5::Deny);

    let AtomicMutationV5::JitDecision(no_hist_mut) =
        &no_history.trace_by_target[&("JIT_DECISION_MUTATION".to_owned(), replay_req_id)].mutation
    else {
        panic!("expected JitDecision");
    };
    assert_eq!(
        no_hist_mut.jit_decision,
        JITDecisionKindV5::ApproveJitCosign
    );
}

#[test]
fn test_f12_registered_root_then_d3_and_d2_hop_gates() {
    let cell = dev_cell("F12");
    let (episode, oracle) = build_v5_episode(&cell).expect("build F12");
    let world = &oracle.omniscient_s2_gold;
    let queue = &episode.phase2_base_manifest.work_queue;

    let registry = oracle
        .gold_p1_mutations
        .iter()
        .find_map(|m| match m {
            AtomicMutationV5::AgentRegistry(a) => Some(a),
            _ => None,
        })
        .expect("AgentRegistry");
    assert_eq!(registry.agent_action, AgentRegistryActionV5::RegisterActive);

    let root_id = world
        .agent_delegation_requests
        .values()
        .find(|r| r.parent_delegation_request_id.is_none())
        .map(|r| r.request_id.clone())
        .expect("root");
    let child_id = world
        .agent_delegation_requests
        .values()
        .find(|r| r.parent_delegation_request_id.is_some())
        .map(|r| r.request_id.clone())
        .expect("child");

    let normal = evaluate_phase_with_trace(world, queue, &[]).expect("normal");
    let AtomicMutationV5::AgentDelegation(root_mut) =
        &normal.trace_by_target[&("AGENT_DELEGATION_MUTATION".to_owned(), root_id)].mutation
    else {
        panic!("expected AgentDelegation");
    };
    let child_trace =
        &normal.trace_by_target[&("AGENT_DELEGATION_MUTATION".to_owned(), child_id.clone())];
    let AtomicMutationV5::AgentDelegation(child_mut) = &child_trace.mutation else {
        panic!("expected AgentDelegation");
    };
    assert_eq!(
        root_mut.delegation_decision,
        AgentDelegationDecisionKindV5::ApproveDelegationCosign
    );
    assert!(root_mut.hitl_cosigner_account_id.is_some());
    assert_eq!(
        child_mut.delegation_decision,
        AgentDelegationDecisionKindV5::DenyDelegation
    );
    assert_eq!(child_trace.context.failing_predicate.as_deref(), Some("D3"));

    let mut no_temporal = world.clone();
    no_temporal.overlay.temporal_sod_rules.clear();
    let valid_res = evaluate_phase_with_trace(&no_temporal, queue, &[]).expect("valid child");
    let AtomicMutationV5::AgentDelegation(valid_child) = &valid_res.trace_by_target
        [&("AGENT_DELEGATION_MUTATION".to_owned(), child_id.clone())]
        .mutation
    else {
        panic!("expected AgentDelegation");
    };
    assert_eq!(
        valid_child.delegation_decision,
        AgentDelegationDecisionKindV5::ApproveDelegation
    );

    let mut overwide = no_temporal.clone();
    overwide
        .agent_delegation_requests
        .get_mut(&child_id)
        .expect("child")
        .requested_scope_path = "/d".to_owned();
    let overwide_res = evaluate_phase_with_trace(&overwide, queue, &[]).expect("overwide");
    let overwide_trace =
        &overwide_res.trace_by_target[&("AGENT_DELEGATION_MUTATION".to_owned(), child_id.clone())];
    let AtomicMutationV5::AgentDelegation(overwide_mut) = &overwide_trace.mutation else {
        panic!("expected AgentDelegation");
    };
    assert_eq!(
        overwide_mut.delegation_decision,
        AgentDelegationDecisionKindV5::DenyDelegation
    );
    assert_eq!(
        overwide_trace.context.failing_predicate.as_deref(),
        Some("D2")
    );

    let mut wrong_hop = no_temporal;
    wrong_hop
        .agent_delegation_requests
        .get_mut(&child_id)
        .expect("child")
        .delegation_hop += 1;
    let wrong_hop_res = evaluate_phase_with_trace(&wrong_hop, queue, &[]).expect("wrong hop");
    let wrong_hop_trace =
        &wrong_hop_res.trace_by_target[&("AGENT_DELEGATION_MUTATION".to_owned(), child_id)];
    assert_eq!(
        wrong_hop_trace.context.failing_predicate.as_deref(),
        Some("D2")
    );

    let _ = V5_SUITE_COUNTS;
}
