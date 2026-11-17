//! Error types for IdentityBench v5 scoring and inference.

use thiserror::Error;

/// Error returned by v5 scoring, artifact verification, and inference functions.
#[derive(Debug, Error)]
pub enum ScoringError {
    #[error("{0}")]
    Value(String),

    #[error("type error: {0}")]
    Type(String),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("canonical JSON error: {0}")]
    CanonicalJson(#[from] ib_core::JsonError),

    #[error("schema validation failed: {0}")]
    Schema(#[from] ib_v5_schemas::SchemaValidationError),

    #[error("runtime error: {0}")]
    Runtime(#[from] ib_v5_runtime::RuntimeError),

    #[error("verifier error: {0}")]
    Verifier(#[from] ib_v5_runtime::VerifierContextError),

    #[error("oracle error: {0}")]
    Oracle(#[from] ib_v5_oracle::OracleError),

    #[error("accounting error: {0}")]
    Accounting(#[from] ib_v5_accounting::AccountingError),

    #[error("numeric inference error: {0}")]
    NumPy(#[from] ib_core::NpError),
}

impl ScoringError {
    #[must_use]
    pub fn value(msg: impl Into<String>) -> Self {
        Self::Value(msg.into())
    }

    #[must_use]
    pub fn type_err(msg: impl Into<String>) -> Self {
        Self::Type(msg.into())
    }
}
