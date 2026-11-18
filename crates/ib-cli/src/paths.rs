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

/// The canonical input location for the active Draft 5 suite artifacts.
pub fn default_dataset(root: &Path, suite: &str) -> PathBuf {
    root.join("datasets/v5_draft5").join(suite)
}

/// The committed Draft 5 blind-conformance receipt for a `v5_*` suite.
pub fn default_draft5_receipt(root: &Path, suite: &str) -> PathBuf {
    let suffix = suite.strip_prefix("v5_").unwrap_or(suite);
    root.join("reports/v5/conformance")
        .join(format!("v5_draft5_{suffix}"))
        .join("receipt.json")
}

/// Compare paths after resolving any existing symlinks and their missing descendants.
pub fn same_path(left: &Path, right: &Path) -> Result<bool> {
    Ok(canonicalize_available(left)? == canonicalize_available(right)?)
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
        // Installed binaries can use an explicit --root; source builds find the workspace root.
        let manifest = directory.join("Cargo.toml");
        if manifest.is_file()
            && directory.join("crates/ib-v5-generator").is_dir()
            && std::fs::read_to_string(&manifest).is_ok_and(|text| text.contains("[workspace]"))
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

    #[test]
    fn active_draft5_defaults_use_the_current_dataset_and_receipt_layout() {
        let root = Path::new("repo");
        for suite in ["v5_dev", "v5_lite", "v5_core", "v5_full"] {
            let suffix = suite.strip_prefix("v5_").unwrap();
            assert_eq!(
                default_dataset(root, suite),
                root.join("datasets/v5_draft5").join(suite)
            );
            assert_eq!(
                default_draft5_receipt(root, suite),
                root.join(format!(
                    "reports/v5/conformance/v5_draft5_{suffix}/receipt.json"
                ))
            );
        }
    }

    #[test]
    fn same_path_resolves_aliases_before_matching_the_default_dataset() {
        let dir = tempfile::tempdir().unwrap();
        let dataset = default_dataset(dir.path(), "v5_dev");
        std::fs::create_dir_all(&dataset).unwrap();
        let custom = dir.path().join("custom-dataset");
        std::fs::create_dir(&custom).unwrap();
        assert!(!same_path(&custom, &dataset).unwrap());

        #[cfg(unix)]
        {
            let alias = dir.path().join("dataset-alias");
            std::os::unix::fs::symlink(&dataset, &alias).unwrap();
            assert!(same_path(&alias, &dataset).unwrap());
        }
    }
}
