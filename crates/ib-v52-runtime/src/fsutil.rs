//! Crash-safe artifact writes shared by the runner, report, leaderboard, probe, and CLI.

use std::fs::File;
use std::path::Path;

use anyhow::{Context, Result};
use serde::Serialize;
use serde::de::DeserializeOwned;

/// Write `bytes` to `path` atomically: write and fsync a unique sibling temp file, then rename it
/// over `path`. Parent directories are created. A reader never observes a partial file.
///
/// # Errors
/// Fails when the directory, temp file, or rename cannot be created; the temp file is removed.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    ib_core::artifact::write(path, bytes, true)
        .with_context(|| format!("failed to publish {}", path.display()))
}

/// [`atomic_write`] relative to a pinned run-directory handle.
pub fn atomic_write_at(root: &File, relative: &Path, bytes: &[u8]) -> Result<()> {
    ib_core::artifact::write_at(root, relative, bytes, true)
        .with_context(|| format!("failed to publish {}", relative.display()))
}

/// Pretty-printed JSON followed by one LF, the form of every JSON artifact the harness writes.
///
/// # Errors
/// Fails when `value` cannot be serialized.
pub fn pretty_json_bytes<T: Serialize + ?Sized>(value: &T) -> Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    Ok(bytes)
}

/// [`atomic_write`] of [`pretty_json_bytes`]; returns the written bytes.
///
/// # Errors
/// Fails when `value` cannot be serialized or the file cannot be written.
pub fn write_pretty_json<T: Serialize + ?Sized>(path: &Path, value: &T) -> Result<Vec<u8>> {
    let bytes = pretty_json_bytes(value)?;
    atomic_write(path, &bytes)?;
    Ok(bytes)
}

/// Pretty-printed JSON followed by LF, atomically written below a pinned directory.
pub fn write_pretty_json_at<T: Serialize + ?Sized>(
    root: &File,
    relative: &Path,
    value: &T,
) -> Result<Vec<u8>> {
    let bytes = pretty_json_bytes(value)?;
    atomic_write_at(root, relative, &bytes)?;
    Ok(bytes)
}

/// Read and parse a JSON file, naming the path in every error.
///
/// # Errors
/// Fails when the file cannot be read or is not valid JSON for `T`.
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let bytes = ib_core::artifact::read_limited(path, 64 * 1024 * 1024)
        .with_context(|| format!("failed to read {}", path.display()))?;
    serde_json::from_slice(&bytes).with_context(|| format!("{} is invalid JSON", path.display()))
}
