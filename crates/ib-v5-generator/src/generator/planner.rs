//! Cell planner, deterministic ID/evidence/slice codebooks, and replica builder.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::str::FromStr;

use ib_core::PyRandom;
use ib_core::hashing::{sha256, sha256_hex, sha256_prefix_u64};
use ib_v5_schemas::{RegimeIdV5, StrictV5Model, SuiteIdV5};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::GeneratorError;

pub const V5_FAMILIES: [&str; 12] = [
    "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08", "F09", "F10", "F11", "F12",
];

pub const V5_REGIMES: [RegimeIdV5; 4] = [
    RegimeIdV5::R1,
    RegimeIdV5::R2,
    RegimeIdV5::R3,
    RegimeIdV5::R4,
];

pub const V5_SUITE_COUNTS: [(&str, usize); 4] = [
    ("v5_dev", 12),
    ("v5_lite", 24),
    ("v5_core", 48),
    ("v5_full", 96),
];

pub const V5_SUITE_SEED_OFFSETS: [(&str, u64); 4] = [
    ("v5_dev", 0),
    ("v5_lite", 100_000),
    ("v5_core", 200_000),
    ("v5_full", 300_000),
];

pub const DEFAULT_V5_SEED: u64 = 20_261_111;
pub const DEFAULT_V5_DRAFT5_SEED: u64 = 20_261_114;
pub const COMPACT_OBSERVATION_PROFILE_V1: &str = "dev-compact-observation-v1";

pub(crate) const ROLE_NAMES: [&str; 10] = [
    "pay",
    "wire",
    "deploy",
    "deploy_admin",
    "iam",
    "review",
    "read",
    "change",
    "decoy_a",
    "decoy_b",
];

const EVIDENCE_LABELS_V5: [&str; 105] = [
    "account:manager",
    "account:mover",
    "account:primary",
    "account:reviewer",
    "agent:target",
    "baseline_agent:existing",
    "binding:critical",
    "binding:safe",
    "birthright:base",
    "birthright:contractor_critical",
    "birthright:vendor_admin",
    "bnhi:parent",
    "bridge:mover",
    "campaign:first",
    "campaign:second",
    "ciem:task",
    "connector:0",
    "control:mover",
    "delegation:child",
    "delegation:root",
    "feed:legacy",
    "feed:spoke",
    "feed:workday",
    "grant:manager_admin",
    "grant:manager_review",
    "grant:mover_deploy",
    "grant:mover_pay",
    "grant:mover_wire",
    "grant:pay",
    "grant:reviewer_deploy_eligible",
    "grant:reviewer_opt_admin",
    "grant:reviewer_review",
    "grant:wire",
    "group:deny_wire",
    "group:review_child",
    "group:review_parent",
    "hrmap:mapping",
    "incident",
    "itsm:phantom",
    "jml:sealed:1",
    "jml:sealed:10",
    "jml:sealed:11",
    "jml:sealed:12",
    "jml:sealed:13",
    "jml:sealed:14",
    "jml:sealed:15",
    "jml:sealed:16",
    "jml:sealed:2",
    "jml:sealed:3",
    "jml:sealed:4",
    "jml:sealed:5",
    "jml:sealed:6",
    "jml:sealed:7",
    "jml:sealed:8",
    "jml:sealed:9",
    "jml:visible:1",
    "jml:visible:2",
    "jml:visible:3",
    "jml:visible:4",
    "nhi:target",
    "opt:p1",
    "opt:p2",
    "p1:app-decoy_a",
    "p1:app-decoy_b",
    "p2:decoy-campaign:decoy_a",
    "p2:decoy-campaign:decoy_b",
    "p2:decoy-item:decoy_a",
    "p2:decoy-item:decoy_b",
    "page:main",
    "person:alice:p1",
    "person:alice:p2-refresh",
    "person:approver:p1",
    "person:boomerang:p1",
    "person:boomerang:p2-refresh",
    "person:boomerang:p2-retro",
    "person:reviewer:p1",
    "request:first",
    "request:replay",
    "request:second",
    "role:change",
    "role:deploy",
    "role:deploy_admin",
    "role:iam",
    "role:pay",
    "role:read",
    "role:review",
    "role:wire",
    "session:prior",
    "signal:iac",
    "signal:parent",
    "signal:vault",
    "ticket:primary",
    "token:access",
    "token:obo",
    "token:refresh",
    "token:root",
    "uar:item:first:first",
    "uar:item:first:second",
    "uar:item:fourth:first",
    "uar:item:fourth:second",
    "uar:item:second:first",
    "uar:item:second:second",
    "uar:item:third:first",
    "uar:item:third:second",
    "workflow:task",
];

const REPLICA_LABELS_V5: [&str; 13] = [
    "p1:connector-main",
    "p1:dept-finance-control",
    "p1:dept-payroll-control",
    "p1:dept-bridge",
    "p1:app-finance",
    "p2:app-campaign",
    "p2:app-delegation",
    "p2:app-decoy_a-campaign",
    "p2:app-decoy_b-campaign",
    "p2:app-decoy_a-delegation",
    "p2:app-decoy_b-delegation",
    "p2:ticket-decoy_a",
    "p2:ticket-decoy_b",
];

const SLICE_LABELS_V5: [&str; 15] = [
    "p1-core",
    "p1-connector-main",
    "p1-dept-00",
    "p1-dept-01",
    "p1-dept-02",
    "p1-app-finance",
    "p1-app-unused-decoy_a",
    "p1-app-unused-decoy_b",
    "p2-core",
    "p2-app-finance",
    "p2-app-unused-decoy_a",
    "p2-app-unused-decoy_b",
    "p2-ticket-primary",
    "p2-ticket-unused-decoy_a",
    "p2-ticket-unused-decoy_b",
];

/// Preregistered v5 suite identifiers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum V5Suite {
    #[serde(rename = "v5_dev")]
    V5Dev,
    #[serde(rename = "v5_lite")]
    V5Lite,
    #[serde(rename = "v5_core")]
    V5Core,
    #[serde(rename = "v5_full")]
    V5Full,
}

impl V5Suite {
    pub const ALL: [Self; 4] = [Self::V5Dev, Self::V5Lite, Self::V5Core, Self::V5Full];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::V5Dev => "v5_dev",
            Self::V5Lite => "v5_lite",
            Self::V5Core => "v5_core",
            Self::V5Full => "v5_full",
        }
    }

    #[must_use]
    pub const fn count(self) -> usize {
        match self {
            Self::V5Dev => 12,
            Self::V5Lite => 24,
            Self::V5Core => 48,
            Self::V5Full => 96,
        }
    }

    #[must_use]
    pub const fn seed_offset(self) -> u64 {
        match self {
            Self::V5Dev => 0,
            Self::V5Lite => 100_000,
            Self::V5Core => 200_000,
            Self::V5Full => 300_000,
        }
    }

    #[must_use]
    pub const fn to_schema_suite_id(self) -> SuiteIdV5 {
        match self {
            Self::V5Dev => SuiteIdV5::V5Dev,
            Self::V5Lite => SuiteIdV5::V5Lite,
            Self::V5Core => SuiteIdV5::V5Core,
            Self::V5Full => SuiteIdV5::V5Full,
        }
    }
}

impl fmt::Display for V5Suite {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for V5Suite {
    type Err = GeneratorError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "v5_dev" => Ok(Self::V5Dev),
            "v5_lite" => Ok(Self::V5Lite),
            "v5_core" => Ok(Self::V5Core),
            "v5_full" => Ok(Self::V5Full),
            other => Err(GeneratorError::UnknownSuite(other.to_owned())),
        }
    }
}

impl From<SuiteIdV5> for V5Suite {
    fn from(value: SuiteIdV5) -> Self {
        match value {
            SuiteIdV5::V5Dev => Self::V5Dev,
            SuiteIdV5::V5Lite => Self::V5Lite,
            SuiteIdV5::V5Core => Self::V5Core,
            SuiteIdV5::V5Full => Self::V5Full,
        }
    }
}

/// One deterministic family/regime/seed cell in a preregistered split.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct V5Cell {
    pub suite: V5Suite,
    pub family_id: String,
    pub regime_id: RegimeIdV5,
    pub replicate: u64,
    pub seed: u64,
    pub episode_id: String,
}

/// Summary returned by [`verify_v5_split_seed_disjointness`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SplitDisjointnessSummary {
    pub seed: u64,
    pub against_historical_seed: Option<u64>,
    pub split_unique_seed_counts: BTreeMap<String, usize>,
    pub total_unique_base_world_seeds: usize,
    pub pairwise_disjoint: bool,
    pub disjoint_from_historical_seed: Option<bool>,
    pub v5_lite_regime_counts: BTreeMap<String, usize>,
    pub v5_lite_family_regimes: BTreeMap<String, Vec<RegimeIdV5>>,
}

#[must_use]
pub(crate) fn cell_seed(seed: u64, family: &str, replicate: u64) -> u64 {
    let material = format!("identitybench-v5\0{seed}\0{family}\0{replicate}");
    sha256_prefix_u64(material.as_bytes())
}

/// Return the exact `family x regime x replicate` layout required for `suite`.
#[must_use]
pub fn plan_v5_cells(suite: V5Suite, seed: u64) -> Vec<V5Cell> {
    let offset = suite.seed_offset();
    let specs: Vec<(&str, RegimeIdV5, u64)> = match suite {
        V5Suite::V5Dev => V5_FAMILIES
            .iter()
            .enumerate()
            .map(|(index, &family)| (family, V5_REGIMES[index % V5_REGIMES.len()], 0))
            .collect(),
        V5Suite::V5Lite => V5_FAMILIES
            .iter()
            .enumerate()
            .flat_map(|(family_index, &family)| {
                (0..2).map(move |regime_offset| {
                    (
                        family,
                        V5_REGIMES[(family_index + regime_offset) % 4],
                        0,
                    )
                })
            })
            .collect(),
        V5Suite::V5Core => V5_FAMILIES
            .iter()
            .flat_map(|&family| V5_REGIMES.iter().map(move |&regime| (family, regime, 0)))
            .collect(),
        V5Suite::V5Full => V5_FAMILIES
            .iter()
            .flat_map(|&family| {
                (0..2u64).flat_map(move |replicate| {
                    V5_REGIMES
                        .iter()
                        .map(move |&regime| (family, regime, replicate))
                })
            })
            .collect(),
    };

    let suite_str = suite.as_str();
    let suffix = suite_str.strip_prefix("v5_").unwrap_or(suite_str);
    let mut cells: Vec<V5Cell> = specs
        .into_iter()
        .map(|(family, regime, replicate)| {
            let derived_seed = seed + offset;
            let c_seed = cell_seed(derived_seed, family, replicate);
            let regime_str = regime_as_str(regime);
            let episode_material = format!(
                "identitybench-v5-episode\0{suite_str}\0{derived_seed}\0{family}\0{regime_str}\0{replicate}"
            );
            let episode_tag = &sha256_hex(episode_material.as_bytes())[..20];
            let episode_id = format!("v5_{suffix}_ep_{episode_tag}");
            V5Cell {
                suite,
                family_id: family.to_owned(),
                regime_id: regime,
                replicate,
                seed: c_seed,
                episode_id,
            }
        })
        .collect();

    assert_eq!(
        cells.len(),
        suite.count(),
        "invalid {suite_str} planner count: {}",
        cells.len()
    );
    cells.sort_by(|a, b| a.episode_id.cmp(&b.episode_id));
    cells
}

#[must_use]
pub(crate) const fn regime_as_str(regime: RegimeIdV5) -> &'static str {
    match regime {
        RegimeIdV5::R1 => "R1",
        RegimeIdV5::R2 => "R2",
        RegimeIdV5::R3 => "R3",
        RegimeIdV5::R4 => "R4",
    }
}

/// Verify split seed disjointness, `v5_lite` regime balance, and historical separation.
pub fn verify_v5_split_seed_disjointness(
    seed: u64,
    against_historical_seed: Option<u64>,
) -> Result<SplitDisjointnessSummary, GeneratorError> {
    let mut planned: BTreeMap<V5Suite, Vec<V5Cell>> = BTreeMap::new();
    for suite in V5Suite::ALL {
        planned.insert(suite, plan_v5_cells(suite, seed));
    }

    let mut split_seeds: BTreeMap<V5Suite, BTreeSet<u64>> = BTreeMap::new();
    let mut split_episode_ids: BTreeMap<V5Suite, BTreeSet<String>> = BTreeMap::new();
    for (&suite, cells) in &planned {
        let expected_seeds = match suite {
            V5Suite::V5Dev | V5Suite::V5Lite | V5Suite::V5Core => 12,
            V5Suite::V5Full => 24,
        };
        let seeds: BTreeSet<u64> = cells.iter().map(|c| c.seed).collect();
        if seeds.len() != expected_seeds {
            return Err(GeneratorError::DisjointnessViolation(format!(
                "{suite} expected {expected_seeds} unique base-world seeds, got {}",
                seeds.len()
            )));
        }
        split_seeds.insert(suite, seeds);
        split_episode_ids.insert(
            suite,
            cells.iter().map(|c| c.episode_id.clone()).collect(),
        );
    }

    let full_replicates: BTreeMap<(&str, u64), u64> = planned[&V5Suite::V5Full]
        .iter()
        .map(|c| ((c.family_id.as_str(), c.replicate), c.seed))
        .collect();
    let unique_full_seeds: BTreeSet<u64> = full_replicates.values().copied().collect();
    if full_replicates.len() != 24 || unique_full_seeds.len() != 24 {
        return Err(GeneratorError::DisjointnessViolation(
            "v5_full must have 24 distinct (family_id, replicate) base-world seeds".to_owned(),
        ));
    }

    let lite_cells = &planned[&V5Suite::V5Lite];
    let mut expected_lite_regimes: BTreeMap<String, Vec<RegimeIdV5>> = BTreeMap::new();
    for (idx, &family) in V5_FAMILIES.iter().enumerate() {
        let pair = vec![V5_REGIMES[idx % 4], V5_REGIMES[(idx + 1) % 4]];
        let family_cells: Vec<&V5Cell> = lite_cells
            .iter()
            .filter(|c| c.family_id == family)
            .collect();
        let family_seeds: BTreeSet<u64> = family_cells.iter().map(|c| c.seed).collect();
        if family_cells.len() != 2 || family_seeds.len() != 1 {
            return Err(GeneratorError::DisjointnessViolation(format!(
                "v5_lite family {family} must have 2 cells on 1 shared seed"
            )));
        }
        let actual_regimes: BTreeSet<RegimeIdV5> =
            family_cells.iter().map(|c| c.regime_id).collect();
        let expected_set: BTreeSet<RegimeIdV5> = pair.iter().copied().collect();
        if actual_regimes != expected_set {
            return Err(GeneratorError::DisjointnessViolation(format!(
                "v5_lite family {family} regimes {actual_regimes:?} != expected {pair:?}"
            )));
        }
        expected_lite_regimes.insert(family.to_owned(), pair);
    }

    let mut lite_regime_counts: BTreeMap<String, usize> = BTreeMap::new();
    for cell in lite_cells {
        *lite_regime_counts
            .entry(regime_as_str(cell.regime_id).to_owned())
            .or_insert(0) += 1;
    }
    if V5_REGIMES
        .iter()
        .any(|&r| lite_regime_counts.get(regime_as_str(r)).copied().unwrap_or(0) != 6)
    {
        return Err(GeneratorError::DisjointnessViolation(format!(
            "v5_lite regime counts {lite_regime_counts:?} must assign 6 cells per regime"
        )));
    }

    for (idx, &left) in V5Suite::ALL.iter().enumerate() {
        for &right in &V5Suite::ALL[idx + 1..] {
            let overlap: Vec<u64> = split_seeds[&left]
                .intersection(&split_seeds[&right])
                .copied()
                .collect();
            if !overlap.is_empty() {
                return Err(GeneratorError::DisjointnessViolation(format!(
                    "base-world seeds overlap between {left} and {right}: {overlap:?}"
                )));
            }
            let ep_overlap: Vec<&String> = split_episode_ids[&left]
                .intersection(&split_episode_ids[&right])
                .collect();
            if !ep_overlap.is_empty() {
                return Err(GeneratorError::DisjointnessViolation(format!(
                    "episode_ids overlap between {left} and {right}: {ep_overlap:?}"
                )));
            }
        }
    }

    let all_candidate_seeds: BTreeSet<u64> =
        split_seeds.values().flatten().copied().collect();
    let all_candidate_episodes: BTreeSet<&str> = split_episode_ids
        .values()
        .flatten()
        .map(String::as_str)
        .collect();

    let mut disjoint_from_historical = None;
    if let Some(hist_seed) = against_historical_seed
        && seed != hist_seed
    {
        let mut hist_seeds = BTreeSet::new();
        let mut hist_episodes = BTreeSet::new();
        for suite in V5Suite::ALL {
            for cell in plan_v5_cells(suite, hist_seed) {
                hist_seeds.insert(cell.seed);
                hist_episodes.insert(cell.episode_id);
            }
        }
        let seed_overlap: Vec<u64> = all_candidate_seeds
            .intersection(&hist_seeds)
            .copied()
            .collect();
        if !seed_overlap.is_empty() {
            return Err(GeneratorError::DisjointnessViolation(format!(
                "candidate seed {seed} shares base-world seeds with historical seed {hist_seed}: {seed_overlap:?}"
            )));
        }
        let ep_overlap: Vec<&str> = hist_episodes
            .iter()
            .map(String::as_str)
            .filter(|id| all_candidate_episodes.contains(id))
            .collect();
        if !ep_overlap.is_empty() {
            return Err(GeneratorError::DisjointnessViolation(format!(
                "candidate seed {seed} shares episode_ids with historical seed {hist_seed}: {ep_overlap:?}"
            )));
        }
        disjoint_from_historical = Some(true);
    }

    let split_unique_seed_counts = split_seeds
        .into_iter()
        .map(|(k, v)| (k.as_str().to_owned(), v.len()))
        .collect();

    Ok(SplitDisjointnessSummary {
        seed,
        against_historical_seed,
        split_unique_seed_counts,
        total_unique_base_world_seeds: all_candidate_seeds.len(),
        pairwise_disjoint: true,
        disjoint_from_historical_seed: disjoint_from_historical,
        v5_lite_regime_counts: lite_regime_counts,
        v5_lite_family_regimes: expected_lite_regimes,
    })
}

#[must_use]
pub(crate) fn etag(cell: &V5Cell, label: &str) -> String {
    let prefix = sha256_prefix_u64(format!("v5-etag\0{}\0{label}", cell.seed).as_bytes());
    format!("{:016}", prefix % 10_000_000_000_000_000u64)
}

#[must_use]
pub(crate) fn evidence_codes(episode_id: &str) -> HashMap<&'static str, usize> {
    let mut labels: Vec<([u8; 32], &'static str)> = EVIDENCE_LABELS_V5
        .iter()
        .map(|&label| {
            let digest = sha256(format!("v5-evidence-code\0{episode_id}\0{label}").as_bytes());
            (digest, label)
        })
        .collect();
    labels.sort_by_key(|a| a.0);
    labels
        .into_iter()
        .enumerate()
        .map(|(index, (_, label))| (label, index + 1))
        .collect()
}

pub(crate) fn evidence(
    codes: &HashMap<&'static str, usize>,
    label: &str,
) -> Result<String, GeneratorError> {
    let code = codes
        .get(label)
        .copied()
        .ok_or_else(|| GeneratorError::UnknownEvidenceLabel(label.to_owned()))?;
    Ok(format!("ev_{code:05}"))
}

#[must_use]
pub(crate) fn id_map(cell: &V5Cell, prefix: &str, names: &[&str]) -> Vec<(String, String)> {
    let mut numbers: Vec<usize> = (1..=names.len()).collect();
    PyRandom::from_str_seed(&format!("{}:{prefix}", cell.seed)).shuffle(&mut numbers);
    let width = 2.max(names.len().to_string().len());
    names
        .iter()
        .zip(numbers)
        .map(|(&name, number)| (name.to_owned(), format!("{prefix}_{number:0width$}")))
        .collect()
}

#[must_use]
pub(crate) fn id_lookup(pairs: &[(String, String)], key: &str) -> String {
    pairs
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.clone())
        .expect("id_map key must exist")
}

#[must_use]
pub(crate) fn replica_evidence_codes(episode_id: &str) -> HashMap<&'static str, usize> {
    let mut labels: Vec<([u8; 32], &'static str)> = REPLICA_LABELS_V5
        .iter()
        .map(|&label| {
            let digest = sha256(format!("v5-replica-code\0{episode_id}\0{label}").as_bytes());
            (digest, label)
        })
        .collect();
    labels.sort_by_key(|a| a.0);
    labels
        .into_iter()
        .enumerate()
        .map(|(index, (_, label))| (label, index + 1))
        .collect()
}

pub(crate) fn slice_id(cell: &V5Cell, label: &str) -> Result<String, GeneratorError> {
    let idx = SLICE_LABELS_V5
        .iter()
        .position(|&item| item == label)
        .ok_or_else(|| GeneratorError::UnknownSliceLabel(label.to_owned()))?;
    let mut numbers: Vec<usize> = (1..=SLICE_LABELS_V5.len()).collect();
    PyRandom::from_str_seed(&format!("v5-slice-map\0{}", cell.episode_id)).shuffle(&mut numbers);
    Ok(format!("sl_{:02}", numbers[idx]))
}

pub(crate) fn replica<T: StrictV5Model>(
    cell: &V5Cell,
    model: &T,
    label: &str,
) -> Result<T, GeneratorError> {
    let codes = replica_evidence_codes(&cell.episode_id);
    let replica_code = codes
        .get(label)
        .copied()
        .ok_or_else(|| GeneratorError::UnknownReplicaLabel(label.to_owned()))?;
    let data = model.to_strict_json_value()?;
    let mut counter = 0usize;

    fn replace(
        cell: &V5Cell,
        label: &str,
        replica_code: usize,
        counter: &mut usize,
        value: Value,
        path: &str,
    ) -> Result<Value, GeneratorError> {
        match value {
            Value::Object(map) => {
                let mut result = Map::with_capacity(map.len());
                for (key, child) in map {
                    if key == "evidence_id" && child.is_string() {
                        *counter += 1;
                        if *counter >= 100 {
                            return Err(GeneratorError::InvalidInput(format!(
                                "replica {label} has too many versioned fields"
                            )));
                        }
                        result.insert(
                            key,
                            Value::String(format!("ev_{:05}", 50_000 + replica_code * 100 + *counter)),
                        );
                    } else if key == "etag" && child.is_string() {
                        *counter += 1;
                        let tag = etag(cell, &format!("replica:{label}:{path}:{counter}"));
                        result.insert(key, Value::String(tag));
                    } else {
                        let next_path = format!("{path}.{key}");
                        let replaced = replace(cell, label, replica_code, counter, child, &next_path)?;
                        result.insert(key, replaced);
                    }
                }
                Ok(Value::Object(result))
            }
            Value::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for (index, child) in items.into_iter().enumerate() {
                    let next_path = format!("{path}[{index}]");
                    out.push(replace(cell, label, replica_code, counter, child, &next_path)?);
                }
                Ok(Value::Array(out))
            }
            other => Ok(other),
        }
    }

    let replaced = replace(cell, label, replica_code, &mut counter, data, label)?;
    Ok(T::from_strict_json_value(&replaced)?)
}
