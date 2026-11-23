//! Map serde's input position to an RFC 6901 pointer without changing strict deserialization.

use std::borrow::Cow;

use ib_v52_schemas::{TransformExprV52, json_pointer_child};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::{Value, error::Category};

pub(super) fn deserialize_config_with_pointer<T: DeserializeOwned>(
    input: &Value,
) -> Result<T, (String, String)> {
    let text = match input {
        Value::String(raw) => Cow::Borrowed(raw.as_str()),
        value => Cow::Owned(value.to_string()),
    };
    serde_json::from_str(&text).map_err(|error| {
        let message = error.to_string();
        let line_start: usize = text
            .split_inclusive('\n')
            .take(error.line().saturating_sub(1))
            .map(str::len)
            .sum();
        let at = (line_start + error.column().saturating_sub(1)).min(text.len());
        let previous_byte = text.as_bytes()[..at]
            .iter()
            .rposition(|b| !b.is_ascii_whitespace());
        let mut pointer = match previous_byte {
            Some(pos) if error.is_syntax() && matches!(text.as_bytes()[pos], b':' | b',') => {
                input_pointer(&text, pos)
            }
            _ => input_pointer(&text, at),
        };
        if error.classify() == Category::Data {
            let description = message
                .rsplit_once(" at line ")
                .map_or(message.as_str(), |(msg, _)| msg);
            if let Some(field) = description
                .strip_prefix("duplicate field `")
                .and_then(|s| s.strip_suffix('`'))
                && let (_, Some(duplicate)) = locate_input(&text, at, Some(field))
            {
                return (duplicate, message);
            }
            if let Ok(document) = serde_json::from_str::<Value>(&text) {
                // The byte reported for a buffered value may be the following comma or brace.
                let previous = previous_byte.map(|pos| input_pointer(&text, pos));
                for candidate in std::iter::once(&pointer).chain(previous.as_ref()) {
                    let Some(node) = document.pointer(candidate) else {
                        continue;
                    };
                    if node.get("op").is_some()
                        && transform_error(node).as_deref() == Some(description)
                    {
                        return (
                            format!("{candidate}{}", transform_member(node, description)),
                            message,
                        );
                    }
                }
                if document.pointer(&pointer).is_some_and(Value::is_object)
                    && let Some(key) = missing_field(description)
                {
                    pointer = json_pointer_child(&pointer, key);
                }
            }
        }
        (pointer, message)
    })
}

fn missing_field(description: &str) -> Option<&str> {
    description
        .strip_prefix("missing field `")?
        .strip_suffix('`')
}

struct Container {
    path: String,
    key: Option<String>,
    array_index: Option<usize>,
    saw_field: bool,
}

impl Container {
    fn child(&self) -> String {
        if let Some(idx) = self.array_index {
            json_pointer_child(&self.path, &idx.to_string())
        } else if let Some(key) = &self.key {
            json_pointer_child(&self.path, key)
        } else {
            self.path.clone()
        }
    }
}

/// Follow only JSON tokens through serde's byte offset. This also locates errors in the
/// original raw string, where reparsing to a Value would discard duplicate object members.
fn input_pointer(text: &str, at: usize) -> String {
    locate_input(text, at, None).0
}

fn locate_input(text: &str, at: usize, duplicate_field: Option<&str>) -> (String, Option<String>) {
    let bytes = text.as_bytes();
    let mut duplicate_pointer = None;
    let mut stack: Vec<Container> = Vec::new();
    let mut cursor = 0;
    while cursor < bytes.len() {
        let start = cursor;
        let token = bytes[cursor];
        cursor += 1;
        match token {
            b' ' | b'\r' | b'\t' | b'\n' | b':' => {}
            b',' => {
                if let Some(parent) = stack.last_mut() {
                    if let Some(idx) = parent.array_index.as_mut() {
                        *idx += 1;
                    }
                    parent.key = None;
                }
            }
            b'}' | b']' => {
                if let Some(closed) = stack.pop()
                    && start >= at
                {
                    return (closed.path, duplicate_pointer);
                }
            }
            b'{' | b'[' => {
                let path = stack.last().map_or_else(String::new, Container::child);
                if start >= at {
                    return (path, duplicate_pointer);
                }
                stack.push(Container {
                    path,
                    key: None,
                    array_index: (token == b'[').then_some(0),
                    saw_field: false,
                });
            }
            _ => {
                if token == b'"' {
                    while cursor < bytes.len() {
                        let byte = bytes[cursor];
                        cursor += 1;
                        if byte == b'"' {
                            break;
                        }
                        if byte == b'\\' && cursor < bytes.len() {
                            cursor += 1;
                        }
                    }
                    if let Some(parent) = stack.last_mut()
                        && parent.array_index.is_none()
                        && parent.key.is_none()
                    {
                        parent.key = serde_json::from_slice(&bytes[start..cursor]).ok();
                        if duplicate_field.is_some() && parent.key.as_deref() == duplicate_field {
                            if parent.saw_field {
                                duplicate_pointer = Some(parent.child());
                            }
                            parent.saw_field = true;
                        }
                    }
                } else {
                    while cursor < bytes.len()
                        && !matches!(
                            bytes[cursor],
                            b',' | b'}' | b']' | b' ' | b'\t' | b'\n' | b'\r'
                        )
                    {
                        cursor += 1;
                    }
                }
                if at < cursor {
                    break;
                }
            }
        }
        if cursor > at {
            break;
        }
    }
    (
        stack.last().map_or_else(String::new, Container::child),
        duplicate_pointer,
    )
}

fn transform_error(value: &Value) -> Option<String> {
    TransformExprV52::deserialize(value)
        .err()
        .map(|err| err.to_string())
}

/// Locate errors hidden by serde's internally tagged enum buffering. Restrict successive
/// prefixes of the invalid expression using the actual derived schema as the validator.
/// Keeping the discriminator allows an "op" in any input position. This runs only on errors.
fn transform_member(node: &Value, description: &str) -> String {
    if let Some(missing) = missing_field(description) {
        // A missing key has no token. Look inside child expressions that produce the same
        // error before attributing it to the current variant.
        for (key, member) in node.as_object().into_iter().flatten() {
            let child = json_pointer_child("", key);
            match key.as_str() {
                "parts" | "values" => {
                    for (index, expr) in member.as_array().into_iter().flatten().enumerate() {
                        if transform_error(expr).as_deref() == Some(description) {
                            return format!(
                                "{child}/{index}{}",
                                transform_member(expr, description)
                            );
                        }
                    }
                }
                "input" | "left" | "right" | "if_true" | "if_false" | "default_value"
                    if transform_error(member).as_deref() == Some(description) =>
                {
                    return format!("{child}{}", transform_member(member, description));
                }
                _ => {}
            }
        }
        return json_pointer_child("", missing);
    }
    let mut expression = node.clone();
    let mut pointer = String::new();
    loop {
        let current = expression
            .pointer(&pointer)
            .expect("selected expression member");
        let (keys, empty) = match current {
            Value::Object(map) => {
                let mut empty = serde_json::Map::new();
                if let Some(op) = map.get("op") {
                    empty.insert("op".into(), op.clone());
                }
                let keys: Vec<_> = map
                    .keys()
                    .filter(|k| !empty.contains_key(*k))
                    .cloned()
                    .collect();
                (keys, Value::Object(empty))
            }
            Value::Array(array) => (
                (0..array.len()).map(|i| i.to_string()).collect(),
                Value::Array(Vec::new()),
            ),
            _ => return pointer,
        };
        // Identify the first prefix that produces the same error.
        let original = current.clone();
        let mut lo = 0;
        let mut hi = keys.len();
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let mut prefix = empty.clone();
            if let Some(map) = prefix.as_object_mut() {
                for key in &keys[..mid] {
                    map.insert(key.clone(), original[key].clone());
                }
            } else {
                prefix = Value::Array(original.as_array().unwrap()[..mid].to_vec());
            }
            *expression.pointer_mut(&pointer).unwrap() = prefix;
            if transform_error(&expression).as_deref() == Some(description) {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        *expression.pointer_mut(&pointer).unwrap() = original;
        if lo == 0 {
            if description.starts_with("unknown variant `")
                && node
                    .pointer(&pointer)
                    .is_some_and(|v| v.get("op").is_some())
            {
                return json_pointer_child(&pointer, "op");
            }
            return pointer;
        }
        pointer = json_pointer_child(&pointer, &keys[lo - 1]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ib_v52_schemas::{GoLivePlanConfigV52, TransformsConfigV52};
    use serde_json::json;

    #[test]
    fn points_into_structs_arrays_and_malformed_input() {
        use ib_v52_schemas::IdentityProfileConfigV52 as Profile;
        for (text, want) in [
            (
                r#"{"authoritative_sources":[{"source_id":"s","priority_rank":"bad"}]}"#,
                "/authoritative_sources/0/priority_rank",
            ),
            (
                r#"{"authoritative_sources":[{"source_id":"ok"}]}"#,
                "/authoritative_sources/0/priority_rank",
            ),
            (
                r#"{"authoritative_sources":[{"source_id": ]}"#,
                "/authoritative_sources/0/source_id",
            ),
            (
                r#"{"authoritative_sources":[{"source_id":"s","priority_rank":1,"bad/key":true}]}"#,
                "/authoritative_sources/0/bad~1key",
            ),
        ] {
            let (pointer, message) =
                deserialize_config_with_pointer::<Profile>(&json!(text)).unwrap_err();
            assert_eq!(pointer, want, "{message}");
        }
    }

    #[test]
    fn locates_members_in_raw_json_and_buffered_enum_errors() {
        for (value, want) in [
            (
                json!({"op":"lookup","input":{"op":"literal","value":"ok"}}),
                "/table",
            ),
            (json!({"value":"x"}), "/op"),
            (
                json!({"op":"concat","parts":[{"op":"literal","value":1}]}),
                "/parts/0/value",
            ),
            (
                json!({"op":"lookup","input":{"op":"literal","value":"x"},"table":{"a/b~":7}}),
                "/table/a~1b~0",
            ),
            (
                json!({"parts":[{"op":"bogus"}], "op":"concat"}),
                "/parts/0/op",
            ),
            (
                json!({"op":"trim","input":{"op":"literal"}}),
                "/input/value",
            ),
            (json!({"op":"literal","value":"x","a/b~":7}), "/a~1b~0"),
        ] {
            let config = json!({"schema_version":"v5.2","transforms":{"a/b~":value}});
            let expected = format!("/transforms/a~1b~0{want}");
            for input in [&config, &Value::String(config.to_string())] {
                let (pointer, msg) =
                    deserialize_config_with_pointer::<TransformsConfigV52>(input).unwrap_err();
                assert_eq!(pointer, expected, "{msg}");
            }
        }
        let duplicate =
            json!(r#"{"schema_version":"v5.2","schema_version":"v5.2","transforms":{}}"#);
        assert_eq!(
            deserialize_config_with_pointer::<TransformsConfigV52>(&duplicate)
                .unwrap_err()
                .0,
            "/schema_version"
        );
        let nested_duplicate = json!(
            r#"{"schema_version":"v5.2","transforms":{"a":{"op":"trim","input":{"op":"literal","value":"a","value":"b"}}}}"#
        );
        assert_eq!(
            deserialize_config_with_pointer::<TransformsConfigV52>(&nested_duplicate)
                .unwrap_err()
                .0,
            "/transforms/a/input/value"
        );
        let bad = json!({"schema_version":"v5.2","ordered_phases":["future"]});
        assert_eq!(
            deserialize_config_with_pointer::<GoLivePlanConfigV52>(&bad)
                .unwrap_err()
                .0,
            "/ordered_phases/0"
        );
        let missing = json!({"schema_version":"v5.2","transforms":{},"odd/field~":true});
        assert_eq!(
            deserialize_config_with_pointer::<TransformsConfigV52>(&missing)
                .unwrap_err()
                .0,
            "/odd~1field~0"
        );
    }
}
