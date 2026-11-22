//! Sealed dataset serialization, cryptographic `SEAL.json`, and verified loading for IdentityBench v5.2.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use ib_core::hashing::sha256_hex;
use ib_v52_schemas::{
    CONTRACT_ID_V52, EpisodeV52, GENERATOR_VERSION_V52, SPEC_VERSION_V52, SealedEpisodeOracleV52,
};
use serde::{Deserialize, Serialize};

use crate::builder::GeneratedV52Dataset;
use crate::error::V52GeneratorError;

pub const EPISODES_FILENAME_V52: &str = "episodes.jsonl";
pub const ORACLES_FILENAME_V52: &str = "oracles.jsonl";
pub const SEAL_FILENAME_V52: &str = "SEAL.json";

/// Cryptographic dataset manifest serialized to `SEAL.json` (`G-SEAL-V52`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatasetSealV52 {
    pub suite: String,
    pub master_seed: u64,
    pub episode_count: usize,
    pub family_count: usize,
    pub episodes_sha256: String,
    pub oracles_sha256: String,
    pub spec_version: String,
    pub spec_sha256: String,
    pub generator_version: String,
    pub contract_id: String,
    pub episode_seeds: Vec<u64>,
}

/// Verified sealed v5.2 dataset loaded from disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoadedV52Dataset {
    pub dataset_dir: PathBuf,
    pub seal: DatasetSealV52,
    pub episodes_bytes: Vec<u8>,
    pub oracles_bytes: Vec<u8>,
    pub episodes: Vec<EpisodeV52>,
    pub oracles: Vec<SealedEpisodeOracleV52>,
}

/// Serialize a slice of records into canonical deterministic JSONL bytes (UTF-8, `\n`-terminated).
///
/// # Errors
/// Propagates `serde_json::Error`.
pub fn serialize_jsonl<T: Serialize>(items: &[T]) -> Result<Vec<u8>, V52GeneratorError> {
    let mut out = String::new();
    for item in items {
        let line = serde_json::to_string(item)?;
        out.push_str(&line);
        out.push('\n');
    }
    Ok(out.into_bytes())
}

/// Write `episodes.jsonl`, `oracles.jsonl`, and `SEAL.json` into `out_dir`.
///
/// `spec_path` should point to `SPEC-v5.2.md` so `spec_sha256` binds the normative spec verbatim.
///
/// # Errors
/// Returns [`V52GeneratorError::Io`] or [`V52GeneratorError::Json`] on I/O or encoding failures.
pub fn write_v52_dataset(
    dataset: &GeneratedV52Dataset,
    out_dir: &Path,
    spec_path: &Path,
) -> Result<DatasetSealV52, V52GeneratorError> {
    fs::create_dir_all(out_dir).map_err(|source| V52GeneratorError::Io {
        path: out_dir.display().to_string(),
        source,
    })?;

    let spec_bytes = fs::read(spec_path).map_err(|source| V52GeneratorError::Io {
        path: spec_path.display().to_string(),
        source,
    })?;
    let spec_sha256 = sha256_hex(&spec_bytes);

    let episodes_bytes = serialize_jsonl(&dataset.episodes)?;
    let oracles_bytes = serialize_jsonl(&dataset.oracles)?;

    let episodes_sha256 = sha256_hex(&episodes_bytes);
    let oracles_sha256 = sha256_hex(&oracles_bytes);

    let families: HashSet<&str> = dataset
        .episodes
        .iter()
        .map(|ep| ep.family_id.as_str())
        .collect();
    let episode_seeds: Vec<u64> = dataset.oracles.iter().map(|o| o.seed).collect();

    let seal = DatasetSealV52 {
        suite: dataset.suite.as_str().to_owned(),
        master_seed: dataset.master_seed,
        episode_count: dataset.episodes.len(),
        family_count: families.len(),
        episodes_sha256,
        oracles_sha256,
        spec_version: SPEC_VERSION_V52.to_owned(),
        spec_sha256,
        generator_version: GENERATOR_VERSION_V52.to_owned(),
        contract_id: CONTRACT_ID_V52.to_owned(),
        episode_seeds,
    };

    let seal_json = format!("{}\n", serde_json::to_string_pretty(&seal)?);

    let episodes_path = out_dir.join(EPISODES_FILENAME_V52);
    let oracles_path = out_dir.join(ORACLES_FILENAME_V52);
    let seal_path = out_dir.join(SEAL_FILENAME_V52);

    fs::write(&episodes_path, &episodes_bytes).map_err(|source| V52GeneratorError::Io {
        path: episodes_path.display().to_string(),
        source,
    })?;
    fs::write(&oracles_path, &oracles_bytes).map_err(|source| V52GeneratorError::Io {
        path: oracles_path.display().to_string(),
        source,
    })?;
    fs::write(&seal_path, seal_json.as_bytes()).map_err(|source| V52GeneratorError::Io {
        path: seal_path.display().to_string(),
        source,
    })?;

    Ok(seal)
}

/// Load and cryptographically verify a sealed v5.2 dataset directory (`episodes.jsonl`, `oracles.jsonl`, `SEAL.json`).
///
/// If `spec_path` is provided, also verifies `seal.spec_sha256` against the file at `spec_path`.
///
/// # Errors
/// Returns [`V52GeneratorError::SealVerification`], [`V52GeneratorError::Io`], or [`V52GeneratorError::Json`].
pub fn load_v52_dataset(
    dataset_dir: &Path,
    spec_path: Option<&Path>,
) -> Result<LoadedV52Dataset, V52GeneratorError> {
    let seal_path = dataset_dir.join(SEAL_FILENAME_V52);
    let episodes_path = dataset_dir.join(EPISODES_FILENAME_V52);
    let oracles_path = dataset_dir.join(ORACLES_FILENAME_V52);

    let seal_bytes = fs::read(&seal_path).map_err(|source| V52GeneratorError::Io {
        path: seal_path.display().to_string(),
        source,
    })?;
    let seal: DatasetSealV52 = serde_json::from_slice(&seal_bytes)?;

    let episodes_bytes = fs::read(&episodes_path).map_err(|source| V52GeneratorError::Io {
        path: episodes_path.display().to_string(),
        source,
    })?;
    let oracles_bytes = fs::read(&oracles_path).map_err(|source| V52GeneratorError::Io {
        path: oracles_path.display().to_string(),
        source,
    })?;

    verify_v52_seal(&seal, &episodes_bytes, &oracles_bytes, spec_path)?;

    let episodes = parse_jsonl::<EpisodeV52>(&episodes_bytes)?;
    let oracles = parse_jsonl::<SealedEpisodeOracleV52>(&oracles_bytes)?;

    if episodes.len() != seal.episode_count || oracles.len() != seal.episode_count {
        return Err(V52GeneratorError::SealVerification(format!(
            "record count mismatch: seal={}, episodes={}, oracles={}",
            seal.episode_count,
            episodes.len(),
            oracles.len()
        )));
    }

    for (ep, orc) in episodes.iter().zip(oracles.iter()) {
        if ep.episode_id != orc.episode_id {
            return Err(V52GeneratorError::SealVerification(format!(
                "episode/oracle ID mismatch: '{}' != '{}'",
                ep.episode_id, orc.episode_id
            )));
        }
    }

    Ok(LoadedV52Dataset {
        dataset_dir: dataset_dir.to_path_buf(),
        seal,
        episodes_bytes,
        oracles_bytes,
        episodes,
        oracles,
    })
}

/// Verify cryptographic digests and metadata invariants on `seal`.
///
/// # Errors
/// Returns [`V52GeneratorError::SealVerification`] if any digest or metadata check fails.
pub fn verify_v52_seal(
    seal: &DatasetSealV52,
    episodes_bytes: &[u8],
    oracles_bytes: &[u8],
    spec_path: Option<&Path>,
) -> Result<(), V52GeneratorError> {
    if seal.spec_version != SPEC_VERSION_V52 {
        return Err(V52GeneratorError::SealVerification(format!(
            "unexpected spec_version '{}', expected '{SPEC_VERSION_V52}'",
            seal.spec_version
        )));
    }
    if seal.generator_version != GENERATOR_VERSION_V52 {
        return Err(V52GeneratorError::SealVerification(format!(
            "unexpected generator_version '{}', expected '{GENERATOR_VERSION_V52}'",
            seal.generator_version
        )));
    }
    if seal.contract_id != CONTRACT_ID_V52 {
        return Err(V52GeneratorError::SealVerification(format!(
            "unexpected contract_id '{}', expected '{CONTRACT_ID_V52}'",
            seal.contract_id
        )));
    }

    let actual_ep = sha256_hex(episodes_bytes);
    if actual_ep != seal.episodes_sha256 {
        return Err(V52GeneratorError::SealVerification(format!(
            "episodes.jsonl sha256 mismatch: expected {}, got {actual_ep}",
            seal.episodes_sha256
        )));
    }
    let actual_orc = sha256_hex(oracles_bytes);
    if actual_orc != seal.oracles_sha256 {
        return Err(V52GeneratorError::SealVerification(format!(
            "oracles.jsonl sha256 mismatch: expected {}, got {actual_orc}",
            seal.oracles_sha256
        )));
    }

    if let Some(sp) = spec_path {
        let spec_bytes = fs::read(sp).map_err(|source| V52GeneratorError::Io {
            path: sp.display().to_string(),
            source,
        })?;
        let actual_spec = sha256_hex(&spec_bytes);
        if actual_spec != seal.spec_sha256 {
            return Err(V52GeneratorError::SealVerification(format!(
                "SPEC-v5.2.md sha256 mismatch: expected {}, got {actual_spec}",
                seal.spec_sha256
            )));
        }
    }

    let unique_seeds: HashSet<u64> = seal.episode_seeds.iter().copied().collect();
    if unique_seeds.len() != seal.episode_count {
        return Err(V52GeneratorError::SealVerification(format!(
            "duplicate seeds detected in SEAL.json: {} unique for {} episodes",
            unique_seeds.len(),
            seal.episode_count
        )));
    }

    Ok(())
}

fn parse_jsonl<T: for<'de> Deserialize<'de>>(bytes: &[u8]) -> Result<Vec<T>, V52GeneratorError> {
    let text = std::str::from_utf8(bytes)
        .map_err(|e| V52GeneratorError::SealVerification(format!("non-UTF8 JSONL: {e}")))?;
    let mut items = Vec::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        items.push(serde_json::from_str::<T>(trimmed)?);
    }
    Ok(items)
}
