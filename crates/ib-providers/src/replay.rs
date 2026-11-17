//! Replay provider: serves recorded responses by cache key, for CI and re-scoring (SPEC §11.1).

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use async_trait::async_trait;
use ib_core::pyjson::{PyNumber, classify_number};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::base::{
    ModelRequest, ModelResponse, Provider, ProviderCapabilities, ToolCall, Usage, hash_endpoint,
};
use crate::json::{bounded_token_count, inspect_and_clamp_usage, sanitize_response_post_usage};
use crate::subprocess::{redact, redact_diagnostics_map};

pub const CACHE_KEY_PARAM: &str = "cache_key";
pub const COST_FIELDS: &[&str] = &["cost_usd", "cost_source"];

const RESPONSE_FIELDS: &[&str] = &[
    "text",
    "parsed",
    "tool_calls",
    "stop_reason",
    "refused",
    "usage",
    "latency_s",
    "served_model",
    "reported_cost_usd",
    "error",
    "raw",
];

const USAGE_FIELDS: &[&str] = &[
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CostSource {
    Metered,
    Reported,
    Unmetered,
    Unknown,
}

impl CostSource {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Metered => "metered",
            Self::Reported => "reported",
            Self::Unmetered => "unmetered",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecordedCost {
    pub cost_usd: Option<f64>,
    pub cost_source: CostSource,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ReplayError(pub String);

#[must_use]
pub fn response_to_dict(response: &ModelResponse) -> Value {
    let mut map = Map::new();
    map.insert(
        "text".to_string(),
        response
            .text
            .as_ref()
            .map_or(Value::Null, |s| Value::String(s.clone())),
    );
    map.insert(
        "parsed".to_string(),
        response
            .parsed
            .as_ref()
            .map_or(Value::Null, |m| Value::Object(m.clone())),
    );
    let calls = response
        .tool_calls
        .iter()
        .map(|call| {
            let mut cm = Map::new();
            cm.insert("id".to_string(), Value::String(call.id.clone()));
            cm.insert("name".to_string(), Value::String(call.name.clone()));
            cm.insert(
                "arguments".to_string(),
                Value::Object(call.arguments.clone()),
            );
            Value::Object(cm)
        })
        .collect();
    map.insert("tool_calls".to_string(), Value::Array(calls));
    map.insert(
        "stop_reason".to_string(),
        response
            .stop_reason
            .as_ref()
            .map_or(Value::Null, |s| Value::String(s.clone())),
    );
    map.insert("refused".to_string(), Value::Bool(response.refused));

    let mut usage_map = Map::new();
    usage_map.insert(
        "input_tokens".to_string(),
        Value::from(response.usage.input_tokens),
    );
    usage_map.insert(
        "output_tokens".to_string(),
        Value::from(response.usage.output_tokens),
    );
    usage_map.insert(
        "cache_read_tokens".to_string(),
        Value::from(response.usage.cache_read_tokens),
    );
    usage_map.insert(
        "cache_write_tokens".to_string(),
        Value::from(response.usage.cache_write_tokens),
    );
    usage_map.insert(
        "reasoning_tokens".to_string(),
        Value::from(response.usage.reasoning_tokens),
    );
    map.insert("usage".to_string(), Value::Object(usage_map));

    map.insert("latency_s".to_string(), Value::from(response.latency_s));
    map.insert(
        "served_model".to_string(),
        response
            .served_model
            .as_ref()
            .map_or(Value::Null, |s| Value::String(s.clone())),
    );
    map.insert(
        "reported_cost_usd".to_string(),
        response
            .reported_cost_usd
            .map_or(Value::Null, Value::from),
    );
    map.insert(
        "error".to_string(),
        response
            .error
            .as_ref()
            .map_or(Value::Null, |e| Value::String(redact(e, None, &[]))),
    );
    map.insert(
        "raw".to_string(),
        response
            .raw
            .as_ref()
            .map_or(Value::Null, |r| Value::Object(redact_diagnostics_map(r, None, &[]))),
    );
    Value::Object(map)
}

pub fn response_from_dict(data: &Map<String, Value>) -> Result<ModelResponse, ReplayError> {
    let allowed: BTreeSet<&str> = RESPONSE_FIELDS.iter().copied().collect();
    let unknown: Vec<&str> = data
        .keys()
        .map(String::as_str)
        .filter(|k| !allowed.contains(k))
        .collect();
    if !unknown.is_empty() {
        let mut sorted = unknown;
        sorted.sort_unstable();
        return Err(ReplayError(format!(
            "unknown ModelResponse fields: {sorted:?}"
        )));
    }

    let raw_usage_fields: Option<Map<String, Value>>;
    let mut usage = Usage::default();
    match data.get("usage") {
        None | Some(Value::Null) => {
            raw_usage_fields = None;
        }
        Some(Value::Object(umap)) => {
            let allowed_u: BTreeSet<&str> = USAGE_FIELDS.iter().copied().collect();
            let mut bad_u: Vec<&str> = umap
                .keys()
                .map(String::as_str)
                .filter(|k| !allowed_u.contains(k))
                .collect();
            if !bad_u.is_empty() {
                bad_u.sort_unstable();
                return Err(ReplayError(format!("unknown Usage fields: {bad_u:?}")));
            }
            usage = Usage {
                input_tokens: bounded_token_count(umap.get("input_tokens")),
                output_tokens: bounded_token_count(umap.get("output_tokens")),
                cache_read_tokens: bounded_token_count(umap.get("cache_read_tokens")),
                cache_write_tokens: bounded_token_count(umap.get("cache_write_tokens")),
                reasoning_tokens: bounded_token_count(umap.get("reasoning_tokens")),
            };
            raw_usage_fields = Some(umap.clone());
        }
        Some(_) => {
            return Err(ReplayError(
                "malformed response (usage must be an object)".to_string(),
            ));
        }
    }

    let mut calls = Vec::new();
    match data.get("tool_calls") {
        None => {}
        Some(Value::Array(arr)) => {
            for item in arr {
                let Some(obj) = item.as_object() else {
                    return Err(ReplayError(
                        "malformed response (invalid tool calls)".to_string(),
                    ));
                };
                let Some(id) = obj.get("id").and_then(Value::as_str) else {
                    return Err(ReplayError(
                        "malformed response (invalid tool calls)".to_string(),
                    ));
                };
                let Some(name) = obj.get("name").and_then(Value::as_str) else {
                    return Err(ReplayError(
                        "malformed response (invalid tool calls)".to_string(),
                    ));
                };
                let arguments = match obj.get("arguments") {
                    None => Map::new(),
                    Some(Value::Object(args)) => args.clone(),
                    Some(_) => {
                        return Err(ReplayError(
                            "malformed response (invalid tool calls)".to_string(),
                        ));
                    }
                };
                calls.push(ToolCall {
                    id: id.to_string(),
                    name: name.to_string(),
                    arguments,
                });
            }
        }
        Some(_) => {
            return Err(ReplayError(
                "malformed response (invalid tool calls)".to_string(),
            ));
        }
    }

    let mut resp = ModelResponse {
        text: match data.get("text") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                return Err(ReplayError("malformed response (invalid text)".to_string()));
            }
        },
        parsed: match data.get("parsed") {
            None | Some(Value::Null) => None,
            Some(Value::Object(m)) => Some(m.clone()),
            Some(_) => {
                return Err(ReplayError(
                    "malformed response (invalid parsed)".to_string(),
                ));
            }
        },
        tool_calls: calls,
        stop_reason: match data.get("stop_reason") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                return Err(ReplayError(
                    "malformed response (invalid stop_reason)".to_string(),
                ));
            }
        },
        refused: data
            .get("refused")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        usage,
        latency_s: data
            .get("latency_s")
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        served_model: match data.get("served_model") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                return Err(ReplayError(
                    "malformed response (invalid served_model)".to_string(),
                ));
            }
        },
        reported_cost_usd: match data.get("reported_cost_usd") {
            None | Some(Value::Null) => None,
            Some(Value::Number(n)) => n.as_f64(),
            Some(_) => None,
        },
        error: match data.get("error") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                return Err(ReplayError(
                    "malformed response (invalid error)".to_string(),
                ));
            }
        },
        raw: match data.get("raw") {
            None | Some(Value::Null) => None,
            Some(Value::Object(m)) => Some(m.clone()),
            Some(_) => {
                return Err(ReplayError("malformed response (invalid raw)".to_string()));
            }
        },
    };

    inspect_and_clamp_usage(
        raw_usage_fields.as_ref(),
        &mut resp.usage,
        &mut resp.raw,
    );
    Ok(sanitize_response_post_usage(resp, "replay"))
}

pub fn recorded_cost_from(record: &Map<String, Value>) -> Result<Option<RecordedCost>, ReplayError> {
    let has_usd = record.contains_key("cost_usd");
    let has_src = record.contains_key("cost_source");
    if !has_usd && !has_src {
        return Ok(None);
    }
    if has_usd && !has_src {
        return Err(ReplayError(
            "'cost_usd' needs {'cost_source'}".to_string(),
        ));
    }
    if !has_usd && has_src {
        return Err(ReplayError(
            "'cost_source' needs {'cost_usd'}".to_string(),
        ));
    }

    let cost_usd = match record.get("cost_usd") {
        Some(Value::Null) => None,
        Some(Value::Number(n)) => {
            let f = match classify_number(n) {
                PyNumber::Int(s) => s.parse::<f64>().unwrap_or(f64::NAN),
                PyNumber::Float(f) => f,
            };
            if !f.is_finite() || f < 0.0 {
                return Err(ReplayError(format!(
                    "'cost_usd' must be a non-negative number or null, got {n}"
                )));
            }
            Some(f)
        }
        Some(other) => {
            return Err(ReplayError(format!(
                "'cost_usd' must be a non-negative number or null, got {other}"
            )));
        }
        None => unreachable!(),
    };

    let source_str = record
        .get("cost_source")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ReplayError(format!(
                "'cost_source' must be one of [\"metered\", \"reported\", \"unknown\", \"unmetered\"], got {:?}",
                record.get("cost_source")
            ))
        })?;
    let cost_source = match source_str {
        "metered" => CostSource::Metered,
        "reported" => CostSource::Reported,
        "unmetered" => CostSource::Unmetered,
        "unknown" => CostSource::Unknown,
        other => {
            return Err(ReplayError(format!(
                "'cost_source' must be one of [\"metered\", \"reported\", \"unknown\", \"unmetered\"], got {other:?}"
            )));
        }
    };

    Ok(Some(RecordedCost {
        cost_usd,
        cost_source,
    }))
}

fn load_records(
    path: &Path,
) -> Result<BTreeMap<String, (ModelResponse, Option<RecordedCost>)>, ReplayError> {
    let text = fs::read_to_string(path)
        .map_err(|e| ReplayError(format!("{}: could not read file ({e})", path.display())))?;
    let mut records = BTreeMap::new();
    for (idx, line) in text.lines().enumerate() {
        let number = idx + 1;
        if line.trim().is_empty() {
            continue;
        }
        let parsed_line: Value = serde_json::from_str(line).map_err(|e| {
            ReplayError(format!(
                "{}:{number}: not a replay record ({e})",
                path.display()
            ))
        })?;
        let obj = parsed_line.as_object().ok_or_else(|| {
            ReplayError(format!(
                "{}:{number}: not a replay record (expected object)",
                path.display()
            ))
        })?;
        let key = obj.get("key").and_then(Value::as_str).ok_or_else(|| {
            ReplayError(format!(
                "{}:{number}: not a replay record ('key' must be a string and 'response' an object)",
                path.display()
            ))
        })?;
        let resp_obj = obj
            .get("response")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                ReplayError(format!(
                    "{}:{number}: not a replay record ('key' must be a string and 'response' an object)",
                    path.display()
                ))
            })?;
        let resp = response_from_dict(resp_obj).map_err(|e| {
            ReplayError(format!(
                "{}:{number}: not a replay record ({e})",
                path.display()
            ))
        })?;
        let cost = recorded_cost_from(obj).map_err(|e| {
            ReplayError(format!(
                "{}:{number}: not a replay record ({e})",
                path.display()
            ))
        })?;
        records.insert(key.to_string(), (resp, cost));
    }
    Ok(records)
}

pub fn load_responses_jsonl(
    path: impl AsRef<Path>,
) -> Result<BTreeMap<String, ModelResponse>, ReplayError> {
    let loaded = load_records(path.as_ref())?;
    Ok(loaded
        .into_iter()
        .map(|(k, (resp, _))| (k, resp))
        .collect())
}

pub struct ReplayProvider {
    model: String,
    records: BTreeMap<String, ModelResponse>,
    costs: BTreeMap<String, RecordedCost>,
    identity: String,
}

impl ReplayProvider {
    pub fn from_path(path: impl AsRef<Path>, model: impl Into<String>) -> Result<Self, ReplayError> {
        let model = model.into();
        let loaded = load_records(path.as_ref())?;
        let mut records = BTreeMap::new();
        let mut costs = BTreeMap::new();
        for (key, (resp, cost)) in loaded {
            records.insert(key.clone(), resp);
            if let Some(c) = cost {
                costs.insert(key, c);
            }
        }
        Ok(Self::build_with_maps(model, records, costs))
    }

    #[must_use]
    pub fn from_responses(
        records: BTreeMap<String, ModelResponse>,
        model: impl Into<String>,
    ) -> Self {
        let sanitized = records
            .into_iter()
            .map(|(k, v)| (k, sanitize_response_post_usage(v, "replay")))
            .collect();
        Self::build_with_maps(model.into(), sanitized, BTreeMap::new())
    }

    pub fn from_dicts(
        records: BTreeMap<String, Map<String, Value>>,
        model: impl Into<String>,
    ) -> Result<Self, ReplayError> {
        let mut out = BTreeMap::new();
        for (k, v) in records {
            out.insert(k, response_from_dict(&v)?);
        }
        Ok(Self::build_with_maps(model.into(), out, BTreeMap::new()))
    }

    fn build_with_maps(
        model: String,
        records: BTreeMap<String, ModelResponse>,
        costs: BTreeMap<String, RecordedCost>,
    ) -> Self {
        let joined_keys: Vec<&str> = records.keys().map(String::as_str).collect();
        let keys_digest = hex::encode(Sha256::digest(joined_keys.join("\n").as_bytes()));
        let identity = hash_endpoint(&[Some("replay"), Some(&model), Some(&keys_digest)]);
        Self {
            model,
            records,
            costs,
            identity,
        }
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.records.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    #[must_use]
    pub fn has(&self, key: &str) -> bool {
        self.records.contains_key(key)
    }

    #[must_use]
    pub fn recorded_cost(&self, key: &str) -> Option<&RecordedCost> {
        self.costs.get(key)
    }
}

#[async_trait]
impl Provider for ReplayProvider {
    fn kind(&self) -> &str {
        "replay"
    }

    fn model(&self) -> &str {
        &self.model
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("identitybench.providers.replay", "ReplayProvider")
    }

    fn endpoint_identity(&self) -> String {
        self.identity.clone()
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(true, true, true)
    }

    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        let Some(Value::String(key)) = request.params.extra.get(CACHE_KEY_PARAM) else {
            return ModelResponse::with_error(format!(
                "replay: request has no params.extra[{CACHE_KEY_PARAM:?}]"
            ));
        };
        let Some(recorded) = self.records.get(key) else {
            return ModelResponse::with_error(format!(
                "replay: no recorded response for key {key}"
            ));
        };
        sanitize_response_post_usage(recorded.clone(), self.kind())
    }
}
