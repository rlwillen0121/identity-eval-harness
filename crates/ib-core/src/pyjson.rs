//! Byte-exact re-implementations of the Python JSON encoders used by IdentityBench.
//!
//! * [`jcs_dumps`] mirrors `identitybench.v4.jcs.jcs_dumps`: RFC 8785 key ordering
//!   (UTF-16 code units) and compact separators, but **Python** number text
//!   (`repr(float)`, so `1.0` stays `1.0`; only `0.0`/`-0.0` collapse to `0`).
//! * [`dumps`] mirrors `json.dumps(obj, ensure_ascii=..., sort_keys=..., indent=...,
//!   separators=..., allow_nan=...)`.
//!
//! Values are `serde_json::Value` built with `arbitrary_precision`, so integers of any
//! size keep their exact text. A number is a Python `float` iff its text contains `.`,
//! `e`, or `E` (true for both CPython `repr` output and `ryu` output from Rust `f64`).

use std::cmp::Ordering;
use std::fmt::Write as _;

use serde::Serialize;
use serde_json::{Map, Number, Value};

use crate::pyfloat;

/// Errors raised where CPython would raise `ValueError` / `TypeError`.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum JsonError {
    #[error("Out of range float values are not JSON compliant: {0}")]
    NonFinite(String),
    #[error("RFC 8785 JCS forbids NaN and Infinity float values")]
    JcsNonFinite,
    #[error("value is not serializable: {0}")]
    Serialize(String),
}

/// A JSON number classified the way CPython would have parsed it.
#[derive(Debug, Clone, PartialEq)]
pub enum PyNumber {
    /// Exact integer text (arbitrary size), normalised (`-0` becomes `0`).
    Int(String),
    /// IEEE-754 double.
    Float(f64),
}

/// Classify a `serde_json::Number` as a Python `int` or `float`.
#[must_use]
pub fn classify_number(n: &Number) -> PyNumber {
    let text = n.as_str();
    if text.contains(['.', 'e', 'E']) {
        let parsed = text.parse::<f64>().unwrap_or(f64::NAN);
        PyNumber::Float(parsed)
    } else if text == "-0" {
        PyNumber::Int("0".to_string())
    } else {
        PyNumber::Int(text.to_string())
    }
}

/// Encode `s` exactly like `json.encoder.py_encode_basestring(_ascii)`.
pub fn write_escaped_str(out: &mut String, s: &str, ensure_ascii: bool) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.dumps(s, ensure_ascii=...)` for a single string.
#[must_use]
pub fn escape_str(s: &str, ensure_ascii: bool) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    write_escaped_str(&mut out, s, ensure_ascii);
    out
}

fn utf16_cmp(a: &str, b: &str) -> Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

// ---------------------------------------------------------------------------------------------
// JCS (identitybench.v4.jcs)
// ---------------------------------------------------------------------------------------------

fn jcs_write(out: &mut String, v: &Value) -> Result<(), JsonError> {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match classify_number(n) {
            PyNumber::Int(text) => out.push_str(&text),
            PyNumber::Float(f) => {
                if !f.is_finite() {
                    return Err(JsonError::JcsNonFinite);
                }
                if f == 0.0 {
                    out.push('0');
                } else {
                    out.push_str(&pyfloat::repr(f));
                }
            }
        },
        Value::String(s) => write_escaped_str(out, s, false),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                jcs_write(out, item)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|a, b| utf16_cmp(a.0, b.0));
            out.push('{');
            for (i, (k, val)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_escaped_str(out, k, false);
                out.push(':');
                jcs_write(out, val)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// `identitybench.v4.jcs.jcs_dumps(value).decode()`.
///
/// # Errors
/// Returns [`JsonError::JcsNonFinite`] for NaN/Infinity floats.
pub fn jcs_string(v: &Value) -> Result<String, JsonError> {
    let mut out = String::new();
    jcs_write(&mut out, v)?;
    Ok(out)
}

/// `identitybench.v4.jcs.jcs_dumps(value)` as UTF-8 bytes.
///
/// # Errors
/// Returns [`JsonError::JcsNonFinite`] for NaN/Infinity floats.
pub fn jcs_dumps(v: &Value) -> Result<Vec<u8>, JsonError> {
    jcs_string(v).map(String::into_bytes)
}

/// Serialize any `Serialize` value to a `Value` and then to JCS bytes.
///
/// # Errors
/// Fails if serde serialization fails or a float is non-finite.
pub fn jcs_dumps_ser<T: Serialize + ?Sized>(v: &T) -> Result<Vec<u8>, JsonError> {
    let value = serde_json::to_value(v).map_err(|e| JsonError::Serialize(e.to_string()))?;
    jcs_dumps(&value)
}

// ---------------------------------------------------------------------------------------------
// json.dumps
// ---------------------------------------------------------------------------------------------

/// Options mirroring `json.dumps` keyword arguments.
#[derive(Debug, Clone)]
pub struct DumpOptions {
    pub ensure_ascii: bool,
    pub sort_keys: bool,
    /// `None` = single line; `Some(" ".repeat(n))` for `indent=n`.
    pub indent: Option<String>,
    /// `(item_separator, key_separator)`; `None` = Python defaults for the indent mode.
    pub separators: Option<(String, String)>,
    pub allow_nan: bool,
}

impl Default for DumpOptions {
    fn default() -> Self {
        Self { ensure_ascii: true, sort_keys: false, indent: None, separators: None, allow_nan: true }
    }
}

impl DumpOptions {
    /// `json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`.
    #[must_use]
    pub fn compact_sorted() -> Self {
        Self {
            ensure_ascii: false,
            sort_keys: true,
            indent: None,
            separators: Some((",".into(), ":".into())),
            allow_nan: false,
        }
    }

    /// `json.dumps(x, ensure_ascii=False, sort_keys=True, indent=2)`.
    #[must_use]
    pub fn pretty_sorted() -> Self {
        Self {
            ensure_ascii: false,
            sort_keys: true,
            indent: Some("  ".into()),
            separators: None,
            allow_nan: true,
        }
    }

    /// Builder: set `indent=n`.
    #[must_use]
    pub fn with_indent(mut self, n: usize) -> Self {
        self.indent = Some(" ".repeat(n));
        self
    }

    /// Builder: set explicit separators.
    #[must_use]
    pub fn with_separators(mut self, item: &str, key: &str) -> Self {
        self.separators = Some((item.into(), key.into()));
        self
    }

    /// Builder: set `ensure_ascii`.
    #[must_use]
    pub fn with_ensure_ascii(mut self, ensure_ascii: bool) -> Self {
        self.ensure_ascii = ensure_ascii;
        self
    }

    /// Builder: set `sort_keys`.
    #[must_use]
    pub fn with_sort_keys(mut self, sort_keys: bool) -> Self {
        self.sort_keys = sort_keys;
        self
    }

    /// Builder: set `allow_nan`.
    #[must_use]
    pub fn with_allow_nan(mut self, allow_nan: bool) -> Self {
        self.allow_nan = allow_nan;
        self
    }
}

struct Encoder<'a> {
    opts: &'a DumpOptions,
    item_sep: &'a str,
    key_sep: &'a str,
}

impl Encoder<'_> {
    fn float(&self, out: &mut String, f: f64) -> Result<(), JsonError> {
        if f.is_finite() {
            out.push_str(&pyfloat::repr(f));
            return Ok(());
        }
        let text = if f.is_nan() {
            "NaN"
        } else if f > 0.0 {
            "Infinity"
        } else {
            "-Infinity"
        };
        if !self.opts.allow_nan {
            return Err(JsonError::NonFinite(pyfloat::repr(f)));
        }
        out.push_str(text);
        Ok(())
    }

    fn newline(&self, out: &mut String, level: usize) {
        if let Some(indent) = &self.opts.indent {
            out.push('\n');
            for _ in 0..level {
                out.push_str(indent);
            }
        }
    }

    fn write(&self, out: &mut String, v: &Value, level: usize) -> Result<(), JsonError> {
        match v {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => match classify_number(n) {
                PyNumber::Int(text) => out.push_str(&text),
                PyNumber::Float(f) => self.float(out, f)?,
            },
            Value::String(s) => write_escaped_str(out, s, self.opts.ensure_ascii),
            Value::Array(items) => {
                if items.is_empty() {
                    out.push_str("[]");
                    return Ok(());
                }
                out.push('[');
                self.newline(out, level + 1);
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(self.item_sep);
                        self.newline(out, level + 1);
                    }
                    self.write(out, item, level + 1)?;
                }
                self.newline(out, level);
                out.push(']');
            }
            Value::Object(map) => {
                if map.is_empty() {
                    out.push_str("{}");
                    return Ok(());
                }
                let mut entries: Vec<(&String, &Value)> = map.iter().collect();
                if self.opts.sort_keys {
                    entries.sort_by(|a, b| a.0.cmp(b.0));
                }
                out.push('{');
                self.newline(out, level + 1);
                for (i, (k, val)) in entries.into_iter().enumerate() {
                    if i > 0 {
                        out.push_str(self.item_sep);
                        self.newline(out, level + 1);
                    }
                    write_escaped_str(out, k, self.opts.ensure_ascii);
                    out.push_str(self.key_sep);
                    self.write(out, val, level + 1)?;
                }
                self.newline(out, level);
                out.push('}');
            }
        }
        Ok(())
    }
}

/// `json.dumps(value, **opts)`.
///
/// # Errors
/// Fails on non-finite floats when `allow_nan` is false.
pub fn dumps(v: &Value, opts: &DumpOptions) -> Result<String, JsonError> {
    let (item_sep, key_sep) = match &opts.separators {
        Some((i, k)) => (i.as_str(), k.as_str()),
        None if opts.indent.is_some() => (",", ": "),
        None => (", ", ": "),
    };
    let enc = Encoder { opts, item_sep, key_sep };
    let mut out = String::new();
    enc.write(&mut out, v, 0)?;
    Ok(out)
}

/// Serialize a `Serialize` value and run [`dumps`].
///
/// # Errors
/// Fails if serde serialization fails or a non-finite float is rejected.
pub fn dumps_ser<T: Serialize + ?Sized>(v: &T, opts: &DumpOptions) -> Result<String, JsonError> {
    let value = serde_json::to_value(v).map_err(|e| JsonError::Serialize(e.to_string()))?;
    dumps(&value, opts)
}

/// Build a JSON number `Value` holding a Python float (keeps `1.0` as a float).
#[must_use]
pub fn float_value(f: f64) -> Value {
    if f.is_finite() {
        let text = pyfloat::repr(f);
        serde_json::from_str::<Number>(&text).map_or(Value::Null, Value::Number)
    } else {
        Value::Null
    }
}

/// Sort a JSON object map by key (Python `sort_keys=True` / code point order).
#[must_use]
pub fn sorted_map(map: &Map<String, Value>) -> Map<String, Value> {
    let mut entries: Vec<(&String, &Value)> = map.iter().collect();
    entries.sort_by(|a, b| a.0.cmp(b.0));
    entries.into_iter().map(|(k, v)| (k.clone(), v.clone())).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn jcs_basics() {
        let v: Value = serde_json::from_str(r#"{"b":1.0,"a":[0.0,-0.0,1e-05,"é\n"],"c":null}"#)
            .unwrap();
        assert_eq!(jcs_string(&v).unwrap(), r#"{"a":[0,0,1e-05,"é\n"],"b":1.0,"c":null}"#);
    }

    #[test]
    fn jcs_big_ints() {
        let v: Value = serde_json::from_str("[17565442252895372630, 123456789012345678901234567890]")
            .unwrap();
        assert_eq!(jcs_string(&v).unwrap(), "[17565442252895372630,123456789012345678901234567890]");
    }

    #[test]
    fn dumps_defaults_match_python() {
        let v = json!({"x": [1, 2.5, "é"], "a": {}});
        let want = format!(r#"{{"x": [1, 2.5, "{}u00e9"], "a": {{}}}}"#, '\\');
        assert_eq!(dumps(&v, &DumpOptions::default()).unwrap(), want);
        assert_eq!(
            dumps(&v, &DumpOptions::pretty_sorted()).unwrap(),
            "{\n  \"a\": {},\n  \"x\": [\n    1,\n    2.5,\n    \"é\"\n  ]\n}"
        );
    }

    #[test]
    fn float_value_keeps_float() {
        assert_eq!(jcs_string(&float_value(2.0)).unwrap(), "2.0");
    }
}
