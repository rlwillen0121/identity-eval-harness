//! Error types for IdentityBench v5 report generation and provenance verification.

use std::fmt;

use ib_core::JsonError;
use ib_v5_generator::V5ArtifactError;
use ib_v5_schemas::SchemaValidationError;
use ib_v5_scoring::ScoringError;

/// Raised when an optional historical comparator source artifact is absent.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ComparatorEvidenceUnavailableError(pub String);

impl ComparatorEvidenceUnavailableError {
    #[must_use]
    pub fn new(msg: impl Into<String>) -> Self {
        Self(msg.into())
    }
}

/// Errors produced during v5 report validation, provenance checking, and rendering.
#[derive(Debug, thiserror::Error)]
pub enum ReportError {
    #[error("{0}")]
    Value(String),
    #[error(transparent)]
    ComparatorUnavailable(#[from] ComparatorEvidenceUnavailableError),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] JsonError),
    #[error(transparent)]
    Scoring(#[from] ScoringError),
    #[error(transparent)]
    Generator(#[from] V5ArtifactError),
    #[error(transparent)]
    Schema(#[from] SchemaValidationError),
}

impl ReportError {
    #[must_use]
    pub fn value(msg: impl fmt::Display) -> Self {
        Self::Value(msg.to_string())
    }
}
