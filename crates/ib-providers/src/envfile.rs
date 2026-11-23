//! Minimal `.env` loading for the command line (SPEC §11.1, §11.4, §17.3).
//!
//! - **Never override.** A variable already set in the environment wins over the file.
//! - **Never print values.** Problems report line numbers and variable names only.
//! - **Never block a command.** `.env` is optional; malformed lines or unreadable files are skipped
//!   and reported as problems.

use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;

static KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*$").expect("valid key regex"));
static EXPORT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^export\s+").expect("valid export regex"));
static INLINE_COMMENT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?:^|\s)#").expect("valid inline comment regex"));

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvFile {
    pub values: BTreeMap<String, String>,
    pub problems: Vec<String>,
}

fn double_quoted(body: &str) -> Option<(String, &str)> {
    let mut out = String::new();
    let mut chars = body.char_indices();
    while let Some((idx, ch)) = chars.next() {
        if ch == '\\' {
            if let Some((_, nxt)) = chars.next() {
                match nxt {
                    'n' => out.push('\n'),
                    't' => out.push('\t'),
                    'r' => out.push('\r'),
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '$' => out.push('$'),
                    other => {
                        out.push('\\');
                        out.push(other);
                    }
                }
                continue;
            }
            return None;
        }
        if ch == '"' {
            return Some((out, &body[idx + 1..]));
        }
        out.push(ch);
    }
    None
}

fn parse_value(raw: &str) -> (Option<String>, Option<&'static str>) {
    let text = raw.trim();
    if let Some(after_sq) = text.strip_prefix('\'') {
        let Some(end) = after_sq.find('\'') else {
            return (
                None,
                Some("unterminated quote (multi-line values are not supported)"),
            );
        };
        let value = &after_sq[..end];
        let rest = after_sq[end + 1..].trim();
        if !rest.is_empty() && !rest.starts_with('#') {
            return (None, Some("text after the closing quote"));
        }
        return (Some(value.to_string()), None);
    }
    if let Some(after_dq) = text.strip_prefix('"') {
        let Some((value, rest)) = double_quoted(after_dq) else {
            return (
                None,
                Some("unterminated quote (multi-line values are not supported)"),
            );
        };
        let rest = rest.trim();
        if !rest.is_empty() && !rest.starts_with('#') {
            return (None, Some("text after the closing quote"));
        }
        return (Some(value), None);
    }

    let trimmed = if let Some(m) = INLINE_COMMENT_RE.find(text) {
        &text[..m.start()]
    } else {
        text
    }
    .trim();

    if trimmed.is_empty() {
        (None, None)
    } else {
        (Some(trimmed.to_string()), None)
    }
}

fn split_lines_py(text: &str) -> Vec<&str> {
    if text.is_empty() {
        return Vec::new();
    }
    let mut lines = Vec::new();
    let mut start = 0;
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'\r' {
            lines.push(&text[start..i]);
            if i + 1 < bytes.len() && bytes[i + 1] == b'\n' {
                i += 2;
            } else {
                i += 1;
            }
            start = i;
        } else if bytes[i] == b'\n' {
            lines.push(&text[start..i]);
            i += 1;
            start = i;
        } else {
            i += 1;
        }
    }
    if start < bytes.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Parse `.env` text. Later assignments of a name win, as in a shell (an empty one unsets it).
#[must_use]
pub fn parse_env(text: &str) -> EnvFile {
    let mut values = BTreeMap::new();
    let mut problems = Vec::new();

    for (idx, line) in split_lines_py(text).into_iter().enumerate() {
        let number = idx + 1;
        let mut stripped = line.trim();
        if stripped.is_empty() || stripped.starts_with('#') {
            continue;
        }
        if let Some(m) = EXPORT_RE.find(stripped) {
            stripped = &stripped[m.end()..];
        }
        let Some((key_raw, raw)) = stripped.split_once('=') else {
            problems.push(format!("line {number}: expected KEY=VALUE; line skipped"));
            continue;
        };
        let key = key_raw.trim();
        if !KEY_RE.is_match(key) {
            problems.push(format!(
                "line {number}: invalid variable name; line skipped"
            ));
            continue;
        }
        let (value, problem) = parse_value(raw);
        if let Some(prob) = problem {
            problems.push(format!("line {number} ({key}): {prob}; line skipped"));
        } else if let Some(val) = value {
            values.insert(key.to_string(), val);
        } else {
            values.remove(key);
        }
    }

    EnvFile { values, problems }
}

/// Load `.env` from `path` into `environ` for keys not already present.
/// Returns `(sorted_loaded_keys, problems)`.
///
/// The caller passes an explicit map (the process environment plus the file) rather than mutating
/// `std::env`: `std::env::set_var` is `unsafe` in edition 2024 and the workspace forbids
/// `unsafe_code`.
pub fn load_env_file_into(
    path: impl AsRef<Path>,
    environ: &mut BTreeMap<String, String>,
) -> (Vec<String>, Vec<String>) {
    let file = path.as_ref();
    if !file.is_file() {
        return (Vec::new(), Vec::new());
    }
    let bytes = match std::fs::read(file) {
        Ok(b) => b,
        Err(err) => {
            let cls = match err.kind() {
                ErrorKind::PermissionDenied => "PermissionError",
                ErrorKind::NotFound => "FileNotFoundError",
                ErrorKind::IsADirectory => "IsADirectoryError",
                _ => "OSError",
            };
            return (
                Vec::new(),
                vec![format!("unreadable ({cls}); file skipped")],
            );
        }
    };
    let text = match std::str::from_utf8(&bytes) {
        Ok(s) => s.strip_prefix('\u{feff}').unwrap_or(s),
        Err(_) => {
            return (
                Vec::new(),
                vec!["not valid UTF-8 text; file skipped".to_string()],
            );
        }
    };
    let parsed = parse_env(text);
    let mut loaded = Vec::new();
    for (key, value) in parsed.values {
        if !environ.contains_key(&key) {
            environ.insert(key.clone(), value);
            loaded.push(key);
        }
    }
    loaded.sort();
    (loaded, parsed.problems)
}
