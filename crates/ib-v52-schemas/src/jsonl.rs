//! The JSON Lines reader shared by the dataset loaders and the run-record loader.

use std::fmt;

use serde::de::DeserializeOwned;

/// Why a JSON Lines document could not be parsed.
#[derive(Debug)]
pub enum JsonlError {
    /// The bytes are not UTF-8.
    Utf8(std::str::Utf8Error),
    /// Line `line` (1-based) is not a valid record.
    Line {
        line: usize,
        source: serde_json::Error,
    },
}

impl fmt::Display for JsonlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Utf8(err) => write!(f, "non-UTF8 JSONL: {err}"),
            Self::Line { line, source } => write!(f, "line {line}: {source}"),
        }
    }
}

impl std::error::Error for JsonlError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Utf8(err) => Some(err),
            Self::Line { source, .. } => Some(source),
        }
    }
}

/// Parse UTF-8 JSON Lines: one record per line, whitespace-only lines skipped, each line trimmed.
///
/// # Errors
/// Returns [`JsonlError::Utf8`] for non-UTF-8 input and [`JsonlError::Line`] for the first line
/// that is not a valid `T`.
pub fn parse_jsonl<T: DeserializeOwned>(bytes: &[u8]) -> Result<Vec<T>, JsonlError> {
    let text = std::str::from_utf8(bytes).map_err(JsonlError::Utf8)?;
    let mut items = Vec::new();
    for (idx, line) in text.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        items.push(
            serde_json::from_str(trimmed).map_err(|source| JsonlError::Line {
                line: idx + 1,
                source,
            })?,
        );
    }
    Ok(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skips_blank_lines_and_reports_the_failing_line() {
        let ok: Vec<u32> = parse_jsonl(b"1\n\n  2 \n").unwrap();
        assert_eq!(ok, vec![1, 2]);
        match parse_jsonl::<u32>(b"1\n\nx\n") {
            Err(JsonlError::Line { line, .. }) => assert_eq!(line, 3),
            other => panic!("{other:?}"),
        }
        assert!(matches!(
            parse_jsonl::<u32>(&[0xff]),
            Err(JsonlError::Utf8(_))
        ));
    }
}
