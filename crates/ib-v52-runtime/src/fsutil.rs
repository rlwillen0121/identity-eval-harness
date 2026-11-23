//! Crash-safe artifact writes shared by the runner, report, leaderboard, probe, and CLI.

use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use anyhow::{Context, Result};
use serde::Serialize;
use serde::de::DeserializeOwned;

static ATOMIC_TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// Write `bytes` to `path` atomically: write and fsync a unique sibling temp file, then rename it
/// over `path`. Parent directories are created. A reader never observes a partial file.
///
/// # Errors
/// Fails when the directory, temp file, or rename cannot be created; the temp file is removed.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent)?;
    let seq = ATOMIC_TMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("artifact");
    let tmp_path = parent.join(format!(".{file_name}.tmp.{pid}.{seq}"));
    let write_res = (|| -> Result<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp_path)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        Ok(())
    })();
    if let Err(err) = write_res {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(err);
    }
    if let Err(err) = std::fs::rename(&tmp_path, path) {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(err.into());
    }
    Ok(())
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

/// Read and parse a JSON file, naming the path in every error.
///
/// # Errors
/// Fails when the file cannot be read or is not valid JSON for `T`.
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let bytes =
        std::fs::read(path).with_context(|| format!("failed to read {}", path.display()))?;
    serde_json::from_slice(&bytes).with_context(|| format!("{} is invalid JSON", path.display()))
}
