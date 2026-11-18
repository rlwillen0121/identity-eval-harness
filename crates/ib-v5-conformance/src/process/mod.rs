//! Bounded process transport, provenance verification, and hygiene audits for v5 conformance.
//!
//! Mirrors `identitybench.v5.conformance_process`.

pub mod freeze;
pub mod native;
pub mod native_trace_delivery;
pub mod paths;
pub mod profiles;
pub mod python_hygiene;
pub mod verify;

use thiserror::Error;

use crate::shared::ConformanceProtocolError;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct SharedRuntimeError(pub String);

impl From<ConformanceProtocolError> for SharedRuntimeError {
    fn from(err: ConformanceProtocolError) -> Self {
        Self(err.to_string())
    }
}

pub use freeze::*;
pub use native::*;
pub use native_trace_delivery::*;
pub use paths::*;
pub use profiles::*;
pub use python_hygiene::*;
pub use verify::*;

#[cfg(test)]
#[path = "tests/native_trace_delivery_tests.rs"]
mod native_trace_delivery_tests;
