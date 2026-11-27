use std::collections::BTreeMap;
use std::fs;

use ib_providers::anthropic_api::{AnthropicConfig, AnthropicProvider};
use ib_providers::base::{
    GenerationParams, ModelRequest, ModelResponse, Provider, ToolCall, Usage,
};
use ib_providers::cli::CliConfig;
use ib_providers::factory::{ModelSpec, PROVIDER_KINDS, ProviderKind, make_provider_with_env};
use ib_providers::openai_compat::{OpenAICompatConfig, OpenAICompatProvider};
use ib_providers::replay::{
    CACHE_KEY_PARAM, CostSource, RecordedCost, ReplayProvider, load_responses_jsonl,
    recorded_cost_from, response_from_dict, response_to_dict,
};
use serde_json::{Map, json};
use tempfile::TempDir;

fn sample_response() -> ModelResponse {
    ModelResponse {
        text: Some(r#"{"decision": "DENY"}"#.to_string()),
        parsed: Some(json!({"decision": "DENY"}).as_object().unwrap().clone()),
        tool_calls: vec![ToolCall {
            id: "t1".to_string(),
            name: "get_identity".to_string(),
            arguments: json!({"id": "U1"}).as_object().unwrap().clone(),
        }],
        stop_reason: Some("end_turn".to_string()),
        refused: false,
        usage: Usage {
            input_tokens: 10,
            output_tokens: 5,
            cache_read_tokens: 100,
            cache_write_tokens: 0,
            reasoning_tokens: 2,
        },
        latency_s: 1.5,
        served_model: Some("claude-opus-5-5".to_string()),
        reported_cost_usd: Some(0.01),
        error: None,
        raw: Some(json!({"id": "msg_1"}).as_object().unwrap().clone()),
    }
}

fn keyed(key: Option<&str>) -> ModelRequest {
    let mut req = ModelRequest::default();
    if let Some(k) = key {
        req.params
            .extra
            .insert(CACHE_KEY_PARAM.to_string(), json!(k));
    }
    req
}

#[tokio::test]
async fn replay_round_trip_jsonl_costs_and_has() {
    let rec = sample_response();
    let dict = response_to_dict(&rec);
    assert_eq!(response_from_dict(dict.as_object().unwrap()).unwrap(), rec);

    // Rejects unknown fields and non-object usage
    assert!(
        response_from_dict(json!({"text": "x", "confidence": 1}).as_object().unwrap()).is_err()
    );
    assert!(
        response_from_dict(
            json!({"text": "x", "usage": {"tokens": 1}})
                .as_object()
                .unwrap()
        )
        .is_err()
    );
    for bad_usage in [json!([]), json!(false), json!(0), json!("")] {
        let mut m = Map::new();
        m.insert("usage".to_string(), bad_usage);
        assert!(response_from_dict(&m).is_err());
    }

    let tmp = TempDir::new().unwrap();
    let path = tmp.path().join("responses.jsonl");
    let lines = [
        json!({"key": "k1", "response": response_to_dict(&ModelResponse { text: Some("old".to_string()), ..ModelResponse::default() })}).to_string(),
        String::new(),
        json!({"key": "k1", "response": response_to_dict(&rec), "cost_usd": 0.5, "cost_source": "metered"}).to_string(),
        json!({"key": "k2", "response": {"error": "transport failed"}, "cost_usd": null, "cost_source": "unknown"}).to_string(),
        json!({"key": "k3", "response": response_to_dict(&rec), "cost_usd": 0, "cost_source": "unmetered"}).to_string(),
    ];
    fs::write(&path, lines.join("\n") + "\n").unwrap();

    let replay = ReplayProvider::from_path(&path, "claude-opus-5-5").unwrap();
    assert_eq!(replay.len(), 3);
    assert!(replay.has("k1"));
    assert!(!replay.has("missing"));
    assert_eq!(replay.complete(&keyed(Some("k1"))).await, rec);
    assert_eq!(
        replay.complete(&keyed(Some("k2"))).await.error.as_deref(),
        Some("transport failed")
    );
    assert!(
        replay
            .complete(&keyed(Some("missing")))
            .await
            .error
            .as_deref()
            .unwrap()
            .contains("no recorded response")
    );
    assert!(
        replay
            .complete(&keyed(None))
            .await
            .error
            .as_deref()
            .unwrap()
            .contains("cache_key")
    );
    assert_eq!(
        replay.recorded_cost("k1"),
        Some(&RecordedCost {
            cost_usd: Some(0.5),
            cost_source: CostSource::Metered
        })
    );
    assert_eq!(
        replay.recorded_cost("k2"),
        Some(&RecordedCost {
            cost_usd: None,
            cost_source: CostSource::Unknown
        })
    );
    assert_eq!(
        replay.recorded_cost("k3"),
        Some(&RecordedCost {
            cost_usd: Some(0.0),
            cost_source: CostSource::Unmetered
        })
    );
    assert!(load_responses_jsonl(&path).unwrap().contains_key("k1"));

    // Malformed cost fails loudly
    assert!(recorded_cost_from(json!({"cost_usd": 0.1}).as_object().unwrap()).is_err());
    assert!(
        recorded_cost_from(
            json!({"cost_usd": -1, "cost_source": "metered"})
                .as_object()
                .unwrap()
        )
        .is_err()
    );
    assert!(
        recorded_cost_from(
            json!({"cost_usd": true, "cost_source": "metered"})
                .as_object()
                .unwrap()
        )
        .is_err()
    );
    assert!(
        recorded_cost_from(
            json!({"cost_usd": 0.1, "cost_source": "free"})
                .as_object()
                .unwrap()
        )
        .is_err()
    );
}

#[test]
fn replay_file_must_be_bounded_regular_and_not_a_symlink() {
    let tmp = TempDir::new().unwrap();
    let regular = tmp.path().join("responses.jsonl");
    fs::write(&regular, b"\n").unwrap();

    #[cfg(unix)]
    {
        let linked = tmp.path().join("linked.jsonl");
        std::os::unix::fs::symlink(&regular, &linked).unwrap();
        assert!(load_responses_jsonl(&linked).is_err());

        let directory = tmp.path().join("responses.directory");
        fs::create_dir(&directory).unwrap();
        assert!(load_responses_jsonl(&directory).is_err());
    }

    let oversized = tmp.path().join("oversized.jsonl");
    let file = fs::File::create(&oversized).unwrap();
    file.set_len(256 * 1024 * 1024 + 1).unwrap();
    assert!(load_responses_jsonl(&oversized).is_err());
}

#[test]
fn factory_validation_yaml_and_make_provider() {
    assert_eq!(
        PROVIDER_KINDS,
        &[
            "anthropic",
            "anthropic_compatible",
            "openai_compatible",
            "claude_cli",
            "codex_cli",
            "grok_cli",
            "agy_cli",
            "cursor_cli",
            "muse_cli",
            "replay",
        ]
    );

    let spec = ModelSpec::from_yaml_str(
        "provider: openai_compatible\nmodel: local-model\nbase_url_env: LOCAL_LLM_BASE_URL\ncache_namespace: deploy-1\nextra:\n  reasoning_effort_param: false\n  max_tokens_param: max_tokens\n",
    )
    .unwrap();
    assert_eq!(spec.display_label(), "openai_compatible:local-model");
    assert_eq!(spec.generation_params(), GenerationParams::default());

    let codex_spec = ModelSpec::new(ProviderKind::CodexCli, "gpt-x");
    assert_eq!(codex_spec.effective_effort(), Some("medium"));
    assert_eq!(
        codex_spec.generation_params().effort.as_deref(),
        Some("medium")
    );

    // Rejections matching test_factory.py
    let bad_yamls = [
        "provider: gemini\nmodel: m\n",
        "provider: anthropic\nmodel: \"\"\n",
        "provider: anthropic\nmodel: m\ntypo_field: 1\n",
        "provider: anthropic\nmodel: m\nmax_output_tokens: 0\n",
        "provider: anthropic\nmodel: m\nbase_url_env: X\n",
        "provider: anthropic_compatible\nmodel: m\n",
        "provider: claude_cli\nmodel: m\nbase_url_env: X\n",
        "provider: cursor_cli\nmodel: auto\neffort: high\n",
        "provider: replay\nmodel: m\n",
        "provider: openai_compatible\nmodel: m\nextra:\n  reasoning_effort_param: \"false\"\n",
        "provider: anthropic\nmodel: m\nextra:\n  max_retries: true\n",
        "provider: anthropic\nmodel: m\nextra:\n  max_retries: -1\n",
        "provider: grok_cli\nmodel: m\nextra:\n  binary: \" \"\n",
        "provider: openai_compatible\nmodel: m\nextra:\n  provider_routing: []\n",
        "provider: openai_compatible\nmodel: m\nextra:\n  headers:\n    a: 1\n",
    ];
    for y in bad_yamls {
        assert!(
            ModelSpec::from_yaml_str(y).is_err(),
            "expected error for:\n{y}"
        );
    }

    // Build all providers via factory
    let env = BTreeMap::from([
        ("LOCAL_URL".to_string(), "http://127.0.0.1:8000".to_string()),
        (
            "LOCAL_LLM_BASE_URL".to_string(),
            "http://127.0.0.1:8001/v1".to_string(),
        ),
    ]);
    let ant = make_provider_with_env(
        &ModelSpec::from_yaml_str("provider: anthropic_compatible\nmodel: local\nbase_url_env: LOCAL_URL\nextra:\n  max_retries: 1\n").unwrap(),
        Some(env.clone()),
    )
    .unwrap();
    assert_eq!(ant.kind(), "anthropic_compatible");

    let oai = make_provider_with_env(&spec, Some(env)).unwrap();
    assert_eq!(oai.kind(), "openai_compatible");
}

#[test]
fn adapters_default_to_no_internal_retries() {
    // SPEC-v5.2 §7.4: every adapter defaults to `max_retries = 0`; the runner owns the cap.
    assert_eq!(OpenAICompatConfig::new("m").max_retries, 0);
    assert_eq!(AnthropicConfig::new("m").max_retries, 0);
    assert_eq!(CliConfig::new("m", "claude", 600.0).max_retries, 0);
}

#[test]
fn unrepresentable_provider_deadlines_are_rejected_before_dispatch() {
    let mut spec = ModelSpec::new(ProviderKind::OpenaiCompatible, "offline-fixture");
    spec.timeout_s = Some(f64::MAX);
    assert!(spec.validate().is_err());
    let mut anthropic = AnthropicConfig::new("offline-fixture");
    anthropic.timeout_s = f64::MAX;
    assert!(AnthropicProvider::new(anthropic).is_err());
    let mut compatible = OpenAICompatConfig::new("offline-fixture");
    compatible.timeout_s = f64::MAX;
    assert!(OpenAICompatProvider::new(compatible).is_err());
}
