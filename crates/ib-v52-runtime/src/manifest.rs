//! `manifest.json` of a v5.2 run directory (`SPEC-v5.2` §7.5, §10.1).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::integrity::{RunFingerprintInputsV52, SealDigestsV52};

/// `manifest.json` schema identifier.
pub const MANIFEST_SCHEMA_V52: &str = "identitybench-v52-run-v2";

/// One `run` invocation over a run directory.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunInvocationV52 {
    pub started_at: String,
    pub resume: bool,
    pub fingerprint: String,
}

/// The run manifest, rewritten atomically before the first slot and when the invocation returns.
/// Members serialize in declaration order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunManifestV52 {
    pub schema_version: String,
    pub spec_version: String,
    pub protocol_id: String,
    pub compaction_policy_id: String,
    pub suite_id: String,
    pub model_label: String,
    pub model_id: String,
    pub transport_id: String,
    pub reasoning_effort: String,
    pub requested_output_tokens: Option<u64>,
    /// `"transport_default"` or `{"temperature": <t>}`.
    pub sampling: Value,
    pub system_prompt_sha256: String,
    pub system_prompt_channel: String,
    pub pricing_sha256: String,
    pub receipt_sha256: Option<String>,
    pub endpoint_identity: String,
    pub binary_sha256: String,
    pub seal: SealDigestsV52,
    pub episode_count: usize,
    pub repeats: usize,
    pub concurrency: usize,
    pub boundary_window: usize,
    pub shuffle_seed: u64,
    /// Slot stems (`<episode_id>_r<repeat>`) in dispatch order.
    pub slot_order: Vec<String>,
    pub planned_slots: usize,
    pub fingerprint: String,
    pub fingerprint_inputs: RunFingerprintInputsV52,
    /// Slots with a terminal (non-provider-failure) record.
    pub observed_slots: usize,
    pub written_records: usize,
    pub completed: bool,
    pub invocations: Vec<RunInvocationV52>,
    pub withheld_bundle_sha256: String,
}

/// The `sampling` member for a requested temperature.
#[must_use]
pub fn sampling_json(temperature: Option<f64>) -> Value {
    temperature.map_or_else(
        || Value::String("transport_default".to_owned()),
        |t| serde_json::json!({ "temperature": t }),
    )
}
