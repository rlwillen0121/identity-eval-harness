use ib_providers::base::{ModelResponse, ToolCall, Usage};
use ib_providers::json::{
    MAX_JSON_TEXT, MAX_RESPONSE_DEPTH, MAX_TOKEN_COUNT, extract_json_object, json_safe_usage_value,
    load_object, raw_usage_payload, safe_nonnegative_float_add, sanitize_response, strip_reasoning,
};
use ib_providers::replay::{response_from_dict, response_to_dict};
use serde_json::{Map, Value, json};

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

#[test]
fn extracts_objects_and_final_answer_wins() {
    let cases = [
        (r#"{"decision": "DENY"}"#, json!({"decision": "DENY"})),
        (
            "  \n{\"decision\": \"DENY\"}\n\n",
            json!({"decision": "DENY"}),
        ),
        ("\u{feff}{\"a\": 1}", json!({"a": 1})),
        (
            "```json\n{\"decision\": \"APPROVE\"}\n```",
            json!({"decision": "APPROVE"}),
        ),
        (
            "```JSON\n{\"decision\": \"APPROVE\"}\n```",
            json!({"decision": "APPROVE"}),
        ),
        (
            "```\n{\"decision\": \"ESCALATE\"}\n```",
            json!({"decision": "ESCALATE"}),
        ),
        (
            "Here you go:\n```json\n{\"x\": [1, 2]}\n```\nThanks!",
            json!({"x": [1, 2]}),
        ),
        (
            r#"My answer is {"decision": "DENY", "rules": ["SOD-001"]} as required."#,
            json!({"decision": "DENY", "rules": ["SOD-001"]}),
        ),
        (
            r#"Use the {name} placeholder... {"ok": true}"#,
            json!({"ok": true}),
        ),
        (
            r#"{"outer": {"inner": {"deep": 1}}} trailing"#,
            json!({"outer": {"inner": {"deep": 1}}}),
        ),
        (
            r#"{"s": "a } brace and a { brace"}"#,
            json!({"s": "a } brace and a { brace"}),
        ),
        (
            r#"{"first": 1} and then {"second": 2}"#,
            json!({"second": 2}),
        ),
        (
            r#"{"s": "<think>not reasoning</think>"}"#,
            json!({"s": "<think>not reasoning</think>"}),
        ),
    ];

    for (text, expected) in cases {
        assert_eq!(
            extract_json_object(Some(text), None, None),
            Some(obj(expected)),
            "failed for {text}"
        );
    }
}

#[test]
fn reasoning_and_code_blocks_and_required_and_schema_keys() {
    let deny = obj(json!({"decision": "DENY", "rules": ["SOD-001"]}));
    let texts = [
        "<think>Draft: {\"decision\": \"APPROVE\"}</think>\n{\"decision\": \"DENY\", \"rules\": [\"SOD-001\"]}",
        "<thinking>{\"decision\": \"APPROVE\"}</thinking>{\"decision\": \"DENY\", \"rules\": [\"SOD-001\"]}",
        "Considering {\"decision\": \"APPROVE\"}...</think>\n{\"decision\": \"DENY\", \"rules\": [\"SOD-001\"]}",
        "```python\nprint(\"hi\")\n```\nresult: {\"decision\": \"DENY\", \"rules\": [\"SOD-001\"]}\n```yaml\n{\"decision\": \"APPROVE\"}\n```",
    ];
    for t in texts {
        assert_eq!(extract_json_object(Some(t), None, None), Some(deny.clone()));
    }

    assert_eq!(
        extract_json_object(
            Some(r#"<think>{"decision": "APPROVE"}</think>"#),
            None,
            None
        ),
        None
    );
    assert_eq!(
        extract_json_object(Some(r#"<think>{"decision": "APPROVE"}"#), None, None),
        None
    );
    assert_eq!(
        extract_json_object(Some(r#"{"decision": "APPROVE"}</think>"#), None, None),
        None
    );
    assert_eq!(
        extract_json_object(Some("```python\nx = {\"a\": 1}\n```"), None, None),
        None
    );

    assert_eq!(
        strip_reasoning("a<think>x</think>b<THINKING>y</THINKING>c"),
        "abc"
    );
    assert_eq!(strip_reasoning("reasoning</think>answer"), "answer");
    assert_eq!(strip_reasoning("answer<think>unfinished"), "answer");
    assert_eq!(
        strip_reasoning(r#"<think>draft</think>{"a": "<think>"} tail<think>unfinished"#),
        r#"{"a": "<think>"} tail"#
    );

    let aside = r#"Answer: {"decision":"DENY","rules":[]} Note: see {"rule":"LIFE-001"}"#;
    assert_eq!(
        extract_json_object(Some(aside), Some(&["decision", "rules"]), None),
        Some(obj(json!({"decision": "DENY", "rules": []})))
    );
    assert_eq!(
        extract_json_object(Some(aside), None, None),
        Some(obj(json!({"rule": "LIFE-001"})))
    );

    // Schema keys pick an incomplete answer-like object over an earlier complete draft
    let incomplete_after_draft =
        r#"Draft {"decision":"APPROVE","rules":[]} Final {"decision":"DENY"} Note {"rule":"X"}"#;
    assert_eq!(
        extract_json_object(
            Some(incomplete_after_draft),
            Some(&["decision", "rules"]),
            Some(&["decision", "rules"])
        ),
        Some(obj(json!({"decision": "DENY"})))
    );
}

#[test]
fn bounds_and_deep_nesting_fail_closed() {
    let oversized = format!(r#"{{"answer":"{}"}}"#, "x".repeat(MAX_JSON_TEXT + 1));
    assert_eq!(extract_json_object(Some(&oversized), None, None), None);

    let deep_braces = "{".repeat(1000);
    assert_eq!(extract_json_object(Some(&deep_braces), None, None), None);
    let deep_arrays = "[".repeat(30_000);
    assert_eq!(load_object(&deep_arrays), None);
    assert_eq!(extract_json_object(Some(&deep_arrays), None, None), None);
    assert_eq!(extract_json_object(Some(r#"{"a": 1,}"#), None, None), None);
}

#[test]
fn safe_nonnegative_float_add_edges() {
    assert_eq!(safe_nonnegative_float_add(1.0, 2.5), Ok((3.5, false)));
    assert_eq!(
        safe_nonnegative_float_add(f64::MAX, 1.0),
        Ok((f64::MAX, true))
    );
    assert_eq!(
        safe_nonnegative_float_add(f64::MAX, f64::MAX),
        Ok((f64::MAX, true))
    );
    assert!(safe_nonnegative_float_add(-1.0, 1.0).is_err());
    assert!(safe_nonnegative_float_add(1.0, f64::NAN).is_err());
    assert!(safe_nonnegative_float_add(f64::INFINITY, 1.0).is_err());
}

#[test]
fn response_boundary_clamps_usage_and_rejects_excessive_values() {
    let mut big_usage_map = Map::new();
    let huge_int: Value = serde_json::from_str(&format!("1{}", "0".repeat(400))).unwrap();
    big_usage_map.insert("output_tokens".to_string(), huge_int);
    let mut resp_dict = Map::new();
    resp_dict.insert("usage".to_string(), Value::Object(big_usage_map));
    resp_dict.insert("text".to_string(), Value::String("ok".to_string()));
    let resp = response_from_dict(&resp_dict).unwrap();
    assert_eq!(resp.usage.output_tokens, MAX_TOKEN_COUNT);
    let raw = resp.raw.as_ref().unwrap();
    assert_eq!(raw["usage_anomaly"], json!({"output_tokens": "clamped"}));
    assert_eq!(
        raw["usage_original"]["output_tokens"],
        json!({"type": "integer", "value": format!("1{}", "0".repeat(400))})
    );

    // Oversized text fails closed
    let oversized_resp = sanitize_response(
        ModelResponse {
            text: Some("x".repeat(MAX_JSON_TEXT + 1)),
            tool_calls: vec![ToolCall {
                id: "c".to_string(),
                name: "t".to_string(),
                arguments: Map::new(),
            }],
            ..ModelResponse::default()
        },
        "fixture",
    );
    assert_eq!(
        oversized_resp.error.as_deref(),
        Some("fixture: malformed response")
    );
    assert!(oversized_resp.text.is_none());
    assert!(oversized_resp.tool_calls.is_empty());

    // Oversized integer in raw fails closed
    let big_raw_val: Value =
        serde_json::from_str(&format!("{{\"value\": 1{}}}", "0".repeat(400))).unwrap();
    let big_raw_resp = sanitize_response(
        ModelResponse {
            raw: Some(obj(big_raw_val)),
            ..ModelResponse::default()
        },
        "fixture",
    );
    assert_eq!(
        big_raw_resp.error.as_deref(),
        Some("fixture: malformed response")
    );
    assert!(big_raw_resp.raw.is_none());

    // Excessive list length (> 10_000) fails closed
    let many = vec![Value::Null; 10_001];
    let mut many_map = Map::new();
    many_map.insert("items".to_string(), Value::Array(many));
    let many_resp = sanitize_response(
        ModelResponse {
            raw: Some(many_map),
            ..ModelResponse::default()
        },
        "fixture",
    );
    assert_eq!(
        many_resp.error.as_deref(),
        Some("fixture: malformed response")
    );

    // Excessive depth (> MAX_RESPONSE_DEPTH) fails closed
    let mut deep = json!({"leaf": "value"});
    for _ in 0..(MAX_RESPONSE_DEPTH + 2) {
        deep = json!({"next": deep});
    }
    let deep_resp = sanitize_response(
        ModelResponse {
            raw: Some(obj(json!({"deep": deep}))),
            ..ModelResponse::default()
        },
        "fixture",
    );
    assert_eq!(
        deep_resp.error.as_deref(),
        Some("fixture: malformed response")
    );
    assert!(deep_resp.raw.is_none());

    // Normal usage & codex list raw_usage preserved
    let normal = sanitize_response(
        ModelResponse {
            usage: Usage {
                input_tokens: 3,
                output_tokens: 2,
                ..Usage::default()
            },
            raw: Some(obj(
                json!({"raw_usage": [{"input_tokens": 3, "output_tokens": 2}]}),
            )),
            ..ModelResponse::default()
        },
        "codex",
    );
    assert!(normal.error.is_none());
    assert_eq!(
        normal.raw.as_ref().unwrap(),
        &obj(json!({"raw_usage": [{"input_tokens": 3, "output_tokens": 2}]}))
    );

    // json_safe_usage_value & raw_usage_payload
    let payload =
        raw_usage_payload(&json!({"input_tokens": 1, "big": "x".repeat(20_000)})).unwrap();
    assert_eq!(
        payload["big"]["__identitybench_usage_marker__"],
        "string_limit"
    );
    assert_eq!(json_safe_usage_value(&json!(1), &[]), json!(1));
    let serialized = response_to_dict(&normal);
    assert_eq!(serialized["usage"]["input_tokens"], json!(3));
}
