//! Sealed IdentityBench v5 dataset loader and seal verifier.
//!
//! Ports `load_v5_dataset` / `DatasetArtifactsV5` / `V5ArtifactError` from
//! `src/identitybench/v5/validation.py` lines 1-533.

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use ib_core::hashing::sha256_hex;
use ib_core::jcs_dumps;
use ib_core::pyjson::{PyNumber, classify_number};
use ib_v5_schemas::{
    EpisodeV5, SealedEpisodeOracleV5, StrictV5Model, V5_BASE_SPEC_SHA256, V5_DRAFT5_SPEC_SHA256,
    validate_finite_json_tree,
};
use regex::Regex;
use serde_json::{Map, Value};

use crate::error::V5ArtifactError;

pub const SEAL_FILENAME_V5: &str = "SEAL.json";
pub const EPISODES_FILENAME_V5: &str = "episodes.jsonl";
pub const ORACLES_FILENAME_V5: &str = "oracles.jsonl";

const ACCEPTED_SEAL_SPEC_SHA256_V5: [&str; 2] = [V5_BASE_SPEC_SHA256, V5_DRAFT5_SPEC_SHA256];

const SEAL_REQUIRED_KEYS_V5: [&str; 8] = [
    "suite",
    "seed",
    "episode_count",
    "family_count",
    "episodes_sha256",
    "oracles_sha256",
    "spec_version",
    "generator_params",
];

const SEAL_OPTIONAL_KEYS_V5: [&str; 5] = [
    "spec_sha256",
    "generator_version",
    "protocol_spec_sha256",
    "normative_contract_id",
    "seed_provenance",
];

const GENERATOR_PARAMS_OPTIONAL_KEYS_V5: [&str; 7] = [
    "protocol_spec_path",
    "protocol_spec_sha256",
    "normative_contract_id",
    "normative_contract",
    "family_regime_assignment",
    "base_world_seeds",
    "seed_provenance",
];

static SHA256_HEX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-f0-9]{64}\z").expect("valid sha256 regex"));

/// Loaded and verified sealed v5 dataset artifacts (`DatasetArtifactsV5`).
#[derive(Debug, Clone, PartialEq)]
pub struct LoadedV5Dataset {
    pub dataset_dir: PathBuf,
    pub seal: Map<String, Value>,
    pub seal_bytes: Vec<u8>,
    pub episodes_bytes: Vec<u8>,
    pub oracles_bytes: Vec<u8>,
    pub episodes: Vec<EpisodeV5>,
    pub oracles: Vec<SealedEpisodeOracleV5>,
}

/// Python-parity alias for [`LoadedV5Dataset`].
pub type DatasetArtifactsV5 = LoadedV5Dataset;

fn is_sensitive_provenance_path(path: &Path) -> bool {
    path.components().any(|comp| {
        let Component::Normal(os_str) = comp else {
            return false;
        };
        let part = os_str.to_string_lossy();
        let lower = part.to_lowercase();
        part == ".env"
            || part.starts_with(".env.")
            || lower.contains("credential")
            || lower.contains("secret")
            || lower.contains("token")
    })
}

fn skip_ws(bytes: &[u8], pos: &mut usize) {
    while let Some(&b) = bytes.get(*pos) {
        if matches!(b, b' ' | b'\t' | b'\r' | b'\n') {
            *pos += 1;
        } else {
            break;
        }
    }
}

fn scan_json_string(bytes: &[u8], pos: &mut usize) -> Result<String, V5ArtifactError> {
    let start = *pos;
    if bytes.get(*pos) != Some(&b'"') {
        return Err(V5ArtifactError::new("expected JSON string"));
    }
    *pos += 1;
    while let Some(&b) = bytes.get(*pos) {
        *pos += 1;
        match b {
            b'"' => {
                let raw = std::str::from_utf8(&bytes[start..*pos])
                    .map_err(|e| V5ArtifactError::new(format!("invalid UTF-8: {e}")))?;
                return serde_json::from_str::<String>(raw)
                    .map_err(|e| V5ArtifactError::new(format!("invalid JSON string: {e}")));
            }
            b'\\' if *pos < bytes.len() => {
                *pos += 1;
            }
            b'\\' => {}
            _ => {}
        }
    }
    Err(V5ArtifactError::new("unterminated JSON string"))
}

fn scan_duplicate_keys(bytes: &[u8], pos: &mut usize) -> Result<(), V5ArtifactError> {
    skip_ws(bytes, pos);
    match bytes.get(*pos) {
        Some(b'{') => {
            *pos += 1;
            skip_ws(bytes, pos);
            if bytes.get(*pos) == Some(&b'}') {
                *pos += 1;
                return Ok(());
            }
            let mut keys = HashSet::new();
            loop {
                skip_ws(bytes, pos);
                let key = scan_json_string(bytes, pos)?;
                if !keys.insert(key.clone()) {
                    return Err(V5ArtifactError::new(format!(
                        "duplicate JSON object key {key:?}"
                    )));
                }
                skip_ws(bytes, pos);
                if bytes.get(*pos) != Some(&b':') {
                    return Err(V5ArtifactError::new("expected ':' after JSON object key"));
                }
                *pos += 1;
                scan_duplicate_keys(bytes, pos)?;
                skip_ws(bytes, pos);
                match bytes.get(*pos) {
                    Some(b',') => *pos += 1,
                    Some(b'}') => {
                        *pos += 1;
                        return Ok(());
                    }
                    _ => return Err(V5ArtifactError::new("expected ',' or '}' in JSON object")),
                }
            }
        }
        Some(b'[') => {
            *pos += 1;
            skip_ws(bytes, pos);
            if bytes.get(*pos) == Some(&b']') {
                *pos += 1;
                return Ok(());
            }
            loop {
                scan_duplicate_keys(bytes, pos)?;
                skip_ws(bytes, pos);
                match bytes.get(*pos) {
                    Some(b',') => *pos += 1,
                    Some(b']') => {
                        *pos += 1;
                        return Ok(());
                    }
                    _ => return Err(V5ArtifactError::new("expected ',' or ']' in JSON array")),
                }
            }
        }
        Some(b'"') => {
            let _ = scan_json_string(bytes, pos)?;
            Ok(())
        }
        Some(_) => {
            while let Some(&b) = bytes.get(*pos) {
                if matches!(b, b' ' | b'\t' | b'\r' | b'\n' | b',' | b'}' | b']') {
                    break;
                }
                *pos += 1;
            }
            Ok(())
        }
        None => Ok(()),
    }
}

fn strict_json(raw: &[u8], label: &str) -> Result<Value, V5ArtifactError> {
    let text = std::str::from_utf8(raw)
        .map_err(|err| V5ArtifactError::new(format!("{label} is not valid UTF-8 JSON: {err}")))?;
    let value: Value = serde_json::from_str(text)
        .map_err(|err| V5ArtifactError::new(format!("{label} is not valid UTF-8 JSON: {err}")))?;
    validate_finite_json_tree(&value, "$").map_err(|err| {
        V5ArtifactError::new(format!("non-finite JSON number is forbidden: {err}"))
    })?;
    let mut pos = 0;
    scan_duplicate_keys(text.as_bytes(), &mut pos)?;
    Ok(value)
}

fn strict_json_int(value: &Value) -> Option<i64> {
    let Value::Number(n) = value else {
        return None;
    };
    match classify_number(n) {
        PyNumber::Int(text) => text.parse::<i64>().ok(),
        PyNumber::Float(_) => None,
    }
}

fn is_strict_json_int(value: &Value) -> bool {
    matches!(value, Value::Number(n) if matches!(classify_number(n), PyNumber::Int(_)))
}

fn parse_jsonl<T: StrictV5Model>(raw: &[u8], label: &str) -> Result<Vec<T>, V5ArtifactError> {
    if raw.is_empty() {
        return Err(V5ArtifactError::new(format!("{label} is empty")));
    }
    let mut records = Vec::new();
    let mut line_number = 0usize;
    let mut start = 0usize;
    while start < raw.len() {
        line_number += 1;
        let rel_nl = raw[start..].iter().position(|&b| b == b'\n');
        let Some(offset) = rel_nl else {
            return Err(V5ArtifactError::new(format!(
                "{label} line {line_number} is not newline-terminated"
            )));
        };
        let end = start + offset;
        let payload = &raw[start..end];
        start = end + 1;
        if payload.is_empty() {
            return Err(V5ArtifactError::new(format!(
                "{label} contains a blank line at {line_number}"
            )));
        }
        let parsed = strict_json(payload, &format!("{label} line {line_number}"))?;
        if !parsed.is_object() {
            return Err(V5ArtifactError::new(format!(
                "{label} line {line_number} must be a JSON object"
            )));
        }
        let canonical = jcs_dumps(&parsed)?;
        if canonical.as_slice() != payload {
            return Err(V5ArtifactError::new(format!(
                "{label} line {line_number} is not canonical JCS JSON"
            )));
        }
        let model = T::from_strict_json_value(&parsed).map_err(|err| {
            V5ArtifactError::new(format!(
                "{label} line {line_number} failed strict schema validation: {err}"
            ))
        })?;
        records.push(model);
    }
    Ok(records)
}

/// Read and strictly reparse all three sealed dataset artifacts (`SEAL.json`, `episodes.jsonl`, `oracles.jsonl`).
pub fn load_v5_dataset(dataset_dir: &Path) -> Result<LoadedV5Dataset, V5ArtifactError> {
    let root = std::fs::canonicalize(dataset_dir).map_err(|err| {
        V5ArtifactError::new(format!(
            "incomplete v5 dataset at {}: {err}",
            dataset_dir.display()
        ))
    })?;
    let artifact_names = [SEAL_FILENAME_V5, EPISODES_FILENAME_V5, ORACLES_FILENAME_V5];
    let mut resolved_paths = Vec::with_capacity(3);
    for name in artifact_names {
        let artifact_path = root.join(name);
        let resolved = std::fs::canonicalize(&artifact_path).map_err(|err| {
            V5ArtifactError::new(format!(
                "incomplete v5 dataset at {}: {err}",
                root.display()
            ))
        })?;
        if is_sensitive_provenance_path(&resolved) {
            return Err(V5ArtifactError::new(
                "dataset artifact path resolves to a sensitive location",
            ));
        }
        if !resolved.starts_with(&root) {
            return Err(V5ArtifactError::new(
                "dataset artifact path escapes its directory",
            ));
        }
        resolved_paths.push(resolved);
    }

    let seal_bytes = std::fs::read(&resolved_paths[0]).map_err(|err| {
        V5ArtifactError::new(format!(
            "incomplete v5 dataset at {}: {err}",
            root.display()
        ))
    })?;
    let episodes_bytes = std::fs::read(&resolved_paths[1]).map_err(|err| {
        V5ArtifactError::new(format!(
            "incomplete v5 dataset at {}: {err}",
            root.display()
        ))
    })?;
    let oracles_bytes = std::fs::read(&resolved_paths[2]).map_err(|err| {
        V5ArtifactError::new(format!(
            "incomplete v5 dataset at {}: {err}",
            root.display()
        ))
    })?;

    let seal_value = strict_json(&seal_bytes, SEAL_FILENAME_V5)?;
    let Value::Object(seal) = seal_value else {
        return Err(V5ArtifactError::new("SEAL.json must be a JSON object"));
    };

    let mut missing: Vec<&str> = SEAL_REQUIRED_KEYS_V5
        .iter()
        .copied()
        .filter(|k| !seal.contains_key(*k))
        .collect();
    if !missing.is_empty() {
        missing.sort_unstable();
        return Err(V5ArtifactError::new(format!(
            "SEAL.json lacks required fields: {}",
            missing.join(", ")
        )));
    }
    let mut extra: Vec<&str> = seal
        .keys()
        .map(String::as_str)
        .filter(|k| !SEAL_REQUIRED_KEYS_V5.contains(k) && !SEAL_OPTIONAL_KEYS_V5.contains(k))
        .collect();
    if !extra.is_empty() {
        extra.sort_unstable();
        return Err(V5ArtifactError::new(format!(
            "SEAL.json has unexpected fields: {}",
            extra.join(", ")
        )));
    }

    let seed = strict_json_int(&seal["seed"])
        .ok_or_else(|| V5ArtifactError::new("SEAL.json seed must be an integer"))?;
    let episode_count = strict_json_int(&seal["episode_count"])
        .ok_or_else(|| V5ArtifactError::new("SEAL.json episode_count must be an integer"))?;
    let family_count = strict_json_int(&seal["family_count"])
        .ok_or_else(|| V5ArtifactError::new("SEAL.json family_count must be an integer"))?;

    let suite = seal["suite"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| V5ArtifactError::new("SEAL.json suite must be a non-empty string"))?;
    if seed < 0 {
        return Err(V5ArtifactError::new("SEAL.json seed must be non-negative"));
    }
    if seal["spec_version"].as_str() != Some("SPEC-v5.0") {
        return Err(V5ArtifactError::new(
            "SEAL.json spec_version must equal 'SPEC-v5.0'",
        ));
    }

    let Some(generator_params) = seal["generator_params"].as_object() else {
        return Err(V5ArtifactError::new(
            "SEAL.json generator_params must contain suite and seed",
        ));
    };
    if !generator_params.contains_key("suite") || !generator_params.contains_key("seed") {
        return Err(V5ArtifactError::new(
            "SEAL.json generator_params must contain suite and seed",
        ));
    }
    let mut extra_gp: Vec<&str> = generator_params
        .keys()
        .map(String::as_str)
        .filter(|k| *k != "suite" && *k != "seed" && !GENERATOR_PARAMS_OPTIONAL_KEYS_V5.contains(k))
        .collect();
    if !extra_gp.is_empty() {
        extra_gp.sort_unstable();
        return Err(V5ArtifactError::new(format!(
            "SEAL.json generator_params has unexpected fields: {}",
            extra_gp.join(", ")
        )));
    }
    let gp_seed = generator_params.get("seed").and_then(strict_json_int);
    if generator_params.get("suite").and_then(Value::as_str) != Some(suite) || gp_seed != Some(seed)
    {
        return Err(V5ArtifactError::new(
            "SEAL.json generator_params disagree with normalized suite and seed",
        ));
    }

    for (name, payload) in [
        ("episodes_sha256", episodes_bytes.as_slice()),
        ("oracles_sha256", oracles_bytes.as_slice()),
    ] {
        let actual = sha256_hex(payload);
        let declared = seal[name]
            .as_str()
            .filter(|s| SHA256_HEX_RE.is_match(s))
            .ok_or_else(|| {
                V5ArtifactError::new(format!("SEAL.json {name} must be lowercase SHA-256"))
            })?;
        if declared != actual {
            return Err(V5ArtifactError::new(format!(
                "SEAL.json {name} does not match artifact bytes"
            )));
        }
    }

    if let Some(spec_sha_val) = seal.get("spec_sha256") {
        let valid = spec_sha_val.as_str().is_some_and(|s| {
            SHA256_HEX_RE.is_match(s) && ACCEPTED_SEAL_SPEC_SHA256_V5.contains(&s)
        });
        if !valid {
            return Err(V5ArtifactError::new(
                "SEAL.json spec_sha256 does not match normative SPEC-v5.md or SPEC-v5.0-draft.5.md",
            ));
        }
    }

    for (container_name, container) in [
        ("SEAL.json", &seal),
        ("SEAL.json generator_params", generator_params),
    ] {
        if let Some(proto_sha_val) = container.get("protocol_spec_sha256") {
            let valid = proto_sha_val.as_str().is_some_and(|s| {
                SHA256_HEX_RE.is_match(s) && ACCEPTED_SEAL_SPEC_SHA256_V5.contains(&s)
            });
            if !valid {
                return Err(V5ArtifactError::new(format!(
                    "{container_name} protocol_spec_sha256 must match a normative v5 spec digest"
                )));
            }
        }
        if let Some(contract_id_val) = container.get("normative_contract_id") {
            let valid = contract_id_val.as_str().is_some_and(|s| !s.is_empty());
            if !valid {
                return Err(V5ArtifactError::new(format!(
                    "{container_name} normative_contract_id must be a non-empty string"
                )));
            }
        }
        if let Some(sp) = container.get("seed_provenance")
            && !sp.is_object()
        {
            return Err(V5ArtifactError::new(format!(
                "{container_name} seed_provenance must be a JSON object"
            )));
        }
    }

    if let (Some(a), Some(b)) = (
        seal.get("protocol_spec_sha256"),
        generator_params.get("protocol_spec_sha256"),
    ) && a != b
    {
        return Err(V5ArtifactError::new(
            "SEAL.json protocol_spec_sha256 disagrees with generator_params.protocol_spec_sha256",
        ));
    }
    if let (Some(a), Some(b)) = (
        seal.get("normative_contract_id"),
        generator_params.get("normative_contract_id"),
    ) && a != b
    {
        return Err(V5ArtifactError::new(
            "SEAL.json normative_contract_id disagrees with generator_params.normative_contract_id",
        ));
    }
    if let Some(nc) = generator_params.get("normative_contract")
        && !nc.is_object()
    {
        return Err(V5ArtifactError::new(
            "SEAL.json generator_params.normative_contract must be a JSON object",
        ));
    }
    if let Some(base_seeds_val) = generator_params.get("base_world_seeds") {
        let valid = base_seeds_val
            .as_array()
            .is_some_and(|arr| arr.iter().all(is_strict_json_int));
        if !valid {
            return Err(V5ArtifactError::new(
                "SEAL.json generator_params.base_world_seeds must be integers",
            ));
        }
    }
    if let Some(fra) = generator_params.get("family_regime_assignment")
        && !fra.is_array()
    {
        return Err(V5ArtifactError::new(
            "SEAL.json generator_params.family_regime_assignment must be a JSON array",
        ));
    }

    let episodes: Vec<EpisodeV5> = parse_jsonl(&episodes_bytes, EPISODES_FILENAME_V5)?;
    let oracles: Vec<SealedEpisodeOracleV5> = parse_jsonl(&oracles_bytes, ORACLES_FILENAME_V5)?;

    let ep_count_usize = usize::try_from(episode_count)
        .map_err(|_| V5ArtifactError::new("SEAL.json episode_count must be non-negative"))?;
    if episodes.len() != ep_count_usize || oracles.len() != ep_count_usize {
        return Err(V5ArtifactError::new(
            "SEAL.json episode_count disagrees with JSONL row counts",
        ));
    }

    let episode_ids: Vec<&str> = episodes.iter().map(|e| e.episode_id.as_str()).collect();
    let oracle_ids: Vec<&str> = oracles.iter().map(|o| o.episode_id.as_str()).collect();
    let unique_ep_ids: HashSet<&str> = episode_ids.iter().copied().collect();
    let unique_or_ids: HashSet<&str> = oracle_ids.iter().copied().collect();
    if unique_ep_ids.len() != episode_ids.len() || unique_or_ids.len() != oracle_ids.len() {
        return Err(V5ArtifactError::new(
            "episode and oracle files must have unique episode IDs",
        ));
    }
    if episode_ids != oracle_ids {
        return Err(V5ArtifactError::new(
            "episode and oracle IDs or ordering differ",
        ));
    }

    let ep_suites: HashSet<&str> = episodes
        .iter()
        .map(|e| crate::generator::V5Suite::from(e.suite_id).as_str())
        .collect();
    if ep_suites.len() != 1 || !ep_suites.contains(suite) {
        return Err(V5ArtifactError::new(
            "SEAL.json suite disagrees with episode suite IDs",
        ));
    }

    let unique_families: HashSet<&str> = episodes.iter().map(|e| e.family_id.as_str()).collect();
    if i64::try_from(unique_families.len()).ok() != Some(family_count) {
        return Err(V5ArtifactError::new(
            "SEAL.json family_count disagrees with episode families",
        ));
    }

    Ok(LoadedV5Dataset {
        dataset_dir: root,
        seal,
        seal_bytes,
        episodes_bytes,
        oracles_bytes,
        episodes,
        oracles,
    })
}
