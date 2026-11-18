//! Delivered-view runtime and mechanical transaction gates for IdentityBench v5.
//!
//! Mirrors `identitybench.v5.runtime` and `identitybench.v5.state_machine`.

pub mod engine;
pub mod ledger;
pub mod transitions;
pub mod view;

pub use engine::{
    CoSignMintKey, CoSignTuple, DiagnosticServiceV5, PhaseSnapshotSetV5, RuntimeOptionsV5,
    RuntimeSnapshotV5, RuntimeV5, SteerMintKey, TokenTotalsV5, cosign_requirements, stage_digests,
};
pub use ledger::{
    LedgerProjection, build_internal_prior_ledger, build_visible_prior_ledger,
    ledger_from_projection,
};
pub use transitions::{
    WorkflowDAGResultV5, apply_phase1, apply_phase2, completeness_ok_from_mutations_only,
    induce_phase2_world, sim_jml_workflow, simulate_jml_workflow_dag, simulate_workflow_dag,
};
pub use view::{
    RuntimeError, ViewV5, all_evidence_ids, build_view_v5, completeness_ok, live_version,
    mutation_key, mutation_map, required_mutation_keys, structural_errors, structural_ok,
};
