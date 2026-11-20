//! Deterministic cell planning and pairwise-disjoint seed allocation for IdentityBench v5.1.

use std::collections::HashSet;

use ib_core::PyRandom;
use ib_v51_schemas::{FAMILIES_V51, REGIMES_V51, V51Suite};
use serde::{Deserialize, Serialize};

/// Domain separator mixed into `master_seed` so v5.1 base seeds are structurally disjoint from v5.0.
const V51_SEED_MIXER: u64 = 0x5151_A7E3_9C2B_410F;

/// A single planned engagement cell `(suite, family, regime, seed_slot, seed)`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct V51Cell {
    pub suite: V51Suite,
    pub family_id: String,
    pub regime_id: String,
    /// `0` for `v51_dev`, `v51_lite`, `v51_core`, and the first half of `v51_full`; `1` for the second half of `v51_full`.
    pub seed_slot: usize,
    /// Globally unique base engagement seed across all 108 base cells (`12` dev + `96` held-out).
    pub seed: u64,
    /// Deterministic canonical episode ID (`<suite>_<family>_<regime>_s<slot:02>`).
    pub episode_id: String,
}

/// Allocate all 108 pairwise-disjoint base seeds deterministically from `master_seed`:
/// - `[0..12)`: `v51_dev` (`12` families, balanced `3` per regime `R1..R4`)
/// - `[12..60)`: `v51_core` (`12` families x `4` regimes, `seed_slot = 0`; `v51_lite` selects a balanced 24-cell subset)
/// - `[60..108)`: `v51_full` second seed pass (`12` families x `4` regimes, `seed_slot = 1`)
#[must_use]
pub fn all_base_v51_seeds(master_seed: u64) -> Vec<u64> {
    let mut rng = PyRandom::from_u64(master_seed ^ V51_SEED_MIXER);
    let mut seen = HashSet::with_capacity(108);
    let mut seeds = Vec::with_capacity(108);
    while seeds.len() < 108 {
        let candidate = u64::try_from(rng.randint(100_000_000, 999_999_999))
            .expect("seed candidate in positive u64 range");
        if seen.insert(candidate) {
            seeds.push(candidate);
        }
    }
    seeds
}

/// Plan the deterministic list of [`V51Cell`] engagements for `suite` and `master_seed`.
///
/// Guarantees:
/// - `v51_dev` (`12` cells) and `v51_full` (`96` cells) use 108 pairwise-disjoint seeds.
/// - `v51_core` (`48` cells) is the exact `seed_slot == 0` half of `v51_full`.
/// - `v51_lite` (`24` cells) is a balanced subset of `v51_core` (`2` regimes per family, `6` cells per regime).
#[must_use]
pub fn plan_v51_cells(suite: V51Suite, master_seed: u64) -> Vec<V51Cell> {
    let base_seeds = all_base_v51_seeds(master_seed);
    match suite {
        V51Suite::V51Dev => FAMILIES_V51
            .iter()
            .enumerate()
            .map(|(f_idx, family_id)| {
                let regime_id = REGIMES_V51[f_idx % REGIMES_V51.len()];
                let seed = base_seeds[f_idx];
                V51Cell {
                    suite,
                    family_id: (*family_id).to_owned(),
                    regime_id: regime_id.to_owned(),
                    seed_slot: 0,
                    seed,
                    episode_id: format!("{}_{}_{}_s00", suite.as_str(), family_id, regime_id),
                }
            })
            .collect(),
        V51Suite::V51Lite => {
            let mut cells = Vec::with_capacity(24);
            for (f_idx, family_id) in FAMILIES_V51.iter().enumerate() {
                // Even families take R1 & R3; odd families take R2 & R4 -> 6 per regime, 2 per family.
                let selected_regimes = if f_idx % 2 == 0 { [0, 2] } else { [1, 3] };
                for r_idx in selected_regimes {
                    let regime_id = REGIMES_V51[r_idx];
                    let seed = base_seeds[12 + f_idx * 4 + r_idx];
                    cells.push(V51Cell {
                        suite,
                        family_id: (*family_id).to_owned(),
                        regime_id: regime_id.to_owned(),
                        seed_slot: 0,
                        seed,
                        episode_id: format!("{}_{}_{}_s00", suite.as_str(), family_id, regime_id),
                    });
                }
            }
            cells
        }
        V51Suite::V51Core => {
            let mut cells = Vec::with_capacity(48);
            for (f_idx, family_id) in FAMILIES_V51.iter().enumerate() {
                for (r_idx, regime_id) in REGIMES_V51.iter().enumerate() {
                    let seed = base_seeds[12 + f_idx * 4 + r_idx];
                    cells.push(V51Cell {
                        suite,
                        family_id: (*family_id).to_owned(),
                        regime_id: (*regime_id).to_owned(),
                        seed_slot: 0,
                        seed,
                        episode_id: format!("{}_{}_{}_s00", suite.as_str(), family_id, regime_id),
                    });
                }
            }
            cells
        }
        V51Suite::V51Full => {
            let mut cells = Vec::with_capacity(96);
            for seed_slot in 0..2 {
                let offset = 12 + seed_slot * 48;
                for (f_idx, family_id) in FAMILIES_V51.iter().enumerate() {
                    for (r_idx, regime_id) in REGIMES_V51.iter().enumerate() {
                        let seed = base_seeds[offset + f_idx * 4 + r_idx];
                        cells.push(V51Cell {
                            suite,
                            family_id: (*family_id).to_owned(),
                            regime_id: (*regime_id).to_owned(),
                            seed_slot,
                            seed,
                            episode_id: format!(
                                "{}_{}_{}_s{:02}",
                                suite.as_str(),
                                family_id,
                                regime_id,
                                seed_slot
                            ),
                        });
                    }
                }
            }
            cells
        }
    }
}
