use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use ib_v5_accounting::{
    AccountingCoverageStatusV5, AdapterRetryAccountingContractV5, BuildProviderRequestUsageInput,
    CANONICAL_ATTEMPT_STATUSES_V5, CANONICAL_PROVIDER_USAGE_POLICIES_V5, CostBasisV5,
    CurrencyConversionRateV5, DescriptiveUsageSummaryV5, EpisodeAttemptV5, EvaluateGEcon01Options,
    FrozenPricingRateV5, OPERATOR_OR_GATEWAY_ATTESTATION_V5, ProviderFamilyV5,
    ProviderIdentityInfo, ProviderRoleV5, ProviderUsageAccountingPolicyV5,
    RetryAccountingAdapterV5, StrictV5Model, SummarizeDescriptiveUsageOptions,
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingAttestationV5,
    UsageAccountingEvidenceV5, UsageAccountingManifestEntryV5, UsageAccountingManifestV5,
    UsageInclusionSemanticsV5, V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
    V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA, build_provider_request_usage_v5,
    compute_active_elapsed_ms, compute_attempt_timings_ms_v5, convert_micros_round_half_up,
    derive_physical_segment_usage_from_cumulative_v5,
    effective_local_retry_contract_from_adapter_v5, effective_local_retry_contract_v5,
    estimate_frozen_cost_micros_v5, evaluate_g_econ_01_v5, get_provider_usage_policy_v5,
    normalize_raw_usage_v5, provider_fingerprint_sha256_v5, select_contributing_usage_records_v5,
    summarize_descriptive_usage_v5,
};
use ib_v5_schemas::{
    AttemptStatusV5, NormalizedDecodeStatusV5, ProviderRequestKindV5, ProviderRequestStatusV5,
    ProviderRequestUsageV5, UsageScopeV5,
};
use serde_json::{Map, Value, json};

const RUN_SHA: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const PRICING_SHA: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const RATE_SHA: &str = "3333333333333333333333333333333333333333333333333333333333333333";
const EP_SHA: &str = "aaaaaaaaaaaaaaaa";
const EP_SHA_B: &str = "bbbbbbbbbbbbbbbb";
const EVIDENCE_SHA: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn fingerprint(role: &str) -> String {
    provider_fingerprint_sha256_v5(role, "openai_compatible", "test-model", EP_SHA)
}

fn json_obj(val: Value) -> Map<String, Value> {
    val.as_object().expect("JSON object").clone()
}

struct ReqBuilder {
    request_id: String,
    logical_request_id: String,
    request_kind: ProviderRequestKindV5,
    retry_index: Option<i64>,
    segment_continuation_index: Option<i64>,
    decode_segment_index: i64,
    attempt_id: String,
    request_status: ProviderRequestStatusV5,
    post_terminal: bool,
    raw_usage: Option<Map<String, Value>>,
    policy: ProviderUsageAccountingPolicyV5,
    usage_scope_override: Option<UsageScopeV5>,
    provider_reported_cost_micros: Option<i64>,
    currency_code: Option<String>,
    pricing_rate: Option<FrozenPricingRateV5>,
}

impl ReqBuilder {
    fn new(request_id: &str) -> Self {
        Self {
            request_id: request_id.to_owned(),
            logical_request_id: "lreq_1".to_owned(),
            request_kind: ProviderRequestKindV5::Initial,
            retry_index: Some(0),
            segment_continuation_index: None,
            decode_segment_index: 0,
            attempt_id: "att_1".to_owned(),
            request_status: ProviderRequestStatusV5::Completed,
            post_terminal: false,
            raw_usage: None,
            policy: get_provider_usage_policy_v5("openai_responses").unwrap(),
            usage_scope_override: None,
            provider_reported_cost_micros: None,
            currency_code: None,
            pricing_rate: None,
        }
    }

    fn build(self) -> ProviderRequestUsageV5 {
        build_provider_request_usage_v5(BuildProviderRequestUsageInput {
            request_id: self.request_id,
            logical_request_id: self.logical_request_id,
            request_kind: self.request_kind,
            retry_index: self.retry_index,
            quota_continuation_index: None,
            recovery_continuation_index: None,
            segment_continuation_index: self.segment_continuation_index,
            decode_segment_index: self.decode_segment_index,
            attempt_id: self.attempt_id,
            agent_id: "unified".to_owned(),
            role: "unified".to_owned(),
            dispatch_round: 0,
            reservation_ordinal: (1, 1, 0, "unified".to_owned(), 0),
            request_status: self.request_status,
            post_terminal: self.post_terminal,
            provider_stop_reason: None,
            provider_http_status: None,
            provider_error_code: None,
            provider_decode_status_raw: None,
            normalized_decode_status: NormalizedDecodeStatusV5::TerminalNormal,
            provider_refusal_signal: None,
            actual_native_numeric_budget_tokens: None,
            native_generation_parameters_json: None,
            rendered_input_context_tokens: None,
            effective_output_allowance_tokens: None,
            raw_usage: self.raw_usage.as_ref(),
            policy: &self.policy,
            usage_scope_override: self.usage_scope_override,
            provider_reported_cost_micros: self.provider_reported_cost_micros,
            currency_code: self.currency_code,
            pricing_rate: self.pricing_rate.as_ref(),
        })
        .unwrap()
    }
}

fn make_attempt(
    attempt_id: &str,
    repeat_index: i64,
    attempt_status: AttemptStatusV5,
    provider_requests: Vec<ProviderRequestUsageV5>,
    completed_assistant_generations: i64,
    retry_request_count: i64,
) -> EpisodeAttemptV5 {
    let is_incomplete = attempt_status.is_incomplete();
    let is_within = attempt_status.is_within_protocol_terminated();
    EpisodeAttemptV5 {
        attempt_id: attempt_id.to_owned(),
        episode_id: "ep_1".to_owned(),
        repeat_index,
        run_config_sha256: RUN_SHA.to_owned(),
        attempt_status,
        completed_assistant_generations,
        terminal_reservation_ordinal: None,
        terminal_cause: None,
        context_limit_cause: None,
        tool_call_count: if is_incomplete { 0 } else { 1 },
        retry_request_count,
        elapsed_wall_time_ms: Some(1000),
        terminal_outcome_elapsed_ms: Some(900),
        active_elapsed_ms: Some(700),
        provider_requests,
        g_srr: if is_incomplete {
            None
        } else {
            Some(!is_within)
        },
        sem_srr_commit: if is_incomplete {
            None
        } else {
            Some(!is_within)
        },
        overgrant_count: if is_incomplete { None } else { Some(0) },
        avail_breach_count: if is_incomplete { None } else { Some(0) },
    }
}

#[test]
fn provider_fingerprint_matches_python_golden_and_binds_role_and_endpoint() {
    let initial = fingerprint("unified");
    assert_eq!(initial.len(), 64);
    assert_eq!(initial, fingerprint("unified"));
    assert_eq!(
        initial,
        "0f1721c6c3a8f2323da0528f03dd181329539584708d472c52e4630d600dfce1"
    );
    let exec_fp = fingerprint("executor");
    assert_ne!(initial, exec_fp);
    assert_eq!(
        exec_fp,
        "562614e5161c5545de7d415a09fea5aa8307e774b234bb067cb53c1a8b68231d"
    );
    assert_ne!(
        initial,
        provider_fingerprint_sha256_v5("unified", "openai_compatible", "test-model", EP_SHA_B)
    );
    // Ensure ensure_ascii=False parity with non-ASCII Unicode
    assert_eq!(
        provider_fingerprint_sha256_v5("unified", "agy_cli", "modèle-é-α", "ba12dfe1a111d492"),
        "b98a1408a122272e9299ee9332a9e6a00acecbfea81b226d50ed5fd8c0343375"
    );
}

#[test]
fn manifest_and_attestation_contracts_are_strict_and_unique_by_role() {
    let fp = fingerprint("unified");
    let evidence_json = json!({
        "role": "unified",
        "provider_kind": "openai_compatible",
        "model_id": "test-model",
        "provider_fingerprint_sha256": fp,
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "evidence_sha256": EVIDENCE_SHA,
        "adapter_retry_limit": 0,
    });
    let parsed = UsageAccountingEvidenceV5::from_strict_json_value(&evidence_json).unwrap();
    assert_eq!(parsed.evidence_sha256, EVIDENCE_SHA);
    assert_eq!(parsed.adapter_retry_limit, 0);

    let attestation = UsageAccountingAttestationV5 {
        schema_version: V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA.to_owned(),
        role: ProviderRoleV5::Unified,
        provider_kind: "openai_compatible".to_owned(),
        model_id: "test-model".to_owned(),
        provider_fingerprint_sha256: fp.clone(),
        aggregate_usage_scope: USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5.to_owned(),
        attestation: OPERATOR_OR_GATEWAY_ATTESTATION_V5.to_owned(),
    };
    attestation.validate().unwrap();
    assert_eq!(attestation.attestation, "operator_or_gateway");

    let entry = UsageAccountingManifestEntryV5 {
        role: ProviderRoleV5::Unified,
        provider_kind: "openai_compatible".to_owned(),
        model_id: "test-model".to_owned(),
        provider_fingerprint_sha256: fp,
        aggregate_usage_scope: USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5.to_owned(),
        evidence_path: "attestations/usage.json".to_owned(),
        evidence_sha256: EVIDENCE_SHA.to_owned(),
        adapter_retry_limit: 0,
    };
    let manifest = UsageAccountingManifestV5 {
        schema_version: V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA.to_owned(),
        entries: vec![entry.clone()],
    };
    manifest.validate().unwrap();
    assert_eq!(manifest.entries[0].runner_evidence(), parsed);

    // Reject boolean in strict integer field
    let mut bad_bool = evidence_json.clone();
    bad_bool["adapter_retry_limit"] = json!(true);
    assert!(UsageAccountingEvidenceV5::from_strict_json_value(&bad_bool).is_err());

    // Reject unknown field
    let mut extra_field = evidence_json;
    extra_field["unexpected"] = json!("nope");
    assert!(UsageAccountingEvidenceV5::from_strict_json_value(&extra_field).is_err());

    // Reject duplicate roles in manifest
    let dup_manifest = UsageAccountingManifestV5 {
        schema_version: V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA.to_owned(),
        entries: vec![entry.clone(), entry],
    };
    assert!(dup_manifest.validate().is_err());
}

#[test]
fn sdk_retry_contract_checks_adapter_and_client_without_creation() {
    let lazy_provider = ProviderIdentityInfo::builtin_lazy(
        "identitybench.providers.openai_compat",
        "OpenAICompatProvider",
        0,
    );
    let lazy_contract = effective_local_retry_contract_v5(&lazy_provider).unwrap();
    assert_eq!(lazy_contract.max_retries, 0);
    assert_eq!(
        lazy_contract.control_source,
        "sdk_adapter_lazy_client_max_retries"
    );
    assert_eq!(
        lazy_contract.policy_id,
        "identitybench-v5-sdk_max_retries-max-retries-v1"
    );

    let injected_provider = ProviderIdentityInfo::builtin_injected(
        "identitybench.providers.openai_compat",
        "OpenAICompatProvider",
        0,
        Some(0),
    );
    let injected_contract = effective_local_retry_contract_v5(&injected_provider).unwrap();
    assert_eq!(injected_contract.max_retries, 0);
    assert_eq!(
        injected_contract.control_source,
        "sdk_adapter_and_injected_client_max_retries"
    );

    let mismatched_client = ProviderIdentityInfo::builtin_injected(
        "identitybench.providers.openai_compat",
        "OpenAICompatProvider",
        0,
        Some(2),
    );
    assert!(effective_local_retry_contract_v5(&mismatched_client).is_none());

    // CLI adapter checks out cleanly
    let agy_cli =
        ProviderIdentityInfo::builtin_lazy("identitybench.providers.agy_cli", "AgyCliProvider", 0);
    let cli_contract = effective_local_retry_contract_v5(&agy_cli).unwrap();
    assert_eq!(cli_contract.control_source, "cli_adapter_max_retries");
    assert_eq!(
        cli_contract.policy_id,
        "identitybench-v5-cli_max_retries-max-retries-v1"
    );

    // Wrong retry attribute name must not gain retry proof
    let mut wrong_attr = lazy_provider;
    wrong_attr.retry_attr_name = Some("max_retries".to_owned());
    assert!(effective_local_retry_contract_v5(&wrong_attr).is_none());
}

#[test]
fn unrecognized_adapter_does_not_gain_retry_proof() {
    let provider = ProviderIdentityInfo {
        module_name: "types".to_owned(),
        class_name: "SimpleNamespace".to_owned(),
        retry_attr_name: Some("_max_retries".to_owned()),
        retry_limit_value: Some(0),
        ..ProviderIdentityInfo::default()
    };
    assert!(effective_local_retry_contract_v5(&provider).is_none());
}

#[test]
fn explicit_v5_adapter_contract_is_supported() {
    struct ExplicitAdapter;
    impl RetryAccountingAdapterV5 for ExplicitAdapter {
        fn v5_retry_accounting_contract(&self) -> Option<AdapterRetryAccountingContractV5> {
            Some(AdapterRetryAccountingContractV5 {
                policy_id: "test-adapter-retry-v1".to_owned(),
                max_retries: 0,
                control_source: "test_adapter_contract".to_owned(),
            })
        }
    }

    let info = ProviderIdentityInfo::explicit(AdapterRetryAccountingContractV5 {
        policy_id: "test-adapter-retry-v1".to_owned(),
        max_retries: 0,
        control_source: "test_adapter_contract".to_owned(),
    });
    let contract = effective_local_retry_contract_v5(&info).unwrap();
    assert_eq!(contract.policy_id, "test-adapter-retry-v1");
    assert_eq!(contract.max_retries, 0);

    let trait_contract = effective_local_retry_contract_from_adapter_v5(&ExplicitAdapter).unwrap();
    assert_eq!(trait_contract, contract);

    // Invalid explicit contract label or failing explicit hook must NOT fall back to builtin
    let mut invalid_explicit = ProviderIdentityInfo::builtin_lazy(
        "identitybench.providers.openai_compat",
        "OpenAICompatProvider",
        0,
    );
    invalid_explicit.has_explicit_contract = true;
    invalid_explicit.explicit_contract = Some(AdapterRetryAccountingContractV5 {
        policy_id: "invalid spaces!".to_owned(),
        max_retries: 0,
        control_source: "test".to_owned(),
    });
    assert!(effective_local_retry_contract_v5(&invalid_explicit).is_none());
}

#[test]
fn canonical_policy_registry_matches_python_jcs_golden_and_validates() {
    let keys: Vec<&str> = CANONICAL_PROVIDER_USAGE_POLICIES_V5
        .keys()
        .copied()
        .collect();
    assert_eq!(
        keys,
        vec![
            "openai_responses",
            "openai_chat_completions",
            "anthropic_messages",
            "google_gemini_vertex",
            "offline_replay",
            "unknown_mapping",
        ]
    );
    assert_eq!(
        get_provider_usage_policy_v5("OPENAI_RESPONSES").unwrap(),
        CANONICAL_PROVIDER_USAGE_POLICIES_V5["openai_responses"]
    );
    assert_eq!(
        get_provider_usage_policy_v5("anthropic_messages")
            .unwrap()
            .to_canonical_json()
            .unwrap(),
        get_provider_usage_policy_v5("ANTHROPIC_MESSAGES")
            .unwrap()
            .to_canonical_json()
            .unwrap()
    );
    assert!(get_provider_usage_policy_v5("mystery_provider").is_err());

    let expected_golden = [
        (
            "openai_responses",
            r#"{"cache_billing_semantics":"cached_tokens_documented_subset_of_input_tokens;no_standard_cache_write_field","cache_write_input_field":null,"cache_write_input_inclusion":"NOT_APPLICABLE","cached_input_field":"input_tokens_details.cached_tokens","cached_input_inclusion":"INCLUDED","default_usage_scope":"PHYSICAL_REQUEST","documentation_source":"OpenAI Responses API usage object documentation (input_tokens includes input_tokens_details.cached_tokens; output_tokens includes output_tokens_details.reasoning_tokens)","input_base_field":"input_tokens","output_base_field":"output_tokens","policy_id":"identitybench-v5-usage-openai-responses-v1","policy_version":"v1","provider_family":"OPENAI_RESPONSES","reasoning_inclusion":"INCLUDED","reasoning_tokens_field":"output_tokens_details.reasoning_tokens","tool_serialization_inclusion":"NOT_APPLICABLE","tool_serialization_tokens_field":null}"#,
        ),
        (
            "openai_chat_completions",
            r#"{"cache_billing_semantics":"cached_tokens_documented_subset_of_prompt_tokens;no_standard_cache_write_field","cache_write_input_field":null,"cache_write_input_inclusion":"NOT_APPLICABLE","cached_input_field":"prompt_tokens_details.cached_tokens","cached_input_inclusion":"INCLUDED","default_usage_scope":"PHYSICAL_REQUEST","documentation_source":"OpenAI Chat Completions API usage object documentation (prompt_tokens includes prompt_tokens_details.cached_tokens; completion_tokens includes completion_tokens_details.reasoning_tokens)","input_base_field":"prompt_tokens","output_base_field":"completion_tokens","policy_id":"identitybench-v5-usage-openai-chat-completions-v1","policy_version":"v1","provider_family":"OPENAI_CHAT_COMPLETIONS","reasoning_inclusion":"INCLUDED","reasoning_tokens_field":"completion_tokens_details.reasoning_tokens","tool_serialization_inclusion":"NOT_APPLICABLE","tool_serialization_tokens_field":null}"#,
        ),
        (
            "anthropic_messages",
            r#"{"cache_billing_semantics":"input_tokens_excludes_cache_read_and_cache_creation;cache_read_discounted;cache_creation_surcharged_preserved_in_raw","cache_write_input_field":"cache_creation_input_tokens","cache_write_input_inclusion":"DISJOINT_EXCLUDED","cached_input_field":"cache_read_input_tokens","cached_input_inclusion":"DISJOINT_EXCLUDED","default_usage_scope":"PHYSICAL_REQUEST","documentation_source":"Anthropic Messages API usage object documentation (total_input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens; output_tokens includes thinking tokens)","input_base_field":"input_tokens","output_base_field":"output_tokens","policy_id":"identitybench-v5-usage-anthropic-messages-v1","policy_version":"v1","provider_family":"ANTHROPIC_MESSAGES","reasoning_inclusion":"INCLUDED","reasoning_tokens_field":"output_tokens_details.thinking_tokens","tool_serialization_inclusion":"NOT_APPLICABLE","tool_serialization_tokens_field":null}"#,
        ),
        (
            "google_gemini_vertex",
            r#"{"cache_billing_semantics":"cachedContentTokenCount_documented_subset_of_promptTokenCount;no_cache_write_field","cache_write_input_field":null,"cache_write_input_inclusion":"NOT_APPLICABLE","cached_input_field":"cachedContentTokenCount","cached_input_inclusion":"INCLUDED","default_usage_scope":"PHYSICAL_REQUEST","documentation_source":"Google Gemini / Vertex AI GenerateContentResponse.usageMetadata (promptTokenCount includes cachedContentTokenCount; output = candidatesTokenCount + thoughtsTokenCount disjoint)","input_base_field":"promptTokenCount","output_base_field":"candidatesTokenCount","policy_id":"identitybench-v5-usage-google-gemini-vertex-v1","policy_version":"v1","provider_family":"GOOGLE_GEMINI_VERTEX","reasoning_inclusion":"DISJOINT_EXCLUDED","reasoning_tokens_field":"thoughtsTokenCount","tool_serialization_inclusion":"NOT_APPLICABLE","tool_serialization_tokens_field":null}"#,
        ),
        (
            "offline_replay",
            r#"{"cache_billing_semantics":"input_tokens_excludes_cache_read_tokens_and_cache_write_tokens;disjoint_additive","cache_write_input_field":"cache_write_tokens","cache_write_input_inclusion":"DISJOINT_EXCLUDED","cached_input_field":"cache_read_tokens","cached_input_inclusion":"DISJOINT_EXCLUDED","default_usage_scope":"PHYSICAL_REQUEST","documentation_source":"IdentityBench providers.base.Usage contract (input_tokens excludes cache_read_tokens and cache_write_tokens; output_tokens includes reasoning_tokens)","input_base_field":"input_tokens","output_base_field":"output_tokens","policy_id":"identitybench-v5-usage-offline-replay-v1","policy_version":"v1","provider_family":"OFFLINE_REPLAY","reasoning_inclusion":"INCLUDED","reasoning_tokens_field":"reasoning_tokens","tool_serialization_inclusion":"NOT_APPLICABLE","tool_serialization_tokens_field":null}"#,
        ),
        (
            "unknown_mapping",
            r#"{"cache_billing_semantics":"UNKNOWN","cache_write_input_field":null,"cache_write_input_inclusion":"UNKNOWN","cached_input_field":null,"cached_input_inclusion":"UNKNOWN","default_usage_scope":"UNAVAILABLE","documentation_source":"UNSPECIFIED_OR_UNDOCUMENTED_MAPPING","input_base_field":null,"output_base_field":null,"policy_id":"identitybench-v5-usage-unknown-mapping-v1","policy_version":"v1","provider_family":"UNKNOWN_MAPPING","reasoning_inclusion":"UNKNOWN","reasoning_tokens_field":null,"tool_serialization_inclusion":"UNKNOWN","tool_serialization_tokens_field":null}"#,
        ),
    ];
    for (key, expected_jcs) in expected_golden {
        let policy = get_provider_usage_policy_v5(key).unwrap();
        assert_eq!(policy.to_canonical_json().unwrap(), expected_jcs);
    }

    let bad_disjoint = ProviderUsageAccountingPolicyV5 {
        policy_id: "bad-disjoint".to_owned(),
        provider_family: ProviderFamilyV5::Custom,
        policy_version: "v1".to_owned(),
        documentation_source: "doc".to_owned(),
        default_usage_scope: UsageScopeV5::PhysicalRequest,
        input_base_field: Some("input_tokens".to_owned()),
        cached_input_field: None,
        cached_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        cache_write_input_field: None,
        cache_write_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        cache_billing_semantics: "none".to_owned(),
        output_base_field: Some("output_tokens".to_owned()),
        reasoning_tokens_field: None,
        reasoning_inclusion: UsageInclusionSemanticsV5::DisjointExcluded,
        tool_serialization_tokens_field: None,
        tool_serialization_inclusion: UsageInclusionSemanticsV5::NotApplicable,
    };
    assert!(bad_disjoint.validate().is_err());
}

#[test]
fn normalization_included_vs_disjoint_vs_unknown_and_contradictory() {
    // 1. OpenAI Responses
    let raw_oai_resp = json_obj(json!({
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 400},
        "output_tokens": 300,
        "output_tokens_details": {"reasoning_tokens": 120},
    }));
    let norm_oai_resp = normalize_raw_usage_v5(
        Some(&raw_oai_resp),
        &get_provider_usage_policy_v5("openai_responses").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_oai_resp.input_tokens_total, Some(1200));
    assert_eq!(norm_oai_resp.cached_input_tokens, Some(400));
    assert_eq!(norm_oai_resp.output_tokens_total, Some(300));
    assert_eq!(norm_oai_resp.reasoning_tokens_subset, Some(120));
    assert_eq!(norm_oai_resp.tool_serialization_tokens_subset, None);

    // 2. OpenAI Chat Completions
    let raw_oai_chat = json_obj(json!({
        "prompt_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 300},
        "completion_tokens": 250,
        "completion_tokens_details": {"reasoning_tokens": 100},
    }));
    let norm_oai_chat = normalize_raw_usage_v5(
        Some(&raw_oai_chat),
        &get_provider_usage_policy_v5("openai_chat_completions").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_oai_chat.input_tokens_total, Some(900));
    assert_eq!(norm_oai_chat.cached_input_tokens, Some(300));
    assert_eq!(norm_oai_chat.output_tokens_total, Some(250));
    assert_eq!(norm_oai_chat.reasoning_tokens_subset, Some(100));

    // 3. Anthropic Messages
    let raw_anth = json_obj(json!({
        "input_tokens": 500,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 250,
        "output_tokens": 400,
        "cache_creation": {"ephemeral_5m_input_tokens": 250},
    }));
    let norm_anth = normalize_raw_usage_v5(
        Some(&raw_anth),
        &get_provider_usage_policy_v5("anthropic_messages").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_anth.input_tokens_total, Some(1750));
    assert_eq!(norm_anth.cached_input_tokens, Some(1000));
    assert_eq!(norm_anth.output_tokens_total, Some(400));
    assert_eq!(norm_anth.reasoning_tokens_subset, None);
    let parsed_raw: Value =
        serde_json::from_str(norm_anth.raw_usage_fields_json.as_deref().unwrap()).unwrap();
    assert_eq!(parsed_raw["cache_creation_input_tokens"], json!(250));

    // Missing disjoint cache_creation_input_tokens -> input_tokens_total=None
    let raw_anth_missing = json_obj(json!({
        "input_tokens": 500,
        "cache_read_input_tokens": 100,
        "output_tokens": 400,
    }));
    let norm_anth_missing = normalize_raw_usage_v5(
        Some(&raw_anth_missing),
        &get_provider_usage_policy_v5("anthropic_messages").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_anth_missing.input_tokens_total, None);
    assert_eq!(norm_anth_missing.cached_input_tokens, None);
    assert_eq!(norm_anth_missing.output_tokens_total, Some(400));

    // 4. Google Gemini / Vertex
    let raw_gemini = json_obj(json!({
        "promptTokenCount": 800,
        "cachedContentTokenCount": 200,
        "candidatesTokenCount": 150,
        "thoughtsTokenCount": 350,
        "totalTokenCount": 1300,
    }));
    let norm_gemini = normalize_raw_usage_v5(
        Some(&raw_gemini),
        &get_provider_usage_policy_v5("google_gemini_vertex").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_gemini.input_tokens_total, Some(800));
    assert_eq!(norm_gemini.cached_input_tokens, Some(200));
    assert_eq!(norm_gemini.output_tokens_total, Some(500));
    assert_eq!(norm_gemini.reasoning_tokens_subset, Some(350));

    let raw_gemini_missing = json_obj(json!({
        "promptTokenCount": 800,
        "candidatesTokenCount": 150,
    }));
    let norm_gemini_missing = normalize_raw_usage_v5(
        Some(&raw_gemini_missing),
        &get_provider_usage_policy_v5("google_gemini_vertex").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_gemini_missing.input_tokens_total, Some(800));
    assert_eq!(norm_gemini_missing.output_tokens_total, None);
    assert_eq!(norm_gemini_missing.reasoning_tokens_subset, None);

    // 5. Offline Replay
    let raw_replay = json_obj(json!({
        "input_tokens": 100,
        "cache_read_tokens": 200,
        "cache_write_tokens": 50,
        "output_tokens": 90,
        "reasoning_tokens": 40,
    }));
    let norm_replay = normalize_raw_usage_v5(
        Some(&raw_replay),
        &get_provider_usage_policy_v5("offline_replay").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(norm_replay.input_tokens_total, Some(350));
    assert_eq!(norm_replay.cached_input_tokens, Some(200));
    assert_eq!(norm_replay.output_tokens_total, Some(90));
    assert_eq!(norm_replay.reasoning_tokens_subset, Some(40));

    // 6. Contradictory subset (> total)
    let raw_contradictory = json_obj(json!({
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 150},
        "output_tokens": 80,
        "output_tokens_details": {"reasoning_tokens": 90},
    }));
    let contradictory = normalize_raw_usage_v5(
        Some(&raw_contradictory),
        &get_provider_usage_policy_v5("openai_responses").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(contradictory.input_tokens_total, None);
    assert_eq!(contradictory.cached_input_tokens, None);
    assert_eq!(contradictory.output_tokens_total, None);
    assert_eq!(contradictory.reasoning_tokens_subset, None);
    let c_raw: Value =
        serde_json::from_str(contradictory.raw_usage_fields_json.as_deref().unwrap()).unwrap();
    assert_eq!(c_raw["output_tokens"], json!(80));

    // 7. UNKNOWN_MAPPING preserves raw JSON and leaves normalized fields None
    let raw_unknown = json_obj(json!({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "mystery_reasoning": 20,
    }));
    let unknown = normalize_raw_usage_v5(
        Some(&raw_unknown),
        &get_provider_usage_policy_v5("unknown_mapping").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(unknown.usage_scope, UsageScopeV5::Unavailable);
    assert_eq!(unknown.input_tokens_total, None);
    assert_eq!(unknown.output_tokens_total, None);
    assert_eq!(unknown.reasoning_tokens_subset, None);
    let u_raw: Value =
        serde_json::from_str(unknown.raw_usage_fields_json.as_deref().unwrap()).unwrap();
    assert_eq!(u_raw["mystery_reasoning"], json!(20));
}

#[test]
fn custom_tool_serialization_subset_policy() {
    let custom = ProviderUsageAccountingPolicyV5 {
        policy_id: "custom-tool-ser-v1".to_owned(),
        provider_family: ProviderFamilyV5::Custom,
        policy_version: "v1".to_owned(),
        documentation_source: "documented tool_serialization_tokens subset of output_tokens"
            .to_owned(),
        default_usage_scope: UsageScopeV5::PhysicalRequest,
        input_base_field: Some("input_tokens".to_owned()),
        cached_input_field: None,
        cached_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        cache_write_input_field: None,
        cache_write_input_inclusion: UsageInclusionSemanticsV5::NotApplicable,
        cache_billing_semantics: "none".to_owned(),
        output_base_field: Some("output_tokens".to_owned()),
        reasoning_tokens_field: Some("reasoning_tokens".to_owned()),
        reasoning_inclusion: UsageInclusionSemanticsV5::Included,
        tool_serialization_tokens_field: Some("tool_serialization_tokens".to_owned()),
        tool_serialization_inclusion: UsageInclusionSemanticsV5::Included,
    };
    let raw = json_obj(json!({
        "input_tokens": 300,
        "output_tokens": 200,
        "reasoning_tokens": 80,
        "tool_serialization_tokens": 45,
    }));
    let norm = normalize_raw_usage_v5(Some(&raw), &custom, None).unwrap();
    assert_eq!(norm.output_tokens_total, Some(200));
    assert_eq!(norm.reasoning_tokens_subset, Some(80));
    assert_eq!(norm.tool_serialization_tokens_subset, Some(45));
}

#[test]
fn usage_scope_prevents_cumulative_double_counting_and_supports_derivation() {
    let rate = FrozenPricingRateV5 {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: 1_000_000,
        cached_input_cost_per_million_micros: None,
        cache_write_cost_per_million_micros: None,
        output_cost_per_million_micros: 2_000_000,
        reasoning_cost_per_million_micros: None,
    };

    let mut seg0 = ReqBuilder::new("req_seg0");
    seg0.raw_usage = Some(json_obj(
        json!({"input_tokens": 1000, "output_tokens": 400}),
    ));
    seg0.pricing_rate = Some(rate.clone());
    let seg0 = seg0.build();

    let mut seg1 = ReqBuilder::new("req_seg1");
    seg1.request_kind = ProviderRequestKindV5::SegmentContinuation;
    seg1.retry_index = None;
    seg1.segment_continuation_index = Some(1);
    seg1.decode_segment_index = 1;
    seg1.raw_usage = Some(json_obj(
        json!({"input_tokens": 1400, "output_tokens": 600}),
    ));
    seg1.pricing_rate = Some(rate.clone());
    let seg1 = seg1.build();

    let mut redundant_cum = ReqBuilder::new("req_cum");
    redundant_cum.request_kind = ProviderRequestKindV5::SegmentContinuation;
    redundant_cum.retry_index = None;
    redundant_cum.segment_continuation_index = Some(1);
    redundant_cum.decode_segment_index = 1;
    redundant_cum.raw_usage = Some(json_obj(
        json!({"input_tokens": 2400, "output_tokens": 1000}),
    ));
    redundant_cum.usage_scope_override = Some(UsageScopeV5::LogicalResponseCumulative);
    redundant_cum.pricing_rate = Some(rate);
    let redundant_cum = redundant_cum.build();

    let all_reqs = vec![seg0, seg1, redundant_cum];
    let selected = select_contributing_usage_records_v5(&all_reqs);
    let selected_ids: Vec<&str> = selected.iter().map(|r| r.request_id.as_str()).collect();
    assert_eq!(selected_ids, vec!["req_seg0", "req_seg1"]);

    let attempt = make_attempt("att_1", 1, AttemptStatusV5::Completed, all_reqs, 1, 0);
    let summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        1,
        std::slice::from_ref(&attempt),
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    assert_eq!(summary.total_input_tokens, Some(2400));
    assert_eq!(summary.total_output_tokens, Some(1000));
    assert_eq!(
        summary.accounting_coverage_status,
        AccountingCoverageStatusV5::Complete
    );
    let gate = evaluate_g_econ_01_v5(&[attempt], &summary, EvaluateGEcon01Options::default());
    assert!(gate.passed);

    // Cumulative-to-segment derivation
    let mut cum0 = ReqBuilder::new("cum_0");
    cum0.logical_request_id = "lreq_2".to_owned();
    cum0.raw_usage = Some(json_obj(
        json!({"input_tokens": 1000, "output_tokens": 300}),
    ));
    cum0.usage_scope_override = Some(UsageScopeV5::LogicalResponseCumulative);
    let cum0 = cum0.build();

    let mut cum1 = ReqBuilder::new("cum_1");
    cum1.logical_request_id = "lreq_2".to_owned();
    cum1.request_kind = ProviderRequestKindV5::SegmentContinuation;
    cum1.retry_index = None;
    cum1.segment_continuation_index = Some(1);
    cum1.decode_segment_index = 1;
    cum1.raw_usage = Some(json_obj(
        json!({"input_tokens": 2500, "output_tokens": 900}),
    ));
    cum1.usage_scope_override = Some(UsageScopeV5::LogicalResponseCumulative);
    let cum1 = cum1.build();

    let derived =
        derive_physical_segment_usage_from_cumulative_v5(&[cum0.clone(), cum1.clone()]).unwrap();
    assert_eq!(derived[0].usage_scope, UsageScopeV5::PhysicalRequest);
    assert_eq!(derived[1].usage_scope, UsageScopeV5::PhysicalRequest);
    assert_eq!(
        (
            derived[0].input_tokens_total,
            derived[0].output_tokens_total
        ),
        (Some(1000), Some(300))
    );
    assert_eq!(
        (
            derived[1].input_tokens_total,
            derived[1].output_tokens_total
        ),
        (Some(1500), Some(600))
    );

    // Non-monotonic cumulative series must fail loudly
    let mut bad_cum1 = cum1;
    bad_cum1.output_tokens_total = Some(100);
    let err = derive_physical_segment_usage_from_cumulative_v5(&[cum0, bad_cum1]).unwrap_err();
    assert!(err.to_string().contains("decreased"));
}

#[test]
fn billed_vs_estimated_cost_and_currency_mismatch_guard() {
    let anth_policy = get_provider_usage_policy_v5("anthropic_messages").unwrap();
    let anth_rate = FrozenPricingRateV5 {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: 3_000_000,
        cached_input_cost_per_million_micros: Some(300_000),
        cache_write_cost_per_million_micros: Some(3_750_000),
        output_cost_per_million_micros: 15_000_000,
        reasoning_cost_per_million_micros: None,
    };
    let raw_anth = json_obj(json!({
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }));

    let mut est_req = ReqBuilder::new("req_est");
    est_req.raw_usage = Some(raw_anth);
    est_req.policy = anth_policy;
    est_req.pricing_rate = Some(anth_rate.clone());
    let est_req = est_req.build();

    assert_eq!(est_req.cost_basis, CostBasisV5::FrozenRateEstimate);
    assert_eq!(est_req.estimated_cost_micros, Some(22_050_000));
    assert_eq!(est_req.provider_reported_cost_micros, None);
    assert_eq!(
        est_req.pricing_manifest_sha256.as_deref(),
        Some(PRICING_SHA)
    );

    // If cached rate differs and cached_input_tokens is None, estimate must return None
    let raw_oai_no_cache = json_obj(json!({
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
    }));
    let oai_norm_no_cache = normalize_raw_usage_v5(
        Some(&raw_oai_no_cache),
        &get_provider_usage_policy_v5("openai_responses").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(
        estimate_frozen_cost_micros_v5(&oai_norm_no_cache, &anth_rate, None, None),
        None
    );

    let mut billed_eur_req = ReqBuilder::new("req_billed_eur");
    billed_eur_req.attempt_id = "att_2".to_owned();
    billed_eur_req.raw_usage = Some(json_obj(
        json!({"input_tokens": 1000, "output_tokens": 500}),
    ));
    billed_eur_req.provider_reported_cost_micros = Some(2_000);
    billed_eur_req.currency_code = Some("EUR".to_owned());
    let billed_eur_req = billed_eur_req.build();

    assert_eq!(billed_eur_req.cost_basis, CostBasisV5::ProviderBilled);
    assert_eq!(billed_eur_req.provider_reported_cost_micros, Some(2_000));
    assert_eq!(billed_eur_req.estimated_cost_micros, None);

    let a1 = make_attempt("att_1", 1, AttemptStatusV5::Completed, vec![est_req], 1, 0);
    let a2 = make_attempt(
        "att_2",
        2,
        AttemptStatusV5::Completed,
        vec![billed_eur_req],
        1,
        0,
    );
    let attempts = vec![a1, a2];

    // Mixed USD + EUR without conversion rate is forbidden
    let err = summarize_descriptive_usage_v5(
        RUN_SHA,
        2,
        &attempts,
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap_err();
    assert!(err.to_string().contains("mismatched currencies"));

    // Explicit frozen conversion rate allows conversion
    let conv = CurrencyConversionRateV5 {
        from_currency: "EUR".to_owned(),
        to_currency: "USD".to_owned(),
        rate_numerator: 11,
        rate_denominator: 10,
        rate_source: "ECB_FROZEN_2026_11_01".to_owned(),
        pricing_manifest_sha256: Some(RATE_SHA.to_owned()),
    };
    let convs = vec![conv];
    let summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        2,
        &attempts,
        SummarizeDescriptiveUsageOptions {
            target_currency: Some("USD"),
            currency_conversions: &convs,
            ..SummarizeDescriptiveUsageOptions::default()
        },
    )
    .unwrap();
    assert_eq!(summary.currency_code.as_deref(), Some("USD"));
    assert_eq!(summary.estimated_cost_micros, Some(22_050_000));
    assert_eq!(summary.provider_billed_cost_micros, Some(2_200));

    let gate = evaluate_g_econ_01_v5(
        &attempts,
        &summary,
        EvaluateGEcon01Options {
            target_currency: Some("USD"),
            currency_conversions: &convs,
            require_complete_coverage: false,
        },
    );
    assert!(gate.passed);
}

#[test]
fn interval_union_active_elapsed_ms_and_attempt_timings() {
    assert_eq!(compute_active_elapsed_ms(&[]).unwrap(), 0);
    assert_eq!(compute_active_elapsed_ms(&[(100, 100)]).unwrap(), 0);
    let intervals = [(0, 100), (20, 120), (120, 200), (500, 650), (550, 700)];
    assert_eq!(compute_active_elapsed_ms(&intervals).unwrap(), 400);

    let timings = compute_attempt_timings_ms_v5(
        1_000,
        Some(1_800),
        Some(2_000),
        &[(1_000, 1_300), (1_100, 1_400), (1_700, 1_900)],
    )
    .unwrap();
    assert_eq!(timings.elapsed_wall_time_ms, Some(1_000));
    assert_eq!(timings.terminal_outcome_elapsed_ms, Some(800));
    assert_eq!(timings.active_elapsed_ms, 600);

    assert!(compute_active_elapsed_ms(&[(200, 100)]).is_err());
    assert!(compute_active_elapsed_ms(&[(-1, 10)]).is_err());
}

#[test]
fn summarize_descriptive_usage_sc03_and_coverage_levels() {
    let rate = FrozenPricingRateV5 {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: 1_000_000,
        cached_input_cost_per_million_micros: None,
        cache_write_cost_per_million_micros: None,
        output_cost_per_million_micros: 2_000_000,
        reasoning_cost_per_million_micros: None,
    };

    let mut req_ok = ReqBuilder::new("req_1");
    req_ok.raw_usage = Some(json_obj(
        json!({"input_tokens": 1000, "output_tokens": 500}),
    ));
    req_ok.pricing_rate = Some(rate);
    let req_ok = req_ok.build();

    let mut req_unknown = ReqBuilder::new("req_2");
    req_unknown.attempt_id = "att_2".to_owned();
    req_unknown.request_status = ProviderRequestStatusV5::TransientFailure;
    req_unknown.raw_usage = Some(json_obj(json!({"mystery_tokens": 50})));
    req_unknown.policy = get_provider_usage_policy_v5("unknown_mapping").unwrap();
    let req_unknown = req_unknown.build();

    let a_completed = make_attempt("att_1", 1, AttemptStatusV5::Completed, vec![req_ok], 1, 0);
    let a_within = make_attempt(
        "att_2",
        2,
        AttemptStatusV5::WithinProtocolRefusal,
        vec![req_unknown],
        1,
        0,
    );

    let partial_summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        3,
        &[a_completed.clone(), a_within.clone()],
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    assert_eq!(partial_summary.completed_attempts, 1);
    assert_eq!(partial_summary.incomplete_harness_attempts, 1);
    assert_eq!(
        partial_summary.accounting_coverage_status,
        AccountingCoverageStatusV5::Partial
    );
    assert_eq!(partial_summary.total_input_tokens, Some(1000));
    assert_eq!(partial_summary.total_output_tokens, Some(500));
    assert_eq!(partial_summary.estimated_cost_micros, Some(2_000));

    let counts: BTreeMap<String, i64> =
        serde_json::from_str(&partial_summary.attempt_status_counts_json).unwrap();
    assert_eq!(counts.len(), CANONICAL_ATTEMPT_STATUSES_V5.len());
    assert_eq!(counts["COMPLETED"], 1);
    assert_eq!(counts["WITHIN_PROTOCOL_REFUSAL"], 1);
    assert_eq!(counts["INCOMPLETE_HARNESS_FAILURE"], 1);
    assert_eq!(counts.values().sum::<i64>(), 3);

    let err = summarize_descriptive_usage_v5(
        RUN_SHA,
        3,
        &[a_completed.clone(), a_within.clone()],
        SummarizeDescriptiveUsageOptions {
            missing_slot_status: None,
            ..SummarizeDescriptiveUsageOptions::default()
        },
    )
    .unwrap_err();
    assert!(err.to_string().contains("expected 3 materialized attempts"));

    // Require complete coverage fails on PARTIAL
    let partial_gate = evaluate_g_econ_01_v5(
        &[a_completed, a_within],
        &partial_summary,
        EvaluateGEcon01Options {
            require_complete_coverage: true,
            ..EvaluateGEcon01Options::default()
        },
    );
    assert!(!partial_gate.passed);

    // Unavailable coverage leaves all token/cost fields None
    let mut req_unavail = ReqBuilder::new("req_3");
    req_unavail.attempt_id = "att_3".to_owned();
    req_unavail.request_status = ProviderRequestStatusV5::AmbiguousFailure;
    req_unavail.policy = get_provider_usage_policy_v5("unknown_mapping").unwrap();
    let req_unavail = req_unavail.build();

    let a_unavail = make_attempt(
        "att_3",
        3,
        AttemptStatusV5::IncompleteProviderFailure,
        vec![req_unavail],
        0,
        0,
    );
    let unavail_summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        1,
        std::slice::from_ref(&a_unavail),
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    assert_eq!(
        unavail_summary.accounting_coverage_status,
        AccountingCoverageStatusV5::Unavailable
    );
    assert_eq!(unavail_summary.total_input_tokens, None);
    assert_eq!(unavail_summary.total_output_tokens, None);
    assert_eq!(unavail_summary.provider_billed_cost_micros, None);
    assert_eq!(unavail_summary.estimated_cost_micros, None);
    assert!(
        evaluate_g_econ_01_v5(
            &[a_unavail],
            &unavail_summary,
            EvaluateGEcon01Options::default()
        )
        .passed
    );
}

#[test]
fn unavailable_retry_and_segment_usage_preserve_known_billed_costs() {
    let mut retry = ReqBuilder::new("req_retry_billed");
    retry.logical_request_id = "lreq_retry_billed".to_owned();
    retry.request_kind = ProviderRequestKindV5::TransientRetry;
    retry.retry_index = Some(1);
    retry.raw_usage = Some(json_obj(
        json!({"input_tokens": 1.5, "output_tokens": false}),
    ));
    retry.usage_scope_override = Some(UsageScopeV5::Unavailable);
    retry.provider_reported_cost_micros = Some(1_200);
    retry.currency_code = Some("USD".to_owned());
    let retry = retry.build();

    let mut segment = ReqBuilder::new("req_segment_billed");
    segment.logical_request_id = "lreq_segment_billed".to_owned();
    segment.request_kind = ProviderRequestKindV5::SegmentContinuation;
    segment.retry_index = None;
    segment.segment_continuation_index = Some(1);
    segment.decode_segment_index = 1;
    segment.raw_usage = Some(json_obj(
        json!({"input_tokens": "unknown", "output_tokens": -1}),
    ));
    segment.usage_scope_override = Some(UsageScopeV5::Unavailable);
    segment.provider_reported_cost_micros = Some(2_300);
    segment.currency_code = Some("USD".to_owned());
    let segment = segment.build();

    let attempt = make_attempt(
        "att_1",
        1,
        AttemptStatusV5::Completed,
        vec![retry, segment],
        1,
        1,
    );
    let summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        1,
        &[attempt],
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    assert_eq!(
        summary.accounting_coverage_status,
        AccountingCoverageStatusV5::Partial
    );
    assert_eq!(summary.total_input_tokens, None);
    assert_eq!(summary.total_output_tokens, None);
    assert_eq!(summary.provider_billed_cost_micros, Some(3_500));
    assert_eq!(summary.estimated_cost_micros, None);
}

#[test]
fn evaluate_g_econ_01_has_no_spend_ceiling_and_catches_incompleteness() {
    let mut huge_req = ReqBuilder::new("req_huge");
    huge_req.attempt_id = "att_huge".to_owned();
    huge_req.raw_usage = Some(json_obj(
        json!({"input_tokens": 100_000_000, "output_tokens": 50_000_000}),
    ));
    huge_req.provider_reported_cost_micros = Some(10_000_000_000);
    huge_req.currency_code = Some("USD".to_owned());
    let huge_req = huge_req.build();

    let mut post_term_cancel = ReqBuilder::new("req_cancel");
    post_term_cancel.logical_request_id = "lreq_cancel".to_owned();
    post_term_cancel.request_kind = ProviderRequestKindV5::TransientRetry;
    post_term_cancel.retry_index = Some(1);
    post_term_cancel.attempt_id = "att_huge".to_owned();
    post_term_cancel.request_status = ProviderRequestStatusV5::Cancelled;
    post_term_cancel.post_terminal = true;
    post_term_cancel.raw_usage = Some(json_obj(json!({"input_tokens": 500, "output_tokens": 0})));
    post_term_cancel.provider_reported_cost_micros = Some(5);
    post_term_cancel.currency_code = Some("USD".to_owned());
    let post_term_cancel = post_term_cancel.build();

    let a_huge = make_attempt(
        "att_huge",
        1,
        AttemptStatusV5::Completed,
        vec![huge_req.clone(), post_term_cancel.clone()],
        1,
        1,
    );
    let summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        1,
        std::slice::from_ref(&a_huge),
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    let result = evaluate_g_econ_01_v5(
        std::slice::from_ref(&a_huge),
        &summary,
        EvaluateGEcon01Options::default(),
    );
    assert!(result.passed);
    assert!(!result.spend_or_token_ceiling_applied);
    assert_eq!(
        result.accounting_coverage_status,
        AccountingCoverageStatusV5::Complete
    );

    // Tamper summary -> FAIL
    let mut bad_summary = summary.clone();
    bad_summary.total_input_tokens = Some(200_000_000);
    let bad_result = evaluate_g_econ_01_v5(
        std::slice::from_ref(&a_huge),
        &bad_summary,
        EvaluateGEcon01Options::default(),
    );
    assert!(!bad_result.passed);
    assert!(
        bad_result
            .errors
            .iter()
            .any(|e| e.contains("total_input_tokens"))
    );

    // Missing retry record -> FAIL
    let mut missing_retry_attempt = a_huge;
    missing_retry_attempt.retry_request_count = 2;
    let missing_retry_result = evaluate_g_econ_01_v5(
        &[missing_retry_attempt],
        &summary,
        EvaluateGEcon01Options::default(),
    );
    assert!(!missing_retry_result.passed);
    assert!(
        missing_retry_result
            .errors
            .iter()
            .any(|e| e.contains("retry_request_count"))
    );

    // Non-contiguous decode_segment_index -> FAIL
    let mut gap_seg = ReqBuilder::new("req_gap");
    gap_seg.logical_request_id = "lreq_1".to_owned();
    gap_seg.request_kind = ProviderRequestKindV5::SegmentContinuation;
    gap_seg.retry_index = None;
    gap_seg.segment_continuation_index = Some(2);
    gap_seg.decode_segment_index = 2;
    gap_seg.attempt_id = "att_huge".to_owned();
    gap_seg.raw_usage = Some(json_obj(json!({"input_tokens": 100, "output_tokens": 50})));
    gap_seg.provider_reported_cost_micros = Some(10);
    gap_seg.currency_code = Some("USD".to_owned());
    let gap_seg = gap_seg.build();

    let gap_attempt = make_attempt(
        "att_huge",
        1,
        AttemptStatusV5::Completed,
        vec![huge_req, gap_seg],
        1,
        0,
    );
    let gap_summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        1,
        std::slice::from_ref(&gap_attempt),
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    let gap_result = evaluate_g_econ_01_v5(
        &[gap_attempt],
        &gap_summary,
        EvaluateGEcon01Options::default(),
    );
    assert!(!gap_result.passed);
    assert!(
        gap_result
            .errors
            .iter()
            .any(|e| e.contains("non-contiguous decode_segment_index"))
    );
}

#[test]
fn round_half_up_and_saturating_micros_overflow_guards() {
    // Exact ROUND_HALF_UP parity with Python
    let rate_half = FrozenPricingRateV5 {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: 500_000,
        cached_input_cost_per_million_micros: None,
        cache_write_cost_per_million_micros: None,
        output_cost_per_million_micros: 500_000,
        reasoning_cost_per_million_micros: None,
    };
    let raw_one = json_obj(json!({"input_tokens": 1, "output_tokens": 0}));
    let norm_one = normalize_raw_usage_v5(
        Some(&raw_one),
        &get_provider_usage_policy_v5("openai_responses").unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(
        estimate_frozen_cost_micros_v5(&norm_one, &rate_half, None, None),
        Some(1)
    );

    let rate_below = FrozenPricingRateV5 {
        input_cost_per_million_micros: 499_999,
        output_cost_per_million_micros: 0,
        ..rate_half
    };
    assert_eq!(
        estimate_frozen_cost_micros_v5(&norm_one, &rate_below, None, None),
        Some(0)
    );

    // Currency conversion ROUND_HALF_UP
    let half_conv = CurrencyConversionRateV5 {
        from_currency: "GBP".to_owned(),
        to_currency: "USD".to_owned(),
        rate_numerator: 3,
        rate_denominator: 2,
        rate_source: "FROZEN_HALF_TEST".to_owned(),
        pricing_manifest_sha256: None,
    };
    let mut idx = BTreeMap::new();
    idx.insert(("GBP".to_owned(), "USD".to_owned()), &half_conv);
    // 1 * 3 / 2 = 1.5 -> 2 (ROUND_HALF_UP)
    assert_eq!(
        convert_micros_round_half_up(1, "GBP", "USD", &idx).unwrap(),
        2
    );

    // Saturating estimate_frozen_cost_micros_v5 near i64::MAX must never panic or wrap negative
    let huge_rate = FrozenPricingRateV5 {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: i64::MAX,
        cached_input_cost_per_million_micros: None,
        cache_write_cost_per_million_micros: None,
        output_cost_per_million_micros: i64::MAX,
        reasoning_cost_per_million_micros: None,
    };
    let raw_huge = json_obj(json!({
        "input_tokens": i64::MAX,
        "output_tokens": i64::MAX,
    }));
    let norm_huge = normalize_raw_usage_v5(
        Some(&raw_huge),
        &get_provider_usage_policy_v5("openai_responses").unwrap(),
        None,
    )
    .unwrap();
    let sat_est = estimate_frozen_cost_micros_v5(&norm_huge, &huge_rate, None, None).unwrap();
    assert_eq!(sat_est, i64::MAX);

    // Saturating currency conversion near i64::MAX
    assert_eq!(
        convert_micros_round_half_up(i64::MAX, "GBP", "USD", &idx).unwrap(),
        i64::MAX
    );

    // Exact active elapsed ms near i64::MAX (gap of 100 ms excluded)
    assert_eq!(
        compute_active_elapsed_ms(&[(0, i64::MAX / 2 + 100), (i64::MAX / 2 + 200, i64::MAX)])
            .unwrap(),
        i64::MAX - 100
    );

    // Saturating aggregation across two i64::MAX-bearing attempts
    let mut r1 = ReqBuilder::new("req_sat_1");
    r1.attempt_id = "att_sat_1".to_owned();
    r1.raw_usage = Some(json_obj(
        json!({"input_tokens": i64::MAX - 10, "output_tokens": 100}),
    ));
    r1.provider_reported_cost_micros = Some(i64::MAX - 5);
    r1.currency_code = Some("USD".to_owned());

    let mut r2 = ReqBuilder::new("req_sat_2");
    r2.attempt_id = "att_sat_2".to_owned();
    r2.raw_usage = Some(json_obj(json!({"input_tokens": 500, "output_tokens": 200})));
    r2.provider_reported_cost_micros = Some(500);
    r2.currency_code = Some("USD".to_owned());

    let mut a1 = make_attempt(
        "att_sat_1",
        1,
        AttemptStatusV5::Completed,
        vec![r1.build()],
        1,
        0,
    );
    a1.elapsed_wall_time_ms = Some(i64::MAX - 10);
    a1.active_elapsed_ms = Some(i64::MAX - 20);
    let mut a2 = make_attempt(
        "att_sat_2",
        2,
        AttemptStatusV5::Completed,
        vec![r2.build()],
        1,
        0,
    );
    a2.elapsed_wall_time_ms = Some(1000);
    a2.active_elapsed_ms = Some(900);
    let sat_summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        2,
        &[a1.clone(), a2.clone()],
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();
    assert_eq!(sat_summary.total_input_tokens, Some(i64::MAX));
    assert_eq!(sat_summary.provider_billed_cost_micros, Some(i64::MAX));
    assert_eq!(sat_summary.elapsed_wall_time_ms, Some(i64::MAX));
    assert_eq!(sat_summary.active_elapsed_ms, Some(i64::MAX));
    let sat_eval =
        evaluate_g_econ_01_v5(&[a1, a2], &sat_summary, EvaluateGEcon01Options::default());
    assert!(sat_eval.passed, "sat_eval errors: {:?}", sat_eval.errors);
}

#[test]
fn read_only_pilot_manifest_descriptive_usage_parity() {
    let Some(pilot_dir) = real_pilot_run_dir("v5_draft5_pilot_v3_gemini_hydrogen_t0") else {
        return;
    };
    let manifest_path = pilot_dir.join("manifest.json");
    let episodes_path = pilot_dir.join("episodes.jsonl");

    let manifest_file = File::open(&manifest_path).unwrap();
    let manifest: Value = serde_json::from_reader(manifest_file).unwrap();
    let recorded_summary =
        DescriptiveUsageSummaryV5::from_strict_json_value(&manifest["descriptive_usage_summary"])
            .unwrap();

    let ep_file = File::open(&episodes_path).unwrap();
    let reader = BufReader::new(ep_file);
    let mut attempts = Vec::new();
    for line in reader.lines() {
        let line = line.unwrap();
        if line.trim().is_empty() {
            continue;
        }
        let row: Value = serde_json::from_str(&line).unwrap();
        let attempt = EpisodeAttemptV5::from_strict_json_value(&row["attempt"]).unwrap();
        attempts.push(attempt);
    }

    let recomputed = summarize_descriptive_usage_v5(
        &recorded_summary.run_config_sha256,
        recorded_summary.planned_attempt_slots,
        &attempts,
        SummarizeDescriptiveUsageOptions::default(),
    )
    .unwrap();

    assert_eq!(recomputed, recorded_summary);

    let eval = evaluate_g_econ_01_v5(
        &attempts,
        &recorded_summary,
        EvaluateGEcon01Options {
            require_complete_coverage: true,
            ..EvaluateGEcon01Options::default()
        },
    );
    assert!(eval.passed, "G-ECON-01 errors: {:?}", eval.errors);
    assert_eq!(eval.status, "PASS");
}

fn real_pilot_run_dir(run_name: &str) -> Option<PathBuf> {
    // Default local runs are optional; an explicit root makes the requested run required.
    let explicit_root = std::env::var_os("IDENTITYBENCH_REAL_PILOT_ROOT");
    let root = explicit_root
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runs"));
    let run_dir = root.join(run_name);
    if run_dir.exists() {
        Some(run_dir)
    } else if explicit_root.is_some() {
        panic!("explicit real-pilot root is missing requested run: {run_name}")
    } else {
        None
    }
}
