//! Timing intervals, cumulative segment derivation, scoped usage aggregation, currency conversion, and G-ECON-01 evaluation.

use std::collections::{BTreeMap, BTreeSet};

use ib_core::pyjson::{PyNumber, classify_number, jcs_string};
use ib_v5_schemas::{
    AccountingCoverageStatusV5, AttemptStatusV5, CANONICAL_ATTEMPT_STATUSES_V5,
    ContextLimitCauseV5, CostBasisV5, DescriptiveUsageSummaryV5, EpisodeAttemptV5,
    ProviderRequestKindV5, ProviderRequestUsageV5, UsageScopeV5,
};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AccountingError;
use crate::policy::CurrencyConversionRateV5;

/// Incomplete attempt statuses admissible for filling unmaterialized planned slots (SC-03).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum IncompleteAttemptStatusV5 {
    IncompleteProviderQuota,
    IncompleteProviderFailure,
    IncompleteContextIncompatible,
    IncompleteHarnessFailure,
}

impl IncompleteAttemptStatusV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::IncompleteProviderQuota => "INCOMPLETE_PROVIDER_QUOTA",
            Self::IncompleteProviderFailure => "INCOMPLETE_PROVIDER_FAILURE",
            Self::IncompleteContextIncompatible => "INCOMPLETE_CONTEXT_INCOMPATIBLE",
            Self::IncompleteHarnessFailure => "INCOMPLETE_HARNESS_FAILURE",
        }
    }
}

impl From<IncompleteAttemptStatusV5> for AttemptStatusV5 {
    fn from(value: IncompleteAttemptStatusV5) -> Self {
        match value {
            IncompleteAttemptStatusV5::IncompleteProviderQuota => Self::IncompleteProviderQuota,
            IncompleteAttemptStatusV5::IncompleteProviderFailure => Self::IncompleteProviderFailure,
            IncompleteAttemptStatusV5::IncompleteContextIncompatible => {
                Self::IncompleteContextIncompatible
            }
            IncompleteAttemptStatusV5::IncompleteHarnessFailure => Self::IncompleteHarnessFailure,
        }
    }
}

/// Compute the exact union length in ms across `(start_ms, end_ms)` intervals.
///
/// Overlapping concurrent intervals are merged before summing so concurrency is never
/// double-counted; quota pauses, harness queueing, and idle operator intervals are excluded
/// by omitting them from `intervals_ms`. Sums saturate at `i64::MAX`.
pub fn compute_active_elapsed_ms(intervals_ms: &[(i64, i64)]) -> Result<i64, AccountingError> {
    if intervals_ms.is_empty() {
        return Ok(0);
    }
    let mut normalized = Vec::with_capacity(intervals_ms.len());
    for &(start_ms, end_ms) in intervals_ms {
        if start_ms < 0 || end_ms < start_ms {
            return Err(AccountingError::Validation(format!(
                "invalid active interval [{start_ms}, {end_ms}]"
            )));
        }
        normalized.push((start_ms, end_ms));
    }

    normalized.sort_unstable();
    let mut total_ms: i64 = 0;
    let (mut cur_start, mut cur_end) = normalized[0];
    for &(start_ms, end_ms) in &normalized[1..] {
        if start_ms <= cur_end {
            if end_ms > cur_end {
                cur_end = end_ms;
            }
        } else {
            total_ms = total_ms.saturating_add(cur_end - cur_start);
            cur_start = start_ms;
            cur_end = end_ms;
        }
    }
    total_ms = total_ms.saturating_add(cur_end - cur_start);
    Ok(total_ms)
}

/// Result of [`compute_attempt_timings_ms_v5`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct AttemptTimingsMsV5 {
    pub elapsed_wall_time_ms: Option<i64>,
    pub terminal_outcome_elapsed_ms: Option<i64>,
    pub active_elapsed_ms: i64,
}

/// Compute `(elapsed_wall_time_ms, terminal_outcome_elapsed_ms, active_elapsed_ms)`.
pub fn compute_attempt_timings_ms_v5(
    first_dispatch_ms: i64,
    terminal_outcome_ms: Option<i64>,
    final_persisted_ms: Option<i64>,
    active_intervals_ms: &[(i64, i64)],
) -> Result<AttemptTimingsMsV5, AccountingError> {
    if first_dispatch_ms < 0 {
        return Err(AccountingError::Validation(
            "first_dispatch_ms must be a non-negative integer".to_owned(),
        ));
    }
    let active_ms = compute_active_elapsed_ms(active_intervals_ms)?;
    let terminal_elapsed = match terminal_outcome_ms {
        None => None,
        Some(t) => {
            if t < first_dispatch_ms {
                return Err(AccountingError::Validation(
                    "terminal_outcome_ms must be >= first_dispatch_ms".to_owned(),
                ));
            }
            Some(t - first_dispatch_ms)
        }
    };
    let wall_elapsed = match final_persisted_ms {
        None => None,
        Some(f) => {
            if f < first_dispatch_ms {
                return Err(AccountingError::Validation(
                    "final_persisted_ms must be >= first_dispatch_ms".to_owned(),
                ));
            }
            if let Some(t) = terminal_outcome_ms
                && f < t
            {
                return Err(AccountingError::Validation(
                    "final_persisted_ms must be >= terminal_outcome_ms".to_owned(),
                ));
            }
            let wall = f - first_dispatch_ms;
            if active_ms > wall {
                return Err(AccountingError::Validation(
                    "active_elapsed_ms cannot exceed elapsed_wall_time_ms".to_owned(),
                ));
            }
            Some(wall)
        }
    };

    Ok(AttemptTimingsMsV5 {
        elapsed_wall_time_ms: wall_elapsed,
        terminal_outcome_elapsed_ms: terminal_elapsed,
        active_elapsed_ms: active_ms,
    })
}

fn delta_optional(
    cur: Option<i64>,
    prev: Option<i64>,
    label: &str,
) -> Result<Option<i64>, AccountingError> {
    match (cur, prev) {
        (Some(c), Some(p)) => {
            let delta = c - p;
            if delta < 0 {
                return Err(AccountingError::Validation(format!(
                    "cumulative {label} decreased from {p} to {c}"
                )));
            }
            Ok(Some(delta))
        }
        _ => Ok(None),
    }
}

/// Derive per-segment `PHYSICAL_REQUEST` deltas from a `LOGICAL_RESPONSE_CUMULATIVE` series.
pub fn derive_physical_segment_usage_from_cumulative_v5(
    cumulative_segments: &[ProviderRequestUsageV5],
) -> Result<Vec<ProviderRequestUsageV5>, AccountingError> {
    if cumulative_segments.is_empty() {
        return Ok(Vec::new());
    }
    let mut ordered: Vec<&ProviderRequestUsageV5> = cumulative_segments.iter().collect();
    ordered.sort_by_key(|item| item.decode_segment_index);

    let first_logical_id = &ordered[0].logical_request_id;
    if ordered
        .iter()
        .any(|item| &item.logical_request_id != first_logical_id)
    {
        return Err(AccountingError::Validation(
            "cumulative derivation requires a single logical_request_id".to_owned(),
        ));
    }

    let mut seen_indices = BTreeSet::new();
    let mut derived = Vec::with_capacity(ordered.len());
    let mut prev: Option<&ProviderRequestUsageV5> = None;

    for row in ordered {
        if row.usage_scope != UsageScopeV5::LogicalResponseCumulative {
            return Err(AccountingError::Validation(
                "expected LOGICAL_RESPONSE_CUMULATIVE rows for cumulative derivation".to_owned(),
            ));
        }
        if !seen_indices.insert(row.decode_segment_index) {
            return Err(AccountingError::Validation(format!(
                "duplicate decode_segment_index {}",
                row.decode_segment_index
            )));
        }
        if let Some(p) = prev {
            if row.currency_code != p.currency_code || row.cost_basis != p.cost_basis {
                return Err(AccountingError::Validation(
                    "cumulative segment cost_basis/currency_code must match".to_owned(),
                ));
            }
            let mut next = row.clone();
            next.usage_scope = UsageScopeV5::PhysicalRequest;
            next.input_tokens_total = delta_optional(
                row.input_tokens_total,
                p.input_tokens_total,
                "input_tokens_total",
            )?;
            next.cached_input_tokens = delta_optional(
                row.cached_input_tokens,
                p.cached_input_tokens,
                "cached_input_tokens",
            )?;
            next.output_tokens_total = delta_optional(
                row.output_tokens_total,
                p.output_tokens_total,
                "output_tokens_total",
            )?;
            next.reasoning_tokens_subset = delta_optional(
                row.reasoning_tokens_subset,
                p.reasoning_tokens_subset,
                "reasoning_tokens_subset",
            )?;
            next.tool_serialization_tokens_subset = delta_optional(
                row.tool_serialization_tokens_subset,
                p.tool_serialization_tokens_subset,
                "tool_serialization_tokens_subset",
            )?;
            next.provider_reported_cost_micros = delta_optional(
                row.provider_reported_cost_micros,
                p.provider_reported_cost_micros,
                "provider_reported_cost_micros",
            )?;
            next.estimated_cost_micros = delta_optional(
                row.estimated_cost_micros,
                p.estimated_cost_micros,
                "estimated_cost_micros",
            )?;
            derived.push(next);
        } else {
            let mut next = row.clone();
            next.usage_scope = UsageScopeV5::PhysicalRequest;
            derived.push(next);
        }
        prev = Some(row);
    }

    Ok(derived)
}

struct ScopedUsageSelection<'a> {
    contributing: Vec<&'a ProviderRequestUsageV5>,
    #[allow(dead_code)]
    skipped_redundant_cumulative: Vec<&'a ProviderRequestUsageV5>,
    ambiguous_cumulative_logical_ids: Vec<String>,
}

fn partition_scoped_usage(requests: &[ProviderRequestUsageV5]) -> ScopedUsageSelection<'_> {
    let mut by_logical: IndexMap<&str, Vec<&ProviderRequestUsageV5>> = IndexMap::new();
    for req in requests {
        by_logical
            .entry(req.logical_request_id.as_str())
            .or_default()
            .push(req);
    }

    let mut contributing = Vec::new();
    let mut skipped_redundant = Vec::new();
    let mut ambiguous_logical_ids = Vec::new();

    for (logical_id, group) in by_logical {
        let mut physical_or_unavail = Vec::new();
        let mut cumulative = Vec::new();
        for req in group {
            match req.usage_scope {
                UsageScopeV5::PhysicalRequest | UsageScopeV5::Unavailable => {
                    physical_or_unavail.push(req);
                }
                UsageScopeV5::LogicalResponseCumulative => {
                    cumulative.push(req);
                }
            }
        }

        if !physical_or_unavail.is_empty() {
            contributing.extend(physical_or_unavail);
            skipped_redundant.extend(cumulative);
        } else if cumulative.len() == 1 {
            contributing.push(cumulative[0]);
        } else if cumulative.len() > 1 {
            ambiguous_logical_ids.push(logical_id.to_owned());
            skipped_redundant.extend(cumulative);
        }
    }

    ambiguous_logical_ids.sort();
    ScopedUsageSelection {
        contributing,
        skipped_redundant_cumulative: skipped_redundant,
        ambiguous_cumulative_logical_ids: ambiguous_logical_ids,
    }
}

/// Return non-redundant usage rows for aggregation without cumulative double counting.
#[must_use]
pub fn select_contributing_usage_records_v5(
    requests: &[ProviderRequestUsageV5],
) -> Vec<ProviderRequestUsageV5> {
    partition_scoped_usage(requests)
        .contributing
        .into_iter()
        .cloned()
        .collect()
}

/// Convert integer micros using an explicit frozen conversion rate with `ROUND_HALF_UP`
/// exact rational arithmetic, saturating at `i64::MAX`.
pub fn convert_micros_round_half_up(
    amount_micros: i64,
    from_currency: &str,
    target_currency: &str,
    conversion_index: &BTreeMap<(String, String), &CurrencyConversionRateV5>,
) -> Result<i64, AccountingError> {
    if amount_micros < 0 {
        return Err(AccountingError::Validation(
            "amount_micros must be non-negative".to_owned(),
        ));
    }
    if from_currency == target_currency {
        return Ok(amount_micros);
    }
    let Some(rate) = conversion_index.get(&(from_currency.to_owned(), target_currency.to_owned()))
    else {
        return Err(AccountingError::Validation(format!(
            "cannot sum costs across mismatched currencies ({from_currency:?} vs {target_currency:?}) without an explicit frozen conversion rate and source"
        )));
    };
    if rate.rate_numerator < 1 || rate.rate_denominator < 1 {
        return Err(AccountingError::Validation(
            "currency conversion rate numerator and denominator must be >= 1".to_owned(),
        ));
    }
    let num = rate.rate_numerator as u128;
    let den = rate.rate_denominator as u128;
    let converted = (amount_micros as u128)
        .saturating_mul(num)
        .saturating_add(den / 2)
        / den;
    Ok(converted.min(i64::MAX as u128) as i64)
}

fn build_conversion_index(
    conversions: &[CurrencyConversionRateV5],
) -> Result<BTreeMap<(String, String), &CurrencyConversionRateV5>, AccountingError> {
    let mut index = BTreeMap::new();
    for rate in conversions {
        if rate.rate_numerator < 1 || rate.rate_denominator < 1 {
            return Err(AccountingError::Validation(
                "currency conversion rate numerator and denominator must be >= 1".to_owned(),
            ));
        }
        let key = (rate.from_currency.clone(), rate.to_currency.clone());
        if index.contains_key(&key) {
            return Err(AccountingError::Validation(format!(
                "duplicate currency conversion rate for {key:?}"
            )));
        }
        index.insert(key, rate);
    }
    Ok(index)
}

fn sum_optional_i64_saturating(values: impl Iterator<Item = Option<i64>>) -> Option<i64> {
    let mut any = false;
    let mut acc: i64 = 0;
    for v in values.flatten() {
        any = true;
        acc = acc.saturating_add(v.max(0));
    }
    any.then_some(acc)
}

fn sum_i64_slice_saturating(values: &[i64]) -> Option<i64> {
    if values.is_empty() {
        return None;
    }
    let mut acc: i64 = 0;
    for &v in values {
        acc = acc.saturating_add(v.max(0));
    }
    Some(acc)
}

struct AggregatedSummaryValues {
    coverage_status: AccountingCoverageStatusV5,
    total_input_tokens: Option<i64>,
    cached_input_tokens: Option<i64>,
    total_output_tokens: Option<i64>,
    reasoning_tokens_subset: Option<i64>,
    tool_serialization_tokens_subset: Option<i64>,
    provider_billed_cost_micros: Option<i64>,
    estimated_cost_micros: Option<i64>,
    currency_code: Option<String>,
    elapsed_wall_time_ms: Option<i64>,
    active_elapsed_ms: Option<i64>,
    ambiguous_cumulative_logical_ids: Vec<String>,
}

fn has_full_token_and_cost(req: &ProviderRequestUsageV5) -> bool {
    let token_ok = req.usage_scope == UsageScopeV5::PhysicalRequest
        && req.input_tokens_total.is_some()
        && req.output_tokens_total.is_some();
    let cost_ok = (req.cost_basis == CostBasisV5::ProviderBilled
        && req.provider_reported_cost_micros.is_some())
        || (req.cost_basis == CostBasisV5::FrozenRateEstimate
            && req.estimated_cost_micros.is_some());
    token_ok && cost_ok
}

fn has_any_known_metric(req: &ProviderRequestUsageV5) -> bool {
    req.input_tokens_total.is_some()
        || req.output_tokens_total.is_some()
        || req.provider_reported_cost_micros.is_some()
        || req.estimated_cost_micros.is_some()
}

fn aggregate_attempts_usage(
    attempts: &[EpisodeAttemptV5],
    has_missing_slots: bool,
    target_currency: Option<&str>,
    currency_conversions: &[CurrencyConversionRateV5],
) -> Result<AggregatedSummaryValues, AccountingError> {
    let all_requests: Vec<ProviderRequestUsageV5> = attempts
        .iter()
        .flat_map(|a| a.provider_requests.iter().cloned())
        .collect();
    let scoped = partition_scoped_usage(&all_requests);
    let contributing = &scoped.contributing;

    let conversion_index = build_conversion_index(currency_conversions)?;
    let cost_rows: Vec<&ProviderRequestUsageV5> = contributing
        .iter()
        .copied()
        .filter(|req| {
            (req.cost_basis == CostBasisV5::ProviderBilled
                && req.provider_reported_cost_micros.is_some())
                || (req.cost_basis == CostBasisV5::FrozenRateEstimate
                    && req.estimated_cost_micros.is_some())
        })
        .collect();

    let mut currencies = BTreeSet::new();
    for req in &cost_rows {
        let Some(curr) = req.currency_code.as_deref() else {
            return Err(AccountingError::Validation(
                "cost-bearing usage records require a non-empty currency_code".to_owned(),
            ));
        };
        if curr.is_empty() {
            return Err(AccountingError::Validation(
                "cost-bearing usage records require a non-empty currency_code".to_owned(),
            ));
        }
        currencies.insert(curr);
    }

    let resolved_currency: Option<String> = match target_currency {
        Some(t) => Some(t.to_owned()),
        None => {
            if currencies.len() > 1 {
                let currency_list: Vec<&str> = currencies.into_iter().collect();
                return Err(AccountingError::Validation(format!(
                    "cannot sum costs across mismatched currencies {currency_list:?} without an explicit frozen conversion rate and target_currency"
                )));
            }
            currencies.into_iter().next().map(str::to_owned)
        }
    };

    let mut billed_values = Vec::new();
    let mut estimated_values = Vec::new();
    for req in &cost_rows {
        let target = resolved_currency
            .as_deref()
            .expect("resolved_currency must be Some when cost_rows is non-empty");
        let from_curr = req
            .currency_code
            .as_deref()
            .expect("cost-bearing row currency_code validated non-empty");
        if req.cost_basis == CostBasisV5::ProviderBilled
            && let Some(billed) = req.provider_reported_cost_micros
        {
            billed_values.push(convert_micros_round_half_up(
                billed,
                from_curr,
                target,
                &conversion_index,
            )?);
        }
        if req.cost_basis == CostBasisV5::FrozenRateEstimate
            && let Some(est) = req.estimated_cost_micros
        {
            estimated_values.push(convert_micros_round_half_up(
                est,
                from_curr,
                target,
                &conversion_index,
            )?);
        }
    }

    let any_known = contributing.iter().any(|r| has_any_known_metric(r));
    let all_complete = !contributing.is_empty()
        && !has_missing_slots
        && scoped.ambiguous_cumulative_logical_ids.is_empty()
        && contributing.iter().all(|r| has_full_token_and_cost(r));

    let coverage = if !any_known {
        AccountingCoverageStatusV5::Unavailable
    } else if all_complete {
        AccountingCoverageStatusV5::Complete
    } else {
        AccountingCoverageStatusV5::Partial
    };

    let wall_ms = sum_optional_i64_saturating(attempts.iter().map(|a| a.elapsed_wall_time_ms));
    let active_ms = sum_optional_i64_saturating(attempts.iter().map(|a| a.active_elapsed_ms));

    if coverage == AccountingCoverageStatusV5::Unavailable {
        return Ok(AggregatedSummaryValues {
            coverage_status: AccountingCoverageStatusV5::Unavailable,
            total_input_tokens: None,
            cached_input_tokens: None,
            total_output_tokens: None,
            reasoning_tokens_subset: None,
            tool_serialization_tokens_subset: None,
            provider_billed_cost_micros: None,
            estimated_cost_micros: None,
            currency_code: resolved_currency,
            elapsed_wall_time_ms: wall_ms,
            active_elapsed_ms: active_ms,
            ambiguous_cumulative_logical_ids: scoped.ambiguous_cumulative_logical_ids,
        });
    }

    let valid_scope_rows: Vec<&ProviderRequestUsageV5> = contributing
        .iter()
        .copied()
        .filter(|r| r.usage_scope != UsageScopeV5::Unavailable)
        .collect();

    Ok(AggregatedSummaryValues {
        coverage_status: coverage,
        total_input_tokens: sum_optional_i64_saturating(
            valid_scope_rows.iter().map(|r| r.input_tokens_total),
        ),
        cached_input_tokens: sum_optional_i64_saturating(
            valid_scope_rows.iter().map(|r| r.cached_input_tokens),
        ),
        total_output_tokens: sum_optional_i64_saturating(
            valid_scope_rows.iter().map(|r| r.output_tokens_total),
        ),
        reasoning_tokens_subset: sum_optional_i64_saturating(
            valid_scope_rows.iter().map(|r| r.reasoning_tokens_subset),
        ),
        tool_serialization_tokens_subset: sum_optional_i64_saturating(
            valid_scope_rows
                .iter()
                .map(|r| r.tool_serialization_tokens_subset),
        ),
        provider_billed_cost_micros: sum_i64_slice_saturating(&billed_values),
        estimated_cost_micros: sum_i64_slice_saturating(&estimated_values),
        currency_code: resolved_currency,
        elapsed_wall_time_ms: wall_ms,
        active_elapsed_ms: active_ms,
        ambiguous_cumulative_logical_ids: scoped.ambiguous_cumulative_logical_ids,
    })
}

fn compute_status_counts(
    planned_attempt_slots: i64,
    attempts: &[EpisodeAttemptV5],
    missing_slot_status: Option<IncompleteAttemptStatusV5>,
) -> Result<(BTreeMap<&'static str, i64>, bool), AccountingError> {
    if planned_attempt_slots < 0 {
        return Err(AccountingError::Validation(
            "planned_attempt_slots must be non-negative".to_owned(),
        ));
    }
    let observed = i64::try_from(attempts.len())
        .map_err(|_| AccountingError::Validation("attempts count exceeds i64 range".to_owned()))?;
    if observed > planned_attempt_slots {
        return Err(AccountingError::Validation(format!(
            "received {observed} attempts for only {planned_attempt_slots} planned slots"
        )));
    }
    let has_missing_slots = observed < planned_attempt_slots;
    if has_missing_slots && missing_slot_status.is_none() {
        return Err(AccountingError::Validation(format!(
            "expected {planned_attempt_slots} materialized attempts, got {observed}"
        )));
    }

    let mut counts: BTreeMap<&'static str, i64> = CANONICAL_ATTEMPT_STATUSES_V5
        .iter()
        .map(|&status| (status, 0))
        .collect();
    for attempt in attempts {
        let entry = counts
            .get_mut(attempt.attempt_status.as_str())
            .expect("AttemptStatusV5::as_str is always a canonical status");
        *entry += 1;
    }
    if has_missing_slots {
        let status = missing_slot_status.expect("checked Some above");
        let entry = counts
            .get_mut(status.as_str())
            .expect("IncompleteAttemptStatusV5::as_str is always a canonical status");
        *entry += planned_attempt_slots - observed;
    }

    Ok((counts, has_missing_slots))
}

/// Optional settings for [`summarize_descriptive_usage_v5`].
#[derive(Debug, Clone)]
pub struct SummarizeDescriptiveUsageOptions<'a> {
    pub missing_slot_status: Option<IncompleteAttemptStatusV5>,
    pub target_currency: Option<&'a str>,
    pub currency_conversions: &'a [CurrencyConversionRateV5],
}

impl Default for SummarizeDescriptiveUsageOptions<'_> {
    fn default() -> Self {
        Self {
            missing_slot_status: Some(IncompleteAttemptStatusV5::IncompleteHarnessFailure),
            target_currency: None,
            currency_conversions: &[],
        }
    }
}

/// Aggregate `EpisodeAttemptV5` records into a `DescriptiveUsageSummaryV5`.
pub fn summarize_descriptive_usage_v5(
    run_config_sha256: &str,
    planned_attempt_slots: i64,
    attempts: &[EpisodeAttemptV5],
    options: SummarizeDescriptiveUsageOptions<'_>,
) -> Result<DescriptiveUsageSummaryV5, AccountingError> {
    let mut seen_attempt_ids = BTreeSet::new();
    let mut seen_slots = BTreeSet::new();
    for attempt in attempts {
        if attempt.run_config_sha256 != run_config_sha256 {
            return Err(AccountingError::Validation(format!(
                "attempt {:?} run_config_sha256 does not match summary",
                attempt.attempt_id
            )));
        }
        if !seen_attempt_ids.insert(attempt.attempt_id.as_str()) {
            return Err(AccountingError::Validation(format!(
                "duplicate attempt_id: {:?}",
                attempt.attempt_id
            )));
        }
        let slot_key = (attempt.episode_id.as_str(), attempt.repeat_index);
        if !seen_slots.insert(slot_key) {
            return Err(AccountingError::Validation(format!(
                "duplicate (episode_id, repeat_index) slot: {slot_key:?}"
            )));
        }
    }

    let (status_counts, has_missing_slots) =
        compute_status_counts(planned_attempt_slots, attempts, options.missing_slot_status)?;
    let aggregated = aggregate_attempts_usage(
        attempts,
        has_missing_slots,
        options.target_currency,
        options.currency_conversions,
    )?;

    let mut counts_obj = Map::new();
    for (status, count) in &status_counts {
        counts_obj.insert((*status).to_owned(), Value::Number((*count).into()));
    }
    let attempt_status_counts_json = jcs_string(&Value::Object(counts_obj))?;

    Ok(DescriptiveUsageSummaryV5 {
        run_config_sha256: run_config_sha256.to_owned(),
        planned_attempt_slots,
        completed_attempts: status_counts["COMPLETED"],
        incomplete_provider_quota_attempts: status_counts["INCOMPLETE_PROVIDER_QUOTA"],
        incomplete_provider_failure_attempts: status_counts["INCOMPLETE_PROVIDER_FAILURE"],
        incomplete_context_attempts: status_counts["INCOMPLETE_CONTEXT_INCOMPATIBLE"],
        incomplete_harness_attempts: status_counts["INCOMPLETE_HARNESS_FAILURE"],
        attempt_status_counts_json,
        elapsed_wall_time_ms: aggregated.elapsed_wall_time_ms,
        active_elapsed_ms: aggregated.active_elapsed_ms,
        total_input_tokens: aggregated.total_input_tokens,
        cached_input_tokens: aggregated.cached_input_tokens,
        total_output_tokens: aggregated.total_output_tokens,
        reasoning_tokens_subset: aggregated.reasoning_tokens_subset,
        tool_serialization_tokens_subset: aggregated.tool_serialization_tokens_subset,
        provider_billed_cost_micros: aggregated.provider_billed_cost_micros,
        estimated_cost_micros: aggregated.estimated_cost_micros,
        currency_code: aggregated.currency_code,
        accounting_coverage_status: aggregated.coverage_status,
    })
}

fn check_request_row_invariants(req: &ProviderRequestUsageV5) -> Vec<String> {
    let mut errors = Vec::new();
    if req.usage_scope == UsageScopeV5::Unavailable
        && (req.input_tokens_total.is_some()
            || req.cached_input_tokens.is_some()
            || req.output_tokens_total.is_some()
            || req.reasoning_tokens_subset.is_some()
            || req.tool_serialization_tokens_subset.is_some())
    {
        errors.push(format!(
            "request {:?} has usage_scope=UNAVAILABLE with non-null tokens",
            req.request_id
        ));
    }
    if let Some(raw_json) = req.raw_usage_fields_json.as_deref() {
        match serde_json::from_str::<Value>(raw_json) {
            Err(_) => {
                errors.push(format!(
                    "request {:?} raw_usage_fields_json is invalid JSON",
                    req.request_id
                ));
            }
            Ok(val) => {
                if !val.is_object() {
                    errors.push(format!(
                        "request {:?} raw_usage_fields_json must decode to JSON object",
                        req.request_id
                    ));
                }
            }
        }
    }
    match req.cost_basis {
        CostBasisV5::ProviderBilled => {
            if req.provider_reported_cost_micros.is_none()
                || req.currency_code.as_deref().is_none_or(str::is_empty)
            {
                errors.push(format!(
                    "request {:?} PROVIDER_BILLED requires cost and currency_code",
                    req.request_id
                ));
            }
        }
        CostBasisV5::FrozenRateEstimate => {
            if req.estimated_cost_micros.is_none()
                || req
                    .pricing_manifest_sha256
                    .as_deref()
                    .is_none_or(str::is_empty)
                || req.currency_code.as_deref().is_none_or(str::is_empty)
            {
                errors.push(format!(
                    "request {:?} FROZEN_RATE_ESTIMATE requires cost, pricing_manifest_sha256, and currency_code",
                    req.request_id
                ));
            }
        }
        CostBasisV5::Unavailable => {
            if req.provider_reported_cost_micros.is_some() || req.estimated_cost_micros.is_some() {
                errors.push(format!(
                    "request {:?} cost_basis=UNAVAILABLE must have null cost amounts",
                    req.request_id
                ));
            }
        }
    }
    errors
}

fn check_attempt_completeness_errors(attempts: &[EpisodeAttemptV5]) -> Vec<String> {
    let mut errors = Vec::new();
    let mut seen_request_ids = BTreeSet::new();

    for attempt in attempts {
        let reqs = &attempt.provider_requests;
        let transient_retries = reqs
            .iter()
            .filter(|r| r.request_kind == ProviderRequestKindV5::TransientRetry)
            .count() as i64;
        if attempt.retry_request_count != transient_retries {
            errors.push(format!(
                "attempt {:?} retry_request_count={} does not match TRANSIENT_RETRY records ({transient_retries})",
                attempt.attempt_id, attempt.retry_request_count
            ));
        }
        if attempt.completed_assistant_generations > 0 && reqs.is_empty() {
            errors.push(format!(
                "attempt {:?} has {} completed generations but zero provider_requests",
                attempt.attempt_id, attempt.completed_assistant_generations
            ));
        }
        if (attempt.terminal_cause.as_deref() == Some("PROVIDER_REQUEST_POLICY_REJECTION")
            || attempt.context_limit_cause
                == Some(ContextLimitCauseV5::ProviderRejectionConfirmedLimit))
            && reqs.is_empty()
        {
            errors.push(format!(
                "attempt {:?} records provider rejection without provider_requests telemetry",
                attempt.attempt_id
            ));
        }
        if let (Some(wall), Some(active)) =
            (attempt.elapsed_wall_time_ms, attempt.active_elapsed_ms)
            && active > wall
        {
            errors.push(format!(
                "attempt {:?} active_elapsed_ms exceeds elapsed_wall_time_ms",
                attempt.attempt_id
            ));
        }
        if let (Some(wall), Some(term)) = (
            attempt.elapsed_wall_time_ms,
            attempt.terminal_outcome_elapsed_ms,
        ) && term > wall
        {
            errors.push(format!(
                "attempt {:?} terminal_outcome_elapsed_ms exceeds elapsed_wall_time_ms",
                attempt.attempt_id
            ));
        }

        let mut by_logical: IndexMap<&str, Vec<&ProviderRequestUsageV5>> = IndexMap::new();
        for req in reqs {
            if req.attempt_id != attempt.attempt_id {
                errors.push(format!(
                    "request {:?} attempt_id {:?} != {:?}",
                    req.request_id, req.attempt_id, attempt.attempt_id
                ));
            }
            if !seen_request_ids.insert(req.request_id.as_str()) {
                errors.push(format!(
                    "duplicate physical request_id: {:?}",
                    req.request_id
                ));
            }
            by_logical
                .entry(req.logical_request_id.as_str())
                .or_default()
                .push(req);
            errors.extend(check_request_row_invariants(req));
        }

        for (logical_id, group) in by_logical {
            let phys: Vec<&ProviderRequestUsageV5> = group
                .into_iter()
                .filter(|r| r.usage_scope == UsageScopeV5::PhysicalRequest)
                .collect();
            if phys
                .iter()
                .any(|r| r.request_kind == ProviderRequestKindV5::SegmentContinuation)
            {
                let segs: BTreeSet<i64> = phys.iter().map(|r| r.decode_segment_index).collect();
                let is_contiguous = segs
                    .iter()
                    .copied()
                    .enumerate()
                    .all(|(expected, actual)| actual == expected as i64);
                if !is_contiguous {
                    let segs_vec: Vec<i64> = segs.into_iter().collect();
                    errors.push(format!(
                        "logical request {logical_id:?} has non-contiguous decode_segment_index coverage: {segs_vec:?}"
                    ));
                }
            }
        }
    }

    errors
}

/// Descriptive metrics payload embedded in [`GEcon01EvaluationV5`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DescriptiveMetricsV5 {
    pub planned_attempt_slots: i64,
    pub completed_attempts: i64,
    pub total_input_tokens: Option<i64>,
    pub cached_input_tokens: Option<i64>,
    pub total_output_tokens: Option<i64>,
    pub reasoning_tokens_subset: Option<i64>,
    pub tool_serialization_tokens_subset: Option<i64>,
    pub provider_billed_cost_micros: Option<i64>,
    pub estimated_cost_micros: Option<i64>,
    pub currency_code: Option<String>,
    pub elapsed_wall_time_ms: Option<i64>,
    pub active_elapsed_ms: Option<i64>,
}

/// Result of evaluating `SPEC-v5.0-draft.5.md` §4.3 `G-ECON-01` completeness without spend caps.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GEcon01EvaluationV5 {
    pub gate_id: String,
    pub status: String,
    pub passed: bool,
    pub accounting_coverage_status: AccountingCoverageStatusV5,
    pub spend_or_token_ceiling_applied: bool,
    pub errors: Vec<String>,
    pub descriptive_metrics: DescriptiveMetricsV5,
}

/// Optional settings for [`evaluate_g_econ_01_v5`].
#[derive(Debug, Clone, Default)]
pub struct EvaluateGEcon01Options<'a> {
    pub target_currency: Option<&'a str>,
    pub currency_conversions: &'a [CurrencyConversionRateV5],
    pub require_complete_coverage: bool,
}

fn check_opt_i64_field(
    errors: &mut Vec<String>,
    field_name: &str,
    actual: Option<i64>,
    expected: Option<i64>,
) {
    if actual != expected {
        errors.push(format!(
            "summary {field_name}={actual:?} mismatches scoped recomputation {expected:?}"
        ));
    }
}

/// Evaluate `SPEC-v5.0-draft.5.md` §4.3 `G-ECON-01` completeness without spend caps.
#[must_use]
pub fn evaluate_g_econ_01_v5(
    attempts: &[EpisodeAttemptV5],
    summary: &DescriptiveUsageSummaryV5,
    options: EvaluateGEcon01Options<'_>,
) -> GEcon01EvaluationV5 {
    let mut errors = Vec::new();
    let observed = i64::try_from(attempts.len()).unwrap_or(i64::MAX);

    if observed != summary.planned_attempt_slots {
        errors.push(format!(
            "materialized attempts ({observed}) != planned_attempt_slots ({})",
            summary.planned_attempt_slots
        ));
    }
    for attempt in attempts {
        if attempt.run_config_sha256 != summary.run_config_sha256 {
            errors.push(format!(
                "attempt {:?} run_config_sha256 mismatches summary",
                attempt.attempt_id
            ));
        }
    }

    let seen_attempts: BTreeSet<&str> = attempts.iter().map(|a| a.attempt_id.as_str()).collect();
    if seen_attempts.len() != attempts.len() {
        errors.push("duplicate attempt_id detected in attempts".to_owned());
    }
    let seen_slots: BTreeSet<(&str, i64)> = attempts
        .iter()
        .map(|a| (a.episode_id.as_str(), a.repeat_index))
        .collect();
    if seen_slots.len() != attempts.len() {
        errors.push("duplicate (episode_id, repeat_index) slot detected in attempts".to_owned());
    }

    let mut actual_counts: BTreeMap<&'static str, i64> = CANONICAL_ATTEMPT_STATUSES_V5
        .iter()
        .map(|&s| (s, 0))
        .collect();
    for attempt in attempts {
        *actual_counts
            .get_mut(attempt.attempt_status.as_str())
            .expect("canonical status") += 1;
    }

    match serde_json::from_str::<Value>(&summary.attempt_status_counts_json) {
        Ok(Value::Object(parsed_obj)) => {
            let canonical_keys: BTreeSet<&str> =
                CANONICAL_ATTEMPT_STATUSES_V5.iter().copied().collect();
            let parsed_keys: BTreeSet<&str> = parsed_obj.keys().map(String::as_str).collect();
            if parsed_keys != canonical_keys {
                errors.push(
                    "attempt_status_counts_json must contain all 10 canonical attempt statuses"
                        .to_owned(),
                );
            }
            for &status in CANONICAL_ATTEMPT_STATUSES_V5.iter() {
                let actual = actual_counts[status];
                let parsed_val = parsed_obj.get(status).and_then(|v| match v {
                    Value::Number(n) => match classify_number(n) {
                        PyNumber::Int(_) => n.as_i64(),
                        PyNumber::Float(_) => None,
                    },
                    _ => None,
                });
                if parsed_val != Some(actual) {
                    errors.push(format!(
                        "attempt_status_counts_json[{status:?}]={:?} != actual count {actual}",
                        parsed_obj.get(status)
                    ));
                }
            }
        }
        _ => {
            errors.push("attempt_status_counts_json must be a valid JSON object".to_owned());
        }
    }

    let expected_completed = actual_counts["COMPLETED"];
    if summary.completed_attempts != expected_completed {
        errors.push(format!(
            "SC-03 violation: completed_attempts={} != count(COMPLETED)={expected_completed}",
            summary.completed_attempts
        ));
    }

    errors.extend(check_attempt_completeness_errors(attempts));

    let has_missing_slots = observed < summary.planned_attempt_slots;
    match aggregate_attempts_usage(
        attempts,
        has_missing_slots,
        options.target_currency,
        options.currency_conversions,
    ) {
        Err(err) => {
            errors.push(err.to_string());
        }
        Ok(recomputed) => {
            if !recomputed.ambiguous_cumulative_logical_ids.is_empty() {
                errors.push(format!(
                    "multiple LOGICAL_RESPONSE_CUMULATIVE rows without PHYSICAL_REQUEST rows for logical_request_id(s): {:?}",
                    recomputed.ambiguous_cumulative_logical_ids
                ));
            }
            if summary.accounting_coverage_status != recomputed.coverage_status {
                errors.push(format!(
                    "summary accounting_coverage_status={:?} mismatches scoped recomputation {:?}",
                    summary.accounting_coverage_status, recomputed.coverage_status
                ));
            }
            check_opt_i64_field(
                &mut errors,
                "total_input_tokens",
                summary.total_input_tokens,
                recomputed.total_input_tokens,
            );
            check_opt_i64_field(
                &mut errors,
                "cached_input_tokens",
                summary.cached_input_tokens,
                recomputed.cached_input_tokens,
            );
            check_opt_i64_field(
                &mut errors,
                "total_output_tokens",
                summary.total_output_tokens,
                recomputed.total_output_tokens,
            );
            check_opt_i64_field(
                &mut errors,
                "reasoning_tokens_subset",
                summary.reasoning_tokens_subset,
                recomputed.reasoning_tokens_subset,
            );
            check_opt_i64_field(
                &mut errors,
                "tool_serialization_tokens_subset",
                summary.tool_serialization_tokens_subset,
                recomputed.tool_serialization_tokens_subset,
            );
            check_opt_i64_field(
                &mut errors,
                "provider_billed_cost_micros",
                summary.provider_billed_cost_micros,
                recomputed.provider_billed_cost_micros,
            );
            check_opt_i64_field(
                &mut errors,
                "estimated_cost_micros",
                summary.estimated_cost_micros,
                recomputed.estimated_cost_micros,
            );
            if summary.currency_code != recomputed.currency_code {
                errors.push(format!(
                    "summary currency_code={:?} mismatches scoped recomputation {:?}",
                    summary.currency_code, recomputed.currency_code
                ));
            }
            check_opt_i64_field(
                &mut errors,
                "elapsed_wall_time_ms",
                summary.elapsed_wall_time_ms,
                recomputed.elapsed_wall_time_ms,
            );
            check_opt_i64_field(
                &mut errors,
                "active_elapsed_ms",
                summary.active_elapsed_ms,
                recomputed.active_elapsed_ms,
            );
        }
    }

    if summary.accounting_coverage_status == AccountingCoverageStatusV5::Unavailable
        && (summary.total_input_tokens.is_some()
            || summary.cached_input_tokens.is_some()
            || summary.total_output_tokens.is_some()
            || summary.reasoning_tokens_subset.is_some()
            || summary.tool_serialization_tokens_subset.is_some()
            || summary.provider_billed_cost_micros.is_some()
            || summary.estimated_cost_micros.is_some())
    {
        errors.push(
            "accounting_coverage_status=UNAVAILABLE must leave token/cost totals null".to_owned(),
        );
    }

    if options.require_complete_coverage
        && summary.accounting_coverage_status != AccountingCoverageStatusV5::Complete
    {
        errors.push(format!(
            "accounting_coverage_status={:?} != 'COMPLETE'",
            summary.accounting_coverage_status
        ));
    }

    let passed = errors.is_empty();
    GEcon01EvaluationV5 {
        gate_id: "G-ECON-01".to_owned(),
        status: if passed {
            "PASS".to_owned()
        } else {
            "FAIL".to_owned()
        },
        passed,
        accounting_coverage_status: summary.accounting_coverage_status,
        spend_or_token_ceiling_applied: false,
        errors,
        descriptive_metrics: DescriptiveMetricsV5 {
            planned_attempt_slots: summary.planned_attempt_slots,
            completed_attempts: summary.completed_attempts,
            total_input_tokens: summary.total_input_tokens,
            cached_input_tokens: summary.cached_input_tokens,
            total_output_tokens: summary.total_output_tokens,
            reasoning_tokens_subset: summary.reasoning_tokens_subset,
            tool_serialization_tokens_subset: summary.tool_serialization_tokens_subset,
            provider_billed_cost_micros: summary.provider_billed_cost_micros,
            estimated_cost_micros: summary.estimated_cost_micros,
            currency_code: summary.currency_code.clone(),
            elapsed_wall_time_ms: summary.elapsed_wall_time_ms,
            active_elapsed_ms: summary.active_elapsed_ms,
        },
    }
}
