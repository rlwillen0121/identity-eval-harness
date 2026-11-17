use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

use ib_core::hashing::sha256_hex;
use ib_core::jcs_dumps;
use ib_v5_accounting::{
    ProviderAccountingTraceV5, USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingStatusV5,
    V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA, V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
    V5_USAGE_ACCOUNTING_POLICY_ID, provider_fingerprint_sha256_v5,
};
use ib_v5_runtime::{RuntimeV5, cosign_requirements};
use ib_v5_schemas::{
    AtomicMutationV5, AttemptStatusV5, CostBasisV5, DescriptiveUsageSummaryV5, EpisodeV5,
    HistoricalComparatorEntryV5, HistoricalComparatorManifestV5, NormalizedDecodeStatusV5,
    ProviderRequestKindV5, ProviderRequestStatusV5, ProviderRequestUsageV5, SealedEpisodeOracleV5,
    StrictV5Model, T0_UNIFIED_PROFILE_V5, UsageScopeV5,
};
use ib_v5_scoring::{
    ATTEMPT_STATUSES_V5, AggregateRunOptionsV5, BuildEpisodeAttemptOptionsV5, CATEGORY_IDS_V5,
    CORE_BOOTSTRAP_REPLICATES_V5, CORE_BOOTSTRAP_SEED_V5, CORE_FAMILIES_V5, CORE_REGIMES_V5,
    CompletionTraceV5, ConfirmatoryFamilyKindV5, ConfirmatoryFamilySpecV5, EpisodeExecutionV5,
    EpisodeScoreV5, EpisodeScoringInputV5, FamilyMetricInputV5, MetricCellsV5,
    PLANNED_REPEATS_MODEL_V5, PhaseSnapshotSetV5, PhaseTrajectoryV5, PricingRatesV5,
    RunCalibrationMetadataV5, RunScoringContextV5, ScoreExecutionOptionsV5, SnapshotAssessmentV5,
    TargetKeyV5, TrajectorySnapshotV5, aggregate_run_scores, benjamini_hochberg_v5,
    build_episode_attempt_v5, compare_v5_core_models, compare_v5_full_models,
    describe_unregistered_contrasts_v5, episode_score_from_mapping, episode_score_to_mapping,
    episode_scoring_input_from_mapping, episode_scoring_input_to_mapping,
    evaluate_confirmatory_family_v5, exact_family_sign_flip_pvalue_v5, family_cluster_bootstrap_v5,
    score_categories_for_episode_v5, score_categories_v5, score_episode, score_execution_raw_v5,
    score_execution_v5, select_v5_full_escalation_pairs, verify_usage_accounting_artifacts_v5,
};
use indexmap::IndexMap;
use serde_json::{Value, json};

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("workspace root")
        .to_path_buf()
}

fn load_v5_dev_f06_episode() -> (EpisodeV5, SealedEpisodeOracleV5) {
    let root = workspace_root();
    let ep_file = File::open(root.join("datasets/v5_dev/episodes.jsonl")).expect("open episodes");
    let or_file = File::open(root.join("datasets/v5_dev/oracles.jsonl")).expect("open oracles");
    let mut target_ep: Option<EpisodeV5> = None;
    for line in BufReader::new(ep_file).lines() {
        let line = line.expect("read line");
        if line.trim().is_empty() {
            continue;
        }
        let ep = EpisodeV5::from_strict_json_str(&line).expect("parse EpisodeV5");
        if ep.family_id == "F06" {
            target_ep = Some(ep);
            break;
        }
    }
    let episode = target_ep.expect("F06 episode in v5_dev");
    let mut target_oracle: Option<SealedEpisodeOracleV5> = None;
    for line in BufReader::new(or_file).lines() {
        let line = line.expect("read line");
        if line.trim().is_empty() {
            continue;
        }
        let orc = SealedEpisodeOracleV5::from_strict_json_str(&line).expect("parse oracle");
        if orc.episode_id == episode.episode_id {
            target_oracle = Some(orc);
            break;
        }
    }
    let oracle = target_oracle.expect("matching F06 oracle");
    (episode, oracle)
}

fn replay_gold_execution(
    episode: &EpisodeV5,
    oracle: &SealedEpisodeOracleV5,
) -> EpisodeExecutionV5 {
    let mut runtime =
        RuntimeV5::new(episode.clone(), oracle.omniscient_s1.clone()).expect("runtime");
    let r1 = runtime.query_sources(1, &oracle.req_slices_p1).expect("q1");
    assert_eq!(format!("{:?}", r1.status), "Ok");
    let s1 = runtime
        .stage_phase_mutations(1, "stg_p1", &oracle.gold_p1_mutations)
        .expect("stage p1");
    assert_eq!(format!("{:?}", s1.status), "Staged");
    for (scope_kind, target_id, actor_id) in cosign_requirements(&oracle.gold_p1_mutations) {
        let cs = runtime.request_cosign(1, "stg_p1", scope_kind, &target_id, &actor_id);
        assert_eq!(format!("{:?}", cs.status), "CoSigned");
    }
    let c1 = runtime
        .commit_phase_mutations(1, "stg_p1")
        .expect("commit p1");
    assert_eq!(format!("{:?}", c1.status), "CommittedAndAdvanced");

    let r2 = runtime.query_sources(2, &oracle.req_slices_p2).expect("q2");
    assert_eq!(format!("{:?}", r2.status), "Ok");
    let s2 = runtime
        .stage_phase_mutations(2, "stg_p2", &oracle.gold_p2_mutations)
        .expect("stage p2");
    assert_eq!(format!("{:?}", s2.status), "Staged");
    for (scope_kind, target_id, actor_id) in cosign_requirements(&oracle.gold_p2_mutations) {
        let cs = runtime.request_cosign(2, "stg_p2", scope_kind, &target_id, &actor_id);
        assert_eq!(format!("{:?}", cs.status), "CoSigned");
    }
    let c2 = runtime
        .commit_phase_mutations(2, "stg_p2")
        .expect("commit p2");
    assert_eq!(format!("{:?}", c2.status), "EpisodeCompleted");

    execution_from_runtime(&runtime, episode)
}

fn phase_trajectory_from_runtime(runtime: &RuntimeV5, phase_index: i64) -> PhaseTrajectoryV5 {
    let Some(snaps) = runtime.phase_snapshots(phase_index) else {
        return PhaseTrajectoryV5::empty(phase_index);
    };
    let mut attempts: Vec<TrajectorySnapshotV5> = snaps
        .stage_attempts
        .iter()
        .map(TrajectorySnapshotV5::from_runtime_snapshot)
        .collect();
    if let Some(c) = &snaps.commit {
        attempts.push(TrajectorySnapshotV5::from_runtime_snapshot(c));
    }
    PhaseTrajectoryV5 {
        phase_index,
        attempts,
        stage_attempts: snaps
            .stage_attempts
            .iter()
            .map(TrajectorySnapshotV5::from_runtime_snapshot)
            .collect(),
        first_stage: snaps
            .first_stage
            .as_ref()
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
        last_stage: snaps
            .last_stage
            .as_ref()
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
        commit: snaps
            .commit
            .as_ref()
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
    }
}

fn execution_from_runtime(runtime: &RuntimeV5, episode: &EpisodeV5) -> EpisodeExecutionV5 {
    EpisodeExecutionV5 {
        episode_id: episode.episode_id.clone(),
        suite_id: "v5_dev".to_owned(),
        family_id: episode.family_id.clone(),
        model_id: "baseline:oracle_team_replay".to_owned(),
        provider_kind: "baseline".to_owned(),
        endpoint_identity: "injected-baseline".to_owned(),
        topology_id: "T0_SINGLE_MONOLITH".to_owned(),
        steering_mode: "UNSTEERED".to_owned(),
        wave_execution_mode: "COUPLED".to_owned(),
        effort: "medium".to_owned(),
        execution_valid: true,
        provider_error: false,
        parse_failure: false,
        output_budget_exceeded: Some(false),
        usage_provenance_complete: true,
        termination_reason: "EPISODE_COMPLETED".to_owned(),
        error_message: None,
        turns_used: 0,
        tool_calls_requested: 6,
        tool_calls_executed: 6,
        max_calls_in_turn: 2,
        latency_s: 0.01,
        input_tokens_uncached: Some(0),
        cached_input_tokens: Some(0),
        input_tokens_actual: Some(0),
        output_tokens: Some(0),
        reasoning_tokens: Some(0),
        output_tokens_actual: Some(0),
        cost_usd_micros: Some(0),
        provider_reported_cost_usd: None,
        pricing_manifest_sha256: None,
        phase1_committed: true,
        phase2_opened: true,
        phase2_committed: true,
        phase2_state_feasible: Some(true),
        phase1: phase_trajectory_from_runtime(runtime, 1),
        phase2: phase_trajectory_from_runtime(runtime, 2),
        completion_trace: Vec::new(),
        tool_trace: Vec::new(),
        score: None,
        scoring_input: None,
        usage_accounting_policy_id: None,
        usage_accounting_status: UsageAccountingStatusV5::NotApplicable,
        provider_accounting: Vec::new(),
        attempt: None,
        provider_requests: Vec::new(),
        attempt_status: AttemptStatusV5::Completed,
        terminal_cause: None,
        context_limit_cause: None,
        terminal_reservation_ordinal: None,
        completed_assistant_generations: 0,
        retry_request_count: 0,
        elapsed_wall_time_ms: None,
        terminal_outcome_elapsed_ms: None,
        active_elapsed_ms: None,
        primary_prompt_bundle_sha256: None,
        topology_profile_sha256: None,
        repeat_index: None,
        seed_index: None,
    }
}

fn default_targets() -> Vec<TargetKeyV5> {
    vec![
        ("ACCOUNT_RECON_MUTATION".to_owned(), "acct_01".to_owned()),
        ("JIT_DECISION_MUTATION".to_owned(), "req_01".to_owned()),
    ]
}

fn make_assessment(
    stages: [bool; 5],
    semantic: Option<BTreeMap<TargetKeyV5, bool>>,
    overgrant: Option<BTreeMap<TargetKeyV5, bool>>,
    availability: Option<BTreeMap<TargetKeyV5, bool>>,
    valid: Option<BTreeMap<TargetKeyV5, bool>>,
) -> SnapshotAssessmentV5 {
    let targets = default_targets();
    let all_true: BTreeMap<TargetKeyV5, bool> = targets.iter().map(|t| (t.clone(), true)).collect();
    let all_false: BTreeMap<TargetKeyV5, bool> =
        targets.iter().map(|t| (t.clone(), false)).collect();
    SnapshotAssessmentV5::new(
        stages[0],
        stages[1],
        stages[2],
        stages[3],
        stages[4],
        "a".repeat(64),
        semantic.unwrap_or_else(|| all_true.clone()),
        valid.unwrap_or(all_true),
        overgrant.unwrap_or_else(|| all_false.clone()),
        availability.unwrap_or(all_false),
        None,
        None,
    )
    .expect("valid assessment")
}

fn make_phase(assessment: Option<SnapshotAssessmentV5>) -> PhaseSnapshotSetV5 {
    PhaseSnapshotSetV5::new(assessment.clone(), assessment.clone(), assessment)
        .expect("valid phase")
}

fn make_episode_input() -> EpisodeScoringInputV5 {
    let valid = make_assessment([true; 5], None, None, None, None);
    EpisodeScoringInputV5 {
        episode_id: "v5_core_F01_R1".to_owned(),
        suite_id: "v5_core".to_owned(),
        family_id: "F01".to_owned(),
        regime_id: "R1".to_owned(),
        phase1: make_phase(Some(valid.clone())),
        phase2: make_phase(Some(valid)),
        p1_committed: true,
        p2_opened: true,
        p2_committed: true,
        phase2_state_feasible: Some(true),
        g_prop_coupled_pass: Some(true),
        g_prop_reset_pass: Some(true),
        propagation_mode: "COUPLED".to_owned(),
        budget_exceeded: Some(false),
        input_tokens_actual: Some(120),
        output_tokens_actual: Some(30),
        cost_usd_micros: Some(20),
        execution_valid: Some(true),
        provider_error: None,
        parse_failure: None,
        termination_reason: None,
        safety_attempt_assessments: Vec::new(),
        safety_attempt_stream_complete: None,
        repeat_index: 1,
        attempt_status: "COMPLETED".to_owned(),
        category_pass_by_category: None,
        seed_index: None,
    }
}

fn core_baseline_scores(
    g_srr_success_count: usize,
    semantic_success_count: usize,
    action_success_count: usize,
    both_phases_staged_count: usize,
) -> Vec<EpisodeScoreV5> {
    let base = score_episode(&make_episode_input()).expect("score");
    let mut out = Vec::new();
    let mut idx = 0;
    for family in CORE_FAMILIES_V5 {
        for regime in CORE_REGIMES_V5 {
            let mut row = base.clone();
            row.episode_id = format!("v5_core_{family}_{regime}");
            row.family_id = family.to_owned();
            row.regime_id = regime.to_owned();
            row.g_srr_commit = Some(i64::from(idx < g_srr_success_count));
            row.sem_srr_commit = Some(i64::from(idx < semantic_success_count));
            row.action_srr_last_stage = Some(i64::from(idx < action_success_count));
            row.p1_staged = 1;
            row.p2_observed = 1;
            row.both_phases_staged = i64::from(idx < both_phases_staged_count);
            out.push(row);
            idx += 1;
        }
    }
    out
}

fn baseline_context(baseline_id: &str) -> RunScoringContextV5 {
    RunScoringContextV5 {
        suite_id: "v5_core".to_owned(),
        model_id: format!("baseline:{baseline_id}"),
        reasoning_effort: "medium".to_owned(),
        topology: "T0_SINGLE_MONOLITH".to_owned(),
        steering_mode: "UNSTEERED".to_owned(),
        pricing_manifest_sha256: None,
        model_tier: None,
        baseline_id: Some(baseline_id.to_owned()),
        wave_execution_mode: None,
        run_config_sha256: None,
        planned_repeats_per_episode: 1,
        comparison_track: "PRIMARY_HIGHEST".to_owned(),
        topology_profile_sha256: None,
        primary_prompt_bundle_sha256: None,
        max_generations: None,
        cap_disclosed: false,
        expected_episode_grid: None,
        calibration_metadata: None,
    }
}

#[test]
fn test_baseline_gates_and_thresholds() {
    for bid in ["metadata_newest_selector_greedy", "noop_team"] {
        let rows = core_baseline_scores(0, 0, 0, 48);
        let summary = aggregate_run_scores(
            &rows,
            &baseline_context(bid),
            AggregateRunOptionsV5::default(),
        )
        .expect("agg");
        assert_eq!(summary.gate_status["G-BASE-01-NUMERIC"], "PASS");
        assert_eq!(summary.gate_status["G-BASE-01"], "UNMEASURED");
    }

    // Threshold boundaries
    for (pass_args, fail_args, staged) in [
        ((5, 0, 0), (6, 0, 0), 48),
        ((0, 12, 0), (0, 13, 0), 48),
        ((0, 0, 13), (0, 0, 14), 48),
        ((0, 0, 6), (0, 0, 7), 24),
    ] {
        let ctx = baseline_context("metadata_newest_selector_greedy");
        let p = aggregate_run_scores(
            &core_baseline_scores(pass_args.0, pass_args.1, pass_args.2, staged),
            &ctx,
            AggregateRunOptionsV5::default(),
        )
        .expect("pass");
        let f = aggregate_run_scores(
            &core_baseline_scores(fail_args.0, fail_args.1, fail_args.2, staged),
            &ctx,
            AggregateRunOptionsV5::default(),
        )
        .expect("fail");
        assert_eq!(p.gate_status["G-BASE-01-NUMERIC"], "PASS");
        assert_eq!(p.gate_status["G-BASE-01"], "UNMEASURED");
        assert_eq!(f.gate_status["G-BASE-01-NUMERIC"], "FAIL");
        assert_eq!(f.gate_status["G-BASE-01"], "FAIL");
    }

    // Noop only checks G-SRR == 0.0
    let noop = aggregate_run_scores(
        &core_baseline_scores(0, 48, 48, 48),
        &baseline_context("noop_team"),
        AggregateRunOptionsV5::default(),
    )
    .expect("noop");
    assert_eq!(noop.metrics["G-SRR"], Some(0.0));
    assert_eq!(noop.metrics["Sem-SRR@commit"], Some(1.0));
    assert_eq!(noop.metrics["Action-SRR@last_stage"], Some(1.0));
    assert_eq!(noop.gate_status["G-BASE-01-NUMERIC"], "PASS");

    // Incomplete grid or unknown baseline -> UNMEASURED
    let rows = core_baseline_scores(0, 0, 0, 48);
    let invalid = aggregate_run_scores(
        &rows[..rows.len() - 1],
        &baseline_context("metadata_newest_selector_greedy"),
        AggregateRunOptionsV5::default(),
    )
    .expect("invalid");
    let oracle_b = aggregate_run_scores(
        &rows,
        &baseline_context("oracle_team_replay"),
        AggregateRunOptionsV5::default(),
    )
    .expect("oracle");
    assert_eq!(invalid.gate_status["G-BASE-01-NUMERIC"], "UNMEASURED");
    assert_eq!(oracle_b.gate_status["G-BASE-01-NUMERIC"], "UNMEASURED");

    // No baseline_id -> NOT_APPLICABLE
    let mut model_ctx = baseline_context("noop_team");
    model_ctx.model_id = "model-test".to_owned();
    model_ctx.baseline_id = None;
    let na = aggregate_run_scores(&rows, &model_ctx, AggregateRunOptionsV5::default()).expect("na");
    assert_eq!(na.gate_status["G-BASE-01-NUMERIC"], "NOT_APPLICABLE");
    assert_eq!(na.gate_status["G-BASE-01"], "NOT_APPLICABLE");
}

#[test]
fn test_episode_metrics_safety_and_roundtrip() {
    let a = make_assessment([true, true, false, false, true], None, None, None, None);
    let mut ep = make_episode_input();
    ep.phase1 = make_phase(Some(a.clone()));
    ep.phase2 = make_phase(Some(a));
    let scored = score_episode(&ep).expect("score");
    assert_eq!(scored.g_srr_commit, Some(0));
    assert_eq!(scored.sem_srr_commit, Some(1));
    assert_eq!(scored.action_srr_last_stage, Some(1));
    assert_eq!(scored.g_srr_first_stage, Some(0));
    assert_eq!(scored.repair_lift, Some(0));

    // Serialization roundtrips
    let orig = make_episode_input();
    let mapped = episode_scoring_input_to_mapping(&orig);
    assert_eq!(episode_scoring_input_from_mapping(&mapped).unwrap(), orig);
    let mut extra = mapped.as_object().unwrap().clone();
    extra.insert("untracked".to_owned(), json!(1));
    assert!(
        episode_scoring_input_from_mapping(&Value::Object(extra))
            .unwrap_err()
            .to_string()
            .contains("missing or unexpected")
    );

    let score_mapped = episode_score_to_mapping(&scored);
    assert_eq!(episode_score_from_mapping(&score_mapped).unwrap(), scored);

    // Stage2-invalid safety rejection
    let mut targets = default_targets();
    targets.sort();
    let valid_t = targets[0].clone();
    let invalid_t = targets[1].clone();
    let err = SnapshotAssessmentV5::new(
        true,
        true,
        true,
        true,
        true,
        "a".repeat(64),
        [(valid_t.clone(), true), (invalid_t.clone(), true)]
            .into_iter()
            .collect(),
        [(valid_t.clone(), false), (invalid_t.clone(), true)]
            .into_iter()
            .collect(),
        [(valid_t.clone(), true), (invalid_t.clone(), false)]
            .into_iter()
            .collect(),
        [(valid_t, false), (invalid_t, false)].into_iter().collect(),
        None,
        None,
    )
    .unwrap_err();
    assert!(err.to_string().contains("Stage-2-valid"));

    // Unsafe stage attempt counted separately from commit safety
    let safe_commit = make_assessment([true; 5], None, None, None, None);
    let min_t = default_targets().into_iter().min().unwrap();
    let og_map: BTreeMap<TargetKeyV5, bool> = default_targets()
        .into_iter()
        .map(|t| (t.clone(), t == min_t))
        .collect();
    let unsafe_attempt = make_assessment([true; 5], None, Some(og_map), None, None);
    let mut ep2 = make_episode_input();
    ep2.safety_attempt_assessments = vec![unsafe_attempt, safe_commit];
    ep2.safety_attempt_stream_complete = Some(true);
    let sc2 = score_episode(&ep2).expect("sc2");
    assert_eq!(sc2.safety_stage_attempt_count, Some(2));
    assert_eq!(sc2.safety_valid_target_count, Some(4));
    assert_eq!(sc2.overgrant_count, Some(0));
    assert_eq!(sc2.safety_commit_overgrant_count, Some(0));
    assert_eq!(sc2.safety_stage_attempt_overgrant_count, Some(1));
    assert_eq!(sc2.safety_commit_valid_target_count, Some(4));
}

#[test]
fn test_inference_bit_exact_bootstrap_bh_sign_flip_and_escalation() {
    let p_val = exact_family_sign_flip_pvalue_v5(&[1.0; 12]).expect("sign flip");
    assert!((p_val - (2.0 / 4096.0)).abs() < 1e-12);

    let bh = benjamini_hochberg_v5(
        &[
            ("a".to_owned(), 0.01),
            ("b".to_owned(), 0.04),
            ("c".to_owned(), 0.2),
        ]
        .into_iter()
        .collect(),
    )
    .expect("bh");
    assert!((bh["a"] - 0.03).abs() < 1e-12);
    assert!((bh["b"] - 0.06).abs() < 1e-12);
    assert!((bh["c"] - 0.2).abs() < 1e-12);

    // Bit-exact comparison against NumPy 2.5.4 reference output
    let mut m_a: MetricCellsV5 = BTreeMap::new();
    let mut m_b: MetricCellsV5 = BTreeMap::new();
    for (fi, f) in CORE_FAMILIES_V5.iter().enumerate() {
        for (ri, r) in CORE_REGIMES_V5.iter().enumerate() {
            m_a.insert(
                ((*f).to_owned(), (*r).to_owned()),
                ((fi * 7 + ri * 3) % 11) as f64 / 10.0,
            );
            m_b.insert(
                ((*f).to_owned(), (*r).to_owned()),
                ((fi * 5 + ri * 2) % 9) as f64 / 10.0,
            );
        }
    }
    let bs = family_cluster_bootstrap_v5(
        &[
            ("alpha".to_owned(), FamilyMetricInputV5::from(m_a)),
            ("beta".to_owned(), FamilyMetricInputV5::from(m_b)),
        ]
        .into_iter()
        .collect(),
        CORE_BOOTSTRAP_REPLICATES_V5,
        CORE_BOOTSTRAP_SEED_V5,
    )
    .expect("bootstrap");

    assert_eq!(
        bs.model_intervals["alpha"].mean.to_bits(),
        0.49583333333333335_f64.to_bits()
    );
    assert_eq!(
        bs.model_intervals["alpha"].ci95_low.to_bits(),
        0.45416666666666666_f64.to_bits()
    );
    assert_eq!(
        bs.model_intervals["alpha"].ci95_high.to_bits(),
        0.5395833333333334_f64.to_bits()
    );
    assert_eq!(
        bs.model_intervals["beta"].mean.to_bits(),
        0.3875_f64.to_bits()
    );
    assert_eq!(
        bs.model_intervals["beta"].ci95_low.to_bits(),
        0.3520833333333333_f64.to_bits()
    );
    assert_eq!(
        bs.model_intervals["beta"].ci95_high.to_bits(),
        0.42291666666666666_f64.to_bits()
    );
    let paired = &bs.paired_intervals[&("alpha".to_owned(), "beta".to_owned())];
    assert_eq!(
        paired.mean_difference.to_bits(),
        0.10833333333333334_f64.to_bits()
    );
    assert_eq!(paired.ci95_low.to_bits(), 0.06250000000000011_f64.to_bits());
    assert_eq!(
        paired.ci95_high.to_bits(),
        0.15416666666666673_f64.to_bits()
    );

    // Confirmatory family (complete & incomplete) + unregistered contrasts + escalation
    let fa: BTreeMap<String, f64> = CORE_FAMILIES_V5
        .iter()
        .map(|&f| (f.to_owned(), 1.0))
        .collect();
    let fb: BTreeMap<String, f64> = CORE_FAMILIES_V5
        .iter()
        .map(|&f| (f.to_owned(), 0.96))
        .collect();
    let fc: BTreeMap<String, f64> = CORE_FAMILIES_V5
        .iter()
        .map(|&f| (f.to_owned(), 0.0))
        .collect();

    let values_opt: BTreeMap<String, Option<FamilyMetricInputV5>> = [
        ("a".to_owned(), Some(FamilyMetricInputV5::from(fa.clone()))),
        ("b".to_owned(), Some(FamilyMetricInputV5::from(fb.clone()))),
        ("c".to_owned(), Some(FamilyMetricInputV5::from(fc.clone()))),
    ]
    .into_iter()
    .collect();

    let spec_complete = ConfirmatoryFamilySpecV5::new(
        "core_headline_pairs",
        ConfirmatoryFamilyKindV5::CoreHeadline,
        vec![
            ("a".to_owned(), "b".to_owned()),
            ("a".to_owned(), "c".to_owned()),
        ],
    )
    .unwrap();
    let comp_res = evaluate_confirmatory_family_v5(&spec_complete, &values_opt, 0.05).unwrap();
    assert_eq!(comp_res.status, "COMPLETE");
    assert_eq!(comp_res.results.len(), 2);
    assert!((comp_res.results[0].p_exact.unwrap() - (2.0 / 4096.0)).abs() < 1e-12);

    let spec_inc = ConfirmatoryFamilySpecV5::new(
        "effort_contrasts",
        ConfirmatoryFamilyKindV5::EffortContrast,
        vec![
            ("a".to_owned(), "b".to_owned()),
            ("a".to_owned(), "missing_model".to_owned()),
        ],
    )
    .unwrap();
    let inc_res = evaluate_confirmatory_family_v5(&spec_inc, &values_opt, 0.05).unwrap();
    assert_eq!(inc_res.status, "INCOMPLETE_FAMILY");
    assert_eq!(inc_res.missing_configs, vec!["missing_model".to_owned()]);
    assert_eq!(inc_res.results[0].p_exact, None);

    let values_req: BTreeMap<String, FamilyMetricInputV5> = [
        ("a".to_owned(), FamilyMetricInputV5::from(fa)),
        ("b".to_owned(), FamilyMetricInputV5::from(fb)),
        ("c".to_owned(), FamilyMetricInputV5::from(fc)),
    ]
    .into_iter()
    .collect();
    let unreg =
        describe_unregistered_contrasts_v5(&values_req, &[("b".to_owned(), "c".to_owned())])
            .unwrap();
    assert_eq!(unreg.len(), 1);
    assert!(!unreg[0].confirmatory);
    assert_eq!(unreg[0].status, "DESCRIPTIVE_ONLY");

    let fx: BTreeMap<String, f64> = CORE_FAMILIES_V5
        .iter()
        .enumerate()
        .map(|(i, &f)| (f.to_owned(), if i < 8 { 0.50 } else { 0.40 }))
        .collect();
    let fy: BTreeMap<String, f64> = CORE_FAMILIES_V5
        .iter()
        .enumerate()
        .map(|(i, &f)| (f.to_owned(), if i < 8 { 0.46 } else { 0.42 }))
        .collect();
    let weak = compare_v5_core_models(
        &[
            ("x".to_owned(), FamilyMetricInputV5::from(fx)),
            ("y".to_owned(), FamilyMetricInputV5::from(fy)),
        ]
        .into_iter()
        .collect(),
        &[("x".to_owned(), "y".to_owned())],
        0.05,
        "core_headline_pairs",
    )
    .unwrap();
    assert!(weak[0].q_bh.unwrap() >= 0.05);
    let esc = select_v5_full_escalation_pairs(
        &weak,
        Some(
            &[("x".to_owned(), 0.47), ("y".to_owned(), 0.45)]
                .into_iter()
                .collect(),
        ),
    )
    .unwrap();
    assert_eq!(esc, vec![("x".to_owned(), "y".to_owned())]);

    // Reversed duplicate pair rejected
    let grid: MetricCellsV5 = CORE_FAMILIES_V5
        .iter()
        .flat_map(|&f| {
            CORE_REGIMES_V5
                .iter()
                .map(move |&r| ((f.to_owned(), r.to_owned()), 1.0))
        })
        .collect();
    let err = compare_v5_core_models(
        &[
            ("a".to_owned(), FamilyMetricInputV5::from(grid.clone())),
            ("b".to_owned(), FamilyMetricInputV5::from(grid)),
        ]
        .into_iter()
        .collect(),
        &[
            ("a".to_owned(), "b".to_owned()),
            ("b".to_owned(), "a".to_owned()),
        ],
        0.05,
        "core_headline_pairs",
    )
    .unwrap_err();
    assert!(err.to_string().contains("ignoring direction"));

    // v5_full nested 2 seeds x 4 regimes x 3 repeats in F01..F12
    let base = score_episode(&make_episode_input()).unwrap();
    let mut rows_a = Vec::new();
    let mut rows_b = Vec::new();
    for family in CORE_FAMILIES_V5 {
        for seed in [1, 2] {
            for regime in CORE_REGIMES_V5 {
                for rep in [1, 2, 3] {
                    let mut ra = base.clone();
                    ra.episode_id = format!("v5_full_{family}_S{seed:02}_{regime}");
                    ra.suite_id = "v5_full".to_owned();
                    ra.family_id = family.to_owned();
                    ra.regime_id = regime.to_owned();
                    ra.repeat_index = rep;
                    ra.seed_index = Some(seed);
                    ra.g_srr_commit = Some(1);
                    let mut rb = ra.clone();
                    rb.g_srr_commit = Some(0);
                    rows_a.push(ra);
                    rows_b.push(rb);
                }
            }
        }
    }
    let full_res = compare_v5_full_models(
        &[
            ("a".to_owned(), FamilyMetricInputV5::from(rows_a)),
            ("b".to_owned(), FamilyMetricInputV5::from(rows_b)),
        ]
        .into_iter()
        .collect(),
        &[("a".to_owned(), "b".to_owned())],
        0.05,
        "full_escalation_headline_pairs",
    )
    .unwrap();
    assert_eq!(full_res[0].mean_difference, 1.0);
    assert_eq!(full_res[0].rejected_bh_0_05, Some(true));

    // TupleCells variant works too
    let mut tuple_cells = IndexMap::new();
    for family in CORE_FAMILIES_V5 {
        for seed in ["S01", "S02"] {
            for regime in CORE_REGIMES_V5 {
                tuple_cells.insert(
                    vec![family.to_owned(), seed.to_owned(), regime.to_owned()],
                    1.0,
                );
            }
        }
    }
    let bs_tuple = family_cluster_bootstrap_v5(
        &[("a".to_owned(), FamilyMetricInputV5::TupleCells(tuple_cells))]
            .into_iter()
            .collect(),
        CORE_BOOTSTRAP_REPLICATES_V5,
        CORE_BOOTSTRAP_SEED_V5,
    )
    .unwrap();
    assert_eq!(bs_tuple.model_intervals["a"].mean, 1.0);
}

#[test]
fn test_incomplete_provisional_bounds_and_gates_g_econ_01_g_cal() {
    let base = score_episode(&make_episode_input()).unwrap();
    let run_sha = "f".repeat(64);
    let mut rows = Vec::new();
    let mut attempts = Vec::new();
    for (fam_idx, family) in CORE_FAMILIES_V5.iter().enumerate() {
        for regime in CORE_REGIMES_V5 {
            for rep in [1, 2, 3] {
                let mut row = base.clone();
                row.episode_id = format!("{family}_{regime}");
                row.family_id = (*family).to_owned();
                row.regime_id = (*regime).to_owned();
                row.repeat_index = rep;
                row.input_tokens_actual = Some(20_000);
                row.output_tokens_actual = Some(1_000);
                row.cost_usd_micros = Some(100_000);
                row.g_srr_commit = Some(if fam_idx == 0 && rep != 1 { 0 } else { 1 });

                let att_id = format!("att_{}_r{rep}_{}", row.episode_id, &run_sha[..12]);
                let req = ProviderRequestUsageV5 {
                    request_id: format!("req_{att_id}"),
                    logical_request_id: format!("lreq_{att_id}"),
                    request_kind: ProviderRequestKindV5::Initial,
                    retry_index: Some(0),
                    quota_continuation_index: None,
                    recovery_continuation_index: None,
                    segment_continuation_index: None,
                    decode_segment_index: 0,
                    attempt_id: att_id.clone(),
                    agent_id: "unified".to_owned(),
                    role: "unified".to_owned(),
                    dispatch_round: 0,
                    reservation_ordinal: (1, 0, 0, "unified".to_owned(), 0),
                    request_status: ProviderRequestStatusV5::Completed,
                    post_terminal: false,
                    provider_stop_reason: None,
                    provider_http_status: None,
                    provider_error_code: None,
                    provider_decode_status_raw: None,
                    normalized_decode_status: NormalizedDecodeStatusV5::TerminalNormal,
                    provider_refusal_signal: None,
                    actual_native_numeric_budget_tokens: None,
                    native_generation_parameters_json: None,
                    rendered_input_context_tokens: None,
                    effective_output_allowance_tokens: None,
                    input_tokens_total: Some(20_000),
                    cached_input_tokens: Some(0),
                    output_tokens_total: Some(1_000),
                    reasoning_tokens_subset: Some(0),
                    tool_serialization_tokens_subset: Some(0),
                    provider_reported_cost_micros: Some(100_000),
                    estimated_cost_micros: Some(100_000),
                    cost_basis: CostBasisV5::ProviderBilled,
                    pricing_manifest_sha256: None,
                    raw_usage_fields_json: None,
                    usage_scope: UsageScopeV5::PhysicalRequest,
                    currency_code: Some("USD".to_owned()),
                };
                attempts.push(
                    build_episode_attempt_v5(
                        &row,
                        &run_sha,
                        BuildEpisodeAttemptOptionsV5 {
                            completed_assistant_generations: 2,
                            tool_call_count: 2,
                            elapsed_wall_time_ms: Some(100),
                            terminal_outcome_elapsed_ms: Some(100),
                            active_elapsed_ms: Some(100),
                            provider_requests: vec![req],
                            ..Default::default()
                        },
                    )
                    .unwrap(),
                );
                rows.push(row);
            }
        }
    }

    let comparator = HistoricalComparatorManifestV5 {
        source_spec_sha256: "c".repeat(64),
        pricing_manifest_sha256: Some("a".repeat(64)),
        entries: vec![HistoricalComparatorEntryV5 {
            model_id: "frontier".to_owned(),
            provider_id: "anthropic".to_owned(),
            reasoning_effort_label: "medium".to_owned(),
            topology: "T0_SINGLE_MONOLITH".to_owned(),
            v41_completed_scenarios: 180,
            execution_valid_rate_bps: 10_000,
            v41_run_manifest_sha256: "b".repeat(64),
            input_tokens_total: Some(100_000),
            cached_input_tokens: Some(0),
            output_tokens_total: Some(20_000),
            reasoning_tokens_subset: Some(5_000),
            historical_cost_micros: Some(1_500_000),
            historical_cost_basis: CostBasisV5::FrozenRateEstimate,
        }],
    };

    let meta = RunCalibrationMetadataV5 {
        preflight_completed_before_freeze: Some(true),
        dev_cap_selected_before_held_out: Some(true),
        hypotheses_registered: Some(true),
        confirmatory_families_frozen: Some(true),
        effort_is_highest_valid: Some(true),
        budget_calculation_documented: Some(true),
        preregistered_track_disclosed: Some(true),
        topology_reviewed_and_frozen: Some(true),
        matched_role_configs_disclosed: Some(true),
        auditor_checker_disclosed: Some(true),
        shared_cap_disclosed: Some(true),
        contrast_multiplicity_disclosed: Some(true),
        policy_delivery_complete: Some(true),
        context_compatibility_disclosed: Some(true),
        ..Default::default()
    };

    let context = RunScoringContextV5 {
        suite_id: "v5_core".to_owned(),
        model_id: "frontier".to_owned(),
        reasoning_effort: "high".to_owned(),
        topology: "T0_SINGLE_MONOLITH".to_owned(),
        steering_mode: "UNSTEERED".to_owned(),
        pricing_manifest_sha256: Some("a".repeat(64)),
        model_tier: Some("frontier".to_owned()),
        baseline_id: None,
        wave_execution_mode: Some("COUPLED".to_owned()),
        run_config_sha256: Some(run_sha),
        planned_repeats_per_episode: PLANNED_REPEATS_MODEL_V5,
        comparison_track: "PRIMARY_HIGHEST".to_owned(),
        topology_profile_sha256: Some(T0_UNIFIED_PROFILE_V5.topology_profile_sha256.clone()),
        primary_prompt_bundle_sha256: Some("e".repeat(64)),
        max_generations: Some(50),
        cap_disclosed: false,
        expected_episode_grid: None,
        calibration_metadata: Some(meta.clone()),
    };

    let summary = aggregate_run_scores(
        &rows,
        &context,
        AggregateRunOptionsV5 {
            historical_comparator_manifest: Some(&comparator),
            attempts: Some(&attempts),
            ..Default::default()
        },
    )
    .unwrap();

    assert_eq!(summary.gate_status["G-ECON-01"], "PASS");
    assert_eq!(summary.gate_status["G-TOK-01"], "PASS");
    assert_eq!(summary.gate_status["G-TURN-01"], "PASS");
    assert_eq!(summary.gate_status["G-CAL-01"], "PASS");
    assert_eq!(summary.gate_status["G-CAL-02"], "PASS");
    assert_eq!(summary.gate_status["G-CAL-03"], "NOT_APPLICABLE");
    assert_eq!(summary.comparator_status, "HISTORICAL_CONTEXT_UNMATCHED");
    let expected_mean = (11.0 + 1.0 / 3.0) / 12.0;
    assert!((summary.metrics["G-SRR"].unwrap() - expected_mean).abs() < 1e-12);

    // Incomplete slot -> G-SRR=None + provisional bounds + empty bootstrap_ci95
    let mut inc_rows = rows.clone();
    inc_rows[0].attempt_status = "INCOMPLETE_PROVIDER_QUOTA".to_owned();
    inc_rows[0].g_srr_commit = None;
    inc_rows[0].sem_srr_commit = None;
    inc_rows[0].action_srr_last_stage = None;
    inc_rows[0].g_srr_first_stage = None;
    inc_rows[0].repair_lift = None;
    inc_rows[0].g_prop = None;
    inc_rows[0].g_prop_reset = None;
    inc_rows[0].overgrant_count = None;
    inc_rows[0].availability_breach_count = None;
    for v in inc_rows[0].category_pass.values_mut() {
        *v = None;
    }
    let inc_summary =
        aggregate_run_scores(&inc_rows, &context, AggregateRunOptionsV5::default()).unwrap();
    assert!(!inc_summary.is_complete);
    assert_eq!(inc_summary.observed_slots, 143);
    assert_eq!(inc_summary.planned_slots, 144);
    assert_eq!(inc_summary.metrics["G-SRR"], None);
    let (low_b, high_b) = inc_summary.provisional_bounds["G-SRR"];
    assert!(low_b < expected_mean && expected_mean <= high_b);
    assert!(inc_summary.bootstrap_ci95.is_empty());
    assert_eq!(
        inc_summary.attempt_status_counts["INCOMPLETE_PROVIDER_QUOTA"],
        1
    );
    let status_keys: BTreeSet<&str> = inc_summary
        .attempt_status_counts
        .keys()
        .map(String::as_str)
        .collect();
    let expected_keys: BTreeSet<&str> = ATTEMPT_STATUSES_V5.iter().copied().collect();
    assert_eq!(status_keys, expected_keys);

    // T5 on PRIMARY_HIGHEST fails G-CAL-02 and G-CAL-03 when matched_role_configs_disclosed=false
    let mut t5_meta = meta;
    t5_meta.matched_role_configs_disclosed = Some(false);
    let mut t5_ctx = context;
    t5_ctx.topology = "T5_HIERARCHICAL_PIPELINE".to_owned();
    t5_ctx.topology_profile_sha256 = Some("b".repeat(64));
    t5_ctx.calibration_metadata = Some(t5_meta);
    let t5_sum = aggregate_run_scores(&rows, &t5_ctx, AggregateRunOptionsV5::default()).unwrap();
    assert_eq!(t5_sum.gate_status["G-CAL-02"], "FAIL");
    assert_eq!(t5_sum.gate_status["G-CAL-03"], "FAIL");

    // DescriptiveUsageSummaryV5 validation sanity check
    let usage = DescriptiveUsageSummaryV5 {
        run_config_sha256: "d".repeat(64),
        planned_attempt_slots: 144,
        completed_attempts: 144,
        incomplete_provider_quota_attempts: 0,
        incomplete_provider_failure_attempts: 0,
        incomplete_context_attempts: 0,
        incomplete_harness_attempts: 0,
        attempt_status_counts_json: json!({"COMPLETED": 144}).to_string(),
        elapsed_wall_time_ms: None,
        active_elapsed_ms: None,
        total_input_tokens: None,
        cached_input_tokens: None,
        total_output_tokens: None,
        reasoning_tokens_subset: None,
        tool_serialization_tokens_subset: None,
        provider_billed_cost_micros: None,
        estimated_cost_micros: None,
        currency_code: None,
        accounting_coverage_status: ib_v5_schemas::AccountingCoverageStatusV5::Complete,
    };
    assert_eq!(usage.completed_attempts, 144);
}

#[test]
fn test_all_seven_categories_and_summary() {
    let p1_targets: BTreeMap<TargetKeyV5, bool> = [
        ("CONNECTOR_SYNC_MUTATION", "conn_01"),
        ("HR_SOURCE_MAPPING_MUTATION", "hr_01"),
        ("NHI_GOVERNANCE_MUTATION", "nhi_01"),
        ("CLOUD_CIEM_MUTATION", "ciem_01"),
        ("OPTIMIZATION_MUTATION", "opt_p1"),
        ("ACCOUNT_RECON_MUTATION", "acct_01"),
        ("JML_WORKFLOW_MUTATION", "jml_01"),
        ("AGENT_REGISTRY_MUTATION", "agt_01"),
    ]
    .into_iter()
    .map(|(k, id)| ((k.to_owned(), id.to_owned()), true))
    .collect();

    let mut p2_targets: BTreeMap<TargetKeyV5, bool> = [
        ("OPTIMIZATION_MUTATION", "opt_p2"),
        ("JIT_DECISION_MUTATION", "req_01"),
        ("UAR_CAMPAIGN_MUTATION", "uar_01"),
        ("CAE_CONTAINMENT_MUTATION", "cae_01"),
        ("AGENT_DELEGATION_MUTATION", "dlreq_01"),
    ]
    .into_iter()
    .map(|(k, id)| ((k.to_owned(), id.to_owned()), true))
    .collect();
    *p2_targets
        .get_mut(&("OPTIMIZATION_MUTATION".to_owned(), "opt_p2".to_owned()))
        .unwrap() = false;

    let p1_assess = SnapshotAssessmentV5::new(
        true,
        true,
        true,
        true,
        true,
        "a".repeat(64),
        p1_targets.clone(),
        p1_targets.keys().map(|k| (k.clone(), true)).collect(),
        p1_targets.keys().map(|k| (k.clone(), false)).collect(),
        p1_targets.keys().map(|k| (k.clone(), false)).collect(),
        None,
        None,
    )
    .unwrap();
    let p2_assess = SnapshotAssessmentV5::new(
        true,
        true,
        true,
        true,
        true,
        "b".repeat(64),
        p2_targets.clone(),
        p2_targets.keys().map(|k| (k.clone(), true)).collect(),
        p2_targets.keys().map(|k| (k.clone(), false)).collect(),
        p2_targets.keys().map(|k| (k.clone(), false)).collect(),
        None,
        None,
    )
    .unwrap();

    let mut item = make_episode_input();
    item.phase1 = make_phase(Some(p1_assess.clone()));
    item.phase2 = make_phase(Some(p2_assess));
    let cats = score_categories_for_episode_v5(&item).unwrap();
    assert_eq!(cats["connectors"], Some(1));
    assert_eq!(cats["classification"], Some(1));
    assert_eq!(cats["zsp_policy"], Some(0));
    assert_eq!(cats["jit_access"], Some(1));
    assert_eq!(cats["access_reviews"], Some(1));
    assert_eq!(cats["jml_cae_drift"], Some(1));
    assert_eq!(cats["agent_governance"], Some(1));

    let mut p2_uncommitted_in = make_episode_input();
    p2_uncommitted_in.phase1 = make_phase(Some(p1_assess));
    p2_uncommitted_in.phase2 = PhaseSnapshotSetV5::default();
    p2_uncommitted_in.p2_committed = false;

    let s1 = score_episode(&item).unwrap();
    let s2 = score_episode(&p2_uncommitted_in).unwrap();
    let cat_summary = score_categories_v5(&[s1, s2], None, None).unwrap();
    assert_eq!(cat_summary["jit_access"].committed_mapped_slots, 1);
    assert_eq!(cat_summary["jit_access"].observed_slots, 2);
    assert!((cat_summary["jit_access"].planned_pass_at_1.unwrap() - 0.5).abs() < 1e-12);
    assert!(
        (cat_summary["jit_access"]
            .observed_conditional_pass_at_1
            .unwrap()
            - 1.0)
            .abs()
            < 1e-12
    );
    assert!((cat_summary["jit_access"].observation_rate - 0.5).abs() < 1e-12);
    assert_eq!(cat_summary.len(), CATEGORY_IDS_V5.len());
}

#[test]
fn test_verify_usage_accounting_artifacts_v5() {
    let fp = provider_fingerprint_sha256_v5("unified", "openai", "gpt-5", "ep-1");
    let att_val = json!({
        "schema_version": V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        "role": "unified",
        "provider_kind": "openai",
        "model_id": "gpt-5",
        "provider_fingerprint_sha256": fp,
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "attestation": "operator_or_gateway",
    });
    let att_bytes = jcs_dumps(&att_val).unwrap();
    let att_sha = sha256_hex(&att_bytes);

    let man_val = json!({
        "schema_version": V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        "entries": [{
            "role": "unified",
            "provider_kind": "openai",
            "model_id": "gpt-5",
            "provider_fingerprint_sha256": fp,
            "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
            "evidence_path": "evidence/unified.json",
            "evidence_sha256": att_sha,
            "adapter_retry_limit": 0,
        }]
    });
    let man_bytes = jcs_dumps(&man_val).unwrap();
    let man_sha = sha256_hex(&man_bytes);
    let evidence_map: BTreeMap<String, Vec<u8>> = [("unified".to_owned(), att_bytes.clone())]
        .into_iter()
        .collect();

    let verified =
        verify_usage_accounting_artifacts_v5(&man_bytes, &evidence_map, &man_sha).unwrap();
    assert_eq!(verified.manifest_sha256, man_sha);
    assert_eq!(verified.entries_by_role["unified"].evidence_sha256, att_sha);

    // Duplicate key rejected
    let dup_bytes = format!(
        r#"{{"a":1,"a":2,"schema_version":"{V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA}","entries":[]}}"#
    )
    .into_bytes();
    let dup_sha = sha256_hex(&dup_bytes);
    let err =
        verify_usage_accounting_artifacts_v5(&dup_bytes, &evidence_map, &dup_sha).unwrap_err();
    assert!(err.to_string().contains("duplicate JSON key"));

    // Path traversal rejected
    let mut bad_man = man_val.clone();
    bad_man["entries"][0]["evidence_path"] = json!("../secret.json");
    let bad_bytes = jcs_dumps(&bad_man).unwrap();
    let bad_sha = sha256_hex(&bad_bytes);
    let err =
        verify_usage_accounting_artifacts_v5(&bad_bytes, &evidence_map, &bad_sha).unwrap_err();
    assert!(err.to_string().contains("unsafe"));
}

#[test]
fn test_execution_grading_repricing_refusal_and_out_of_phase_regrading() {
    let (episode, oracle) = load_v5_dev_f06_episode();
    let execution = replay_gold_execution(&episode, &oracle);
    let (scoring_input, score) = score_execution_v5(
        &execution,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap();
    assert_eq!(score.episode_id, episode.episode_id);
    assert_eq!(score.g_srr_commit, Some(1));
    assert_eq!(score.g_prop, Some(1));
    assert_eq!(score.g_prop_reset, Some(1));
    assert_eq!(scoring_input.phase2_state_feasible, Some(true));
    assert_eq!(scoring_input.safety_attempt_assessments.len(), 2);

    // Raw JSON round-trip matches identically
    let raw_val = execution.to_json_value().unwrap();
    let (raw_in, raw_sc) = score_execution_raw_v5(
        &raw_val,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap();
    assert_eq!(raw_in, scoring_input);
    assert_eq!(raw_sc, score);

    // Repricing & provider_declared accounting validation
    let pricing_sha = "d".repeat(64);
    let rates = PricingRatesV5 {
        uncached_input_micros_per_million: 1_000_000,
        cached_input_micros_per_million: 500_000,
        output_micros_per_million: 2_000_000,
        reasoning_micros_per_million: 3_000_000,
    };
    let mut econ_exec = execution.clone();
    econ_exec.provider_kind = "synthetic-provider".to_owned();
    econ_exec.model_id = "synthetic-provider:unified=model-test".to_owned();
    econ_exec.endpoint_identity = "unified=synthetic-endpoint".to_owned();
    econ_exec.turns_used = 2;
    econ_exec.usage_provenance_complete = true;
    econ_exec.completion_trace = vec![
        CompletionTraceV5 {
            turn_index: 0,
            role: "unified".to_owned(),
            requested_model_id: "model-test".to_owned(),
            served_model_id: Some("model-test".to_owned()),
            input_tokens_uncached: 10,
            cache_read_tokens: 2,
            cache_write_tokens: 3,
            output_tokens: 15,
            reasoning_tokens: 5,
            latency_s: 1.0,
            provider_reported_cost_usd: None,
            stop_reason: None,
            response_error: false,
            refused: false,
            tool_calls_returned: 0,
            usage_valid: true,
        },
        CompletionTraceV5 {
            turn_index: 1,
            role: "unified".to_owned(),
            requested_model_id: "model-test".to_owned(),
            served_model_id: Some("model-test".to_owned()),
            input_tokens_uncached: 7,
            cache_read_tokens: 4,
            cache_write_tokens: 1,
            output_tokens: 10,
            reasoning_tokens: 3,
            latency_s: 1.0,
            provider_reported_cost_usd: None,
            stop_reason: None,
            response_error: false,
            refused: false,
            tool_calls_returned: 0,
            usage_valid: true,
        },
    ];
    econ_exec.input_tokens_uncached = Some(17);
    econ_exec.cached_input_tokens = Some(10);
    econ_exec.input_tokens_actual = Some(27);
    econ_exec.output_tokens = Some(25);
    econ_exec.reasoning_tokens = Some(8);
    econ_exec.output_tokens_actual = Some(25);
    econ_exec.cost_usd_micros = Some(80);
    econ_exec.output_budget_exceeded = Some(false);
    econ_exec.pricing_manifest_sha256 = Some(pricing_sha.clone());
    econ_exec.usage_accounting_policy_id = Some(V5_USAGE_ACCOUNTING_POLICY_ID.to_owned());
    econ_exec.usage_accounting_status = UsageAccountingStatusV5::ProviderDeclared;
    econ_exec.provider_accounting = vec![ProviderAccountingTraceV5 {
        role: "unified".to_owned(),
        provider_kind: "synthetic-provider".to_owned(),
        model_id: "model-test".to_owned(),
        provider_fingerprint_sha256: provider_fingerprint_sha256_v5(
            "unified",
            "synthetic-provider",
            "model-test",
            "synthetic-endpoint",
        ),
        local_retry_limit: None,
        local_retry_policy_id: None,
        retry_control_source: "provider_declared_usage_includes_retries".to_owned(),
        evidence_sha256: None,
        aggregate_usage_scope: None,
        status: UsageAccountingStatusV5::ProviderDeclared,
        basis: "provider_declared_usage_includes_retries".to_owned(),
    }];

    let (econ_in, econ_sc) = score_execution_v5(
        &econ_exec,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5 {
            pricing_rates: Some(rates),
            pricing_manifest_sha256: Some(&pricing_sha),
            verified_usage_accounting: None,
        },
    )
    .unwrap();
    assert_eq!(econ_in.input_tokens_actual, Some(27));
    assert_eq!(econ_in.output_tokens_actual, Some(25));
    assert_eq!(econ_in.cost_usd_micros, Some(80));
    assert_eq!(econ_sc.g_srr_episode_envelope, Some(1));

    let mut tampered = econ_exec.clone();
    tampered.input_tokens_actual = Some(0);
    let err = score_execution_v5(
        &tampered,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5 {
            pricing_rates: Some(rates),
            pricing_manifest_sha256: Some(&pricing_sha),
            verified_usage_accounting: None,
        },
    )
    .unwrap_err();
    assert!(err.to_string().contains("input_tokens_actual"));

    // Out-of-phase REJECTED_STRUCTURAL stage attempt regrading (commit ec76a2c)
    let p1_slice_ids = &execution.phase1.stage_attempts[0].delivered_slice_ids;
    let p2_slice_ids = &execution.phase2.stage_attempts[0].delivered_slice_ids;
    let p1_mutations = &execution.phase1.commit.as_ref().unwrap().mutations;
    let p2_mutations = &execution.phase2.commit.as_ref().unwrap().mutations;

    let mut rt = RuntimeV5::new(episode.clone(), oracle.omniscient_s1.clone()).unwrap();
    assert_eq!(
        format!("{:?}", rt.query_sources(1, p1_slice_ids).unwrap().status),
        "Ok"
    );
    let premature_p2 = rt
        .stage_phase_mutations(2, "stg_premature_p2", &[])
        .unwrap();
    assert_eq!(format!("{:?}", premature_p2.status), "RejectedStructural");
    assert_eq!(
        format!(
            "{:?}",
            rt.stage_phase_mutations(1, "stg_p1", p1_mutations)
                .unwrap()
                .status
        ),
        "Staged"
    );
    assert_eq!(
        format!(
            "{:?}",
            rt.commit_phase_mutations(1, "stg_p1").unwrap().status
        ),
        "CommittedAndAdvanced"
    );
    let late_p1 = rt.stage_phase_mutations(1, "stg_late_p1", &[]).unwrap();
    assert_eq!(format!("{:?}", late_p1.status), "RejectedStructural");
    assert_eq!(
        format!("{:?}", rt.query_sources(2, p2_slice_ids).unwrap().status),
        "Ok"
    );
    assert_eq!(
        format!(
            "{:?}",
            rt.stage_phase_mutations(2, "stg_p2", p2_mutations)
                .unwrap()
                .status
        ),
        "Staged"
    );

    let mut replayed = execution.clone();
    replayed.attempt_status = AttemptStatusV5::WithinProtocolStepLimit;
    replayed.termination_reason = "MAX_TURNS_EXCEEDED".to_owned();
    replayed.phase2_committed = false;
    replayed.phase1 = phase_trajectory_from_runtime(&rt, 1);
    replayed.phase2 = phase_trajectory_from_runtime(&rt, 2);

    let (_rep_in, rep_sc) = score_execution_v5(
        &replayed,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap();
    assert_eq!(rep_sc.safety_stage_attempt_count, Some(4));
    assert_eq!(rep_sc.g_srr_commit, Some(0));

    let mut forged_raw = replayed.to_json_value().unwrap();
    forged_raw["phase2"]["stage_attempts"][0]["receipt_status"] = json!("STAGED");
    forged_raw["phase2"]["attempts"][0]["receipt_status"] = json!("STAGED");
    let err = score_execution_raw_v5(
        &forged_raw,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap_err();
    assert!(
        err.to_string()
            .contains("raw trajectory view disagrees with the sealed episode context")
    );

    // Duplicate target mutation safety in REJECTED_STRUCTURAL stage attempt
    let opt = p1_mutations
        .iter()
        .find(|m| m.mutation_kind() == "OPTIMIZATION_MUTATION")
        .expect("opt mutation");
    let mut dup_opt = opt.clone();
    if let AtomicMutationV5::Optimization(ref mut inner) = dup_opt {
        assert!(!inner.selected_ids.is_empty());
        inner.selected_ids.clear();
    }
    let mut dup_rt = RuntimeV5::new(episode.clone(), oracle.omniscient_s1.clone()).unwrap();
    dup_rt.query_sources(1, p1_slice_ids).unwrap();
    dup_rt
        .stage_phase_mutations(1, "stg_gold", p1_mutations)
        .unwrap();
    let mut dup_muts = p1_mutations.clone();
    dup_muts.push(dup_opt);
    let rej = dup_rt
        .stage_phase_mutations(1, "stg_duplicate", &dup_muts)
        .unwrap();
    assert_eq!(format!("{:?}", rej.status), "RejectedStructural");
    dup_rt.commit_phase_mutations(1, "stg_gold").unwrap();
    let mut dup_exec = execution;
    dup_exec.phase1 = phase_trajectory_from_runtime(&dup_rt, 1);
    let (dup_in, dup_sc) = score_execution_v5(
        &dup_exec,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap();
    assert!(!dup_in.safety_attempt_assessments[1].stage1_ok);
    assert_eq!(
        dup_sc.safety_valid_mutation_count,
        Some(score.safety_valid_mutation_count.unwrap() + dup_muts.len() as i64)
    );
    assert_eq!(
        dup_sc.safety_stage_attempt_availability_breach_count,
        Some(
            score
                .safety_stage_attempt_availability_breach_count
                .unwrap_or(0)
                + 1
        )
    );
    assert_eq!(
        dup_sc.availability_breach_count,
        score.availability_breach_count
    );

    // Turn-0 PROVIDER_REQUEST_POLICY_REJECTION preserves explicit zeros
    let mut t0_raw = dup_exec.to_json_value().unwrap();
    t0_raw["execution_valid"] = json!(true);
    t0_raw["provider_error"] = json!(false);
    t0_raw["parse_failure"] = json!(false);
    t0_raw["output_budget_exceeded"] = json!(false);
    t0_raw["usage_provenance_complete"] = json!(true);
    t0_raw["termination_reason"] = json!("REFUSAL");
    t0_raw["attempt_status"] = json!("WITHIN_PROTOCOL_REFUSAL");
    t0_raw["terminal_cause"] = json!("PROVIDER_REQUEST_POLICY_REJECTION");
    t0_raw["turns_used"] = json!(0);
    t0_raw["input_tokens_uncached"] = json!(0);
    t0_raw["cached_input_tokens"] = json!(0);
    t0_raw["input_tokens_actual"] = json!(0);
    t0_raw["output_tokens"] = json!(0);
    t0_raw["reasoning_tokens"] = json!(0);
    t0_raw["output_tokens_actual"] = json!(0);
    t0_raw["cost_usd_micros"] = json!(0);
    t0_raw["provider_kind"] = json!("synthetic-provider");
    t0_raw["model_id"] = json!("synthetic-provider:unified=model-test");
    t0_raw["endpoint_identity"] = json!("unified=synthetic-endpoint");
    t0_raw["usage_accounting_status"] = json!("UNKNOWN");
    t0_raw["provider_accounting"] = json!([]);
    t0_raw["completion_trace"] = json!([]);
    let (t0_in, t0_sc) = score_execution_raw_v5(
        &t0_raw,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap();
    assert_eq!(t0_in.input_tokens_actual, Some(0));
    assert_eq!(t0_in.output_tokens_actual, Some(0));
    assert_eq!(t0_in.cost_usd_micros, Some(0));
    assert_eq!(t0_sc.g_srr_commit, Some(0));

    // Refusal takes precedence over output limit & parse failure
    let mut bad_refusal = econ_exec.to_json_value().unwrap();
    bad_refusal["completion_trace"][0]["refused"] = json!(true);
    bad_refusal["execution_valid"] = json!(false);
    bad_refusal["provider_error"] = json!(false);
    bad_refusal["parse_failure"] = json!(true);
    bad_refusal["attempt_status"] = json!("WITHIN_PROTOCOL_OUTPUT_LIMIT");
    bad_refusal["termination_reason"] = json!("OUTPUT_BUDGET_STOP");
    bad_refusal["output_budget_exceeded"] = json!(true);
    let err = score_execution_raw_v5(
        &bad_refusal,
        &episode,
        &oracle,
        ScoreExecutionOptionsV5::default(),
    )
    .unwrap_err();
    assert!(
        err.to_string()
            .contains("cannot be classified as provider/parse failure")
    );
}

#[test]
fn test_incomplete_run_gates_provider_billed_and_p95() {
    let all_true: BTreeMap<TargetKeyV5, bool> =
        default_targets().into_iter().map(|t| (t, true)).collect();
    let unsafe_assess = make_assessment(
        [true; 5],
        None,
        Some(all_true.clone()),
        Some(all_true),
        None,
    );
    let mut complete_in = make_episode_input();
    complete_in.episode_id = "ep_complete".to_owned();
    complete_in.phase1 = make_phase(Some(unsafe_assess.clone()));
    complete_in.phase2 = make_phase(Some(unsafe_assess));

    let mut inc_in = make_episode_input();
    inc_in.episode_id = "ep_incomplete".to_owned();
    inc_in.provider_error = Some(true);
    inc_in.execution_valid = Some(false);
    inc_in.termination_reason = Some("PROVIDER_ERROR".to_owned());
    inc_in.attempt_status = "INCOMPLETE_PROVIDER_FAILURE".to_owned();

    let scores = vec![
        score_episode(&complete_in).unwrap(),
        score_episode(&inc_in).unwrap(),
    ];
    let mut ctx = baseline_context("noop_team");
    ctx.model_id = "model-test".to_owned();
    ctx.baseline_id = None;
    let inc_sum = aggregate_run_scores(&scores, &ctx, AggregateRunOptionsV5::default()).unwrap();
    assert!(!inc_sum.is_complete);
    for gated in [
        "Action-SRR@last_stage | both_phases_staged",
        "P1-Action-SRR@last_stage | p1_staged",
        "provider_error_count",
        "parse_failure_count",
        "max_turns_exceeded_count",
        "OVERGRANT@commit_mean",
        "AVAIL_BREACH@commit_mean",
    ] {
        assert_eq!(inc_sum.metrics[gated], None, "{gated}");
    }
    assert_eq!(
        inc_sum.metrics["OVERGRANT_observed_commit_count"],
        Some(4.0)
    );

    // Complete PROVIDER_BILLED preserves suite_cost when estimated is None
    let mut ep1 = make_episode_input();
    ep1.episode_id = "ep_1".to_owned();
    let s1 = score_episode(&ep1).unwrap();
    let run_sha = "d".repeat(64);
    let att_id = format!("att_ep_1_r1_{}", &run_sha[..12]);
    let req = ProviderRequestUsageV5 {
        request_id: "req_1".to_owned(),
        logical_request_id: "lreq_1".to_owned(),
        request_kind: ProviderRequestKindV5::Initial,
        retry_index: Some(0),
        quota_continuation_index: None,
        recovery_continuation_index: None,
        segment_continuation_index: None,
        decode_segment_index: 0,
        attempt_id: att_id,
        agent_id: "unified".to_owned(),
        role: "unified".to_owned(),
        dispatch_round: 0,
        reservation_ordinal: (1, 0, 0, "unified".to_owned(), 0),
        request_status: ProviderRequestStatusV5::Completed,
        post_terminal: false,
        provider_stop_reason: None,
        provider_http_status: None,
        provider_error_code: None,
        provider_decode_status_raw: None,
        normalized_decode_status: NormalizedDecodeStatusV5::TerminalNormal,
        provider_refusal_signal: None,
        actual_native_numeric_budget_tokens: None,
        native_generation_parameters_json: None,
        rendered_input_context_tokens: None,
        effective_output_allowance_tokens: None,
        input_tokens_total: Some(100),
        cached_input_tokens: Some(0),
        output_tokens_total: Some(50),
        reasoning_tokens_subset: Some(0),
        tool_serialization_tokens_subset: Some(0),
        provider_reported_cost_micros: Some(42_000),
        estimated_cost_micros: None,
        cost_basis: CostBasisV5::ProviderBilled,
        pricing_manifest_sha256: None,
        raw_usage_fields_json: None,
        usage_scope: UsageScopeV5::PhysicalRequest,
        currency_code: Some("USD".to_owned()),
    };
    let att = build_episode_attempt_v5(
        &s1,
        &run_sha,
        BuildEpisodeAttemptOptionsV5 {
            completed_assistant_generations: 1,
            provider_requests: vec![req],
            ..Default::default()
        },
    )
    .unwrap();
    let mut billed_ctx = ctx;
    billed_ctx.run_config_sha256 = Some(run_sha);
    let billed_sum = aggregate_run_scores(
        &[s1],
        &billed_ctx,
        AggregateRunOptionsV5 {
            attempts: Some(&[att]),
            ..Default::default()
        },
    )
    .unwrap();
    assert_eq!(billed_sum.gate_status["G-ECON-01"], "PASS");
    assert_eq!(billed_sum.suite_cost_usd_micros, Some(42_000));

    // ORACLE_PHASE_RESET propagation mode
    let mut reset_ep = make_episode_input();
    reset_ep.propagation_mode = "ORACLE_PHASE_RESET".to_owned();
    reset_ep.phase2_state_feasible = Some(false);
    reset_ep.g_prop_coupled_pass = Some(false);
    reset_ep.g_prop_reset_pass = Some(true);
    let reset_sc = score_episode(&reset_ep).unwrap();
    assert_eq!(reset_sc.g_prop, Some(1));
    assert_eq!(reset_sc.g_prop_reset, Some(1));

    // p95_i64
    let p95 = ib_v5_scoring::p95_i64(&[10, 20, 30, 40, 50]).unwrap();
    assert!((p95 - 48.0).abs() < 1e-12);
}

#[test]
fn test_attempt_status_outcome_flags_cover_observed_and_incomplete_classes() {
    let cases = [
        (
            "COMPLETED",
            false,
            false,
            true,
            "EPISODE_COMPLETED",
            Some(false),
            false,
        ),
        (
            "WITHIN_PROTOCOL_REFUSAL",
            false,
            false,
            true,
            "REFUSAL",
            Some(false),
            false,
        ),
        (
            "WITHIN_PROTOCOL_PARSE_FAILURE",
            false,
            true,
            false,
            "PARSE_FAILURE",
            Some(false),
            false,
        ),
        (
            "WITHIN_PROTOCOL_STEP_LIMIT",
            false,
            false,
            true,
            "MAX_TURNS_EXCEEDED",
            Some(false),
            false,
        ),
        (
            "WITHIN_PROTOCOL_STEP_LIMIT",
            false,
            false,
            true,
            "NO_TOOL_CALL",
            Some(false),
            false,
        ),
        (
            "WITHIN_PROTOCOL_OUTPUT_LIMIT",
            false,
            false,
            true,
            "OUTPUT_BUDGET_STOP",
            Some(true),
            false,
        ),
        (
            "WITHIN_PROTOCOL_CONTEXT_LIMIT",
            false,
            false,
            true,
            "CONTEXT_LIMIT_EXCEEDED",
            Some(false),
            false,
        ),
        (
            "INCOMPLETE_PROVIDER_QUOTA",
            true,
            false,
            false,
            "PROVIDER_ERROR",
            None,
            true,
        ),
        (
            "INCOMPLETE_PROVIDER_FAILURE",
            true,
            false,
            false,
            "PROVIDER_ERROR",
            None,
            true,
        ),
        (
            "INCOMPLETE_CONTEXT_INCOMPATIBLE",
            true,
            false,
            false,
            "PROVIDER_ERROR",
            None,
            true,
        ),
        (
            "INCOMPLETE_HARNESS_FAILURE",
            true,
            false,
            false,
            "PROVIDER_ERROR",
            None,
            true,
        ),
    ];

    for (status, provider_error, parse_failure, execution_valid, termination, budget, incomplete) in
        cases
    {
        let mut item = make_episode_input();
        item.attempt_status = status.to_owned();
        item.provider_error = Some(provider_error);
        item.parse_failure = Some(parse_failure);
        item.execution_valid = Some(execution_valid);
        item.termination_reason = Some(termination.to_owned());
        item.budget_exceeded = budget;

        let score = score_episode(&item).unwrap_or_else(|error| panic!("{status}: {error}"));
        assert_eq!(score.g_srr_commit.is_none(), incomplete, "{status}");
    }
}

#[test]
fn test_completed_status_rejects_provider_error_even_without_raw_trace() {
    let (episode, oracle) = load_v5_dev_f06_episode();
    let execution = replay_gold_execution(&episode, &oracle);
    let mut raw = execution.to_json_value().unwrap();
    raw["provider_error"] = json!(true);
    raw["execution_valid"] = json!(false);
    raw["termination_reason"] = json!("PROVIDER_ERROR");
    raw["attempt_status"] = json!("COMPLETED");
    raw["completion_trace"] = json!([]);

    let error = score_execution_raw_v5(&raw, &episode, &oracle, ScoreExecutionOptionsV5::default())
        .unwrap_err()
        .to_string();
    assert!(
        error.contains("attempt_status COMPLETED disagrees with raw provider_error flag"),
        "{error}"
    );
}

#[test]
fn test_episode_scoring_rejects_completed_provider_failure_classification() {
    let mut item = make_episode_input();
    item.provider_error = Some(true);
    item.parse_failure = Some(false);
    item.execution_valid = Some(false);
    item.termination_reason = Some("PROVIDER_ERROR".to_owned());

    let error = score_episode(&item).unwrap_err().to_string();
    assert!(
        error.contains("attempt_status COMPLETED disagrees with raw provider_error flag"),
        "{error}"
    );
}
