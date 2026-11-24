//! `estimate_cost_micros` equals the frozen-rate path the v5.2 runtime used before it existed: a
//! `FrozenPricingRate` with every component rate set (cache-write optional) and a snapshot that
//! reports inclusive totals built from disjoint `u64` counts, clamped to `i64::MAX`, with
//! reasoning clamped to output and a declined estimate mapped to `0`.

use ib_accounting::{
    FrozenPricingRate, HasUsageTokenCounts, TokenCounts, TokenRates, estimate_cost_micros,
    estimate_frozen_cost_micros,
};

struct Snapshot {
    total_in: i64,
    cached_in: i64,
    cache_write_in: i64,
    total_out: i64,
    reasoning: i64,
}

impl HasUsageTokenCounts for Snapshot {
    fn input_tokens_total(&self) -> Option<i64> {
        Some(self.total_in)
    }
    fn cached_input_tokens(&self) -> Option<i64> {
        Some(self.cached_in)
    }
    fn output_tokens_total(&self) -> Option<i64> {
        Some(self.total_out)
    }
    fn reasoning_tokens_subset(&self) -> Option<i64> {
        Some(self.reasoning)
    }
    fn cache_write_input_tokens(&self) -> Option<i64> {
        Some(self.cache_write_in)
    }
}

/// The pre-refactor `PricingRatesV52::compute_cost_usd_micros`, verbatim apart from its inputs.
fn previous_adapter(rates: [i64; 4], cache_write_rate: Option<i64>, counts: [u64; 5]) -> u64 {
    let [uncached_rate, cached_rate, output_rate, reasoning_rate] = rates;
    let [
        input_tokens,
        cache_read_tokens,
        cache_write_tokens,
        output_tokens,
        reasoning_tokens,
    ] = counts;
    let to_i64 = |n: u64| i64::try_from(n).unwrap_or(i64::MAX);
    let snapshot = Snapshot {
        total_in: to_i64(
            input_tokens
                .saturating_add(cache_read_tokens)
                .saturating_add(cache_write_tokens),
        ),
        cached_in: to_i64(cache_read_tokens),
        cache_write_in: to_i64(cache_write_tokens),
        total_out: to_i64(output_tokens),
        reasoning: to_i64(reasoning_tokens.min(output_tokens)),
    };
    let frozen = FrozenPricingRate {
        pricing_manifest_sha256: "0".repeat(64),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: uncached_rate,
        cached_input_cost_per_million_micros: Some(cached_rate),
        cache_write_cost_per_million_micros: cache_write_rate,
        output_cost_per_million_micros: output_rate,
        reasoning_cost_per_million_micros: Some(reasoning_rate),
    };
    match estimate_frozen_cost_micros(&snapshot, &frozen) {
        Some(micros) if micros >= 0 => micros as u64,
        _ => 0,
    }
}

#[test]
fn plain_estimator_matches_the_previous_runtime_adapter() {
    let token_values: [u64; 9] = [
        0,
        1,
        499_999,
        500_000,
        1_000_000,
        123_456_789,
        i64::MAX as u64 - 1,
        i64::MAX as u64,
        u64::MAX,
    ];
    let rate_values: [i64; 6] = [0, 1, 100_000, 4_000_000, i64::MAX, -1];
    let mut checked = 0_u64;
    for &uncached in &rate_values {
        for &cached in &rate_values {
            for &output in &rate_values {
                for cache_write in [None, Some(0), Some(1_250_000), Some(i64::MAX), Some(-1)] {
                    let reasoning_rate = output.max(0);
                    let rates = TokenRates {
                        input: uncached,
                        cached_input: Some(cached),
                        cache_write,
                        output,
                        reasoning: Some(reasoning_rate),
                    };
                    for (i, &a) in token_values.iter().enumerate() {
                        let b = token_values[(i + 3) % token_values.len()];
                        let c = token_values[(i + 5) % token_values.len()];
                        let d = token_values[(i * 7 + 1) % token_values.len()];
                        for &e in &token_values {
                            let counts = [a, b, c, d, e];
                            let expected = previous_adapter(
                                [uncached, cached, output, reasoning_rate],
                                cache_write,
                                counts,
                            );
                            let got = estimate_cost_micros(
                                &rates,
                                &TokenCounts {
                                    uncached_input: a,
                                    cache_read: b,
                                    cache_write: c,
                                    output: d,
                                    reasoning: e,
                                },
                            );
                            assert_eq!(got, expected, "rates {rates:?}, counts {counts:?}");
                            checked += 1;
                        }
                    }
                }
            }
        }
    }
    assert!(checked > 50_000);
}

#[test]
fn plain_estimator_rounds_half_up_and_prices_cache_writes_separately() {
    let rates = TokenRates {
        input: 1_000_000,
        cached_input: Some(100_000),
        cache_write: Some(1_250_000),
        output: 4_000_000,
        reasoning: Some(4_000_000),
    };
    let one_m = TokenCounts {
        uncached_input: 1_000_000,
        cache_read: 1_000_000,
        cache_write: 1_000_000,
        output: 1_000_000,
        reasoning: 0,
    };
    assert_eq!(
        estimate_cost_micros(&rates, &one_m),
        1_000_000 + 100_000 + 1_250_000 + 4_000_000
    );
    // 1 token at 1_000_000 micros per million is exactly 1 micro; 1 token at 499_999 rounds down,
    // at 500_000 rounds half up.
    let one_token = TokenCounts {
        uncached_input: 1,
        ..TokenCounts::default()
    };
    for (rate, micros) in [(1_000_000, 1), (499_999, 0), (500_000, 1)] {
        let r = TokenRates {
            input: rate,
            ..rates
        };
        assert_eq!(estimate_cost_micros(&r, &one_token), micros);
    }
}
