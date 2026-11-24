//! Replay provider: serves recorded responses by cache key, for CI and re-scoring (SPEC §11.1).

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{self, BufRead, BufReader};
use std::path::Path;

use async_trait::async_trait;
use ib_core::pyjson::{PyNumber, classify_number};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::base::{
    ModelRequest, ModelResponse, Provider, ProviderCapabilities, ToolCall, Usage, hash_endpoint,
};
use crate::json::{
    BoundedJsonError, MAX_RESPONSE_DEPTH, MAX_RESPONSE_NODES, bounded_token_count,
    inspect_and_clamp_usage, parse_bounded_json, sanitize_response_post_usage,
};
use crate::subprocess::{redact, redact_diagnostics_map};

pub const CACHE_KEY_PARAM: &str = "cache_key";
const MAX_REPLAY_RECORD_BYTES: usize = 16 * 1024 * 1024;
const MAX_REPLAY_KEY_BYTES: usize = 4 * 1024;

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
            .map_or(Value::Null, |s| Value::String(redact(s, None, &[]))),
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
            .map_or(Value::Null, |s| Value::String(redact(s, None, &[]))),
    );
    map.insert(
        "reported_cost_usd".to_string(),
        response.reported_cost_usd.map_or(Value::Null, Value::from),
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
        response.raw.as_ref().map_or(Value::Null, |r| {
            Value::Object(redact_diagnostics_map(r, None, &[]))
        }),
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
            "unknown ModelResponse fields ({} fields)",
            sorted.len()
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
                return Err(ReplayError(format!(
                    "unknown Usage fields ({} fields)",
                    bad_u.len()
                )));
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
        latency_s: data.get("latency_s").and_then(Value::as_f64).unwrap_or(0.0),
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

    inspect_and_clamp_usage(raw_usage_fields.as_ref(), &mut resp.usage, &mut resp.raw);
    Ok(sanitize_replay_response(resp, "replay"))
}

fn redact_replay_diagnostics(mut response: ModelResponse) -> ModelResponse {
    if let Some(error) = response.error.as_mut() {
        *error = redact(error, None, &[]);
    }
    for field in [&mut response.stop_reason, &mut response.served_model]
        .into_iter()
        .flatten()
    {
        *field = redact(field, None, &[]);
    }
    if let Some(raw) = response.raw.take() {
        response.raw = Some(redact_diagnostics_map(&raw, None, &[]));
    }
    response
}

fn sanitize_replay_response(response: ModelResponse, kind: &str) -> ModelResponse {
    redact_replay_diagnostics(sanitize_response_post_usage(response, kind))
}

pub fn recorded_cost_from(
    record: &Map<String, Value>,
) -> Result<Option<RecordedCost>, ReplayError> {
    let has_usd = record.contains_key("cost_usd");
    let has_src = record.contains_key("cost_source");
    if !has_usd && !has_src {
        return Ok(None);
    }
    if has_usd && !has_src {
        return Err(ReplayError("'cost_usd' needs {'cost_source'}".to_string()));
    }
    if !has_usd && has_src {
        return Err(ReplayError("'cost_source' needs {'cost_usd'}".to_string()));
    }

    let cost_usd = match record.get("cost_usd") {
        Some(Value::Null) => None,
        Some(Value::Number(n)) => {
            let f = match classify_number(n) {
                PyNumber::Int(s) => s.parse::<f64>().unwrap_or(f64::NAN),
                PyNumber::Float(f) => f,
            };
            if !f.is_finite() || f < 0.0 {
                return Err(ReplayError(
                    "'cost_usd' must be a finite non-negative number or null".to_string(),
                ));
            }
            Some(f)
        }
        Some(_) => {
            return Err(ReplayError(
                "'cost_usd' must be a finite non-negative number or null".to_string(),
            ));
        }
        None => unreachable!(),
    };

    let source_str = record
        .get("cost_source")
        .and_then(Value::as_str)
        .ok_or_else(|| ReplayError("'cost_source' must be a supported string".to_string()))?;
    let cost_source = match source_str {
        "metered" => CostSource::Metered,
        "reported" => CostSource::Reported,
        "unmetered" => CostSource::Unmetered,
        "unknown" => CostSource::Unknown,
        _ => {
            return Err(ReplayError(
                "'cost_source' must be a supported string".to_string(),
            ));
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
    let file = File::open(path).map_err(|error| {
        ReplayError(format!(
            "replay file could not be read ({:?})",
            error.kind()
        ))
    })?;
    let mut reader = BufReader::new(file);
    let mut line = Vec::new();
    let mut records = BTreeMap::new();
    let mut number = 0usize;
    loop {
        line.clear();
        let read = read_bounded_line(&mut reader, &mut line, MAX_REPLAY_RECORD_BYTES).map_err(
            |error| {
                if error.kind() == io::ErrorKind::InvalidData {
                    ReplayError(format!(
                        "replay line {} exceeds the {MAX_REPLAY_RECORD_BYTES} byte limit",
                        number + 1
                    ))
                } else {
                    ReplayError(format!("replay file read failed ({:?})", error.kind()))
                }
            },
        )?;
        if !read {
            break;
        }
        number += 1;
        if line.last() == Some(&b'\n') {
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
        }
        let text = std::str::from_utf8(&line)
            .map_err(|_| ReplayError(format!("replay line {number}: invalid UTF-8")))?;
        if text.trim().is_empty() {
            continue;
        }
        let (parsed_line, _) = parse_bounded_json(text, MAX_RESPONSE_NODES, MAX_RESPONSE_DEPTH)
            .map_err(|error| match error {
                BoundedJsonError::TooManyNodes | BoundedJsonError::TooDeep => ReplayError(format!(
                    "replay line {number}: record exceeds structural limits"
                )),
                BoundedJsonError::Invalid => {
                    ReplayError(format!("replay line {number}: invalid JSON record"))
                }
            })?;
        let obj = parsed_line.as_object().ok_or_else(|| {
            ReplayError(format!(
                "replay line {number}: record must be a JSON object"
            ))
        })?;
        let key = obj.get("key").and_then(Value::as_str).ok_or_else(|| {
            ReplayError(format!(
                "replay line {number}: 'key' must be a string and 'response' an object"
            ))
        })?;
        if key.len() > MAX_REPLAY_KEY_BYTES {
            return Err(ReplayError(format!(
                "replay line {number}: key exceeds the {MAX_REPLAY_KEY_BYTES} byte limit"
            )));
        }
        let resp_obj = obj
            .get("response")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                ReplayError(format!(
                    "replay line {number}: 'key' must be a string and 'response' an object"
                ))
            })?;
        let resp = response_from_dict(resp_obj)
            .map_err(|e| ReplayError(format!("replay line {number}: invalid response ({e})")))?;
        let cost = recorded_cost_from(obj).map_err(|e| {
            ReplayError(format!("replay line {number}: invalid recorded cost ({e})"))
        })?;
        records.insert(key.to_string(), (resp, cost));
    }
    Ok(records)
}

fn read_bounded_line(
    reader: &mut impl BufRead,
    output: &mut Vec<u8>,
    max_bytes: usize,
) -> io::Result<bool> {
    output.clear();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(!output.is_empty());
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let take = newline.map_or(available.len(), |index| index + 1);
        if output.len().saturating_add(take) > max_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "replay record exceeds size limit",
            ));
        }
        output.extend_from_slice(&available[..take]);
        reader.consume(take);
        if newline.is_some() {
            return Ok(true);
        }
    }
}

pub fn load_responses_jsonl(
    path: impl AsRef<Path>,
) -> Result<BTreeMap<String, ModelResponse>, ReplayError> {
    let loaded = load_records(path.as_ref())?;
    Ok(loaded.into_iter().map(|(k, (resp, _))| (k, resp)).collect())
}

pub struct ReplayProvider {
    model: String,
    records: BTreeMap<String, ModelResponse>,
    costs: BTreeMap<String, RecordedCost>,
    identity: String,
}

impl ReplayProvider {
    pub fn from_path(
        path: impl AsRef<Path>,
        model: impl Into<String>,
    ) -> Result<Self, ReplayError> {
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
            .map(|(k, v)| (k, sanitize_replay_response(v, "replay")))
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
            return ModelResponse::with_error("replay: no recorded response for requested key");
        };
        sanitize_replay_response(recorded.clone(), self.kind())
    }
}

#[cfg(test)]
mod replay_security_tests {
    use super::read_bounded_line;
    use std::io::{BufReader, Cursor, ErrorKind};

    #[test]
    fn bounded_replay_line_reader_stops_at_limit_without_consuming_a_record() {
        let mut reader = BufReader::new(Cursor::new(b"abc\ndef"));
        let mut line = Vec::new();

        assert!(read_bounded_line(&mut reader, &mut line, 4).unwrap());
        assert_eq!(line, b"abc\n");
        assert!(read_bounded_line(&mut reader, &mut line, 3).unwrap());
        assert_eq!(line, b"def");
        assert!(!read_bounded_line(&mut reader, &mut line, 3).unwrap());

        let mut oversized = BufReader::new(Cursor::new(b"abcdef\n"));
        assert_eq!(
            read_bounded_line(&mut oversized, &mut line, 5)
                .unwrap_err()
                .kind(),
            ErrorKind::InvalidData
        );
    }

    #[test]
    fn replay_record_redacts_metadata_but_preserves_visible_model_output() {
        use crate::base::{ModelResponse, ToolCall};
        use serde_json::{Map, json};

        use super::{response_from_dict, response_to_dict};

        let credential_pattern = "sk-123456789012345678901234";
        let response = ModelResponse {
            text: Some(credential_pattern.to_string()),
            served_model: Some(credential_pattern.to_string()),
            stop_reason: Some(credential_pattern.to_string()),
            tool_calls: vec![ToolCall {
                id: credential_pattern.to_string(),
                name: "lookup".to_string(),
                arguments: Map::new(),
            }],
            raw: Some(
                json!({"assistant_content": credential_pattern, "id": credential_pattern})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            ..ModelResponse::default()
        };
        let dict = response_to_dict(&response);
        assert_eq!(dict["text"], credential_pattern);
        assert!(
            !dict["served_model"]
                .as_str()
                .unwrap()
                .contains(credential_pattern)
        );
        assert!(
            !dict["stop_reason"]
                .as_str()
                .unwrap()
                .contains(credential_pattern)
        );
        assert_eq!(dict["tool_calls"][0]["id"], credential_pattern);
        assert!(
            !dict["raw"]["id"]
                .as_str()
                .unwrap()
                .contains(credential_pattern)
        );
        assert_eq!(dict["raw"]["assistant_content"], credential_pattern);

        let round_trip = response_from_dict(dict.as_object().unwrap()).unwrap();
        assert_eq!(round_trip.text.as_deref(), Some(credential_pattern));
        assert!(
            !round_trip
                .served_model
                .as_deref()
                .unwrap()
                .contains(credential_pattern)
        );
        assert!(
            !round_trip
                .stop_reason
                .as_deref()
                .unwrap()
                .contains(credential_pattern)
        );
        assert_eq!(round_trip.tool_calls[0].id, credential_pattern);
        assert_eq!(
            round_trip.raw.unwrap()["assistant_content"],
            credential_pattern
        );
    }
}
