//! Configuration loading and identity for `identitybench v5 run`.
//!
//! Parsing is deliberately completed before `.env` values are loaded or a provider is created.

use std::path::Path;

use anyhow::{Context, Result, bail};
use ib_core::hashing::sha256_hex;
use ib_core::jcs_dumps;
use ib_providers::base::Provider;
use ib_providers::factory::{ModelSpec, ProviderKind};
use ib_v5_runner::{RunConfigV5, prompts::tool_specs, render_v5_system_prompt};
use serde_json::{Map, Value, json};

const MODEL_TIERS: &[&str] = &["frontier", "fast"];

#[derive(Debug, Clone)]
pub struct LoadedRunConfig {
    pub model_spec: ModelSpec,
    pub run_config: RunConfigV5,
    pub model_tier: Option<String>,
}

/// Parse the Python-compatible YAML shape, applying the CLI's documented defaults.
///
/// The error message is intentionally generic. A malformed config can contain private endpoint
/// values or request headers, so parser and schema diagnostics are never returned to the CLI.
pub fn load(path: &Path) -> Result<LoadedRunConfig> {
    let raw = std::fs::read(path).context("model config read failed")?;
    let text = std::str::from_utf8(&raw).context("model config must be UTF-8")?;
    let mut payload: Value = serde_yaml_ng::from_str(text)
        .map_err(|_| anyhow::anyhow!("model config must be valid YAML"))?;
    let object = payload
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("model config must be a mapping"))?;
    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "model" | "run_config" | "model_tier"))
    {
        bail!("model config contains unsupported top-level fields");
    }

    let model_tier = match object.get("model_tier") {
        None | Some(Value::Null) => None,
        Some(Value::String(tier)) if MODEL_TIERS.contains(&tier.as_str()) => Some(tier.clone()),
        _ => bail!("model config model_tier must be frontier or fast"),
    };

    let model_value = object
        .get_mut("model")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow::anyhow!("model config must contain a model mapping"))?;
    normalize_model_value(model_value);
    let model_spec = ModelSpec::from_json_value(Value::Object(model_value.clone()))
        .map_err(|_| anyhow::anyhow!("model config is invalid"))?;
    let has_explicit_output_limit = model_value_has_explicit_output_limit(model_value);

    let run_value = object
        .entry("run_config".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    let run_map = run_value
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("model config run_config must be a mapping"))?;
    if run_map.contains_key("run_config_sha256") {
        bail!("run_config_sha256 cannot be specified in run_config YAML");
    }
    run_map
        .entry("capture_transcript".to_owned())
        .or_insert(Value::Bool(true));
    if !run_map.contains_key("provider_max_output_tokens") && has_explicit_output_limit {
        run_map.insert(
            "provider_max_output_tokens".to_owned(),
            Value::from(model_spec.max_output_tokens),
        );
        if model_spec.max_output_tokens < ib_v5_runner::MAX_OUTPUT_TOKENS_V5 as u64
            && !run_map.contains_key("max_output_tokens")
            && !run_map.contains_key("requested_output_ceiling_tokens")
            && !run_map.contains_key("base_effective_output_ceiling_tokens")
        {
            run_map.insert(
                "max_output_tokens".to_owned(),
                Value::from(model_spec.max_output_tokens),
            );
        }
    }
    let run_config = RunConfigV5::from_value(Value::Object(run_map.clone()))
        .map_err(|_| anyhow::anyhow!("model run configuration is invalid"))?;

    Ok(LoadedRunConfig {
        model_spec,
        run_config,
        model_tier,
    })
}

fn model_value_has_explicit_output_limit(model: &Map<String, Value>) -> bool {
    model.contains_key("max_output_tokens")
}

fn normalize_model_value(model: &mut Map<String, Value>) {
    let provider = model.get("provider").and_then(Value::as_str);
    let normalize_retries = matches!(
        provider,
        Some(
            "anthropic"
                | "anthropic_compatible"
                | "openai_compatible"
                | "claude_cli"
                | "codex_cli"
                | "grok_cli"
                | "agy_cli"
                | "cursor_cli"
                | "muse_cli"
        )
    );
    if !normalize_retries {
        return;
    }
    match model.get_mut("extra") {
        None | Some(Value::Null) => {
            let mut extra = Map::new();
            extra.insert("max_retries".to_owned(), Value::from(0));
            model.insert("extra".to_owned(), Value::Object(extra));
        }
        Some(Value::Object(extra)) => {
            extra
                .entry("max_retries".to_owned())
                .or_insert(Value::from(0));
        }
        Some(_) => {}
    }
}

/// Build the canonical public identity hashed into `run_config_sha256`.
///
/// Source hashes describe this Rust checkout. They are not presented as hashes of the Python
/// implementation that the port replaces.
pub fn config_identity(
    model_spec: &ModelSpec,
    run_config: &RunConfigV5,
    model_tier: Option<&str>,
    pricing_manifest_sha256: &str,
    usage_accounting_manifest_sha256: Option<&str>,
    provider: &dyn Provider,
    repeats: u8,
) -> Result<Value> {
    let mut config = serde_json::to_value(run_config)?;
    let run = config
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("run config serialization is invalid"))?;
    run.remove("pricing");
    run.remove("usage_accounting_evidence");
    run.remove("run_config_sha256");

    let mut provider_settings = Map::new();
    provider_settings.insert(
        "max_retries".to_owned(),
        model_spec
            .extra
            .get("max_retries")
            .cloned()
            .unwrap_or_else(|| Value::String("provider_default".to_owned())),
    );
    provider_settings.insert(
        "timeout_s".to_owned(),
        effective_timeout_s(model_spec).map_or(Value::Null, Value::from),
    );
    provider_settings.insert(
        "concurrency".to_owned(),
        Value::from(model_spec.concurrency.unwrap_or(1)),
    );
    match model_spec.provider {
        ProviderKind::OpenaiCompatible => {
            provider_settings.insert(
                "api".to_owned(),
                model_spec
                    .extra
                    .get("api")
                    .cloned()
                    .unwrap_or_else(|| Value::String("chat_completions".to_owned())),
            );
            provider_settings.insert(
                "reasoning_effort_parameter_enabled".to_owned(),
                model_spec
                    .extra
                    .get("reasoning_effort_param")
                    .cloned()
                    .unwrap_or(Value::Bool(true)),
            );
            provider_settings.insert(
                "max_tokens_parameter".to_owned(),
                model_spec
                    .extra
                    .get("max_tokens_param")
                    .cloned()
                    .unwrap_or_else(|| Value::String("max_completion_tokens".to_owned())),
            );
            if let Some(routing) = model_spec.extra.get("provider_routing") {
                provider_settings.insert(
                    "provider_routing_sha256".to_owned(),
                    Value::String(sha256_hex(jcs_dumps(routing)?)),
                );
            }
        }
        _ => {}
    }
    if let Some(binary) = model_spec.extra.get("binary").and_then(Value::as_str) {
        provider_settings.insert(
            "binary_reference_sha256".to_owned(),
            Value::String(sha256_hex(binary.as_bytes())),
        );
    }
    if let Some(path) = model_spec.extra.get("path").and_then(Value::as_str) {
        provider_settings.insert(
            "replay_path_sha256".to_owned(),
            Value::String(sha256_hex(path.as_bytes())),
        );
        if model_spec.provider == ProviderKind::Replay {
            let responses = std::fs::read(path)
                .map_err(|_| anyhow::anyhow!("replay source identity is unavailable"))?;
            provider_settings.insert(
                "replay_responses_sha256".to_owned(),
                Value::String(sha256_hex(responses)),
            );
        }
    }
    provider_settings.insert(
        "endpoint_identity".to_owned(),
        Value::String(provider.endpoint_identity()),
    );

    let profile = run_config
        .resolved_topology_profile()
        .map_err(|_| anyhow::anyhow!("run topology profile is invalid"))?;
    let profile_sha = run_config
        .run_configuration
        .as_ref()
        .map(|frozen| frozen.topology_profile_sha256.clone())
        .unwrap_or(profile.topology_profile_sha256);
    let primary_prompt_sha = run_config
        .run_configuration
        .as_ref()
        .map(|frozen| frozen.primary_prompt_bundle_sha256.clone())
        .unwrap_or_else(|| sha256_hex(render_v5_system_prompt(run_config).as_bytes()));
    let prompt_sha = sha256_hex(render_v5_system_prompt(run_config).as_bytes());
    let tools = tool_specs(None)
        .map_err(|_| anyhow::anyhow!("runner tool schema identity is unavailable"))?;
    let tool_schema_sha = sha256_hex(jcs_dumps(&Value::Array(
        tools
            .iter()
            .map(|tool| {
                json!({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                })
            })
            .collect(),
    ))?);
    let source_hashes = rust_source_identity();

    Ok(json!({
        "model_id": model_spec.display_label(),
        "model_label": model_spec.label,
        "provider": provider.kind(),
        "model": provider.model(),
        "model_tier": model_tier,
        "effort": run_config.effort,
        "topology_id": run_config.topology_id,
        "steering_mode": run_config.steering_mode,
        "wave_execution_mode": run_config.wave_execution_mode,
        "planned_repeats_per_episode": repeats,
        "comparison_track": run_config.effort_track,
        "max_generations": run_config.max_assistant_generations_per_episode,
        "cap_disclosed": run_config.cap_disclosed,
        "topology_profile_sha256": profile_sha,
        "primary_prompt_bundle_sha256": primary_prompt_sha,
        "prompt_sha256": prompt_sha,
        "run_config": Value::Object(run.clone()),
        "provider_settings": Value::Object(provider_settings),
        "tool_schema_sha256": tool_schema_sha,
        "rust_source_sha256": source_hashes,
        "pricing_manifest_sha256": pricing_manifest_sha256,
        // This Rust identity binds the verified usage authority because it changes how billed
        // usage is interpreted. It also invalidates resume rows when an attestation is added,
        // replaced, or removed. The Python implementation did not include this field.
        "usage_accounting_manifest_sha256": usage_accounting_manifest_sha256,
    }))
}

fn effective_timeout_s(model_spec: &ModelSpec) -> Option<f64> {
    let default = match model_spec.provider {
        ProviderKind::Anthropic
        | ProviderKind::AnthropicCompatible
        | ProviderKind::OpenaiCompatible
        | ProviderKind::ClaudeCli
        | ProviderKind::GrokCli
        | ProviderKind::CursorCli => 600.0,
        ProviderKind::CodexCli | ProviderKind::AgyCli | ProviderKind::MuseCli => 900.0,
        // The replay provider does not perform timed I/O.
        ProviderKind::Replay => return None,
    };
    Some(model_spec.timeout_s.unwrap_or(default))
}

include!(concat!(env!("OUT_DIR"), "/source_identity.rs"));

fn rust_source_identity() -> Value {
    // The build script hashes a sorted source list and watches each input for changes. Digests
    // stay embedded in the executable without embedding source files or reading the checkout.
    let mut hashes = Map::new();
    for (label, digest) in EMBEDDED_SOURCE_IDENTITY.iter().copied() {
        hashes.insert(label.to_owned(), Value::String(digest.to_owned()));
    }
    Value::Object(hashes)
}

#[cfg(test)]
mod tests {
    use super::*;

    use ib_providers::factory::make_provider_with_env;
    use ib_providers::replay::ReplayProvider;

    #[test]
    fn usage_accounting_manifest_digest_changes_run_identity() {
        let model = ModelSpec::new(ProviderKind::Replay, "offline-replay");
        let provider = ReplayProvider::from_responses(Default::default(), "offline-replay");
        let run_config = RunConfigV5::default();
        let identity = |manifest_sha: Option<&str>| {
            config_identity(
                &model,
                &run_config,
                None,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                manifest_sha,
                &provider,
                1,
            )
            .unwrap()
        };
        let absent = identity(None);
        let first = identity(Some(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ));
        let replacement = identity(Some(
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        ));
        let digest = |value: &Value| sha256_hex(jcs_dumps(value).unwrap());
        assert_eq!(absent["usage_accounting_manifest_sha256"], Value::Null);
        assert_ne!(digest(&absent), digest(&first));
        assert_ne!(digest(&first), digest(&replacement));
        assert_ne!(digest(&absent), digest(&replacement));
    }

    #[test]
    fn effective_timeout_and_concurrency_change_run_identity() {
        let model = ModelSpec::new(ProviderKind::Anthropic, "mock-model");
        let provider =
            make_provider_with_env(&model, Some(std::collections::BTreeMap::new())).unwrap();
        let run_config = RunConfigV5::default();
        let identity = |spec: &ModelSpec, provider: &dyn Provider| {
            config_identity(
                spec,
                &run_config,
                None,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                None,
                provider,
                1,
            )
            .unwrap()
        };
        let base = identity(&model, provider.as_ref());
        assert_eq!(base["provider_settings"]["timeout_s"], Value::from(600.0));
        assert_eq!(base["provider_settings"]["concurrency"], Value::from(1));

        let mut slower_timeout = model.clone();
        slower_timeout.timeout_s = Some(601.0);
        let slower_provider =
            make_provider_with_env(&slower_timeout, Some(std::collections::BTreeMap::new()))
                .unwrap();
        let timeout_identity = identity(&slower_timeout, slower_provider.as_ref());

        let mut more_concurrency = model.clone();
        more_concurrency.concurrency = Some(2);
        let concurrent_provider =
            make_provider_with_env(&more_concurrency, Some(std::collections::BTreeMap::new()))
                .unwrap();
        let concurrency_identity = identity(&more_concurrency, concurrent_provider.as_ref());

        let digest = |value: &Value| sha256_hex(jcs_dumps(value).unwrap());
        assert_ne!(digest(&base), digest(&timeout_identity));
        assert_ne!(digest(&base), digest(&concurrency_identity));
    }

    #[test]
    fn replay_response_bytes_change_run_identity_even_when_keys_are_unchanged() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("responses.jsonl");
        let record = |answer: &str| {
            format!("{{\"key\":\"same-cache-key\",\"response\":{{\"text\":\"{answer}\"}}}}\n")
        };
        std::fs::write(&path, record("answer A")).unwrap();

        let mut model = ModelSpec::new(ProviderKind::Replay, "offline-replay");
        model.extra.insert(
            "path".to_owned(),
            Value::String(path.to_string_lossy().into_owned()),
        );
        let run_config = RunConfigV5::default();
        let provider_a = ReplayProvider::from_path(&path, "offline-replay").unwrap();
        let identity_a = config_identity(
            &model,
            &run_config,
            None,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            None,
            &provider_a,
            1,
        )
        .unwrap();

        std::fs::write(&path, record("answer B")).unwrap();
        let provider_b = ReplayProvider::from_path(&path, "offline-replay").unwrap();
        let identity_b = config_identity(
            &model,
            &run_config,
            None,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            None,
            &provider_b,
            1,
        )
        .unwrap();

        assert_eq!(
            provider_a.endpoint_identity(),
            provider_b.endpoint_identity()
        );
        assert_ne!(
            identity_a["provider_settings"]["replay_responses_sha256"],
            identity_b["provider_settings"]["replay_responses_sha256"]
        );
        assert_ne!(
            sha256_hex(jcs_dumps(&identity_a).unwrap()),
            sha256_hex(jcs_dumps(&identity_b).unwrap())
        );
    }
}
