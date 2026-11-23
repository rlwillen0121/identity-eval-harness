//! Frozen-rate token cost estimation in integer micros.

use serde::{Deserialize, Serialize};

use crate::error::AccountingError;

/// Frozen per-million-token pricing rate anchored to a pricing manifest SHA-256.
///
/// Optional component rates (cached input, cache write, reasoning) are charged only when present;
/// when a component rate is present, the matching token subset must be known or estimation
/// declines rather than guessing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FrozenPricingRate {
    pub pricing_manifest_sha256: String,
    pub currency_code: String,
    pub input_cost_per_million_micros: i64,
    #[serde(default)]
    pub cached_input_cost_per_million_micros: Option<i64>,
    #[serde(default)]
    pub cache_write_cost_per_million_micros: Option<i64>,
    pub output_cost_per_million_micros: i64,
    #[serde(default)]
    pub reasoning_cost_per_million_micros: Option<i64>,
}

impl FrozenPricingRate {
    /// Check the currency code length and that every configured rate is non-negative.
    ///
    /// # Errors
    /// Returns [`AccountingError::Validation`] naming the first offending field.
    pub fn validate(&self) -> Result<(), AccountingError> {
        let len = self.currency_code.chars().count();
        if !(1..=16).contains(&len) {
            return Err(AccountingError::Validation(format!(
                "FrozenPricingRate.currency_code length {len} outside [1, 16]"
            )));
        }
        for (field, val) in [
            (
                "input_cost_per_million_micros",
                Some(self.input_cost_per_million_micros),
            ),
            (
                "cached_input_cost_per_million_micros",
                self.cached_input_cost_per_million_micros,
            ),
            (
                "cache_write_cost_per_million_micros",
                self.cache_write_cost_per_million_micros,
            ),
            (
                "output_cost_per_million_micros",
                Some(self.output_cost_per_million_micros),
            ),
            (
                "reasoning_cost_per_million_micros",
                self.reasoning_cost_per_million_micros,
            ),
        ] {
            if let Some(v) = val
                && v < 0
            {
                return Err(AccountingError::Validation(format!(
                    "FrozenPricingRate.{field} must be >= 0"
                )));
            }
        }
        Ok(())
    }
}

/// Inclusive token totals and verified subsets used for frozen-rate estimation.
///
/// `input_tokens_total` includes cached-read and cache-write input; `output_tokens_total`
/// includes reasoning output. `None` means the count is unknown.
pub trait HasUsageTokenCounts {
    fn input_tokens_total(&self) -> Option<i64>;
    fn cached_input_tokens(&self) -> Option<i64>;
    fn output_tokens_total(&self) -> Option<i64>;
    fn reasoning_tokens_subset(&self) -> Option<i64>;
    /// Cache-write input tokens included in `input_tokens_total`, if known.
    fn cache_write_input_tokens(&self) -> Option<i64> {
        None
    }
}

/// Estimate cost in integer micros (`ROUND_HALF_UP` to nearest integer micro, saturating at
/// `i64::MAX`) without double-charging subsets or guessing.
///
/// Returns `None` when a total is unknown or negative, when a priced subset is unknown or
/// negative, or when subsets exceed their inclusive total.
#[must_use]
pub fn estimate_frozen_cost_micros(
    usage: &impl HasUsageTokenCounts,
    pricing_rate: &FrozenPricingRate,
) -> Option<i64> {
    estimate_inclusive(
        &TokenRates {
            input: pricing_rate.input_cost_per_million_micros,
            cached_input: pricing_rate.cached_input_cost_per_million_micros,
            cache_write: pricing_rate.cache_write_cost_per_million_micros,
            output: pricing_rate.output_cost_per_million_micros,
            reasoning: pricing_rate.reasoning_cost_per_million_micros,
        },
        &InclusiveCounts {
            input_total: usage.input_tokens_total(),
            cached_input: usage.cached_input_tokens(),
            cache_write_input: usage.cache_write_input_tokens(),
            output_total: usage.output_tokens_total(),
            reasoning: usage.reasoning_tokens_subset(),
        },
    )
}

/// Per-million-token rates in integer micros. A `None` component rate charges that subset at the
/// inclusive input (cached, cache-write) or output (reasoning) rate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenRates {
    pub input: i64,
    pub cached_input: Option<i64>,
    pub cache_write: Option<i64>,
    pub output: i64,
    pub reasoning: Option<i64>,
}

/// Disjoint token counts as a run accumulates them: uncached input, cache-read input and
/// cache-write input are separate; `reasoning` is a subset of `output` (clamped to it).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TokenCounts {
    pub uncached_input: u64,
    pub cache_read: u64,
    pub cache_write: u64,
    pub output: u64,
    pub reasoning: u64,
}

/// Cost in integer micros of `counts` at `rates`, with the rounding and saturation of
/// [`estimate_frozen_cost_micros`]. Counts above `i64::MAX` (and an input total whose sum
/// overflows) saturate at `i64::MAX`; a declined estimate (for example a negative rate) is `0`.
#[must_use]
pub fn estimate_cost_micros(rates: &TokenRates, counts: &TokenCounts) -> u64 {
    let to_i64 = |n: u64| i64::try_from(n).unwrap_or(i64::MAX);
    let inclusive = InclusiveCounts {
        input_total: Some(to_i64(
            counts
                .uncached_input
                .saturating_add(counts.cache_read)
                .saturating_add(counts.cache_write),
        )),
        cached_input: Some(to_i64(counts.cache_read)),
        cache_write_input: Some(to_i64(counts.cache_write)),
        output_total: Some(to_i64(counts.output)),
        reasoning: Some(to_i64(counts.reasoning.min(counts.output))),
    };
    match estimate_inclusive(rates, &inclusive) {
        Some(micros) if micros >= 0 => micros as u64,
        _ => 0,
    }
}

/// Inclusive totals and subsets; `None` means unknown.
struct InclusiveCounts {
    input_total: Option<i64>,
    cached_input: Option<i64>,
    cache_write_input: Option<i64>,
    output_total: Option<i64>,
    reasoning: Option<i64>,
}

fn estimate_inclusive(rates: &TokenRates, usage: &InclusiveCounts) -> Option<i64> {
    let total_in = usage.input_total?;
    let total_out = usage.output_total?;
    if total_in < 0 || total_out < 0 || rates.input < 0 || rates.output < 0 {
        return None;
    }

    let mut remaining_input = total_in;
    let mut cached_tokens: i64 = 0;
    if let Some(cached_rate) = rates.cached_input {
        if cached_rate < 0 {
            return None;
        }
        cached_tokens = usage.cached_input?;
        if cached_tokens < 0 || cached_tokens > remaining_input {
            return None;
        }
        remaining_input -= cached_tokens;
    }

    let mut cache_write_tokens: i64 = 0;
    if let Some(cw_rate) = rates.cache_write {
        if cw_rate < 0 {
            return None;
        }
        cache_write_tokens = usage.cache_write_input?;
        if cache_write_tokens < 0 || cache_write_tokens > remaining_input {
            return None;
        }
        remaining_input -= cache_write_tokens;
    }

    if remaining_input < 0 {
        return None;
    }

    let mut remaining_output = total_out;
    let mut reasoning_tokens: i64 = 0;
    if let Some(reasoning_rate) = rates.reasoning {
        if reasoning_rate < 0 {
            return None;
        }
        reasoning_tokens = usage.reasoning?;
        if reasoning_tokens < 0 {
            return None;
        }
        remaining_output -= reasoning_tokens;
    }

    if remaining_output < 0 {
        return None;
    }

    let input_num = (remaining_input as u128)
        .saturating_mul(rates.input as u128)
        .saturating_add(
            (cached_tokens as u128).saturating_mul(rates.cached_input.unwrap_or(0) as u128),
        )
        .saturating_add(
            (cache_write_tokens as u128).saturating_mul(rates.cache_write.unwrap_or(0) as u128),
        );

    let output_num = (remaining_output as u128)
        .saturating_mul(rates.output as u128)
        .saturating_add(
            (reasoning_tokens as u128).saturating_mul(rates.reasoning.unwrap_or(0) as u128),
        );

    let total_num = input_num.saturating_add(output_num);
    let rounded = total_num.saturating_add(500_000) / 1_000_000;
    Some(rounded.min(i64::MAX as u128) as i64)
}
