//! Structured, source-qualified JSON Pointer diagnostics shared by v5.2 runtime and engine.

use serde::{Deserialize, Serialize};

/// One safe diagnostic located in a JSON document.
///
/// `pointer` is an RFC 6901 JSON Pointer. The empty string identifies the document root.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JsonPointerDiagnosticV52 {
    /// The document the pointer addresses, such as `config/transforms.json` or `output`.
    pub document: String,
    /// RFC 6901 pointer to the invalid or noteworthy value; `""` means the root.
    pub pointer: String,
    pub message: String,
}

/// Append one RFC 6901 escaped segment to a JSON Pointer.
#[must_use]
pub fn json_pointer_child(pointer: &str, segment: &str) -> String {
    let escaped = segment.replace('~', "~0").replace('/', "~1");
    format!("{pointer}/{escaped}")
}
