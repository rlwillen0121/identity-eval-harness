//! Provider usage normalization policies, raw usage normalization, frozen-rate estimation, and request record construction.

use std::sync::LazyLock;

use ib_core::pyjson::{PyNumber, classify_number, jcs_string};
use ib_v5_schemas::{
    CostBasisV5, NormalizedDecodeStatusV5, ProviderRequestKindV5, ProviderRequestStatusV5,
    ProviderRequestUsageV5, SHA256_HEX_RE, SchemaValidationError, StrictV5Model, UsageScopeV5,
};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AccountingError;

/// Inclusion semantics for sub-component token fields relative to the base token field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UsageInclusionSemanticsV5 {
    Included,
    DisjointExcluded,
    Unknown,
    NotApplicable,
}

/// Documented provider API family for usage-normalization contracts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProviderFamilyV5 {
    OpenaiResponses,
    OpenaiChatCompletions,
    AnthropicMessages,
    GoogleGeminiVertex,
    OfflineReplay,
    UnknownMapping,
    Custom,
}

impl ProviderFamilyV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::OpenaiResponses => "OPENAI_RESPONSES",
            Self::OpenaiChatCompletions => "OPENAI_CHAT_COMPLETIONS",
            Self::AnthropicMessages => "ANTHROPIC_MESSAGES",
            Self::GoogleGeminiVertex => "GOOGLE_GEMINI_VERTEX",
            Self::OfflineReplay => "OFFLINE_REPLAY",
            Self::UnknownMapping => "UNKNOWN_MAPPING",
            Self::Custom => "CUSTOM",
        }
    }
}

fn check_len_bounds(
    val: &str,
    min_chars: usize,
    max_chars: usize,
    field: &str,
    model: &str,
) -> Result<(), SchemaValidationError> {
    let len = val.chars().count();
    if len < min_chars || len > max_chars {
        return Err(SchemaValidationError::validation(format!(
            "{model}.{field} length {len} outside [{min_chars}, {max_chars}]"
        )));
    }
    Ok(())
}

/// Frozen documented usage-normalization contract for a provider/model adapter.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderUsageAccountingPolicyV5 {
    pub policy_id: String,
    pub provider_family: ProviderFamilyV5,
    pub policy_version: String,
    pub documentation_source: String,
    pub default_usage_scope: UsageScopeV5,
    #[serde(default)]
    pub input_base_field: Option<String>,
    #[serde(default)]
    pub cached_input_field: Option<String>,
    pub cached_input_inclusion: UsageInclusionSemanticsV5,
    #[serde(default)]
    pub cache_write_input_field: Option<String>,
    pub cache_write_input_inclusion: UsageInclusionSemanticsV5,
    pub cache_billing_semantics: String,
    #[serde(default)]
    pub output_base_field: Option<String>,
    #[serde(default)]
    pub reasoning_tokens_field: Option<String>,
    pub reasoning_inclusion: UsageInclusionSemanticsV5,
    #[serde(default)]
    pub tool_serialization_tokens_field: Option<String>,
    pub tool_serialization_inclusion: UsageInclusionSemanticsV5,
}

impl ProviderUsageAccountingPolicyV5 {
    /// Serialize this policy deterministically for `RunConfigurationV5`.
    pub fn to_canonical_json(&self) -> Result<String, SchemaValidationError> {
        self.to_jcs_string()
    }
}

impl StrictV5Model for ProviderUsageAccountingPolicyV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_len_bounds(
            &self.policy_id,
            1,
            128,
            "policy_id",
            "ProviderUsageAccountingPolicyV5",
        )?;
        check_len_bounds(
            &self.policy_version,
            1,
            64,
            "policy_version",
            "ProviderUsageAccountingPolicyV5",
        )?;
        check_len_bounds(
            &self.documentation_source,
            1,
            512,
            "documentation_source",
            "ProviderUsageAccountingPolicyV5",
        )?;
        check_len_bounds(
            &self.cache_billing_semantics,
            1,
            256,
            "cache_billing_semantics",
            "ProviderUsageAccountingPolicyV5",
        )?;

        if self.cached_input_inclusion == UsageInclusionSemanticsV5::DisjointExcluded
            && self.cached_input_field.is_none()
        {
            return Err(SchemaValidationError::validation(
                "DISJOINT_EXCLUDED cached_input_inclusion requires cached_input_field",
            ));
        }
        if self.cache_write_input_inclusion == UsageInclusionSemanticsV5::DisjointExcluded
            && self.cache_write_input_field.is_none()
        {
            return Err(SchemaValidationError::validation(
                "DISJOINT_EXCLUDED cache_write_input_inclusion requires cache_write_input_field",
            ));
        }
        if self.reasoning_inclusion == UsageInclusionSemanticsV5::DisjointExcluded
            && self.reasoning_tokens_field.is_none()
        {
            return Err(SchemaValidationError::validation(
                "DISJOINT_EXCLUDED reasoning_inclusion requires reasoning_tokens_field",
            ));
        }
        if self.tool_serialization_inclusion == UsageInclusionSemanticsV5::DisjointExcluded
            && self.tool_serialization_tokens_field.is_none()
        {
            return Err(SchemaValidationError::validation(
                "DISJOINT_EXCLUDED tool_serialization_inclusion requires tool_serialization_tokens_field",
            ));
        }
        if self.provider_family == ProviderFamilyV5::UnknownMapping
            && [
                self.cached_input_inclusion,
                self.cache_write_input_inclusion,
                self.reasoning_inclusion,
                self.tool_serialization_inclusion,
            ]
            .into_iter()
            .any(|inc| inc != UsageInclusionSemanticsV5::Unknown)
        {
            return Err(SchemaValidationError::validation(
                "UNKNOWN_MAPPING policy must keep all inclusion rules UNKNOWN",
            ));
        }
        Ok(())
    }
}

/// Canonical registry of frozen v5 provider usage normalization policies.
pub static CANONICAL_PROVIDER_USAGE_POLICIES_V5: LazyLock<
    IndexMap<&'static str, ProviderUsageAccountingPolicyV5>,
> = LazyLock::new(|| {
    let mut map = IndexMap::new();
    map.insert(
        "openai_responses",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-openai-responses-v1".to_owned(),
            provider_family: ProviderFamilyV5::OpenaiResponses,
            policy_version: "v1".to_owned(),
            documentation_source: "OpenAI Responses API usage object documentation (input_tokens includes input_tokens_details.cached_tokens; output_tokens includes output_tokens_details.reasoning_tokens)".to_owned(),
            default_usage_scope: UsageScopeV5::PhysicalRequest,
            input_base_field: Some("input_tokens".to_owned()),
            cached_input_field: Some("input_tokens_details.cached_tokens".to_owned()),
            cached_input_inclusion: UsageInclusionSemanticsV5::Included,
            cache_write_input_field: None,
            cache_write_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
            cache_billing_semantics:
                "cached_tokens_documented_subset_of_input_tokens;no_standard_cache_write_field"
                    .to_owned(),
            output_base_field: Some("output_tokens".to_owned()),
            reasoning_tokens_field: Some("output_tokens_details.reasoning_tokens".to_owned()),
            reasoning_inclusion: UsageInclusionSemanticsV5::Included,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        },
    );
    map.insert(
        "openai_chat_completions",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-openai-chat-completions-v1".to_owned(),
            provider_family: ProviderFamilyV5::OpenaiChatCompletions,
            policy_version: "v1".to_owned(),
            documentation_source: "OpenAI Chat Completions API usage object documentation (prompt_tokens includes prompt_tokens_details.cached_tokens; completion_tokens includes completion_tokens_details.reasoning_tokens)".to_owned(),
            default_usage_scope: UsageScopeV5::PhysicalRequest,
            input_base_field: Some("prompt_tokens".to_owned()),
            cached_input_field: Some("prompt_tokens_details.cached_tokens".to_owned()),
            cached_input_inclusion: UsageInclusionSemanticsV5::Included,
            cache_write_input_field: None,
            cache_write_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
            cache_billing_semantics:
                "cached_tokens_documented_subset_of_prompt_tokens;no_standard_cache_write_field"
                    .to_owned(),
            output_base_field: Some("completion_tokens".to_owned()),
            reasoning_tokens_field: Some("completion_tokens_details.reasoning_tokens".to_owned()),
            reasoning_inclusion: UsageInclusionSemanticsV5::Included,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        },
    );
    map.insert(
        "anthropic_messages",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-anthropic-messages-v1".to_owned(),
            provider_family: ProviderFamilyV5::AnthropicMessages,
            policy_version: "v1".to_owned(),
            documentation_source: "Anthropic Messages API usage object documentation (total_input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens; output_tokens includes thinking tokens)".to_owned(),
            default_usage_scope: UsageScopeV5::PhysicalRequest,
            input_base_field: Some("input_tokens".to_owned()),
            cached_input_field: Some("cache_read_input_tokens".to_owned()),
            cached_input_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
            cache_write_input_field: Some("cache_creation_input_tokens".to_owned()),
            cache_write_input_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
            cache_billing_semantics: "input_tokens_excludes_cache_read_and_cache_creation;cache_read_discounted;cache_creation_surcharged_preserved_in_raw".to_owned(),
            output_base_field: Some("output_tokens".to_owned()),
            reasoning_tokens_field: Some("output_tokens_details.thinking_tokens".to_owned()),
            reasoning_inclusion: UsageInclusionSemanticsV5::Included,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        },
    );
    map.insert(
        "google_gemini_vertex",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-google-gemini-vertex-v1".to_owned(),
            provider_family: ProviderFamilyV5::GoogleGeminiVertex,
            policy_version: "v1".to_owned(),
            documentation_source: "Google Gemini / Vertex AI GenerateContentResponse.usageMetadata (promptTokenCount includes cachedContentTokenCount; output = candidatesTokenCount + thoughtsTokenCount disjoint)".to_owned(),
            default_usage_scope: UsageScopeV5::PhysicalRequest,
            input_base_field: Some("promptTokenCount".to_owned()),
            cached_input_field: Some("cachedContentTokenCount".to_owned()),
            cached_input_inclusion: UsageInclusionSemanticsV5::Included,
            cache_write_input_field: None,
            cache_write_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
            cache_billing_semantics:
                "cachedContentTokenCount_documented_subset_of_promptTokenCount;no_cache_write_field"
                    .to_owned(),
            output_base_field: Some("candidatesTokenCount".to_owned()),
            reasoning_tokens_field: Some("thoughtsTokenCount".to_owned()),
            reasoning_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        },
    );
    map.insert(
        "offline_replay",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-offline-replay-v1".to_owned(),
            provider_family: ProviderFamilyV5::OfflineReplay,
            policy_version: "v1".to_owned(),
            documentation_source: "IdentityBench providers.base.Usage contract (input_tokens excludes cache_read_tokens and cache_write_tokens; output_tokens includes reasoning_tokens)".to_owned(),
            default_usage_scope: UsageScopeV5::PhysicalRequest,
            input_base_field: Some("input_tokens".to_owned()),
            cached_input_field: Some("cache_read_tokens".to_owned()),
            cached_input_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
            cache_write_input_field: Some("cache_write_tokens".to_owned()),
            cache_write_input_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
            cache_billing_semantics:
                "input_tokens_excludes_cache_read_tokens_and_cache_write_tokens;disjoint_additive"
                    .to_owned(),
            output_base_field: Some("output_tokens".to_owned()),
            reasoning_tokens_field: Some("reasoning_tokens".to_owned()),
            reasoning_inclusion: UsageInclusionSemanticsV5::Included,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        },
    );
    map.insert(
        "unknown_mapping",
        ProviderUsageAccountingPolicyV5 {
            policy_id: "identitybench-v5-usage-unknown-mapping-v1".to_owned(),
            provider_family: ProviderFamilyV5::UnknownMapping,
            policy_version: "v1".to_owned(),
            documentation_source: "UNSPECIFIED_OR_UNDOCUMENTED_MAPPING".to_owned(),
            default_usage_scope: UsageScopeV5::Unavailable,
            input_base_field: None,
            cached_input_field: None,
            cached_input_inclusion: UsageInclusionSemanticsV5::Unknown,
            cache_write_input_field: None,
            cache_write_input_inclusion: UsageInclusionSemanticsV5::Unknown,
            cache_billing_semantics: "UNKNOWN".to_owned(),
            output_base_field: None,
            reasoning_tokens_field: None,
            reasoning_inclusion: UsageInclusionSemanticsV5::Unknown,
            tool_serialization_tokens_field: None,
            tool_serialization_inclusion: UsageInclusionSemanticsV5::Unknown,
        },
    );
    map
});

/// Return a frozen documented usage-normalization policy by registry key or family.
pub fn get_provider_usage_policy_v5(
    key_or_family: &str,
) -> Result<ProviderUsageAccountingPolicyV5, AccountingError> {
    let normalized = key_or_family.trim().to_ascii_lowercase();
    if let Some(policy) = CANONICAL_PROVIDER_USAGE_POLICIES_V5.get(normalized.as_str()) {
        return Ok(policy.clone());
    }
    let upper = key_or_family.trim().to_ascii_uppercase();
    for policy in CANONICAL_PROVIDER_USAGE_POLICIES_V5.values() {
        if policy.provider_family.as_str() == upper {
            return Ok(policy.clone());
        }
    }
    Err(AccountingError::UnknownPolicy(key_or_family.to_owned()))
}

/// Frozen per-million-token pricing rate anchored to a pricing manifest SHA-256.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FrozenPricingRateV5 {
    pub pricing_manifest_sha256: String,
    pub currency_code: String,
    pub input_cost_per_million_micros: i64,
    #[serde(default)]
    pub cached_input_cost_per_million_micros: Option<i64>,
    #[serde(default)]
    pub cache_write_cost_per_million_micros: Option<i64>,
    pub output_cost_per_million_micros: i64,
    #[serde(default)]
    pub reasoning_cost_per_million_micros: Option<i64>,
}

impl StrictV5Model for FrozenPricingRateV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_len_bounds(
            &self.currency_code,
            1,
            16,
            "currency_code",
            "FrozenPricingRateV5",
        )?;
        for (field, val) in [
            (
                "input_cost_per_million_micros",
                Some(self.input_cost_per_million_micros),
            ),
            (
                "cached_input_cost_per_million_micros",
                self.cached_input_cost_per_million_micros,
            ),
            (
                "cache_write_cost_per_million_micros",
                self.cache_write_cost_per_million_micros,
            ),
            (
                "output_cost_per_million_micros",
                Some(self.output_cost_per_million_micros),
            ),
            (
                "reasoning_cost_per_million_micros",
                self.reasoning_cost_per_million_micros,
            ),
        ] {
            if let Some(v) = val
                && v < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "FrozenPricingRateV5.{field} must be >= 0"
                )));
            }
        }
        Ok(())
    }
}

/// Explicit frozen currency conversion ratio with provenance source.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CurrencyConversionRateV5 {
    pub from_currency: String,
    pub to_currency: String,
    pub rate_numerator: i64,
    pub rate_denominator: i64,
    pub rate_source: String,
    #[serde(default)]
    pub pricing_manifest_sha256: Option<String>,
}

impl StrictV5Model for CurrencyConversionRateV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_len_bounds(
            &self.from_currency,
            1,
            16,
            "from_currency",
            "CurrencyConversionRateV5",
        )?;
        check_len_bounds(
            &self.to_currency,
            1,
            16,
            "to_currency",
            "CurrencyConversionRateV5",
        )?;
        if self.rate_numerator < 1 {
            return Err(SchemaValidationError::validation(
                "CurrencyConversionRateV5.rate_numerator must be >= 1",
            ));
        }
        if self.rate_denominator < 1 {
            return Err(SchemaValidationError::validation(
                "CurrencyConversionRateV5.rate_denominator must be >= 1",
            ));
        }
        check_len_bounds(
            &self.rate_source,
            1,
            256,
            "rate_source",
            "CurrencyConversionRateV5",
        )?;
        Ok(())
    }
}

/// Normalized inclusive token totals, verified subsets, scope, and verbatim raw JSON.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NormalizedUsageBreakdownV5 {
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
    pub usage_scope: UsageScopeV5,
    #[serde(default)]
    pub raw_usage_fields_json: Option<String>,
}

impl StrictV5Model for NormalizedUsageBreakdownV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        for (field, val) in [
            ("input_tokens_total", self.input_tokens_total),
            ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens_total", self.output_tokens_total),
            ("reasoning_tokens_subset", self.reasoning_tokens_subset),
            (
                "tool_serialization_tokens_subset",
                self.tool_serialization_tokens_subset,
            ),
        ] {
            if let Some(v) = val
                && v < 0
            {
                return Err(SchemaValidationError::validation(format!(
                    "NormalizedUsageBreakdownV5.{field} must be >= 0"
                )));
            }
        }
        Ok(())
    }
}

fn lookup_raw_field<'a>(raw_usage: &'a Map<String, Value>, dotted_path: &str) -> Option<&'a Value> {
    let mut parts = dotted_path.split('.');
    let first = parts.next()?;
    let mut current = raw_usage.get(first)?;
    for part in parts {
        current = current.as_object()?.get(part)?;
    }
    Some(current)
}

fn as_non_neg_int(value: &Value) -> (bool, Option<i64>) {
    match value {
        Value::Null => (true, None),
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(text) => match text.parse::<i64>() {
                Ok(v) if v >= 0 => (true, Some(v)),
                _ => (false, None),
            },
            PyNumber::Float(_) => (false, None),
        },
        _ => (false, None),
    }
}

fn resolve_component(
    raw_usage: &Map<String, Value>,
    field_path: Option<&str>,
    inclusion: UsageInclusionSemanticsV5,
) -> (Option<i64>, i64, bool) {
    if inclusion == UsageInclusionSemanticsV5::NotApplicable {
        return (None, 0, true);
    }
    let Some(path) = field_path else {
        return (None, 0, inclusion == UsageInclusionSemanticsV5::Included);
    };
    if inclusion == UsageInclusionSemanticsV5::Unknown {
        return (None, 0, false);
    }

    let Some(raw_obj) = lookup_raw_field(raw_usage, path) else {
        return match inclusion {
            UsageInclusionSemanticsV5::Included => (None, 0, true),
            UsageInclusionSemanticsV5::DisjointExcluded => (None, 0, false),
            UsageInclusionSemanticsV5::Unknown | UsageInclusionSemanticsV5::NotApplicable => {
                (None, 0, false)
            }
        };
    };

    let (valid, parsed) = as_non_neg_int(raw_obj);
    if !valid {
        return (None, 0, false);
    }

    if inclusion == UsageInclusionSemanticsV5::Included {
        return match parsed {
            None => (None, 0, true),
            Some(v) => (Some(v), 0, true),
        };
    }

    // DISJOINT_EXCLUDED requires the documented disjoint component to be explicitly present.
    match parsed {
        None => (None, 0, false),
        Some(v) => (Some(v), v, true),
    }
}

fn normalize_input_tokens(
    raw_usage: &Map<String, Value>,
    policy: &ProviderUsageAccountingPolicyV5,
) -> (Option<i64>, Option<i64>) {
    let Some(input_base_field) = policy.input_base_field.as_deref() else {
        return (None, None);
    };
    if policy.cached_input_inclusion == UsageInclusionSemanticsV5::Unknown
        || policy.cache_write_input_inclusion == UsageInclusionSemanticsV5::Unknown
    {
        return (None, None);
    }

    let Some(raw_base_obj) = lookup_raw_field(raw_usage, input_base_field) else {
        return (None, None);
    };
    let (valid_base, Some(raw_base)) = as_non_neg_int(raw_base_obj) else {
        return (None, None);
    };
    if !valid_base {
        return (None, None);
    }

    let (cached_val, add_cached, cached_ok) = resolve_component(
        raw_usage,
        policy.cached_input_field.as_deref(),
        policy.cached_input_inclusion,
    );
    if !cached_ok {
        return (None, None);
    }

    let (cache_write_val, add_cache_write, cache_write_ok) = resolve_component(
        raw_usage,
        policy.cache_write_input_field.as_deref(),
        policy.cache_write_input_inclusion,
    );
    if !cache_write_ok {
        return (None, None);
    }

    let total_in_i128 = i128::from(raw_base) + i128::from(add_cached) + i128::from(add_cache_write);
    if let Some(cv) = cached_val
        && i128::from(cv) > total_in_i128
    {
        return (None, None);
    }
    if let Some(cwv) = cache_write_val
        && i128::from(cwv) > total_in_i128
    {
        return (None, None);
    }
    if policy.cached_input_inclusion == UsageInclusionSemanticsV5::Included
        && policy.cache_write_input_inclusion == UsageInclusionSemanticsV5::Included
        && let (Some(cv), Some(cwv)) = (cached_val, cache_write_val)
        && i128::from(cv) + i128::from(cwv) > total_in_i128
    {
        return (None, None);
    }

    let total_in = i64::try_from(total_in_i128).unwrap_or(i64::MAX);
    (Some(total_in), cached_val)
}

fn normalize_output_tokens(
    raw_usage: &Map<String, Value>,
    policy: &ProviderUsageAccountingPolicyV5,
) -> (Option<i64>, Option<i64>, Option<i64>) {
    let Some(output_base_field) = policy.output_base_field.as_deref() else {
        return (None, None, None);
    };
    if policy.reasoning_inclusion == UsageInclusionSemanticsV5::Unknown
        || policy.tool_serialization_inclusion == UsageInclusionSemanticsV5::Unknown
    {
        return (None, None, None);
    }

    let Some(raw_base_obj) = lookup_raw_field(raw_usage, output_base_field) else {
        return (None, None, None);
    };
    let (valid_base, Some(raw_base)) = as_non_neg_int(raw_base_obj) else {
        return (None, None, None);
    };
    if !valid_base {
        return (None, None, None);
    }

    let (reasoning_val, add_reasoning, reasoning_ok) = resolve_component(
        raw_usage,
        policy.reasoning_tokens_field.as_deref(),
        policy.reasoning_inclusion,
    );
    if !reasoning_ok {
        return (None, None, None);
    }

    let (tool_ser_val, add_tool_ser, tool_ser_ok) = resolve_component(
        raw_usage,
        policy.tool_serialization_tokens_field.as_deref(),
        policy.tool_serialization_inclusion,
    );
    if !tool_ser_ok {
        return (None, None, None);
    }

    let total_out_i128 =
        i128::from(raw_base) + i128::from(add_reasoning) + i128::from(add_tool_ser);
    if let Some(rv) = reasoning_val
        && i128::from(rv) > total_out_i128
    {
        return (None, None, None);
    }
    if let Some(tsv) = tool_ser_val
        && i128::from(tsv) > total_out_i128
    {
        return (None, None, None);
    }

    let total_out = i64::try_from(total_out_i128).unwrap_or(i64::MAX);
    (Some(total_out), reasoning_val, tool_ser_val)
}

/// Normalize a raw provider usage payload strictly from a frozen documented policy.
pub fn normalize_raw_usage_v5(
    raw_usage: Option<&Map<String, Value>>,
    policy: &ProviderUsageAccountingPolicyV5,
    usage_scope_override: Option<UsageScopeV5>,
) -> Result<NormalizedUsageBreakdownV5, AccountingError> {
    let Some(raw_map) = raw_usage else {
        return Ok(NormalizedUsageBreakdownV5 {
            input_tokens_total: None,
            cached_input_tokens: None,
            output_tokens_total: None,
            reasoning_tokens_subset: None,
            tool_serialization_tokens_subset: None,
            usage_scope: usage_scope_override.unwrap_or(UsageScopeV5::Unavailable),
            raw_usage_fields_json: None,
        });
    };

    let raw_json = jcs_string(&Value::Object(raw_map.clone()))?;
    let scope = usage_scope_override.unwrap_or(policy.default_usage_scope);
    if scope == UsageScopeV5::Unavailable {
        return Ok(NormalizedUsageBreakdownV5 {
            input_tokens_total: None,
            cached_input_tokens: None,
            output_tokens_total: None,
            reasoning_tokens_subset: None,
            tool_serialization_tokens_subset: None,
            usage_scope: UsageScopeV5::Unavailable,
            raw_usage_fields_json: Some(raw_json),
        });
    }

    let (input_total, cached_input) = normalize_input_tokens(raw_map, policy);
    let (output_total, reasoning_subset, tool_ser_subset) =
        normalize_output_tokens(raw_map, policy);
    Ok(NormalizedUsageBreakdownV5 {
        input_tokens_total: input_total,
        cached_input_tokens: cached_input,
        output_tokens_total: output_total,
        reasoning_tokens_subset: reasoning_subset,
        tool_serialization_tokens_subset: tool_ser_subset,
        usage_scope: scope,
        raw_usage_fields_json: Some(raw_json),
    })
}

/// Common view over normalized usage breakdowns and physical request usage rows.
pub trait HasUsageTokenCounts {
    fn input_tokens_total(&self) -> Option<i64>;
    fn cached_input_tokens(&self) -> Option<i64>;
    fn output_tokens_total(&self) -> Option<i64>;
    fn reasoning_tokens_subset(&self) -> Option<i64>;
}

impl HasUsageTokenCounts for NormalizedUsageBreakdownV5 {
    fn input_tokens_total(&self) -> Option<i64> {
        self.input_tokens_total
    }
    fn cached_input_tokens(&self) -> Option<i64> {
        self.cached_input_tokens
    }
    fn output_tokens_total(&self) -> Option<i64> {
        self.output_tokens_total
    }
    fn reasoning_tokens_subset(&self) -> Option<i64> {
        self.reasoning_tokens_subset
    }
}

impl HasUsageTokenCounts for ProviderRequestUsageV5 {
    fn input_tokens_total(&self) -> Option<i64> {
        self.input_tokens_total
    }
    fn cached_input_tokens(&self) -> Option<i64> {
        self.cached_input_tokens
    }
    fn output_tokens_total(&self) -> Option<i64> {
        self.output_tokens_total
    }
    fn reasoning_tokens_subset(&self) -> Option<i64> {
        self.reasoning_tokens_subset
    }
}

/// Estimate cost in integer micros (`ROUND_HALF_UP` to nearest integer micro, saturating at `i64::MAX`)
/// without double-charging subsets or guessing.
#[must_use]
pub fn estimate_frozen_cost_micros_v5(
    usage: &impl HasUsageTokenCounts,
    pricing_rate: &FrozenPricingRateV5,
    raw_usage: Option<&Map<String, Value>>,
    policy: Option<&ProviderUsageAccountingPolicyV5>,
) -> Option<i64> {
    let total_in = usage.input_tokens_total()?;
    let total_out = usage.output_tokens_total()?;
    if total_in < 0
        || total_out < 0
        || pricing_rate.input_cost_per_million_micros < 0
        || pricing_rate.output_cost_per_million_micros < 0
    {
        return None;
    }

    let mut remaining_input = total_in;
    let mut cached_tokens: i64 = 0;
    if let Some(cached_rate) = pricing_rate.cached_input_cost_per_million_micros {
        if cached_rate < 0 {
            return None;
        }
        cached_tokens = usage.cached_input_tokens()?;
        if cached_tokens < 0 {
            return None;
        }
        remaining_input -= cached_tokens;
    }

    let mut cache_write_tokens: i64 = 0;
    if let Some(cw_rate) = pricing_rate.cache_write_cost_per_million_micros {
        if cw_rate < 0 {
            return None;
        }
        let pol = policy?;
        match pol.cache_write_input_inclusion {
            UsageInclusionSemanticsV5::NotApplicable => {
                cache_write_tokens = 0;
            }
            UsageInclusionSemanticsV5::Included | UsageInclusionSemanticsV5::DisjointExcluded => {
                let cw_field = pol.cache_write_input_field.as_deref()?;
                let raw_map = raw_usage?;
                let raw_cw = lookup_raw_field(raw_map, cw_field)?;
                let (valid, parsed_cw) = as_non_neg_int(raw_cw);
                let parsed_cw = parsed_cw?;
                if !valid {
                    return None;
                }
                cache_write_tokens = parsed_cw;
                remaining_input -= cache_write_tokens;
            }
            UsageInclusionSemanticsV5::Unknown => return None,
        }
    }

    if remaining_input < 0 {
        return None;
    }

    let mut remaining_output = total_out;
    let mut reasoning_tokens: i64 = 0;
    if let Some(reasoning_rate) = pricing_rate.reasoning_cost_per_million_micros {
        if reasoning_rate < 0 {
            return None;
        }
        reasoning_tokens = usage.reasoning_tokens_subset()?;
        if reasoning_tokens < 0 {
            return None;
        }
        remaining_output -= reasoning_tokens;
    }

    if remaining_output < 0 {
        return None;
    }

    let input_num = (remaining_input as u128)
        .saturating_mul(pricing_rate.input_cost_per_million_micros as u128)
        .saturating_add(
            (cached_tokens as u128).saturating_mul(
                pricing_rate
                    .cached_input_cost_per_million_micros
                    .unwrap_or(0) as u128,
            ),
        )
        .saturating_add(
            (cache_write_tokens as u128).saturating_mul(
                pricing_rate
                    .cache_write_cost_per_million_micros
                    .unwrap_or(0) as u128,
            ),
        );

    let output_num =
        (remaining_output as u128)
            .saturating_mul(pricing_rate.output_cost_per_million_micros as u128)
            .saturating_add((reasoning_tokens as u128).saturating_mul(
                pricing_rate.reasoning_cost_per_million_micros.unwrap_or(0) as u128,
            ));

    let total_num = input_num.saturating_add(output_num);
    let rounded = total_num.saturating_add(500_000) / 1_000_000;
    Some(rounded.min(i64::MAX as u128) as i64)
}

/// Parameters for [`build_provider_request_usage_v5`].
#[derive(Debug, Clone)]
pub struct BuildProviderRequestUsageInput<'a> {
    pub request_id: String,
    pub logical_request_id: String,
    pub request_kind: ProviderRequestKindV5,
    pub retry_index: Option<i64>,
    pub quota_continuation_index: Option<i64>,
    pub recovery_continuation_index: Option<i64>,
    pub segment_continuation_index: Option<i64>,
    pub decode_segment_index: i64,
    pub attempt_id: String,
    pub agent_id: String,
    pub role: String,
    pub dispatch_round: i64,
    pub reservation_ordinal: (i64, i64, i64, String, i64),
    pub request_status: ProviderRequestStatusV5,
    pub post_terminal: bool,
    pub provider_stop_reason: Option<String>,
    pub provider_http_status: Option<i64>,
    pub provider_error_code: Option<String>,
    pub provider_decode_status_raw: Option<String>,
    pub normalized_decode_status: NormalizedDecodeStatusV5,
    pub provider_refusal_signal: Option<bool>,
    pub actual_native_numeric_budget_tokens: Option<i64>,
    pub native_generation_parameters_json: Option<String>,
    pub rendered_input_context_tokens: Option<i64>,
    pub effective_output_allowance_tokens: Option<i64>,
    pub raw_usage: Option<&'a Map<String, Value>>,
    pub policy: &'a ProviderUsageAccountingPolicyV5,
    pub usage_scope_override: Option<UsageScopeV5>,
    pub provider_reported_cost_micros: Option<i64>,
    pub currency_code: Option<String>,
    pub pricing_rate: Option<&'a FrozenPricingRateV5>,
}

/// Normalize raw usage and cost provenance into a validated `ProviderRequestUsageV5`.
pub fn build_provider_request_usage_v5(
    input: BuildProviderRequestUsageInput<'_>,
) -> Result<ProviderRequestUsageV5, AccountingError> {
    let normalized =
        normalize_raw_usage_v5(input.raw_usage, input.policy, input.usage_scope_override)?;

    let cost_basis: CostBasisV5;
    let mut billed_micros: Option<i64> = None;
    let mut estimated_micros: Option<i64> = None;
    let mut pricing_sha256: Option<String> = None;
    let mut resolved_currency = input.currency_code;

    if let Some(reported) = input.provider_reported_cost_micros {
        if reported < 0 {
            return Err(AccountingError::Validation(
                "provider_reported_cost_micros must be a non-negative integer".to_owned(),
            ));
        }
        if resolved_currency.as_deref().is_none_or(str::is_empty) {
            return Err(AccountingError::Validation(
                "PROVIDER_BILLED cost requires a non-empty currency_code".to_owned(),
            ));
        }
        cost_basis = CostBasisV5::ProviderBilled;
        billed_micros = Some(reported);
    } else if let Some(rate) = input.pricing_rate {
        if let Some(ref curr) = resolved_currency
            && curr != &rate.currency_code
        {
            return Err(AccountingError::Validation(format!(
                "currency_code {curr:?} conflicts with pricing_rate currency {:?}",
                rate.currency_code
            )));
        }
        estimated_micros =
            estimate_frozen_cost_micros_v5(&normalized, rate, input.raw_usage, Some(input.policy));
        if estimated_micros.is_some() {
            if !SHA256_HEX_RE.is_match(&rate.pricing_manifest_sha256) {
                return Err(AccountingError::Schema(SchemaValidationError::validation(
                    format!(
                        "invalid pricing_manifest_sha256 in FrozenPricingRateV5: {:?}",
                        rate.pricing_manifest_sha256
                    ),
                )));
            }
            cost_basis = CostBasisV5::FrozenRateEstimate;
            pricing_sha256 = Some(rate.pricing_manifest_sha256.clone());
            resolved_currency = Some(rate.currency_code.clone());
        } else {
            cost_basis = CostBasisV5::Unavailable;
        }
    } else {
        cost_basis = CostBasisV5::Unavailable;
    }

    let record = ProviderRequestUsageV5 {
        request_id: input.request_id,
        logical_request_id: input.logical_request_id,
        request_kind: input.request_kind,
        retry_index: input.retry_index,
        quota_continuation_index: input.quota_continuation_index,
        recovery_continuation_index: input.recovery_continuation_index,
        segment_continuation_index: input.segment_continuation_index,
        decode_segment_index: input.decode_segment_index,
        attempt_id: input.attempt_id,
        agent_id: input.agent_id,
        role: input.role,
        dispatch_round: input.dispatch_round,
        reservation_ordinal: input.reservation_ordinal,
        request_status: input.request_status,
        post_terminal: input.post_terminal,
        provider_stop_reason: input.provider_stop_reason,
        provider_http_status: input.provider_http_status,
        provider_error_code: input.provider_error_code,
        provider_decode_status_raw: input.provider_decode_status_raw,
        normalized_decode_status: input.normalized_decode_status,
        provider_refusal_signal: input.provider_refusal_signal,
        actual_native_numeric_budget_tokens: input.actual_native_numeric_budget_tokens,
        native_generation_parameters_json: input.native_generation_parameters_json,
        rendered_input_context_tokens: input.rendered_input_context_tokens,
        effective_output_allowance_tokens: input.effective_output_allowance_tokens,
        input_tokens_total: normalized.input_tokens_total,
        cached_input_tokens: normalized.cached_input_tokens,
        output_tokens_total: normalized.output_tokens_total,
        reasoning_tokens_subset: normalized.reasoning_tokens_subset,
        tool_serialization_tokens_subset: normalized.tool_serialization_tokens_subset,
        provider_reported_cost_micros: billed_micros,
        estimated_cost_micros: estimated_micros,
        cost_basis,
        pricing_manifest_sha256: pricing_sha256,
        raw_usage_fields_json: normalized.raw_usage_fields_json,
        usage_scope: normalized.usage_scope,
        currency_code: resolved_currency,
    };
    record.validate()?;
    Ok(record)
}
