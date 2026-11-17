//! Python-compatibility primitives for the IdentityBench v5 Rust implementation.
//!
//! Every byte the Python harness wrote (sealed datasets, oracles, run artifacts, reports)
//! must be reproducible from Rust. These modules re-implement the exact CPython / NumPy
//! behaviours those bytes depend on.

pub mod hashing;
pub mod npcompat;
pub mod pyfloat;
pub mod pyjson;
pub mod pyrandom;

pub use npcompat::{Generator, NpError, Pcg64, SeedSequence};
pub use pyjson::{DumpOptions, JsonError, dumps, jcs_dumps, jcs_string};
pub use pyrandom::PyRandom;
