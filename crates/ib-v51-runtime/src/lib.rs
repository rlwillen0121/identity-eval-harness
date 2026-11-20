//! Virtual workspace tools, provider agent loop, platform reference, validation, and report runner for IdentityBench v5.1.

pub mod config;
pub mod report;
pub mod runner;
pub mod validation;
pub mod workspace;

pub const PLATFORM_REFERENCE_V51: &str = include_str!("platform_reference_v51.md");

pub use config::{
    DEFAULT_V51_MAX_OUTPUT_TOKENS, LoadedPricingV51, LoadedRunConfigV51, LoadedV51Dataset,
    PricingRatesV51, RunConfigV51, load_pricing_v51, load_run_config_v51, load_v51_dataset,
};
pub use report::{
    RunMetadataHintV51, render_v51_report_markdown, report_v51_run, summarize_v51_records,
};
pub use runner::{
    RunV51EpisodeOptions, RunV51SuiteOptions, render_v51_opening_user_message,
    render_v51_system_prompt, run_v51_episode, run_v51_suite,
};
pub use validation::{GateResultV51, ValidationReportV51, validate_v51_dataset};
pub use workspace::{
    CONFIG_FILE_PATHS_V51, DEFAULT_READ_LINE_WINDOW, MAX_PREVIEW_CUBES, MAX_READ_LINES_PER_CALL,
    VirtualWorkspaceRuntimeV51, tool_definitions_v51,
};
