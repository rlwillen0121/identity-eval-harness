//! Error types for the IdentityBench v5 generator and sealed dataset loader.

use ib_core::JsonError;
use ib_v5_oracle::OracleError;
use ib_v5_runtime::{ObsStrategyError, PolicyCardError, RuntimeError};
use ib_v5_schemas::SchemaValidationError;
use thiserror::Error;

/// Errors raised during v5 cell planning, fixture generation, or dataset writing.
#[derive(Debug, Error)]
pub enum GeneratorError {
    #[error("unknown v5 suite: {0:?}")]
    UnknownSuite(String),

    #[error("unknown v5 coupling family: {0}")]
    UnknownFamily(String),

    #[error("unknown v5 regime: {0}")]
    UnknownRegime(String),

    #[error("unknown v5 evidence label: {0}")]
    UnknownEvidenceLabel(String),

    #[error("unknown v5 replica label: {0}")]
    UnknownReplicaLabel(String),

    #[error("unknown v5 slice label: {0}")]
    UnknownSliceLabel(String),

    #[error("{0}")]
    InvalidInput(String),

    #[error("{0}")]
    DisjointnessViolation(String),

    #[error("{0}")]
    ProtectedPath(String),

    #[error(transparent)]
    Schema(#[from] SchemaValidationError),

    #[error(transparent)]
    Oracle(#[from] OracleError),

    #[error(transparent)]
    Runtime(#[from] RuntimeError),

    #[error(transparent)]
    PolicyCard(#[from] PolicyCardError),

    #[error(transparent)]
    ObsStrategy(#[from] ObsStrategyError),

    #[error(transparent)]
    Json(#[from] JsonError),

    #[error("JSON error: {0}")]
    SerdeJson(#[from] serde_json::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

/// Errors raised when a persisted v5 dataset artifact is missing, malformed, or inconsistent.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct V5ArtifactError(pub String);

impl V5ArtifactError {
    #[must_use]
    pub fn new(msg: impl Into<String>) -> Self {
        Self(msg.into())
    }
}

impl From<SchemaValidationError> for V5ArtifactError {
    fn from(err: SchemaValidationError) -> Self {
        Self(err.to_string())
    }
}

impl From<JsonError> for V5ArtifactError {
    fn from(err: JsonError) -> Self {
        Self(err.to_string())
    }
}

impl From<std::io::Error> for V5ArtifactError {
    fn from(err: std::io::Error) -> Self {
        Self(err.to_string())
    }
}
