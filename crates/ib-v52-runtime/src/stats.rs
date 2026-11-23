//! `SPEC-v5.2` §9 statistics, as a pure module: the Rao–Wu stratified cluster bootstrap with
//! `m_h = n_h − 1` and the pinned ChaCha20 draw loop, Type-7 quantiles, the two-sided 95%
//! Clopper–Pearson interval and the outer-union reporting rule, joint bootstrap rank intervals,
//! and the total leaderboard order.
//!
//! Nothing here reads files, clocks, or global state: every result is a function of its inputs.
//!
//! **Normative configuration.** On the ranked suite (`v52_core`) the strata are the `12` families
//! `F01..F12`, each holding its `4` scenarios ordered `R1..R4` (indices `0..3`), with `k = 3`
//! repeats. [`BootstrapDrawsV52::generate`] then consumes exactly the §9 loop
//! `for b in 0..10_000 { for f in 0..12 { for d in 0..3 { idx = (rng.next_u32() & 3) } } }`.
//! The general form (`idx = next_u32() % n_h`, which equals `& 3` for `n_h = 4`) exists only so
//! that test fixtures and the test-only leaderboard override can use other equal-size strata.

use std::cmp::Ordering;

use ib_core::hashing::sha256_parts;

use crate::integrity::ChaCha20RngV52;

/// `B`, the number of bootstrap replicates (§9).
pub const BOOTSTRAP_REPLICATES_V52: usize = 10_000;
/// Key preimage prefix: `SHA-256(ASCII(prefix) || ASCII(episodes_sha256))` (§9).
pub const BOOTSTRAP_KEY_PREFIX_V52: &str = "identitybench-v5.2-bootstrap|";
/// Two-sided level of every reported interval (95%).
pub const INTERVAL_ALPHA_V52: f64 = 0.05;
/// Lower and upper quantile probabilities of the bootstrap and rank intervals.
pub const QUANTILE_LO_V52: f64 = 0.025;
pub const QUANTILE_HI_V52: f64 = 0.975;

/// The 32-byte ChaCha20 key of the bootstrap RNG: `SHA-256(ASCII("identitybench-v5.2-bootstrap|")
/// || ASCII(episodes_sha256))`, where `episodes_sha256` is the suite's 64-character lowercase hex
/// digest.
#[must_use]
pub fn bootstrap_key_v52(episodes_sha256_hex: &str) -> [u8; 32] {
    sha256_parts(&[
        BOOTSTRAP_KEY_PREFIX_V52.as_bytes(),
        episodes_sha256_hex.as_bytes(),
    ])
}

/// Type-7 (linear interpolation, the R/NumPy default) quantile of an ascending-sorted slice:
/// `h = (n − 1)·p`, `q = x[⌊h⌋] + (h − ⌊h⌋)·(x[⌊h⌋ + 1] − x[⌊h⌋])`. Returns `None` when empty.
#[must_use]
pub fn type7_quantile(sorted: &[f64], p: f64) -> Option<f64> {
    let n = sorted.len();
    if n == 0 {
        return None;
    }
    let p = p.clamp(0.0, 1.0);
    let h = (n - 1) as f64 * p;
    let lo = h.floor() as usize;
    if lo + 1 >= n {
        return Some(sorted[n - 1]);
    }
    let frac = h - lo as f64;
    Some(sorted[lo] + frac * (sorted[lo + 1] - sorted[lo]))
}

/// `P(X ≤ k)` for `X ~ Binomial(n, p)`, summed in `f64` from exact binomial coefficients.
fn binomial_cdf(k: u64, n: u64, p: f64) -> f64 {
    if p <= 0.0 {
        return 1.0;
    }
    if p >= 1.0 {
        return if k >= n { 1.0 } else { 0.0 };
    }
    let mut coeff = 1.0_f64; // C(n, 0)
    let mut total = 0.0_f64;
    for i in 0..=k.min(n) {
        if i > 0 {
            coeff = coeff * (n - i + 1) as f64 / i as f64;
        }
        total += coeff * p.powi(i as i32) * (1.0 - p).powi((n - i) as i32);
    }
    total.min(1.0)
}

/// Bisection on `[0, 1]` for the root of a monotone function, to the limit of `f64` resolution.
fn bisect_unit(mut above: impl FnMut(f64) -> bool) -> f64 {
    // `above(p)` is true when the root lies above `p`.
    let (mut lo, mut hi) = (0.0_f64, 1.0_f64);
    for _ in 0..200 {
        let mid = 0.5 * (lo + hi);
        if mid <= lo || mid >= hi {
            break;
        }
        if above(mid) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    0.5 * (lo + hi)
}

/// Lower bound of the two-sided `1 − alpha` Clopper–Pearson interval for `k` successes in `n`
/// trials: `0` when `k = 0`, otherwise the `p` with `P(X ≥ k; n, p) = alpha / 2`.
#[must_use]
pub fn clopper_pearson_lower(k: u64, n: u64, alpha: f64) -> f64 {
    if n == 0 || k == 0 {
        return 0.0;
    }
    let k = k.min(n);
    let tail = alpha / 2.0;
    // P(X ≥ k; p) = 1 − CDF(k − 1; p) increases in p.
    bisect_unit(|p| 1.0 - binomial_cdf(k - 1, n, p) < tail)
}

/// Upper bound of the two-sided `1 − alpha` Clopper–Pearson interval for `k` successes in `n`
/// trials: `1` when `k = n`, otherwise the `p` with `P(X ≤ k; n, p) = alpha / 2`.
#[must_use]
pub fn clopper_pearson_upper(k: u64, n: u64, alpha: f64) -> f64 {
    if n == 0 || k >= n {
        return 1.0;
    }
    let tail = alpha / 2.0;
    // CDF(k; p) decreases in p.
    bisect_unit(|p| binomial_cdf(k, n, p) > tail)
}

/// Errors in the shape of bootstrap inputs.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum StatsErrorV52 {
    #[error("the bootstrap needs at least one stratum")]
    NoStrata,
    #[error("every stratum needs at least 2 scenarios (m_h = n_h − 1 ≥ 1); got sizes {0:?}")]
    StratumTooSmall(Vec<usize>),
    #[error("strata must be equal-sized; got sizes {0:?}")]
    UnequalStrata(Vec<usize>),
    #[error("an entry has {got} scenario counts; the strata hold {expected}")]
    EngagementCountMismatch { expected: usize, got: usize },
    #[error("a scenario has {resolved} resolved slots out of {repeats} repeats")]
    ResolvedExceedsRepeats { resolved: u32, repeats: u32 },
    #[error("repeats must be at least 1")]
    ZeroRepeats,
    #[error("ranked entries must share one repeat count")]
    MixedRepeats,
}

/// The shared bootstrap draws of one suite (§9 "Sharing"): for every replicate and stratum, the
/// `m_h = n_h − 1` drawn scenario indices (within the stratum), in draw order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BootstrapDrawsV52 {
    replicates: usize,
    /// Number of strata `H`.
    strata: usize,
    /// Scenarios per stratum `n` (equal-sized strata).
    stratum_size: usize,
    /// `replicates · strata · (stratum_size − 1)` indices in loop order.
    indices: Vec<u8>,
}

impl BootstrapDrawsV52 {
    /// Run the pinned draw loop: `for b in 0..replicates { for h in 0..strata { for d in 0..m {
    /// idx = rng.next_u32() % n } } }` with `m = n − 1`, on `ChaCha20Rng` keyed by `key`
    /// (stream `0`, block `0`, word `0`). For `n = 4` the draw is exactly `next_u32() & 3`.
    ///
    /// # Errors
    /// Rejects no strata, strata with fewer than `2` scenarios, unequal strata, or strata
    /// larger than `256` scenarios.
    pub fn generate(
        key: [u8; 32],
        stratum_sizes: &[usize],
        replicates: usize,
    ) -> Result<Self, StatsErrorV52> {
        let n = validate_strata(stratum_sizes)?;
        let m = n - 1;
        let n_u32 = n as u32;
        let mut rng = ChaCha20RngV52::from_key(key);
        let mut indices = Vec::with_capacity(replicates * stratum_sizes.len() * m);
        for _b in 0..replicates {
            for _h in 0..stratum_sizes.len() {
                for _d in 0..m {
                    let word = rng.next_u32();
                    let idx = if n_u32 == 4 { word & 3 } else { word % n_u32 };
                    indices.push(idx as u8);
                }
            }
        }
        Ok(Self {
            replicates,
            strata: stratum_sizes.len(),
            stratum_size: n,
            indices,
        })
    }

    /// Build draws from explicit indices (hand-checked fixtures): `indices[b][h]` holds the `m`
    /// indices drawn for stratum `h` in replicate `b`.
    ///
    /// # Errors
    /// Rejects malformed shapes or out-of-range indices.
    pub fn from_explicit(
        stratum_sizes: &[usize],
        indices: &[Vec<Vec<u8>>],
    ) -> Result<Self, StatsErrorV52> {
        let n = validate_strata(stratum_sizes)?;
        let m = n - 1;
        let mut flat = Vec::new();
        for replicate in indices {
            if replicate.len() != stratum_sizes.len() {
                return Err(StatsErrorV52::EngagementCountMismatch {
                    expected: stratum_sizes.len(),
                    got: replicate.len(),
                });
            }
            for stratum in replicate {
                if stratum.len() != m || stratum.iter().any(|&i| usize::from(i) >= n) {
                    return Err(StatsErrorV52::StratumTooSmall(stratum_sizes.to_vec()));
                }
                flat.extend_from_slice(stratum);
            }
        }
        Ok(Self {
            replicates: indices.len(),
            strata: stratum_sizes.len(),
            stratum_size: n,
            indices: flat,
        })
    }

    #[must_use]
    pub const fn replicates(&self) -> usize {
        self.replicates
    }

    #[must_use]
    pub const fn strata(&self) -> usize {
        self.strata
    }

    #[must_use]
    pub const fn stratum_size(&self) -> usize {
        self.stratum_size
    }

    /// Scenarios drawn per stratum, `m = n − 1`.
    #[must_use]
    pub const fn draws_per_stratum(&self) -> usize {
        self.stratum_size - 1
    }

    /// The drawn indices of replicate `b` (flattened over strata, `m` per stratum).
    #[must_use]
    pub fn replicate_indices(&self, b: usize) -> &[u8] {
        let width = self.strata * self.draws_per_stratum();
        &self.indices[b * width..(b + 1) * width]
    }

    /// Integer replicate totals `T_b = Σ_h Σ_d r[h·n + idx_{b,h,d}]` for one entry, where
    /// `resolved[e]` is the number of resolved repeats of scenario `e` (strata contiguous, in
    /// stratum order). The replicate statistic is `T_b / (H · m · k)`; ranking on `T_b` is exact.
    ///
    /// # Errors
    /// Rejects a scenario vector that does not match the strata.
    pub fn replicate_totals(&self, resolved: &[u32]) -> Result<Vec<u64>, StatsErrorV52> {
        let expected = self.strata * self.stratum_size;
        if resolved.len() != expected {
            return Err(StatsErrorV52::EngagementCountMismatch {
                expected,
                got: resolved.len(),
            });
        }
        let m = self.draws_per_stratum();
        let mut totals = Vec::with_capacity(self.replicates);
        for b in 0..self.replicates {
            let drawn = self.replicate_indices(b);
            let mut total = 0_u64;
            for h in 0..self.strata {
                let base = h * self.stratum_size;
                for &idx in &drawn[h * m..(h + 1) * m] {
                    total += u64::from(resolved[base + usize::from(idx)]);
                }
            }
            totals.push(total);
        }
        Ok(totals)
    }

    /// The denominator `H · m · k` that maps a replicate total to the replicate statistic.
    #[must_use]
    pub fn statistic_denominator(&self, repeats: u32) -> u64 {
        (self.strata * self.draws_per_stratum()) as u64 * u64::from(repeats)
    }
}

fn validate_strata(stratum_sizes: &[usize]) -> Result<usize, StatsErrorV52> {
    let Some(&n) = stratum_sizes.first() else {
        return Err(StatsErrorV52::NoStrata);
    };
    if stratum_sizes.iter().any(|&s| !(2..=256).contains(&s)) {
        return Err(StatsErrorV52::StratumTooSmall(stratum_sizes.to_vec()));
    }
    if stratum_sizes.iter().any(|&s| s != n) {
        return Err(StatsErrorV52::UnequalStrata(stratum_sizes.to_vec()));
    }
    Ok(n)
}

/// One entry's per-scenario outcome vector, in stratum order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntryOutcomesV52 {
    /// Resolved repeats per scenario, `0..=repeats`.
    pub resolved: Vec<u32>,
    /// Repeats per scenario `k`.
    pub repeats: u32,
}

impl EntryOutcomesV52 {
    /// Resolved slots `R`.
    #[must_use]
    pub fn resolved_slots(&self) -> u64 {
        self.resolved.iter().map(|&r| u64::from(r)).sum()
    }

    /// Planned slots `N · k`.
    #[must_use]
    pub fn planned_slots(&self) -> u64 {
        self.resolved.len() as u64 * u64::from(self.repeats)
    }

    fn validate(&self) -> Result<(), StatsErrorV52> {
        if self.repeats == 0 {
            return Err(StatsErrorV52::ZeroRepeats);
        }
        if let Some(&bad) = self.resolved.iter().find(|&&r| r > self.repeats) {
            return Err(StatsErrorV52::ResolvedExceedsRepeats {
                resolved: bad,
                repeats: self.repeats,
            });
        }
        Ok(())
    }
}

/// The §9 reported `G-SRR` interval of one entry and its components.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct GsrrIntervalV52 {
    /// Point estimate `R / (N · k)`.
    pub point: f64,
    /// Type-7 bootstrap quantiles at `0.025` and `0.975`.
    pub boot_lo: f64,
    pub boot_hi: f64,
    /// Clopper–Pearson counts `k_lo = ⌊R / k⌋`, `k_hi = ⌈R / k⌉` out of `n = N` scenarios.
    pub cp_n: u64,
    pub cp_k_lo: u64,
    pub cp_k_hi: u64,
    pub cp_lo: f64,
    pub cp_hi: f64,
    /// The reported outer union `[min(boot_lo, cp_lo), max(boot_hi, cp_hi)]`.
    pub lo: f64,
    pub hi: f64,
}

/// The §9 interval of one entry from its replicate totals.
///
/// # Errors
/// Rejects malformed outcome vectors.
pub fn gsrr_interval_v52(
    outcomes: &EntryOutcomesV52,
    replicate_totals: &[u64],
    statistic_denominator: u64,
) -> Result<GsrrIntervalV52, StatsErrorV52> {
    outcomes.validate()?;
    let r = outcomes.resolved_slots();
    let planned = outcomes.planned_slots();
    let k = u64::from(outcomes.repeats);
    let n = outcomes.resolved.len() as u64;
    let point = if planned == 0 {
        0.0
    } else {
        r as f64 / planned as f64
    };
    let mut stats: Vec<f64> = replicate_totals
        .iter()
        .map(|&t| t as f64 / statistic_denominator.max(1) as f64)
        .collect();
    stats.sort_by(f64::total_cmp);
    let boot_lo = type7_quantile(&stats, QUANTILE_LO_V52).unwrap_or(point);
    let boot_hi = type7_quantile(&stats, QUANTILE_HI_V52).unwrap_or(point);
    let k_lo = r / k;
    let k_hi = r.div_ceil(k);
    let cp_lo = clopper_pearson_lower(k_lo, n, INTERVAL_ALPHA_V52);
    let cp_hi = clopper_pearson_upper(k_hi, n, INTERVAL_ALPHA_V52);
    Ok(GsrrIntervalV52 {
        point,
        boot_lo,
        boot_hi,
        cp_n: n,
        cp_k_lo: k_lo,
        cp_k_hi: k_hi,
        cp_lo,
        cp_hi,
        lo: boot_lo.min(cp_lo),
        hi: boot_hi.max(cp_hi),
    })
}

/// A 95% rank interval (Type-7 quantiles of the replicate ranks; rank 1 is the highest `G-SRR`).
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct RankIntervalV52 {
    pub lo: f64,
    pub hi: f64,
}

/// Joint rank intervals (§9): within every replicate, entries are ranked by replicate `G-SRR`
/// (rank 1 is the highest; ties take their average rank). `totals[e][b]` is entry `e`'s
/// replicate total; all entries share one denominator, so ranking on totals is exact.
#[must_use]
pub fn rank_intervals_v52(totals: &[Vec<u64>]) -> Vec<RankIntervalV52> {
    let entries = totals.len();
    if entries == 0 {
        return Vec::new();
    }
    let replicates = totals.iter().map(Vec::len).min().unwrap_or(0);
    let mut ranks: Vec<Vec<f64>> = vec![Vec::with_capacity(replicates); entries];
    let mut order: Vec<usize> = (0..entries).collect();
    for b in 0..replicates {
        order.sort_by(|&x, &y| totals[y][b].cmp(&totals[x][b]).then(x.cmp(&y)));
        let mut start = 0;
        while start < entries {
            let value = totals[order[start]][b];
            let mut end = start;
            while end + 1 < entries && totals[order[end + 1]][b] == value {
                end += 1;
            }
            // 1-based positions start+1 ..= end+1 share their mean.
            let avg = (start + end + 2) as f64 / 2.0;
            for &e in &order[start..=end] {
                ranks[e].push(avg);
            }
            start = end + 1;
        }
    }
    ranks
        .into_iter()
        .map(|mut r| {
            r.sort_by(f64::total_cmp);
            RankIntervalV52 {
                lo: type7_quantile(&r, QUANTILE_LO_V52).unwrap_or(1.0),
                hi: type7_quantile(&r, QUANTILE_HI_V52).unwrap_or(1.0),
            }
        })
        .collect()
}

/// A rational `num / den` compared exactly by cross-multiplication.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RatioV52 {
    pub num: u64,
    pub den: u64,
}

impl RatioV52 {
    #[must_use]
    pub const fn new(num: u64, den: u64) -> Self {
        Self { num, den }
    }

    fn cmp_exact(self, other: Self) -> Ordering {
        (u128::from(self.num) * u128::from(other.den))
            .cmp(&(u128::from(other.num) * u128::from(self.den)))
    }
}

/// The §9 ordering keys of one entry.
#[derive(Debug, Clone, PartialEq)]
pub struct LeaderboardOrderKeyV52 {
    /// 1. `G-SRR` point estimate (`R / P`), descending.
    pub g_srr: RatioV52,
    /// 2. `pass^3` (scenarios resolved on every repeat / scenarios), descending; `None` last.
    pub pass_hat_3: Option<RatioV52>,
    /// 3. `Macro-Cat-Acc`, descending.
    pub macro_cat_acc: f64,
    /// 4. `commit_rate` (`Cm / P`), descending.
    pub commit_rate: RatioV52,
    /// 5. `(OG + AB) / Cm`, ascending, `None` (`Cm = 0`) last.
    pub violations_per_committed: Option<RatioV52>,
    /// 6. `(model_id, reasoning_effort, transport_id)`, ascending byte-wise.
    pub model_id: String,
    pub reasoning_effort: String,
    pub transport_id: String,
}

/// Total order of §9: keys 1–6 in sequence.
#[must_use]
pub fn compare_leaderboard_keys_v52(
    a: &LeaderboardOrderKeyV52,
    b: &LeaderboardOrderKeyV52,
) -> Ordering {
    // Descending: compare b to a.
    b.g_srr
        .cmp_exact(a.g_srr)
        .then_with(|| match (a.pass_hat_3, b.pass_hat_3) {
            (Some(x), Some(y)) => y.cmp_exact(x),
            (Some(_), None) => Ordering::Less,
            (None, Some(_)) => Ordering::Greater,
            (None, None) => Ordering::Equal,
        })
        .then_with(|| b.macro_cat_acc.total_cmp(&a.macro_cat_acc))
        .then_with(|| b.commit_rate.cmp_exact(a.commit_rate))
        .then_with(
            || match (a.violations_per_committed, b.violations_per_committed) {
                (Some(x), Some(y)) => x.cmp_exact(y),
                (Some(_), None) => Ordering::Less,
                (None, Some(_)) => Ordering::Greater,
                (None, None) => Ordering::Equal,
            },
        )
        .then_with(|| a.model_id.as_bytes().cmp(b.model_id.as_bytes()))
        .then_with(|| {
            a.reasoning_effort
                .as_bytes()
                .cmp(b.reasoning_effort.as_bytes())
        })
        .then_with(|| a.transport_id.as_bytes().cmp(b.transport_id.as_bytes()))
}

/// Indices of `keys` in leaderboard order (rank 1 first).
#[must_use]
pub fn leaderboard_order_v52(keys: &[LeaderboardOrderKeyV52]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..keys.len()).collect();
    order.sort_by(|&x, &y| compare_leaderboard_keys_v52(&keys[x], &keys[y]));
    order
}

/// Everything §9 reports for a set of entries on one suite, computed jointly.
#[derive(Debug, Clone, PartialEq)]
pub struct JointStatisticsV52 {
    pub intervals: Vec<GsrrIntervalV52>,
    /// Rank intervals among the entries flagged `ranked`; `None` for the others.
    pub rank_intervals: Vec<Option<RankIntervalV52>>,
}

/// Compute every entry's interval and the ranked entries' joint rank intervals from one shared
/// set of draws.
///
/// # Errors
/// Rejects outcome vectors that do not match the draws, or entries with differing repeats.
pub fn joint_statistics_v52(
    draws: &BootstrapDrawsV52,
    entries: &[EntryOutcomesV52],
    ranked: &[bool],
) -> Result<JointStatisticsV52, StatsErrorV52> {
    let mut totals = Vec::with_capacity(entries.len());
    let mut intervals = Vec::with_capacity(entries.len());
    for entry in entries {
        entry.validate()?;
        let t = draws.replicate_totals(&entry.resolved)?;
        intervals.push(gsrr_interval_v52(
            entry,
            &t,
            draws.statistic_denominator(entry.repeats),
        )?);
        totals.push(t);
    }
    let ranked_idx: Vec<usize> = (0..entries.len())
        .filter(|&i| ranked.get(i).copied().unwrap_or(false))
        .collect();
    // Ranking compares replicate statistics T_b / (H·m·k); with one shared k that is T_b exactly.
    if let Some(&first) = ranked_idx.first()
        && ranked_idx
            .iter()
            .any(|&i| entries[i].repeats != entries[first].repeats)
    {
        return Err(StatsErrorV52::MixedRepeats);
    }
    let ranked_totals: Vec<Vec<u64>> = ranked_idx.iter().map(|&i| totals[i].clone()).collect();
    let rank = rank_intervals_v52(&ranked_totals);
    let mut rank_intervals = vec![None; entries.len()];
    for (pos, &i) in ranked_idx.iter().enumerate() {
        rank_intervals[i] = Some(rank[pos]);
    }
    Ok(JointStatisticsV52 {
        intervals,
        rank_intervals,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEV_EPISODES_SHA: &str =
        "b7d7132e0b4efba01f350fd07fea12a771b86d0cfde658b11a4212cd8e3c2d32";

    fn close(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() <= tol
    }

    #[test]
    fn type7_quantiles_match_hand_values() {
        let x = [1.0, 2.0, 3.0, 4.0];
        // h = 3 · 0.025 = 0.075 → 1 + 0.075; h = 3 · 0.975 = 2.925 → 3 + 0.925.
        assert!(close(type7_quantile(&x, 0.025).unwrap(), 1.075, 1e-12));
        assert!(close(type7_quantile(&x, 0.975).unwrap(), 3.925, 1e-12));
        assert_eq!(type7_quantile(&x, 0.0), Some(1.0));
        assert_eq!(type7_quantile(&x, 1.0), Some(4.0));
        assert_eq!(type7_quantile(&x, 0.5), Some(2.5));
        assert_eq!(type7_quantile(&[7.0], 0.025), Some(7.0));
        assert_eq!(type7_quantile(&[], 0.5), None);
    }

    /// Reference values computed independently by exact rational bisection (Python `fractions`).
    #[test]
    fn clopper_pearson_bounds_match_exact_values() {
        let cases: [(u64, f64, f64); 8] = [
            (0, 0.0, 0.073_972_785_347),
            (1, 0.000_527_315_253, 0.110_696_160_593),
            (2, 0.005_086_467_930, 0.142_540_973_201),
            (3, 0.013_079_244_410, 0.171_959_930_791),
            (10, 0.104_690_683_033, 0.349_910_240_257),
            (24, 0.352_251_630_553, 0.647_748_369_447),
            (47, 0.889_303_839_407, 0.999_472_684_747),
            (48, 0.926_027_214_653, 1.0),
        ];
        for (k, lo, hi) in cases {
            assert!(
                close(clopper_pearson_lower(k, 48, 0.05), lo, 1e-10),
                "lower k={k}: {}",
                clopper_pearson_lower(k, 48, 0.05)
            );
            assert!(
                close(clopper_pearson_upper(k, 48, 0.05), hi, 1e-10),
                "upper k={k}: {}",
                clopper_pearson_upper(k, 48, 0.05)
            );
        }
        // The §9 statements, to four decimals.
        assert_eq!(
            format!("{:.4}", clopper_pearson_upper(0, 48, 0.05)),
            "0.0740"
        );
        assert!(close(
            clopper_pearson_upper(0, 48, 0.05),
            1.0 - 0.025_f64.powf(1.0 / 48.0),
            1e-12
        ));
        assert_eq!(
            format!("{:.4}", clopper_pearson_upper(1, 48, 0.05)),
            "0.1107"
        );
        assert_eq!(
            format!("{:.4}", clopper_pearson_lower(48, 48, 0.05)),
            "0.9260"
        );
    }

    #[test]
    fn clopper_pearson_counts_use_floor_and_ceil_of_r_over_three() {
        // R = 1..3 of 144 all give k_hi = 1 → cp_hi ≈ 0.1107; R = 0 gives 0.0740.
        let draws = BootstrapDrawsV52::generate([0; 32], &[4; 12], 50).unwrap();
        for (r_total, k_lo, k_hi) in [(0_u32, 0, 0), (1, 0, 1), (2, 0, 1), (3, 1, 1), (4, 1, 2)] {
            let mut resolved = vec![0_u32; 48];
            let mut left = r_total;
            for slot in &mut resolved {
                let take = left.min(3);
                *slot = take;
                left -= take;
            }
            let entry = EntryOutcomesV52 {
                resolved,
                repeats: 3,
            };
            let totals = draws.replicate_totals(&entry.resolved).unwrap();
            let iv = gsrr_interval_v52(&entry, &totals, draws.statistic_denominator(3)).unwrap();
            assert_eq!((iv.cp_k_lo, iv.cp_k_hi), (k_lo, k_hi), "R={r_total}");
            assert!(iv.lo <= iv.point && iv.point <= iv.hi);
        }
    }

    #[test]
    fn bootstrap_key_and_keystream_match_independent_openssl_chacha20() {
        // node:crypto `chacha20` (OpenSSL) with a zero counter and nonce, keyed identically.
        let key = bootstrap_key_v52(DEV_EPISODES_SHA);
        assert_eq!(
            hex::encode(key),
            "a5b6d9d7b159f791f7cf672f402a92910c18c4aac740fdd0e66310dabc88f5c9"
        );
        let mut rng = ChaCha20RngV52::from_key(key);
        let words: Vec<u32> = (0..8).map(|_| rng.next_u32()).collect();
        assert_eq!(
            words,
            [
                1_490_611_025,
                3_624_591_844,
                1_265_474_225,
                2_549_235_193,
                2_525_704_710,
                2_453_193_736,
                806_110_130,
                4_141_728_205
            ]
        );
        // The first replicate's 36 draws (12 families × 3), `word & 3`.
        let draws = BootstrapDrawsV52::generate(key, &[4; 12], 1).unwrap();
        let first: String = draws
            .replicate_indices(0)
            .iter()
            .map(|i| char::from(b'0' + i))
            .collect();
        assert_eq!(first, "101120212232221311100000013301300332");
    }

    /// A hand-checked example: 2 strata × 3 scenarios (m = 2), k = 2 repeats, 4 replicates.
    #[test]
    fn hand_checked_small_bootstrap() {
        // Stratum 0 resolved [2, 0, 1]; stratum 1 resolved [1, 1, 0].
        let resolved = vec![2, 0, 1, 1, 1, 0];
        let draws = BootstrapDrawsV52::from_explicit(
            &[3, 3],
            &[
                vec![vec![0, 0], vec![2, 2]], // 2+2 + 0+0 = 4
                vec![vec![1, 2], vec![0, 1]], // 0+1 + 1+1 = 3
                vec![vec![0, 2], vec![0, 2]], // 2+1 + 1+0 = 4
                vec![vec![1, 1], vec![2, 2]], // 0+0 + 0+0 = 0
            ],
        )
        .unwrap();
        let totals = draws.replicate_totals(&resolved).unwrap();
        assert_eq!(totals, vec![4, 3, 4, 0]);
        // Statistic = T / (H·m·k) = T / 8 → [0.5, 0.375, 0.5, 0.0]; sorted [0, .375, .5, .5].
        assert_eq!(draws.statistic_denominator(2), 8);
        let entry = EntryOutcomesV52 {
            resolved,
            repeats: 2,
        };
        let iv = gsrr_interval_v52(&entry, &totals, 8).unwrap();
        // Point = 5 / 12. Type-7 at 0.025: h = 0.075 → 0 + 0.075·0.375 = 0.028125.
        // At 0.975: h = 2.925 → 0.5 + 0.925·0 = 0.5.
        assert!(close(iv.point, 5.0 / 12.0, 1e-15));
        assert!(close(iv.boot_lo, 0.028_125, 1e-15));
        assert!(close(iv.boot_hi, 0.5, 1e-15));
        // CP over n = 6 scenarios at k_lo = ⌊5/2⌋ = 2 and k_hi = ⌈5/2⌉ = 3.
        assert_eq!((iv.cp_n, iv.cp_k_lo, iv.cp_k_hi), (6, 2, 3));
        assert!(close(iv.cp_lo, clopper_pearson_lower(2, 6, 0.05), 0.0));
        assert!(close(iv.cp_hi, clopper_pearson_upper(3, 6, 0.05), 0.0));
        assert_eq!(iv.lo, iv.boot_lo.min(iv.cp_lo));
        assert_eq!(iv.hi, iv.boot_hi.max(iv.cp_hi));
    }

    #[test]
    fn rank_intervals_average_ties_and_rank_one_is_highest() {
        // Three entries over four replicates.
        let totals = vec![
            vec![10, 5, 7, 7], // A
            vec![10, 9, 3, 7], // B
            vec![1, 9, 8, 7],  // C
        ];
        // Replicate ranks: b0 A=B=1.5, C=3; b1 B=C=1.5, A=3; b2 C=1, A=2, B=3; b3 all 2.
        // A: [1.5, 3, 2, 2] → sorted [1.5, 2, 2, 3]; B: [1.5, 1.5, 3, 2] → [1.5, 1.5, 2, 3];
        // C: [3, 1.5, 1, 2] → [1, 1.5, 2, 3].
        let iv = rank_intervals_v52(&totals);
        // A: lo = 1.5 + 0.075·0.5 = 1.5375; hi = 2 + 0.925·1 = 2.925.
        assert!(close(iv[0].lo, 1.5375, 1e-12) && close(iv[0].hi, 2.925, 1e-12));
        // B: lo = 1.5; hi = 2 + 0.925 = 2.925.
        assert!(close(iv[1].lo, 1.5, 1e-12) && close(iv[1].hi, 2.925, 1e-12));
        // C: lo = 1 + 0.075·0.5 = 1.0375; hi = 2.925.
        assert!(close(iv[2].lo, 1.0375, 1e-12) && close(iv[2].hi, 2.925, 1e-12));
        assert!(rank_intervals_v52(&[]).is_empty());
        // A single entry always ranks 1.
        let single = rank_intervals_v52(&[vec![3, 1, 4]]);
        assert_eq!((single[0].lo, single[0].hi), (1.0, 1.0));
    }

    #[test]
    fn degenerate_all_fail_and_all_pass_entries() {
        let draws = BootstrapDrawsV52::generate(bootstrap_key_v52(DEV_EPISODES_SHA), &[4; 12], 200)
            .unwrap();
        let fail = EntryOutcomesV52 {
            resolved: vec![0; 48],
            repeats: 3,
        };
        let pass = EntryOutcomesV52 {
            resolved: vec![3; 48],
            repeats: 3,
        };
        let joint = joint_statistics_v52(&draws, &[fail, pass], &[true, true]).unwrap();
        let (f, p) = (&joint.intervals[0], &joint.intervals[1]);
        assert_eq!(
            (f.point, f.boot_lo, f.boot_hi, f.cp_lo),
            (0.0, 0.0, 0.0, 0.0)
        );
        assert!(close(f.hi, 0.073_972_785_347, 1e-10));
        assert_eq!(
            (p.point, p.boot_lo, p.boot_hi, p.cp_hi, p.hi),
            (1.0, 1.0, 1.0, 1.0, 1.0)
        );
        assert!(close(p.lo, 0.926_027_214_653, 1e-10));
        assert_eq!(
            joint.rank_intervals[1],
            Some(RankIntervalV52 { lo: 1.0, hi: 1.0 })
        );
        assert_eq!(
            joint.rank_intervals[0],
            Some(RankIntervalV52 { lo: 2.0, hi: 2.0 })
        );
        // One resolved slot of 144: k_lo = 0, k_hi = 1 → [0, 0.1107].
        let mut one = vec![0; 48];
        one[17] = 1;
        let joint = joint_statistics_v52(
            &draws,
            &[EntryOutcomesV52 {
                resolved: one,
                repeats: 3,
            }],
            &[false],
        )
        .unwrap();
        assert_eq!(joint.intervals[0].lo, 0.0);
        assert_eq!(format!("{:.4}", joint.intervals[0].hi), "0.1107");
        assert_eq!(joint.rank_intervals[0], None);
    }

    #[test]
    fn shape_errors_are_rejected() {
        assert_eq!(
            BootstrapDrawsV52::generate([0; 32], &[], 1),
            Err(StatsErrorV52::NoStrata)
        );
        assert!(matches!(
            BootstrapDrawsV52::generate([0; 32], &[1; 12], 1),
            Err(StatsErrorV52::StratumTooSmall(_))
        ));
        assert!(matches!(
            BootstrapDrawsV52::generate([0; 32], &[4, 3], 1),
            Err(StatsErrorV52::UnequalStrata(_))
        ));
        let draws = BootstrapDrawsV52::generate([0; 32], &[4; 12], 2).unwrap();
        assert!(draws.replicate_totals(&[0; 47]).is_err());
        let bad = EntryOutcomesV52 {
            resolved: vec![4; 48],
            repeats: 3,
        };
        assert!(joint_statistics_v52(&draws, &[bad], &[true]).is_err());
    }

    #[test]
    fn bootstrap_is_deterministic_and_pinned_for_a_fixed_entry() {
        let key = bootstrap_key_v52(DEV_EPISODES_SHA);
        let a = BootstrapDrawsV52::generate(key, &[4; 12], BOOTSTRAP_REPLICATES_V52).unwrap();
        let b = BootstrapDrawsV52::generate(key, &[4; 12], BOOTSTRAP_REPLICATES_V52).unwrap();
        assert_eq!(a, b);
        assert_eq!(a.replicates(), 10_000);
        // A fixed mixed entry: scenario e resolves (e mod 4) repeats capped at 3.
        let resolved: Vec<u32> = (0..48).map(|e| (e % 4).min(3)).collect();
        let entry = EntryOutcomesV52 {
            resolved,
            repeats: 3,
        };
        let t1 = a.replicate_totals(&entry.resolved).unwrap();
        let t2 = b.replicate_totals(&entry.resolved).unwrap();
        assert_eq!(t1, t2);
        let iv = gsrr_interval_v52(&entry, &t1, a.statistic_denominator(3)).unwrap();
        // Point: Σ (0+1+2+3)·12 = 72 of 144.
        assert_eq!(iv.point, 0.5);
        assert_eq!((iv.cp_k_lo, iv.cp_k_hi), (24, 24));
        // Golden bootstrap quantiles under the dev-suite key (pins the draw loop end to end).
        assert_eq!(
            format!("{:.6} {:.6}", iv.boot_lo, iv.boot_hi),
            GOLDEN_MIXED_BOOT
        );
    }

    /// Cross-checked by an independent implementation (OpenSSL `chacha20` via `node:crypto`, the
    /// same draw loop and Type-7 rule): `41/108` and `67/108`.
    const GOLDEN_MIXED_BOOT: &str = "0.379630 0.620370";

    #[test]
    fn leaderboard_order_applies_every_key_and_null_last() {
        let base = LeaderboardOrderKeyV52 {
            g_srr: RatioV52::new(72, 144),
            pass_hat_3: Some(RatioV52::new(10, 48)),
            macro_cat_acc: 0.8,
            commit_rate: RatioV52::new(140, 144),
            violations_per_committed: Some(RatioV52::new(3, 140)),
            model_id: "m".to_owned(),
            reasoning_effort: "high".to_owned(),
            transport_id: "anthropic".to_owned(),
        };
        let mk = |f: &dyn Fn(&mut LeaderboardOrderKeyV52)| {
            let mut k = base.clone();
            f(&mut k);
            k
        };
        let keys = vec![
            mk(&|k| k.model_id = "z".to_owned()), // 0: tie to 1 except id
            base.clone(),                         // 1
            mk(&|k| k.violations_per_committed = None), // 2: null last on key 5
            mk(&|k| k.violations_per_committed = Some(RatioV52::new(0, 140))), // 3: fewer violations
            mk(&|k| k.commit_rate = RatioV52::new(144, 144)), // 4: higher commit rate
            mk(&|k| k.macro_cat_acc = 0.9),                   // 5: higher macro
            mk(&|k| k.pass_hat_3 = Some(RatioV52::new(11, 48))), // 6: higher pass^3
            mk(&|k| k.g_srr = RatioV52::new(73, 144)),        // 7: higher G-SRR
            mk(&|k| k.g_srr = RatioV52::new(1, 2)),           // 8: equal to 72/144 exactly
            mk(&|k| k.pass_hat_3 = None),                     // 9: null pass^3 last on key 2
        ];
        let order = leaderboard_order_v52(&keys);
        assert_eq!(order, vec![7, 6, 5, 4, 3, 1, 8, 0, 2, 9]);
    }
}
