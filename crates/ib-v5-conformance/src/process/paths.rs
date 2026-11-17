//! Path confinement, canonicalization, read-only enforcement, and chronology checks.

use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use super::SharedRuntimeError;
use super::profiles::{APPROVED_RESULT_ROOT, V1_RESULT_ROOT};
use crate::shared::{is_sha256, sha256_bytes};

fn check_posix_relative_parts(relative: &str) -> Option<Vec<&str>> {
    if relative.is_empty() || relative.starts_with('/') {
        return None;
    }
    let parts: Vec<&str> = relative.split('/').collect();
    if parts.iter().any(|p| matches!(*p, "" | "." | "..")) {
        return None;
    }
    Some(parts)
}

fn check_no_symlinks_under_root(
    root: &Path,
    candidate: &Path,
    err_msg_symlink: &str,
    err_msg_parent_symlink: &str,
) -> Result<(), SharedRuntimeError> {
    let meta = std::fs::symlink_metadata(candidate)
        .map_err(|exc| SharedRuntimeError(format!("artifact path missing: {exc}")))?;
    if meta.file_type().is_symlink() {
        return Err(SharedRuntimeError(err_msg_symlink.to_owned()));
    }
    let mut cur = candidate.parent();
    while let Some(parent) = cur {
        if parent == root {
            break;
        }
        let pmeta = std::fs::symlink_metadata(parent)
            .map_err(|exc| SharedRuntimeError(format!("artifact parent missing: {exc}")))?;
        if pmeta.file_type().is_symlink() {
            return Err(SharedRuntimeError(err_msg_parent_symlink.to_owned()));
        }
        cur = parent.parent();
    }
    Ok(())
}

pub fn resolve_result_artifact(root: &Path, relative: &str) -> Result<PathBuf, SharedRuntimeError> {
    let Some(parts) = check_posix_relative_parts(relative) else {
        return Err(SharedRuntimeError(format!(
            "unsafe artifact path: {relative:?}"
        )));
    };
    let v1_prefix = format!("{V1_RESULT_ROOT}/");
    let approved_prefix = format!("{APPROVED_RESULT_ROOT}/");
    let allowed_prefix = if relative.starts_with(&v1_prefix) {
        V1_RESULT_ROOT
    } else if relative.starts_with(&approved_prefix) {
        APPROVED_RESULT_ROOT
    } else {
        return Err(SharedRuntimeError(
            "artifact path is outside a versioned shared-results root".to_owned(),
        ));
    };
    let mut candidate = root.to_path_buf();
    for p in parts {
        candidate.push(p);
    }
    let resolved = candidate.canonicalize().map_err(|exc| {
        SharedRuntimeError(format!("artifact path does not exist ({relative}): {exc}"))
    })?;
    let allowed_root = root
        .join(allowed_prefix)
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("approved results root unavailable: {exc}")))?;
    check_no_symlinks_under_root(
        root,
        &candidate,
        "artifact path escaped the approved results root",
        "artifact path traverses a symlink",
    )?;
    if !resolved.starts_with(&allowed_root) {
        return Err(SharedRuntimeError(
            "artifact path escaped the approved results root".to_owned(),
        ));
    }
    if !resolved.is_file() {
        return Err(SharedRuntimeError(format!(
            "artifact path is not a file: {relative}"
        )));
    }
    Ok(resolved)
}

pub fn resolve_shared_input_artifact(
    root: &Path,
    relative: &str,
) -> Result<PathBuf, SharedRuntimeError> {
    let Some(parts) = check_posix_relative_parts(relative) else {
        return Err(SharedRuntimeError(
            "shared input artifact path is unsafe or outside shared_inputs".to_owned(),
        ));
    };
    let allowed_prefix = "reports/v5/conformance/shared_inputs/";
    if !relative.starts_with(allowed_prefix) {
        return Err(SharedRuntimeError(
            "shared input artifact path is unsafe or outside shared_inputs".to_owned(),
        ));
    }
    let mut candidate = root.to_path_buf();
    for p in parts {
        candidate.push(p);
    }
    let resolved = candidate.canonicalize().map_err(|exc| {
        SharedRuntimeError(format!(
            "shared input artifact does not exist ({relative}): {exc}"
        ))
    })?;
    let allowed_root = root
        .join("reports/v5/conformance/shared_inputs")
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("shared_inputs root unavailable: {exc}")))?;
    check_no_symlinks_under_root(
        root,
        &candidate,
        "shared input artifact path escaped its approved root",
        "shared input artifact path traverses a symlink",
    )?;
    if !resolved.starts_with(&allowed_root) {
        return Err(SharedRuntimeError(
            "shared input artifact path escaped its approved root".to_owned(),
        ));
    }
    if !resolved.is_file() {
        return Err(SharedRuntimeError(
            "shared input artifact path is not a file".to_owned(),
        ));
    }
    Ok(resolved)
}

pub fn resolve_qualification_artifact(
    root: &Path,
    relative: &str,
) -> Result<PathBuf, SharedRuntimeError> {
    let Some(parts) = check_posix_relative_parts(relative) else {
        return Err(SharedRuntimeError(
            "qualification artifact path is unsafe or outside review".to_owned(),
        ));
    };
    let allowed_prefix = "reports/v5/conformance/review/";
    if !relative.starts_with(allowed_prefix) {
        return Err(SharedRuntimeError(
            "qualification artifact path is unsafe or outside review".to_owned(),
        ));
    }
    let mut candidate = root.to_path_buf();
    for p in parts {
        candidate.push(p);
    }
    let resolved = candidate.canonicalize().map_err(|exc| {
        SharedRuntimeError(format!(
            "qualification artifact does not exist ({relative}): {exc}"
        ))
    })?;
    let allowed_root = root
        .join("reports/v5/conformance/review")
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("review root unavailable: {exc}")))?;
    check_no_symlinks_under_root(
        root,
        &candidate,
        "qualification artifact path escaped its approved root",
        "qualification artifact path traverses a symlink",
    )?;
    if !resolved.starts_with(&allowed_root) {
        return Err(SharedRuntimeError(
            "qualification artifact path escaped its approved root".to_owned(),
        ));
    }
    if !resolved.is_file() {
        return Err(SharedRuntimeError(
            "qualification artifact path is not a file".to_owned(),
        ));
    }
    Ok(resolved)
}

pub fn v3_relative_input_path(
    root: &Path,
    manifest_path: &Path,
    relative: &str,
) -> Result<String, SharedRuntimeError> {
    let Some(parts) = check_posix_relative_parts(relative) else {
        return Err(SharedRuntimeError(
            "v3 corpus-relative path is unsafe".to_owned(),
        ));
    };
    let parent = manifest_path.parent().ok_or_else(|| {
        SharedRuntimeError("v3 corpus-relative path escaped the repository".to_owned())
    })?;
    let mut candidate = parent.to_path_buf();
    for p in parts {
        candidate.push(p);
    }
    let rel = candidate.strip_prefix(root).map_err(|_| {
        SharedRuntimeError("v3 corpus-relative path escaped the repository".to_owned())
    })?;
    let posix = rel
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/");
    Ok(posix)
}

pub fn require_readonly_file(path: &Path) -> Result<(), SharedRuntimeError> {
    let fname = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let meta = std::fs::symlink_metadata(path)
        .map_err(|_| SharedRuntimeError(format!("expected a regular read-only file: {fname}")))?;
    if meta.file_type().is_symlink() || !meta.file_type().is_file() {
        return Err(SharedRuntimeError(format!(
            "expected a regular read-only file: {fname}"
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = meta.permissions().mode() & 0o777;
        if (mode & 0o222) != 0 {
            return Err(SharedRuntimeError(format!(
                "artifact is not immutable/read-only: {fname}"
            )));
        }
    }
    #[cfg(not(unix))]
    {
        if !meta.permissions().readonly() {
            return Err(SharedRuntimeError(format!(
                "artifact is not immutable/read-only: {fname}"
            )));
        }
    }
    Ok(())
}

pub fn verify_readonly_digest(
    path: &Path,
    expected_sha256: &str,
) -> Result<Vec<u8>, SharedRuntimeError> {
    require_readonly_file(path)?;
    let fname = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    if !is_sha256(expected_sha256) {
        return Err(SharedRuntimeError(format!(
            "artifact digest mismatch: {fname}"
        )));
    }
    let bytes = std::fs::read(path)
        .map_err(|exc| SharedRuntimeError(format!("failed reading {fname}: {exc}")))?;
    if sha256_bytes(&bytes) != expected_sha256 {
        return Err(SharedRuntimeError(format!(
            "artifact digest mismatch: {fname}"
        )));
    }
    Ok(bytes)
}

pub fn make_readonly(root: &Path) -> Result<(), SharedRuntimeError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut entries = collect_tree_postorder(root)?;
        for path in entries.drain(..) {
            let perms = if path.is_dir() {
                std::fs::Permissions::from_mode(0o555)
            } else {
                std::fs::Permissions::from_mode(0o444)
            };
            std::fs::set_permissions(&path, perms).map_err(|exc| {
                SharedRuntimeError(format!("failed setting readonly permissions: {exc}"))
            })?;
        }
        std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o555)).map_err(|exc| {
            SharedRuntimeError(format!("failed setting root readonly permissions: {exc}"))
        })?;
    }
    Ok(())
}

pub fn remove_readonly_tree(root: &Path) {
    if !root.exists() {
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700));
        if let Ok(entries) = collect_tree_postorder(root) {
            for path in entries {
                let mode = if path.is_dir() { 0o700 } else { 0o600 };
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode));
            }
        }
    }
    let _ = std::fs::remove_dir_all(root);
}

fn collect_tree_postorder(root: &Path) -> Result<Vec<PathBuf>, SharedRuntimeError> {
    let mut out = Vec::new();
    if !root.is_dir() {
        return Ok(out);
    }
    let read_dir = std::fs::read_dir(root)
        .map_err(|exc| SharedRuntimeError(format!("failed listing {root:?}: {exc}")))?;
    for entry in read_dir {
        let entry =
            entry.map_err(|exc| SharedRuntimeError(format!("failed reading dir entry: {exc}")))?;
        let p = entry.path();
        if p.is_dir() && !p.is_symlink() {
            out.extend(collect_tree_postorder(&p)?);
        }
        out.push(p);
    }
    Ok(out)
}

/// Parse an ISO-8601 UTC timestamp string (e.g. `"2099-01-01T00:00:00Z"` or `+00:00`) into UTC nanoseconds.
pub fn parse_utc_timestamp_ns(value: &str) -> Result<i128, SharedRuntimeError> {
    if value.is_empty() {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc is invalid".to_owned(),
        ));
    }
    let (dt_part, tz_part) = if let Some(stripped) = value.strip_suffix('Z') {
        (stripped, Some(0_i64))
    } else if let Some(stripped) = value.strip_suffix('z') {
        (stripped, Some(0_i64))
    } else if let Some(pos) = value.rfind(['+', '-']) {
        // Must be after date/time 'T' or ' '
        if pos > 10 {
            let tz_str = &value[pos..];
            let sign: i64 = if tz_str.starts_with('-') { -1 } else { 1 };
            let rest = &tz_str[1..];
            let (hh, mm) = rest
                .split_once(':')
                .and_then(|(h, m)| Some((h.parse::<i64>().ok()?, m.parse::<i64>().ok()?)))
                .ok_or_else(|| {
                    SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned())
                })?;
            (&value[..pos], Some(sign * (hh * 3600 + mm * 60)))
        } else {
            (value, None)
        }
    } else {
        (value, None)
    };
    let Some(offset_s) = tz_part else {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc must be UTC".to_owned(),
        ));
    };
    if offset_s != 0 {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc must be UTC".to_owned(),
        ));
    }

    let (date_str, time_str) = dt_part.split_once(['T', 't', ' ']).ok_or_else(|| {
        SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned())
    })?;
    let date_parts: Vec<&str> = date_str.split('-').collect();
    if date_parts.len() != 3
        || date_parts[0].len() != 4
        || date_parts[1].len() != 2
        || date_parts[2].len() != 2
    {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc is invalid".to_owned(),
        ));
    }
    let year: i64 = date_parts[0]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    let month: u32 = date_parts[1]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    let day: u32 = date_parts[2]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc is invalid".to_owned(),
        ));
    }

    let (hms_str, frac_ns) = if let Some((h, f)) = time_str.split_once('.') {
        if f.is_empty() || !f.bytes().all(|b| b.is_ascii_digit()) {
            return Err(SharedRuntimeError(
                "diagnostic batch frozen_at_utc is invalid".to_owned(),
            ));
        }
        let mut padded = f.chars().take(9).collect::<String>();
        while padded.len() < 9 {
            padded.push('0');
        }
        let ns: i128 = padded.parse().map_err(|_| {
            SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned())
        })?;
        (h, ns)
    } else {
        (time_str, 0_i128)
    };
    let hms_parts: Vec<&str> = hms_str.split(':').collect();
    if hms_parts.len() != 3 {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc is invalid".to_owned(),
        ));
    }
    let hour: i64 = hms_parts[0]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    let min: i64 = hms_parts[1]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    let sec: i64 = hms_parts[2]
        .parse()
        .map_err(|_| SharedRuntimeError("diagnostic batch frozen_at_utc is invalid".to_owned()))?;
    if !(0..=23).contains(&hour) || !(0..=59).contains(&min) || !(0..=59).contains(&sec) {
        return Err(SharedRuntimeError(
            "diagnostic batch frozen_at_utc is invalid".to_owned(),
        ));
    }

    let days = days_from_civil(year, month, day);
    let secs = i128::from(days) * 86_400
        + i128::from(hour) * 3_600
        + i128::from(min) * 60
        + i128::from(sec);
    Ok(secs * 1_000_000_000 + frac_ns)
}

/// Howard Hinnant's civil-to-days algorithm (days since 1970-01-01).
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = y - i64::from(m <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let m_adj = i64::from(m) + if m > 2 { -3 } else { 9 }; // [0, 11]
    let doy = (153 * m_adj + 2) / 5 + i64::from(d) - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

pub fn verify_artifact_chronology(
    frozen_at_ns: i128,
    artifacts: &[(PathBuf, String)],
) -> Result<(), SharedRuntimeError> {
    for (path, _) in artifacts {
        let meta = std::fs::metadata(path).map_err(|exc| {
            SharedRuntimeError(format!("artifact stat failed ({}): {exc}", path.display()))
        })?;
        let modified = meta
            .modified()
            .map_err(|exc| SharedRuntimeError(format!("artifact mtime unavailable: {exc}")))?;
        let mtime_ns: i128 = match modified.duration_since(UNIX_EPOCH) {
            Ok(d) => i128::from(d.as_secs()) * 1_000_000_000 + i128::from(d.subsec_nanos()),
            Err(e) => {
                let d = e.duration();
                -(i128::from(d.as_secs()) * 1_000_000_000 + i128::from(d.subsec_nanos()))
            }
        };
        if mtime_ns > frozen_at_ns {
            return Err(SharedRuntimeError(
                "diagnostic freeze timestamp predates a bound artifact".to_owned(),
            ));
        }
    }
    Ok(())
}
