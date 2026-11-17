//! Run configuration, provider request accounting, episode attempts, and comparator models for v5.

use std::collections::BTreeSet;

use ib_core::pyjson::{PyNumber, classify_number};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::common::{
    AttemptStatusV5, CANONICAL_ATTEMPT_STATUSES_V5, SHA256_HEX_RE, SchemaValidationError,
    StrictV5Model, USAGE_AGENT_ID_RE, check_opt_re, check_re,
};

fn default_fifty() -> i64 {
    50
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SteeringModeV5 {
    Unsteered,
    CheckpointSteered,
    FullLiveSteered,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CouplingModeV5 {
    Coupled,
    OraclePhaseReset,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EffortTrackV5 {
    PrimaryHighest,
    PreregisteredLower,
    PreregisteredAblation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum NativeEffortPolicyV5 {
    FixedNativeSetting,
    AdaptiveNativeMode,
    MaximumValidNativeBudget,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SegmentContinuationCapabilityV5 {
    NotRequired,
    Supported,
    Unsupported,
    Unverified,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunConfigurationV5 {
    pub model_id: String,
    pub provider_id: String,
    pub provider_model_version: String,
    pub topology: String,
    pub steering_mode: SteeringModeV5,
    pub coupling_mode: CouplingModeV5,
    pub effort_track: EffortTrackV5,
    pub native_effort_policy: NativeEffortPolicyV5,
    pub reasoning_effort_label: String,
    pub native_effort_config_json: String,
    #[serde(default)]
    pub native_numeric_budget_cap_tokens: Option<i64>,
    #[serde(default)]
    pub native_numeric_budget_min_tokens: Option<i64>,
    #[serde(default)]
    pub native_visible_output_reserve_tokens: Option<i64>,
    pub supported_reasoning_effort_labels_json: String,
    #[serde(default = "default_fifty")]
    pub max_assistant_generations_per_episode: i64,
    pub effort_order_source: String,
    pub effort_minimum_output_tokens: i64,
    pub reasoning_retention_policy_json: String,
    pub usage_accounting_policy_json: String,
    pub transport_api_id: String,
    pub transport_api_version: String,
    pub transport_adapter_version: String,
    pub transport_streaming: bool,
    #[serde(default)]
    pub transport_max_logical_output_tokens: Option<i64>,
    #[serde(default)]
    pub provider_segment_output_limit_tokens: Option<i64>,
    pub segment_continuation_capability: SegmentContinuationCapabilityV5,
    pub decode_segmentation_policy_json: String,
    pub tokenizer_id: String,
    pub tokenizer_version: String,
    pub context_window_tokens: i64,
    pub provider_schema_overhead_tokens: i64,
    pub provider_max_output_tokens: i64,
    pub requested_output_ceiling_tokens: i64,
    pub base_effective_output_ceiling_tokens: i64,
    pub minimum_context_headroom_tokens: i64,
    pub headroom_algorithm_id: String,
    pub sampling_config_sha256: String,
    pub toolset_sha256: String,
    pub retry_policy_id: String,
    pub request_client_timeout_s: i64,
    pub recovery_policy_json: String,
    #[serde(default)]
    pub cap_disclosed: bool,
    pub prompt_sha256: String,
    pub primary_prompt_bundle_sha256: String,
    pub adapter_sha256: String,
    pub tool_decoding_policy_json: String,
    pub harness_sha256: String,
    pub dataset_seal_sha256: String,
    pub topology_profile_sha256: String,
    pub topology_role_priority_json: String,
    pub repeat_seed_schedule_json: String,
    pub repeat_execution_order_json: String,
    pub run_config_sha256: String,
}

impl RunConfigurationV5 {
    /// Return whether the configured logical ceiling exceeds a provider segment limit.
    #[must_use]
    pub fn requires_supported_segment_continuation(&self) -> bool {
        self.provider_segment_output_limit_tokens
            .is_some_and(|limit| self.base_effective_output_ceiling_tokens > limit)
    }

    /// Return whether segment continuation capability satisfies the configured ceiling.
    #[must_use]
    pub fn is_segment_continuation_eligible(&self) -> bool {
        !self.requires_supported_segment_continuation()
            || self.segment_continuation_capability == SegmentContinuationCapabilityV5::Supported
    }
}

impl StrictV5Model for RunConfigurationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if let Some(v) = self.native_numeric_budget_cap_tokens
            && v < 1
        {
            return Err(SchemaValidationError::validation(
                "native_numeric_budget_cap_tokens must be >= 1",
            ));
        }
        if let Some(v) = self.native_numeric_budget_min_tokens
            && v < 0
        {
            return Err(SchemaValidationError::validation(
                "native_numeric_budget_min_tokens must be >= 0",
            ));
        }
        if let Some(v) = self.native_visible_output_reserve_tokens
            && v < 1
        {
            return Err(SchemaValidationError::validation(
                "native_visible_output_reserve_tokens must be >= 1",
            ));
        }
        if !matches!(self.max_assistant_generations_per_episode, 50 | 100) {
            return Err(SchemaValidationError::validation(
                "max_assistant_generations_per_episode must be 50 or 100",
            ));
        }
        if self.effort_minimum_output_tokens < 1 {
            return Err(SchemaValidationError::validation(
                "effort_minimum_output_tokens must be >= 1",
            ));
        }
        if let Some(v) = self.transport_max_logical_output_tokens
            && v < 1
        {
            return Err(SchemaValidationError::validation(
                "transport_max_logical_output_tokens must be >= 1",
            ));
        }
        if let Some(v) = self.provider_segment_output_limit_tokens
            && v < 1
        {
            return Err(SchemaValidationError::validation(
                "provider_segment_output_limit_tokens must be >= 1",
            ));
        }
        if self.context_window_tokens < 1
            || self.provider_schema_overhead_tokens < 0
            || self.provider_max_output_tokens < 1
            || self.requested_output_ceiling_tokens < 1
            || self.base_effective_output_ceiling_tokens < 1
            || self.minimum_context_headroom_tokens < 1
            || self.request_client_timeout_s < 1
        {
            return Err(SchemaValidationError::validation(
                "RunConfigurationV5 token/timeout bounds violated",
            ));
        }
        if self.cap_disclosed {
            return Err(SchemaValidationError::validation(
                "RunConfigurationV5.cap_disclosed must be false",
            ));
        }
        for (field, val) in [
            ("sampling_config_sha256", &self.sampling_config_sha256),
            ("toolset_sha256", &self.toolset_sha256),
            ("prompt_sha256", &self.prompt_sha256),
            (
                "primary_prompt_bundle_sha256",
                &self.primary_prompt_bundle_sha256,
            ),
            ("adapter_sha256", &self.adapter_sha256),
            ("harness_sha256", &self.harness_sha256),
            ("dataset_seal_sha256", &self.dataset_seal_sha256),
            ("topology_profile_sha256", &self.topology_profile_sha256),
            ("run_config_sha256", &self.run_config_sha256),
        ] {
            check_re(val, &SHA256_HEX_RE, field, "RunConfigurationV5")?;
        }

        if self.requested_output_ceiling_tokens != self.base_effective_output_ceiling_tokens {
            return Err(SchemaValidationError::validation(
                "requested and base effective output ceilings must match",
            ));
        }
        if !(self.effort_minimum_output_tokens <= self.base_effective_output_ceiling_tokens
            && self.base_effective_output_ceiling_tokens <= self.provider_max_output_tokens)
        {
            return Err(SchemaValidationError::validation(
                "base output allowance must support the fixed effort and provider",
            ));
        }
        if let Some(max_logical) = self.transport_max_logical_output_tokens
            && self.base_effective_output_ceiling_tokens > max_logical
        {
            return Err(SchemaValidationError::validation(
                "base effective output ceiling must not exceed transport max logical output",
            ));
        }
        if self.native_effort_policy == NativeEffortPolicyV5::MaximumValidNativeBudget {
            match (
                self.native_numeric_budget_cap_tokens,
                self.native_numeric_budget_min_tokens,
                self.native_visible_output_reserve_tokens,
            ) {
                (Some(cap), Some(minimum), Some(reserve)) if cap >= minimum => {
                    if self.effort_minimum_output_tokens != minimum + reserve {
                        return Err(SchemaValidationError::validation(
                            "numeric effort minimum must equal documented minimum plus reserve",
                        ));
                    }
                }
                _ => {
                    return Err(SchemaValidationError::validation(
                        "numeric maximum policy needs documented cap, minimum, reserve",
                    ));
                }
            }
        }
        if self.effort_track == EffortTrackV5::PrimaryHighest
            && (
                self.topology.as_str(),
                self.steering_mode,
                self.coupling_mode,
            ) != (
                "T0_SINGLE_MONOLITH",
                SteeringModeV5::Unsteered,
                CouplingModeV5::Coupled,
            )
        {
            return Err(SchemaValidationError::validation(
                "primary track requires the shared unsteered coupled single agent",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProviderRequestKindV5 {
    Initial,
    TransientRetry,
    QuotaContinuation,
    RecoveryContinuation,
    SegmentContinuation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProviderRequestStatusV5 {
    Completed,
    TransientFailure,
    QuotaInterruption,
    NontransientFailure,
    AmbiguousFailure,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum NormalizedDecodeStatusV5 {
    SegmentRequiresContinuation,
    TerminalNormal,
    TerminalRefusal,
    TerminalOutputLimit,
    NoResponse,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CostBasisV5 {
    ProviderBilled,
    FrozenRateEstimate,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UsageScopeV5 {
    PhysicalRequest,
    LogicalResponseCumulative,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRequestUsageV5 {
    pub request_id: String,
    pub logical_request_id: String,
    pub request_kind: ProviderRequestKindV5,
    #[serde(default)]
    pub retry_index: Option<i64>,
    #[serde(default)]
    pub quota_continuation_index: Option<i64>,
    #[serde(default)]
    pub recovery_continuation_index: Option<i64>,
    #[serde(default)]
    pub segment_continuation_index: Option<i64>,
    pub decode_segment_index: i64,
    pub attempt_id: String,
    pub agent_id: String,
    pub role: String,
    pub dispatch_round: i64,
    pub reservation_ordinal: (i64, i64, i64, String, i64),
    pub request_status: ProviderRequestStatusV5,
    #[serde(default)]
    pub post_terminal: bool,
    #[serde(default)]
    pub provider_stop_reason: Option<String>,
    #[serde(default)]
    pub provider_http_status: Option<i64>,
    #[serde(default)]
    pub provider_error_code: Option<String>,
    #[serde(default)]
    pub provider_decode_status_raw: Option<String>,
    pub normalized_decode_status: NormalizedDecodeStatusV5,
    #[serde(default)]
    pub provider_refusal_signal: Option<bool>,
    #[serde(default)]
    pub actual_native_numeric_budget_tokens: Option<i64>,
    #[serde(default)]
    pub native_generation_parameters_json: Option<String>,
    #[serde(default)]
    pub rendered_input_context_tokens: Option<i64>,
    #[serde(default)]
    pub effective_output_allowance_tokens: Option<i64>,
    #[serde(default)]
    pub input_tokens_total: Option<i64>,
    #[serde(default)]
    pub cached_input_tokens: Option<i64>,
    #[serde(default)]
    pub output_tokens_total: Option<i64>,
    #[serde(default)]
    pub reasoning_tokens_subset: Option<i64>,
    #[serde(default)]
    pub tool_serialization_tokens_subset: Option<i64>,
    #[serde(default)]
    pub provider_reported_cost_micros: Option<i64>,
    #[serde(default)]
    pub estimated_cost_micros: Option<i64>,
    pub cost_basis: CostBasisV5,
    #[serde(default)]
    pub pricing_manifest_sha256: Option<String>,
    #[serde(default)]
    pub raw_usage_fields_json: Option<String>,
    pub usage_scope: UsageScopeV5,
    #[serde(default)]
    pub currency_code: Option<String>,
}

impl StrictV5Model for ProviderRequestUsageV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.agent_id,
            &USAGE_AGENT_ID_RE,
            "agent_id",
            "ProviderRequestUsageV5",
        )?;
        check_opt_re(
            self.pricing_manifest_sha256.as_deref(),
            &SHA256_HEX_RE,
            "pricing_manifest_sha256",
            "ProviderRequestUsageV5",
        )?;
        if let Some(idx) = self.retry_index
            && !(0..=2).contains(&idx)
        {
            return Err(SchemaValidationError::validation(
                "retry_index must be in 0..=2",
            ));
        }
        for (name, val) in [
            ("quota_continuation_index", self.quota_continuation_index),
            (
                "recovery_continuation_index",
                self.recovery_continuation_index,
            ),
            (
                "segment_continuation_index",
                self.segment_continuation_index,
            ),
            ("decode_segment_index", Some(self.decode_segment_index)),
            ("dispatch_round", Some(self.dispatch_round)),
            (
                "actual_native_numeric_budget_tokens",
                self.actual_native_numeric_budget_tokens,
            ),
            (
                "rendered_input_context_tokens",
                self.rendered_input_context_tokens,
            ),
            ("input_tokens_total", self.input_tokens_total),
            ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens_total", self.output_tokens_total),
            ("reasoning_tokens_subset", self.reasoning_tokens_subset),
            (
                "tool_serialization_tokens_subset",
                self.tool_serialization_tokens_subset,
            ),
            (
                "provider_reported_cost_micros",
                self.provider_reported_cost_micros,
            ),
            ("estimated_cost_micros", self.estimated_cost_micros),
        ] {
            if let Some(v) = val
                && v < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "{name} must be >= 0"
                )));
            }
        }
        if let Some(status) = self.provider_http_status
            && !(100..=599).contains(&status)
        {
            return Err(SchemaValidationError::validation(
                "provider_http_status must be in 100..=599",
            ));
        }
        if let Some(allowance) = self.effective_output_allowance_tokens
            && allowance < 1
        {
            return Err(SchemaValidationError::validation(
                "effective_output_allowance_tokens must be >= 1",
            ));
        }

        match self.request_kind {
            ProviderRequestKindV5::Initial => {
                if self.retry_index != Some(0)
                    || self.quota_continuation_index.is_some()
                    || self.recovery_continuation_index.is_some()
                    || self.segment_continuation_index.is_some()
                    || self.decode_segment_index != 0
                {
                    return Err(SchemaValidationError::validation(
                        "INITIAL request must have retry_index=0, decode_segment_index=0, and null continuation indices",
                    ));
                }
            }
            ProviderRequestKindV5::TransientRetry => {
                if !matches!(self.retry_index, Some(1 | 2))
                    || self.quota_continuation_index.is_some()
                    || self.recovery_continuation_index.is_some()
                    || self.segment_continuation_index.is_some()
                {
                    return Err(SchemaValidationError::validation(
                        "TRANSIENT_RETRY request must have retry_index in {1, 2} and null continuation indices",
                    ));
                }
            }
            ProviderRequestKindV5::QuotaContinuation => {
                if self.quota_continuation_index.is_none()
                    || self.retry_index.is_some()
                    || self.recovery_continuation_index.is_some()
                    || self.segment_continuation_index.is_some()
                {
                    return Err(SchemaValidationError::validation(
                        "QUOTA_CONTINUATION request requires quota_continuation_index and null retry/other continuation indices",
                    ));
                }
            }
            ProviderRequestKindV5::RecoveryContinuation => {
                if self.recovery_continuation_index.is_none()
                    || self.retry_index.is_some()
                    || self.quota_continuation_index.is_some()
                    || self.segment_continuation_index.is_some()
                {
                    return Err(SchemaValidationError::validation(
                        "RECOVERY_CONTINUATION request requires recovery_continuation_index and null retry/other continuation indices",
                    ));
                }
            }
            ProviderRequestKindV5::SegmentContinuation => {
                if self.segment_continuation_index.is_none()
                    || self.decode_segment_index < 1
                    || self.retry_index.is_some()
                    || self.quota_continuation_index.is_some()
                    || self.recovery_continuation_index.is_some()
                {
                    return Err(SchemaValidationError::validation(
                        "SEGMENT_CONTINUATION request requires segment_continuation_index, decode_segment_index >= 1, and null retry/other continuation indices",
                    ));
                }
            }
        }

        if let (Some(cached), Some(total)) = (self.cached_input_tokens, self.input_tokens_total)
            && cached > total
        {
            return Err(SchemaValidationError::validation(
                "cached_input_tokens cannot exceed input_tokens_total",
            ));
        }
        if let (Some(reasoning), Some(total)) =
            (self.reasoning_tokens_subset, self.output_tokens_total)
            && reasoning > total
        {
            return Err(SchemaValidationError::validation(
                "reasoning_tokens_subset cannot exceed output_tokens_total",
            ));
        }
        if let (Some(tool_ser), Some(total)) = (
            self.tool_serialization_tokens_subset,
            self.output_tokens_total,
        ) && tool_ser > total
        {
            return Err(SchemaValidationError::validation(
                "tool_serialization_tokens_subset cannot exceed output_tokens_total",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ContextLimitCauseV5 {
    AllowanceBelowEffortMin,
    ProviderRejectionConfirmedLimit,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EpisodeAttemptV5 {
    pub attempt_id: String,
    pub episode_id: String,
    pub repeat_index: i64,
    pub run_config_sha256: String,
    pub attempt_status: AttemptStatusV5,
    pub completed_assistant_generations: i64,
    #[serde(default)]
    pub terminal_reservation_ordinal: Option<(i64, i64, i64, String, i64)>,
    #[serde(default)]
    pub terminal_cause: Option<String>,
    #[serde(default)]
    pub context_limit_cause: Option<ContextLimitCauseV5>,
    pub tool_call_count: i64,
    pub retry_request_count: i64,
    #[serde(default)]
    pub elapsed_wall_time_ms: Option<i64>,
    #[serde(default)]
    pub terminal_outcome_elapsed_ms: Option<i64>,
    #[serde(default)]
    pub active_elapsed_ms: Option<i64>,
    pub provider_requests: Vec<ProviderRequestUsageV5>,
    #[serde(default)]
    pub g_srr: Option<bool>,
    #[serde(default)]
    pub sem_srr_commit: Option<bool>,
    #[serde(default)]
    pub overgrant_count: Option<i64>,
    #[serde(default)]
    pub avail_breach_count: Option<i64>,
}

impl StrictV5Model for EpisodeAttemptV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if !(1..=3).contains(&self.repeat_index) {
            return Err(SchemaValidationError::validation(
                "EpisodeAttemptV5.repeat_index must be 1, 2, or 3",
            ));
        }
        check_re(
            &self.run_config_sha256,
            &SHA256_HEX_RE,
            "run_config_sha256",
            "EpisodeAttemptV5",
        )?;
        if !(0..=100).contains(&self.completed_assistant_generations) {
            return Err(SchemaValidationError::validation(
                "completed_assistant_generations must be in 0..=100",
            ));
        }
        if self.tool_call_count < 0 || self.retry_request_count < 0 {
            return Err(SchemaValidationError::validation(
                "tool_call_count and retry_request_count must be >= 0",
            ));
        }
        for (name, v) in [
            ("elapsed_wall_time_ms", self.elapsed_wall_time_ms),
            (
                "terminal_outcome_elapsed_ms",
                self.terminal_outcome_elapsed_ms,
            ),
            ("active_elapsed_ms", self.active_elapsed_ms),
            ("overgrant_count", self.overgrant_count),
            ("avail_breach_count", self.avail_breach_count),
        ] {
            if let Some(n) = v
                && n < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "{name} must be >= 0"
                )));
            }
        }
        for req in &self.provider_requests {
            req.validate()?;
        }

        let all_none = self.g_srr.is_none()
            && self.sem_srr_commit.is_none()
            && self.overgrant_count.is_none()
            && self.avail_breach_count.is_none();
        let all_some = self.g_srr.is_some()
            && self.sem_srr_commit.is_some()
            && self.overgrant_count.is_some()
            && self.avail_breach_count.is_some();

        if self.attempt_status.is_incomplete() {
            if !all_none {
                return Err(SchemaValidationError::validation(
                    "infrastructure-incomplete attempt metrics must be null",
                ));
            }
        } else {
            if !all_some {
                return Err(SchemaValidationError::validation(
                    "observed attempt metrics must be non-null",
                ));
            }
            if self.attempt_status.is_within_protocol_terminated()
                && (self.g_srr != Some(false) || self.sem_srr_commit != Some(false))
            {
                return Err(SchemaValidationError::validation(
                    "within-protocol terminated attempt must record g_srr=False and sem_srr_commit=False",
                ));
            }
        }

        if self.context_limit_cause.is_some()
            != (self.attempt_status == AttemptStatusV5::WithinProtocolContextLimit)
        {
            return Err(SchemaValidationError::validation(
                "context_limit_cause must be set iff attempt_status is WITHIN_PROTOCOL_CONTEXT_LIMIT",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AccountingCoverageStatusV5 {
    Complete,
    Partial,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DescriptiveUsageSummaryV5 {
    pub run_config_sha256: String,
    pub planned_attempt_slots: i64,
    pub completed_attempts: i64,
    pub incomplete_provider_quota_attempts: i64,
    pub incomplete_provider_failure_attempts: i64,
    pub incomplete_context_attempts: i64,
    pub incomplete_harness_attempts: i64,
    pub attempt_status_counts_json: String,
    #[serde(default)]
    pub elapsed_wall_time_ms: Option<i64>,
    #[serde(default)]
    pub active_elapsed_ms: Option<i64>,
    #[serde(default)]
    pub total_input_tokens: Option<i64>,
    #[serde(default)]
    pub cached_input_tokens: Option<i64>,
    #[serde(default)]
    pub total_output_tokens: Option<i64>,
    #[serde(default)]
    pub reasoning_tokens_subset: Option<i64>,
    #[serde(default)]
    pub tool_serialization_tokens_subset: Option<i64>,
    #[serde(default)]
    pub provider_billed_cost_micros: Option<i64>,
    #[serde(default)]
    pub estimated_cost_micros: Option<i64>,
    #[serde(default)]
    pub currency_code: Option<String>,
    pub accounting_coverage_status: AccountingCoverageStatusV5,
}

impl StrictV5Model for DescriptiveUsageSummaryV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.run_config_sha256,
            &SHA256_HEX_RE,
            "run_config_sha256",
            "DescriptiveUsageSummaryV5",
        )?;
        for (name, v) in [
            ("planned_attempt_slots", Some(self.planned_attempt_slots)),
            ("completed_attempts", Some(self.completed_attempts)),
            (
                "incomplete_provider_quota_attempts",
                Some(self.incomplete_provider_quota_attempts),
            ),
            (
                "incomplete_provider_failure_attempts",
                Some(self.incomplete_provider_failure_attempts),
            ),
            (
                "incomplete_context_attempts",
                Some(self.incomplete_context_attempts),
            ),
            (
                "incomplete_harness_attempts",
                Some(self.incomplete_harness_attempts),
            ),
            ("elapsed_wall_time_ms", self.elapsed_wall_time_ms),
            ("active_elapsed_ms", self.active_elapsed_ms),
            ("total_input_tokens", self.total_input_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
            ("total_output_tokens", self.total_output_tokens),
            ("reasoning_tokens_subset", self.reasoning_tokens_subset),
            (
                "tool_serialization_tokens_subset",
                self.tool_serialization_tokens_subset,
            ),
            (
                "provider_billed_cost_micros",
                self.provider_billed_cost_micros,
            ),
            ("estimated_cost_micros", self.estimated_cost_micros),
        ] {
            if let Some(n) = v
                && n < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "{name} must be >= 0"
                )));
            }
        }

        let parsed: Value =
            serde_json::from_str(&self.attempt_status_counts_json).map_err(|_| {
                SchemaValidationError::validation("attempt_status_counts_json must be valid JSON")
            })?;
        let obj = parsed.as_object().ok_or_else(|| {
            SchemaValidationError::validation(
                "attempt_status_counts_json must decode to a JSON object",
            )
        })?;

        let mut total: i64 = 0;
        let get_count = |k: &str| -> i64 {
            obj.get(k)
                .and_then(|v| v.as_number())
                .and_then(|n| n.as_i64())
                .unwrap_or(0)
        };

        for (status, count_val) in obj {
            if !CANONICAL_ATTEMPT_STATUSES_V5.contains(&status.as_str()) {
                return Err(SchemaValidationError::validation(format!(
                    "unknown attempt status in attempt_status_counts_json: {status:?}"
                )));
            }
            let count = match count_val {
                Value::Number(n) => match classify_number(n) {
                    PyNumber::Int(_) => n.as_i64().filter(|c| *c >= 0),
                    PyNumber::Float(_) => None,
                },
                _ => None,
            }
            .ok_or_else(|| {
                SchemaValidationError::validation(
                    "attempt_status_counts_json values must be non-negative integers",
                )
            })?;
            total = total.checked_add(count).ok_or_else(|| {
                SchemaValidationError::validation("attempt_status_counts_json sum overflow")
            })?;
        }

        if total != self.planned_attempt_slots {
            return Err(SchemaValidationError::validation(
                "attempt_status_counts_json sum must equal planned_attempt_slots",
            ));
        }
        if get_count("COMPLETED") != self.completed_attempts {
            return Err(SchemaValidationError::validation(
                "COMPLETED count must match completed_attempts",
            ));
        }
        if get_count("INCOMPLETE_PROVIDER_QUOTA") != self.incomplete_provider_quota_attempts {
            return Err(SchemaValidationError::validation(
                "INCOMPLETE_PROVIDER_QUOTA count must match incomplete_provider_quota_attempts",
            ));
        }
        if get_count("INCOMPLETE_PROVIDER_FAILURE") != self.incomplete_provider_failure_attempts {
            return Err(SchemaValidationError::validation(
                "INCOMPLETE_PROVIDER_FAILURE count must match incomplete_provider_failure_attempts",
            ));
        }
        if get_count("INCOMPLETE_CONTEXT_INCOMPATIBLE") != self.incomplete_context_attempts {
            return Err(SchemaValidationError::validation(
                "INCOMPLETE_CONTEXT_INCOMPATIBLE count must match incomplete_context_attempts",
            ));
        }
        if get_count("INCOMPLETE_HARNESS_FAILURE") != self.incomplete_harness_attempts {
            return Err(SchemaValidationError::validation(
                "INCOMPLETE_HARNESS_FAILURE count must match incomplete_harness_attempts",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoricalComparatorEntryV5 {
    pub model_id: String,
    pub provider_id: String,
    pub reasoning_effort_label: String,
    pub topology: String,
    pub v41_completed_scenarios: i64,
    pub execution_valid_rate_bps: i64,
    pub v41_run_manifest_sha256: String,
    #[serde(default)]
    pub input_tokens_total: Option<i64>,
    #[serde(default)]
    pub cached_input_tokens: Option<i64>,
    #[serde(default)]
    pub output_tokens_total: Option<i64>,
    #[serde(default)]
    pub reasoning_tokens_subset: Option<i64>,
    #[serde(default)]
    pub historical_cost_micros: Option<i64>,
    pub historical_cost_basis: CostBasisV5,
}

impl StrictV5Model for HistoricalComparatorEntryV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if self.v41_completed_scenarios < 0 {
            return Err(SchemaValidationError::validation(
                "v41_completed_scenarios must be >= 0",
            ));
        }
        if !(0..=10000).contains(&self.execution_valid_rate_bps) {
            return Err(SchemaValidationError::validation(
                "execution_valid_rate_bps must be in 0..=10000",
            ));
        }
        check_re(
            &self.v41_run_manifest_sha256,
            &SHA256_HEX_RE,
            "v41_run_manifest_sha256",
            "HistoricalComparatorEntryV5",
        )?;
        for (name, v) in [
            ("input_tokens_total", self.input_tokens_total),
            ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens_total", self.output_tokens_total),
            ("reasoning_tokens_subset", self.reasoning_tokens_subset),
            ("historical_cost_micros", self.historical_cost_micros),
        ] {
            if let Some(n) = v
                && n < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "{name} must be >= 0"
                )));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoricalComparatorManifestV5 {
    pub source_spec_sha256: String,
    #[serde(default)]
    pub pricing_manifest_sha256: Option<String>,
    pub entries: Vec<HistoricalComparatorEntryV5>,
}

impl StrictV5Model for HistoricalComparatorManifestV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_re(
            &self.source_spec_sha256,
            &SHA256_HEX_RE,
            "source_spec_sha256",
            "HistoricalComparatorManifestV5",
        )?;
        check_opt_re(
            self.pricing_manifest_sha256.as_deref(),
            &SHA256_HEX_RE,
            "pricing_manifest_sha256",
            "HistoricalComparatorManifestV5",
        )?;
        let mut seen = BTreeSet::new();
        for e in &self.entries {
            e.validate()?;
            let key = (
                e.model_id.as_str(),
                e.provider_id.as_str(),
                e.reasoning_effort_label.as_str(),
                e.topology.as_str(),
            );
            if !seen.insert(key) {
                return Err(SchemaValidationError::validation(
                    "historical comparator entries must be unique by exact configuration",
                ));
            }
        }
        Ok(())
    }
}
