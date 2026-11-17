//! Atomic replacement for generated report files.

use std::io::Write;
use std::path::Path;

use crate::error::ReportError;

/// Replace a report file without following an existing symlink or truncating a hardlink target.
pub(crate) fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), ReportError> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent)?;

    let mut temp_file = tempfile::NamedTempFile::new_in(parent)?;
    temp_file.write_all(bytes)?;
    temp_file.as_file().sync_all()?;
    temp_file.persist(path).map_err(|error| error.error)?;
    Ok(())
}
