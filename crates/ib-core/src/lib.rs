//! Python-compatibility primitives for the IdentityBench Rust implementation.
//!
//! Every byte the Python harness wrote (sealed datasets, oracles, run artifacts, reports)
//! must be reproducible from Rust. These modules re-implement the exact CPython
//! behaviours those bytes depend on.

pub mod artifact;
pub mod hashing;
pub mod pyfloat;
pub mod pyjson;
pub mod pyrandom;

pub use pyjson::{DumpOptions, JsonError, dumps, jcs_dumps, jcs_string};
pub use pyrandom::PyRandom;
