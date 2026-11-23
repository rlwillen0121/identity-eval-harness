//! Safe, crash-aware filesystem operations for benchmark artifacts.

use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Component, Path};
use std::sync::atomic::{AtomicU64, Ordering};

use rustix::fs::{
    AtFlags, Dir, Mode, OFlags, RenameFlags, fchmod, fstat, mkdirat, open, openat, renameat,
    renameat_with, statat, unlinkat,
};
use rustix::process::geteuid;

pub use rustix::fs::FileType;

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// One name and its no-follow filesystem type from an opened directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirectoryEntry {
    pub name: String,
    pub file_type: FileType,
}

fn invalid_data(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn from_rustix<T>(result: rustix::io::Result<T>) -> io::Result<T> {
    result.map_err(Into::into)
}

fn opened_file(fd: rustix::fd::OwnedFd) -> File {
    fd.into()
}

fn open_absolute_directory(path: &Path) -> io::Result<File> {
    if !path.is_absolute() {
        return Err(invalid_data("directory path must be absolute"));
    }
    let mut current = opened_file(from_rustix(open(
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    ))?);
    for component in path.components() {
        match component {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(name) => {
                let next = from_rustix(openat(
                    &current,
                    name,
                    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                    Mode::empty(),
                ))?;
                current = opened_file(next);
            }
            Component::ParentDir | Component::Prefix(_) => {
                return Err(invalid_data("directory path is not normalized"));
            }
        }
    }
    Ok(current)
}

fn file_name(path: &Path) -> io::Result<OsString> {
    let name = path
        .file_name()
        .ok_or_else(|| invalid_data("artifact path has no file name"))?;
    if name == "." || name == ".." {
        return Err(invalid_data("artifact path has an invalid final component"));
    }
    Ok(name.to_os_string())
}

fn open_parent_path(path: &Path, create: bool) -> io::Result<(File, OsString)> {
    let name = file_name(path)?;
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    if create && !parent.exists() {
        std::fs::create_dir_all(parent)?;
    }
    // Parent aliases selected by the operator (including macOS /tmp and /var) are resolved once.
    // The returned directory is then traversed by descriptor with O_NOFOLLOW on every component.
    let parent = std::fs::canonicalize(parent)?;
    Ok((open_absolute_directory(&parent)?, name))
}

fn open_relative_directory(root: &File, relative: &Path, create: bool) -> io::Result<File> {
    let mut current = root.try_clone()?;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return Err(invalid_data(
                "artifact relative path must contain only normal components",
            ));
        };
        if create {
            match from_rustix(mkdirat(&current, name, Mode::RWXU)) {
                Ok(()) => {}
                Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {}
                Err(err) => return Err(err),
            }
        }
        let next = from_rustix(openat(
            &current,
            name,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        ))?;
        current = opened_file(next);
    }
    Ok(current)
}

fn open_parent_at(root: &File, relative: &Path, create: bool) -> io::Result<(File, OsString)> {
    let name = file_name(relative)?;
    let parent = relative.parent().unwrap_or_else(|| Path::new(""));
    let parent = open_relative_directory(root, parent, create)?;
    Ok((parent, name))
}

fn open_regular_at(parent: &File, name: &std::ffi::OsStr) -> io::Result<File> {
    let fd = from_rustix(openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    ))?;
    let stat = from_rustix(fstat(&fd))?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(invalid_data("artifact path is not a regular file"));
    }
    Ok(opened_file(fd))
}

fn read_open_file(mut file: File, max_bytes: Option<usize>) -> io::Result<Vec<u8>> {
    let capacity = if let Some(max_bytes) = max_bytes {
        let len = file.metadata()?.len();
        if len > max_bytes as u64 {
            return Err(invalid_data("artifact file exceeds the size limit"));
        }
        usize::try_from(len).unwrap_or(max_bytes).min(max_bytes)
    } else {
        usize::try_from(file.metadata()?.len()).unwrap_or(0)
    };
    let mut bytes = Vec::with_capacity(capacity);
    if let Some(max_bytes) = max_bytes {
        let limit = u64::try_from(max_bytes)
            .unwrap_or(u64::MAX)
            .saturating_add(1);
        file.take(limit).read_to_end(&mut bytes)?;
        if bytes.len() > max_bytes {
            return Err(invalid_data("artifact file exceeds the size limit"));
        }
    } else {
        file.read_to_end(&mut bytes)?;
    }
    Ok(bytes)
}

fn publish(
    parent: &File,
    name: &std::ffi::OsStr,
    bytes: &[u8],
    private: bool,
    exclusive: bool,
    allow_sticky_parent: bool,
) -> io::Result<()> {
    let parent_stat = from_rustix(fstat(parent))?;
    if FileType::from_raw_mode(parent_stat.st_mode) != FileType::Directory {
        return Err(invalid_data("artifact parent is not a directory"));
    }
    let owned_safe = parent_stat.st_uid == geteuid().as_raw() && parent_stat.st_mode & 0o022 == 0;
    let trusted_sticky_owner = parent_stat.st_uid == 0 || parent_stat.st_uid == geteuid().as_raw();
    let sticky_public =
        allow_sticky_parent && parent_stat.st_mode & 0o1000 != 0 && trusted_sticky_owner;
    if !owned_safe && !sticky_public {
        return Err(invalid_data("artifact parent is writable by another user"));
    }
    match from_rustix(statat(parent, name, AtFlags::SYMLINK_NOFOLLOW)) {
        Ok(stat) => {
            if exclusive {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "artifact already exists",
                ));
            }
            if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
                return Err(invalid_data("artifact destination is not a regular file"));
            }
            if stat.st_uid != geteuid().as_raw() {
                return Err(invalid_data(
                    "artifact destination is not owned by the current user",
                ));
            }
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => return Err(err),
    }

    let mode = if private {
        Mode::RUSR | Mode::WUSR
    } else {
        Mode::from(0o666)
    };
    let mut temp_name = OsString::new();
    let mut temp = None;
    for _ in 0..32 {
        let seq = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
        let candidate = format!(".artifact.tmp.{}.{}", std::process::id(), seq);
        match from_rustix(openat(
            parent,
            candidate.as_str(),
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            mode,
        )) {
            Ok(fd) => {
                temp_name.push(candidate);
                temp = Some(opened_file(fd));
                break;
            }
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    }
    let mut file = temp.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not allocate artifact temp file",
        )
    })?;
    let result = (|| {
        if private {
            from_rustix(fchmod(&file, Mode::RUSR | Mode::WUSR))?;
        }
        file.write_all(bytes)?;
        file.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = from_rustix(unlinkat(parent, &temp_name, AtFlags::empty()));
        return result;
    }
    let rename_result = if exclusive {
        from_rustix(renameat_with(
            parent,
            &temp_name,
            parent,
            name,
            RenameFlags::NOREPLACE,
        ))
    } else {
        from_rustix(renameat(parent, &temp_name, parent, name))
    };
    if let Err(err) = rename_result {
        let _ = from_rustix(unlinkat(parent, &temp_name, AtFlags::empty()));
        return Err(err);
    }
    parent.sync_all()
}

/// Open an existing directory, or create only the final directory component with mode 0700.
/// Existing directory permissions are never changed. Parent aliases such as macOS `/tmp` are
/// canonicalized before the final component is opened with `O_NOFOLLOW`.
pub fn directory(path: &Path, create: bool) -> io::Result<File> {
    if path == Path::new("/") {
        return open_absolute_directory(path);
    }
    if path == Path::new(".") || path == Path::new("") {
        return open_absolute_directory(&std::env::current_dir()?);
    }
    let name = file_name(path)?;
    let parent_path = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    if create && !parent_path.exists() {
        std::fs::create_dir_all(parent_path)?;
    }
    let parent_path = std::fs::canonicalize(parent_path)?;
    let parent = open_absolute_directory(&parent_path)?;
    if create {
        match from_rustix(mkdirat(&parent, &name, Mode::RWXU)) {
            Ok(()) => {}
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {}
            Err(err) => return Err(err),
        }
    }
    let fd = from_rustix(openat(
        &parent,
        &name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    ))?;
    Ok(opened_file(fd))
}

/// Open or create a descendant directory relative to an already-open root. Every component is
/// validated as a normal single directory name and opened with `O_NOFOLLOW`.
pub fn directory_at(root: &File, relative: &Path, create: bool) -> io::Result<File> {
    open_relative_directory(root, relative, create)
}

/// Require current-user ownership and deny group/other write access.
pub fn require_owned_dir(dir: &File) -> io::Result<()> {
    let stat = from_rustix(fstat(dir))?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::Directory {
        return Err(invalid_data("artifact path is not a directory"));
    }
    if stat.st_uid != geteuid().as_raw() {
        return Err(invalid_data(
            "artifact directory is not owned by the current user",
        ));
    }
    if stat.st_mode & 0o022 != 0 {
        return Err(invalid_data(
            "artifact directory must not be writable by group or others",
        ));
    }
    Ok(())
}

/// Require a current-user-owned directory with no group/other access.
pub fn require_private_dir(dir: &File) -> io::Result<()> {
    require_owned_dir(dir)?;
    let stat = from_rustix(fstat(dir))?;
    if stat.st_mode & 0o077 != 0 {
        return Err(invalid_data(
            "artifact directory must be private to its owner",
        ));
    }
    if stat.st_mode & 0o700 != 0o700 {
        return Err(invalid_data(
            "artifact directory must be accessible to its owner",
        ));
    }
    Ok(())
}

/// Open a regular file without following a final symlink or blocking on a FIFO/device.
pub fn regular_file(path: &Path) -> io::Result<File> {
    let (parent, name) = open_parent_path(path, false)?;
    open_regular_at(&parent, &name)
}

/// Read a regular file without following a final symlink.
pub fn read(path: &Path) -> io::Result<Vec<u8>> {
    read_open_file(regular_file(path)?, None)
}

/// Read a regular file while enforcing `max_bytes` before allocating beyond that limit.
pub fn read_limited(path: &Path, max_bytes: usize) -> io::Result<Vec<u8>> {
    read_open_file(regular_file(path)?, Some(max_bytes))
}

/// Open a run artifact below a pinned root; every descendant directory and the file itself are
/// opened relative to descriptors with `O_NOFOLLOW`.
pub fn regular_file_at(root: &File, relative: &Path) -> io::Result<File> {
    let (parent, name) = open_parent_at(root, relative, false)?;
    open_regular_at(&parent, &name)
}

/// Read a run artifact below a pinned root.
pub fn read_at(root: &File, relative: &Path) -> io::Result<Vec<u8>> {
    read_open_file(regular_file_at(root, relative)?, None)
}

/// Read a run artifact below a pinned root with a strict byte limit.
pub fn read_limited_at(root: &File, relative: &Path, max_bytes: usize) -> io::Result<Vec<u8>> {
    read_open_file(regular_file_at(root, relative)?, Some(max_bytes))
}

/// List entries from a directory below a pinned root and classify each without following links.
pub fn entries_at(root: &File, relative: &Path) -> io::Result<Vec<DirectoryEntry>> {
    let dir = open_relative_directory(root, relative, false)?;
    let mut entries = Vec::new();
    let mut stream = from_rustix(Dir::read_from(&dir))?;
    while let Some(entry) = stream.read() {
        let entry = from_rustix(entry)?;
        let name = entry
            .file_name()
            .to_str()
            .map_err(|_| invalid_data("artifact directory contains a non-UTF-8 name"))?;
        if name == "." || name == ".." {
            continue;
        }
        let stat = from_rustix(statat(&dir, name, AtFlags::SYMLINK_NOFOLLOW))?;
        entries.push(DirectoryEntry {
            name: name.to_owned(),
            file_type: FileType::from_raw_mode(stat.st_mode),
        });
    }
    Ok(entries)
}

fn remove_entries(dir: &File) -> io::Result<()> {
    let mut stream = from_rustix(Dir::read_from(dir))?;
    let mut names = Vec::new();
    while let Some(entry) = stream.read() {
        let entry = from_rustix(entry)?;
        let name = entry
            .file_name()
            .to_str()
            .map_err(|_| invalid_data("artifact directory contains a non-UTF-8 name"))?;
        if name == "." || name == ".." {
            continue;
        }
        let stat = from_rustix(statat(dir, name, AtFlags::SYMLINK_NOFOLLOW))?;
        names.push((name.to_owned(), FileType::from_raw_mode(stat.st_mode)));
    }
    for (name, file_type) in names {
        if file_type == FileType::Directory {
            let child = from_rustix(openat(
                dir,
                name.as_str(),
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                Mode::empty(),
            ))?;
            let child = opened_file(child);
            remove_entries(&child)?;
            from_rustix(unlinkat(dir, name.as_str(), AtFlags::REMOVEDIR))?;
        } else {
            from_rustix(unlinkat(dir, name.as_str(), AtFlags::empty()))?;
        }
    }
    Ok(())
}

/// Remove a directory tree below a pinned root without following descendant symlinks.
pub fn remove_dir_all_at(root: &File, relative: &Path) -> io::Result<()> {
    let (parent, name) = open_parent_at(root, relative, false)?;
    let dir = open_relative_directory(root, relative, false)?;
    remove_entries(&dir)?;
    from_rustix(unlinkat(&parent, &name, AtFlags::REMOVEDIR))
}

/// Create/open a named lock file relative to an already-open directory.
pub fn named_lock_file(dir: &File, name: &std::ffi::OsStr) -> io::Result<File> {
    if Path::new(name).components().count() != 1 || name == "." || name == ".." {
        return Err(invalid_data("lock name must be a single path component"));
    }
    let fd = from_rustix(openat(
        dir,
        name,
        OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::RUSR | Mode::WUSR,
    ))?;
    let stat = from_rustix(fstat(&fd))?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(invalid_data("lock path is not a regular file"));
    }
    if stat.st_nlink != 1 {
        return Err(invalid_data(
            "lock file must not have additional hard links",
        ));
    }
    if stat.st_uid != geteuid().as_raw() || stat.st_mode & 0o022 != 0 {
        return Err(invalid_data(
            "lock file must be owned by the current user and not group/world writable",
        ));
    }
    Ok(opened_file(fd))
}

/// Open the persistent `.run.lock` file relative to an already-open run directory.
pub fn lock_file(dir: &File) -> io::Result<File> {
    named_lock_file(dir, std::ffi::OsStr::new(".run.lock"))
}

/// Atomically replace a regular-file artifact. `private` sets file mode 0600.
pub fn write(path: &Path, bytes: &[u8], private: bool) -> io::Result<()> {
    let (parent, name) = open_parent_path(path, true)?;
    publish(&parent, &name, bytes, private, false, true)
}

/// Atomically create an artifact only if it does not already exist. `private` sets mode 0600.
pub fn write_new(path: &Path, bytes: &[u8], private: bool) -> io::Result<()> {
    let (parent, name) = open_parent_path(path, true)?;
    publish(&parent, &name, bytes, private, true, true)
}

/// Atomically replace a path below a pinned root, refusing symlinked intermediate directories.
pub fn write_at(root: &File, relative: &Path, bytes: &[u8], private: bool) -> io::Result<()> {
    let (parent, name) = open_parent_at(root, relative, true)?;
    publish(&parent, &name, bytes, private, false, false)
}

/// Atomically create a path below a pinned root only when it does not already exist.
pub fn write_new_at(root: &File, relative: &Path, bytes: &[u8], private: bool) -> io::Result<()> {
    let (parent, name) = open_parent_at(root, relative, true)?;
    publish(&parent, &name, bytes, private, true, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    use std::os::unix::fs::{PermissionsExt, symlink};

    #[test]
    fn reads_reject_symlinks_and_limit_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("input");
        std::fs::write(&file, b"12345").unwrap();
        assert_eq!(read_limited(&file, 5).unwrap(), b"12345");
        assert!(read_limited(&file, 4).is_err());
        let sparse = dir.path().join("sparse");
        File::create(&sparse)
            .unwrap()
            .set_len(64 * 1024 * 1024)
            .unwrap();
        assert!(read_limited(&sparse, 8).is_err());
        #[cfg(unix)]
        {
            let alias = dir.path().join("alias");
            symlink(&file, &alias).unwrap();
            assert!(read(&alias).is_err());
        }
    }

    #[test]
    fn root_relative_io_rejects_linked_descendants() {
        let root = tempfile::tempdir().unwrap();
        let external = tempfile::tempdir().unwrap();
        let root_dir = directory(root.path(), false).unwrap();
        let sentinel = external.path().join("sentinel");
        std::fs::write(&sentinel, b"unchanged").unwrap();
        #[cfg(unix)]
        {
            symlink(external.path(), root.path().join("configs")).unwrap();
            assert!(write_at(&root_dir, Path::new("configs/slot/file"), b"bad", true).is_err());
            assert!(read_at(&root_dir, Path::new("configs/sentinel")).is_err());
            assert_eq!(std::fs::read(sentinel).unwrap(), b"unchanged");
        }
        write_at(&root_dir, Path::new("checkpoints/a.json"), b"ok", true).unwrap();
        assert_eq!(
            read_at(&root_dir, Path::new("checkpoints/a.json")).unwrap(),
            b"ok"
        );
    }

    #[test]
    fn private_and_exclusive_writes_keep_their_contracts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("artifact");
        write(&path, b"first", true).unwrap();
        write(&path, b"second", true).unwrap();
        assert_eq!(read(&path).unwrap(), b"second");
        let created = dir.path().join("new");
        write_new(&created, b"only once", true).unwrap();
        assert!(write_new(&created, b"overwrite", true).is_err());
        #[cfg(unix)]
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[cfg(unix)]
    #[test]
    fn directories_are_not_mutated_and_mode_checks_are_distinct() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("shared");
        std::fs::create_dir(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        let handle = directory(&path, true).unwrap();
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o755
        );
        require_owned_dir(&handle).unwrap();
        assert!(require_private_dir(&handle).is_err());
    }
}
