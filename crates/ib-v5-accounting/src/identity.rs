//! Provider identity fingerprinting, operator usage attestations, manifests, and retry contracts.

use std::collections::BTreeSet;
use std::sync::LazyLock;

use ib_core::hashing::sha256_hex;
use ib_core::pyjson::{DumpOptions, dumps};
use ib_v5_schemas::{SHA256_HEX_RE, SchemaValidationError, StrictV5Model};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const V5_USAGE_ACCOUNTING_POLICY_ID: &str = "identitybench-v5-all-attempt-usage-v1";
pub const V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA: &str =
    "identitybench-v5-usage-accounting-manifest-v1";
pub const V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA: &str =
    "identitybench-v5-provider-usage-attestation-v1";
pub const V5_PROVIDER_FINGERPRINT_SCHEMA: &str = "identitybench-v5-provider-fingerprint-v1";
pub const USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5: &str =
    "all_billed_input_output_tokens_for_every_completion_and_upstream_retry";

pub const OPERATOR_OR_GATEWAY_ATTESTATION_V5: &str = "operator_or_gateway";

/// Execution role of a provider instance in a v5 topology.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderRoleV5 {
    Unified,
    Planner,
    Executor,
    Auditor,
}

impl ProviderRoleV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Unified => "unified",
            Self::Planner => "planner",
            Self::Executor => "executor",
            Self::Auditor => "auditor",
        }
    }
}

/// Attestation status for upstream provider retry/usage accounting.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UsageAccountingStatusV5 {
    Attested,
    ProviderDeclared,
    Unknown,
    NotApplicable,
}

impl UsageAccountingStatusV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Attested => "ATTESTED",
            Self::ProviderDeclared => "PROVIDER_DECLARED",
            Self::Unknown => "UNKNOWN",
            Self::NotApplicable => "NOT_APPLICABLE",
        }
    }
}

/// Hash a role and the provider's already non-secret identity, never a raw endpoint URL.
///
/// Mirrors `provider_fingerprint_sha256_v5` in `identitybench.v5.provider_accounting` bit-for-bit:
/// compact sorted JSON (`ensure_ascii=False, sort_keys=True, separators=(",", ":")`) over
/// `{schema_version, role, provider_kind, model_id, endpoint_identity}` hashed with SHA-256.
#[must_use]
pub fn provider_fingerprint_sha256_v5(
    role: &str,
    provider_kind: &str,
    model_id: &str,
    endpoint_identity: &str,
) -> String {
    let mut payload = Map::new();
    payload.insert(
        "schema_version".to_owned(),
        Value::String(V5_PROVIDER_FINGERPRINT_SCHEMA.to_owned()),
    );
    payload.insert("role".to_owned(), Value::String(role.to_owned()));
    payload.insert(
        "provider_kind".to_owned(),
        Value::String(provider_kind.to_owned()),
    );
    payload.insert("model_id".to_owned(), Value::String(model_id.to_owned()));
    payload.insert(
        "endpoint_identity".to_owned(),
        Value::String(endpoint_identity.to_owned()),
    );
    let canonical = dumps(&Value::Object(payload), &DumpOptions::compact_sorted())
        .expect("string-only JSON object serialization cannot fail");
    sha256_hex(canonical.as_bytes())
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

fn check_sha256_hex(val: &str, field: &str, model: &str) -> Result<(), SchemaValidationError> {
    if !SHA256_HEX_RE.is_match(val) {
        return Err(SchemaValidationError::validation(format!(
            "invalid {field} in {model}: {val:?}"
        )));
    }
    Ok(())
}

/// Validated per-role evidence passed from the v5 CLI to the offline runner.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageAccountingEvidenceV5 {
    pub role: ProviderRoleV5,
    pub provider_kind: String,
    pub model_id: String,
    pub provider_fingerprint_sha256: String,
    pub aggregate_usage_scope: String,
    pub evidence_sha256: String,
    pub adapter_retry_limit: i64,
}

impl StrictV5Model for UsageAccountingEvidenceV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_len_bounds(
            &self.provider_kind,
            1,
            80,
            "provider_kind",
            "UsageAccountingEvidenceV5",
        )?;
        check_len_bounds(
            &self.model_id,
            1,
            200,
            "model_id",
            "UsageAccountingEvidenceV5",
        )?;
        check_sha256_hex(
            &self.provider_fingerprint_sha256,
            "provider_fingerprint_sha256",
            "UsageAccountingEvidenceV5",
        )?;
        if self.aggregate_usage_scope != USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5 {
            return Err(SchemaValidationError::validation(format!(
                "invalid aggregate_usage_scope in UsageAccountingEvidenceV5: {:?}",
                self.aggregate_usage_scope
            )));
        }
        check_sha256_hex(
            &self.evidence_sha256,
            "evidence_sha256",
            "UsageAccountingEvidenceV5",
        )?;
        if self.adapter_retry_limit != 0 {
            return Err(SchemaValidationError::validation(
                "UsageAccountingEvidenceV5.adapter_retry_limit must be 0",
            ));
        }
        Ok(())
    }
}

/// The immutable, non-secret operator/gateway evidence document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageAccountingAttestationV5 {
    pub schema_version: String,
    pub role: ProviderRoleV5,
    pub provider_kind: String,
    pub model_id: String,
    pub provider_fingerprint_sha256: String,
    pub aggregate_usage_scope: String,
    pub attestation: String,
}

impl StrictV5Model for UsageAccountingAttestationV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if self.schema_version != V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA {
            return Err(SchemaValidationError::validation(format!(
                "invalid schema_version in UsageAccountingAttestationV5: {:?}",
                self.schema_version
            )));
        }
        check_len_bounds(
            &self.provider_kind,
            1,
            80,
            "provider_kind",
            "UsageAccountingAttestationV5",
        )?;
        check_len_bounds(
            &self.model_id,
            1,
            200,
            "model_id",
            "UsageAccountingAttestationV5",
        )?;
        check_sha256_hex(
            &self.provider_fingerprint_sha256,
            "provider_fingerprint_sha256",
            "UsageAccountingAttestationV5",
        )?;
        if self.aggregate_usage_scope != USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5 {
            return Err(SchemaValidationError::validation(format!(
                "invalid aggregate_usage_scope in UsageAccountingAttestationV5: {:?}",
                self.aggregate_usage_scope
            )));
        }
        if self.attestation != OPERATOR_OR_GATEWAY_ATTESTATION_V5 {
            return Err(SchemaValidationError::validation(format!(
                "invalid attestation in UsageAccountingAttestationV5: {:?}",
                self.attestation
            )));
        }
        Ok(())
    }
}

/// One manifest entry; the CLI verifies and copies `evidence_path` before provider setup.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageAccountingManifestEntryV5 {
    pub role: ProviderRoleV5,
    pub provider_kind: String,
    pub model_id: String,
    pub provider_fingerprint_sha256: String,
    pub aggregate_usage_scope: String,
    pub evidence_path: String,
    pub evidence_sha256: String,
    pub adapter_retry_limit: i64,
}

impl UsageAccountingManifestEntryV5 {
    /// Drop the input path after the CLI has verified and copied its immutable bytes.
    #[must_use]
    pub fn runner_evidence(&self) -> UsageAccountingEvidenceV5 {
        UsageAccountingEvidenceV5 {
            role: self.role,
            provider_kind: self.provider_kind.clone(),
            model_id: self.model_id.clone(),
            provider_fingerprint_sha256: self.provider_fingerprint_sha256.clone(),
            aggregate_usage_scope: self.aggregate_usage_scope.clone(),
            evidence_sha256: self.evidence_sha256.clone(),
            adapter_retry_limit: self.adapter_retry_limit,
        }
    }
}

impl StrictV5Model for UsageAccountingManifestEntryV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        check_len_bounds(
            &self.evidence_path,
            1,
            512,
            "evidence_path",
            "UsageAccountingManifestEntryV5",
        )?;
        self.runner_evidence().validate()
    }
}

/// Manifest binding each active role to an immutable operator/gateway usage attestation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageAccountingManifestV5 {
    pub schema_version: String,
    pub entries: Vec<UsageAccountingManifestEntryV5>,
}

impl StrictV5Model for UsageAccountingManifestV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if self.schema_version != V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA {
            return Err(SchemaValidationError::validation(format!(
                "invalid schema_version in UsageAccountingManifestV5: {:?}",
                self.schema_version
            )));
        }
        if self.entries.is_empty() {
            return Err(SchemaValidationError::validation(
                "usage-accounting manifest must contain at least one role",
            ));
        }
        let mut seen_roles = BTreeSet::new();
        for entry in &self.entries {
            entry.validate()?;
            if !seen_roles.insert(entry.role) {
                return Err(SchemaValidationError::validation(
                    "usage-accounting manifest roles must be unique",
                ));
            }
        }
        Ok(())
    }
}

/// Explicit extension contract for a provider adapter's effective local retry setting.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdapterRetryAccountingContractV5 {
    pub policy_id: String,
    pub max_retries: i64,
    pub control_source: String,
}

/// Optional v5 contract for provider adapters added without changing shared provider types.
pub trait RetryAccountingAdapterV5 {
    fn v5_retry_accounting_contract(&self) -> Option<AdapterRetryAccountingContractV5>;
}

/// Safe per-role accounting provenance serialized with one episode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderAccountingTraceV5 {
    pub role: String,
    pub provider_kind: String,
    pub model_id: String,
    pub provider_fingerprint_sha256: String,
    pub local_retry_limit: Option<i64>,
    pub local_retry_policy_id: Option<String>,
    pub retry_control_source: String,
    pub evidence_sha256: Option<String>,
    pub aggregate_usage_scope: Option<String>,
    pub status: UsageAccountingStatusV5,
    pub basis: String,
}

/// Built-in provider adapter `(module_name, class_name, adapter_retry_kind)` mappings.
pub const BUILTIN_RETRY_ADAPTERS_V5: &[(&str, &str, &str)] = &[
    (
        "identitybench.providers.openai_compat",
        "OpenAICompatProvider",
        "sdk_max_retries",
    ),
    (
        "identitybench.providers.anthropic_api",
        "AnthropicProvider",
        "sdk_max_retries",
    ),
    (
        "identitybench.providers.claude_cli",
        "ClaudeCliProvider",
        "cli_max_retries",
    ),
    (
        "identitybench.providers.codex_cli",
        "CodexCliProvider",
        "cli_max_retries",
    ),
    (
        "identitybench.providers.grok_cli",
        "GrokCliProvider",
        "cli_max_retries",
    ),
    (
        "identitybench.providers.agy_cli",
        "AgyCliProvider",
        "cli_max_retries",
    ),
    (
        "identitybench.providers.cursor_cli",
        "CursorCliProvider",
        "cli_max_retries",
    ),
    (
        "identitybench.providers.muse_cli",
        "MuseCliProvider",
        "cli_max_retries",
    ),
];

static CONTRACT_LABEL_RE_V5: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[A-Za-z0-9_.:-]{1,96}\z").expect("static contract label regex must compile")
});

/// Return whether `value` matches the v5 safe contract label pattern `^[A-Za-z0-9_.:-]{1,96}$`.
#[must_use]
pub fn safe_contract_label_v5(value: &str) -> bool {
    CONTRACT_LABEL_RE_V5.is_match(value)
}

/// Look up the built-in adapter retry kind (`"sdk_max_retries"` or `"cli_max_retries"`).
#[must_use]
pub fn builtin_retry_adapter_kind_v5(module_name: &str, class_name: &str) -> Option<&'static str> {
    BUILTIN_RETRY_ADAPTERS_V5
        .iter()
        .find(|(m, c, _)| *m == module_name && *c == class_name)
        .map(|(_, _, kind)| *kind)
}

/// Structured provider adapter identity & retry configuration metadata.
///
/// Allows `ib-providers` (or Python-mirrored test doubles) to pass adapter identity and local
/// retry settings directly to [`effective_local_retry_contract_v5`] without dynamic reflection.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ProviderIdentityInfo {
    pub module_name: String,
    pub class_name: String,
    /// Name of the retry attribute read on the adapter (must be `"_max_retries"` or `None`).
    pub retry_attr_name: Option<String>,
    /// Effective integer retry limit (`_max_retries`) on the adapter.
    pub retry_limit_value: Option<i64>,
    /// Whether the SDK adapter was constructed with an injected client object.
    pub has_injected_client: bool,
    /// Public `max_retries` read off the injected SDK client, if present.
    pub injected_client_max_retries: Option<i64>,
    /// Whether the adapter reports token usage at all.
    pub reports_usage: bool,
    /// Whether the adapter declares `usage_includes_retries = True`.
    pub usage_includes_retries: bool,
    /// Whether the adapter provides an explicit `v5_retry_accounting_contract` method.
    pub has_explicit_contract: bool,
    /// Contract returned by `v5_retry_accounting_contract()`, if `has_explicit_contract` is true.
    pub explicit_contract: Option<AdapterRetryAccountingContractV5>,
}

impl ProviderIdentityInfo {
    /// Construct metadata for a known built-in adapter without an injected SDK client.
    #[must_use]
    pub fn builtin_lazy(
        module_name: impl Into<String>,
        class_name: impl Into<String>,
        retry_limit_value: i64,
    ) -> Self {
        Self {
            module_name: module_name.into(),
            class_name: class_name.into(),
            retry_attr_name: Some("_max_retries".to_owned()),
            retry_limit_value: Some(retry_limit_value),
            has_injected_client: false,
            injected_client_max_retries: None,
            reports_usage: true,
            usage_includes_retries: false,
            has_explicit_contract: false,
            explicit_contract: None,
        }
    }

    /// Construct metadata for a known built-in SDK adapter with an injected client.
    #[must_use]
    pub fn builtin_injected(
        module_name: impl Into<String>,
        class_name: impl Into<String>,
        retry_limit_value: i64,
        injected_client_max_retries: Option<i64>,
    ) -> Self {
        Self {
            module_name: module_name.into(),
            class_name: class_name.into(),
            retry_attr_name: Some("_max_retries".to_owned()),
            retry_limit_value: Some(retry_limit_value),
            has_injected_client: true,
            injected_client_max_retries,
            reports_usage: true,
            usage_includes_retries: false,
            has_explicit_contract: false,
            explicit_contract: None,
        }
    }

    /// Construct metadata for an adapter exposing an explicit v5 retry contract.
    #[must_use]
    pub fn explicit(contract: AdapterRetryAccountingContractV5) -> Self {
        Self {
            has_explicit_contract: true,
            explicit_contract: Some(contract),
            ..Self::default()
        }
    }
}

fn validate_contract(
    contract: &AdapterRetryAccountingContractV5,
) -> Option<AdapterRetryAccountingContractV5> {
    if contract.max_retries >= 0
        && safe_contract_label_v5(&contract.policy_id)
        && safe_contract_label_v5(&contract.control_source)
    {
        Some(contract.clone())
    } else {
        None
    }
}

/// Evaluate [`RetryAccountingAdapterV5`] directly when using the explicit trait extension.
#[must_use]
pub fn effective_local_retry_contract_from_adapter_v5(
    adapter: &dyn RetryAccountingAdapterV5,
) -> Option<AdapterRetryAccountingContractV5> {
    adapter
        .v5_retry_accounting_contract()
        .and_then(|c| validate_contract(&c))
}

/// Return a proven adapter retry setting without creating clients, probing networks, or env.
///
/// Mirrors `effective_local_retry_contract_v5` in `identitybench.v5.provider_accounting`.
#[must_use]
pub fn effective_local_retry_contract_v5(
    provider: &ProviderIdentityInfo,
) -> Option<AdapterRetryAccountingContractV5> {
    if provider.has_explicit_contract {
        return provider
            .explicit_contract
            .as_ref()
            .and_then(validate_contract);
    }
    if let Some(contract) = provider.explicit_contract.as_ref() {
        return validate_contract(contract);
    }

    let adapter_kind = builtin_retry_adapter_kind_v5(&provider.module_name, &provider.class_name)?;
    if let Some(attr) = provider.retry_attr_name.as_deref()
        && attr != "_max_retries"
    {
        return None;
    }
    let max_retries = provider.retry_limit_value.filter(|&v| v >= 0)?;

    let control_source = if adapter_kind == "sdk_max_retries" {
        if provider.has_injected_client {
            if provider.injected_client_max_retries != Some(max_retries) {
                return None;
            }
            "sdk_adapter_and_injected_client_max_retries"
        } else {
            "sdk_adapter_lazy_client_max_retries"
        }
    } else {
        "cli_adapter_max_retries"
    };

    Some(AdapterRetryAccountingContractV5 {
        policy_id: format!("identitybench-v5-{adapter_kind}-max-retries-v1"),
        max_retries,
        control_source: control_source.to_owned(),
    })
}
