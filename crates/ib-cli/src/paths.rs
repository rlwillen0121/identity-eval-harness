//! CLI path handling without accessing provider credentials for offline commands.
use std::path::{Component, Path, PathBuf};

use anyhow::{Result, bail};

pub fn resolve(root: &Path, value: &Path) -> PathBuf {
    if value.is_absolute() {
        value.to_path_buf()
    } else {
        root.join(value)
    }
}

pub fn sensitive_path(path: &Path) -> bool {
    path.components().any(|part| {
        let Component::Normal(part) = part else {
            return false;
        };
        let part = part.to_string_lossy().to_lowercase();
        part == ".env"
            || part.starts_with(".env.")
            || ["credential", "secret", "token"]
                .iter()
                .any(|word| part.contains(word))
    })
}

pub fn provenance_input(root: &Path, path: &Path) -> Result<PathBuf> {
    let requested = resolve(root, path);
    if sensitive_path(path) || sensitive_path(&requested) {
        bail!("sensitive input paths are not allowed");
    }
    // A missing blind receipt is reported as UNKNOWN by validation, not a CLI error.
    // Resolve existing ancestors as well, so dangling aliases cannot bypass the check.
    let resolved = canonicalize_available(&requested)?;
    if sensitive_path(&resolved) {
        bail!("sensitive input paths are not allowed");
    }
    Ok(resolved)
}

fn canonicalize_available(path: &Path) -> Result<PathBuf> {
    match path.canonicalize() {
        Ok(path) => Ok(path),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            if let (Some(parent), Some(name)) = (path.parent(), path.file_name()) {
                Ok(canonicalize_available(parent)?.join(name))
            } else {
                Err(error.into())
            }
        }
        Err(error) => Err(error.into()),
    }
}

/// Run artifacts must never replace a sealed input, including through directory aliases.
/// Check before provider construction or writes so an invalid target cannot incur spend.
pub fn ensure_output_separate(output: &Path, dataset: &Path) -> Result<()> {
    let source = dataset.canonicalize()?;
    let target = canonicalize_available(output)?;
    if target.starts_with(&source)
        || source.starts_with(&target)
        || target.join("SEAL.json").try_exists()?
    {
        bail!("run output overlaps a sealed dataset; choose a separate output directory");
    }
    Ok(())
}

/// A resumed run may already contain an identical dataset copy. Never follow a
/// directory alias when preparing that copy, or copy into the original dataset.
pub fn ensure_dataset_copy_separate(output: &Path, dataset: &Path) -> Result<()> {
    let source = dataset.canonicalize()?;
    let target = canonicalize_available(output)?;
    if target.starts_with(&source)
        || source.starts_with(&target)
        || std::fs::symlink_metadata(output).is_ok_and(|m| m.file_type().is_symlink())
    {
        bail!("run output overlaps a sealed dataset; choose a separate output directory");
    }
    Ok(())
}

pub fn find_root(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path.canonicalize()?);
    }
    let cwd = std::env::current_dir()?;
    for directory in cwd.ancestors() {
        // Installed binaries can use an explicit --root; source builds find either tree.
        let manifest = directory.join("Cargo.toml");
        if manifest.is_file() && directory.join("crates/ib-v5-generator").is_dir() {
            return Ok(directory.to_path_buf());
        }
        if let Ok(text) = std::fs::read_to_string(directory.join("pyproject.toml"))
            && text
                .lines()
                .any(|line| line.trim() == "name = \"identitybench\"")
        {
            return Ok(directory.to_path_buf());
        }
    }
    Ok(cwd)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sensitive_alias_is_checked_before_and_after_resolution() {
        let dir = tempfile::tempdir().unwrap();
        let secret = dir.path().join(".env");
        std::fs::write(&secret, b"not read").unwrap();
        #[cfg(unix)]
        {
            let alias = dir.path().join("receipt.json");
            std::os::unix::fs::symlink(&secret, &alias).unwrap();
            assert!(provenance_input(dir.path(), &alias).is_err());
        }
        assert!(provenance_input(dir.path(), &secret).is_err());
        assert!(provenance_input(dir.path(), Path::new("missing/receipt.json")).is_ok());
    }
}
