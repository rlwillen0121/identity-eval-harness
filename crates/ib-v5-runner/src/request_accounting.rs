//! Physical request accounting and the retry/continuation state machine for one logical
//! response. Opaque continuation state is passed only to the provider, never to artifacts.
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use std::time::Instant;

use ib_core::pyjson::{DumpOptions, dumps};
use ib_providers::base::{ModelRequest, ModelResponse, Provider, Usage};
use ib_providers::json::{MAX_TOKEN_COUNT, json_safe_usage_value, safe_nonnegative_float_add};
use ib_v5_schemas::{
    AttemptStatusV5, ContextLimitCauseV5, CostBasisV5, NormalizedDecodeStatusV5 as Decode,
    ProviderRequestKindV5 as Kind, ProviderRequestStatusV5 as Status, ProviderRequestUsageV5,
    StrictV5Model, UsageScopeV5,
};
use serde_json::{Map, Value, json};

use crate::{
    PricingRatesV5, RunConfigV5, RunnerError, compute_effective_output_allowance_v5,
    select_actual_native_numeric_budget_v5,
};

const OPAQUE: &[&str] = &[
    "continuation_token",
    "opaque_continuation_state",
    "session_token",
    "cursor_token",
    "provider_state_token",
];
const MAX_LATENCY: f64 = f64::MAX / 200_000.0;
type Ordinal = (i64, i64, i64, String, i64);

#[derive(Debug, Clone)]
pub struct RequestContextV5 {
    pub attempt_id: String,
    pub logical_request_id: String,
    pub role: String,
    pub agent_id: String,
    pub dispatch_round: i64,
    pub reservation_ordinal: Ordinal,
}

#[derive(Debug, Clone)]
pub struct RequestParametersV5 {
    pub rendered_input_context_tokens: i64,
    pub effective_output_allowance_tokens: i64,
    pub actual_native_numeric_budget_tokens: Option<i64>,
    pub native_params: Option<Map<String, Value>>,
    pub request_sequence: Arc<AtomicU64>,
    pub run_start: Instant,
}

impl Default for RequestParametersV5 {
    fn default() -> Self {
        Self {
            rendered_input_context_tokens: 0,
            effective_output_allowance_tokens: 64_000,
            actual_native_numeric_budget_tokens: None,
            native_params: None,
            request_sequence: Arc::new(AtomicU64::new(0)),
            run_start: Instant::now(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LogicalRequestOutcomeV5 {
    pub physical_requests: Vec<ProviderRequestUsageV5>,
    pub response: Option<ModelResponse>,
    pub status: Option<AttemptStatusV5>,
    pub terminal_cause: Option<String>,
    pub context_limit_cause: Option<ContextLimitCauseV5>,
    pub error_message: Option<String>,
    pub completed_generation: bool,
    pub normalized_decode_status: Decode,
    pub physical_latency_s: f64,
    pub usage_sum: Usage,
    pub usage_problem_detected: bool,
    pub provider_error_detected: bool,
    pub reported_cost_sum: f64,
    pub reported_cost_count: i64,
    pub active_intervals_ms: Vec<(i64, i64)>,
}

impl Default for LogicalRequestOutcomeV5 {
    fn default() -> Self {
        Self {
            physical_requests: vec![],
            response: None,
            status: None,
            terminal_cause: None,
            context_limit_cause: None,
            error_message: None,
            completed_generation: false,
            normalized_decode_status: Decode::NoResponse,
            physical_latency_s: 0.0,
            usage_sum: Usage::default(),
            usage_problem_detected: false,
            provider_error_detected: false,
            reported_cost_sum: 0.0,
            reported_cost_count: 0,
            active_intervals_ms: vec![],
        }
    }
}

impl LogicalRequestOutcomeV5 {
    fn terminal(
        mut self,
        status: AttemptStatusV5,
        cause: &str,
        error: &str,
        provider_error: bool,
        usage_problem: bool,
    ) -> Self {
        self.status = Some(status);
        self.terminal_cause = Some(cause.to_owned());
        self.error_message = Some(error.to_owned());
        self.provider_error_detected |= provider_error;
        self.usage_problem_detected |= usage_problem;
        self
    }
}

fn raw(response: &ModelResponse) -> Map<String, Value> {
    response.raw.clone().unwrap_or_default()
}
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(v)) => *v,
        Some(Value::String(v)) => !v.is_empty(),
        Some(Value::Array(v)) => !v.is_empty(),
        Some(Value::Object(v)) => !v.is_empty(),
        Some(Value::Number(v)) => v.as_f64() != Some(0.0),
    }
}
fn compact(value: &Value) -> Result<String, RunnerError> {
    dumps(value, &DumpOptions::compact_sorted())
        .map_err(|e| RunnerError::InvalidInput(e.to_string()))
}

fn validate_reconciliation(value: &Map<String, Value>) -> Result<(), RunnerError> {
    let allowed = [
        "disposition",
        "reconciled_input_context_tokens",
        "reconciled_output_allowance_tokens",
        "detail",
    ];
    if value.keys().any(|key| !allowed.contains(&key.as_str()))
        || !matches!(
            value.get("disposition").and_then(Value::as_str),
            Some(
                "CONFIRMED_LIMIT"
                    | "MISCOUNT_OR_FRAMING_FAULT"
                    | "UNEXPLAINED_REJECTION"
                    | "RECONCILED_RESUMABLE"
            )
        )
        || value.get("detail").is_some_and(|v| !v.is_string())
    {
        return Err(RunnerError::InvalidInput(
            "invalid context reconciliation schema".into(),
        ));
    }
    for key in [
        "reconciled_input_context_tokens",
        "reconciled_output_allowance_tokens",
    ] {
        if let Some(v) = value.get(key).filter(|v| !v.is_null())
            && !v.as_i64().is_some_and(|v| v >= 0)
        {
            return Err(RunnerError::InvalidInput(
                "invalid context reconciliation token count".into(),
            ));
        }
    }
    Ok(())
}

pub fn usage_problem(response: &ModelResponse) -> Option<&'static str> {
    if truthy(response.raw.as_ref().and_then(|m| m.get("usage_anomaly"))) {
        return Some("usage_invalid_counts");
    }
    let u = response.usage;
    if [
        u.input_tokens,
        u.output_tokens,
        u.cache_read_tokens,
        u.cache_write_tokens,
        u.reasoning_tokens,
    ]
    .into_iter()
    .any(|v| v > MAX_TOKEN_COUNT)
    {
        return Some("usage_invalid_counts");
    }
    if u.input_tokens + u.cache_read_tokens + u.cache_write_tokens == 0 {
        return Some("usage_missing_input");
    }
    if u.output_tokens == 0 {
        return Some("usage_missing_output");
    }
    if u.reasoning_tokens > u.output_tokens {
        return Some("usage_reasoning_exceeds_output");
    }
    None
}

pub fn has_decode_progress(response: &ModelResponse, segment: i64) -> bool {
    if segment >= 1 {
        return true;
    }
    let m = raw(response);
    if response.error.is_some() && m.get("cli_buffered_error") == Some(&Value::Bool(true)) {
        return false;
    }
    response.text.as_ref().is_some_and(|s| !s.is_empty())
        || response.parsed.as_ref().is_some_and(|p| !p.is_empty())
        || !response.tool_calls.is_empty()
        || response.usage.output_tokens > 0
        || response.usage.reasoning_tokens > 0
        || [
            "decode_progress",
            "thinking",
            "partial_tool_bytes",
            "continuation_token",
            "opaque_continuation_state",
            "checkpoint_id",
        ]
        .iter()
        .any(|k| truthy(m.get(*k)))
}

pub fn normalized_decode_status(response: &ModelResponse) -> Decode {
    if let Some(value) = response
        .raw
        .as_ref()
        .and_then(|m| m.get("normalized_decode_status"))
        && let Ok(status) = serde_json::from_value::<Decode>(value.clone())
    {
        return status;
    }
    if response.error.is_some() {
        return Decode::NoResponse;
    }
    match response.stop_reason.as_deref() {
        Some("segment_requires_continuation" | "pause_turn" | "max_segment_tokens") => {
            Decode::SegmentRequiresContinuation
        }
        Some(
            "refusal" | "content_filter" | "safety" | "SAFETY" | "RECITATION"
            | "PROHIBITED_CONTENT",
        ) => Decode::TerminalRefusal,
        _ if response.refused => Decode::TerminalRefusal,
        Some("max_tokens" | "length" | "max_output_tokens") => Decode::TerminalOutputLimit,
        _ => Decode::TerminalNormal,
    }
}

pub fn sanitize_response_latency(response: &mut ModelResponse) {
    if !response.latency_s.is_finite()
        || response.latency_s < 0.0
        || response.latency_s > MAX_LATENCY
    {
        let old = if response.latency_s.is_finite() {
            json!(response.latency_s)
        } else {
            json!({"__identitybench_usage_marker__": "nonfinite_float"})
        };
        let m = response.raw.get_or_insert_with(Map::new);
        m.insert("latency_anomaly".into(), json!("invalid_or_out_of_range"));
        m.insert("latency_original_s".into(), old);
        response.latency_s = 0.0;
    }
}

/// Round the decimal spelling of a provider USD amount to micros (Python Decimal HALF_UP),
/// with checked arithmetic so unrepresentable evidence cannot silently saturate into a cost.
fn billed_micros(value: f64) -> Option<i64> {
    if !value.is_finite() || value < 0.0 {
        return None;
    }
    let s = ib_core::pyfloat::repr(value);
    let (base, exponent) = s
        .split_once('e')
        .map_or((s.as_str(), 0), |(a, b)| (a, b.parse::<i32>().unwrap_or(0)));
    let fraction = base.split_once('.').map_or(0, |(_, b)| b.len() as i32);
    let digits: String = base.chars().filter(|c| c.is_ascii_digit()).collect();
    let significand = digits.parse::<u128>().ok()?;
    let scale = exponent - fraction + 6;
    let rounded = if scale >= 0 {
        significand.checked_mul(10_u128.checked_pow(scale as u32)?)?
    } else if -scale > 38 {
        0
    } else {
        let denominator = 10_u128.checked_pow((-scale) as u32)?;
        (significand + denominator / 2) / denominator
    };
    i64::try_from(rounded).ok()
}

fn estimated_micros(u: Usage, pricing: &PricingRatesV5) -> Option<i64> {
    let numerator = u128::from(u.input_tokens)
        .checked_mul(pricing.uncached_input_micros_per_million as u128)?
        .checked_add(
            u128::from(u.cache_read_tokens + u.cache_write_tokens)
                .checked_mul(pricing.cached_input_micros_per_million as u128)?,
        )?
        .checked_add(
            u128::from(u.output_tokens.saturating_sub(u.reasoning_tokens))
                .checked_mul(pricing.output_micros_per_million as u128)?,
        )?
        .checked_add(
            u128::from(u.reasoning_tokens)
                .checked_mul(pricing.reasoning_micros_per_million as u128)?,
        )?;
    i64::try_from((numerator + 500_000) / 1_000_000).ok()
}

#[derive(Debug, Clone)]
struct PhysicalState {
    kind: Kind,
    retry: Option<i64>,
    quota: Option<i64>,
    recovery: Option<i64>,
    continuation: Option<i64>,
    segment: i64,
}
impl Default for PhysicalState {
    fn default() -> Self {
        Self {
            kind: Kind::Initial,
            retry: Some(0),
            quota: None,
            recovery: None,
            continuation: None,
            segment: 0,
        }
    }
}
impl PhysicalState {
    fn transition(&mut self, kind: Kind, index: i64) {
        self.kind = kind;
        self.retry = (kind == Kind::TransientRetry).then_some(index);
        self.quota = (kind == Kind::QuotaContinuation).then_some(index);
        self.recovery = (kind == Kind::RecoveryContinuation).then_some(index);
        self.continuation = (kind == Kind::SegmentContinuation).then_some(index);
    }
}

#[allow(clippy::too_many_arguments)]
fn record(
    context: &RequestContextV5,
    params: &RequestParametersV5,
    state: &PhysicalState,
    req_id: String,
    response: &ModelResponse,
    status: Status,
    decode: Decode,
    pricing: Option<&PricingRatesV5>,
    reports_usage: bool,
    native_params: &Map<String, Value>,
) -> Result<ProviderRequestUsageV5, RunnerError> {
    let m = raw(response);
    let usage_valid = reports_usage && usage_problem(response).is_none();
    let u = response.usage;
    let mut raw_usage = json!({"input_tokens_uncached":u.input_tokens,
        "cache_read_tokens":u.cache_read_tokens,"cache_write_tokens":u.cache_write_tokens,
        "output_tokens":u.output_tokens,"reasoning_tokens":u.reasoning_tokens});
    if let Some(provider_raw) = m.get("raw_usage").filter(|v| v.is_object() || v.is_array()) {
        raw_usage["provider_raw_usage"] = json_safe_usage_value(provider_raw, OPAQUE);
    }
    for key in [
        "usage_original",
        "usage_anomaly",
        "latency_anomaly",
        "latency_original_s",
        "reported_cost_saturated",
    ] {
        if let Some(value) = m.get(key) {
            raw_usage[key] = json_safe_usage_value(value, OPAQUE);
        }
    }
    let mut basis = CostBasisV5::Unavailable;
    let mut pricing_sha = None;
    let mut billed = None;
    let mut estimated = None;
    if let Some(value) = response.reported_cost_usd {
        billed = billed_micros(value);
        if value.is_finite() && billed.is_none() {
            return Err(RunnerError::InvalidInput(
                "provider cost is negative or exceeds the supported integer range".into(),
            ));
        }
        if billed.is_some() {
            basis = CostBasisV5::ProviderBilled
        } else {
            raw_usage["provider_reported_cost_usd_original"] =
                json_safe_usage_value(&json!(value), OPAQUE);
        }
    } else if usage_valid && let Some(price) = pricing {
        estimated = estimated_micros(u, price);
        if estimated.is_none() {
            return Err(RunnerError::InvalidInput(
                "estimated cost exceeds the supported integer range".into(),
            ));
        }
        if estimated.is_some() {
            basis = CostBasisV5::FrozenRateEstimate;
            pricing_sha = Some(price.pricing_manifest_sha256.clone());
        }
    }
    let mut native = native_params.clone();
    for key in OPAQUE {
        native.remove(*key);
    }
    let refusal = if decode == Decode::TerminalRefusal
        || response.refused
        || m.get("request_policy_rejection") == Some(&json!(true))
        || m.get("error_class") == Some(&json!("CONTENT_POLICY_REJECTION"))
    {
        Some(true)
    } else if status == Status::Completed {
        Some(false)
    } else {
        None
    };
    let row = ProviderRequestUsageV5 {
        request_id: req_id,
        logical_request_id: context.logical_request_id.clone(),
        request_kind: state.kind,
        retry_index: state.retry,
        quota_continuation_index: state.quota,
        recovery_continuation_index: state.recovery,
        segment_continuation_index: state.continuation,
        decode_segment_index: state.segment,
        attempt_id: context.attempt_id.clone(),
        agent_id: context.agent_id.clone(),
        role: context.role.clone(),
        dispatch_round: context.dispatch_round,
        reservation_ordinal: context.reservation_ordinal.clone(),
        request_status: status,
        post_terminal: false,
        provider_stop_reason: response.stop_reason.clone(),
        provider_http_status: m
            .get("http_status")
            .and_then(Value::as_i64)
            .filter(|n| (100..=599).contains(n)),
        provider_error_code: m
            .get("error_code")
            .and_then(Value::as_str)
            .map(str::to_owned),
        provider_decode_status_raw: m
            .get("decode_status_raw")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| response.stop_reason.clone()),
        normalized_decode_status: decode,
        provider_refusal_signal: refusal,
        actual_native_numeric_budget_tokens: params.actual_native_numeric_budget_tokens,
        native_generation_parameters_json: if native.is_empty() {
            None
        } else {
            Some(compact(&Value::Object(native))?)
        },
        rendered_input_context_tokens: Some(params.rendered_input_context_tokens),
        effective_output_allowance_tokens: Some(params.effective_output_allowance_tokens),
        input_tokens_total: if usage_valid {
            Some((u.input_tokens + u.cache_read_tokens + u.cache_write_tokens) as i64)
        } else {
            None
        },
        cached_input_tokens: usage_valid.then_some(u.cache_read_tokens as i64),
        output_tokens_total: usage_valid.then_some(u.output_tokens as i64),
        reasoning_tokens_subset: usage_valid.then_some(u.reasoning_tokens as i64),
        tool_serialization_tokens_subset: if usage_valid {
            m.get("tool_serialization_tokens_subset")
                .and_then(Value::as_i64)
                .filter(|v| *v >= 0 && *v as u64 <= u.output_tokens)
        } else {
            None
        },
        provider_reported_cost_micros: billed,
        estimated_cost_micros: estimated,
        cost_basis: basis,
        pricing_manifest_sha256: pricing_sha,
        raw_usage_fields_json: Some(compact(&raw_usage)?),
        usage_scope: if usage_valid
            && m.get("usage_scope") == Some(&json!("LOGICAL_RESPONSE_CUMULATIVE"))
        {
            UsageScopeV5::LogicalResponseCumulative
        } else if usage_valid {
            UsageScopeV5::PhysicalRequest
        } else {
            UsageScopeV5::Unavailable
        },
        currency_code: (basis != CostBasisV5::Unavailable).then(|| "USD".to_owned()),
    };
    row.validate()
        .map_err(|e| RunnerError::Schema(e.to_string()))?;
    Ok(row)
}

pub async fn execute_logical_request_v5(
    provider: &dyn Provider,
    config: &RunConfigV5,
    request: ModelRequest,
    context: RequestContextV5,
    mut params: RequestParametersV5,
) -> Result<LogicalRequestOutcomeV5, RunnerError> {
    let mut outcome = LogicalRequestOutcomeV5::default();
    if params.effective_output_allowance_tokens < config.effort_minimum_output_tokens as i64 {
        outcome.context_limit_cause = Some(ContextLimitCauseV5::AllowanceBelowEffortMin);
        return Ok(outcome.terminal(
            AttemptStatusV5::WithinProtocolContextLimit,
            "ALLOWANCE_BELOW_EFFORT_MIN",
            "effective output allowance below effort minimum",
            false,
            false,
        ));
    }
    let mut state = PhysicalState::default();
    let (mut next_retry, mut next_quota, mut next_recovery, mut next_segment) = (1, 0, 0, 0);
    let mut any_progress = false;
    let mut malformed_reminder = false;
    let mut opaque = Map::new();
    let mut last_request_id = None;
    let mut texts = Vec::new();
    let mut calls = Vec::new();
    loop {
        let mut req = request.clone();
        req.params.max_output_tokens = params.effective_output_allowance_tokens as u64;
        req.params.extra.extend(opaque.clone());
        req.params
            .extra
            .insert("decode_segment_index".into(), json!(state.segment));
        req.params.extra.insert(
            "logical_request_id".into(),
            json!(context.logical_request_id),
        );
        if state.kind != Kind::Initial {
            req.params
                .extra
                .insert("request_kind".into(), serde_json::to_value(state.kind)?);
        }
        if let Some(budget) = params.actual_native_numeric_budget_tokens {
            req.params
                .extra
                .insert("native_numeric_budget_tokens".into(), json!(budget));
        }
        if malformed_reminder {
            req.system.push_str("\n\nCRITICAL REMINDER: Do NOT invoke native function/tool calls; emit ONLY the plain-text JSON object {\"actions\":[...]}.");
        }
        let mut native = params.native_params.clone().unwrap_or_default();
        native.extend(json!({"effort":config.effort,"native_effort_policy":config.native_effort_policy,
            "max_output_tokens":params.effective_output_allowance_tokens,"decode_segment_index":state.segment}).as_object().unwrap().clone());
        if let Some(budget) = params.actual_native_numeric_budget_tokens {
            native.insert("actual_native_numeric_budget_tokens".into(), json!(budget));
        }
        let seq = params.request_sequence.fetch_add(1, Ordering::SeqCst) + 1;
        let req_id = format!("{}:req:{seq}", context.attempt_id);
        let start = params.run_start.elapsed().as_millis().min(i64::MAX as u128) as i64;
        let mut response = provider.complete(&req).await;
        let end = params.run_start.elapsed().as_millis().min(i64::MAX as u128) as i64;
        outcome.active_intervals_ms.push((start, end));
        sanitize_response_latency(&mut response);
        outcome.physical_latency_s =
            (outcome.physical_latency_s + response.latency_s).min(MAX_LATENCY);
        let m = raw(&response);
        if let Some(id) = m.get("provider_request_id").and_then(Value::as_str) {
            last_request_id = Some(id.to_owned());
        }
        for key in OPAQUE
            .iter()
            .copied()
            .chain(std::iter::once("checkpoint_id"))
        {
            if let Some(value) = m.get(key) {
                opaque.insert(key.to_owned(), value.clone());
            }
        }
        if let Some(cost) = response.reported_cost_usd {
            if let Ok((sum, _)) = safe_nonnegative_float_add(outcome.reported_cost_sum, cost) {
                outcome.reported_cost_count += 1;
                outcome.reported_cost_sum = sum;
            } else {
                outcome.usage_problem_detected = true;
            }
        }
        if usage_problem(&response).is_none() {
            outcome.usage_sum.add(&response.usage)
        } else {
            outcome.usage_problem_detected = true
        }
        let progress = has_decode_progress(&response, 0);
        any_progress |= progress;
        let class = m.get("error_class").and_then(Value::as_str).unwrap_or("");
        let code = m.get("error_code").and_then(Value::as_str).unwrap_or("");
        let is_error = response.error.is_some();
        let policy = is_error
            && config.allow_request_policy_rejection_map
            && (m.get("request_policy_rejection") == Some(&json!(true))
                || class == "CONTENT_POLICY_REJECTION"
                || [
                    "content_policy_violation",
                    "request_content_policy_rejected",
                    "safety_block",
                    "content_filter",
                ]
                .contains(&code));
        let context_limit = is_error
            && (class == "CONTEXT_LIMIT_REJECTION"
                || [
                    "context_length_exceeded",
                    "prompt_too_long",
                    "context_window_exceeded",
                    "max_context_length_exceeded",
                ]
                .contains(&code));
        let quota = is_error
            && (class == "QUOTA_INTERRUPTION"
                || m.get("quota_exhausted") == Some(&json!(true))
                || [
                    "insufficient_quota",
                    "quota_exceeded",
                    "rate_limit_quota_exhausted",
                ]
                .contains(&code));
        let ambiguous = is_error
            && (class == "AMBIGUOUS_FAILURE"
                || m.get("ambiguous_timeout") == Some(&json!(true))
                || progress);
        let transient = is_error
            && (class == "TRANSIENT_FAILURE"
                || m.get("transient") == Some(&json!(true))
                || code == "CLI_MALFORMED_FUNCTION_CALL"
                || m.get("http_status")
                    .and_then(Value::as_i64)
                    .is_some_and(|n| [408, 429, 500, 502, 503, 504].contains(&n)));
        let status = if policy || context_limit {
            Status::NontransientFailure
        } else if quota {
            Status::QuotaInterruption
        } else if ambiguous {
            Status::AmbiguousFailure
        } else if transient {
            Status::TransientFailure
        } else if is_error {
            Status::NontransientFailure
        } else {
            Status::Completed
        };
        let decode = if ambiguous && progress {
            Decode::Unknown
        } else if is_error {
            Decode::NoResponse
        } else {
            normalized_decode_status(&response)
        };
        outcome.physical_requests.push(record(
            &context,
            &params,
            &state,
            req_id.clone(),
            &response,
            status,
            decode,
            config.pricing.as_ref(),
            provider.capabilities().reports_usage,
            &native,
        )?);
        if policy {
            return Ok(outcome.terminal(
                AttemptStatusV5::WithinProtocolRefusal,
                "PROVIDER_REQUEST_POLICY_REJECTION",
                "provider rejected request under documented content policy",
                false,
                false,
            ));
        }
        if context_limit {
            let reconciliation = m
                .get("context_reconciliation")
                .and_then(Value::as_object)
                .cloned()
                .or_else(|| provider.reconcile_context_count(&req, &response))
                .unwrap_or_else(|| {
                    json!({"disposition":"UNEXPLAINED_REJECTION"})
                        .as_object()
                        .unwrap()
                        .clone()
                });
            validate_reconciliation(&reconciliation)?;
            match reconciliation.get("disposition").and_then(Value::as_str) {
                Some("CONFIRMED_LIMIT") => {
                    outcome.context_limit_cause =
                        Some(ContextLimitCauseV5::ProviderRejectionConfirmedLimit);
                    return Ok(outcome.terminal(
                        AttemptStatusV5::WithinProtocolContextLimit,
                        "PROVIDER_REJECTION_CONFIRMED_LIMIT",
                        "provider rejection confirmed context/output limit",
                        false,
                        false,
                    ));
                }
                Some("MISCOUNT_OR_FRAMING_FAULT") => {
                    return Ok(outcome.terminal(
                        AttemptStatusV5::IncompleteHarnessFailure,
                        "CONTEXT_COUNT_MISCOUNT_OR_FRAMING_FAULT",
                        "harness context count or framing defect",
                        false,
                        true,
                    ));
                }
                Some("RECONCILED_RESUMABLE") => {
                    if state.segment >= 1 || any_progress {
                        return Ok(outcome.terminal(
                            AttemptStatusV5::IncompleteHarnessFailure,
                            "SC02_ALLOWANCE_MUTATION_FORBIDDEN_AFTER_DECODE_PROGRESS",
                            "cannot mutate allowance after decode progress",
                            false,
                            true,
                        ));
                    }
                    if let Some(tokens) = reconciliation
                        .get("reconciled_input_context_tokens")
                        .and_then(Value::as_i64)
                    {
                        if tokens < 0 {
                            return Err(RunnerError::InvalidInput(
                                "negative reconciled context count".into(),
                            ));
                        }
                        params.rendered_input_context_tokens = tokens;
                    }
                    let allowance = reconciliation
                        .get("reconciled_output_allowance_tokens")
                        .and_then(Value::as_i64)
                        .unwrap_or_else(|| {
                            compute_effective_output_allowance_v5(
                                config.base_effective_output_ceiling_tokens as i64,
                                config.provider_max_output_tokens as i64,
                                config.context_window_tokens as i64,
                                params.rendered_input_context_tokens,
                                config.provider_schema_overhead_tokens as i64,
                                config.minimum_context_headroom_tokens as i64,
                            )
                        });
                    if allowance < config.effort_minimum_output_tokens as i64 {
                        outcome.context_limit_cause =
                            Some(ContextLimitCauseV5::ProviderRejectionConfirmedLimit);
                        return Ok(outcome.terminal(
                            AttemptStatusV5::WithinProtocolContextLimit,
                            "PROVIDER_REJECTION_CONFIRMED_LIMIT",
                            "reconciled allowance below effort minimum",
                            false,
                            false,
                        ));
                    }
                    if next_recovery < config.max_recovery_continuations {
                        params.effective_output_allowance_tokens = allowance;
                        params.actual_native_numeric_budget_tokens =
                            select_actual_native_numeric_budget_v5(config, allowance)?;
                        state.transition(Kind::RecoveryContinuation, next_recovery as i64);
                        next_recovery += 1;
                        continue;
                    }
                }
                _ => {}
            }
            return Ok(outcome.terminal(
                AttemptStatusV5::IncompleteProviderFailure,
                "UNEXPLAINED_CONTEXT_LIMIT_REJECTION",
                "unexplained provider context limit rejection",
                true,
                true,
            ));
        }
        let resumable = m.get("resumable") == Some(&json!(true));
        if quota {
            let unanswered = m
                .get("proven_unanswered")
                .map_or(!progress, |v| v == &json!(true));
            if (((!progress || resumable) && unanswered) || resumable)
                && next_quota < config.max_quota_continuations
            {
                state.transition(Kind::QuotaContinuation, next_quota as i64);
                next_quota += 1;
                continue;
            }
            return Ok(outcome.terminal(
                AttemptStatusV5::IncompleteProviderQuota,
                "PROVIDER_QUOTA_INTERRUPTION",
                "provider quota interrupted completion",
                true,
                true,
            ));
        }
        if ambiguous {
            let retrieved = if let Some(id) = last_request_id.as_deref() {
                provider.retrieve_response(id).await
            } else {
                None
            };
            if let Some(retrieved) = retrieved.filter(|r| r.error.is_none()) {
                response = retrieved;
                // Match Python: retrieval resolves the same physical response, never starts
                // another decode or increments generation/retry reservations.
                outcome.physical_requests.push(record(
                    &context,
                    &params,
                    &state,
                    req_id,
                    &response,
                    Status::Completed,
                    normalized_decode_status(&response),
                    config.pricing.as_ref(),
                    provider.capabilities().reports_usage,
                    &native,
                )?);
            } else if resumable
                && !opaque.is_empty()
                && next_recovery < config.max_recovery_continuations
            {
                state.transition(Kind::RecoveryContinuation, next_recovery as i64);
                next_recovery += 1;
                continue;
            } else {
                outcome.response = Some(response);
                outcome.normalized_decode_status = decode;
                return Ok(outcome.terminal(
                    AttemptStatusV5::IncompleteProviderFailure,
                    "AMBIGUOUS_OR_PARTIAL_DECODE_UNRECOVERABLE",
                    "ambiguous or partial decode failure cannot be restarted",
                    true,
                    true,
                ));
            }
        } else if transient {
            let unanswered = m.get("proven_unanswered").is_none_or(|v| v == &json!(true));
            if unanswered && !any_progress {
                malformed_reminder |= code == "CLI_MALFORMED_FUNCTION_CALL";
                if next_retry <= config.max_transient_retries {
                    state.transition(Kind::TransientRetry, next_retry as i64);
                    next_retry += 1;
                    continue;
                }
                if next_recovery < config.max_recovery_continuations {
                    state.transition(Kind::RecoveryContinuation, next_recovery as i64);
                    next_recovery += 1;
                    continue;
                }
            } else if unanswered
                && resumable
                && !opaque.is_empty()
                && next_recovery < config.max_recovery_continuations
            {
                state.transition(Kind::RecoveryContinuation, next_recovery as i64);
                next_recovery += 1;
                continue;
            }
            return Ok(outcome.terminal(
                AttemptStatusV5::IncompleteProviderFailure,
                "TRANSIENT_OR_RECOVERY_RETRIES_EXHAUSTED",
                "provider returned transient errors past retry/recovery budget",
                true,
                true,
            ));
        } else if is_error {
            outcome.response = Some(response);
            return Ok(outcome.terminal(
                AttemptStatusV5::IncompleteProviderFailure,
                "PROVIDER_NONTRANSIENT_ERROR",
                "provider returned an error; details omitted from execution artifact",
                true,
                true,
            ));
        }
        let decode = normalized_decode_status(&response);
        outcome.normalized_decode_status = decode;
        if let Some(text) = &response.text
            && !text.is_empty()
        {
            texts.push(text.clone());
        }
        calls.extend(response.tool_calls.iter().cloned());
        if decode == Decode::SegmentRequiresContinuation {
            if config.segment_continuation_capability != "SUPPORTED" {
                return Ok(outcome.terminal(
                    AttemptStatusV5::IncompleteProviderFailure,
                    "SEGMENT_CONTINUATION_UNSUPPORTED",
                    "provider hit segment boundary without supported continuation",
                    true,
                    true,
                ));
            }
            if next_segment >= config.max_segment_continuations {
                return Ok(outcome.terminal(
                    AttemptStatusV5::IncompleteProviderFailure,
                    "SEGMENT_CONTINUATION_LIMIT_EXCEEDED",
                    "segment continuation limit exceeded",
                    true,
                    true,
                ));
            }
            state.segment += 1;
            state.transition(Kind::SegmentContinuation, next_segment as i64);
            next_segment += 1;
            continue;
        }
        if !texts.is_empty() {
            response.text = Some(texts.join(""));
        }
        response.tool_calls = calls;
        response.usage = outcome.usage_sum;
        response.latency_s = outcome.physical_latency_s;
        response.reported_cost_usd =
            (outcome.reported_cost_count > 0).then_some(outcome.reported_cost_sum);
        response.error = None;
        outcome.response = Some(response);
        outcome.completed_generation = true;
        return Ok(outcome);
    }
}
