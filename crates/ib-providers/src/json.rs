//! JSON decoding for providers: safe object decoding, bounded sanitization, and tolerant extraction (SPEC §11.3).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use ib_core::pyjson::{PyNumber, classify_number};
use regex::Regex;
use serde::Deserializer;
use serde::de::{self, DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};

use crate::base::{ModelResponse, ToolCall, Usage};

pub const MAX_JSON_TEXT: usize = 1024 * 1024;
pub const MAX_TOKEN_COUNT: u64 = 1_000_000_000;
pub const MAX_RESPONSE_NODES: usize = 10_000;
pub const MAX_RESPONSE_DEPTH: usize = 64;
pub const MAX_USAGE_DEPTH: usize = 48;
pub const MAX_USAGE_NODES: usize = 4_096;
pub const MAX_USAGE_STRING: usize = 16_384;
const SCAN_WORK_BUDGET: isize = 256 * (MAX_JSON_TEXT as isize);
pub const USAGE_MARKER: &str = "__identitybench_usage_marker__";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BoundedJsonError {
    TooManyNodes,
    TooDeep,
    Invalid,
}

struct JsonNodeBudget {
    nodes: usize,
    max_nodes: usize,
    max_depth: usize,
    exceeded: Option<BoundedJsonError>,
}

struct JsonNodeSeed<'a> {
    budget: &'a mut JsonNodeBudget,
    depth: usize,
}

struct JsonNodeVisitor<'a> {
    budget: &'a mut JsonNodeBudget,
    depth: usize,
}

impl<'de> DeserializeSeed<'de> for JsonNodeSeed<'_> {
    type Value = ();

    fn deserialize<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: Deserializer<'de>,
    {
        if self.depth > self.budget.max_depth {
            self.budget.exceeded = Some(BoundedJsonError::TooDeep);
            return Err(de::Error::custom("JSON nesting exceeds response limit"));
        }
        if self.budget.nodes >= self.budget.max_nodes {
            self.budget.exceeded = Some(BoundedJsonError::TooManyNodes);
            return Err(de::Error::custom("JSON nodes exceed response limit"));
        }
        self.budget.nodes += 1;
        deserializer.deserialize_any(JsonNodeVisitor {
            budget: self.budget,
            depth: self.depth,
        })
    }
}

impl<'de> Visitor<'de> for JsonNodeVisitor<'_> {
    type Value = ();

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON value")
    }

    fn visit_bool<E: de::Error>(self, _value: bool) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_i64<E: de::Error>(self, _value: i64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_u64<E: de::Error>(self, _value: u64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_f64<E: de::Error>(self, _value: f64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_str<E: de::Error>(self, _value: &str) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_string<E: de::Error>(self, _value: String) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        while seq
            .next_element_seed(JsonNodeSeed {
                budget: &mut *self.budget,
                depth: self.depth + 1,
            })?
            .is_some()
        {}
        Ok(())
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        while map.next_key::<IgnoredAny>()?.is_some() {
            map.next_value_seed(JsonNodeSeed {
                budget: &mut *self.budget,
                depth: self.depth + 1,
            })?;
        }
        Ok(())
    }
}

/// Parse JSON without first building a DOM, then parse it into a `Value` only if it stays within
/// the response's node and nesting limits. The returned count supports aggregate SSE budgets.
pub(crate) fn parse_bounded_json(
    text: &str,
    max_nodes: usize,
    max_depth: usize,
) -> Result<(Value, usize), BoundedJsonError> {
    let mut budget = JsonNodeBudget {
        nodes: 0,
        max_nodes,
        max_depth,
        exceeded: None,
    };
    let mut deserializer = serde_json::Deserializer::from_str(text);
    let scan = JsonNodeSeed {
        budget: &mut budget,
        depth: 0,
    }
    .deserialize(&mut deserializer);
    if let Some(error) = budget.exceeded {
        return Err(error);
    }
    scan.map_err(|_| BoundedJsonError::Invalid)?;
    deserializer.end().map_err(|_| BoundedJsonError::Invalid)?;
    let value = serde_json::from_str(text).map_err(|_| BoundedJsonError::Invalid)?;
    Ok((value, budget.nodes))
}

/// Count already-decoded JSON value nodes iteratively while enforcing structure limits.
pub(crate) fn value_node_count(
    value: &Value,
    max_nodes: usize,
    max_depth: usize,
) -> Result<usize, BoundedJsonError> {
    let mut pending = vec![(value, 0usize)];
    let mut nodes = 0usize;
    while let Some((current, depth)) = pending.pop() {
        if depth > max_depth {
            return Err(BoundedJsonError::TooDeep);
        }
        if nodes >= max_nodes {
            return Err(BoundedJsonError::TooManyNodes);
        }
        nodes += 1;
        match current {
            Value::Array(values) => {
                pending.extend(values.iter().map(|child| (child, depth + 1)));
            }
            Value::Object(values) => {
                pending.extend(values.values().map(|child| (child, depth + 1)));
            }
            _ => {}
        }
    }
    Ok(nodes)
}

static FENCE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?s)```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```").expect("valid fence regex")
});
static THINK_TAG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<(/?)(think|thinking)>").expect("valid think regex"));
static CAMEL_CASE_BOUNDARY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"([a-z0-9])([A-Z])").expect("valid camel regex"));
static ACRONYM_BOUNDARY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"([A-Z]+)([A-Z][a-z])").expect("valid acronym regex"));

const DATA_FENCE_TAGS: &[&str] = &["", "json", "jsonc", "json5"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("nesting deeper than decoder supports")]
pub struct TooDeep;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct UnsafeResponseData(pub String);

/// Clamp untrusted usage counts before arithmetic or cache serialization.
#[must_use]
pub fn bounded_token_count(value: Option<&Value>) -> u64 {
    let Some(val) = value else {
        return 0;
    };
    match val {
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(s) => {
                if s.starts_with('-') {
                    0
                } else {
                    s.parse::<u64>()
                        .map_or(MAX_TOKEN_COUNT, |v| v.min(MAX_TOKEN_COUNT))
                }
            }
            PyNumber::Float(f) => {
                if f.is_finite() {
                    let clamped = f.clamp(0.0, MAX_TOKEN_COUNT as f64);
                    clamped as u64
                } else {
                    0
                }
            }
        },
        _ => 0,
    }
}

/// Add finite nonnegative costs without overflowing; return `(sum, saturated)`.
pub fn safe_nonnegative_float_add(total: f64, addition: f64) -> Result<(f64, bool), String> {
    if !total.is_finite() || total < 0.0 {
        return Err("total must be a finite nonnegative float".to_string());
    }
    if !addition.is_finite() || addition < 0.0 {
        return Err("addition must be a finite nonnegative float".to_string());
    }
    if addition > f64::MAX - total {
        Ok((f64::MAX, true))
    } else {
        Ok((total + addition, false))
    }
}

struct SafeBudget {
    nodes: isize,
    bytes: isize,
}

fn marker_obj(marker: &str) -> Value {
    let mut m = Map::new();
    m.insert(USAGE_MARKER.to_string(), Value::String(marker.to_string()));
    Value::Object(m)
}

fn safe_tree(
    value: &Value,
    usage: bool,
    excluded_keys: &[&str],
    budget: &mut SafeBudget,
    depth: usize,
) -> Result<Value, UnsafeResponseData> {
    let depth_limit = if usage {
        MAX_USAGE_DEPTH
    } else {
        MAX_RESPONSE_DEPTH
    };
    budget.nodes -= 1;
    if budget.nodes < 0 || depth > depth_limit {
        if usage {
            return Ok(marker_obj("node_or_depth_limit"));
        }
        return Err(UnsafeResponseData(
            "response data exceeds structural limits".to_string(),
        ));
    }
    match value {
        Value::Null | Value::Bool(_) => Ok(value.clone()),
        Value::String(s) => {
            let char_len = s.chars().count();
            budget.bytes -= char_len as isize;
            let limit = if usage {
                MAX_USAGE_STRING
            } else {
                MAX_JSON_TEXT
            };
            if char_len > limit || budget.bytes < 0 {
                if usage {
                    let mut m = Map::new();
                    m.insert(
                        USAGE_MARKER.to_string(),
                        Value::String("string_limit".to_string()),
                    );
                    m.insert("length".to_string(), Value::from(char_len as u64));
                    return Ok(Value::Object(m));
                }
                return Err(UnsafeResponseData(
                    "response data exceeds string limits".to_string(),
                ));
            }
            Ok(Value::String(s.clone()))
        }
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(s) => {
                let fits = if let Ok(v) = s.parse::<i128>() {
                    v.unsigned_abs() <= u128::from(MAX_TOKEN_COUNT)
                } else {
                    false
                };
                if fits {
                    Ok(Value::Number(n.clone()))
                } else if usage {
                    // Check bit length <= 4096 (~1234 decimal digits)
                    if s.trim_start_matches('-').len() <= 1234 {
                        let mut m = Map::new();
                        m.insert("type".to_string(), Value::String("integer".to_string()));
                        m.insert("value".to_string(), Value::String(s));
                        Ok(Value::Object(m))
                    } else {
                        Ok(marker_obj("integer_limit"))
                    }
                } else {
                    Err(UnsafeResponseData(
                        "response integer exceeds limits".to_string(),
                    ))
                }
            }
            PyNumber::Float(f) => {
                if f.is_finite() {
                    Ok(Value::Number(n.clone()))
                } else if usage {
                    let mut m = Map::new();
                    m.insert("type".to_string(), Value::String("float".to_string()));
                    m.insert("value".to_string(), Value::String(n.as_str().to_string()));
                    Ok(Value::Object(m))
                } else {
                    Ok(Value::Null)
                }
            }
        },
        Value::Array(items) => {
            let mut out = Vec::with_capacity(items.len().min(MAX_RESPONSE_NODES));
            for (index, child) in items.iter().enumerate() {
                if usage && index >= MAX_USAGE_NODES {
                    out.push(marker_obj("node_limit"));
                    break;
                }
                out.push(safe_tree(child, usage, excluded_keys, budget, depth + 1)?);
            }
            Ok(Value::Array(out))
        }
        Value::Object(map) => {
            let mut out = Map::new();
            for (index, (key, child)) in map.iter().enumerate() {
                if usage && index >= MAX_USAGE_NODES {
                    out.insert("__truncated__".to_string(), marker_obj("node_limit"));
                    break;
                }
                if excluded_keys.contains(&key.as_str()) {
                    continue;
                }
                let key_len = key.chars().count();
                budget.bytes -= key_len as isize;
                let limit = if usage {
                    MAX_USAGE_STRING
                } else {
                    MAX_JSON_TEXT
                };
                if key_len > limit || budget.bytes < 0 {
                    if usage {
                        out.insert("<oversized-key>".to_string(), marker_obj("string_limit"));
                        continue;
                    }
                    return Err(UnsafeResponseData(
                        "response data exceeds string limits".to_string(),
                    ));
                }
                out.insert(
                    key.clone(),
                    safe_tree(child, usage, excluded_keys, budget, depth + 1)?,
                );
            }
            Ok(Value::Object(out))
        }
    }
}

fn finite_json_with_budget(
    value: &Value,
    budget: &mut SafeBudget,
) -> Result<Value, UnsafeResponseData> {
    safe_tree(value, false, &[], budget, 0)
}

#[must_use]
pub fn json_safe_usage_value(value: &Value, excluded_keys: &[&str]) -> Value {
    let mut budget = SafeBudget {
        nodes: MAX_USAGE_NODES as isize,
        bytes: MAX_JSON_TEXT as isize,
    };
    safe_tree(value, true, excluded_keys, &mut budget, 0)
        .unwrap_or_else(|_| marker_obj("container_error"))
}

#[must_use]
pub fn raw_usage_payload(value: &Value) -> Option<Map<String, Value>> {
    if !value.is_object() {
        return None;
    }
    match json_safe_usage_value(value, &[]) {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

fn usage_count_problem(value: &Value) -> Option<&'static str> {
    match value {
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(s) => {
                if s.starts_with('-') {
                    Some("negative")
                } else if s.parse::<u64>().is_ok_and(|v| v <= MAX_TOKEN_COUNT) {
                    None
                } else {
                    Some("clamped")
                }
            }
            PyNumber::Float(_) => Some("not_integer"),
        },
        _ => Some("not_integer"),
    }
}

fn normalized_usage_key(value: &str) -> String {
    let step1 = ACRONYM_BOUNDARY_RE.replace_all(value, "${1}_${2}");
    CAMEL_CASE_BOUNDARY_RE
        .replace_all(&step1, "${1}_${2}")
        .to_lowercase()
}

fn truncate_path(path: &str) -> String {
    if path.chars().count() <= 512 {
        path.to_string()
    } else {
        path.chars().take(512).collect()
    }
}

fn provider_usage_problems(value: &Value, path: &str) -> BTreeMap<String, String> {
    let mut problems = BTreeMap::new();
    let mut pending: Vec<(&Value, String, usize)> = vec![(value, path.to_string(), 0)];
    let mut nodes = 0usize;

    while let Some((current, current_path, depth)) = pending.pop() {
        if problems.len() >= 128 {
            break;
        }
        nodes += 1;
        if nodes > MAX_USAGE_NODES {
            problems.insert(truncate_path(&current_path), "node_limit".to_string());
            break;
        }
        if depth > MAX_USAGE_DEPTH {
            problems.insert(truncate_path(&current_path), "depth_limit".to_string());
            continue;
        }
        match current {
            Value::Object(map) => {
                if let Some(Value::String(marker)) = map.get(USAGE_MARKER) {
                    problems.insert(truncate_path(&current_path), marker.clone());
                    continue;
                }
                for (index, (key, child)) in map.iter().enumerate() {
                    if index >= MAX_USAGE_NODES {
                        problems.insert(truncate_path(&current_path), "node_limit".to_string());
                        break;
                    }
                    if key.chars().count() > MAX_USAGE_STRING {
                        problems.insert(truncate_path(&current_path), "string_limit".to_string());
                        continue;
                    }
                    let child_path = format!("{current_path}.{key}");
                    let norm = normalized_usage_key(key);
                    if norm == "tokens"
                        || norm.ends_with("_tokens")
                        || norm.ends_with("_token_count")
                    {
                        match child {
                            Value::Object(cm) => {
                                if let Some(Value::String(m)) = cm.get(USAGE_MARKER) {
                                    problems.insert(truncate_path(&child_path), m.clone());
                                } else if matches!(
                                    cm.get("type").and_then(Value::as_str),
                                    Some("integer" | "float" | "string" | "omitted")
                                ) {
                                    problems.insert(
                                        truncate_path(&child_path),
                                        "not_integer".to_string(),
                                    );
                                } else {
                                    pending.push((child, child_path, depth + 1));
                                }
                            }
                            Value::Array(_) => {
                                pending.push((child, child_path, depth + 1));
                            }
                            _ => {
                                if let Some(problem) = usage_count_problem(child) {
                                    problems
                                        .insert(truncate_path(&child_path), problem.to_string());
                                }
                            }
                        }
                    } else {
                        pending.push((child, child_path, depth + 1));
                    }
                }
            }
            Value::Array(items) => {
                for (index, child) in items.iter().enumerate() {
                    if index >= MAX_USAGE_NODES {
                        problems.insert(truncate_path(&current_path), "node_limit".to_string());
                        break;
                    }
                    pending.push((child, format!("{current_path}[{index}]"), depth + 1));
                }
            }
            Value::String(s) if s.chars().count() > MAX_USAGE_STRING => {
                problems.insert(truncate_path(&current_path), "string_limit".to_string());
            }
            _ => {}
        }
    }

    problems
}

/// Inspect raw usage JSON values for anomalies and record `usage_original` + `usage_anomaly`
/// before normalizing `Usage` fields.
pub fn inspect_and_clamp_usage(
    raw_usage_fields: Option<&Map<String, Value>>,
    usage: &mut Usage,
    raw: &mut Option<Map<String, Value>>,
) {
    let mut usage_anomalies: BTreeMap<String, String> = BTreeMap::new();
    let mut original_usage: Map<String, Value> = Map::new();

    if let Some(fields_map) = raw_usage_fields {
        for (name, current_val) in [
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("cache_read_tokens", usage.cache_read_tokens),
            ("cache_write_tokens", usage.cache_write_tokens),
            ("reasoning_tokens", usage.reasoning_tokens),
        ] {
            let val = fields_map
                .get(name)
                .cloned()
                .unwrap_or_else(|| Value::from(current_val));
            original_usage.insert(name.to_string(), json_safe_usage_value(&val, &[]));
            if let Some(problem) = usage_count_problem(&val) {
                usage_anomalies.insert(name.to_string(), problem.to_string());
            }
        }
    } else {
        for (name, val) in [
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("cache_read_tokens", usage.cache_read_tokens),
            ("cache_write_tokens", usage.cache_write_tokens),
            ("reasoning_tokens", usage.reasoning_tokens),
        ] {
            let json_val = Value::from(val);
            original_usage.insert(name.to_string(), json_safe_usage_value(&json_val, &[]));
            if val > MAX_TOKEN_COUNT {
                usage_anomalies.insert(name.to_string(), "clamped".to_string());
            }
        }
    }

    if let Some(raw_map) = raw.as_ref()
        && let Some(provider_raw) = raw_map.get("raw_usage")
    {
        let provider_problems = provider_usage_problems(provider_raw, "raw_usage");
        if !provider_problems.is_empty() {
            usage_anomalies.extend(provider_problems);
            original_usage.insert(
                "provider_raw_usage".to_string(),
                json_safe_usage_value(provider_raw, &[]),
            );
        }
    }

    usage.input_tokens = usage.input_tokens.min(MAX_TOKEN_COUNT);
    usage.output_tokens = usage.output_tokens.min(MAX_TOKEN_COUNT);
    usage.cache_read_tokens = usage.cache_read_tokens.min(MAX_TOKEN_COUNT);
    usage.cache_write_tokens = usage.cache_write_tokens.min(MAX_TOKEN_COUNT);
    usage.reasoning_tokens = usage.reasoning_tokens.min(MAX_TOKEN_COUNT);

    if !usage_anomalies.is_empty() {
        let target = raw.get_or_insert_with(Map::new);
        if let Some(provider_raw) = target.get("raw_usage").cloned()
            && !original_usage.contains_key("provider_raw_usage")
        {
            original_usage.insert(
                "provider_raw_usage".to_string(),
                json_safe_usage_value(&provider_raw, &[]),
            );
        }
        target.insert("usage_original".to_string(), Value::Object(original_usage));
        let mut anomaly_map = Map::new();
        for (k, v) in usage_anomalies {
            anomaly_map.insert(k, Value::String(v));
        }
        target.insert("usage_anomaly".to_string(), Value::Object(anomaly_map));
    }
}

/// Validate and normalize untrusted provider/replay response data at the boundary.
pub fn sanitize_response(mut response: ModelResponse, kind: &str) -> ModelResponse {
    inspect_and_clamp_usage(None, &mut response.usage, &mut response.raw);
    sanitize_response_post_usage(response, kind)
}

pub(crate) fn sanitize_response_post_usage(
    mut response: ModelResponse,
    kind: &str,
) -> ModelResponse {
    let mut malformed = false;
    // Keep the size and digest of an oversized visible text so a runner can classify it as the
    // model's own output (`SPEC-v5.2` §3.1 `OUTPUT_LIMIT`) rather than a transport failure.
    let oversized_text = response.text.as_ref().and_then(|text| {
        (text.chars().count() > MAX_JSON_TEXT).then(|| {
            (
                text.len(),
                hex::encode(<sha2::Sha256 as sha2::Digest>::digest(text.as_bytes())),
            )
        })
    });
    for field in [
        &mut response.text,
        &mut response.served_model,
        &mut response.stop_reason,
        &mut response.error,
    ] {
        if let Some(s) = field
            && s.chars().count() > MAX_JSON_TEXT
        {
            *field = None;
            malformed = true;
        }
    }

    let mut budget = SafeBudget {
        nodes: MAX_RESPONSE_NODES as isize,
        bytes: MAX_JSON_TEXT as isize,
    };

    if let Some(raw_map) = response.raw.take() {
        match finite_json_with_budget(&Value::Object(raw_map), &mut budget) {
            Ok(Value::Object(clean)) => response.raw = Some(clean),
            _ => malformed = true,
        }
    }
    if let Some(parsed_map) = response.parsed.take() {
        match finite_json_with_budget(&Value::Object(parsed_map), &mut budget) {
            Ok(Value::Object(clean)) => response.parsed = Some(clean),
            _ => malformed = true,
        }
    }

    let mut normalized_calls = Vec::new();
    if response.tool_calls.len() > MAX_RESPONSE_NODES {
        malformed = true;
    } else {
        for call in response.tool_calls {
            let id_len = call.id.chars().count();
            let name_len = call.name.chars().count();
            if id_len > MAX_JSON_TEXT || name_len > MAX_JSON_TEXT {
                malformed = true;
                continue;
            }
            budget.bytes -= (id_len + name_len) as isize;
            if budget.bytes < 0 {
                malformed = true;
                continue;
            }
            match finite_json_with_budget(&Value::Object(call.arguments), &mut budget) {
                Ok(Value::Object(clean_args)) => normalized_calls.push(ToolCall {
                    id: call.id,
                    name: call.name,
                    arguments: clean_args,
                }),
                _ => malformed = true,
            }
        }
    }
    response.tool_calls = normalized_calls;

    if !response.latency_s.is_finite() || response.latency_s < 0.0 {
        response.latency_s = 0.0;
    }
    if let Some(cost) = response.reported_cost_usd
        && (!cost.is_finite() || cost < 0.0)
    {
        response.reported_cost_usd = None;
    }

    if malformed {
        response.text = None;
        response.parsed = None;
        response.tool_calls.clear();
        response.error = Some(format!("{kind}: malformed response"));
        if let Some((bytes, digest)) = oversized_text {
            let raw = response.raw.get_or_insert_with(Map::new);
            raw.insert(OVERSIZED_TEXT_BYTES.to_string(), Value::from(bytes as u64));
            raw.insert(OVERSIZED_TEXT_SHA256.to_string(), Value::String(digest));
        }
    }

    response
}

/// `ModelResponse::raw` key carrying the UTF-8 byte length of a dropped oversized visible text.
pub const OVERSIZED_TEXT_BYTES: &str = "oversized_text_bytes";
/// `ModelResponse::raw` key carrying the SHA-256 (hex) of a dropped oversized visible text.
pub const OVERSIZED_TEXT_SHA256: &str = "oversized_text_sha256";

/// `text` decoded as one JSON object, or `None` (invalid, not an object, or too deep).
#[must_use]
pub fn load_object(text: &str) -> Option<Map<String, Value>> {
    if text.chars().count() > MAX_JSON_TEXT {
        return None;
    }
    match parse_bounded_json(text, MAX_RESPONSE_NODES, MAX_RESPONSE_DEPTH)
        .ok()
        .map(|(value, _)| value)
    {
        Some(Value::Object(map)) => Some(map),
        _ => None,
    }
}

pub type ObjectSpan = (usize, usize, Map<String, Value>);

/// `(start, end, object)` for every top-level JSON object in `text`, in order.
pub fn top_level_objects(text: &str) -> Result<Vec<ObjectSpan>, TooDeep> {
    let mut out = Vec::new();
    let mut start = match text.find('{') {
        Some(pos) => pos,
        None => return Ok(out),
    };
    let mut remaining = SCAN_WORK_BUDGET;

    loop {
        let slice = &text[start..];
        let mut de = serde_json::Deserializer::from_str(slice).into_iter::<Value>();
        match de.next() {
            Some(Ok(Value::Object(map))) => {
                let end = start + de.byte_offset();
                out.push((start, end, map));
                match text[end..].find('{') {
                    Some(rel) => start = end + rel,
                    None => break,
                }
            }
            Some(Ok(_)) => match text[start + 1..].find('{') {
                Some(rel) => start = start + 1 + rel,
                None => break,
            },
            Some(Err(err)) => {
                if err.to_string().contains("recursion limit exceeded") {
                    return Err(TooDeep);
                }
                let consumed = (start + err.column()).max(1) as isize;
                remaining -= consumed;
                if remaining < 0 {
                    return Err(TooDeep);
                }
                match text[start + 1..].find('{') {
                    Some(rel) => start = start + 1 + rel,
                    None => break,
                }
            }
            None => break,
        }
    }

    Ok(out)
}

fn object_spans(text: &str) -> Vec<(usize, usize)> {
    match top_level_objects(text) {
        Ok(objs) => objs.into_iter().map(|(s, e, _)| (s, e)).collect(),
        Err(TooDeep) => Vec::new(),
    }
}

/// Remove `<think>...</think>` / `<thinking>...</thinking>` reasoning spans from a reply.
#[must_use]
pub fn strip_reasoning(text: &str) -> String {
    if text.chars().count() > MAX_JSON_TEXT {
        return String::new();
    }
    let found: Vec<_> = THINK_TAG_RE.captures_iter(text).collect();
    if found.is_empty() {
        return text.to_string();
    }

    let spans = object_spans(text);
    struct Tag {
        start: usize,
        end: usize,
        is_close: bool,
        name: String,
    }

    let mut tags = Vec::new();
    let mut span_index = 0;
    for caps in found {
        let full = caps.get(0).expect("full match");
        let is_close = !caps.get(1).expect("slash group").as_str().is_empty();
        let name = caps.get(2).expect("tag name").as_str().to_lowercase();
        while span_index < spans.len() && spans[span_index].1 <= full.start() {
            span_index += 1;
        }
        if span_index == spans.len() || full.start() <= spans[span_index].0 {
            tags.push(Tag {
                start: full.start(),
                end: full.end(),
                is_close,
                name,
            });
        }
    }

    let mut next_closes: BTreeMap<String, usize> = BTreeMap::new();
    let mut pairs: BTreeMap<usize, usize> = BTreeMap::new();
    for index in (0..tags.len()).rev() {
        if tags[index].is_close {
            next_closes.insert(tags[index].name.clone(), index);
        } else if let Some(&close_idx) = next_closes.get(&tags[index].name) {
            pairs.insert(index, close_idx);
        }
    }

    let mut removed = Vec::new();
    let mut stray = Vec::new();
    let mut index = 0;
    while index < tags.len() {
        if !tags[index].is_close
            && let Some(&close) = pairs.get(&index)
        {
            removed.push((tags[index].start, tags[close].end));
            index = close + 1;
            continue;
        }
        stray.push(index);
        index += 1;
    }

    let begin = stray
        .iter()
        .rev()
        .find(|&&i| tags[i].is_close)
        .map_or(0, |&i| tags[i].end);
    let finish = stray
        .iter()
        .find(|&&i| !tags[i].is_close && tags[i].start >= begin)
        .map_or(text.len(), |&i| tags[i].start);

    let mut pieces = String::new();
    let mut position = begin;
    for (start, end) in removed {
        if start >= begin && end <= finish {
            pieces.push_str(&text[position..start]);
            position = end;
        }
    }
    pieces.push_str(&text[position..finish]);
    pieces
}

/// Replace fenced blocks in non-data languages with spaces while preserving byte offsets.
#[must_use]
pub fn blank_code_fences(text: &str) -> String {
    FENCE_RE
        .replace_all(text, |caps: &regex::Captures<'_>| {
            let full = caps.get(0).expect("full match").as_str();
            let lang = caps.get(1).expect("lang").as_str().to_lowercase();
            if DATA_FENCE_TAGS.contains(&lang.as_str()) {
                full.to_string()
            } else {
                " ".repeat(full.len())
            }
        })
        .into_owned()
}

/// Extract the final JSON object from `text`, or `None`.
#[must_use]
pub fn extract_json_object(
    text: Option<&str>,
    required_keys: Option<&[&str]>,
    schema_keys: Option<&[&str]>,
) -> Option<Map<String, Value>> {
    let raw = text?;
    if raw.is_empty() || raw.chars().count() > MAX_JSON_TEXT {
        return None;
    }
    let stripped = raw.trim().trim_start_matches('\u{feff}').trim();
    if let Some(whole) = load_object(stripped) {
        return Some(whole);
    }

    let cleaned = strip_reasoning(stripped);
    let cleaned = cleaned.trim();
    if cleaned.is_empty() {
        return None;
    }
    if let Some(whole) = load_object(cleaned) {
        return Some(whole);
    }

    let blanked = blank_code_fences(cleaned);
    let objects: Vec<Map<String, Value>> = match top_level_objects(&blanked) {
        Ok(items) => items.into_iter().map(|(_, _, obj)| obj).collect(),
        Err(TooDeep) => return None,
    };
    if objects.is_empty() {
        return None;
    }

    if let Some(skeys) = schema_keys {
        let mut answer_like: BTreeSet<&str> = skeys.iter().copied().collect();
        if let Some(rkeys) = required_keys {
            answer_like.extend(rkeys.iter().copied());
        }
        for obj in objects.iter().rev() {
            if obj.keys().any(|k| answer_like.contains(k.as_str())) {
                return Some(obj.clone());
            }
        }
    } else if let Some(rkeys) = required_keys
        && !rkeys.is_empty()
    {
        for obj in objects.iter().rev() {
            if rkeys.iter().all(|k| obj.contains_key(*k)) {
                return Some(obj.clone());
            }
        }
    }

    objects.into_iter().last()
}

#[cfg(test)]
mod bounded_parse_tests {
    use super::{BoundedJsonError, MAX_JSON_TEXT, parse_bounded_json};
    use serde_json::Value;

    #[test]
    fn preparse_budget_limits_nodes_and_depth_but_not_text_bytes() {
        assert!(matches!(
            parse_bounded_json(r#"{"items":[1,2]}"#, 3, 64),
            Err(BoundedJsonError::TooManyNodes)
        ));
        assert!(matches!(
            parse_bounded_json("[[[]]]", 10, 1),
            Err(BoundedJsonError::TooDeep)
        ));
        assert!(matches!(
            parse_bounded_json("{invalid", 10, 64),
            Err(BoundedJsonError::Invalid)
        ));

        let visible = "x".repeat(MAX_JSON_TEXT + 1);
        let input = serde_json::json!({"text": visible}).to_string();
        let (value, nodes) = parse_bounded_json(&input, 2, 1).unwrap();
        assert_eq!(nodes, 2);
        assert_eq!(
            value.get("text").and_then(Value::as_str).unwrap().len(),
            MAX_JSON_TEXT + 1
        );
    }
}
