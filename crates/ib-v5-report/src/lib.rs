//! IdentityBench v5 offline report generation, provenance verification, and Markdown escaping.
//!
//! Ports `identitybench.report.markdown_text` and `identitybench.v5.report`.

pub mod error;
pub mod markdown_text;
pub mod report;

pub use error::{ComparatorEvidenceUnavailableError, ReportError};
pub use markdown_text::{code_cell, escape_cell, escape_inline, fenced, fenced_text};
pub use report::{
    ArtifactProvenanceV5, CoreInferenceOptionsV5, FmtValue, METHODOLOGY_CAVEATS_V5,
    ReportOptionsV5, V5CoreInferenceArtifact, V5FullTieBreakArtifact, V5ReportArtifact,
    build_v5_core_inference, build_v5_core_inference_with_options,
    build_v5_full_tiebreak_inference, clean_table_text, fmt_f64, fmt_i64, fmt_interval, fmt_value,
    render_v5_core_inference_markdown, render_v5_full_tiebreak_markdown, render_v5_markdown_report,
    report_from_raw_artifacts, report_from_raw_artifacts_with_options, summary_to_json,
    validate_comparator_manifest, write_v5_core_inference_report, write_v5_full_tiebreak_report,
    write_v5_report,
};
