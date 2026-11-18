use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::json;

use super::*;

static NEXT_TEST_DIR: AtomicU64 = AtomicU64::new(0);

struct TestTree(PathBuf);

impl TestTree {
    fn new() -> Self {
        let id = NEXT_TEST_DIR.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("identitybench-native-{}-{id}", std::process::id()));
        fs::create_dir(&path).expect("create isolated test directory");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
            .expect("restrict test directory permissions");
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestTree {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn write_script(path: &Path, body: &str) {
    fs::write(path, format!("#!/bin/sh\n{body}\n")).expect("write test evaluator");
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .expect("make test evaluator executable");
}

fn small_limits() -> ProcessLimits {
    ProcessLimits {
        stdin_bytes: 4096,
        stdout_bytes: 4096,
        stderr_bytes: 4096,
    }
}

#[test]
fn bounded_process_transports_jsonl_and_captures_both_streams() {
    let tree = TestTree::new();
    let executable = tree.path().join("echo");
    write_script(&executable, "/bin/cat\nprintf diagnostic >&2");
    let result = run_bounded_with_limits(
        &executable,
        b"{\"case_id\":\"positive\"}\n",
        Duration::from_secs(2),
        small_limits(),
    )
    .expect("bounded evaluator succeeds");
    assert_eq!(result.exit_code, Some(0));
    assert_eq!(result.stdout.bytes, b"{\"case_id\":\"positive\"}\n");
    assert_eq!(result.stderr.bytes, b"diagnostic");
    assert!(!result.stdout.exceeded);
    assert!(!result.stderr.exceeded);
}

#[test]
fn bounded_process_kills_a_timed_out_direct_child() {
    let tree = TestTree::new();
    let executable = tree.path().join("timeout");
    write_script(&executable, "echo $$ > child.pid\nexec /bin/sleep 5");
    let start = Instant::now();
    let error =
        run_bounded_with_limits(&executable, b"", Duration::from_millis(500), small_limits())
            .expect_err("timed-out child is rejected");
    assert!(error.0.contains("timeout"), "{error}");
    assert!(start.elapsed() < Duration::from_secs(2));
    let child_pid = fs::read_to_string(tree.path().join("child.pid"))
        .expect("child process ID was recorded")
        .trim()
        .parse::<i32>()
        .expect("child process ID is numeric");
    assert_process_exited(child_pid);
}

#[test]
fn bounded_process_cleans_a_grandchild_that_keeps_pipes_open() {
    let tree = TestTree::new();
    let executable = tree.path().join("grandchild");
    write_script(&executable, "/bin/sleep 5 &\necho $! > child.pid\nexit 0");
    let start = Instant::now();
    let error = run_bounded_with_limits(&executable, b"", Duration::from_secs(2), small_limits())
        .expect_err("inherited pipes from a descendant are rejected");
    assert!(error.0.contains("descendants retained"), "{error}");
    assert!(start.elapsed() < Duration::from_secs(2));
    let child_pid = fs::read_to_string(tree.path().join("child.pid"))
        .expect("grandchild process ID was recorded")
        .trim()
        .parse::<i32>()
        .expect("grandchild process ID is numeric");
    assert_process_exited(child_pid);
}

fn assert_process_exited(raw_pid: i32) {
    let pid = nix::unistd::Pid::from_raw(raw_pid);
    let cleanup_deadline = Instant::now() + Duration::from_millis(500);
    while nix::sys::signal::kill(pid, None).is_ok() && Instant::now() < cleanup_deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        nix::sys::signal::kill(pid, None).is_err(),
        "evaluator process was terminated"
    );
}

#[test]
fn bounded_process_kills_on_stdout_and_stderr_caps() {
    let tree = TestTree::new();
    let stdout = tree.path().join("large-stdout");
    write_script(&stdout, "/bin/dd if=/dev/zero bs=65536 count=8 2>/dev/null");
    let mut limits = small_limits();
    limits.stdout_bytes = 1024;
    let start = Instant::now();
    let error = run_bounded_with_limits(&stdout, b"", Duration::from_secs(2), limits)
        .expect_err("oversized stdout is rejected");
    assert!(error.0.contains("output exceeded"), "{error}");
    assert!(start.elapsed() < Duration::from_secs(2));

    let stderr = tree.path().join("large-stderr");
    write_script(
        &stderr,
        "/bin/dd if=/dev/zero bs=65536 count=8 1>&2 2>/dev/null",
    );
    let mut limits = small_limits();
    limits.stderr_bytes = 1024;
    let start = Instant::now();
    let error = run_bounded_with_limits(&stderr, b"", Duration::from_secs(2), limits)
        .expect_err("oversized stderr is rejected");
    assert!(error.0.contains("output exceeded"), "{error}");
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[test]
fn bounded_process_rejects_oversized_stdin_before_spawn() {
    let tree = TestTree::new();
    let executable = tree.path().join("unused");
    write_script(&executable, "exit 0");
    let mut limits = small_limits();
    limits.stdin_bytes = 3;
    let error = run_bounded_with_limits(&executable, b"four", Duration::from_secs(1), limits)
        .expect_err("oversized input is rejected");
    assert!(error.0.contains("stdin exceeds"), "{error}");
}

#[test]
fn artifact_reader_rejects_symlinks_and_sensitive_paths_before_reading() {
    let tree = TestTree::new();
    let report_dir = tree.path().join("reports/v5/conformance");
    fs::create_dir_all(&report_dir).expect("create artifact root");
    let target = report_dir.join("public.json");
    fs::write(&target, b"public").expect("write public artifact");
    let link = report_dir.join("linked.json");
    std::os::unix::fs::symlink(&target, &link).expect("create artifact symlink");
    let root = tree.path().canonicalize().unwrap();
    let error = read_bound_file(
        &root,
        &json!({
            "path":"reports/v5/conformance/linked.json",
            "sha256":sha256_bytes(b"public"),
        }),
        "symlink test",
    )
    .expect_err("symlinked artifacts are rejected");
    assert!(error.0.contains("symlink"), "{error}");

    let error = resolve_artifact_path(&root, "reports/v5/conformance/.env.local")
        .expect_err("sensitive paths are rejected before lookup");
    assert!(error.0.contains("sensitive"), "{error}");
    let error = resolve_artifact_path(&root, "tmp/private.json")
        .expect_err("paths outside approved roots are rejected");
    assert!(error.0.contains("public dataset or conformance"), "{error}");

    let env_file = tree.path().join(".env");
    fs::write(&env_file, b"disposable fixture placeholder").expect("write disposable env fixture");
    let error = stage_executable(&env_file, &report_dir.join("staged-env"))
        .expect_err("sensitive evaluator sources are rejected before reading");
    assert!(error.0.contains("sensitive"), "{error}");
}

#[test]
fn private_artifact_writes_preserve_occupied_files_and_symlinks() {
    let tree = TestTree::new();
    let path = tree.path().join("existing.json");
    fs::write(&path, b"prior evidence").expect("write prior evidence");
    let error = write_private(&path, b"replacement", false)
        .expect_err("existing output is never overwritten");
    assert!(error.0.contains("already exists"), "{error}");
    assert_eq!(fs::read(&path).unwrap(), b"prior evidence");

    let target = tree.path().join("target.json");
    fs::write(&target, b"target").expect("write symlink target");
    let link = tree.path().join("new-output.json");
    std::os::unix::fs::symlink(&target, &link).expect("create occupied output symlink");
    assert!(ensure_absent_path(&link).is_err());
    assert!(write_private(&link, b"replacement", false).is_err());
    assert_eq!(fs::read(&target).unwrap(), b"target");

    let private = tree.path().join("private.json");
    write_private(&private, b"private", false).expect("create private artifact");
    assert_eq!(
        fs::metadata(private).unwrap().permissions().mode() & 0o777,
        0o600
    );

    let private_dir = tree.path().join("private-dir");
    create_private_dir(&private_dir).expect("create private artifact directory");
    assert_eq!(
        fs::metadata(private_dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
}

#[test]
fn output_preflight_rejects_existing_dirs_files_and_symlinks() {
    let tree = TestTree::new();
    let root = tree.path().canonicalize().unwrap();
    let report_root = root.join("reports/v5/conformance");
    fs::create_dir_all(&report_root).expect("create public artifact root");
    let new_run = report_root.join("new-run");
    validate_new_output_directory(&root, &new_run).expect("new output path passes preflight");
    fs::create_dir(&new_run).expect("occupy output directory");
    assert!(validate_new_output_directory(&root, &new_run).is_err());

    let existing = report_root.join("existing-receipt.json");
    fs::write(&existing, b"prior receipt").expect("write prior receipt");
    let label = artifact_label(&root, &existing).unwrap();
    assert!(resolve_new_artifact_path(&root, &label).is_err());
    let linked = report_root.join("linked-receipt.json");
    std::os::unix::fs::symlink(&existing, &linked).expect("create output symlink");
    let label = artifact_label(&root, &linked).unwrap();
    assert!(resolve_new_artifact_path(&root, &label).is_err());
}

#[test]
fn freeze_root_marker_is_relative_and_resolves_after_copying_the_tree() {
    let original = TestTree::new();
    let copied = TestTree::new();
    let freeze_bytes = serde_json::to_vec(&json!({
        "schema_version": NATIVE_FREEZE_SCHEMA,
        "repository_root": ".",
    }))
    .unwrap();
    for tree in [&original, &copied] {
        fs::write(tree.path().join("Cargo.toml"), b"[workspace]\n")
            .expect("write repository marker");
        fs::create_dir_all(tree.path().join("crates/ib-cli")).expect("create crate marker");
        let freeze_dir = tree.path().join("reports/v5/conformance/copied-proof");
        fs::create_dir_all(&freeze_dir).expect("create freeze location");
        fs::write(freeze_dir.join("freeze.json"), &freeze_bytes)
            .expect("write relative-root freeze");
    }
    let original_freeze = read_native_freeze(
        &original
            .path()
            .join("reports/v5/conformance/copied-proof/freeze.json"),
    )
    .expect("original root resolves");
    let copied_freeze = read_native_freeze(
        &copied
            .path()
            .join("reports/v5/conformance/copied-proof/freeze.json"),
    )
    .expect("copied root resolves");
    assert_eq!(original_freeze["repository_root"], ".");
    assert_eq!(copied_freeze["repository_root"], ".");
    let serialized = String::from_utf8(freeze_bytes).unwrap();
    assert!(!serialized.contains(&original.path().display().to_string()));
    assert!(!serialized.contains(&copied.path().display().to_string()));
}

#[test]
fn staged_executable_mutation_is_detected_by_digest_binding() {
    let tree = TestTree::new();
    let reports = tree.path().join("reports/v5/conformance/run/executables");
    fs::create_dir_all(&reports).expect("create staged executable directory");
    let executable = reports.join("reference");
    write_private(&executable, b"frozen executable bytes", true).expect("stage executable bytes");
    let root = tree.path().canonicalize().unwrap();
    let digest = sha256_bytes(b"frozen executable bytes");
    let freeze = json!({
        "executables": {
            "full_spec_blind": {
                "path":"reports/v5/conformance/run/executables/reference",
                "sha256":digest,
            }
        }
    });
    verify_staged_executable(&root, &freeze, "full_spec_blind")
        .expect("original staged executable matches");
    fs::remove_file(&executable).expect("remove staged executable");
    fs::write(&executable, b"mutated executable bytes").expect("mutate staged executable");
    let error = verify_staged_executable(&root, &freeze, "full_spec_blind")
        .expect_err("mutation is detected");
    assert!(error.0.contains("digest mismatch"), "{error}");
}
