use std::collections::BTreeMap;

use ib_providers::anthropic_api::{AnthropicConfig, AnthropicProvider};
use ib_providers::base::Provider;
use ib_providers::factory::{FactoryError, ModelSpec, ProviderKind, make_provider_with_env};
use ib_providers::openai_compat::{OpenAICompatConfig, OpenAICompatProvider};
use serde_json::{Value, json};

const UNTRUSTED_URL: &str = "https://untrusted.example/v1";

fn env(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(name, value)| ((*name).to_string(), (*value).to_string()))
        .collect()
}

fn builtin_spec(kind: ProviderKind) -> ModelSpec {
    ModelSpec::new(kind, "test-model")
}

fn opted_in_spec(kind: ProviderKind) -> ModelSpec {
    let mut spec = builtin_spec(kind);
    spec.extra
        .insert("allow_custom_endpoint".to_string(), Value::Bool(true));
    spec
}

fn assert_requires_opt_in(result: Result<std::sync::Arc<dyn Provider>, FactoryError>) {
    let error = result
        .err()
        .expect("custom vendor endpoint must be rejected");
    assert!(error.to_string().contains("allow_custom_endpoint"));
}

#[test]
fn built_in_vendor_providers_reject_custom_environment_origins() {
    assert_requires_opt_in(make_provider_with_env(
        &builtin_spec(ProviderKind::OpenaiCompatible),
        Some(env(&[
            ("OPENAI_BASE_URL", UNTRUSTED_URL),
            ("OPENAI_API_KEY", "synthetic-vendor-key"),
        ])),
    ));
    assert_requires_opt_in(make_provider_with_env(
        &builtin_spec(ProviderKind::Anthropic),
        Some(env(&[
            ("ANTHROPIC_BASE_URL", UNTRUSTED_URL),
            ("ANTHROPIC_API_KEY", "synthetic-vendor-key"),
        ])),
    ));
}

#[test]
fn vendor_host_suffix_spoofs_are_rejected() {
    for url in [
        "https://api.openai.com.evil.example/v1",
        "https://api.anthropic.com.evil.example/v1",
    ] {
        let openai = make_provider_with_env(
            &builtin_spec(ProviderKind::OpenaiCompatible),
            Some(env(&[("OPENAI_BASE_URL", url)])),
        );
        assert_requires_opt_in(openai);

        let anthropic = make_provider_with_env(
            &builtin_spec(ProviderKind::Anthropic),
            Some(env(&[("ANTHROPIC_BASE_URL", url)])),
        );
        assert_requires_opt_in(anthropic);
    }
}

#[test]
fn explicit_opt_in_allows_custom_vendor_routes_and_marks_endpoint_identity() {
    for (kind, variable) in [
        (ProviderKind::OpenaiCompatible, "OPENAI_BASE_URL"),
        (ProviderKind::Anthropic, "ANTHROPIC_BASE_URL"),
    ] {
        let values = env(&[(variable, UNTRUSTED_URL)]);
        let provider = make_provider_with_env(&opted_in_spec(kind), Some(values))
            .expect("explicit opt-in permits a configured custom vendor route");
        assert!(
            provider
                .endpoint_identity()
                .starts_with("custom-endpoint-opt-in:")
        );
    }
}

#[test]
fn explicit_opt_in_can_pair_a_vendor_provider_with_a_custom_api_key() {
    let mut openai = OpenAICompatConfig::new("custom-model");
    openai.allow_custom_endpoint = true;
    openai.api_key_env = Some("CUSTOM_API_KEY".to_string());
    openai.env_override = Some(env(&[
        ("OPENAI_BASE_URL", UNTRUSTED_URL),
        ("CUSTOM_API_KEY", "synthetic-custom-key"),
        ("OPENAI_API_KEY", "synthetic-vendor-key"),
    ]));
    let openai = OpenAICompatProvider::new(openai).unwrap();
    assert_eq!(openai.api_key_env(), Some("CUSTOM_API_KEY"));

    let mut anthropic = AnthropicConfig::new("custom-model");
    anthropic.allow_custom_endpoint = true;
    anthropic.api_key_env = Some("CUSTOM_API_KEY".to_string());
    anthropic.env_override = Some(env(&[
        ("ANTHROPIC_BASE_URL", UNTRUSTED_URL),
        ("CUSTOM_API_KEY", "synthetic-custom-key"),
        ("ANTHROPIC_API_KEY", "synthetic-vendor-key"),
    ]));
    let anthropic = AnthropicProvider::new(anthropic).unwrap();
    assert_eq!(anthropic.api_key_env(), Some("CUSTOM_API_KEY"));
}

#[test]
fn opt_in_changes_identity_even_when_endpoint_is_the_vendor() {
    for (kind, variable, vendor_url) in [
        (
            ProviderKind::OpenaiCompatible,
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        ),
        (
            ProviderKind::Anthropic,
            "ANTHROPIC_BASE_URL",
            "https://api.anthropic.com",
        ),
    ] {
        let values = || env(&[(variable, vendor_url)]);
        let default = make_provider_with_env(&builtin_spec(kind), Some(values())).unwrap();
        let opted = make_provider_with_env(&opted_in_spec(kind), Some(values())).unwrap();
        assert!(
            opted
                .endpoint_identity()
                .starts_with("custom-endpoint-opt-in:")
        );
        assert_ne!(default.endpoint_identity(), opted.endpoint_identity());
    }
}

#[test]
fn generic_compatible_providers_keep_custom_key_pairing_without_vendor_opt_in() {
    let mut openai = OpenAICompatConfig::new("custom-model");
    openai.base_url_env = Some("TEST_BASE_URL".to_string());
    openai.env_override = Some(env(&[
        ("TEST_BASE_URL", UNTRUSTED_URL),
        ("TEST_API_KEY", "synthetic-custom-key"),
        ("OPENAI_API_KEY", "synthetic-vendor-key"),
    ]));
    let openai = OpenAICompatProvider::new(openai).unwrap();
    assert_eq!(openai.api_key_env(), Some("TEST_API_KEY"));
    assert!(
        !openai
            .endpoint_identity()
            .starts_with("custom-endpoint-opt-in:")
    );

    let mut anthropic = AnthropicConfig::new("custom-model");
    anthropic.base_url_env = Some("TEST_BASE_URL".to_string());
    anthropic.env_override = Some(env(&[
        ("TEST_BASE_URL", UNTRUSTED_URL),
        ("TEST_API_KEY", "synthetic-custom-key"),
        ("ANTHROPIC_API_KEY", "synthetic-vendor-key"),
    ]));
    let anthropic = AnthropicProvider::new(anthropic).unwrap();
    assert_eq!(anthropic.api_key_env(), Some("TEST_API_KEY"));
    assert!(
        !anthropic
            .endpoint_identity()
            .starts_with("custom-endpoint-opt-in:")
    );
}

#[test]
fn custom_endpoint_opt_in_must_be_a_boolean() {
    for invalid in [json!("true"), json!(1), Value::Null] {
        let mut spec = builtin_spec(ProviderKind::OpenaiCompatible);
        spec.extra
            .insert("allow_custom_endpoint".to_string(), invalid);
        assert!(
            matches!(spec.validate(), Err(FactoryError::Validation(message))
            if message.contains("allow_custom_endpoint") && message.contains("true or false"))
        );
    }
}
