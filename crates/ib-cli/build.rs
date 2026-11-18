use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

fn main() {
    println!("cargo:rerun-if-env-changed=RUSTC");
    println!("cargo:rerun-if-env-changed=TARGET");
    let manifest_dir = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR").expect("Cargo sets CARGO_MANIFEST_DIR"),
    );
    let workspace_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .expect("ib-cli must live at crates/ib-cli");
    let mut files = BTreeSet::new();

    for root_file in [
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "build.rs",
    ] {
        add_if_file(workspace_root, Path::new(root_file), &mut files);
    }

    let crates_dir = workspace_root.join("crates");
    watch(&crates_dir);
    for crate_entry in fs::read_dir(&crates_dir).expect("workspace crates directory exists") {
        let crate_entry = crate_entry.expect("read workspace crate entry");
        if !crate_entry
            .file_type()
            .expect("inspect workspace crate")
            .is_dir()
        {
            continue;
        }
        let crate_dir = crate_entry.path();
        watch(&crate_dir);
        let Some(relative_crate_dir) = crate_dir.strip_prefix(workspace_root).ok() else {
            continue;
        };
        add_if_file(
            workspace_root,
            &relative_crate_dir.join("Cargo.toml"),
            &mut files,
        );
        add_if_file(
            workspace_root,
            &relative_crate_dir.join("build.rs"),
            &mut files,
        );

        let source_dir = crate_dir.join("src");
        if source_dir.is_dir() {
            collect_source_files(workspace_root, &source_dir, &mut files)
                .expect("collect workspace source files");
        }
    }

    let out_dir = PathBuf::from(std::env::var_os("OUT_DIR").expect("Cargo sets OUT_DIR"));
    let mut generated = String::from("pub static EMBEDDED_SOURCE_IDENTITY: &[(&str, &str)] = &[\n");
    for label in files {
        let source =
            fs::read(workspace_root.join(&label)).expect("read source for compile-time identity");
        let digest = hex::encode(Sha256::digest(source));
        writeln!(generated, "    ({label:?}, {digest:?}),")
            .expect("write generated source identity");
    }
    generated.push_str("];\n");
    fs::write(out_dir.join("source_identity.rs"), generated)
        .expect("write embedded source identity module");

    let rustc = std::env::var_os("RUSTC").expect("Cargo sets RUSTC");
    let output = Command::new(rustc)
        .arg("-vV")
        .output()
        .expect("run the compiler selected by Cargo for toolchain identity");
    assert!(output.status.success(), "rustc -vV must succeed");
    let rustc_verbose_version = String::from_utf8(output.stdout)
        .expect("rustc -vV output must be UTF-8")
        .trim_end()
        .to_owned();
    let target = std::env::var("TARGET").expect("Cargo sets TARGET");
    let toolchain = format!(
        "pub static EMBEDDED_TOOLCHAIN_IDENTITY: &[(&str, &str)] = &[\n    (\"rustc_verbose_version\", {rustc_verbose_version:?}),\n    (\"target\", {target:?}),\n];\n"
    );
    fs::write(out_dir.join("toolchain_identity.rs"), toolchain)
        .expect("write embedded toolchain identity module");
}

fn collect_source_files(
    workspace_root: &Path,
    directory: &Path,
    files: &mut BTreeSet<String>,
) -> std::io::Result<()> {
    watch(directory);
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            continue;
        }
        let path = entry.path();
        if file_type.is_dir() {
            collect_source_files(workspace_root, &path, files)?;
        } else if file_type.is_file() && should_include_source(&path) {
            add_if_file(workspace_root, &path, files);
        }
    }
    Ok(())
}

fn should_include_source(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let lower = name.to_ascii_lowercase();
    if lower.starts_with(".env")
        || [".pem", ".key", ".p12", ".pfx", ".der", ".crt"]
            .iter()
            .any(|ext| lower.ends_with(ext))
    {
        return false;
    }
    !path.components().any(|part| {
        matches!(
            part.as_os_str().to_str(),
            Some("tests" | "fixtures" | "testdata" | "test_data")
        )
    })
}

fn add_if_file(workspace_root: &Path, path: &Path, files: &mut BTreeSet<String>) {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        workspace_root.join(path)
    };
    let Ok(metadata) = fs::symlink_metadata(&absolute) else {
        return;
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return;
    }
    println!("cargo:rerun-if-changed={}", absolute.display());
    if let Ok(relative) = absolute.strip_prefix(workspace_root)
        && let Some(relative) = relative.to_str()
    {
        files.insert(relative.replace('\\', "/"));
    }
}

fn watch(path: &Path) {
    println!("cargo:rerun-if-changed={}", path.display());
}
