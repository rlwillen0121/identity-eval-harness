//! Environment loading, isolated CLI runners, HTTP API adapters, replay, and provider factory
//! for the IdentityBench Rust harness.

pub mod anthropic_api;
pub mod base;
pub mod cli;
pub mod env;
pub mod envfile;
pub mod factory;
mod http;
pub mod json;
pub mod openai_compat;
pub mod replay;
pub mod subprocess;

pub use cli::agy as agy_cli;
pub use cli::claude as claude_cli;
pub use cli::codex as codex_cli;
pub use cli::cursor as cursor_cli;
pub use cli::grok as grok_cli;
pub use cli::muse as muse_cli;
