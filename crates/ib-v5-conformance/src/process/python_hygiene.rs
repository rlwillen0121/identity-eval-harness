//! Deterministic Python lexical/import/hygiene scanner for conformance worker files.
//!
//! Enforces static import, path-reference, and isolation hygiene over Python worker
//! closures without invoking a Python subprocess.

use std::collections::BTreeSet;
use std::path::Path;

use super::SharedRuntimeError;
use super::profiles::{FORBIDDEN_PARTS, FORBIDDEN_ROOTS, SourceProfile, validate_relative_source};

const BANNED_MODULE_PREFIXES_FOR_BLIND: [&str; 13] = [
    "identitybench.v5.oracle",
    "identitybench.v5.oracle_p2",
    "identitybench.v5.oracle_requests",
    "identitybench.v5.generator",
    "identitybench.v5.verifier",
    "identitybench.v5.baselines",
    "identitybench.v5.scoring",
    "identitybench.v5.runner",
    "identitybench.v5.runtime",
    "identitybench.v5.report",
    "identitybench.v5.validation",
    "identitybench.v5.family_couplings",
    "identitybench.v5.negative_proofs",
];

const BANNED_AGENT_VIEW_NORMATIVE_MODULES: [&str; 2] = [
    "identitybench.v5.normative_contract",
    "identitybench.v5.scope_algebra",
];

const NORMATIVE_SPEC_FILES: [&str; 4] = [
    "SPEC-v5.md",
    "SPEC-v5-ADDENDUM-01.md",
    "SPEC-v5-DIRECTIVE-01.md",
    "SPEC-v5.0-draft.5.md",
];

const BANNED_SIDE_EFFECT_MODULES_FOR_BLIND: [&str; 5] =
    ["subprocess", "socket", "urllib", "requests", "http.client"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonScanReport {
    pub imported_modules: BTreeSet<String>,
    pub string_literals: Vec<String>,
    pub dynamic_import_calls: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    Ident(String),
    Dot,
    Comma,
    Star,
    Semi,
    Newline,
    LParen,
    RParen,
    LBracket,
    RBracket,
    LBrace,
    RBrace,
    Str(String),
    Other(char),
}

/// Tokenize Python source into logical tokens, stripping comments and tracking string literals.
fn tokenize_python(source: &str) -> Vec<Token> {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut tokens = Vec::new();
    let mut i = 0;
    let mut paren_depth: usize = 0;

    while i < len {
        let b = bytes[i];
        // Whitespace
        if b == b' ' || b == b'\t' || b == b'\x0c' {
            i += 1;
            continue;
        }
        // Line continuation
        if b == b'\\' {
            if i + 1 < len && bytes[i + 1] == b'\n' {
                i += 2;
                continue;
            }
            if i + 2 < len && bytes[i + 1] == b'\r' && bytes[i + 2] == b'\n' {
                i += 3;
                continue;
            }
        }
        // Newline
        if b == b'\r' || b == b'\n' {
            if b == b'\r' && i + 1 < len && bytes[i + 1] == b'\n' {
                i += 2;
            } else {
                i += 1;
            }
            if paren_depth == 0 {
                tokens.push(Token::Newline);
            }
            continue;
        }
        // Comment
        if b == b'#' {
            while i < len && bytes[i] != b'\n' && bytes[i] != b'\r' {
                i += 1;
            }
            continue;
        }
        // Check string literal prefixes (e.g. r"...", f"...", b"...", rf"...", etc.)
        if let Some((prefix_len, is_raw)) = string_prefix(bytes, i) {
            let q_start = i + prefix_len;
            let quote = bytes[q_start];
            let triple =
                q_start + 2 < len && bytes[q_start + 1] == quote && bytes[q_start + 2] == quote;
            let content_start = q_start + if triple { 3 } else { 1 };
            let mut j = content_start;
            let mut lit = String::new();
            while j < len {
                let ch = bytes[j];
                if !is_raw && ch == b'\\' && j + 1 < len {
                    let next = bytes[j + 1];
                    match next {
                        b'n' => lit.push('\n'),
                        b'r' => lit.push('\r'),
                        b't' => lit.push('\t'),
                        b'\\' => lit.push('\\'),
                        b'\'' => lit.push('\''),
                        b'"' => lit.push('"'),
                        _ => {
                            lit.push('\\');
                            lit.push(next as char);
                        }
                    }
                    j += 2;
                    continue;
                }
                if triple {
                    if j + 2 < len
                        && bytes[j] == quote
                        && bytes[j + 1] == quote
                        && bytes[j + 2] == quote
                    {
                        j += 3;
                        break;
                    }
                } else if ch == quote || (!triple && (ch == b'\n' || ch == b'\r')) {
                    if ch == quote {
                        j += 1;
                    }
                    break;
                }
                if ch < 0x80 {
                    lit.push(ch as char);
                    j += 1;
                } else if let Some(s) = std::str::from_utf8(&bytes[j..])
                    .ok()
                    .and_then(|s| s.chars().next())
                {
                    lit.push(s);
                    j += s.len_utf8();
                } else {
                    j += 1;
                }
            }
            tokens.push(Token::Str(lit));
            i = j;
            continue;
        }

        // Identifiers
        if b.is_ascii_alphabetic() || b == b'_' {
            let start = i;
            i += 1;
            while i < len && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            let ident = String::from_utf8_lossy(&bytes[start..i]).into_owned();
            tokens.push(Token::Ident(ident));
            continue;
        }

        match b {
            b'.' => {
                tokens.push(Token::Dot);
                i += 1;
            }
            b',' => {
                tokens.push(Token::Comma);
                i += 1;
            }
            b'*' => {
                tokens.push(Token::Star);
                i += 1;
            }
            b';' => {
                tokens.push(Token::Semi);
                i += 1;
            }
            b'(' => {
                paren_depth += 1;
                tokens.push(Token::LParen);
                i += 1;
            }
            b')' => {
                paren_depth = paren_depth.saturating_sub(1);
                tokens.push(Token::RParen);
                i += 1;
            }
            b'[' => {
                paren_depth += 1;
                tokens.push(Token::LBracket);
                i += 1;
            }
            b']' => {
                paren_depth = paren_depth.saturating_sub(1);
                tokens.push(Token::RBracket);
                i += 1;
            }
            b'{' => {
                paren_depth += 1;
                tokens.push(Token::LBrace);
                i += 1;
            }
            b'}' => {
                paren_depth = paren_depth.saturating_sub(1);
                tokens.push(Token::RBrace);
                i += 1;
            }
            _ => {
                if b < 0x80 {
                    tokens.push(Token::Other(b as char));
                    i += 1;
                } else if let Some(ch) = std::str::from_utf8(&bytes[i..])
                    .ok()
                    .and_then(|s| s.chars().next())
                {
                    tokens.push(Token::Other(ch));
                    i += ch.len_utf8();
                } else {
                    i += 1;
                }
            }
        }
    }
    tokens
}

fn string_prefix(bytes: &[u8], i: usize) -> Option<(usize, bool)> {
    let b0 = bytes[i];
    if b0 == b'"' || b0 == b'\'' {
        return Some((0, false));
    }
    let is_prefix_char = |c: u8| matches!(c.to_ascii_lowercase(), b'r' | b'u' | b'b' | b'f');
    if i + 1 < bytes.len() && is_prefix_char(b0) && (bytes[i + 1] == b'"' || bytes[i + 1] == b'\'')
    {
        return Some((1, b0.eq_ignore_ascii_case(&b'r')));
    }
    if i + 2 < bytes.len()
        && is_prefix_char(b0)
        && is_prefix_char(bytes[i + 1])
        && (bytes[i + 2] == b'"' || bytes[i + 2] == b'\'')
    {
        let raw = b0.eq_ignore_ascii_case(&b'r') || bytes[i + 1].eq_ignore_ascii_case(&b'r');
        return Some((2, raw));
    }
    None
}

#[must_use]
pub fn scan_python_source(source: &str) -> PythonScanReport {
    let tokens = tokenize_python(source);
    let mut imported_modules = BTreeSet::new();
    let mut string_literals = Vec::new();
    let mut dynamic_import_calls = Vec::new();

    for (idx, tok) in tokens.iter().enumerate() {
        if let Token::Str(s) = tok {
            string_literals.push(s.clone());
        } else if let Token::Ident(name) = tok {
            if name == "__import__" && matches!(tokens.get(idx + 1), Some(Token::LParen)) {
                dynamic_import_calls.push("__import__".to_owned());
            } else if name == "import_module" && matches!(tokens.get(idx + 1), Some(Token::LParen))
            {
                dynamic_import_calls.push("import_module".to_owned());
            }
        }
    }

    // Split into logical statements on Newline / Semi
    let mut start = 0;
    while start < tokens.len() {
        let mut end = start;
        while end < tokens.len() && !matches!(tokens[end], Token::Newline | Token::Semi) {
            end += 1;
        }
        let stmt = &tokens[start..end];
        parse_import_statement(stmt, &mut imported_modules);
        start = end + 1;
    }

    PythonScanReport {
        imported_modules,
        string_literals,
        dynamic_import_calls,
    }
}

fn parse_import_statement(stmt: &[Token], out: &mut BTreeSet<String>) {
    if stmt.is_empty() {
        return;
    }
    match &stmt[0] {
        Token::Ident(kw) if kw == "import" => {
            // import a.b [as x], c.d [as y]
            let mut idx = 1;
            while idx < stmt.len() {
                let mut parts = Vec::new();
                while idx < stmt.len() {
                    match &stmt[idx] {
                        Token::Ident(id) => {
                            parts.push(id.clone());
                            idx += 1;
                        }
                        Token::Dot => {
                            idx += 1;
                        }
                        _ => break,
                    }
                }
                if !parts.is_empty() {
                    out.insert(parts.join("."));
                }
                // Skip optional `as alias`
                if matches!(stmt.get(idx), Some(Token::Ident(s)) if s == "as") {
                    idx += 2;
                }
                if matches!(stmt.get(idx), Some(Token::Comma)) {
                    idx += 1;
                } else {
                    break;
                }
            }
        }
        Token::Ident(kw) if kw == "from" => {
            // from [.^]pkg.sub import a, b
            let mut idx = 1;
            let mut leading_dots = 0;
            while matches!(stmt.get(idx), Some(Token::Dot)) {
                leading_dots += 1;
                idx += 1;
            }
            let mut base_parts = Vec::new();
            while idx < stmt.len() {
                match &stmt[idx] {
                    Token::Ident(s) if s == "import" => break,
                    Token::Ident(id) => {
                        base_parts.push(id.clone());
                        idx += 1;
                    }
                    Token::Dot => {
                        idx += 1;
                    }
                    _ => break,
                }
            }
            if !matches!(stmt.get(idx), Some(Token::Ident(s)) if s == "import") {
                return;
            }
            idx += 1;
            let base = if leading_dots > 0 {
                format!("{}{}", ".".repeat(leading_dots), base_parts.join("."))
            } else {
                base_parts.join(".")
            };
            if !base.is_empty() {
                out.insert(base.clone());
            }
            // Also record `base.item` for each imported symbol in case `from identitybench.v5 import oracle`
            if matches!(stmt.get(idx), Some(Token::LParen)) {
                idx += 1;
            }
            while idx < stmt.len() {
                match &stmt[idx] {
                    Token::RParen => break,
                    Token::Star => {
                        idx += 1;
                    }
                    Token::Ident(sym) => {
                        if !base.is_empty() && !base.starts_with('.') {
                            out.insert(format!("{base}.{sym}"));
                        }
                        idx += 1;
                        if matches!(stmt.get(idx), Some(Token::Ident(s)) if s == "as") {
                            idx += 2;
                        }
                    }
                    Token::Comma => {
                        idx += 1;
                    }
                    _ => {
                        idx += 1;
                    }
                }
            }
        }
        _ => {}
    }
}

fn module_matches_prefix(module: &str, prefix: &str) -> bool {
    module == prefix
        || module
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.starts_with('.'))
}

pub fn check_python_worker_hygiene(
    source: &str,
    relative_path: &str,
    evaluator_id: &str,
) -> Result<PythonScanReport, SharedRuntimeError> {
    validate_relative_source(relative_path, Some(evaluator_id))?;
    let report = scan_python_source(source);

    if evaluator_id != "production" {
        for mod_name in &report.imported_modules {
            for banned in BANNED_MODULE_PREFIXES_FOR_BLIND {
                if module_matches_prefix(mod_name, banned) {
                    return Err(SharedRuntimeError(format!(
                        "forbidden module import {mod_name:?} in {evaluator_id} file {relative_path}"
                    )));
                }
            }
            for banned_se in BANNED_SIDE_EFFECT_MODULES_FOR_BLIND {
                if module_matches_prefix(mod_name, banned_se) {
                    return Err(SharedRuntimeError(format!(
                        "forbidden side-effect import {mod_name:?} in {evaluator_id} file {relative_path}"
                    )));
                }
            }
            if evaluator_id == "agent_view" {
                for banned_av in BANNED_AGENT_VIEW_NORMATIVE_MODULES {
                    if module_matches_prefix(mod_name, banned_av) {
                        return Err(SharedRuntimeError(format!(
                            "agent_view worker cannot import normative module {mod_name:?} in {relative_path}"
                        )));
                    }
                }
            }
        }

        for lit in &report.string_literals {
            for part in FORBIDDEN_PARTS {
                if lit == part || lit.ends_with(&format!("/{part}")) {
                    return Err(SharedRuntimeError(format!(
                        "forbidden benchmark path literal {lit:?} in {evaluator_id} file {relative_path}"
                    )));
                }
            }
            for root in FORBIDDEN_ROOTS {
                if (lit.starts_with(root) || lit.contains(&format!("/{root}")))
                    && !(root == "datasets/" && lit.ends_with("/episodes.jsonl"))
                    && !(root == "reports/" && lit.starts_with("reports/v5/conformance/inputs/"))
                {
                    return Err(SharedRuntimeError(format!(
                        "forbidden data root literal {lit:?} in {evaluator_id} file {relative_path}"
                    )));
                }
            }
            for banned_mod in BANNED_MODULE_PREFIXES_FOR_BLIND {
                if lit == banned_mod {
                    return Err(SharedRuntimeError(format!(
                        "forbidden dynamic module string {lit:?} in {evaluator_id} file {relative_path}"
                    )));
                }
            }
            if evaluator_id == "agent_view" {
                for spec in NORMATIVE_SPEC_FILES {
                    if lit == spec || lit.ends_with(&format!("/{spec}")) {
                        return Err(SharedRuntimeError(format!(
                            "agent_view worker cannot reference normative spec {lit:?} in {relative_path}"
                        )));
                    }
                }
            }
        }
    }

    Ok(report)
}

pub fn audit_profile_python_hygiene(
    repository_root: &Path,
    profile: &SourceProfile,
) -> Result<(), SharedRuntimeError> {
    for relative in &profile.source_files {
        validate_relative_source(relative, Some(profile.evaluator_id))?;
        if !relative.ends_with(".py") {
            continue;
        }
        let path = repository_root.join(relative);
        let source = std::fs::read_to_string(&path)
            .map_err(|exc| SharedRuntimeError(format!("failed reading {relative}: {exc}")))?;
        check_python_worker_hygiene(&source, relative, profile.evaluator_id)?;
    }
    Ok(())
}
