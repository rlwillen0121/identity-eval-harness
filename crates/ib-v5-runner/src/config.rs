//! Strict execution settings for the v5 runner.
//!
//! This mirrors `RunConfigV5` from the Python runner. The digest-bearing pricing shape lives
//! here because it is part of run configuration; conversion to scoring's rate-only shape is
//! explicit through [`PricingRatesV5::to_scoring_rates`].

use std::collections::BTreeSet;

use ib_v5_accounting::UsageAccountingEvidenceV5;
use ib_v5_schemas::{
    RunConfigurationV5, SchemaValidationError, StrictV5Model, TopologyProfileV5,
    resolve_topology_profile_v5,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::RunnerError;

pub const MAX_OUTPUT_TOKENS_V5: u64 = 64_000;
/// Legacy import-only constant for pre-Wave-3 scoring; it never stops execution.
pub const OUTPUT_BUDGET_STOP_V5: u64 = 14_500;
pub const MAX_CALLS_PER_TURN_V5: u64 = 6;
pub const DEFAULT_MAX_ASSISTANT_GENERATIONS_V5: u64 = 50;
pub const DEV_SENSITIVITY_MAX_ASSISTANT_GENERATIONS_V5: u64 = 100;

/// Frozen API-equivalent retail rates in USD micros per million tokens.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingRatesV5 {
    pub pricing_manifest_sha256: String,
    pub uncached_input_micros_per_million: i64,
    pub cached_input_micros_per_million: i64,
    pub output_micros_per_million: i64,
    pub reasoning_micros_per_million: i64,
}

impl PricingRatesV5 {
    pub fn validate(&self) -> Result<(), RunnerError> {
        if !is_lower_sha256(&self.pricing_manifest_sha256) {
            return Err(RunnerError::Configuration(
                "pricing_manifest_sha256 must be a 64-character lowercase SHA-256 hex digest"
                    .to_owned(),
            ));
        }
        for (name, value) in [
            (
                "uncached_input_micros_per_million",
                self.uncached_input_micros_per_million,
            ),
            (
                "cached_input_micros_per_million",
                self.cached_input_micros_per_million,
            ),
            ("output_micros_per_million", self.output_micros_per_million),
            (
                "reasoning_micros_per_million",
                self.reasoning_micros_per_million,
            ),
        ] {
            if value < 0 {
                return Err(RunnerError::Configuration(format!(
                    "{name} must be a nonnegative integer"
                )));
            }
        }
        Ok(())
    }

    #[must_use]
    pub const fn to_scoring_rates(&self) -> ib_v5_scoring::PricingRatesV5 {
        ib_v5_scoring::PricingRatesV5 {
            uncached_input_micros_per_million: self.uncached_input_micros_per_million,
            cached_input_micros_per_million: self.cached_input_micros_per_million,
            output_micros_per_million: self.output_micros_per_million,
            reasoning_micros_per_million: self.reasoning_micros_per_million,
        }
    }
}

/// Per-episode execution settings; provider objects are supplied separately by the caller.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct RunConfigV5 {
    pub topology_id: String,
    pub topology_profile: Option<TopologyProfileV5>,
    pub steering_mode: String,
    pub wave_execution_mode: String,
    pub effort: String,
    pub effort_track: String,
    pub temperature: Option<f64>,
    pub max_output_tokens: u64,
    pub max_assistant_generations_per_episode: u64,
    pub max_turns: Option<u64>,
    pub max_calls_per_turn: u64,
    pub cap_disclosed: bool,
    pub repeat_index: u64,
    pub pricing: Option<PricingRatesV5>,
    pub capture_transcript: bool,
    pub usage_accounting_evidence: Vec<UsageAccountingEvidenceV5>,
    pub run_configuration: Option<RunConfigurationV5>,
    pub run_config_sha256: Option<String>,
    pub native_effort_policy: String,
    pub native_numeric_budget_cap_tokens: Option<u64>,
    pub native_numeric_budget_min_tokens: Option<u64>,
    pub native_visible_output_reserve_tokens: Option<u64>,
    pub effort_minimum_output_tokens: u64,
    pub context_window_tokens: u64,
    pub provider_schema_overhead_tokens: u64,
    pub provider_max_output_tokens: u64,
    pub requested_output_ceiling_tokens: u64,
    pub base_effective_output_ceiling_tokens: u64,
    pub minimum_context_headroom_tokens: u64,
    pub transport_max_logical_output_tokens: Option<u64>,
    pub provider_segment_output_limit_tokens: Option<u64>,
    pub segment_continuation_capability: String,
    pub headroom_algorithm_id: String,
    pub tokenizer_id: String,
    pub tokenizer_version: String,
    pub request_client_timeout_s: u64,
    pub max_transient_retries: u64,
    pub max_recovery_continuations: u64,
    pub max_quota_continuations: u64,
    pub max_segment_continuations: u64,
    pub allow_request_policy_rejection_map: bool,
}

impl Default for RunConfigV5 {
    fn default() -> Self {
        Self {
            topology_id: "T0_SINGLE_MONOLITH".to_owned(),
            topology_profile: None,
            steering_mode: "UNSTEERED".to_owned(),
            wave_execution_mode: "COUPLED".to_owned(),
            effort: "medium".to_owned(),
            effort_track: "PRIMARY_HIGHEST".to_owned(),
            temperature: None,
            max_output_tokens: MAX_OUTPUT_TOKENS_V5,
            max_assistant_generations_per_episode: DEFAULT_MAX_ASSISTANT_GENERATIONS_V5,
            max_turns: None,
            max_calls_per_turn: MAX_CALLS_PER_TURN_V5,
            cap_disclosed: false,
            repeat_index: 1,
            pricing: None,
            capture_transcript: false,
            usage_accounting_evidence: Vec::new(),
            run_configuration: None,
            run_config_sha256: None,
            native_effort_policy: "FIXED_NATIVE_SETTING".to_owned(),
            native_numeric_budget_cap_tokens: None,
            native_numeric_budget_min_tokens: None,
            native_visible_output_reserve_tokens: None,
            effort_minimum_output_tokens: 1_024,
            context_window_tokens: 200_000,
            provider_schema_overhead_tokens: 0,
            provider_max_output_tokens: MAX_OUTPUT_TOKENS_V5,
            requested_output_ceiling_tokens: MAX_OUTPUT_TOKENS_V5,
            base_effective_output_ceiling_tokens: MAX_OUTPUT_TOKENS_V5,
            minimum_context_headroom_tokens: 1_024,
            transport_max_logical_output_tokens: None,
            provider_segment_output_limit_tokens: None,
            segment_continuation_capability: "NOT_REQUIRED".to_owned(),
            headroom_algorithm_id: "identitybench-v5-deterministic-headroom-v1".to_owned(),
            tokenizer_id: "tiktoken".to_owned(),
            tokenizer_version: "o200k_base".to_owned(),
            request_client_timeout_s: 600,
            max_transient_retries: 2,
            max_recovery_continuations: 5,
            max_quota_continuations: 5,
            max_segment_continuations: 16,
            allow_request_policy_rejection_map: true,
        }
    }
}

impl RunConfigV5 {
    /// Normalize the Python runner's `max_output_tokens` aliases, parse strictly, then validate.
    pub fn from_value(mut value: Value) -> Result<Self, RunnerError> {
        let Some(payload) = value.as_object_mut() else {
            return Err(RunnerError::Configuration(
                "run config must be a JSON object".to_owned(),
            ));
        };

        if let Some(max_out) = payload.get("max_output_tokens").cloned() {
            let has_requested = payload.contains_key("requested_output_ceiling_tokens");
            let has_base = payload.contains_key("base_effective_output_ceiling_tokens");
            if !has_requested && !has_base {
                payload.insert(
                    "requested_output_ceiling_tokens".to_owned(),
                    max_out.clone(),
                );
                payload.insert(
                    "base_effective_output_ceiling_tokens".to_owned(),
                    max_out.clone(),
                );
                if let Some(max_out) = max_out.as_u64() {
                    if max_out > MAX_OUTPUT_TOKENS_V5
                        && !payload.contains_key("provider_max_output_tokens")
                    {
                        payload.insert(
                            "provider_max_output_tokens".to_owned(),
                            Value::from(max_out),
                        );
                    }
                    let max_native = payload
                        .get("native_effort_policy")
                        .and_then(Value::as_str)
                        .unwrap_or("FIXED_NATIVE_SETTING");
                    if max_out < 1_024
                        && !payload.contains_key("effort_minimum_output_tokens")
                        && max_native != "MAXIMUM_VALID_NATIVE_BUDGET"
                    {
                        payload.insert(
                            "effort_minimum_output_tokens".to_owned(),
                            Value::from(max_out),
                        );
                    }
                }
            }
        }
        if payload.contains_key("requested_output_ceiling_tokens")
            && !payload.contains_key("base_effective_output_ceiling_tokens")
        {
            let requested = payload["requested_output_ceiling_tokens"].clone();
            payload.insert("base_effective_output_ceiling_tokens".to_owned(), requested);
        } else if payload.contains_key("base_effective_output_ceiling_tokens")
            && !payload.contains_key("requested_output_ceiling_tokens")
        {
            let base = payload["base_effective_output_ceiling_tokens"].clone();
            payload.insert("requested_output_ceiling_tokens".to_owned(), base);
        }

        let config: Self = serde_json::from_value(value)?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), RunnerError> {
        if !matches!(self.effort.as_str(), "low" | "medium" | "high" | "xhigh") {
            return Err(config_error("effort must be low, medium, high, or xhigh"));
        }
        if !matches!(
            self.effort_track.as_str(),
            "PRIMARY_HIGHEST" | "PREREGISTERED_LOWER" | "PREREGISTERED_ABLATION"
        ) {
            return Err(config_error("effort_track is not a recognized v5 track"));
        }
        if !matches!(
            self.steering_mode.as_str(),
            "UNSTEERED" | "CHECKPOINT_STEERED" | "FULL_LIVE_STEERED"
        ) {
            return Err(config_error("steering_mode is not a recognized v5 mode"));
        }
        if !matches!(
            self.wave_execution_mode.as_str(),
            "COUPLED" | "ORACLE_PHASE_RESET"
        ) {
            return Err(config_error(
                "wave_execution_mode is not a recognized v5 mode",
            ));
        }
        if !matches!(
            self.native_effort_policy.as_str(),
            "FIXED_NATIVE_SETTING" | "ADAPTIVE_NATIVE_MODE" | "MAXIMUM_VALID_NATIVE_BUDGET"
        ) {
            return Err(config_error(
                "native_effort_policy is not a recognized v5 policy",
            ));
        }
        if !matches!(
            self.segment_continuation_capability.as_str(),
            "NOT_REQUIRED" | "SUPPORTED" | "UNSUPPORTED" | "UNVERIFIED"
        ) {
            return Err(config_error(
                "segment_continuation_capability is not a recognized v5 value",
            ));
        }
        if self.topology_id.is_empty() {
            return Err(config_error("topology_id must be non-empty"));
        }
        for (name, value) in [
            ("max_output_tokens", Some(self.max_output_tokens)),
            (
                "native_numeric_budget_cap_tokens",
                self.native_numeric_budget_cap_tokens,
            ),
            (
                "native_numeric_budget_min_tokens",
                self.native_numeric_budget_min_tokens,
            ),
            (
                "native_visible_output_reserve_tokens",
                self.native_visible_output_reserve_tokens,
            ),
            (
                "effort_minimum_output_tokens",
                Some(self.effort_minimum_output_tokens),
            ),
            ("context_window_tokens", Some(self.context_window_tokens)),
            (
                "provider_schema_overhead_tokens",
                Some(self.provider_schema_overhead_tokens),
            ),
            (
                "provider_max_output_tokens",
                Some(self.provider_max_output_tokens),
            ),
            (
                "requested_output_ceiling_tokens",
                Some(self.requested_output_ceiling_tokens),
            ),
            (
                "base_effective_output_ceiling_tokens",
                Some(self.base_effective_output_ceiling_tokens),
            ),
            (
                "minimum_context_headroom_tokens",
                Some(self.minimum_context_headroom_tokens),
            ),
            (
                "transport_max_logical_output_tokens",
                self.transport_max_logical_output_tokens,
            ),
            (
                "provider_segment_output_limit_tokens",
                self.provider_segment_output_limit_tokens,
            ),
            (
                "request_client_timeout_s",
                Some(self.request_client_timeout_s),
            ),
        ] {
            if value.is_some_and(|value| value > i64::MAX as u64) {
                return Err(config_error(format!(
                    "{name} exceeds the runner's supported i64 range"
                )));
            }
        }
        if self.max_output_tokens < 1
            || self.context_window_tokens < 1
            || self.provider_max_output_tokens < 1
            || self.requested_output_ceiling_tokens < 1
            || self.base_effective_output_ceiling_tokens < 1
            || self.minimum_context_headroom_tokens < 1
            || self.effort_minimum_output_tokens < 1
            || self.request_client_timeout_s < 1
        {
            return Err(config_error("token and timeout values must be positive"));
        }
        if self
            .native_numeric_budget_cap_tokens
            .is_some_and(|value| value < 1)
            || self
                .native_visible_output_reserve_tokens
                .is_some_and(|value| value < 1)
            || self
                .transport_max_logical_output_tokens
                .is_some_and(|value| value < 1)
            || self
                .provider_segment_output_limit_tokens
                .is_some_and(|value| value < 1)
        {
            return Err(config_error(
                "native caps, output reserves, and segment limits must be positive when supplied",
            ));
        }
        if !(50..=100).contains(&self.max_assistant_generations_per_episode)
            || !matches!(self.max_assistant_generations_per_episode, 50 | 100)
        {
            return Err(config_error(
                "max_assistant_generations_per_episode must be 50 or 100",
            ));
        }
        if self.max_turns.is_some_and(|turns| {
            turns == 0 || turns > 100 || turns > self.max_assistant_generations_per_episode
        }) {
            let turns = self.max_turns.unwrap_or_default();
            return Err(config_error(format!(
                "max_turns ({turns}) must be in [1, 100] and cannot exceed max_assistant_generations_per_episode ({})",
                self.max_assistant_generations_per_episode
            )));
        }
        if !(1..=MAX_CALLS_PER_TURN_V5).contains(&self.max_calls_per_turn) {
            return Err(config_error(format!(
                "max_calls_per_turn must be in [1, {MAX_CALLS_PER_TURN_V5}]"
            )));
        }
        if self.cap_disclosed {
            return Err(config_error("cap_disclosed must be false"));
        }
        if !(1..=3).contains(&self.repeat_index) {
            return Err(config_error("repeat_index must be 1, 2, or 3"));
        }
        if self.max_transient_retries > 2 {
            return Err(config_error("max_transient_retries must be in [0, 2]"));
        }
        if self.requested_output_ceiling_tokens != self.base_effective_output_ceiling_tokens {
            return Err(config_error(
                "requested and base effective output ceilings must match",
            ));
        }
        if !(self.effort_minimum_output_tokens <= self.base_effective_output_ceiling_tokens
            && self.base_effective_output_ceiling_tokens <= self.provider_max_output_tokens)
        {
            return Err(config_error(
                "base output allowance must support the fixed effort and provider",
            ));
        }
        if self
            .transport_max_logical_output_tokens
            .is_some_and(|limit| self.base_effective_output_ceiling_tokens > limit)
        {
            return Err(config_error(
                "base effective output ceiling must not exceed transport max logical output",
            ));
        }
        if self.native_effort_policy == "MAXIMUM_VALID_NATIVE_BUDGET" {
            let (Some(cap), Some(minimum), Some(reserve)) = (
                self.native_numeric_budget_cap_tokens,
                self.native_numeric_budget_min_tokens,
                self.native_visible_output_reserve_tokens,
            ) else {
                return Err(config_error(
                    "numeric maximum policy needs documented cap, minimum, reserve",
                ));
            };
            if cap < minimum {
                return Err(config_error(
                    "numeric maximum policy cap cannot be less than documented minimum",
                ));
            }
            if minimum.checked_add(reserve) != Some(self.effort_minimum_output_tokens) {
                return Err(config_error(
                    "numeric effort minimum must equal documented minimum plus reserve",
                ));
            }
        }

        let profile = self.resolved_topology_profile()?;
        if let Some(run_configuration) = &self.run_configuration {
            run_configuration
                .validate()
                .map_err(|error| RunnerError::Schema(error.to_string()))?;
            if run_configuration.topology != profile.canonical_topology.as_str() {
                return Err(config_error(
                    "run_configuration.topology disagrees with resolved topology profile",
                ));
            }
            if run_configuration.topology_profile_sha256 != profile.topology_profile_sha256 {
                return Err(config_error(
                    "run_configuration.topology_profile_sha256 disagrees with resolved profile",
                ));
            }
            if run_configuration.max_assistant_generations_per_episode as u64
                != self.max_assistant_generations_per_episode
            {
                return Err(config_error(
                    "run_configuration.max_assistant_generations_per_episode disagrees",
                ));
            }
            if let Some(max_turns) = self.max_turns
                && max_turns != run_configuration.max_assistant_generations_per_episode as u64
            {
                return Err(config_error(format!(
                    "max_turns ({max_turns}) disagrees with frozen run_configuration effective_max_turns ({})",
                    run_configuration.max_assistant_generations_per_episode
                )));
            }
        }

        if let Some(digest) = &self.run_config_sha256
            && !is_lower_sha256(digest)
        {
            return Err(config_error(
                "run_config_sha256 must be a 64-character lowercase SHA-256 hex digest",
            ));
        }
        if let Some(pricing) = &self.pricing {
            pricing.validate()?;
        }
        let mut roles = BTreeSet::new();
        for evidence in &self.usage_accounting_evidence {
            evidence
                .validate()
                .map_err(|error| RunnerError::Schema(error.to_string()))?;
            let role = serde_json::to_string(&evidence.role)?;
            if !roles.insert(role) {
                return Err(config_error(
                    "usage-accounting evidence roles must be unique",
                ));
            }
        }
        Ok(())
    }

    pub fn resolved_topology_profile(&self) -> Result<TopologyProfileV5, RunnerError> {
        resolve_topology_profile_v5(&self.topology_id, self.topology_profile.as_ref())
            .map_err(|error: SchemaValidationError| RunnerError::Schema(error.to_string()))
    }

    #[must_use]
    pub fn requires_supported_segment_continuation(&self) -> bool {
        self.provider_segment_output_limit_tokens
            .is_some_and(|limit| self.base_effective_output_ceiling_tokens > limit)
    }

    #[must_use]
    pub fn is_segment_continuation_eligible(&self) -> bool {
        !self.requires_supported_segment_continuation()
            || self.segment_continuation_capability == "SUPPORTED"
    }

    #[must_use]
    pub fn normative_turn_cap(&self) -> u64 {
        self.max_assistant_generations_per_episode
    }

    #[must_use]
    pub fn effective_max_turns(&self) -> u64 {
        self.max_turns
            .unwrap_or(self.max_assistant_generations_per_episode)
    }

    pub fn runtime_topology(&self) -> Result<String, RunnerError> {
        Ok(self
            .resolved_topology_profile()?
            .canonical_topology
            .as_str()
            .to_owned())
    }

    pub fn uses_specialized_roles(&self) -> Result<bool, RunnerError> {
        Ok(self.runtime_topology()? == "T5_HIERARCHICAL_PIPELINE")
    }

    pub fn scoring_pricing(&self) -> Option<ib_v5_scoring::PricingRatesV5> {
        self.pricing.as_ref().map(PricingRatesV5::to_scoring_rates)
    }
}

impl From<RunConfigurationV5> for RunConfigV5 {
    fn from(run_configuration: RunConfigurationV5) -> Self {
        let mut config = Self::default();
        config.topology_id = run_configuration.topology.clone();
        config.steering_mode = enum_json(&run_configuration.steering_mode);
        config.wave_execution_mode = enum_json(&run_configuration.coupling_mode);
        config.effort_track = enum_json(&run_configuration.effort_track);
        config.native_effort_policy = enum_json(&run_configuration.native_effort_policy);
        config.native_numeric_budget_cap_tokens = run_configuration
            .native_numeric_budget_cap_tokens
            .and_then(|value| u64::try_from(value).ok());
        config.native_numeric_budget_min_tokens = run_configuration
            .native_numeric_budget_min_tokens
            .and_then(|value| u64::try_from(value).ok());
        config.native_visible_output_reserve_tokens = run_configuration
            .native_visible_output_reserve_tokens
            .and_then(|value| u64::try_from(value).ok());
        config.max_assistant_generations_per_episode = run_configuration
            .max_assistant_generations_per_episode
            .try_into()
            .unwrap_or(DEFAULT_MAX_ASSISTANT_GENERATIONS_V5);
        config.effort_minimum_output_tokens = run_configuration
            .effort_minimum_output_tokens
            .try_into()
            .unwrap_or(1_024);
        config.context_window_tokens = run_configuration
            .context_window_tokens
            .try_into()
            .unwrap_or(200_000);
        config.provider_schema_overhead_tokens = run_configuration
            .provider_schema_overhead_tokens
            .try_into()
            .unwrap_or_default();
        config.provider_max_output_tokens = run_configuration
            .provider_max_output_tokens
            .try_into()
            .unwrap_or(MAX_OUTPUT_TOKENS_V5);
        config.requested_output_ceiling_tokens = run_configuration
            .requested_output_ceiling_tokens
            .try_into()
            .unwrap_or(MAX_OUTPUT_TOKENS_V5);
        config.base_effective_output_ceiling_tokens = run_configuration
            .base_effective_output_ceiling_tokens
            .try_into()
            .unwrap_or(MAX_OUTPUT_TOKENS_V5);
        config.max_output_tokens = config.base_effective_output_ceiling_tokens;
        config.minimum_context_headroom_tokens = run_configuration
            .minimum_context_headroom_tokens
            .try_into()
            .unwrap_or(1_024);
        config.transport_max_logical_output_tokens = run_configuration
            .transport_max_logical_output_tokens
            .and_then(|value| value.try_into().ok());
        config.provider_segment_output_limit_tokens = run_configuration
            .provider_segment_output_limit_tokens
            .and_then(|value| value.try_into().ok());
        config.segment_continuation_capability =
            enum_json(&run_configuration.segment_continuation_capability);
        config.headroom_algorithm_id = run_configuration.headroom_algorithm_id.clone();
        config.tokenizer_id = run_configuration.tokenizer_id.clone();
        config.tokenizer_version = run_configuration.tokenizer_version.clone();
        config.request_client_timeout_s = run_configuration
            .request_client_timeout_s
            .try_into()
            .unwrap_or(600);
        config.run_configuration = Some(run_configuration);
        config
    }
}

fn enum_json<T: Serialize>(value: &T) -> String {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn config_error(message: impl Into<String>) -> RunnerError {
    RunnerError::Configuration(message.into())
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Compute the deterministic logical output allowance after context overhead and headroom.
#[must_use]
pub fn compute_effective_output_allowance_v5(
    base_effective_output_ceiling_tokens: i64,
    provider_max_output_tokens: i64,
    context_window_tokens: i64,
    rendered_input_context_tokens: i64,
    provider_schema_overhead_tokens: i64,
    minimum_context_headroom_tokens: i64,
) -> i64 {
    let remaining_context = context_window_tokens as i128
        - rendered_input_context_tokens as i128
        - provider_schema_overhead_tokens as i128
        - minimum_context_headroom_tokens as i128;
    let remaining_context = remaining_context.clamp(i64::MIN as i128, i64::MAX as i128) as i64;
    base_effective_output_ceiling_tokens
        .min(provider_max_output_tokens)
        .min(remaining_context)
}

/// Select the numeric native budget for a single logical response, if required by policy.
pub fn select_actual_native_numeric_budget_v5(
    config: &RunConfigV5,
    effective_output_allowance_tokens: i64,
) -> Result<Option<i64>, RunnerError> {
    if config.native_effort_policy != "MAXIMUM_VALID_NATIVE_BUDGET" {
        return Ok(None);
    }
    let (Some(cap), Some(reserve)) = (
        config.native_numeric_budget_cap_tokens,
        config.native_visible_output_reserve_tokens,
    ) else {
        return Err(config_error(
            "MAXIMUM_VALID_NATIVE_BUDGET requires cap and reserve",
        ));
    };
    let cap = i64::try_from(cap).map_err(|_| config_error("native budget cap exceeds i64"))?;
    let reserve =
        i64::try_from(reserve).map_err(|_| config_error("native budget reserve exceeds i64"))?;
    Ok(Some(cap.min(effective_output_allowance_tokens - reserve)))
}
