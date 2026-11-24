//! Error types for IdentityBench v5.2 dataset generation and sealed artifact I/O.

use thiserror::Error;

/// Errors returned while planning, generating, sealing, or loading IdentityBench v5.2 datasets.
#[derive(Debug, Error)]
pub enum V52GeneratorError {
    #[error("invalid generator configuration: {0}")]
    InvalidConfig(String),

    #[error("engine evaluation failed for episode '{episode_id}': {detail}")]
    EngineFailure { episode_id: String, detail: String },

    #[error("I/O error at '{path}': {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },

    #[error("JSON serialization/deserialization error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("sealed dataset verification failed: {0}")]
    SealVerification(String),
}
