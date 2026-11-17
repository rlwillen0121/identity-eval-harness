//! Markdown escaping for report text, including untrusted case data and model output.
//!
//! Ports `src/identitybench/report/markdown_text.py` verbatim.
//!
//! Justifications, reviewer notes, and rationales can carry attacks (SPEC §8.4), and a report
//! is read on GitHub and in editors that render Markdown and inline HTML. So text never reaches
//! the Markdown as markup:
//! - inline text has every character that could start inline Markdown or HTML syntax
//!   backslash-escaped (an underscore only at a word boundary, where it could open emphasis;
//!   inside a word, as in `always_escalate`, it is literal and left alone so tables stay
//!   readable as plain text);
//! - table cells are inline text on one line, with `|` escaped;
//! - verbatim text goes in a fenced code block whose fence is longer than any backtick run
//!   inside it, so the text can't close the block early.

use std::sync::LazyLock;

use regex::{Captures, Regex};

static BACKTICKS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"`+").expect("valid backticks regex"));
static AUTOLINK_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:https?|ftp)://|mailto:|www\.|@").expect("valid autolink regex")
});

/// Match Python's `str.isspace()` (Unicode White_Space plus ASCII 0x1c..=0x1f).
#[inline]
fn is_py_whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}')
}

/// Match Python 3 `re` `\w` (Unicode word character: alphanumeric or underscore, plus marks/connector).
#[inline]
fn is_py_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Fold runs of Python whitespace to single spaces (`" ".join(text.split())`).
fn collapse_py_whitespace(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_token = false;
    let mut first = true;
    for ch in text.chars() {
        if is_py_whitespace(ch) {
            in_token = false;
        } else {
            if !in_token && !first {
                out.push(' ');
            }
            out.push(ch);
            in_token = true;
            first = false;
        }
    }
    out
}

/// Replace `&(?=#?\w+;)` with `&amp;`.
fn escape_entities(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let ch = text[i..].chars().next().expect("valid char boundary");
        let ch_len = ch.len_utf8();
        if ch == '&' {
            let rest = &text[i + ch_len..];
            let mut rest_chars = rest.chars();
            let after_hash = if rest.starts_with('#') {
                rest_chars.next();
                rest_chars
            } else {
                rest_chars
            };
            let mut word_count = 0usize;
            let mut saw_semi = false;
            for rc in after_hash {
                if is_py_word(rc) {
                    word_count += 1;
                } else if rc == ';' {
                    saw_semi = true;
                    break;
                } else {
                    break;
                }
            }
            if word_count > 0 && saw_semi {
                out.push_str("&amp;");
            } else {
                out.push('&');
            }
        } else {
            out.push(ch);
        }
        i += ch_len;
    }
    out
}

/// Escape `[\\`*\[\]<>|~]` and `(?<!\w)_|_(?!\w)`.
fn escape_special_chars(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len() + 8);
    for (idx, &ch) in chars.iter().enumerate() {
        let is_special = matches!(ch, '\\' | '`' | '*' | '[' | ']' | '<' | '>' | '|' | '~')
            || (ch == '_'
                && (idx == 0
                    || !is_py_word(chars[idx - 1])
                    || idx + 1 >= chars.len()
                    || !is_py_word(chars[idx + 1])));
        if is_special {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

/// `text` as literal Markdown inline content: special characters escaped and line breaks folded
/// to spaces. An `&` that would start an entity becomes `&amp;`.
#[must_use]
pub fn escape_inline(text: &str) -> String {
    let flat = collapse_py_whitespace(text);
    let amp_escaped = escape_entities(&flat);
    let escaped = escape_special_chars(&amp_escaped);
    AUTOLINK_RE
        .replace_all(&escaped, |caps: &Captures<'_>| {
            caps[0]
                .replace(':', "&#58;")
                .replace('.', "&#46;")
                .replace('@', "&#64;")
        })
        .into_owned()
}

/// `text` as one table cell (inline escaping; `|` included).
#[must_use]
pub fn escape_cell(text: &str) -> String {
    let escaped = escape_inline(text);
    if escaped.is_empty() {
        " ".to_string()
    } else {
        escaped
    }
}

/// Literal inline code in a pipe table, using a delimiter the value cannot close.
#[must_use]
pub fn code_cell(text: &str) -> String {
    let flat = collapse_py_whitespace(text).replace('|', "\\|");
    let longest = BACKTICKS_RE
        .find_iter(&flat)
        .map(|m| m.as_str().len())
        .max()
        .unwrap_or(0);
    let delimiter = "`".repeat(longest + 1);
    let body = if longest > 0 {
        format!(" {flat} ")
    } else if flat.is_empty() {
        " ".to_string()
    } else {
        flat
    };
    format!("{delimiter}{body}{delimiter}")
}

/// `text` in a fenced code block that it cannot break out of.
#[must_use]
pub fn fenced(text: &str, info: &str) -> String {
    let longest = BACKTICKS_RE
        .find_iter(text)
        .map(|m| m.as_str().len())
        .max()
        .unwrap_or(0);
    let fence = "`".repeat(3.max(longest + 1));
    let normalized = text.replace("\r\n", "\n");
    let body = normalized.trim_end_matches('\n');
    format!("{fence}{info}\n{body}\n{fence}")
}

/// `text` in a fenced code block with default `info = "text"`.
#[must_use]
pub fn fenced_text(text: &str) -> String {
    fenced(text, "text")
}
