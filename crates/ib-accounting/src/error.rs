//! Error types for `ib-accounting`.

use thiserror::Error;

/// Errors returned by pricing-rate validation.
#[derive(Debug, Error)]
pub enum AccountingError {
    #[error("{0}")]
    Validation(String),
}
