//! `SPEC-v5.2` §2 cell planning and §2.1 seed allocation with the collision-suffix rule.
//!
//! Cells per suite:
//! - `v52_dev`: `(F_f, R_{((f-1) mod 4)+1})`, `f = 1..12`, `seed_slot = 00`.
//! - `v52_lite`: `(F_f, R_{(f mod 4)+1})` and `(F_f, R_{((f+1) mod 4)+1})`, `seed_slot = 00`.
//! - `v52_blind_fixture` and `v52_core`: all `48` `(family, regime)` cells, `seed_slot = 00`.
//! - `v52_full`: all `48` cells with `seed_slot ∈ {00, 01}`.
//!
//! Within a suite, cells are ordered ascending by `(family_id, regime_id, seed_slot)`; that is both
//! the seed evaluation order and the `episodes.jsonl` order. Suites are evaluated in the order retired
//! seed set, `v52_dev`, `v52_lite`, `v52_blind_fixture`, `v52_core`, `v52_full`. A colliding cell's
//! `seed_slot` text gains `"+1"`, `"+2"`, … until its seed is unique. Public suites precede private
//! suites, so public seeds never depend on the private key.

use std::collections::BTreeSet;

use ib_v52_schemas::{FAMILIES_V52, REGIMES_V52, V52Suite};
use serde::{Deserialize, Serialize};

use crate::error::V52GeneratorError;
use crate::seeds::{SecretKey32, base_seed, public_suite_key, retired_seed_set, seed_slot_text};

/// A single planned engagement cell.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct V52Cell {
    pub suite: V52Suite,
    pub family_id: String,
    pub regime_id: String,
    /// `0` (`"00"`) for every suite except the second slot of `v52_full` (`1`, `"01"`).
    pub seed_slot: usize,
    /// §2.1 collision suffix `n` (`seed_slot` text `"00+n"` in `M`); `0` when the first seed was unique.
    pub collision_suffix: u32,
    /// Base engagement seed `u64_be(HMAC-SHA256(S, M)[0..8])`.
    pub seed: u64,
    /// Canonical episode ID `<suite_id>_<family_id>_<regime_id>_s<seed_slot:02>` (no suffix).
    pub episode_id: String,
}

impl V52Cell {
    /// The `seed_slot` text used in `M`, including any collision suffix.
    #[must_use]
    pub fn seed_slot_text(&self) -> String {
        seed_slot_text(self.seed_slot, self.collision_suffix)
    }
}

/// The `(family_id, regime_id, seed_slot)` coordinates of `suite`, ascending.
#[must_use]
pub fn suite_cell_coordinates(suite: V52Suite) -> Vec<(&'static str, &'static str, usize)> {
    let mut coords: Vec<(&'static str, &'static str, usize)> = Vec::new();
    for (f_idx, family_id) in FAMILIES_V52.iter().enumerate() {
        let f = f_idx + 1;
        match suite {
            V52Suite::V52Dev => coords.push((family_id, REGIMES_V52[(f - 1) % 4], 0)),
            V52Suite::V52Lite => {
                coords.push((family_id, REGIMES_V52[f % 4], 0));
                coords.push((family_id, REGIMES_V52[(f + 1) % 4], 0));
            }
            V52Suite::V52BlindFixture | V52Suite::V52Core => {
                for regime_id in REGIMES_V52 {
                    coords.push((family_id, regime_id, 0));
                }
            }
            V52Suite::V52Full => {
                for regime_id in REGIMES_V52 {
                    coords.push((family_id, regime_id, 0));
                    coords.push((family_id, regime_id, 1));
                }
            }
        }
    }
    coords.sort_unstable();
    coords
}

/// Sequential §2.1 allocator: seeds accepted so far, starting from the retired seed set.
#[derive(Debug, Clone)]
pub struct SeedAllocatorV52 {
    accepted: BTreeSet<u64>,
    retired_count: usize,
}

impl Default for SeedAllocatorV52 {
    fn default() -> Self {
        Self::new()
    }
}

impl SeedAllocatorV52 {
    /// Start from the retired seed set ([`retired_seed_set`]).
    #[must_use]
    pub fn new() -> Self {
        let accepted = retired_seed_set();
        let retired_count = accepted.len();
        Self {
            accepted,
            retired_count,
        }
    }

    /// Number of distinct retired seeds the allocator started from.
    #[must_use]
    pub const fn retired_count(&self) -> usize {
        self.retired_count
    }

    /// Allocate every cell of `suite` under `key`, applying the collision-suffix rule.
    pub fn allocate(&mut self, suite: V52Suite, key: &[u8; 32]) -> Vec<V52Cell> {
        suite_cell_coordinates(suite)
            .into_iter()
            .map(|(family_id, regime_id, seed_slot)| {
                let mut collision_suffix = 0u32;
                let seed = loop {
                    let candidate = base_seed(
                        key,
                        suite.as_str(),
                        family_id,
                        regime_id,
                        &seed_slot_text(seed_slot, collision_suffix),
                    );
                    if self.accepted.insert(candidate) {
                        break candidate;
                    }
                    collision_suffix += 1;
                };
                V52Cell {
                    suite,
                    family_id: family_id.to_owned(),
                    regime_id: regime_id.to_owned(),
                    seed_slot,
                    collision_suffix,
                    seed,
                    episode_id: format!(
                        "{}_{}_{}_s{:02}",
                        suite.as_str(),
                        family_id,
                        regime_id,
                        seed_slot
                    ),
                }
            })
            .collect()
    }
}

/// Plan the three public suites in evaluation order (`v52_dev`, `v52_lite`, `v52_blind_fixture`).
#[must_use]
pub fn plan_public_v52_suites() -> Vec<(V52Suite, Vec<V52Cell>)> {
    let s_pub = public_suite_key();
    let mut allocator = SeedAllocatorV52::new();
    V52Suite::PUBLIC
        .iter()
        .map(|&suite| (suite, allocator.allocate(suite, &s_pub)))
        .collect()
}

/// Plan the cells of `suite`.
///
/// Public suites take no key. Private suites (`v52_core`, `v52_full`) require `S_priv`; the public
/// suites are allocated first under `S_pub`, then `v52_core`, then `v52_full`.
///
/// # Errors
/// Returns [`V52GeneratorError::InvalidConfig`] if a private suite has no `S_priv`, or a public suite is
/// given one.
pub fn plan_v52_cells(
    suite: V52Suite,
    s_priv: Option<&SecretKey32>,
) -> Result<Vec<V52Cell>, V52GeneratorError> {
    let s_pub = public_suite_key();
    let mut allocator = SeedAllocatorV52::new();
    let mut planned = None;
    for &public in &V52Suite::PUBLIC {
        let cells = allocator.allocate(public, &s_pub);
        if public == suite {
            planned = Some(cells);
        }
    }
    if !suite.is_private() {
        if s_priv.is_some() {
            return Err(V52GeneratorError::InvalidConfig(format!(
                "{suite} is a public suite and derives from S_pub; no private key may be supplied"
            )));
        }
        return Ok(planned.unwrap_or_default());
    }
    let Some(s_priv) = s_priv else {
        return Err(V52GeneratorError::InvalidConfig(format!(
            "{suite} is a private suite; S_priv (private key K and beacon B) is required"
        )));
    };
    let core = allocator.allocate(V52Suite::V52Core, &s_priv.0);
    if suite == V52Suite::V52Core {
        return Ok(core);
    }
    Ok(allocator.allocate(V52Suite::V52Full, &s_priv.0))
}

/// Plan a public suite. Convenience wrapper over [`plan_v52_cells`].
///
/// # Errors
/// Returns [`V52GeneratorError::InvalidConfig`] for a private suite.
pub fn plan_public_v52_cells(suite: V52Suite) -> Result<Vec<V52Cell>, V52GeneratorError> {
    plan_v52_cells(suite, None)
}

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, HashSet};

    use super::*;
    use crate::seeds::{TEST_KEY_HEX_V52, decode_hex32, private_suite_key};

    fn test_s_priv() -> SecretKey32 {
        let k = SecretKey32(decode_hex32(TEST_KEY_HEX_V52).unwrap());
        private_suite_key(&k, &[0x5au8; 32])
    }

    #[test]
    fn cell_formulas_match_spec_section_2() {
        let dev = suite_cell_coordinates(V52Suite::V52Dev);
        assert_eq!(dev.len(), 12);
        assert_eq!(dev[0], ("F01", "R1", 0));
        assert_eq!(dev[1], ("F02", "R2", 0));
        assert_eq!(dev[4], ("F05", "R1", 0));
        assert_eq!(dev[11], ("F12", "R4", 0));

        let lite = suite_cell_coordinates(V52Suite::V52Lite);
        assert_eq!(lite.len(), 24);
        // f = 1: R_{(1 mod 4)+1} = R2 and R_{(2 mod 4)+1} = R3.
        assert_eq!(&lite[0..2], &[("F01", "R2", 0), ("F01", "R3", 0)]);
        // f = 3: R4 and R1, ordered ascending by regime.
        assert_eq!(&lite[4..6], &[("F03", "R1", 0), ("F03", "R4", 0)]);
        let mut per_regime: BTreeMap<&str, usize> = BTreeMap::new();
        for (_, r, _) in &lite {
            *per_regime.entry(r).or_default() += 1;
        }
        assert!(per_regime.values().all(|&n| n == 6));
        // Lite shares no cell with dev.
        let dev_set: HashSet<_> = dev.iter().collect();
        assert!(lite.iter().all(|c| !dev_set.contains(c)));

        assert_eq!(suite_cell_coordinates(V52Suite::V52BlindFixture).len(), 48);
        assert_eq!(suite_cell_coordinates(V52Suite::V52Core).len(), 48);
        let full = suite_cell_coordinates(V52Suite::V52Full);
        assert_eq!(full.len(), 96);
        assert_eq!(
            &full[0..3],
            &[("F01", "R1", 0), ("F01", "R1", 1), ("F01", "R2", 0)]
        );
        for suite in V52Suite::SEED_EVALUATION_ORDER {
            assert_eq!(suite_cell_coordinates(suite).len(), suite.episode_count());
        }
    }

    #[test]
    fn public_suites_disjoint_from_retired_and_each_other_and_independent_of_k() {
        let retired = retired_seed_set();
        let public = plan_public_v52_suites();
        let mut seen = HashSet::new();
        for (suite, cells) in &public {
            assert_eq!(cells.len(), suite.episode_count());
            for c in cells {
                assert!(
                    !retired.contains(&c.seed),
                    "{} collides with retired",
                    c.episode_id
                );
                assert!(seen.insert(c.seed), "{} duplicate seed", c.episode_id);
            }
        }
        // The dev F05 R1 cell is a published test vector only under K_test; under S_pub it differs.
        let dev = plan_public_v52_cells(V52Suite::V52Dev).unwrap();
        assert_eq!(dev, public[0].1);
        assert_eq!(dev[0].episode_id, "v53_dev_F01_R1_s00");

        // Private planning re-derives identical public seeds first, then core and full.
        let s_priv = test_s_priv();
        let core = plan_v52_cells(V52Suite::V52Core, Some(&s_priv)).unwrap();
        let full = plan_v52_cells(V52Suite::V52Full, Some(&s_priv)).unwrap();
        assert_eq!(core.len(), 48);
        assert_eq!(full.len(), 96);
        for c in core.iter().chain(full.iter()) {
            assert!(!retired.contains(&c.seed));
            assert!(seen.insert(c.seed), "{} duplicate seed", c.episode_id);
        }
        // v52_lite is not a subset of v52_core.
        let core_seeds: HashSet<u64> = core.iter().map(|c| c.seed).collect();
        assert!(public[1].1.iter().all(|c| !core_seeds.contains(&c.seed)));
    }

    #[test]
    fn key_requirements_are_enforced() {
        assert!(plan_v52_cells(V52Suite::V52Core, None).is_err());
        assert!(plan_v52_cells(V52Suite::V52Dev, Some(&test_s_priv())).is_err());
        assert!(plan_public_v52_cells(V52Suite::V52Full).is_err());
    }

    #[test]
    fn collision_rule_appends_suffix_and_records_it() {
        let s_pub = public_suite_key();
        let mut allocator = SeedAllocatorV52::new();
        let first = base_seed(&s_pub, "v53_dev", "F01", "R1", "00");
        // Force a collision on the first dev cell by pre-accepting its natural seed.
        allocator.accepted.insert(first);
        let dev = allocator.allocate(V52Suite::V52Dev, &s_pub);
        assert_eq!(dev[0].collision_suffix, 1);
        assert_eq!(dev[0].seed_slot_text(), "00+1");
        assert_eq!(
            dev[0].seed,
            base_seed(&s_pub, "v53_dev", "F01", "R1", "00+1")
        );
        assert_eq!(dev[0].episode_id, "v53_dev_F01_R1_s00");
    }
}
