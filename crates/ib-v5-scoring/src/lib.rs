//! IdentityBench v5 pure trajectory scoring, execution record contracts, economic gates,
//! and preregistered statistical inference.
//!
//! Mirrors `identitybench.v5.scoring` and `EpisodeExecutionV5` from `identitybench.v5.runner`.

pub mod accounting_verify;
pub mod aggregation;
pub mod constants;
pub mod episode;
pub mod error;
pub mod execution_types;
pub mod inference;
pub mod snapshot;

pub use accounting_verify::{
    VerifiedUsageAccountingEntryV5, VerifiedUsageAccountingV5,
    verify_usage_accounting_artifacts_v5,
};
pub use aggregation::{
    AggregateRunOptionsV5, CategoryScoreSummaryV5, RunCalibrationMetadataV5, RunScoreSummaryV5,
    RunScoringContextV5, aggregate_run_scores, p95_i64, score_categories_v5,
};
pub use constants::{
    ATTEMPT_STATUSES_V5, CATEGORY_IDS_V5, COMPARISON_TRACKS_V5, CORE_BOOTSTRAP_REPLICATES_V5,
    CORE_BOOTSTRAP_SEED_V5, CORE_EXACT_SIGN_FLIPS_V5, CORE_FAMILIES_V5, CORE_FAMILY_COUNT_V5,
    CORE_REGIMES_V5, FULL_SEEDS_PER_FAMILY_V5, G_BASELINE_IDS_V5, INCOMPLETE_ATTEMPT_STATUSES_V5,
    OBSERVED_ATTEMPT_STATUSES_V5, PLANNED_REPEATS_MODEL_V5, category_phase_kinds_v5,
    is_incomplete_attempt_status_v5,
};
pub use episode::{
    BuildEpisodeAttemptOptionsV5, ScoreExecutionOptionsV5, build_episode_attempt_v5,
    gold_trace_entry_from_oracle_trace, score_categories_for_episode_v5, score_episode,
    score_execution_raw_v5, score_execution_v5,
};
pub use error::ScoringError;
pub use execution_types::{
    CompletionTraceV5, EpisodeExecutionV5, PhaseTrajectoryV5, PricingRatesV5, SerializedViewV5,
    ToolTraceV5, TrajectorySnapshotV5, UsageTotalsV5,
};
pub use inference::{
    ConfirmatoryFamilyKindV5, ConfirmatoryFamilyResultV5, ConfirmatoryFamilySpecV5,
    CoreBootstrapResultV5, FamilyMetricInputV5, MetricIntervalV5, PairedMetricIntervalV5,
    PairwiseRandomizationResultV5, benjamini_hochberg_v5, compare_v5_core_models,
    compare_v5_full_models, conditional_rate, describe_unregistered_contrasts_v5,
    evaluate_confirmatory_family_v5, exact_family_sign_flip_pvalue_v5,
    family_cluster_bootstrap_v5, select_v5_full_escalation_pairs, v5_full_escalations,
};
pub use snapshot::{
    CellKeyV5, EpisodeScoreV5, EpisodeScoringInputV5, MetricCellsV5,
    MutationSafetyObservationV5, PhaseSnapshotSetV5, SnapshotAssessmentV5, TargetKeyV5,
    episode_score_from_mapping, episode_score_to_mapping, episode_scoring_input_from_mapping,
    episode_scoring_input_to_mapping, phase_snapshot_set_from_mapping,
    phase_snapshot_set_to_mapping, snapshot_assessment_from_mapping,
    snapshot_assessment_from_verification, snapshot_assessment_to_mapping,
    snapshot_evidence_sha256,
};
