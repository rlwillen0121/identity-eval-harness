use ib_providers::factory::{FactoryError, ModelSpec};
use serde_json::{Value, json};

#[test]
fn yaml_values_keep_the_existing_scalar_and_mapping_behavior() {
    let value: Value = serde_norway::from_str(
        "model: first\nmodel: second\nvalues:\n  true_value: true\n  false_value: FALSE\n  yes_text: yes\n  zero: 0\n  leading_zero: 012\n  underscored: 1_000\n  exponent: 1e3\nbase: &base\n  provider: anthropic\n  retries: 2\nmerged:\n  <<: *base\n  model: local\nnull_value: null\n",
    )
    .expect("valid YAML values");

    assert_eq!(value["model"], "second");
    assert_eq!(
        value["values"],
        json!({
            "true_value": true,
            "false_value": false,
            "yes_text": "yes",
            "zero": 0,
            "leading_zero": "012",
            "underscored": "1_000",
            "exponent": 1000.0,
        })
    );
    assert_eq!(
        value["merged"],
        json!({
            "<<": {"provider": "anthropic", "retries": 2},
            "model": "local",
        })
    );
    assert!(value["null_value"].is_null());
}

#[test]
fn model_spec_yaml_keeps_expected_fields_and_scalar_types() {
    let spec = ModelSpec::from_yaml_str(
        "provider: openai_compatible\nmodel: local-model\nlabel: null\nmax_output_tokens: 2048\nextra:\n  reasoning_effort_param: false\n  max_retries: 1000\n  max_tokens_param: &token_param max_tokens\n",
    )
    .expect("valid model specification");

    assert_eq!(spec.model, "local-model");
    assert_eq!(spec.label, None);
    assert_eq!(spec.max_output_tokens, 2048);
    assert_eq!(spec.extra["reasoning_effort_param"], false);
    assert_eq!(spec.extra["max_retries"], 1000);
    assert_eq!(spec.extra["max_tokens_param"], "max_tokens");

    let alias_spec = ModelSpec::from_yaml_str(
        "provider: openai_compatible\nmodel: &model_name claude-test\ncache_namespace: *model_name\n",
    )
    .expect("scalar aliases remain valid model configuration");
    assert_eq!(alias_spec.model, "claude-test");
    assert_eq!(alias_spec.cache_namespace.as_deref(), Some("claude-test"));
}

#[test]
fn yaml_documents_that_the_existing_loader_rejected_remain_errors() {
    for yaml in [
        "plain: !custom scalar\n",
        "a: 1\n---\nb: 2\n",
        "model: [unterminated\n",
    ] {
        assert!(
            serde_norway::from_str::<Value>(yaml).is_err(),
            "expected YAML to remain invalid: {yaml:?}"
        );
    }

    let duplicate_field = "provider: anthropic\nmodel: first\nmodel: second\n";
    assert!(matches!(
        ModelSpec::from_yaml_str(duplicate_field),
        Err(FactoryError::Validation(_))
    ));
}
