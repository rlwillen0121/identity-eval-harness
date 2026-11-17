//! Strict UTF-8 JSON parsing (duplicate-key rejection), scalar checks, and safe artifact paths.

use std::fmt;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use ib_core::pyjson::{PyNumber, classify_number};
use regex::Regex;
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};

use crate::error::ReportError;

static SHA256_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-f0-9]{64}\z").expect("valid sha256 regex"));

struct UniqueJsonSeed;

impl<'de> DeserializeSeed<'de> for UniqueJsonSeed {
    type Value = Value;

    fn deserialize<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueJsonVisitor)
    }
}

struct UniqueJsonVisitor;

impl<'de> Visitor<'de> for UniqueJsonVisitor {
    type Value = Value;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("any valid JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Self::Value, E> {
        Ok(Value::Bool(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Self::Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Self::Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_f64<E>(self, v: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite float in JSON"))
    }

    fn visit_str<E>(self, v: &str) -> Result<Self::Value, E> {
        Ok(Value::String(v.to_owned()))
    }

    fn visit_string<E>(self, v: String) -> Result<Self::Value, E> {
        Ok(Value::String(v))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(Value::Null)
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(Value::Null)
    }

    fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        UniqueJsonSeed.deserialize(deserializer)
    }

    fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut items = Vec::new();
        while let Some(elem) = seq.next_element_seed(UniqueJsonSeed)? {
            items.push(elem);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let Some(first_key) = map.next_key::<String>()? else {
            return Ok(Value::Object(Map::new()));
        };
        // With `serde_json`'s `arbitrary_precision` feature enabled, numbers arrive via
        // `visit_map` with the internal marker key `"$serde_json::private::Number"`.
        if first_key == "$serde_json::private::Number" {
            let text: String = map.next_value()?;
            let number: Number = text
                .parse()
                .map_err(|err| de::Error::custom(format!("invalid JSON number: {err}")))?;
            return Ok(Value::Number(number));
        }

        let mut out = Map::new();
        let first_val = map.next_value_seed(UniqueJsonSeed)?;
        out.insert(first_key, first_val);

        while let Some(key) = map.next_key::<String>()? {
            if out.contains_key(&key) {
                return Err(de::Error::custom(format!(
                    "duplicate JSON object key '{key}'"
                )));
            }
            let val = map.next_value_seed(UniqueJsonSeed)?;
            out.insert(key, val);
        }
        Ok(Value::Object(out))
    }
}

/// Parse UTF-8 JSON while rejecting duplicate object keys at any depth.
pub fn parse_json_strict(raw: &[u8], label: &str) -> Result<Value, ReportError> {
    let text = std::str::from_utf8(raw)
        .map_err(|_| ReportError::value(format!("{label} is not valid UTF-8 JSON")))?;
    let mut de = serde_json::Deserializer::from_str(text);
    let value = UniqueJsonSeed.deserialize(&mut de).map_err(|err| {
        let msg = err.to_string();
        if msg.contains("duplicate JSON object key") {
            ReportError::value(msg)
        } else {
            ReportError::value(format!("{label} is not valid UTF-8 JSON"))
        }
    })?;
    de.end()
        .map_err(|_| ReportError::value(format!("{label} is not valid UTF-8 JSON")))?;
    Ok(value)
}

#[must_use]
pub fn is_sha256_str(s: &str) -> bool {
    SHA256_RE.is_match(s)
}

#[must_use]
pub fn is_sha256_value(v: &Value) -> bool {
    v.as_str().is_some_and(is_sha256_str)
}

/// Extract a strict JSON integer (rejecting booleans and floats like `1.0`).
#[must_use]
pub fn as_strict_i64(v: &Value) -> Option<i64> {
    let Value::Number(n) = v else {
        return None;
    };
    match classify_number(n) {
        PyNumber::Int(text) => text.parse::<i64>().ok(),
        PyNumber::Float(_) => None,
    }
}

/// Resolve `relative_name` safely beneath `root`, rejecting absolute paths, backslashes, `..`,
/// and symlink escapes (while allowing non-existent leaf paths so callers can treat `NotFound`
/// as optional `None`).
pub fn safe_artifact_file(
    root: &Path,
    relative_name: &str,
    label: &str,
) -> Result<PathBuf, ReportError> {
    if relative_name.starts_with('/')
        || relative_name.contains('\\')
        || relative_name.contains('\0')
    {
        return Err(ReportError::value(format!(
            "{label} must be a safe relative path"
        )));
    }
    let parts: Vec<&str> = relative_name.split('/').filter(|p| !p.is_empty()).collect();
    if parts.contains(&"..") {
        return Err(ReportError::value(format!(
            "{label} must be a safe relative path"
        )));
    }

    let resolved_root = std::fs::canonicalize(root).map_err(|e| {
        ReportError::value(format!("cannot resolve artifact root {}: {e}", root.display()))
    })?;

    let mut candidate = resolved_root.clone();
    for part in &parts {
        if *part != "." {
            candidate.push(part);
        }
    }

    if std::fs::symlink_metadata(&candidate).is_ok() {
        let resolved = std::fs::canonicalize(&candidate).map_err(|e| {
            ReportError::value(format!("cannot resolve {label}: {e}"))
        })?;
        if !resolved.starts_with(&resolved_root) {
            return Err(ReportError::value(format!(
                "{label} escapes its artifact root"
            )));
        }
        return Ok(resolved);
    }

    // Target does not exist; check deepest existing ancestor for symlink escape.
    let mut ancestor = candidate.as_path();
    while let Some(parent) = ancestor.parent() {
        if std::fs::symlink_metadata(parent).is_ok() {
            let resolved_parent = std::fs::canonicalize(parent).map_err(|e| {
                ReportError::value(format!("cannot resolve {label}: {e}"))
            })?;
            if !resolved_parent.starts_with(&resolved_root) {
                return Err(ReportError::value(format!(
                    "{label} escapes its artifact root"
                )));
            }
            break;
        }
        ancestor = parent;
    }

    if !candidate.starts_with(&resolved_root) {
        return Err(ReportError::value(format!(
            "{label} escapes its artifact root"
        )));
    }
    Ok(candidate)
}

/// Read file bytes, returning `Ok(None)` when missing (`NotFound`), or `Err` on other I/O errors.
pub fn read_bytes_optional(path: &Path, err_label: &str) -> Result<Option<Vec<u8>>, ReportError> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(None),
        Err(err) => Err(ReportError::value(format!("{err_label}: {err}"))),
    }
}
