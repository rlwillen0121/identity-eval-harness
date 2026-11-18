use ib_core::hashing::sha256_hex;
use ib_providers::base::{GenerationParams, ModelRequest};
use ib_v5_runner::config::{RunConfigV5, compute_effective_output_allowance_v5};
use ib_v5_runner::prompts::{
    PRIMARY_PROMPT_BUNDLE_SHA256_V5, compute_primary_prompt_bundle_sha256_v5,
    compute_primary_prompt_bundle_v5, count_model_request_tokens_v5, count_text_tokens_v5,
    render_v5_system_prompt, strict_openai_schema, tool_specs,
};
use serde_json::{Value, json};

#[test]
fn default_config_and_python_normalization_match() {
    let default_config = RunConfigV5::default();
    default_config.validate().expect("default config validates");
    assert_eq!(default_config.max_output_tokens, 64_000);
    assert_eq!(default_config.effective_max_turns(), 50);
    assert_eq!(
        default_config.runtime_topology().unwrap(),
        "T0_SINGLE_MONOLITH"
    );

    let normalized = RunConfigV5::from_value(json!({"max_output_tokens": 512}))
        .expect("small max_output_tokens normalizes");
    assert_eq!(normalized.requested_output_ceiling_tokens, 512);
    assert_eq!(normalized.base_effective_output_ceiling_tokens, 512);
    assert_eq!(normalized.effort_minimum_output_tokens, 512);

    let capped = RunConfigV5::from_value(json!({"max_output_tokens": 70_000}))
        .expect("large max_output_tokens normalizes");
    assert_eq!(capped.requested_output_ceiling_tokens, 70_000);
    assert_eq!(capped.provider_max_output_tokens, 70_000);
}

#[test]
fn config_rejects_invalid_caps_and_digest_fields() {
    assert!(RunConfigV5::from_value(json!({"cap_disclosed": true})).is_err());
    assert!(RunConfigV5::from_value(json!({"run_config_sha256": "ABC"})).is_err());
    assert!(RunConfigV5::from_value(json!({"max_turns": 51})).is_err());
}

#[test]
fn config_rejects_token_counts_outside_runner_numeric_range() {
    let error = RunConfigV5::from_value(json!({
        "context_window_tokens": u64::MAX
    }))
    .expect_err("context count must fit runner i64 accounting");
    assert!(error.to_string().contains("i64 range"));

    let error = RunConfigV5::from_value(json!({
        "native_numeric_budget_cap_tokens": u64::MAX
    }))
    .expect_err("native budget cap must fit runner i64 accounting");
    assert!(error.to_string().contains("i64 range"));
}

#[test]
fn output_allowance_uses_wide_intermediate_and_clamps_negative_overflow() {
    assert_eq!(
        compute_effective_output_allowance_v5(
            i64::MAX,
            i64::MAX,
            i64::MAX,
            i64::MAX,
            i64::MAX,
            i64::MAX,
        ),
        i64::MIN
    );
    assert_eq!(
        compute_effective_output_allowance_v5(100, 90, 1_000, 300, 100, 100),
        90
    );
}

#[test]
fn primary_prompt_bundle_and_default_prompt_match_python_hashes() {
    let config = RunConfigV5::default();
    let system_prompt = render_v5_system_prompt(&config);
    assert_eq!(
        sha256_hex(system_prompt.as_bytes()),
        "e719c6cf92e78b58ab5f998d3d6d6b9846347b0a65d9ff2962819260ccc8b7b4"
    );
    assert_eq!(
        compute_primary_prompt_bundle_sha256_v5().unwrap(),
        PRIMARY_PROMPT_BUNDLE_SHA256_V5
    );
    let bundle = compute_primary_prompt_bundle_v5();
    assert_eq!(
        bundle["bundle_version"],
        "identitybench.v5.primary_prompt_bundle.v1"
    );
    assert_eq!(bundle["t0_system_prompt"].as_str().unwrap(), system_prompt);
}

#[test]
fn frozen_tool_schemas_are_phase_specific_and_strict() {
    let all = tool_specs(None).unwrap();
    let phase1 = tool_specs(Some(1)).unwrap();
    let phase2 = tool_specs(Some(2)).unwrap();
    assert_eq!(all.len(), 6);
    assert_eq!(phase1.len(), 6);
    assert_eq!(phase2.len(), 6);
    assert_eq!(all[0].name, "query_sources");
    assert_eq!(all[4].name, "run_phase_checker");
    assert_eq!(all[5].name, "request_team_steer");
    assert_ne!(
        phase1[1].input_schema, phase2[1].input_schema,
        "phase mutation schemas must be narrowed"
    );

    fn assert_no_disallowed_schema_keys(value: &Value) {
        if let Some(object) = value.as_object() {
            for key in [
                "title",
                "discriminator",
                "default",
                "minItems",
                "maxItems",
                "minimum",
                "maximum",
                "pattern",
                "format",
            ] {
                assert!(!object.contains_key(key), "unexpected schema key: {key}");
            }
            for child in object.values() {
                assert_no_disallowed_schema_keys(child);
            }
        } else if let Some(array) = value.as_array() {
            for child in array {
                assert_no_disallowed_schema_keys(child);
            }
        }
    }
    for spec in &all {
        assert_no_disallowed_schema_keys(&Value::Object(spec.input_schema.clone()));
        assert_eq!(
            strict_openai_schema(&Value::Object(spec.input_schema.clone())),
            Value::Object(spec.input_schema.clone())
        );
    }
}

#[test]
fn request_and_text_token_counts_match_python_o200k() {
    assert_eq!(count_text_tokens_v5("hello").unwrap(), 1);
    let system = render_v5_system_prompt(&RunConfigV5::default());
    let request = ModelRequest {
        system,
        messages: Vec::new(),
        output_schema: None,
        schema_name: "tool_action".to_owned(),
        tools: Vec::new(),
        params: GenerationParams {
            max_output_tokens: 64_000,
            ..GenerationParams::default()
        },
    };
    assert_eq!(count_model_request_tokens_v5(&request).unwrap(), 383);
}
