//! Strict offline IdentityBench v5 report generation with artifact provenance.

pub(crate) mod atomic;
pub mod inference;
pub mod json_util;
pub mod provenance;
pub mod render;

pub use inference::{
    CoreInferenceOptionsV5, METHODOLOGY_CAVEATS_V5, V5CoreInferenceArtifact,
    V5FullTieBreakArtifact, build_v5_core_inference, build_v5_core_inference_with_options,
    build_v5_full_tiebreak_inference, render_v5_core_inference_markdown,
    render_v5_full_tiebreak_markdown, write_v5_core_inference_report,
    write_v5_full_tiebreak_report,
};
pub use provenance::{
    ArtifactProvenanceV5, ReportOptionsV5, V5ReportArtifact, report_from_raw_artifacts,
    report_from_raw_artifacts_with_options, validate_comparator_manifest,
};
pub use render::{
    FmtValue, clean_table_text, fmt_f64, fmt_i64, fmt_interval, fmt_value,
    render_v5_markdown_report, summary_to_json, write_v5_report,
};
