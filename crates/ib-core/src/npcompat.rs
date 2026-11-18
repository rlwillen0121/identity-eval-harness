//! Bit-exact port of the NumPy 2.5.4 primitives used by IdentityBench v5 scoring.
//!
//! Includes [`SeedSequence`], [`Pcg64`], [`Generator`] (Lemire bounded integers and
//! float64 draws), [`mean_pairwise`], [`mean_axis1`], [`percentile_linear`],
//! [`percentiles_linear`], and [`dot`].

use std::fmt;

const INIT_A: u32 = 0x43b0_d7e5;
const MULT_A: u32 = 0x931e_8875;
const INIT_B: u32 = 0x8b51_f9dd;
const MULT_B: u32 = 0x58f3_8ded;
const MIX_MULT_L: u32 = 0xca01_f9dd;
const MIX_MULT_R: u32 = 0x4973_f715;
const XSHIFT: u32 = 16;
const DEFAULT_POOL_SIZE: usize = 4;

const PCG_DEFAULT_MULTIPLIER_128: u128 = 0x2360_ED05_1FC6_5DA4_4385_DF64_9FCC_F645;
const PW_BLOCKSIZE: usize = 128;

/// Errors returned by NumPy-compatible helpers.
#[derive(Debug, thiserror::Error, PartialEq)]
pub enum NpError {
    #[error("cannot compute percentile of an empty slice")]
    EmptyInput,
    #[error("percentiles must be in the range [0, 100], got {0}")]
    InvalidPercentile(f64),
    #[error("invalid integer range [{low}, {high}): high must be > low")]
    InvalidRange { low: i64, high: i64 },
    #[error("matrix length {len} does not match {rows}x{cols}")]
    ShapeMismatch {
        len: usize,
        rows: usize,
        cols: usize,
    },
}

/// Port of `numpy.random.SeedSequence` (without `spawn_key`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SeedSequence {
    pool: Vec<u32>,
}

fn hashmix(value: u32, hash_const: &mut u32) -> u32 {
    let mut v = value ^ *hash_const;
    *hash_const = hash_const.wrapping_mul(MULT_A);
    v = v.wrapping_mul(*hash_const);
    v ^ (v >> XSHIFT)
}

fn mix(x: u32, y: u32) -> u32 {
    let result = MIX_MULT_L
        .wrapping_mul(x)
        .wrapping_sub(MIX_MULT_R.wrapping_mul(y));
    result ^ (result >> XSHIFT)
}

fn u128_to_le_u32_words(mut value: u128) -> Vec<u32> {
    let mut words = Vec::new();
    while value > 0 {
        words.push((value & 0xffff_ffff) as u32);
        value >>= 32;
    }
    words
}

impl SeedSequence {
    /// Create a `SeedSequence` from a `u64` integer seed (`0` yields empty entropy).
    #[must_use]
    pub fn from_u64(seed: u64) -> Self {
        Self::from_u128(u128::from(seed))
    }

    /// Create a `SeedSequence` from a `u128` integer seed (`0` yields empty entropy).
    #[must_use]
    pub fn from_u128(seed: u128) -> Self {
        let words = u128_to_le_u32_words(seed);
        Self::from_u32_words(&words, DEFAULT_POOL_SIZE)
    }

    /// Create a `SeedSequence` from little-endian `u32` entropy words.
    ///
    /// Trailing zero words are stripped before mixing (matching NumPy's `0 -> []` and
    /// `[0] -> []` convention), and `pool_size` defaults to 4 when 0 is passed.
    #[must_use]
    pub fn from_u32_words(words: &[u32], pool_size: usize) -> Self {
        let pool_size = if pool_size == 0 {
            DEFAULT_POOL_SIZE
        } else {
            pool_size
        };
        let trimmed_len = words.iter().rposition(|&w| w != 0).map_or(0, |idx| idx + 1);
        let entropy = &words[..trimmed_len];

        let mut pool = vec![0u32; pool_size];
        let mut hash_const = INIT_A;

        for (i, slot) in pool.iter_mut().enumerate() {
            let word = entropy.get(i).copied().unwrap_or(0);
            *slot = hashmix(word, &mut hash_const);
        }

        for i_src in 0..pool_size {
            let src_val = pool[i_src];
            for (i_dst, slot) in pool.iter_mut().enumerate() {
                if i_src != i_dst {
                    let mixed = hashmix(src_val, &mut hash_const);
                    *slot = mix(*slot, mixed);
                }
            }
        }

        for &word in entropy.iter().skip(pool_size) {
            for slot in &mut pool {
                let mixed = hashmix(word, &mut hash_const);
                *slot = mix(*slot, mixed);
            }
        }

        Self { pool }
    }

    /// Create a `SeedSequence` from a `u64` seed using the default pool size (4).
    #[must_use]
    pub fn new(seed: u64) -> Self {
        Self::from_u64(seed)
    }

    /// Inspect the internal `u32` entropy pool.
    #[must_use]
    pub fn pool(&self) -> &[u32] {
        &self.pool
    }

    /// Generate `n` `u32` words from the mixed entropy pool.
    #[must_use]
    pub fn generate_state_u32(&self, n: usize) -> Vec<u32> {
        let mut state = Vec::with_capacity(n);
        let mut hash_const = INIT_B;
        let pool_len = self.pool.len();
        for i in 0..n {
            let mut data_val = self.pool[i % pool_len];
            data_val ^= hash_const;
            hash_const = hash_const.wrapping_mul(MULT_B);
            data_val = data_val.wrapping_mul(hash_const);
            data_val ^= data_val >> XSHIFT;
            state.push(data_val);
        }
        state
    }

    /// Generate `n` `u64` words (pairs of little-endian `u32` words).
    #[must_use]
    pub fn generate_state_u64(&self, n: usize) -> Vec<u64> {
        let u32_words = self.generate_state_u32(n.saturating_mul(2));
        let (pairs, _) = u32_words.as_chunks::<2>();
        pairs
            .iter()
            .map(|&[lo, hi]| u64::from(lo) | (u64::from(hi) << 32))
            .collect()
    }
}

/// Port of `numpy.random.PCG64` (PCG-XSL-RR-128/64 with LCG multiplier and `SeedSequence`).
#[derive(Clone, PartialEq, Eq)]
pub struct Pcg64 {
    state: u128,
    inc: u128,
    has_uint32: bool,
    uinteger: u32,
}

impl fmt::Debug for Pcg64 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Pcg64")
            .field("has_uint32", &self.has_uint32)
            .finish_non_exhaustive()
    }
}

impl Pcg64 {
    /// Construct and seed `Pcg64` from a `u64` seed via `SeedSequence`.
    #[must_use]
    pub fn new(seed: u64) -> Self {
        Self::from_seed_sequence(&SeedSequence::from_u64(seed))
    }

    /// Construct and seed `Pcg64` from an existing [`SeedSequence`].
    #[must_use]
    pub fn from_seed_sequence(seq: &SeedSequence) -> Self {
        let s = seq.generate_state_u64(4);
        Self::from_state_words([s[0], s[1], s[2], s[3]])
    }

    /// Seed `Pcg64` directly from 4 `u64` state words (`pcg_setseq_128_srandom_r`).
    #[must_use]
    pub fn from_state_words(s: [u64; 4]) -> Self {
        let initstate = (u128::from(s[0]) << 64) | u128::from(s[1]);
        let initseq = (u128::from(s[2]) << 64) | u128::from(s[3]);
        let mut pcg = Self {
            state: 0,
            inc: (initseq << 1) | 1,
            has_uint32: false,
            uinteger: 0,
        };
        pcg.step();
        pcg.state = pcg.state.wrapping_add(initstate);
        pcg.step();
        pcg
    }

    #[inline]
    fn step(&mut self) {
        self.state = self
            .state
            .wrapping_mul(PCG_DEFAULT_MULTIPLIER_128)
            .wrapping_add(self.inc);
    }

    #[inline]
    fn output_xsl_rr(state: u128) -> u64 {
        let xored = ((state >> 64) ^ state) as u64;
        let rot = (state >> 122) as u32;
        xored.rotate_right(rot)
    }

    /// Step the 128-bit LCG and emit the next 64-bit XSL-RR output.
    #[inline]
    pub fn next_u64(&mut self) -> u64 {
        self.step();
        Self::output_xsl_rr(self.state)
    }

    /// Emit the next 32-bit unsigned integer using NumPy's low-then-high `u64` buffer.
    #[inline]
    pub fn next_u32(&mut self) -> u32 {
        if self.has_uint32 {
            self.has_uint32 = false;
            self.uinteger
        } else {
            let next = self.next_u64();
            self.has_uint32 = true;
            self.uinteger = (next >> 32) as u32;
            next as u32
        }
    }

    /// Emit the next uniform `[0.0, 1.0)` `f64` matching NumPy's `next_double`.
    #[inline]
    pub fn next_f64(&mut self) -> f64 {
        ((self.next_u64() >> 11) as f64) * (1.0 / 9_007_199_254_740_992.0)
    }

    /// Return `(state, inc, has_uint32, uinteger)` matching NumPy's `PCG64.state` dict.
    #[must_use]
    pub fn state_tuple(&self) -> (u128, u128, bool, u32) {
        (self.state, self.inc, self.has_uint32, self.uinteger)
    }
}

/// Port of `numpy.random.Generator` wrapping [`Pcg64`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Generator {
    pcg: Pcg64,
}

impl Generator {
    /// Construct `Generator(PCG64(seed))`.
    #[must_use]
    pub fn new(seed: u64) -> Self {
        Self {
            pcg: Pcg64::new(seed),
        }
    }

    /// Construct a `Generator` from an existing [`Pcg64`].
    #[must_use]
    pub fn from_pcg64(pcg: Pcg64) -> Self {
        Self { pcg }
    }

    /// Borrow the underlying [`Pcg64`] bit generator.
    #[must_use]
    pub fn bit_generator(&self) -> &Pcg64 {
        &self.pcg
    }

    /// Mutably borrow the underlying [`Pcg64`] bit generator.
    pub fn bit_generator_mut(&mut self) -> &mut Pcg64 {
        &mut self.pcg
    }

    /// Draw a single uniform `[0.0, 1.0)` `f64`.
    pub fn random(&mut self) -> f64 {
        self.pcg.next_f64()
    }

    #[inline]
    fn bounded_u32(&mut self, rng: u32) -> u32 {
        if rng == 0 {
            return 0;
        }
        if rng == u32::MAX {
            return self.pcg.next_u32();
        }
        let bound = u64::from(rng) + 1;
        let bound32 = bound as u32;
        let mut m = u64::from(self.pcg.next_u32()) * bound;
        let mut leftover = m as u32;
        if leftover < bound32 {
            let threshold = bound32.wrapping_neg() % bound32;
            while leftover < threshold {
                m = u64::from(self.pcg.next_u32()) * bound;
                leftover = m as u32;
            }
        }
        (m >> 32) as u32
    }

    #[inline]
    fn bounded_u64(&mut self, rng: u64) -> u64 {
        if let Ok(rng32) = u32::try_from(rng) {
            return u64::from(self.bounded_u32(rng32));
        }
        if rng == u64::MAX {
            return self.pcg.next_u64();
        }
        let bound = u128::from(rng) + 1;
        let bound64 = bound as u64;
        let mut m = u128::from(self.pcg.next_u64()) * bound;
        let mut leftover = m as u64;
        if leftover < bound64 {
            let threshold = bound64.wrapping_neg() % bound64;
            while leftover < threshold {
                m = u128::from(self.pcg.next_u64()) * bound;
                leftover = m as u64;
            }
        }
        (m >> 64) as u64
    }

    fn validated_range_off(low: i64, high: i64) -> Result<u64, NpError> {
        if high <= low {
            return Err(NpError::InvalidRange { low, high });
        }
        let span = (i128::from(high) - i128::from(low)) as u64;
        Ok(span - 1)
    }

    /// Generate `count` `i64` values matching
    /// `rng.integers(low, high, size=count, dtype=np.int64, endpoint=False)`.
    ///
    /// # Errors
    /// Returns [`NpError::InvalidRange`] if `high <= low`.
    pub fn integers_i64(&mut self, low: i64, high: i64, count: usize) -> Result<Vec<i64>, NpError> {
        let off = Self::validated_range_off(low, high)?;
        let mut out = Vec::with_capacity(count);
        if let Ok(off32) = u32::try_from(off) {
            for _ in 0..count {
                let offset = u64::from(self.bounded_u32(off32));
                out.push(low.wrapping_add_unsigned(offset));
            }
        } else {
            for _ in 0..count {
                let offset = self.bounded_u64(off);
                out.push(low.wrapping_add_unsigned(offset));
            }
        }
        Ok(out)
    }

    /// Generate a C-order `(rows, cols)` matrix of `usize` indices matching
    /// `rng.integers(low, high, size=(rows, cols), dtype=np.int64, endpoint=False)`.
    ///
    /// # Errors
    /// Returns [`NpError::InvalidRange`] if `low < 0` or `high <= low`, or
    /// [`NpError::ShapeMismatch`] if `rows * cols` overflows `usize`.
    pub fn integers_usize_2d(
        &mut self,
        low: i64,
        high: i64,
        rows: usize,
        cols: usize,
    ) -> Result<Vec<usize>, NpError> {
        if low < 0 || high <= low {
            return Err(NpError::InvalidRange { low, high });
        }
        let count = rows.checked_mul(cols).ok_or(NpError::ShapeMismatch {
            len: usize::MAX,
            rows,
            cols,
        })?;
        let raw = self.integers_i64(low, high, count)?;
        Ok(raw
            .into_iter()
            .map(|v| usize::try_from(v).expect("low >= 0 ensures non-negative values"))
            .collect())
    }
}

/// NumPy-compatible `pairwise_sum` for `f64` slices (`PW_BLOCKSIZE = 128`).
fn pairwise_sum(values: &[f64]) -> f64 {
    let n = values.len();
    if n < 8 {
        let mut sum = 0.0_f64;
        for &v in values {
            sum += v;
        }
        return sum;
    }
    if n <= PW_BLOCKSIZE {
        let mut r = [0.0_f64; 8];
        let (chunks, rem) = values.as_chunks::<8>();
        for chunk in chunks {
            for (acc, &x) in r.iter_mut().zip(chunk) {
                *acc += x;
            }
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for &v in rem {
            res += v;
        }
        return res;
    }
    let split = ((n / 2) / 8) * 8;
    pairwise_sum(&values[..split]) + pairwise_sum(&values[split..])
}

/// Compute `np.asarray(values, dtype=np.float64).mean()` using NumPy's `pairwise_sum`.
///
/// Returns `f64::NAN` for an empty slice (`0.0 / 0.0`).
#[must_use]
pub fn mean_pairwise(values: &[f64]) -> f64 {
    pairwise_sum(values) / (values.len() as f64)
}

/// Compute `matrix.mean(axis=1)` on a C-order `(rows, cols)` matrix using NumPy's `pairwise_sum`.
///
/// # Panics
/// Panics if `matrix.len() != rows * cols` or `rows * cols` overflows `usize`.
#[must_use]
pub fn mean_axis1(matrix: &[f64], rows: usize, cols: usize) -> Vec<f64> {
    let expected = rows.checked_mul(cols).expect("rows * cols overflows usize");
    assert_eq!(
        matrix.len(),
        expected,
        "matrix length {} does not match {rows}x{cols}",
        matrix.len()
    );
    if cols == 0 {
        return vec![f64::NAN; rows];
    }
    matrix.chunks_exact(cols).map(mean_pairwise).collect()
}

fn lerp(a: f64, b: f64, t: f64) -> f64 {
    let diff_b_a = b - a;
    if t >= 0.5 {
        b - diff_b_a * (1.0 - t)
    } else {
        a + diff_b_a * t
    }
}

fn eval_percentile_sorted(sorted: &[f64], q_percent: f64) -> f64 {
    let n = sorted.len();
    if sorted[n - 1].is_nan() {
        return sorted[n - 1];
    }
    let q = q_percent / 100.0;
    let virtual_index = ((n - 1) as f64) * q;

    let (prev_signed, prev_idx, next_idx) = if virtual_index >= (n - 1) as f64 {
        (-1_isize, n - 1, n - 1)
    } else if virtual_index < 0.0 {
        (0_isize, 0, 0)
    } else {
        let p = virtual_index.floor() as usize;
        (p as isize, p, (p + 1).min(n - 1))
    };

    let gamma = virtual_index - (prev_signed as f64);
    lerp(sorted[prev_idx], sorted[next_idx], gamma)
}

fn validate_q_percent(q_percent: f64) -> Result<(), NpError> {
    if q_percent.is_nan() || !(0.0..=100.0).contains(&q_percent) {
        return Err(NpError::InvalidPercentile(q_percent));
    }
    Ok(())
}

/// Compute `float(np.percentile(values, q_percent, method="linear"))`.
///
/// # Errors
/// Returns [`NpError::EmptyInput`] if `values` is empty, or
/// [`NpError::InvalidPercentile`] if `q_percent` is NaN or outside `[0.0, 100.0]`.
pub fn percentile_linear(values: &[f64], q_percent: f64) -> Result<f64, NpError> {
    if values.is_empty() {
        return Err(NpError::EmptyInput);
    }
    validate_q_percent(q_percent)?;
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    Ok(eval_percentile_sorted(&sorted, q_percent))
}

/// Compute `np.percentile(values, qs_percent, method="linear")` in one sort pass.
///
/// # Errors
/// Returns [`NpError::EmptyInput`] if `values` is empty, or
/// [`NpError::InvalidPercentile`] if any entry in `qs_percent` is NaN or outside `[0.0, 100.0]`.
pub fn percentiles_linear(values: &[f64], qs_percent: &[f64]) -> Result<Vec<f64>, NpError> {
    if values.is_empty() {
        return Err(NpError::EmptyInput);
    }
    for &q in qs_percent {
        validate_q_percent(q)?;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    Ok(qs_percent
        .iter()
        .map(|&q| eval_percentile_sorted(&sorted, q))
        .collect())
}

/// 1D `float64` dot product (`np.dot(a, b)`).
///
/// # Panics
/// Panics if `a.len() != b.len()`.
#[must_use]
pub fn dot(a: &[f64], b: &[f64]) -> f64 {
    assert_eq!(
        a.len(),
        b.len(),
        "dot product length mismatch: {} vs {}",
        a.len(),
        b.len()
    );
    let mut acc = 0.0_f64;
    for (&x, &y) in a.iter().zip(b.iter()) {
        acc += x * y;
    }
    acc
}
