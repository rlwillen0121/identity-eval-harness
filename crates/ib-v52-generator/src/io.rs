//! Sealed dataset serialization, `SPEC-v5.2` §2.2 seals (`G-SEAL-V52`), and verified loading.
//!
//! - **Public suites** (`v52_dev`, `v52_lite`, `v52_blind_fixture`) write one full seal, `SEAL.json`.
//! - **Private suites** (`v52_core`, `v52_full`) write a seed-free public seal, `SEAL.json`, carrying the
//!   commit–reveal binding (`c_k`, `drand_chain_hash`, `round`, `round_unix_time`,
//!   `beacon_randomness_hex`), plus a full private seal, `PRIVATE_SEAL.json`, that is written only to
//!   the operator's output directory and never committed.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use ib_core::hashing::sha256_hex;
use ib_v52_schemas::{
    CONTRACT_ID_V52, EpisodeV52, GENERATOR_VERSION_V52, PROTOCOL_ID_V52, SPEC_VERSION_V52,
    SealedEpisodeOracleV52, V52Suite,
};
use serde::{Deserialize, Serialize};

use crate::builder::{GeneratedV52Dataset, PrivateSuiteCommitmentV52};
use crate::cells::V52Cell;
use crate::error::V52GeneratorError;
use crate::seeds::decode_hex32;

pub const EPISODES_FILENAME_V52: &str = "episodes.jsonl";
pub const ORACLES_FILENAME_V52: &str = "oracles.jsonl";
pub const SEAL_FILENAME_V52: &str = "SEAL.json";
/// Full seal of a private suite, written only to the operator's output directory (§2.2).
pub const PRIVATE_SEAL_FILENAME_V52: &str = "PRIVATE_SEAL.json";
/// Commit file pushed before the beacon round (§2.2).
pub const PRECOMMIT_FILENAME_V52: &str = "PRECOMMIT.json";

/// Seed-derivation key label recorded in a full seal.
pub const SEED_KEY_PUBLIC_V52: &str = "S_pub";
/// Seed-derivation key label recorded in a full private seal.
pub const SEED_KEY_PRIVATE_V52: &str = "S_priv";

/// Seal visibility discriminator of a full seal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SealVisibilityV52 {
    Public,
    Private,
}

/// A cell with its seed material (full seals only).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeededSealCellV52 {
    pub episode_id: String,
    pub family_id: String,
    pub regime_id: String,
    /// Two-digit slot (`"00"`, `"01"`).
    pub seed_slot: String,
    /// §2.1 collision suffix `n`: the `seed_slot` text in `M` was `"<seed_slot>+n"`; `0` means none.
    pub collision_suffix: u32,
    pub base_seed: u64,
}

/// A cell of a private suite's public seal: only the `(family, regime, seed_slot)` coordinates.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SealCellV52 {
    pub family_id: String,
    pub regime_id: String,
    pub seed_slot: String,
}

/// Full seal: `SEAL.json` of a public suite, or `PRIVATE_SEAL.json` of a private suite.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatasetSealV52 {
    pub suite_id: String,
    pub visibility: SealVisibilityV52,
    pub episode_count: usize,
    pub cells: Vec<SeededSealCellV52>,
    pub episode_seeds: Vec<u64>,
    pub episodes_sha256: String,
    pub oracles_sha256: String,
    pub spec_version: String,
    pub spec_sha256: String,
    pub contract_id: String,
    pub protocol_id: String,
    pub generator_version: String,
    /// `"S_pub"` or `"S_priv"`.
    pub seed_key: String,
    /// Commit–reveal binding (private suites only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub private_commitment: Option<PrivateSuiteCommitmentV52>,
}

/// Public seal of a private suite (`SEAL.json`, §2.2). Contains no seed material: no `master_seed`,
/// no `episode_seeds`, no collision suffixes, and no per-episode identifier other than its cell.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrivateSuitePublicSealV52 {
    pub suite_id: String,
    pub episode_count: usize,
    pub cells: Vec<SealCellV52>,
    pub episodes_sha256: String,
    pub oracles_sha256: String,
    pub spec_sha256: String,
    pub contract_id: String,
    pub protocol_id: String,
    pub generator_version: String,
    pub c_k: String,
    pub drand_chain_hash: String,
    pub round: u64,
    pub round_unix_time: u64,
    pub beacon_randomness_hex: String,
}

/// The seal(s) of a loaded dataset.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadedSealV52 {
    /// A public suite's full seal.
    Public(Box<DatasetSealV52>),
    /// A private suite: the public seal, plus the full private seal when the operator holds it.
    Private {
        public_seal: Box<PrivateSuitePublicSealV52>,
        full_seal: Option<Box<DatasetSealV52>>,
    },
}

impl LoadedSealV52 {
    #[must_use]
    pub fn suite_id(&self) -> &str {
        match self {
            Self::Public(seal) => &seal.suite_id,
            Self::Private { public_seal, .. } => &public_seal.suite_id,
        }
    }

    /// The full seal, if held (always for public suites).
    #[must_use]
    pub fn full_seal(&self) -> Option<&DatasetSealV52> {
        match self {
            Self::Public(seal) => Some(seal.as_ref()),
            Self::Private { full_seal, .. } => full_seal.as_deref(),
        }
    }
}

/// Seals written by [`write_v52_dataset`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WrittenSealsV52 {
    /// The full seal (`SEAL.json` for public suites, `PRIVATE_SEAL.json` for private suites).
    pub full_seal: DatasetSealV52,
    /// The seed-free public seal of a private suite (`SEAL.json`).
    pub private_public_seal: Option<PrivateSuitePublicSealV52>,
}

/// Verified sealed v5.2 dataset loaded from disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoadedV52Dataset {
    pub dataset_dir: PathBuf,
    pub seal: LoadedSealV52,
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

fn seeded_cell(cell: &V52Cell) -> SeededSealCellV52 {
    SeededSealCellV52 {
        episode_id: cell.episode_id.clone(),
        family_id: cell.family_id.clone(),
        regime_id: cell.regime_id.clone(),
        seed_slot: format!("{:02}", cell.seed_slot),
        collision_suffix: cell.collision_suffix,
        base_seed: cell.seed,
    }
}

/// Build the full seal of `dataset` from its serialized bytes.
///
/// # Errors
/// Returns [`V52GeneratorError::InvalidConfig`] if the visibility of the suite and the presence of
/// a private commitment disagree.
pub fn build_full_seal(
    dataset: &GeneratedV52Dataset,
    episodes_bytes: &[u8],
    oracles_bytes: &[u8],
    spec_sha256: &str,
) -> Result<DatasetSealV52, V52GeneratorError> {
    let private = dataset.suite.is_private();
    if private != dataset.private_commitment.is_some() {
        return Err(V52GeneratorError::InvalidConfig(format!(
            "{} requires {} commitment",
            dataset.suite,
            if private { "a private" } else { "no private" }
        )));
    }
    Ok(DatasetSealV52 {
        suite_id: dataset.suite.as_str().to_owned(),
        visibility: if private {
            SealVisibilityV52::Private
        } else {
            SealVisibilityV52::Public
        },
        episode_count: dataset.episodes.len(),
        cells: dataset.cells.iter().map(seeded_cell).collect(),
        episode_seeds: dataset.cells.iter().map(|c| c.seed).collect(),
        episodes_sha256: sha256_hex(episodes_bytes),
        oracles_sha256: sha256_hex(oracles_bytes),
        spec_version: SPEC_VERSION_V52.to_owned(),
        spec_sha256: spec_sha256.to_owned(),
        contract_id: CONTRACT_ID_V52.to_owned(),
        protocol_id: PROTOCOL_ID_V52.to_owned(),
        generator_version: GENERATOR_VERSION_V52.to_owned(),
        seed_key: if private {
            SEED_KEY_PRIVATE_V52
        } else {
            SEED_KEY_PUBLIC_V52
        }
        .to_owned(),
        private_commitment: dataset.private_commitment.clone(),
    })
}

/// Derive the seed-free public seal of a private suite from its full seal.
///
/// # Errors
/// Returns [`V52GeneratorError::InvalidConfig`] if the full seal is not a private seal.
pub fn derive_private_public_seal(
    full: &DatasetSealV52,
) -> Result<PrivateSuitePublicSealV52, V52GeneratorError> {
    let Some(commitment) = full.private_commitment.as_ref() else {
        return Err(V52GeneratorError::InvalidConfig(
            "a private public seal requires a private commitment".to_owned(),
        ));
    };
    Ok(PrivateSuitePublicSealV52 {
        suite_id: full.suite_id.clone(),
        episode_count: full.episode_count,
        cells: full
            .cells
            .iter()
            .map(|c| SealCellV52 {
                family_id: c.family_id.clone(),
                regime_id: c.regime_id.clone(),
                seed_slot: c.seed_slot.clone(),
            })
            .collect(),
        episodes_sha256: full.episodes_sha256.clone(),
        oracles_sha256: full.oracles_sha256.clone(),
        spec_sha256: full.spec_sha256.clone(),
        contract_id: full.contract_id.clone(),
        protocol_id: full.protocol_id.clone(),
        generator_version: full.generator_version.clone(),
        c_k: commitment.c_k.clone(),
        drand_chain_hash: commitment.drand_chain_hash.clone(),
        round: commitment.round,
        round_unix_time: commitment.round_unix_time,
        beacon_randomness_hex: commitment.beacon_randomness_hex.clone(),
    })
}

/// Write `bytes` atomically: a synced sibling temp file renamed over `path`, so an interrupted
/// build never leaves a truncated dataset or seal.
fn write_file(path: &Path, bytes: &[u8]) -> Result<(), V52GeneratorError> {
    let io_err = |source| V52GeneratorError::Io {
        path: path.display().to_string(),
        source,
    };
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("artifact");
    let tmp = path.with_file_name(format!(".{file_name}.tmp.{}", std::process::id()));
    let written = fs::File::create(&tmp)
        .and_then(|mut file| {
            std::io::Write::write_all(&mut file, bytes)?;
            file.sync_all()
        })
        .and_then(|()| fs::rename(&tmp, path));
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    written.map_err(io_err)
}

/// Pretty-printed JSON plus one LF: the exact bytes of every written seal file.
///
/// # Errors
/// Returns [`V52GeneratorError::Json`] when `value` cannot be serialized.
pub fn pretty_json<T: Serialize>(value: &T) -> Result<String, V52GeneratorError> {
    Ok(format!("{}\n", serde_json::to_string_pretty(value)?))
}

/// Write `episodes.jsonl`, `oracles.jsonl`, and the seal(s) into `out_dir`.
///
/// `spec_path` should point to `SPEC-v5.2.md` so `spec_sha256` binds the normative spec verbatim.
///
/// # Errors
/// Returns [`V52GeneratorError::Io`] or [`V52GeneratorError::Json`] on I/O or encoding failures.
pub fn write_v52_dataset(
    dataset: &GeneratedV52Dataset,
    out_dir: &Path,
    spec_path: &Path,
) -> Result<WrittenSealsV52, V52GeneratorError> {
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
    let full_seal = build_full_seal(dataset, &episodes_bytes, &oracles_bytes, &spec_sha256)?;

    write_file(&out_dir.join(EPISODES_FILENAME_V52), &episodes_bytes)?;
    write_file(&out_dir.join(ORACLES_FILENAME_V52), &oracles_bytes)?;

    let private_public_seal = if dataset.suite.is_private() {
        let public_seal = derive_private_public_seal(&full_seal)?;
        write_file(
            &out_dir.join(SEAL_FILENAME_V52),
            pretty_json(&public_seal)?.as_bytes(),
        )?;
        write_file(
            &out_dir.join(PRIVATE_SEAL_FILENAME_V52),
            pretty_json(&full_seal)?.as_bytes(),
        )?;
        Some(public_seal)
    } else {
        write_file(
            &out_dir.join(SEAL_FILENAME_V52),
            pretty_json(&full_seal)?.as_bytes(),
        )?;
        None
    };

    Ok(WrittenSealsV52 {
        full_seal,
        private_public_seal,
    })
}

fn read_file(path: &Path) -> Result<Vec<u8>, V52GeneratorError> {
    fs::read(path).map_err(|source| V52GeneratorError::Io {
        path: path.display().to_string(),
        source,
    })
}

/// Parse `SEAL.json` bytes into the full seal of a public suite or the public seal of a private suite.
///
/// # Errors
/// Returns [`V52GeneratorError::Json`] or [`V52GeneratorError::SealVerification`].
pub fn parse_seal_v52(
    seal_bytes: &[u8],
) -> Result<Result<DatasetSealV52, PrivateSuitePublicSealV52>, V52GeneratorError> {
    let value: serde_json::Value = serde_json::from_slice(seal_bytes)?;
    if value.get("c_k").is_some() {
        Ok(Err(serde_json::from_value(value)?))
    } else {
        let seal: DatasetSealV52 = serde_json::from_value(value)?;
        if seal.visibility != SealVisibilityV52::Public {
            return Err(V52GeneratorError::SealVerification(
                "SEAL.json holds a private full seal; private suites publish only the seed-free public seal"
                    .to_owned(),
            ));
        }
        Ok(Ok(seal))
    }
}

/// Load and cryptographically verify a sealed v5.2 dataset directory.
///
/// If `spec_path` is provided, also verifies `spec_sha256` against the file at `spec_path`.
/// For a private suite, `PRIVATE_SEAL.json` is loaded and cross-checked when present.
///
/// # Errors
/// Returns [`V52GeneratorError::SealVerification`], [`V52GeneratorError::Io`], or [`V52GeneratorError::Json`].
pub fn load_v52_dataset(
    dataset_dir: &Path,
    spec_path: Option<&Path>,
) -> Result<LoadedV52Dataset, V52GeneratorError> {
    let seal_bytes = read_file(&dataset_dir.join(SEAL_FILENAME_V52))?;
    let episodes_bytes = read_file(&dataset_dir.join(EPISODES_FILENAME_V52))?;
    let oracles_bytes = read_file(&dataset_dir.join(ORACLES_FILENAME_V52))?;

    let seal = match parse_seal_v52(&seal_bytes)? {
        Ok(full) => {
            verify_v52_seal(&full, &episodes_bytes, &oracles_bytes, spec_path)?;
            LoadedSealV52::Public(Box::new(full))
        }
        Err(public_seal) => {
            verify_private_public_seal(&public_seal, &episodes_bytes, &oracles_bytes, spec_path)?;
            let private_path = dataset_dir.join(PRIVATE_SEAL_FILENAME_V52);
            let full_seal =
                if private_path
                    .try_exists()
                    .map_err(|source| V52GeneratorError::Io {
                        path: private_path.display().to_string(),
                        source,
                    })?
                {
                    let full: DatasetSealV52 = serde_json::from_slice(&read_file(&private_path)?)?;
                    verify_v52_seal(&full, &episodes_bytes, &oracles_bytes, spec_path)?;
                    if derive_private_public_seal(&full)? != public_seal {
                        return Err(V52GeneratorError::SealVerification(
                            "PRIVATE_SEAL.json disagrees with the public SEAL.json".to_owned(),
                        ));
                    }
                    Some(Box::new(full))
                } else {
                    None
                };
            LoadedSealV52::Private {
                public_seal: Box::new(public_seal),
                full_seal,
            }
        }
    };

    let episodes = parse_jsonl::<EpisodeV52>(&episodes_bytes)?;
    let oracles = parse_jsonl::<SealedEpisodeOracleV52>(&oracles_bytes)?;

    let expected_count = match &seal {
        LoadedSealV52::Public(full) => full.episode_count,
        LoadedSealV52::Private { public_seal, .. } => public_seal.episode_count,
    };
    if episodes.len() != expected_count || oracles.len() != expected_count {
        return Err(V52GeneratorError::SealVerification(format!(
            "record count mismatch: seal={expected_count}, episodes={}, oracles={}",
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
        if ep.suite_id != seal.suite_id() {
            return Err(V52GeneratorError::SealVerification(format!(
                "episode '{}' suite_id '{}' != seal suite_id '{}'",
                ep.episode_id,
                ep.suite_id,
                seal.suite_id()
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

fn check_eq(label: &str, actual: &str, expected: &str) -> Result<(), V52GeneratorError> {
    if actual == expected {
        Ok(())
    } else {
        Err(V52GeneratorError::SealVerification(format!(
            "unexpected {label} '{actual}', expected '{expected}'"
        )))
    }
}

fn check_digest(label: &str, bytes: &[u8], sealed: &str) -> Result<(), V52GeneratorError> {
    let actual = sha256_hex(bytes);
    if actual == sealed {
        Ok(())
    } else {
        Err(V52GeneratorError::SealVerification(format!(
            "{label} sha256 mismatch: expected {sealed}, got {actual}"
        )))
    }
}

fn check_spec(sealed: &str, spec_path: Option<&Path>) -> Result<(), V52GeneratorError> {
    if let Some(sp) = spec_path {
        check_digest("SPEC-v5.2.md", &read_file(sp)?, sealed)?;
    }
    Ok(())
}

fn check_suite(suite_id: &str, private: bool) -> Result<V52Suite, V52GeneratorError> {
    let suite: V52Suite = suite_id
        .parse()
        .map_err(V52GeneratorError::SealVerification)?;
    if suite.is_private() != private {
        return Err(V52GeneratorError::SealVerification(format!(
            "{suite_id} has the wrong seal kind (private suite: {})",
            suite.is_private()
        )));
    }
    Ok(suite)
}

/// Verify a full seal's digests, identifiers, cells and seeds.
///
/// # Errors
/// Returns [`V52GeneratorError::SealVerification`] if any digest or metadata check fails.
pub fn verify_v52_seal(
    seal: &DatasetSealV52,
    episodes_bytes: &[u8],
    oracles_bytes: &[u8],
    spec_path: Option<&Path>,
) -> Result<(), V52GeneratorError> {
    let private = seal.visibility == SealVisibilityV52::Private;
    let suite = check_suite(&seal.suite_id, private)?;
    check_eq("spec_version", &seal.spec_version, SPEC_VERSION_V52)?;
    check_eq(
        "generator_version",
        &seal.generator_version,
        GENERATOR_VERSION_V52,
    )?;
    check_eq("contract_id", &seal.contract_id, CONTRACT_ID_V52)?;
    check_eq("protocol_id", &seal.protocol_id, PROTOCOL_ID_V52)?;
    check_eq(
        "seed_key",
        &seal.seed_key,
        if private {
            SEED_KEY_PRIVATE_V52
        } else {
            SEED_KEY_PUBLIC_V52
        },
    )?;
    if private != seal.private_commitment.is_some() {
        return Err(V52GeneratorError::SealVerification(
            "private_commitment must be present exactly on private full seals".to_owned(),
        ));
    }
    check_digest("episodes.jsonl", episodes_bytes, &seal.episodes_sha256)?;
    check_digest("oracles.jsonl", oracles_bytes, &seal.oracles_sha256)?;
    check_spec(&seal.spec_sha256, spec_path)?;

    if seal.episode_count != suite.episode_count()
        || seal.cells.len() != seal.episode_count
        || seal.episode_seeds.len() != seal.episode_count
    {
        return Err(V52GeneratorError::SealVerification(format!(
            "{}: episode_count={}, cells={}, episode_seeds={}, expected {}",
            seal.suite_id,
            seal.episode_count,
            seal.cells.len(),
            seal.episode_seeds.len(),
            suite.episode_count()
        )));
    }
    for (cell, seed) in seal.cells.iter().zip(&seal.episode_seeds) {
        if cell.base_seed != *seed {
            return Err(V52GeneratorError::SealVerification(format!(
                "{}: cell base_seed disagrees with episode_seeds",
                cell.episode_id
            )));
        }
    }
    let unique_seeds: HashSet<u64> = seal.episode_seeds.iter().copied().collect();
    if unique_seeds.len() != seal.episode_count {
        return Err(V52GeneratorError::SealVerification(format!(
            "duplicate seeds detected in seal: {} unique for {} episodes",
            unique_seeds.len(),
            seal.episode_count
        )));
    }
    Ok(())
}

/// Verify a private suite's public seal: digests, identifiers, well-formed commitment hex, cell
/// coordinates, and the absence of seed material.
///
/// # Errors
/// Returns [`V52GeneratorError::SealVerification`] if any check fails.
pub fn verify_private_public_seal(
    seal: &PrivateSuitePublicSealV52,
    episodes_bytes: &[u8],
    oracles_bytes: &[u8],
    spec_path: Option<&Path>,
) -> Result<(), V52GeneratorError> {
    let suite = check_suite(&seal.suite_id, true)?;
    check_eq(
        "generator_version",
        &seal.generator_version,
        GENERATOR_VERSION_V52,
    )?;
    check_eq("contract_id", &seal.contract_id, CONTRACT_ID_V52)?;
    check_eq("protocol_id", &seal.protocol_id, PROTOCOL_ID_V52)?;
    for (label, hex_text) in [
        ("c_k", &seal.c_k),
        ("drand_chain_hash", &seal.drand_chain_hash),
        ("beacon_randomness_hex", &seal.beacon_randomness_hex),
    ] {
        decode_hex32(hex_text)
            .map_err(|e| V52GeneratorError::SealVerification(format!("{label}: {e}")))?;
    }
    check_digest("episodes.jsonl", episodes_bytes, &seal.episodes_sha256)?;
    check_digest("oracles.jsonl", oracles_bytes, &seal.oracles_sha256)?;
    check_spec(&seal.spec_sha256, spec_path)?;
    let expected: Vec<SealCellV52> = crate::cells::suite_cell_coordinates(suite)
        .into_iter()
        .map(|(f, r, slot)| SealCellV52 {
            family_id: f.to_owned(),
            regime_id: r.to_owned(),
            seed_slot: format!("{slot:02}"),
        })
        .collect();
    if seal.episode_count != suite.episode_count() || seal.cells != expected {
        return Err(V52GeneratorError::SealVerification(format!(
            "{}: public seal cells do not match the §2 cell plan",
            seal.suite_id
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
