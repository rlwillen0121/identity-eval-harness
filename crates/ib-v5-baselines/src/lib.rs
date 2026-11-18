//! Deterministic v5 reference policies and offline proof witnesses.

pub mod baselines;
pub mod mutation_alternatives;
pub mod negative_proofs;
pub mod shortcut_proofs;

pub use baselines::{
    ALL_V5_BASELINES, BASELINE_ALIASES_V5, BaselineError, BaselineIdV5, CORE_TABLES_V5,
    HEURISTIC_BASELINES_V5, baseline_policy_v5, default_run_config_for_baseline_v5,
    obs_strategy_q1, obs_strategy_q2, run_v5_baseline_on_episode,
    run_v5_baseline_on_episode_with_options, run_v5_baseline_with_oracle,
};
pub use negative_proofs::{
    NegativeProofReportV5, NegativeWitnessResultV5, NegativeWitnessV5, RuleBoundaryWitnessResultV5,
    build_negative_witnesses, run_negative_proofs,
};
pub use shortcut_proofs::{
    ShortcutProofReportV5, run_dev_regime_matrix_shortcut_proofs_v5, run_shortcut_proofs_v5,
};
