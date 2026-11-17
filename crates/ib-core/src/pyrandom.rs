//! Bit-exact port of CPython 3.12 `random.Random` (MT19937 core + the pure-Python
//! methods in `Lib/random.py` that IdentityBench uses).

use sha2::{Digest, Sha512};

const N: usize = 624;
const M: usize = 397;
const MATRIX_A: u32 = 0x9908_b0df;
const UPPER_MASK: u32 = 0x8000_0000;
const LOWER_MASK: u32 = 0x7fff_ffff;

/// CPython `random.Random`.
#[derive(Clone)]
pub struct PyRandom {
    mt: [u32; N],
    mti: usize,
}

impl std::fmt::Debug for PyRandom {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PyRandom").field("mti", &self.mti).finish_non_exhaustive()
    }
}

impl PyRandom {
    fn blank() -> Self {
        Self { mt: [0; N], mti: N + 1 }
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..N {
            let prev = self.mt[i - 1];
            self.mt[i] = 1_812_433_253u32
                .wrapping_mul(prev ^ (prev >> 30))
                .wrapping_add(u32::try_from(i).expect("i < 624"));
        }
        self.mti = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19_650_218);
        let mut i: usize = 1;
        let mut j: usize = 0;
        let key_length = key.len();
        let mut k = N.max(key_length);
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ ((prev ^ (prev >> 30)).wrapping_mul(1_664_525)))
                .wrapping_add(key[j])
                .wrapping_add(u32::try_from(j).expect("key index fits u32"));
            i += 1;
            j += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            if j >= key_length {
                j = 0;
            }
            k -= 1;
        }
        k = N - 1;
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ ((prev ^ (prev >> 30)).wrapping_mul(1_566_083_941)))
                .wrapping_sub(u32::try_from(i).expect("i < 624"));
            i += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            k -= 1;
        }
        self.mt[0] = 0x8000_0000;
        self.mti = N;
    }

    /// Seed from a non-negative integer given as big-endian magnitude bytes
    /// (`random.Random(int)`; CPython uses `abs(seed)`).
    #[must_use]
    pub fn from_int_be_bytes(magnitude_be: &[u8]) -> Self {
        // Strip leading zero bytes, then pack into little-endian 32-bit words.
        let first = magnitude_be.iter().position(|b| *b != 0).unwrap_or(magnitude_be.len());
        let bytes = &magnitude_be[first..];
        let mut key: Vec<u32> = Vec::with_capacity(bytes.len() / 4 + 1);
        let mut idx = bytes.len();
        while idx > 0 {
            let start = idx.saturating_sub(4);
            let mut word: u32 = 0;
            for b in &bytes[start..idx] {
                word = (word << 8) | u32::from(*b);
            }
            key.push(word);
            idx = start;
        }
        if key.is_empty() {
            key.push(0);
        }
        let mut rng = Self::blank();
        rng.init_by_array(&key);
        rng
    }

    /// `random.Random(n)` for an integer seed (negative seeds use `abs`).
    #[must_use]
    pub fn from_i128(seed: i128) -> Self {
        Self::from_int_be_bytes(&seed.unsigned_abs().to_be_bytes())
    }

    /// `random.Random(n)` for a `u64` seed.
    #[must_use]
    pub fn from_u64(seed: u64) -> Self {
        Self::from_int_be_bytes(&seed.to_be_bytes())
    }

    /// `random.Random(b)` (version 2) for bytes: `int.from_bytes(b + sha512(b).digest(), "big")`.
    #[must_use]
    pub fn from_bytes(seed: &[u8]) -> Self {
        let mut material = seed.to_vec();
        material.extend_from_slice(&Sha512::digest(seed));
        Self::from_int_be_bytes(&material)
    }

    /// `random.Random(s)` (version 2) for a `str` seed (UTF-8 encoded).
    #[must_use]
    pub fn from_str_seed(seed: &str) -> Self {
        Self::from_bytes(seed.as_bytes())
    }

    /// MT19937 `genrand_uint32`.
    pub fn genrand_u32(&mut self) -> u32 {
        if self.mti >= N {
            let mag01 = [0u32, MATRIX_A];
            let mut kk = 0;
            while kk < N - M {
                let y = (self.mt[kk] & UPPER_MASK) | (self.mt[kk + 1] & LOWER_MASK);
                self.mt[kk] = self.mt[kk + M] ^ (y >> 1) ^ mag01[(y & 1) as usize];
                kk += 1;
            }
            while kk < N - 1 {
                let y = (self.mt[kk] & UPPER_MASK) | (self.mt[kk + 1] & LOWER_MASK);
                self.mt[kk] = self.mt[kk + M - N] ^ (y >> 1) ^ mag01[(y & 1) as usize];
                kk += 1;
            }
            let y = (self.mt[N - 1] & UPPER_MASK) | (self.mt[0] & LOWER_MASK);
            self.mt[N - 1] = self.mt[M - 1] ^ (y >> 1) ^ mag01[(y & 1) as usize];
            self.mti = 0;
        }
        let mut y = self.mt[self.mti];
        self.mti += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `random.random()`.
    pub fn random(&mut self) -> f64 {
        let a = self.genrand_u32() >> 5;
        let b = self.genrand_u32() >> 6;
        (f64::from(a) * 67_108_864.0 + f64::from(b)) * (1.0 / 9_007_199_254_740_992.0)
    }

    /// `random.getrandbits(k)` for `k <= 128`.
    ///
    /// # Panics
    /// Panics if `k > 128`.
    pub fn getrandbits(&mut self, k: u32) -> u128 {
        assert!(k <= 128, "getrandbits beyond 128 bits is not supported");
        if k == 0 {
            return 0;
        }
        if k <= 32 {
            return u128::from(self.genrand_u32() >> (32 - k));
        }
        let words = (k - 1) / 32 + 1;
        let mut remaining = k;
        let mut result: u128 = 0;
        for i in 0..words {
            let mut r = self.genrand_u32();
            if remaining < 32 {
                r >>= 32 - remaining;
            }
            result |= u128::from(r) << (32 * i);
            remaining = remaining.saturating_sub(32);
        }
        result
    }

    /// `random._randbelow(n)` (the getrandbits variant). Returns 0 for `n == 0`
    /// to mirror callers that guard emptiness themselves.
    pub fn randbelow(&mut self, n: u128) -> u128 {
        if n == 0 {
            return 0;
        }
        let k = 128 - n.leading_zeros();
        let mut r = self.getrandbits(k);
        while r >= n {
            r = self.getrandbits(k);
        }
        r
    }

    /// `random._randbelow(n)` for `usize` bounds.
    pub fn randbelow_usize(&mut self, n: usize) -> usize {
        usize::try_from(self.randbelow(n as u128)).expect("result < n")
    }

    /// `random.randrange(start, stop)` with step 1.
    ///
    /// # Panics
    /// Panics on an empty range (CPython raises `ValueError`).
    pub fn randrange(&mut self, start: i128, stop: i128) -> i128 {
        assert!(stop > start, "empty range for randrange()");
        let width = u128::try_from(stop - start).expect("positive width");
        start + i128::try_from(self.randbelow(width)).expect("fits")
    }

    /// `random.randint(a, b)` (inclusive).
    pub fn randint(&mut self, a: i128, b: i128) -> i128 {
        self.randrange(a, b + 1)
    }

    /// `random.choice(seq)`. Returns `None` for an empty slice (CPython raises).
    pub fn choice<'a, T>(&mut self, seq: &'a [T]) -> Option<&'a T> {
        if seq.is_empty() {
            return None;
        }
        let idx = self.randbelow_usize(seq.len());
        seq.get(idx)
    }

    /// `random.shuffle(x)` in place.
    pub fn shuffle<T>(&mut self, x: &mut [T]) {
        for i in (1..x.len()).rev() {
            let j = self.randbelow_usize(i + 1);
            x.swap(i, j);
        }
    }

    /// `random.sample(population, k)` (no `counts`).
    ///
    /// # Panics
    /// Panics if `k > population.len()` (CPython raises `ValueError`).
    pub fn sample<T: Clone>(&mut self, population: &[T], k: usize) -> Vec<T> {
        let n = population.len();
        assert!(k <= n, "Sample larger than population or is negative");
        let mut setsize: usize = 21;
        if k > 5 {
            #[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let exp = ((k as f64 * 3.0).ln() / 4f64.ln()).ceil() as u32;
            setsize += 4usize.pow(exp);
        }
        let mut result = Vec::with_capacity(k);
        if n <= setsize {
            let mut pool: Vec<T> = population.to_vec();
            for i in 0..k {
                let j = self.randbelow_usize(n - i);
                result.push(pool[j].clone());
                pool[j] = pool[n - i - 1].clone();
            }
        } else {
            let mut selected = std::collections::HashSet::with_capacity(k);
            for _ in 0..k {
                let mut j = self.randbelow_usize(n);
                while selected.contains(&j) {
                    j = self.randbelow_usize(n);
                }
                selected.insert(j);
                result.push(population[j].clone());
            }
        }
        result
    }

    /// `random.uniform(a, b)`.
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    /// `random.choices(population, k=k)` without weights.
    pub fn choices_unweighted<T: Clone>(&mut self, population: &[T], k: usize) -> Vec<T> {
        #[allow(clippy::cast_precision_loss)]
        let n = population.len() as f64;
        (0..k)
            .map(|_| {
                #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
                let idx = (self.random() * n).floor() as usize;
                population[idx].clone()
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn int_seed_matches_cpython() {
        // python: random.Random(42).random() == 0.6394267984578837
        let mut r = PyRandom::from_u64(42);
        assert_eq!(crate::pyfloat::repr(r.random()), "0.6394267984578837");
    }

    #[test]
    fn str_seed_shuffle_matches_cpython() {
        // python: x=list(range(10)); random.Random("abc").shuffle(x); x
        let mut x: Vec<u32> = (0..10).collect();
        PyRandom::from_str_seed("abc").shuffle(&mut x);
        assert_eq!(x, vec![4, 3, 0, 9, 1, 2, 6, 5, 7, 8]);
    }
}
