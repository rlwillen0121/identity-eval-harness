use thiserror::Error;

#[derive(Debug, Error)]
pub enum RunnerError {
    #[error("invalid runner configuration: {0}")]
    Configuration(String),
    #[error("invalid runner input: {0}")]
    InvalidInput(String),
    #[error("schema error: {0}")]
    Schema(String),
    #[error("runtime error: {0}")]
    Runtime(#[from] ib_v5_runtime::RuntimeError),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("tokenizer error: {0}")]
    Tokenizer(String),
}
