//! Episode orchestration over injected providers or deterministic scripted policies.

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::AtomicU64;
use std::time::Instant;

use ib_providers::base::{Message, ModelRequest, ModelResponse, Provider, ToolCall, Usage};
use ib_providers::json::{extract_json_object, strip_reasoning};
use ib_v5_accounting::{
    ProviderAccountingTraceV5, ProviderIdentityInfo, ProviderRoleV5,
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingStatusV5, V5_USAGE_ACCOUNTING_POLICY_ID,
    effective_local_retry_contract_v5, provider_fingerprint_sha256_v5,
};
use ib_v5_oracle::{TraceContextV5, evaluate_phase_with_trace};
use ib_v5_runtime::{
    DiagnosticServiceV5, GoldTraceEntryV5, GoldTraceMap, RuntimeOptionsV5, RuntimeV5,
    SemanticDiagnosticService, Stage5Context, ViewV5,
};
use ib_v5_schemas::{
    AtomicMutationV5, AttemptStatusV5, CoSignScopeKindV5, ContextLimitCauseV5, EpisodeAttemptV5,
    EpisodeV5, PhaseManifestV5, ProviderRequestUsageV5, SealedEpisodeOracleV5,
    SteerTriggerReasonV5, WorldStateV5,
};
use ib_v5_scoring::{
    CompletionTraceV5, EpisodeExecutionV5, PhaseTrajectoryV5, ToolTraceV5, TrajectorySnapshotV5,
    UsageTotalsV5,
};
use serde_json::{Map, Value, json};

use crate::config::RunConfigV5;
use crate::error::RunnerError;
use crate::prompts::{
    HARNESS_MSG_EPISODE_ALREADY_COMPLETED_V5, HARNESS_MSG_INVALID_ENVELOPE_V5,
    HARNESS_MSG_TOOL_SCHEMA_ERROR_V5, HARNESS_MSG_ZERO_CALL_CONTINUE_V5,
    PRIMARY_PROMPT_BUNDLE_SHA256_V5, make_request, render_v5_episode_opening, tool_specs_for_role,
};
use crate::request::preflight_obs_strategy_witness_v5;
use crate::request_accounting::{
    LogicalRequestOutcomeV5, RequestContextV5, RequestParametersV5, execute_logical_request_v5,
    usage_problem,
};

pub use ib_v5_scoring::CompletionTraceV5 as SharedCompletionTraceV5;

#[derive(Debug, Clone)]
pub struct AgentTurnContextV5 {
    pub episode_id: String,
    pub policy_card: ib_v5_schemas::PolicyCardV5,
    pub phase_index: i64,
    pub manifest: PhaseManifestV5,
    pub view: ViewV5,
    pub delivered_slices: Vec<ib_v5_schemas::DeliveredSliceV5>,
    pub delivered_slice_ids: Vec<String>,
    pub turn_index: u64,
}

/// Agent-visible callback for deterministic offline policies.
pub trait TurnPolicyV5: Send + Sync {
    fn next_turn(&self, context: &AgentTurnContextV5) -> Vec<ToolCall>;
}

#[derive(Clone)]
pub struct ProviderBundleV5 {
    pub planner: Arc<dyn Provider>,
    pub executor: Arc<dyn Provider>,
    pub auditor: Arc<dyn Provider>,
}

impl ProviderBundleV5 {
    fn for_role(&self, role: &str) -> Result<Arc<dyn Provider>, RunnerError> {
        match role {
            "planner" => Ok(self.planner.clone()),
            "executor" => Ok(self.executor.clone()),
            "auditor" => Ok(self.auditor.clone()),
            _ => Err(RunnerError::InvalidInput(format!(
                "unknown provider role {role:?}"
            ))),
        }
    }
}

#[derive(Clone)]
pub enum ProviderInputV5 {
    Single(Arc<dyn Provider>),
    Roles(ProviderBundleV5),
}

impl From<Arc<dyn Provider>> for ProviderInputV5 {
    fn from(value: Arc<dyn Provider>) -> Self {
        Self::Single(value)
    }
}

impl From<ProviderBundleV5> for ProviderInputV5 {
    fn from(value: ProviderBundleV5) -> Self {
        Self::Roles(value)
    }
}

impl ProviderInputV5 {
    fn for_role(&self, role: &str) -> Result<Arc<dyn Provider>, RunnerError> {
        match self {
            Self::Single(provider) => Ok(provider.clone()),
            Self::Roles(bundle) => bundle.for_role(role),
        }
    }

    fn role_names(&self, config: &RunConfigV5) -> Result<Vec<String>, RunnerError> {
        Ok(config
            .resolved_topology_profile()?
            .roles
            .iter()
            .map(|role| role.role_id.clone())
            .collect())
    }
}

#[derive(Clone)]
pub struct RoundRequestSpecV5 {
    pub role: String,
    pub agent_id: String,
    pub messages: Vec<Message>,
    pub phase_index: u8,
    pub provider: Option<Arc<dyn Provider>>,
}

#[derive(Clone)]
pub struct RunEpisodeOptionsV5 {
    pub run_config: RunConfigV5,
    /// Optional host runtime seed. It is kept outside `AgentTurnContextV5` and is useful
    /// for offline noninterference proofs that vary hidden state under a fixed agent view.
    pub runtime_initial_world: Option<WorldStateV5>,
    pub phase2_reset_state: Option<WorldStateV5>,
    pub diagnostic_oracle: Option<SealedEpisodeOracleV5>,
    pub model_id: Option<String>,
    pub capture_transcript: Option<bool>,
    pub round_scheduler: Option<RoundSchedulerV5>,
}

impl Default for RunEpisodeOptionsV5 {
    fn default() -> Self {
        Self {
            run_config: RunConfigV5::default(),
            runtime_initial_world: None,
            phase2_reset_state: None,
            diagnostic_oracle: None,
            model_id: None,
            capture_transcript: None,
            round_scheduler: None,
        }
    }
}

/// Optional local scheduler hook for tests and controlled parallel dispatches.
pub type RoundSchedulerV5 =
    Arc<dyn Fn(u64, &RuntimeV5, &RunConfigV5) -> Vec<RoundRequestSpecV5> + Send + Sync>;

pub fn make_v5_diagnostic_runtime_factory(
    episode: &EpisodeV5,
    oracle: &SealedEpisodeOracleV5,
) -> Result<Arc<dyn DiagnosticServiceV5>, RunnerError> {
    if oracle.episode_id != episode.episode_id {
        return Err(RunnerError::InvalidInput(
            "diagnostic_oracle episode_id does not match execution episode".to_owned(),
        ));
    }
    let phase1_world = oracle.omniscient_s1.clone();
    let phase2_world = oracle.omniscient_s2_gold.clone();
    let manifest1 = episode.phase1_manifest.clone();
    let manifest2 = episode.phase2_base_manifest.clone();
    let events = oracle.sealed_jml_adversarial_events.clone();
    let service = SemanticDiagnosticService::new(move |phase_index, _view| {
        let (world, manifest, phase_events): (
            &WorldStateV5,
            &PhaseManifestV5,
            &[ib_v5_schemas::JMLTraceEventV5],
        ) = match phase_index {
            1 => (&phase1_world, &manifest1, &events),
            2 => (&phase2_world, &manifest2, &[]),
            _ => {
                return Err(ib_v5_runtime::VerifierContextError::Invalid(
                    "diagnostic phase must be 1 or 2".to_owned(),
                ));
            }
        };
        let mut gold = evaluate_phase_with_trace(world, &manifest.work_queue, phase_events)
            .map_err(|e| ib_v5_runtime::VerifierContextError::Invalid(e.to_string()))?;
        let mut traces = GoldTraceMap::new();
        for (target, trace) in std::mem::take(&mut gold.trace_by_target) {
            traces.insert(
                target,
                GoldTraceEntryV5 {
                    mutation: trace.mutation,
                    preconditions: trace.preconditions,
                    citations: trace.citations,
                    context: stage5_context(trace.context),
                },
            );
        }
        Ok(traces)
    });
    Ok(Arc::new(service))
}

fn stage5_context(trace: TraceContextV5) -> Stage5Context {
    use std::collections::{BTreeMap, BTreeSet};
    let feasibility = trace.feasibility.map(|callback| {
        Arc::new(move |candidate_ids: &[String]| {
            let result = callback(candidate_ids);
            ib_v5_runtime::OptFeasibility {
                feasible: result.feasible,
                policy_ok: result.policy_ok,
                coverage_ok: result.coverage_ok,
            }
        }) as Arc<dyn Fn(&[String]) -> ib_v5_runtime::OptFeasibility + Send + Sync>
    });
    let failing_predicate = trace
        .failing_predicate
        .map(|predicate| ib_v5_runtime::FailingPredicate::Single(predicate));
    Stage5Context {
        gold_engagement_status: trace.gold_engagement_status,
        pruned_conversion_grant_ids: Some(trace.pruned_conversion_grant_ids.into_iter().collect()),
        cc_sensitive_grant_ids: Some(trace.cc_sensitive_grant_ids.into_iter().collect()),
        nhi_stale_support_predicate: trace.nhi_stale_support_predicate,
        eligible_owner_person_ids: Some(trace.eligible_owner_person_ids.into_iter().collect()),
        valid_custodian_ids: Some(trace.valid_custodian_ids.into_iter().collect()),
        target_subkind: trace.target_subkind,
        feasibility,
        has_disconnected_spokes: trace.has_disconnected_spokes,
        conversion_carryover_sponsor_allowed: trace.conversion_carryover_sponsor_allowed,
        surviving_item_ids: Some(trace.surviving_item_ids.into_iter().collect()),
        valid_reviewers: Some(
            trace
                .valid_reviewers
                .into_iter()
                .map(|(k, v)| (k, v.into_iter().collect()))
                .collect::<BTreeMap<_, BTreeSet<_>>>(),
        ),
        locked_item_ids: Some(trace.locked_item_ids.into_iter().collect()),
        failing_predicate,
        ..Stage5Context::default()
    }
}

pub async fn run_v5_agent_episode(
    episode: &EpisodeV5,
    initial_world: &WorldStateV5,
    provider: impl Into<ProviderInputV5>,
    run_config: RunConfigV5,
) -> Result<EpisodeExecutionV5, RunnerError> {
    run_v5_agent_episode_with_options(
        episode,
        initial_world,
        provider,
        RunEpisodeOptionsV5 {
            run_config,
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
}

pub async fn run_v5_agent_episode_with_options(
    episode: &EpisodeV5,
    initial_world: &WorldStateV5,
    provider: impl Into<ProviderInputV5>,
    options: RunEpisodeOptionsV5,
) -> Result<EpisodeExecutionV5, RunnerError> {
    run_episode(episode, initial_world, Some(provider.into()), None, options).await
}

pub async fn run_v5_scripted_episode(
    episode: &EpisodeV5,
    initial_world: &WorldStateV5,
    turn_policy: &dyn TurnPolicyV5,
    run_config: RunConfigV5,
) -> Result<EpisodeExecutionV5, RunnerError> {
    run_v5_scripted_episode_with_options(
        episode,
        initial_world,
        turn_policy,
        RunEpisodeOptionsV5 {
            run_config,
            capture_transcript: Some(true),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
}

pub async fn run_v5_scripted_episode_with_options(
    episode: &EpisodeV5,
    initial_world: &WorldStateV5,
    turn_policy: &dyn TurnPolicyV5,
    options: RunEpisodeOptionsV5,
) -> Result<EpisodeExecutionV5, RunnerError> {
    run_episode(episode, initial_world, None, Some(turn_policy), options).await
}

#[derive(Default)]
struct RunCounters {
    usage: Usage,
    latency_s: f64,
    completed_generations: i64,
    requested: i64,
    executed: i64,
    max_calls: i64,
    completion_trace: Vec<CompletionTraceV5>,
    tool_trace: Vec<ToolTraceV5>,
    provider_requests: Vec<ProviderRequestUsageV5>,
    reported_cost_sum: f64,
    reported_cost_count: i64,
    active_intervals_ms: Vec<(i64, i64)>,
    usage_problem_detected: bool,
    provider_error_detected: bool,
}

type ReservationOrdinalV5 = (i64, i64, i64, String, i64);

struct ProviderTurnV5 {
    spec: RoundRequestSpecV5,
    ordinal: ReservationOrdinalV5,
    logical_request_id: String,
    provider: Arc<dyn Provider>,
    outcome: LogicalRequestOutcomeV5,
}

#[derive(Default)]
struct T5State {
    planned: HashMap<i64, bool>,
    active_stage: Option<(i64, String, String)>,
    approved_stage: Option<(i64, String, String)>,
    revision_target: Option<String>,
}

struct TerminalV5 {
    status: AttemptStatusV5,
    cause: Option<String>,
    context_limit_cause: Option<ContextLimitCauseV5>,
    error_message: Option<String>,
    ordinal: Option<ReservationOrdinalV5>,
}

async fn run_provider_round(
    provider_input: &ProviderInputV5,
    config: &RunConfigV5,
    profile: &ib_v5_schemas::TopologyProfileV5,
    specs: Vec<RoundRequestSpecV5>,
    remaining_slots: u64,
    dispatch_round: u64,
    attempt_id: &str,
    seq: &Arc<AtomicU64>,
    run_start: Instant,
    agent_local_seqs: &mut HashMap<String, i64>,
) -> Result<Vec<ProviderTurnV5>, RunnerError> {
    let mut reserved = Vec::with_capacity(specs.len());
    for spec in specs {
        let role_priority = profile
            .role_spec(&spec.role)
            .map_err(|e| RunnerError::Schema(e.to_string()))?
            .role_priority;
        let local_seq = agent_local_seqs.get(&spec.agent_id).copied().unwrap_or(0);
        reserved.push((
            (
                dispatch_round as i64,
                profile.topology_priority,
                role_priority,
                spec.agent_id.clone(),
                local_seq,
            ),
            spec,
        ));
    }
    reserved.sort_by(|a, b| a.0.cmp(&b.0));
    reserved.truncate(remaining_slots as usize);
    for (ordinal, spec) in &reserved {
        agent_local_seqs.insert(spec.agent_id.clone(), ordinal.4 + 1);
    }

    struct Pending {
        ordinal: ReservationOrdinalV5,
        spec: RoundRequestSpecV5,
        provider: Arc<dyn Provider>,
        request: ModelRequest,
        context: RequestContextV5,
        logical_request_id: String,
        params: RequestParametersV5,
        early: Option<LogicalRequestOutcomeV5>,
    }
    let mut pending = Vec::with_capacity(reserved.len());
    for (ordinal, spec) in reserved {
        let role_provider = match spec.provider.as_ref() {
            Some(p) => p.clone(),
            None => provider_input.for_role(&spec.role)?,
        };
        let mut request = make_request(
            Some(role_provider.as_ref()),
            config,
            &spec.messages,
            &spec.role,
            spec.phase_index,
            None,
            None,
            Map::new(),
        )?;
        let rendered_tokens = crate::prompts::count_model_request_tokens_v5(&request)?;
        let allowance = crate::config::compute_effective_output_allowance_v5(
            config.base_effective_output_ceiling_tokens as i64,
            config.provider_max_output_tokens as i64,
            config.context_window_tokens as i64,
            rendered_tokens.min(i64::MAX as u64) as i64,
            config.provider_schema_overhead_tokens as i64,
            config.minimum_context_headroom_tokens as i64,
        );
        let numeric = if allowance >= 0 {
            crate::config::select_actual_native_numeric_budget_v5(config, allowance)?
        } else {
            None
        };
        request.params.max_output_tokens = allowance.max(0) as u64;
        let logical_request_id = format!(
            "{attempt_id}:log:{}:{}:{}",
            ordinal.0, spec.agent_id, ordinal.4
        );
        let context = RequestContextV5 {
            attempt_id: attempt_id.to_owned(),
            logical_request_id: logical_request_id.clone(),
            role: spec.role.clone(),
            agent_id: spec.agent_id.clone(),
            dispatch_round: dispatch_round as i64,
            reservation_ordinal: ordinal.clone(),
        };
        let params = RequestParametersV5 {
            rendered_input_context_tokens: rendered_tokens.min(i64::MAX as u64) as i64,
            effective_output_allowance_tokens: allowance,
            actual_native_numeric_budget_tokens: numeric,
            native_params: Some(request.params.extra.clone()),
            request_sequence: seq.clone(),
            run_start,
        };
        let early = (allowance < config.effort_minimum_output_tokens as i64).then(|| {
            let mut value = LogicalRequestOutcomeV5::default();
            value.status = Some(AttemptStatusV5::WithinProtocolContextLimit);
            value.terminal_cause = Some("ALLOWANCE_BELOW_EFFORT_MIN".into());
            value.context_limit_cause = Some(ContextLimitCauseV5::AllowanceBelowEffortMin);
            value.error_message = Some(format!(
                "effective output allowance {allowance} below effort minimum {}",
                config.effort_minimum_output_tokens
            ));
            value
        });
        pending.push(Pending {
            ordinal,
            spec,
            provider: role_provider,
            request,
            context,
            logical_request_id,
            params,
            early,
        });
    }

    let mut join_set = tokio::task::JoinSet::new();
    let mut ready = Vec::new();
    for job in pending {
        if let Some(outcome) = job.early {
            ready.push(ProviderTurnV5 {
                spec: job.spec,
                ordinal: job.ordinal,
                logical_request_id: job.logical_request_id,
                provider: job.provider,
                outcome,
            });
        } else {
            let provider = job.provider.clone();
            let config = config.clone();
            join_set.spawn(async move {
                let result = execute_logical_request_v5(
                    provider.as_ref(),
                    &config,
                    job.request.clone(),
                    job.context.clone(),
                    job.params.clone(),
                )
                .await;
                (job, result)
            });
        }
    }
    while let Some(joined) = join_set.join_next().await {
        let (job, result) =
            joined.map_err(|e| RunnerError::InvalidInput(format!("provider task failed: {e}")))?;
        ready.push(ProviderTurnV5 {
            spec: job.spec,
            ordinal: job.ordinal,
            logical_request_id: job.logical_request_id,
            provider: job.provider,
            outcome: result?,
        });
    }
    ready.sort_by(|a, b| a.ordinal.cmp(&b.ordinal));
    Ok(ready)
}

fn append_role_message(
    messages: &mut Vec<Message>,
    auditor_messages: &mut Vec<Message>,
    topology_t5: bool,
    role: &str,
    message: Message,
) {
    if topology_t5 && role == "auditor" {
        auditor_messages.push(message);
    } else {
        messages.push(message.clone());
        if topology_t5 {
            auditor_messages.push(message);
        }
    }
}

fn process_provider_turn(
    turn: ProviderTurnV5,
    runtime: &mut RuntimeV5,
    config: &RunConfigV5,
    profile: &ib_v5_schemas::TopologyProfileV5,
    t5: &mut T5State,
    counters: &mut RunCounters,
    provider_accounting: &mut [ProviderAccountingTraceV5],
    messages: &mut Vec<Message>,
    auditor_messages: &mut Vec<Message>,
    keep_transcript: bool,
    run_start: Instant,
    is_t5: bool,
) -> Result<Option<TerminalV5>, RunnerError> {
    let ProviderTurnV5 {
        spec,
        ordinal,
        logical_request_id,
        provider,
        outcome,
    } = turn;
    counters.provider_requests.extend(outcome.physical_requests);
    counters
        .active_intervals_ms
        .extend(outcome.active_intervals_ms);
    counters.usage.add(&outcome.usage_sum);
    counters.latency_s = add_latency(counters.latency_s, outcome.physical_latency_s);
    counters.reported_cost_sum += outcome.reported_cost_sum;
    counters.reported_cost_count += outcome.reported_cost_count;
    counters.usage_problem_detected |= outcome.usage_problem_detected;
    counters.provider_error_detected |= outcome.provider_error_detected;
    if outcome.usage_problem_detected {
        mark_accounting_unknown(provider_accounting, &spec.role, "response_usage_invalid");
    }
    if outcome.provider_error_detected {
        mark_accounting_unknown(provider_accounting, &spec.role, "provider_response_error");
    }
    runtime.set_usage_totals(
        counters.usage.input_tokens.min(i64::MAX as u64) as i64,
        counters
            .usage
            .cache_read_tokens
            .saturating_add(counters.usage.cache_write_tokens)
            .min(i64::MAX as u64) as i64,
        counters
            .usage
            .output_tokens
            .saturating_sub(counters.usage.reasoning_tokens)
            .min(i64::MAX as u64) as i64,
        counters.usage.reasoning_tokens.min(i64::MAX as u64) as i64,
    )?;

    if let Some(status) = outcome.status {
        if let Some(response) = outcome.response.as_ref() {
            if outcome.completed_generation {
                counters.completed_generations += 1;
            }
            push_completion(
                counters,
                ordinal.0 as u64,
                &spec.role,
                provider.as_ref(),
                response,
                response.tool_calls.len(),
            );
        }
        return Ok(Some(TerminalV5 {
            status,
            cause: outcome.terminal_cause,
            context_limit_cause: outcome.context_limit_cause,
            error_message: outcome.error_message,
            ordinal: Some(ordinal),
        }));
    }
    let Some(response) = outcome.response else {
        return Ok(Some(TerminalV5 {
            status: AttemptStatusV5::IncompleteHarnessFailure,
            cause: Some("LOGICAL_REQUEST_RETURNED_NO_RESPONSE".into()),
            context_limit_cause: None,
            error_message: Some("provider request returned no response".into()),
            ordinal: Some(ordinal),
        }));
    };
    counters.completed_generations += 1;

    use ib_v5_schemas::NormalizedDecodeStatusV5 as Decode;
    if outcome.normalized_decode_status == Decode::TerminalRefusal {
        push_completion(
            counters,
            ordinal.0 as u64,
            &spec.role,
            provider.as_ref(),
            &response,
            response.tool_calls.len(),
        );
        return Ok(Some(TerminalV5 {
            status: AttemptStatusV5::WithinProtocolRefusal,
            cause: Some("STRUCTURED_ASSISTANT_REFUSAL".into()),
            context_limit_cause: None,
            error_message: Some("provider refused the completion".into()),
            ordinal: Some(ordinal),
        }));
    }
    if outcome.normalized_decode_status == Decode::TerminalOutputLimit {
        push_completion(
            counters,
            ordinal.0 as u64,
            &spec.role,
            provider.as_ref(),
            &response,
            response.tool_calls.len(),
        );
        return Ok(Some(TerminalV5 {
            status: AttemptStatusV5::WithinProtocolOutputLimit,
            cause: Some("TOTAL_OUTPUT_ALLOWANCE_EXHAUSTED".into()),
            context_limit_cause: None,
            error_message: Some("logical output allowance exhausted".into()),
            ordinal: Some(ordinal),
        }));
    }

    let calls = match extract_and_validate_calls(
        &response,
        spec.phase_index,
        &spec.role,
        config,
        provider.capabilities().native_tools,
        &logical_request_id,
    ) {
        Ok(calls) => calls,
        Err(error) => {
            let call_count = error.call_count;
            counters.requested += call_count as i64;
            counters.max_calls = counters.max_calls.max(call_count as i64);
            push_completion(
                counters,
                ordinal.0 as u64,
                &spec.role,
                provider.as_ref(),
                &response,
                0,
            );
            append_role_message(
                messages,
                auditor_messages,
                is_t5,
                &spec.role,
                Message::user(HARNESS_MSG_INVALID_ENVELOPE_V5),
            );
            return Ok(Some(TerminalV5 {
                status: AttemptStatusV5::WithinProtocolParseFailure,
                cause: Some(error.code.into()),
                context_limit_cause: None,
                error_message: Some(HARNESS_MSG_INVALID_ENVELOPE_V5.to_owned()),
                ordinal: Some(ordinal),
            }));
        }
    };
    push_completion(
        counters,
        ordinal.0 as u64,
        &spec.role,
        provider.as_ref(),
        &response,
        calls.len(),
    );

    if is_t5 && spec.role == "planner" && calls.is_empty() {
        let plan = response.text.as_deref().unwrap_or_default().trim();
        if plan.is_empty() {
            append_role_message(
                messages,
                auditor_messages,
                true,
                &spec.role,
                Message::assistant("", vec![]),
            );
            append_role_message(
                messages,
                auditor_messages,
                true,
                &spec.role,
                Message::user("Provide a concise execution plan for the executor."),
            );
        } else {
            t5.planned.insert(spec.phase_index as i64, true);
            append_role_message(
                messages,
                auditor_messages,
                true,
                &spec.role,
                Message::assistant(plan, vec![]),
            );
            append_role_message(
                messages,
                auditor_messages,
                true,
                &spec.role,
                Message::user(
                    "[Handoff from planner to executor] Proceed with execution. Emit only the required tool call(s) with no prose walkthrough.",
                ),
            );
        }
        return Ok(None);
    }

    if calls.is_empty() {
        if is_t5 && spec.role == "auditor" {
            let (decision, target_role, feedback) = auditor_decision(&response);
            if decision.as_deref() == Some("APPROVE") {
                t5.approved_stage = t5.active_stage.clone();
                append_role_message(
                    messages,
                    auditor_messages,
                    true,
                    &spec.role,
                    Message::assistant(response.text.clone().unwrap_or_default(), vec![]),
                );
                let cue = format!(
                    "[Auditor APPROVED] {} Handing off to executor to co-sign and commit.",
                    feedback
                )
                .trim()
                .to_owned();
                append_shared_message(messages, auditor_messages, cue);
                return Ok(None);
            }
            if decision.as_deref() == Some("REVISE") {
                t5.active_stage = None;
                t5.approved_stage = None;
                let target = target_role.unwrap_or_else(|| "executor".to_owned());
                if target == "planner" {
                    t5.planned.insert(spec.phase_index as i64, false);
                }
                t5.revision_target = Some(target.clone());
                append_role_message(
                    messages,
                    auditor_messages,
                    true,
                    &spec.role,
                    Message::assistant(response.text.clone().unwrap_or_default(), vec![]),
                );
                append_shared_message(
                    messages,
                    auditor_messages,
                    format!("[Auditor REVISE -> {target}] {feedback}")
                        .trim()
                        .to_owned(),
                );
                return Ok(None);
            }
        }
        append_role_message(
            messages,
            auditor_messages,
            is_t5,
            &spec.role,
            Message::assistant(response.text.clone().unwrap_or_default(), vec![]),
        );
        append_role_message(
            messages,
            auditor_messages,
            is_t5,
            &spec.role,
            Message::user(HARNESS_MSG_ZERO_CALL_CONTINUE_V5),
        );
        return Ok(None);
    }

    counters.requested += calls.len() as i64;
    counters.max_calls = counters.max_calls.max(calls.len() as i64);
    let permitted = profile
        .permitted_tools_for_role(&spec.role, &config.steering_mode)
        .map_err(|e| RunnerError::Schema(e.to_string()))?;
    let tool_start = run_start.elapsed().as_millis().min(i64::MAX as u128) as i64;
    let mut results = Vec::with_capacity(calls.len());
    let mut episode_completed_here = false;
    for call in &calls {
        let result = if episode_completed_here {
            json!({"status":"EPISODE_ALREADY_COMPLETED","error_detail":HARNESS_MSG_EPISODE_ALREADY_COMPLETED_V5})
        } else {
            let approved = t5.active_stage.is_some() && t5.active_stage == t5.approved_stage;
            let result = dispatch_tool(runtime, call, &permitted, &spec.role, config, approved)?;
            counters.executed += 1;
            if call.name == "stage_phase_mutations"
                && result.get("status") == Some(&json!("STAGED"))
            {
                t5.active_stage = Some((
                    call.arguments
                        .get("phase_index")
                        .and_then(Value::as_i64)
                        .unwrap_or(spec.phase_index as i64),
                    call.arguments
                        .get("stage_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                    result
                        .get("stage_digest")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                ));
                t5.approved_stage = None;
            }
            if call.name == "commit_phase_mutations"
                && matches!(
                    result.get("status").and_then(Value::as_str),
                    Some("COMMITTED_AND_ADVANCED" | "EPISODE_COMPLETED" | "OK")
                )
            {
                t5.active_stage = None;
                t5.approved_stage = None;
            }
            episode_completed_here = result.get("status") == Some(&json!("EPISODE_COMPLETED"))
                || runtime.episode_completed();
            result
        };
        if keep_transcript {
            counters.tool_trace.push(ToolTraceV5 {
                turn_index: ordinal.0,
                tool_name: call.name.clone(),
                arguments: Value::Object(call.arguments.clone()),
                result: result.clone(),
            });
        }
        results.push(result);
    }
    let tool_end = run_start.elapsed().as_millis().min(i64::MAX as u128) as i64;
    counters.active_intervals_ms.push((tool_start, tool_end));

    let assistant = Message::assistant(
        response
            .text
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| " ".to_owned()),
        calls.clone(),
    );
    append_role_message(messages, auditor_messages, is_t5, &spec.role, assistant);
    if provider.capabilities().native_tools {
        for (call, result) in calls.iter().zip(results.iter()) {
            append_role_message(
                messages,
                auditor_messages,
                is_t5,
                &spec.role,
                Message::tool(&call.id, compact_json(result)?),
            );
        }
    } else {
        let observations: Vec<Value> = calls
            .iter()
            .zip(results.iter())
            .map(|(call, result)| {
                json!({
                    "tool_call_id":call.id,"name":call.name,"result":result
                })
            })
            .collect();
        let payload = compact_json(&Value::Array(observations))?;
        append_role_message(
            messages,
            auditor_messages,
            is_t5,
            &spec.role,
            Message::user(format!("## Tool Execution Results\n{payload}")),
        );
    }

    if is_t5 && spec.role == "auditor" && !episode_completed_here {
        let (decision, target_role, feedback) = auditor_decision(&response);
        if decision.as_deref() == Some("APPROVE") {
            t5.approved_stage = t5.active_stage.clone();
            append_shared_message(
                messages,
                auditor_messages,
                format!(
                    "[Auditor APPROVED] {} Handing off to executor to co-sign and commit.",
                    feedback
                )
                .trim()
                .to_owned(),
            );
        } else if decision.as_deref() == Some("REVISE") {
            t5.active_stage = None;
            t5.approved_stage = None;
            let target = target_role.unwrap_or_else(|| "executor".to_owned());
            if target == "planner" {
                t5.planned.insert(spec.phase_index as i64, false);
            }
            t5.revision_target = Some(target.clone());
            append_shared_message(
                messages,
                auditor_messages,
                format!("[Auditor REVISE -> {target}] {feedback}")
                    .trim()
                    .to_owned(),
            );
        }
    }
    if episode_completed_here || runtime.episode_completed() {
        return Ok(Some(TerminalV5 {
            status: AttemptStatusV5::Completed,
            cause: Some("EPISODE_COMPLETED".into()),
            context_limit_cause: None,
            error_message: None,
            ordinal: Some(ordinal),
        }));
    }
    Ok(None)
}

fn append_shared_message(
    messages: &mut Vec<Message>,
    auditor_messages: &mut Vec<Message>,
    content: String,
) {
    let message = Message::user(content);
    messages.push(message.clone());
    auditor_messages.push(message);
}

fn auditor_decision(response: &ModelResponse) -> (Option<String>, Option<String>, String) {
    let value = response.parsed.clone().map(Value::Object).or_else(|| {
        response.text.as_ref().and_then(|text| {
            let body = strip_reasoning(text);
            serde_json::from_str::<Value>(&body).ok().or_else(|| {
                extract_json_object(
                    Some(&body),
                    Some(&["decision"]),
                    Some(&["decision", "approved", "target_role", "feedback"]),
                )
                .map(Value::Object)
            })
        })
    });
    let Some(object) = value.and_then(|v| v.as_object().cloned()) else {
        return (None, None, String::new());
    };
    let decision = object
        .get("decision")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .or_else(|| {
            object
                .get("approved")
                .and_then(Value::as_bool)
                .map(|b| if b { "APPROVE" } else { "REVISE" }.to_owned())
        });
    let target = object
        .get("target_role")
        .and_then(Value::as_str)
        .filter(|s| matches!(*s, "planner" | "executor"))
        .map(str::to_owned);
    let feedback = object
        .get("feedback")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    (decision, target, feedback)
}

fn mark_post_terminal(
    turn: &mut ProviderTurnV5,
    counters: &mut RunCounters,
    provider_accounting: &mut [ProviderAccountingTraceV5],
) {
    for request in &mut turn.outcome.physical_requests {
        request.post_terminal = true;
    }
    counters
        .provider_requests
        .extend(turn.outcome.physical_requests.clone());
    counters
        .active_intervals_ms
        .extend(turn.outcome.active_intervals_ms.clone());
    counters.usage.add(&turn.outcome.usage_sum);
    counters.latency_s = add_latency(counters.latency_s, turn.outcome.physical_latency_s);
    counters.reported_cost_sum += turn.outcome.reported_cost_sum;
    counters.reported_cost_count += turn.outcome.reported_cost_count;
    counters.usage_problem_detected |= turn.outcome.usage_problem_detected;
    counters.provider_error_detected |= turn.outcome.provider_error_detected;
    if turn.outcome.usage_problem_detected {
        mark_accounting_unknown(
            provider_accounting,
            &turn.spec.role,
            "response_usage_invalid",
        );
    }
    if turn.outcome.provider_error_detected {
        mark_accounting_unknown(
            provider_accounting,
            &turn.spec.role,
            "provider_response_error",
        );
    }
    if turn.outcome.completed_generation {
        counters.completed_generations += 1;
        if let Some(response) = turn.outcome.response.as_ref() {
            push_completion(
                counters,
                turn.ordinal.0 as u64,
                &turn.spec.role,
                turn.provider.as_ref(),
                response,
                response.tool_calls.len(),
            );
        }
    }
}

fn mark_accounting_unknown(traces: &mut [ProviderAccountingTraceV5], role: &str, basis: &str) {
    for trace in traces {
        if trace.role == role || (role == "monolith" && trace.role == "unified") {
            trace.status = UsageAccountingStatusV5::Unknown;
            trace.basis = basis.to_owned();
        }
    }
}

async fn run_episode(
    episode: &EpisodeV5,
    initial_world: &WorldStateV5,
    provider: Option<ProviderInputV5>,
    turn_policy: Option<&dyn TurnPolicyV5>,
    options: RunEpisodeOptionsV5,
) -> Result<EpisodeExecutionV5, RunnerError> {
    if provider.is_some() == turn_policy.is_some() {
        return Err(RunnerError::InvalidInput(
            "supply exactly one of provider or turn_policy".into(),
        ));
    }
    let config = options.run_config;
    config.validate()?;
    if options.phase2_reset_state.is_some() && config.wave_execution_mode != "ORACLE_PHASE_RESET" {
        return Err(RunnerError::InvalidInput(
            "phase2_reset_state requires wave_execution_mode='ORACLE_PHASE_RESET'".into(),
        ));
    }
    if config.wave_execution_mode == "ORACLE_PHASE_RESET" && options.phase2_reset_state.is_none() {
        return Err(RunnerError::InvalidInput(
            "ORACLE_PHASE_RESET requires an explicit phase2_reset_state".into(),
        ));
    }

    let profile = config.resolved_topology_profile()?;
    let topology = profile.canonical_topology.as_str().to_owned();
    let diagnostic_enabled = matches!(
        topology.as_str(),
        "T3_MAKER_CHECKER_GUARDRAIL" | "T5_HIERARCHICAL_PIPELINE"
    ) || config.steering_mode != "UNSTEERED";
    let diagnostic_service = match options.diagnostic_oracle.as_ref() {
        Some(oracle) => Some(make_v5_diagnostic_runtime_factory(episode, oracle)?),
        None if diagnostic_enabled => {
            return Err(RunnerError::InvalidInput(
                "diagnostic_oracle is required for checker/steering-enabled runs".to_owned(),
            ));
        }
        None => None,
    };
    let host_world = options
        .runtime_initial_world
        .clone()
        .unwrap_or_else(|| initial_world.clone());
    let mut runtime = RuntimeV5::with_options(
        episode.clone(),
        host_world,
        RuntimeOptionsV5 {
            topology: topology.clone(),
            steering_mode: config.steering_mode.clone(),
            wave_execution_mode: config.wave_execution_mode.clone(),
            phase2_reset_state: options.phase2_reset_state.clone(),
            phase2_environment_state: None,
            diagnostic_service,
        },
    )?;
    let run_cfg_sha = run_config_sha(&config, &profile)?;
    let attempt_id = format!(
        "att_{}_r{}_{}",
        episode.episode_id,
        config.repeat_index,
        &run_cfg_sha[..12]
    );
    let keep_transcript = options
        .capture_transcript
        .unwrap_or(config.capture_transcript);
    let mut counters = RunCounters::default();
    let mut messages = vec![Message::user(render_v5_episode_opening(episode))];
    let mut auditor_messages = messages.clone();
    let is_t5 = topology == "T5_HIERARCHICAL_PIPELINE";
    let mut t5 = T5State::default();
    let mut status = AttemptStatusV5::Completed;
    let mut terminal_cause = Some("EPISODE_COMPLETED".to_owned());
    let mut context_limit_cause = None;
    let mut terminal_ordinal = None;
    let mut error_message = None;
    let started = Instant::now();
    let seq = Arc::new(AtomicU64::new(0));
    let mut agent_local_seqs = HashMap::<String, i64>::new();
    let mut dispatch_round = 0_u64;
    let mut terminal_latched = false;
    let mut terminal_instant: Option<Instant> = None;
    let mut provider_accounting = accounting_traces(provider.as_ref(), &config)?;
    for trace in &mut provider_accounting {
        if let Some(input) = provider.as_ref() {
            let role = if trace.role == "unified" {
                "monolith"
            } else {
                trace.role.as_str()
            };
            if !input.for_role(role)?.capabilities().reports_usage {
                trace.status = UsageAccountingStatusV5::Unknown;
                trace.basis = "provider_does_not_report_usage".to_owned();
            }
        }
    }

    if let Some(provider_input) = provider.as_ref() {
        if !config.is_segment_continuation_eligible() {
            status = AttemptStatusV5::IncompleteContextIncompatible;
            terminal_cause = Some("SEGMENT_CONTINUATION_CAPABILITY_INCOMPATIBLE".into());
            error_message = Some(
                "segment_continuation_capability is incompatible with configured ceiling".into(),
            );
            terminal_latched = true;
            terminal_instant = Some(Instant::now());
        } else {
            let check = preflight_obs_strategy_witness_v5(episode, &config, None)?;
            if !check.compatible {
                status = AttemptStatusV5::IncompleteContextIncompatible;
                terminal_cause = Some("OBS_STRATEGY_WITNESS_PREFLIGHT_INCOMPATIBLE".into());
                error_message = Some(check.failure_reasons.join("; ").chars().take(240).collect());
                terminal_latched = true;
                terminal_instant = Some(Instant::now());
            }
            // The injected object is intentionally not consulted for account credentials here.
            let _ = provider_input;
        }
    }

    if let Some(provider_input) = provider.as_ref() {
        if !config.is_segment_continuation_eligible() {
            status = AttemptStatusV5::IncompleteContextIncompatible;
            terminal_cause = Some("SEGMENT_CONTINUATION_CAPABILITY_INCOMPATIBLE".into());
            error_message = Some(
                "segment_continuation_capability is incompatible with configured ceiling".into(),
            );
            terminal_latched = true;
            terminal_instant = Some(Instant::now());
        } else {
            let check = preflight_obs_strategy_witness_v5(episode, &config, None)?;
            if !check.compatible {
                status = AttemptStatusV5::IncompleteContextIncompatible;
                terminal_cause = Some("OBS_STRATEGY_WITNESS_PREFLIGHT_INCOMPATIBLE".into());
                error_message = Some(check.failure_reasons.join("; ").chars().take(240).collect());
                terminal_latched = true;
                terminal_instant = Some(Instant::now());
            }
            let _ = provider_input;
        }
    }

    while !terminal_latched {
        let remaining_slots = config
            .effective_max_turns()
            .saturating_sub(counters.completed_generations.max(0) as u64);
        if remaining_slots == 0 {
            status = AttemptStatusV5::WithinProtocolStepLimit;
            terminal_cause = Some("GENERATION_CAP_EXHAUSTED".into());
            terminal_instant = Some(Instant::now());
            break;
        }

        if let Some(policy) = turn_policy {
            let role = profile
                .roles
                .first()
                .map_or("monolith".to_owned(), |r| r.role_id.clone());
            let role_spec = profile
                .role_spec(&role)
                .map_err(|e| RunnerError::Schema(e.to_string()))?;
            let seq_no = agent_local_seqs.get("baseline").copied().unwrap_or(0);
            agent_local_seqs.insert("baseline".to_owned(), seq_no + 1);
            let ordinal = (
                dispatch_round as i64,
                profile.topology_priority,
                role_spec.role_priority,
                "baseline".to_owned(),
                seq_no,
            );
            let t0 = started.elapsed().as_millis().min(i64::MAX as u128) as i64;
            let calls = policy.next_turn(&turn_context(episode, &runtime, dispatch_round));
            counters.completed_generations += 1;
            runtime.set_usage_totals(0, 0, 0, 0)?;
            if calls.is_empty() {
                let t1 = started.elapsed().as_millis().min(i64::MAX as u128) as i64;
                counters.active_intervals_ms.push((t0, t1));
                status = AttemptStatusV5::WithinProtocolStepLimit;
                terminal_cause = Some("SCRIPTED_NOOP_EMPTY_CALLS".into());
                error_message = Some("scripted policy produced no tool calls".into());
                terminal_ordinal = Some(ordinal);
                terminal_instant = Some(Instant::now());
                break;
            }
            counters.requested += calls.len() as i64;
            counters.max_calls = counters.max_calls.max(calls.len() as i64);
            if calls.len() as u64 > config.max_calls_per_turn {
                status = AttemptStatusV5::WithinProtocolParseFailure;
                terminal_cause = Some("TOOL_CALL_LIMIT_EXCEEDED".into());
                error_message = Some(HARNESS_MSG_INVALID_ENVELOPE_V5.to_owned());
                terminal_ordinal = Some(ordinal);
                terminal_instant = Some(Instant::now());
                break;
            }
            let permitted = profile
                .permitted_tools_for_role(&role, &config.steering_mode)
                .map_err(|e| RunnerError::Schema(e.to_string()))?;
            let mut completed_here = false;
            for call in &calls {
                let result = if completed_here {
                    json!({"status":"EPISODE_ALREADY_COMPLETED","error_detail":HARNESS_MSG_EPISODE_ALREADY_COMPLETED_V5})
                } else {
                    let value =
                        dispatch_tool(&mut runtime, call, &permitted, "baseline", &config, true)?;
                    counters.executed += 1;
                    completed_here = value.get("status") == Some(&json!("EPISODE_COMPLETED"))
                        || runtime.episode_completed();
                    value
                };
                if keep_transcript {
                    counters.tool_trace.push(ToolTraceV5 {
                        turn_index: dispatch_round as i64,
                        tool_name: call.name.clone(),
                        arguments: Value::Object(call.arguments.clone()),
                        result,
                    });
                }
            }
            let t1 = started.elapsed().as_millis().min(i64::MAX as u128) as i64;
            counters.active_intervals_ms.push((t0, t1));
            if completed_here || runtime.episode_completed() {
                status = AttemptStatusV5::Completed;
                terminal_cause = Some("EPISODE_COMPLETED".into());
                terminal_ordinal = Some(ordinal);
                terminal_latched = true;
                terminal_instant = Some(Instant::now());
            }
            dispatch_round += 1;
            continue;
        }

        let provider_input = provider.as_ref().expect("provider mode validated");
        let phase = runtime.phase_index().max(1) as u8;
        let mut ready_specs = if let Some(scheduler) = options.round_scheduler.as_ref() {
            scheduler(dispatch_round, &runtime, &config)
        } else if is_t5 {
            let next_role = if let Some(target) = t5.revision_target.take() {
                target
            } else if !t5.planned.get(&(phase as i64)).copied().unwrap_or(false) {
                "planner".to_owned()
            } else if t5.active_stage.is_some() && t5.active_stage != t5.approved_stage {
                "auditor".to_owned()
            } else {
                "executor".to_owned()
            };
            if next_role == "auditor" {
                if let Some((phase_idx, stage_id, stage_digest)) = t5.active_stage.as_ref() {
                    let cue = format!(
                        "[Auditor review required] Phase {phase_idx} active STAGED buffer stage_id={stage_id} stage_digest={stage_digest}. Inspect the staged mutations and complete transcript; you may invoke run_phase_checker once per phase and must return a JSON approval or revision decision."
                    );
                    if auditor_messages.last().is_none_or(|m| m.content != cue) {
                        auditor_messages.push(Message::user(cue));
                    }
                }
            }
            let request_messages = if next_role == "auditor" {
                &auditor_messages
            } else {
                &messages
            };
            vec![RoundRequestSpecV5 {
                role: next_role.clone(),
                agent_id: next_role,
                messages: request_messages.clone(),
                phase_index: phase,
                provider: None,
            }]
        } else {
            let role = profile
                .roles
                .first()
                .map_or("monolith".to_owned(), |r| r.role_id.clone());
            vec![RoundRequestSpecV5 {
                role: role.clone(),
                agent_id: role,
                messages: messages.clone(),
                phase_index: phase,
                provider: None,
            }]
        };
        if ready_specs.is_empty() {
            status = AttemptStatusV5::IncompleteHarnessFailure;
            terminal_cause = Some("EMPTY_DISPATCH_ROUND".into());
            error_message = Some("round scheduler returned no requests".into());
            terminal_instant = Some(Instant::now());
            break;
        }
        let mut turns = run_provider_round(
            provider_input,
            &config,
            &profile,
            std::mem::take(&mut ready_specs),
            remaining_slots,
            dispatch_round,
            &attempt_id,
            &seq,
            started,
            &mut agent_local_seqs,
        )
        .await?;
        for mut turn in turns.drain(..) {
            if terminal_latched {
                mark_post_terminal(&mut turn, &mut counters, &mut provider_accounting);
                continue;
            }
            if let Some(terminal) = process_provider_turn(
                turn,
                &mut runtime,
                &config,
                &profile,
                &mut t5,
                &mut counters,
                &mut provider_accounting,
                &mut messages,
                &mut auditor_messages,
                keep_transcript,
                started,
                is_t5,
            )? {
                status = terminal.status;
                terminal_cause = terminal.cause;
                context_limit_cause = terminal.context_limit_cause;
                error_message = terminal.error_message;
                terminal_ordinal = terminal.ordinal;
                terminal_latched = true;
                terminal_instant = Some(Instant::now());
            }
        }
        if !terminal_latched {
            if counters.completed_generations as u64 >= config.effective_max_turns() {
                status = AttemptStatusV5::WithinProtocolStepLimit;
                terminal_cause = Some("GENERATION_CAP_EXHAUSTED".into());
                terminal_latched = true;
                terminal_instant = Some(Instant::now());
            } else {
                dispatch_round += 1;
            }
        }
    }
    let elapsed_ms = started.elapsed().as_millis().min(i64::MAX as u128) as i64;
    let terminal_elapsed_ms = terminal_instant
        .map(|end| {
            end.saturating_duration_since(started)
                .as_millis()
                .min(i64::MAX as u128) as i64
        })
        .unwrap_or(elapsed_ms);
    let active_ms = union_intervals(&counters.active_intervals_ms);
    let phase1 = phase_trajectory(&runtime, 1);
    let phase2 = phase_trajectory(&runtime, 2);
    let (provider_kind, provider_model, endpoint) = if let Some(provider) = provider.as_ref() {
        let roles = provider.role_names(&config)?;
        let mut kinds = Vec::new();
        let mut models = Vec::with_capacity(roles.len());
        let mut endpoints = Vec::with_capacity(roles.len());
        for role in roles {
            let p = provider.for_role(if role == "unified" { "monolith" } else { &role })?;
            if !kinds.iter().any(|kind: &String| kind == p.kind()) {
                kinds.push(p.kind().to_owned());
            }
            models.push(format!("{role}={}", p.model()));
            endpoints.push(format!("{role}={}", p.endpoint_identity()));
        }
        (kinds.join("+"), models.join(";"), endpoints.join(";"))
    } else {
        (
            "baseline".to_owned(),
            "scripted".to_owned(),
            "injected-baseline".to_owned(),
        )
    };
    let accounting_status = if provider.is_none() {
        UsageAccountingStatusV5::NotApplicable
    } else if provider_accounting
        .iter()
        .all(|t| t.status == UsageAccountingStatusV5::Attested)
    {
        UsageAccountingStatusV5::Attested
    } else if provider_accounting
        .iter()
        .all(|t| t.status == UsageAccountingStatusV5::ProviderDeclared)
    {
        UsageAccountingStatusV5::ProviderDeclared
    } else {
        UsageAccountingStatusV5::Unknown
    };
    let usage_provenance_complete = provider.is_none()
        || (matches!(
            accounting_status,
            UsageAccountingStatusV5::Attested | UsageAccountingStatusV5::ProviderDeclared
        ) && !provider_accounting.is_empty()
            && !counters.usage_problem_detected
            && !counters.provider_error_detected);
    let usage_available = provider.is_none()
        || provider
            .as_ref()
            .unwrap()
            .role_names(&config)?
            .iter()
            .all(|role| {
                provider
                    .as_ref()
                    .unwrap()
                    .for_role(role)
                    .is_ok_and(|p| p.capabilities().reports_usage)
            });
    let usage_valid = usage_provenance_complete && usage_available;
    let totals = UsageTotalsV5 {
        input_tokens_uncached: counters.usage.input_tokens.min(i64::MAX as u64) as i64,
        cached_input_tokens: counters
            .usage
            .cache_read_tokens
            .saturating_add(counters.usage.cache_write_tokens)
            .min(i64::MAX as u64) as i64,
        output_tokens: counters.usage.output_tokens.min(i64::MAX as u64) as i64,
        reasoning_tokens: counters.usage.reasoning_tokens.min(i64::MAX as u64) as i64,
    };
    let costs = if usage_valid {
        config
            .scoring_pricing()
            .and_then(|pricing| compute_cost(&totals, &pricing))
    } else {
        None
    };
    let provider_cost = if counters.completed_generations > 0
        && counters.reported_cost_count == counters.completed_generations
    {
        Some(counters.reported_cost_sum)
    } else {
        None
    };
    let provider_error = matches!(
        status,
        AttemptStatusV5::IncompleteProviderQuota
            | AttemptStatusV5::IncompleteProviderFailure
            | AttemptStatusV5::IncompleteContextIncompatible
            | AttemptStatusV5::IncompleteHarnessFailure
    );
    let parse_failure = status == AttemptStatusV5::WithinProtocolParseFailure;
    let retry_request_count = counters
        .provider_requests
        .iter()
        .filter(|r| r.request_kind == ib_v5_schemas::ProviderRequestKindV5::TransientRetry)
        .count() as i64;
    let mut execution = EpisodeExecutionV5 {
        episode_id: episode.episode_id.clone(),
        suite_id: suite_id(episode.suite_id),
        family_id: episode.family_id.clone(),
        model_id: options
            .model_id
            .unwrap_or_else(|| format!("{provider_kind}:{provider_model}")),
        provider_kind,
        endpoint_identity: endpoint,
        topology_id: topology,
        steering_mode: config.steering_mode.clone(),
        wave_execution_mode: config.wave_execution_mode.clone(),
        effort: config.effort.clone(),
        execution_valid: !provider_error && !parse_failure,
        provider_error,
        parse_failure,
        output_budget_exceeded: if status == AttemptStatusV5::WithinProtocolOutputLimit {
            Some(true)
        } else if provider.is_none() || usage_valid {
            Some(false)
        } else {
            None
        },
        usage_provenance_complete,
        termination_reason: termination_reason(
            status,
            terminal_cause.as_deref() == Some("SCRIPTED_NOOP_EMPTY_CALLS"),
        ),
        error_message,
        turns_used: counters.completed_generations,
        tool_calls_requested: counters.requested,
        tool_calls_executed: counters.executed,
        max_calls_in_turn: counters.max_calls,
        latency_s: counters.latency_s,
        input_tokens_uncached: usage_valid.then_some(totals.input_tokens_uncached),
        cached_input_tokens: usage_valid.then_some(totals.cached_input_tokens),
        input_tokens_actual: usage_valid.then_some(totals.input_tokens_actual()),
        output_tokens: usage_valid.then_some(totals.output_tokens),
        reasoning_tokens: usage_valid.then_some(totals.reasoning_tokens),
        output_tokens_actual: usage_valid.then_some(totals.output_tokens_actual()),
        cost_usd_micros: costs,
        provider_reported_cost_usd: provider_cost,
        pricing_manifest_sha256: config
            .pricing
            .as_ref()
            .map(|p| p.pricing_manifest_sha256.clone()),
        phase1_committed: phase1.commit.is_some(),
        phase2_opened: runtime.phase2_opened(),
        phase2_committed: phase2.commit.is_some(),
        phase2_state_feasible: None,
        phase1,
        phase2,
        completion_trace: counters.completion_trace,
        tool_trace: counters.tool_trace,
        score: None,
        scoring_input: None,
        usage_accounting_policy_id: provider
            .is_some()
            .then(|| V5_USAGE_ACCOUNTING_POLICY_ID.to_owned()),
        usage_accounting_status: accounting_status,
        provider_accounting,
        attempt: None,
        provider_requests: counters.provider_requests.clone(),
        attempt_status: status,
        terminal_cause: terminal_cause.clone(),
        context_limit_cause: context_limit_cause.clone(),
        terminal_reservation_ordinal: terminal_ordinal.clone(),
        completed_assistant_generations: counters.completed_generations,
        retry_request_count,
        elapsed_wall_time_ms: Some(elapsed_ms),
        terminal_outcome_elapsed_ms: Some(terminal_elapsed_ms),
        active_elapsed_ms: Some(active_ms),
        primary_prompt_bundle_sha256: Some(PRIMARY_PROMPT_BUNDLE_SHA256_V5.to_owned()),
        topology_profile_sha256: Some(profile.topology_profile_sha256.clone()),
        repeat_index: Some(config.repeat_index as i64),
        seed_index: None,
    };
    let (g_srr, sem_srr_commit, overgrant_count, avail_breach_count) = if status.is_incomplete() {
        (None, None, None, None)
    } else if status.is_within_protocol_terminated() {
        (Some(false), Some(false), Some(0), Some(0))
    } else if let Some(oracle) = options.diagnostic_oracle.as_ref() {
        match ib_v5_scoring::score_execution_v5(
            &execution,
            episode,
            oracle,
            ib_v5_scoring::ScoreExecutionOptionsV5::default(),
        ) {
            Ok((_input, score)) => (
                score.g_srr_commit.map(|value| value != 0),
                score.sem_srr_commit.map(|value| value != 0),
                score.safety_commit_overgrant_count,
                score.safety_commit_availability_breach_count,
            ),
            Err(_) => (Some(false), Some(false), Some(0), Some(0)),
        }
    } else {
        (Some(false), Some(false), Some(0), Some(0))
    };
    execution.attempt = Some(EpisodeAttemptV5 {
        attempt_id,
        episode_id: episode.episode_id.clone(),
        repeat_index: config.repeat_index as i64,
        run_config_sha256: run_cfg_sha,
        attempt_status: status,
        completed_assistant_generations: counters.completed_generations,
        terminal_reservation_ordinal: terminal_ordinal,
        terminal_cause,
        context_limit_cause,
        tool_call_count: counters.requested,
        retry_request_count,
        elapsed_wall_time_ms: Some(elapsed_ms),
        terminal_outcome_elapsed_ms: Some(terminal_elapsed_ms),
        active_elapsed_ms: Some(active_ms),
        provider_requests: counters.provider_requests.clone(),
        g_srr,
        sem_srr_commit,
        overgrant_count,
        avail_breach_count,
    });
    Ok(execution)
}

fn turn_context(episode: &EpisodeV5, runtime: &RuntimeV5, turn_index: u64) -> AgentTurnContextV5 {
    let view = runtime.view().clone();
    let delivered = view.all_delivered_slices();
    let ids = view
        .phase_delivered_slices()
        .iter()
        .map(|s| s.slice_id.clone())
        .collect();
    AgentTurnContextV5 {
        episode_id: episode.episode_id.clone(),
        policy_card: episode.policy_card.clone(),
        phase_index: runtime.phase_index(),
        manifest: runtime.manifest().clone(),
        view,
        delivered_slices: delivered,
        delivered_slice_ids: ids,
        turn_index,
    }
}

fn phase_trajectory(runtime: &RuntimeV5, phase_index: i64) -> PhaseTrajectoryV5 {
    let snapshots = runtime
        .snapshots()
        .iter()
        .filter(|s| s.phase_index == phase_index)
        .map(TrajectorySnapshotV5::from_runtime_snapshot)
        .collect();
    let set = runtime.phase_snapshots(phase_index);
    PhaseTrajectoryV5 {
        phase_index,
        attempts: snapshots,
        stage_attempts: set
            .map(|s| {
                s.stage_attempts
                    .iter()
                    .map(TrajectorySnapshotV5::from_runtime_snapshot)
                    .collect()
            })
            .unwrap_or_default(),
        first_stage: set
            .and_then(|s| s.first_stage.as_ref())
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
        last_stage: set
            .and_then(|s| s.last_stage.as_ref())
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
        commit: set
            .and_then(|s| s.commit.as_ref())
            .map(TrajectorySnapshotV5::from_runtime_snapshot),
    }
}

#[derive(Debug)]
struct EnvelopeErrorV5 {
    code: &'static str,
    call_count: usize,
}

fn extract_and_validate_calls(
    response: &ModelResponse,
    phase_index: u8,
    role: &str,
    config: &RunConfigV5,
    native_tools: bool,
    logical_request_id: &str,
) -> Result<Vec<ToolCall>, EnvelopeErrorV5> {
    let invalid = |call_count| EnvelopeErrorV5 {
        code: "INVALID_TOOL_CALL_ENVELOPE",
        call_count,
    };
    let permitted = tool_specs_for_role(config, role, Some(phase_index)).map_err(|_| invalid(0))?;
    let declared: BTreeSet<&str> = permitted.iter().map(|t| t.name.as_str()).collect();
    let mut calls = response.tool_calls.clone();
    if !calls.is_empty()
        && response
            .raw
            .as_ref()
            .and_then(|raw| raw.get("malformed_tool_call_envelope"))
            == Some(&Value::Bool(true))
    {
        return Err(invalid(calls.len()));
    }
    if calls.is_empty() {
        let value = response.parsed.clone().map(Value::Object).or_else(|| {
            response.text.as_ref().and_then(|text| {
                let body = strip_markdown_fences(&strip_reasoning(text));
                serde_json::from_str::<Value>(&body).ok().or_else(|| {
                    extract_json_object(
                        Some(&body),
                        Some(&["actions"]),
                        Some(&[
                            "actions",
                            "tool_calls",
                            "decision",
                            "target_role",
                            "feedback",
                        ]),
                    )
                    .map(Value::Object)
                })
            })
        });
        let Some(Value::Object(object)) = value else {
            if response
                .text
                .as_ref()
                .is_some_and(|text| text.trim_start().starts_with('{'))
                || response
                    .raw
                    .as_ref()
                    .and_then(|raw| raw.get("structured_output_expected"))
                    == Some(&Value::Bool(true))
                || (!native_tools
                    && response.text.as_ref().is_some_and(|text| {
                        text.contains("\"actions\"")
                            || text.to_ascii_lowercase().contains("```json")
                    }))
            {
                return Err(invalid(0));
            }
            return Ok(Vec::new());
        };
        let raw_actions = object.get("actions");
        let raw_tool_calls = object.get("tool_calls");
        let has_actions_key = raw_actions.is_some() || raw_tool_calls.is_some();
        let is_auditor_verdict = raw_actions.is_none_or(Value::is_null)
            && raw_tool_calls.is_none_or(Value::is_null)
            && (object.get("decision").is_some_and(|v| !v.is_null())
                || object.get("approved").is_some_and(|v| !v.is_null()));
        let actions_value = if object.contains_key("actions") {
            raw_actions
        } else {
            raw_tool_calls
        };
        if has_actions_key && !is_auditor_verdict {
            let Some(actions) = actions_value else {
                return Err(invalid(0));
            };
            let Some(actions) = actions.as_array() else {
                return Err(invalid(0));
            };
            for (index, item) in actions.iter().enumerate() {
                let Some(action) = item.as_object() else {
                    return Err(invalid(actions.len()));
                };
                let Some(name) = action
                    .get("action")
                    .or_else(|| action.get("name"))
                    .and_then(Value::as_str)
                    .filter(|name| !name.is_empty())
                else {
                    return Err(invalid(actions.len()));
                };
                if action
                    .get("id")
                    .is_some_and(|id| !id.is_null() && !id.is_string())
                {
                    return Err(invalid(actions.len()));
                }
                let raw_args = action
                    .get("arguments")
                    .filter(|value| !value.is_null())
                    .or_else(|| action.get("arguments_json"));
                let args = match raw_args {
                    Some(Value::Object(map)) => map.clone(),
                    Some(Value::String(text)) => serde_json::from_str::<Map<String, Value>>(text)
                        .map_err(|_| invalid(actions.len()))?,
                    None => Map::new(),
                    Some(Value::Null) => Map::new(),
                    _ => return Err(invalid(actions.len())),
                };
                let id = action
                    .get("id")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("{logical_request_id}:call:{index}"));
                calls.push(ToolCall {
                    id,
                    name: name.to_owned(),
                    arguments: args,
                });
            }
        }
    }
    if calls.len() > config.max_calls_per_turn as usize {
        return Err(EnvelopeErrorV5 {
            code: "TOOL_CALL_LIMIT_EXCEEDED",
            call_count: calls.len(),
        });
    }
    let mut seen = BTreeSet::new();
    for (index, call) in calls.iter_mut().enumerate() {
        if !declared.contains(call.name.as_str()) {
            return Err(invalid(calls.len()));
        }
        if call.id.is_empty() && native_tools {
            return Err(invalid(calls.len()));
        }
        if call.id.is_empty() {
            call.id = format!("{logical_request_id}:call:{index}");
        }
        if !seen.insert(call.id.clone()) {
            return Err(invalid(calls.len()));
        }
    }
    Ok(calls)
}

fn dispatch_tool(
    runtime: &mut RuntimeV5,
    call: &ToolCall,
    permitted: &BTreeSet<String>,
    role: &str,
    config: &RunConfigV5,
    t5_auditor_approved: bool,
) -> Result<Value, RunnerError> {
    if !permitted.contains(&call.name) {
        return Ok(json!({"status":"TOOL_NOT_PERMITTED_IN_TOPOLOGY"}));
    }
    if config.uses_specialized_roles()?
        && role == "executor"
        && matches!(
            call.name.as_str(),
            "request_cosign" | "commit_phase_mutations"
        )
        && !t5_auditor_approved
    {
        return Ok(json!({"status":"AUDITOR_APPROVAL_REQUIRED"}));
    }
    let args = &call.arguments;
    let phase = args
        .get("phase_index")
        .and_then(Value::as_i64)
        .filter(|p| matches!(p, 1 | 2));
    let Some(phase) = phase else {
        return Ok(schema_error(&call.name, &call.id));
    };
    let allowed: BTreeSet<&str> = match call.name.as_str() {
        "query_sources" => ["phase_index", "slice_ids"].into_iter().collect(),
        "stage_phase_mutations" => ["phase_index", "stage_id", "mutations"]
            .into_iter()
            .collect(),
        "request_cosign" => [
            "phase_index",
            "stage_id",
            "scope_kind",
            "target_entity_id",
            "actor_account_id",
        ]
        .into_iter()
        .collect(),
        "commit_phase_mutations" | "run_phase_checker" => {
            ["phase_index", "stage_id"].into_iter().collect()
        }
        "request_team_steer" => ["phase_index", "stage_id", "trigger_reason"]
            .into_iter()
            .collect(),
        _ => return Ok(json!({"status":"TOOL_NOT_IMPLEMENTED"})),
    };
    if args.keys().any(|key| !allowed.contains(key.as_str())) {
        return Ok(schema_error(&call.name, &call.id));
    }
    let str_arg = |key: &str| {
        args.get(key)
            .and_then(Value::as_str)
            .ok_or_else(|| schema_error(&call.name, &call.id))
    };
    let parsed: Result<Value, RunnerError> = match call.name.as_str() {
        "query_sources" => {
            let ids = args.get("slice_ids").and_then(Value::as_array);
            if let Some(ids) = ids {
                if ids.iter().all(Value::is_string) {
                    let ids: Vec<String> = ids
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect();
                    Ok(serde_json::to_value(runtime.query_sources(phase, &ids)?)?)
                } else {
                    return Ok(schema_error(&call.name, &call.id));
                }
            } else {
                return Ok(schema_error(&call.name, &call.id));
            }
        }
        "stage_phase_mutations" => {
            let stage = match str_arg("stage_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            let Some(values) = args.get("mutations").and_then(Value::as_array) else {
                return Ok(schema_error(&call.name, &call.id));
            };
            let mutations: Result<Vec<AtomicMutationV5>, _> =
                values.iter().cloned().map(serde_json::from_value).collect();
            let Ok(mutations) = mutations else {
                return Ok(schema_error(&call.name, &call.id));
            };
            Ok(serde_json::to_value(
                runtime.stage_phase_mutations(phase, stage, &mutations)?,
            )?)
        }
        "request_cosign" => {
            let stage = match str_arg("stage_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            let kind: CoSignScopeKindV5 = match args
                .get("scope_kind")
                .cloned()
                .ok_or_else(|| RunnerError::InvalidInput("missing cosign scope".into()))
                .and_then(|v| serde_json::from_value(v).map_err(RunnerError::from))
            {
                Ok(v) => v,
                Err(_) => return Ok(schema_error(&call.name, &call.id)),
            };
            let target = match str_arg("target_entity_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            let actor = match str_arg("actor_account_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            Ok(serde_json::to_value(
                runtime.request_cosign(phase, stage, kind, target, actor),
            )?)
        }
        "commit_phase_mutations" => {
            let stage = match str_arg("stage_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            Ok(serde_json::to_value(
                runtime.commit_phase_mutations(phase, stage)?,
            )?)
        }
        "run_phase_checker" => {
            let stage = match str_arg("stage_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            Ok(serde_json::to_value(
                runtime.run_phase_checker(phase, stage)?,
            )?)
        }
        "request_team_steer" => {
            let stage = match str_arg("stage_id") {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            let reason: SteerTriggerReasonV5 = match args
                .get("trigger_reason")
                .cloned()
                .ok_or_else(|| RunnerError::InvalidInput("missing steering reason".into()))
                .and_then(|v| serde_json::from_value(v).map_err(RunnerError::from))
            {
                Ok(v) => v,
                Err(_) => return Ok(schema_error(&call.name, &call.id)),
            };
            Ok(serde_json::to_value(
                runtime.request_team_steer(phase, stage, reason)?,
            )?)
        }
        _ => Ok(json!({"status":"TOOL_NOT_IMPLEMENTED"})),
    };
    match parsed {
        Ok(value) => Ok(value),
        Err(error @ RunnerError::Runtime(_)) => Err(error),
        Err(error @ RunnerError::Schema(_)) => Err(error),
        Err(
            RunnerError::Json(_) | RunnerError::InvalidInput(_) | RunnerError::Configuration(_),
        ) => Ok(schema_error(&call.name, &call.id)),
        Err(error) => Err(error),
    }
}

fn schema_error(name: &str, call_id: &str) -> Value {
    if matches!(
        name,
        "stage_phase_mutations" | "request_cosign" | "run_phase_checker" | "request_team_steer"
    ) {
        json!({"status":"REJECTED_STRUCTURAL","error_detail":"invalid tool arguments"})
    } else {
        json!({"status":"TOOL_ARGUMENT_SCHEMA_ERROR","tool_call_id":call_id,"error_detail":HARNESS_MSG_TOOL_SCHEMA_ERROR_V5})
    }
}

fn strip_markdown_fences(text: &str) -> String {
    let body = text.trim();
    if !body.starts_with("```") {
        return body.to_owned();
    }
    let after_open = body.find('\n').map_or("", |index| &body[index + 1..]);
    let Some(close) = after_open.rfind("```") else {
        return after_open.trim().to_owned();
    };
    after_open[..close].trim().to_owned()
}

fn push_completion(
    counters: &mut RunCounters,
    turn: u64,
    role: &str,
    provider: &dyn Provider,
    response: &ModelResponse,
    calls: usize,
) {
    let u = response.usage;
    let usage_valid = usage_problem(response).is_none();
    counters.completion_trace.push(CompletionTraceV5 {
        turn_index: turn as i64,
        role: role.to_owned(),
        requested_model_id: provider.model().to_owned(),
        served_model_id: response.served_model.clone(),
        input_tokens_uncached: if usage_valid {
            u.input_tokens as i64
        } else {
            0
        },
        cache_read_tokens: if usage_valid {
            u.cache_read_tokens as i64
        } else {
            0
        },
        cache_write_tokens: if usage_valid {
            u.cache_write_tokens as i64
        } else {
            0
        },
        output_tokens: if usage_valid {
            u.output_tokens as i64
        } else {
            0
        },
        reasoning_tokens: if usage_valid {
            u.reasoning_tokens as i64
        } else {
            0
        },
        latency_s: response.latency_s,
        provider_reported_cost_usd: response.reported_cost_usd,
        stop_reason: response.stop_reason.clone(),
        response_error: response.error.is_some(),
        refused: response.refused,
        tool_calls_returned: calls as i64,
        usage_valid,
    });
}

fn accounting_traces(
    provider: Option<&ProviderInputV5>,
    config: &RunConfigV5,
) -> Result<Vec<ProviderAccountingTraceV5>, RunnerError> {
    let Some(provider) = provider else {
        return Ok(vec![]);
    };
    let roles = provider.role_names(config)?;
    let is_t0 = config.runtime_topology()? == "T0_SINGLE_MONOLITH";
    let active_roles: BTreeSet<String> = roles.iter().cloned().collect();
    let mut evidence_roles: BTreeSet<String> = config
        .usage_accounting_evidence
        .iter()
        .map(|item| item.role.as_str().to_owned())
        .collect();
    if is_t0 && evidence_roles.contains("unified") && !evidence_roles.contains("monolith") {
        evidence_roles.remove("unified");
        evidence_roles.insert("monolith".to_owned());
    }
    let evidence_roles_match = evidence_roles == active_roles;
    let mut out = Vec::with_capacity(roles.len());
    for role in roles {
        let p = provider.for_role(&role)?;
        let trace_role = if role == "monolith" { "unified" } else { &role };
        let account_role = match trace_role {
            "unified" => Some(ProviderRoleV5::Unified),
            "planner" => Some(ProviderRoleV5::Planner),
            "executor" => Some(ProviderRoleV5::Executor),
            "auditor" => Some(ProviderRoleV5::Auditor),
            _ => None,
        };
        let endpoint = p.endpoint_identity();
        let fingerprint =
            provider_fingerprint_sha256_v5(trace_role, p.kind(), p.model(), &endpoint);
        let evidence = account_role.and_then(|expected_role| {
            config
                .usage_accounting_evidence
                .iter()
                .find(|e| e.role == expected_role)
        });
        let evidence_matches = evidence.is_some_and(|e| {
            evidence_roles_match
                && e.role.as_str() == trace_role
                && e.provider_kind == p.kind()
                && e.model_id == p.model()
                && e.provider_fingerprint_sha256 == fingerprint
                && e.aggregate_usage_scope == USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5
                && e.adapter_retry_limit == 0
        });
        let (module_name, class_name) = p.python_type_identity();
        let raw_retry_limit = p.local_retry_limit().map(i64::from);
        let identity = ProviderIdentityInfo {
            module_name: module_name.to_owned(),
            class_name: class_name.to_owned(),
            retry_attr_name: raw_retry_limit.map(|_| "_max_retries".to_owned()),
            retry_limit_value: raw_retry_limit,
            reports_usage: p.capabilities().reports_usage,
            usage_includes_retries: p.usage_includes_retries(),
            ..ProviderIdentityInfo::default()
        };
        let retry_contract = effective_local_retry_contract_v5(&identity);
        let retry_limit = retry_contract.as_ref().map(|contract| contract.max_retries);
        let (status, basis) = if evidence_matches && retry_limit == Some(0) {
            (
                UsageAccountingStatusV5::Attested,
                "operator_or_gateway_attestation",
            )
        } else if config.usage_accounting_evidence.is_empty() && p.usage_includes_retries() {
            (
                UsageAccountingStatusV5::ProviderDeclared,
                "provider_declared_usage_includes_retries",
            )
        } else {
            let basis = if evidence.is_some() && !evidence_roles_match {
                "evidence_role_set_mismatch"
            } else if evidence.is_some() && !evidence_matches {
                "evidence_identity_or_scope_mismatch"
            } else if retry_contract.is_none() {
                "effective_local_retry_config_unavailable"
            } else if retry_limit != Some(0) {
                "local_retries_enabled"
            } else {
                "aggregate_usage_attestation_missing"
            };
            (UsageAccountingStatusV5::Unknown, basis)
        };
        let retry_control_source = retry_contract
            .as_ref()
            .map(|c| c.control_source.clone())
            .unwrap_or_else(|| "unsupported_adapter".to_owned());
        let retry_control_source =
            if status == UsageAccountingStatusV5::ProviderDeclared && retry_contract.is_none() {
                basis.to_owned()
            } else {
                retry_control_source
            };
        out.push(ProviderAccountingTraceV5 {
            role: trace_role.to_owned(),
            provider_kind: p.kind().to_owned(),
            model_id: p.model().to_owned(),
            provider_fingerprint_sha256: fingerprint,
            local_retry_limit: retry_limit,
            local_retry_policy_id: retry_contract.as_ref().map(|c| c.policy_id.clone()),
            retry_control_source,
            evidence_sha256: evidence
                .filter(|_| evidence_matches)
                .map(|e| e.evidence_sha256.clone()),
            aggregate_usage_scope: evidence
                .filter(|_| evidence_matches)
                .map(|e| e.aggregate_usage_scope.clone()),
            status,
            basis: basis.to_owned(),
        });
    }
    Ok(out)
}

fn run_config_sha(
    config: &RunConfigV5,
    profile: &ib_v5_schemas::TopologyProfileV5,
) -> Result<String, RunnerError> {
    if let Some(digest) = &config.run_config_sha256 {
        return Ok(digest.clone());
    }
    if let Some(config) = &config.run_configuration {
        return Ok(config.run_config_sha256.clone());
    }
    let payload = json!({
        "topology":profile.canonical_topology.as_str(),
        "topology_profile_sha256":profile.topology_profile_sha256,
        "steering_mode":config.steering_mode,
        "wave_execution_mode":config.wave_execution_mode,
        "effort":config.effort,
        "effort_track":config.effort_track,
        "max_assistant_generations_per_episode":config.max_assistant_generations_per_episode,
        "primary_prompt_bundle_sha256":PRIMARY_PROMPT_BUNDLE_SHA256_V5,
        "cap_disclosed":false
    });
    let bytes =
        ib_v5_schemas::jcs_dumps_v5(&payload).map_err(|e| RunnerError::Schema(e.to_string()))?;
    Ok(ib_core::hashing::sha256_hex(bytes))
}

fn suite_id(suite: ib_v5_schemas::SuiteIdV5) -> String {
    match suite {
        ib_v5_schemas::SuiteIdV5::V5Dev => "v5_dev",
        ib_v5_schemas::SuiteIdV5::V5Lite => "v5_lite",
        ib_v5_schemas::SuiteIdV5::V5Core => "v5_core",
        ib_v5_schemas::SuiteIdV5::V5Full => "v5_full",
    }
    .to_owned()
}

fn termination_reason(status: AttemptStatusV5, scripted_noop: bool) -> String {
    if scripted_noop {
        return "NO_TOOL_CALL".to_owned();
    }
    match status {
        AttemptStatusV5::Completed => "EPISODE_COMPLETED",
        AttemptStatusV5::WithinProtocolRefusal => "REFUSAL",
        AttemptStatusV5::WithinProtocolParseFailure => "PARSE_FAILURE",
        AttemptStatusV5::WithinProtocolStepLimit => "MAX_TURNS_EXCEEDED",
        AttemptStatusV5::WithinProtocolOutputLimit => "OUTPUT_BUDGET_STOP",
        AttemptStatusV5::WithinProtocolContextLimit => "CONTEXT_LIMIT_EXCEEDED",
        _ => "PROVIDER_ERROR",
    }
    .to_owned()
}

fn compact_json(value: &Value) -> Result<String, RunnerError> {
    ib_core::pyjson::dumps(
        value,
        &ib_core::pyjson::DumpOptions {
            ensure_ascii: false,
            sort_keys: false,
            indent: None,
            separators: Some((",".into(), ":".into())),
            allow_nan: false,
        },
    )
    .map_err(|e| RunnerError::InvalidInput(e.to_string()))
}

fn union_intervals(intervals: &[(i64, i64)]) -> i64 {
    if intervals.is_empty() {
        return 0;
    }
    let mut sorted = intervals.to_vec();
    sorted.sort_unstable();
    let (mut a, mut b) = sorted[0];
    let mut total = 0_i64;
    for (start, end) in sorted.into_iter().skip(1) {
        if start <= b {
            b = b.max(end);
        } else {
            total = total.saturating_add(b.saturating_sub(a));
            a = start;
            b = end;
        }
    }
    total.saturating_add(b.saturating_sub(a))
}

fn add_latency(total: f64, addition: f64) -> f64 {
    let limit = f64::MAX / 2_000.0;
    if !addition.is_finite() || addition <= 0.0 {
        return total;
    }
    if total >= limit - addition {
        limit
    } else {
        total + addition
    }
}

fn compute_cost(totals: &UsageTotalsV5, rates: &ib_v5_scoring::PricingRatesV5) -> Option<i64> {
    let visible = (totals.output_tokens - totals.reasoning_tokens).max(0) as i128;
    let numerator = i128::from(totals.input_tokens_uncached)
        * i128::from(rates.uncached_input_micros_per_million)
        + i128::from(totals.cached_input_tokens)
            * i128::from(rates.cached_input_micros_per_million)
        + visible * i128::from(rates.output_micros_per_million)
        + i128::from(totals.reasoning_tokens) * i128::from(rates.reasoning_micros_per_million);
    i64::try_from((numerator + 500_000) / 1_000_000).ok()
}

#[cfg(test)]
mod parser_tests {
    use super::{RunConfigV5, extract_and_validate_calls};
    use ib_providers::base::ModelResponse;
    use serde_json::{Value, json};

    fn parsed_response(value: Value) -> ModelResponse {
        ModelResponse {
            parsed: Some(value.as_object().expect("object response").clone()),
            ..ModelResponse::default()
        }
    }

    #[test]
    fn null_actions_does_not_hide_non_null_tool_calls() {
        let response = parsed_response(json!({
            "actions": null,
            "tool_calls": [{"action":"query_sources","arguments":{}}],
            "decision": "APPROVE"
        }));
        let error = extract_and_validate_calls(
            &response,
            1,
            "monolith",
            &RunConfigV5::default(),
            true,
            "request",
        )
        .expect_err("conflicting action envelopes must be rejected");
        assert_eq!(error.code, "INVALID_TOOL_CALL_ENVELOPE");
    }

    #[test]
    fn null_arguments_fall_back_to_arguments_json() {
        let response = parsed_response(json!({
            "actions": [{
                "action": "query_sources",
                "arguments": null,
                "arguments_json": "{\"phase_index\":1}"
            }]
        }));
        let calls = extract_and_validate_calls(
            &response,
            1,
            "monolith",
            &RunConfigV5::default(),
            true,
            "request",
        )
        .expect("arguments_json fallback");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].arguments.get("phase_index"), Some(&json!(1)));
    }
}
