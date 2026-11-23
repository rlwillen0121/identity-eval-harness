//! Frozen-rate cost estimation: subset charging, decline-on-unknown, rounding, and saturation.

use ib_accounting::{
    AccountingError, FrozenPricingRate, HasUsageTokenCounts, estimate_frozen_cost_micros,
};
use serde_json::json;

const PRICING_SHA: &str = "0000000000000000000000000000000000000000000000000000000000000000";

#[derive(Default)]
struct Usage {
    input_total: Option<i64>,
    cached: Option<i64>,
    output_total: Option<i64>,
    reasoning: Option<i64>,
    cache_write: Option<i64>,
}

impl HasUsageTokenCounts for Usage {
    fn input_tokens_total(&self) -> Option<i64> {
        self.input_total
    }
    fn cached_input_tokens(&self) -> Option<i64> {
        self.cached
    }
    fn output_tokens_total(&self) -> Option<i64> {
        self.output_total
    }
    fn reasoning_tokens_subset(&self) -> Option<i64> {
        self.reasoning
    }
    fn cache_write_input_tokens(&self) -> Option<i64> {
        self.cache_write
    }
}

/// Usage type relying on the default (unknown) cache-write count.
struct NoCacheWrite(Usage);

impl HasUsageTokenCounts for NoCacheWrite {
    fn input_tokens_total(&self) -> Option<i64> {
        self.0.input_total
    }
    fn cached_input_tokens(&self) -> Option<i64> {
        self.0.cached
    }
    fn output_tokens_total(&self) -> Option<i64> {
        self.0.output_total
    }
    fn reasoning_tokens_subset(&self) -> Option<i64> {
        self.0.reasoning
    }
}

fn rate(input: i64, output: i64) -> FrozenPricingRate {
    FrozenPricingRate {
        pricing_manifest_sha256: PRICING_SHA.to_owned(),
        currency_code: "USD".to_owned(),
        input_cost_per_million_micros: input,
        cached_input_cost_per_million_micros: None,
        cache_write_cost_per_million_micros: None,
        output_cost_per_million_micros: output,
        reasoning_cost_per_million_micros: None,
    }
}

#[test]
fn cached_and_cache_write_subsets_are_charged_once_at_their_own_rates() {
    let anth_rate = FrozenPricingRate {
        cached_input_cost_per_million_micros: Some(300_000),
        cache_write_cost_per_million_micros: Some(3_750_000),
        ..rate(3_000_000, 15_000_000)
    };
    // 1M uncached + 1M cache-read + 1M cache-write input (inclusive total 3M), 1M output.
    let usage = Usage {
        input_total: Some(3_000_000),
        cached: Some(1_000_000),
        cache_write: Some(1_000_000),
        output_total: Some(1_000_000),
        reasoning: None,
    };
    assert_eq!(
        estimate_frozen_cost_micros(&usage, &anth_rate),
        Some(22_050_000)
    );

    // A priced subset that is unknown declines instead of guessing.
    let no_cached = Usage {
        input_total: Some(1_000_000),
        output_total: Some(500_000),
        ..Usage::default()
    };
    assert_eq!(estimate_frozen_cost_micros(&no_cached, &anth_rate), None);
    let default_cache_write = NoCacheWrite(Usage {
        input_total: Some(2_000_000),
        cached: Some(1_000_000),
        output_total: Some(1_000_000),
        ..Usage::default()
    });
    assert_eq!(
        estimate_frozen_cost_micros(&default_cache_write, &anth_rate),
        None
    );
}

#[test]
fn reasoning_subset_is_not_double_charged_and_subsets_cannot_exceed_totals() {
    let r = FrozenPricingRate {
        cached_input_cost_per_million_micros: Some(400_000),
        reasoning_cost_per_million_micros: Some(40_000_000),
        ..rate(8_000_000, 20_000_000)
    };
    let usage = Usage {
        input_total: Some(1_000_000),
        cached: Some(500_000),
        output_total: Some(1_000_000),
        reasoning: Some(250_000),
        ..Usage::default()
    };
    // 0.5M*8 + 0.5M*0.4 + 0.75M*20 + 0.25M*40 = 4 + 0.2 + 15 + 10 = 29.2 USD
    assert_eq!(estimate_frozen_cost_micros(&usage, &r), Some(29_200_000));

    let over_cached = Usage {
        cached: Some(1_000_001),
        ..usage
    };
    assert_eq!(estimate_frozen_cost_micros(&over_cached, &r), None);
    let over_reasoning = Usage {
        input_total: Some(1_000_000),
        cached: Some(0),
        output_total: Some(10),
        reasoning: Some(11),
        ..Usage::default()
    };
    assert_eq!(estimate_frozen_cost_micros(&over_reasoning, &r), None);
    let negative = Usage {
        input_total: Some(-1),
        output_total: Some(0),
        ..Usage::default()
    };
    assert_eq!(estimate_frozen_cost_micros(&negative, &rate(1, 1)), None);
    let unknown_total = Usage {
        output_total: Some(0),
        ..Usage::default()
    };
    assert_eq!(
        estimate_frozen_cost_micros(&unknown_total, &rate(1, 1)),
        None
    );
}

#[test]
fn round_half_up_and_saturating_micros_overflow_guards() {
    let one_input = Usage {
        input_total: Some(1),
        output_total: Some(0),
        ..Usage::default()
    };
    assert_eq!(
        estimate_frozen_cost_micros(&one_input, &rate(500_000, 500_000)),
        Some(1)
    );
    assert_eq!(
        estimate_frozen_cost_micros(&one_input, &rate(499_999, 0)),
        Some(0)
    );

    let huge = Usage {
        input_total: Some(i64::MAX),
        output_total: Some(i64::MAX),
        ..Usage::default()
    };
    assert_eq!(
        estimate_frozen_cost_micros(&huge, &rate(i64::MAX, i64::MAX)),
        Some(i64::MAX)
    );
}

#[test]
fn disjoint_input_subsets_exceeding_total_decline_before_subtraction() {
    let usage = Usage {
        input_total: Some(0),
        output_total: Some(0),
        cached: Some(i64::MAX),
        cache_write: Some(i64::MAX),
        ..Usage::default()
    };
    let pricing = FrozenPricingRate {
        cached_input_cost_per_million_micros: Some(1),
        cache_write_cost_per_million_micros: Some(1),
        ..rate(1, 1)
    };

    assert_eq!(estimate_frozen_cost_micros(&usage, &pricing), None);
}

#[test]
fn validation_and_serde_shape() {
    assert!(rate(0, 0).validate().is_ok());
    let negative = FrozenPricingRate {
        reasoning_cost_per_million_micros: Some(-1),
        ..rate(0, 0)
    };
    let Err(AccountingError::Validation(message)) = negative.validate() else {
        panic!("negative rate must fail validation");
    };
    assert!(message.contains("reasoning_cost_per_million_micros"));
    let empty_currency = FrozenPricingRate {
        currency_code: String::new(),
        ..rate(0, 0)
    };
    assert!(empty_currency.validate().is_err());

    let parsed: FrozenPricingRate = serde_json::from_value(json!({
        "pricing_manifest_sha256": PRICING_SHA,
        "currency_code": "USD",
        "input_cost_per_million_micros": 1,
        "output_cost_per_million_micros": 2,
    }))
    .unwrap();
    assert_eq!(parsed, rate(1, 2));
    assert!(
        serde_json::from_value::<FrozenPricingRate>(json!({
            "pricing_manifest_sha256": PRICING_SHA,
            "currency_code": "USD",
            "input_cost_per_million_micros": 1,
            "output_cost_per_million_micros": 2,
            "unexpected": true,
        }))
        .is_err()
    );
}
