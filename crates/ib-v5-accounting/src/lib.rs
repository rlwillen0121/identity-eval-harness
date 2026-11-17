//! Explicit v5-only provider retry, usage-normalization, timing, and cost accounting.
//!
//! Mirrors `identitybench.v5.provider_accounting` for `SPEC-v5.0-draft.5.md` §2.2, §4.2 item 6,
//! and §4.3 `G-ECON-01` descriptive usage, interval-union active timing, currency-safe
//! billed/estimated cost separation, and completeness checks without spend or token ceilings.

pub mod error;
pub mod identity;
pub mod policy;
pub mod summary;

pub use error::AccountingError;
pub use ib_v5_schemas::{
    AccountingCoverageStatusV5, CANONICAL_ATTEMPT_STATUSES_V5, CostBasisV5,
    DescriptiveUsageSummaryV5, EpisodeAttemptV5, ProviderRequestUsageV5, StrictV5Model,
    UsageScopeV5,
};
pub use identity::{
    AdapterRetryAccountingContractV5, BUILTIN_RETRY_ADAPTERS_V5, OPERATOR_OR_GATEWAY_ATTESTATION_V5,
    ProviderAccountingTraceV5, ProviderIdentityInfo, ProviderRoleV5, RetryAccountingAdapterV5,
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingAttestationV5,
    UsageAccountingEvidenceV5, UsageAccountingManifestEntryV5, UsageAccountingManifestV5,
    UsageAccountingStatusV5, V5_PROVIDER_FINGERPRINT_SCHEMA,
    V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA, V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
    V5_USAGE_ACCOUNTING_POLICY_ID, builtin_retry_adapter_kind_v5,
    effective_local_retry_contract_from_adapter_v5, effective_local_retry_contract_v5,
    provider_fingerprint_sha256_v5, safe_contract_label_v5,
};
pub use policy::{
    BuildProviderRequestUsageInput, CANONICAL_PROVIDER_USAGE_POLICIES_V5,
    CurrencyConversionRateV5, FrozenPricingRateV5, HasUsageTokenCounts,
    NormalizedUsageBreakdownV5, ProviderFamilyV5, ProviderUsageAccountingPolicyV5,
    UsageInclusionSemanticsV5, build_provider_request_usage_v5, estimate_frozen_cost_micros_v5,
    get_provider_usage_policy_v5, normalize_raw_usage_v5,
};
pub use summary::{
    AttemptTimingsMsV5, DescriptiveMetricsV5, EvaluateGEcon01Options, GEcon01EvaluationV5,
    IncompleteAttemptStatusV5, SummarizeDescriptiveUsageOptions, compute_active_elapsed_ms,
    compute_attempt_timings_ms_v5, convert_micros_round_half_up,
    derive_physical_segment_usage_from_cumulative_v5, evaluate_g_econ_01_v5,
    select_contributing_usage_records_v5, summarize_descriptive_usage_v5,
};
