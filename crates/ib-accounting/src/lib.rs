//! Version-neutral provider cost accounting for IdentityBench.
//!
//! Frozen per-million-token pricing rates and an integer-micros cost estimator that charges
//! cached-input, cache-write, and reasoning subsets at their own rates without double-charging
//! them against the inclusive input/output totals, rounding `ROUND_HALF_UP` to the nearest micro
//! and saturating at `i64::MAX`.

pub mod error;
pub mod pricing;

pub use error::AccountingError;
pub use pricing::{
    FrozenPricingRate, HasUsageTokenCounts, TokenCounts, TokenRates, estimate_cost_micros,
    estimate_frozen_cost_micros,
};
