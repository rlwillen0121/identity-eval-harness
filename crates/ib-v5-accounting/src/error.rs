//! Error types for `ib-v5-accounting`.

use ib_core::JsonError;
use ib_v5_schemas::SchemaValidationError;
use thiserror::Error;

/// Errors returned by v5 provider accounting, usage normalization, timing, and cost evaluation.
#[derive(Debug, Error)]
pub enum AccountingError {
    #[error("schema validation error: {0}")]
    Schema(#[from] SchemaValidationError),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("canonical JSON error: {0}")]
    CanonicalJson(#[from] JsonError),

    #[error("unknown v5 provider usage accounting policy: {0:?}")]
    UnknownPolicy(String),

    #[error("{0}")]
    Validation(String),
}
