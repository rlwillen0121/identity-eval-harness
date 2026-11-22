//! CLI path handling without accessing provider credentials for offline commands.
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};

pub fn resolve(root: &Path, value: &Path) -> PathBuf {
    if value.is_absolute() {
        value.to_path_buf()
    } else {
        root.join(value)
    }
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

/// Private suites are never committed (`SPEC-v5.2` §2.2): refuse any output inside the repository,
/// including through directory aliases.
pub fn ensure_outside_repository(output: &Path, root: &Path) -> Result<()> {
    let repo = root.canonicalize()?;
    let target = canonicalize_available(output)?;
    if target.starts_with(&repo) {
        bail!(
            "private suites are never written inside the repository; choose an output directory outside {}",
            repo.display()
        );
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
            && directory.join("crates/ib-v52-generator").is_dir()
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
    fn resolve_keeps_absolute_paths_and_joins_relative_ones() {
        let root = Path::new("/repo");
        assert_eq!(
            resolve(root, Path::new("datasets/v52/v52_dev")),
            root.join("datasets/v52/v52_dev")
        );
        assert_eq!(resolve(root, Path::new("/abs/out")), Path::new("/abs/out"));
    }

    #[test]
    fn output_may_not_overlap_or_alias_a_sealed_dataset() {
        let dir = tempfile::tempdir().unwrap();
        let dataset = dir.path().join("datasets/v52/v52_dev");
        std::fs::create_dir_all(&dataset).unwrap();
        std::fs::write(dataset.join("SEAL.json"), b"{}").unwrap();

        assert!(ensure_output_separate(&dir.path().join("runs/new"), &dataset).is_ok());
        assert!(ensure_output_separate(&dataset, &dataset).is_err());
        assert!(ensure_output_separate(&dataset.join("nested"), &dataset).is_err());
        assert!(ensure_output_separate(&dir.path().join("datasets"), &dataset).is_err());

        let other_sealed = dir.path().join("other");
        std::fs::create_dir(&other_sealed).unwrap();
        std::fs::write(other_sealed.join("SEAL.json"), b"{}").unwrap();
        assert!(ensure_output_separate(&other_sealed, &dataset).is_err());

        #[cfg(unix)]
        {
            let alias = dir.path().join("dataset-alias");
            std::os::unix::fs::symlink(&dataset, &alias).unwrap();
            assert!(ensure_output_separate(&alias.join("run"), &dataset).is_err());
        }
    }

    #[test]
    fn find_root_prefers_explicit_root() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            find_root(Some(dir.path())).unwrap(),
            dir.path().canonicalize().unwrap()
        );
    }
}
