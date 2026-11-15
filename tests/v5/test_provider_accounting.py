from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from identitybench.v5.provider_accounting import (
    CANONICAL_ATTEMPT_STATUSES_V5,
    CANONICAL_PROVIDER_USAGE_POLICIES_V5,
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
    V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
    V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
    AdapterRetryAccountingContractV5,
    CurrencyConversionRateV5,
    FrozenPricingRateV5,
    ProviderUsageAccountingPolicyV5,
    UsageAccountingAttestationV5,
    UsageAccountingEvidenceV5,
    UsageAccountingManifestV5,
    build_provider_request_usage_v5,
    compute_active_elapsed_ms,
    compute_attempt_timings_ms_v5,
    derive_physical_segment_usage_from_cumulative_v5,
    effective_local_retry_contract_v5,
    estimate_frozen_cost_micros_v5,
    evaluate_g_econ_01_v5,
    get_provider_usage_policy_v5,
    normalize_raw_usage_v5,
    provider_fingerprint_sha256_v5,
    select_contributing_usage_records_v5,
    summarize_descriptive_usage_v5,
)
from identitybench.v5.schemas import EpisodeAttemptV5, ProviderRequestUsageV5

RUN_SHA = "1" * 64
PRICING_SHA = "2" * 64
RATE_SHA = "3" * 64


def _fingerprint(role: str = "unified") -> str:
    return provider_fingerprint_sha256_v5(
        role,
        "openai_compatible",
        "test-model",
        "a" * 16,
    )


def _build_req(
    *,
    request_id: str,
    logical_request_id: str = "lreq_1",
    request_kind: str = "INITIAL",
    retry_index: int | None = 0,
    segment_continuation_index: int | None = None,
    decode_segment_index: int = 0,
    attempt_id: str = "att_1",
    request_status: str = "COMPLETED",
    post_terminal: bool = False,
    raw_usage: dict[str, object] | None = None,
    policy: ProviderUsageAccountingPolicyV5 | None = None,
    usage_scope_override: str | None = None,
    provider_reported_cost_micros: int | None = None,
    currency_code: str | None = None,
    pricing_rate: FrozenPricingRateV5 | None = None,
) -> ProviderRequestUsageV5:
    return build_provider_request_usage_v5(
        request_id=request_id,
        logical_request_id=logical_request_id,
        request_kind=request_kind,  # type: ignore[arg-type]
        retry_index=retry_index,
        segment_continuation_index=segment_continuation_index,
        decode_segment_index=decode_segment_index,
        attempt_id=attempt_id,
        agent_id="unified",
        role="unified",
        dispatch_round=0,
        reservation_ordinal=(1, 1, 0, "unified", 0),
        request_status=request_status,  # type: ignore[arg-type]
        post_terminal=post_terminal,
        normalized_decode_status="TERMINAL_NORMAL",
        raw_usage=raw_usage,
        policy=policy or get_provider_usage_policy_v5("openai_responses"),
        usage_scope_override=usage_scope_override,  # type: ignore[arg-type]
        provider_reported_cost_micros=provider_reported_cost_micros,
        currency_code=currency_code,
        pricing_rate=pricing_rate,
    )


def _attempt(
    *,
    attempt_id: str = "att_1",
    episode_id: str = "ep_1",
    repeat_index: int = 1,
    attempt_status: str = "COMPLETED",
    provider_requests: tuple[ProviderRequestUsageV5, ...] = (),
    completed_assistant_generations: int = 1,
    retry_request_count: int = 0,
    elapsed_wall_time_ms: int | None = 1000,
    terminal_outcome_elapsed_ms: int | None = 900,
    active_elapsed_ms: int | None = 700,
    terminal_cause: str | None = None,
    context_limit_cause: str | None = None,
) -> EpisodeAttemptV5:
    is_incomplete = attempt_status.startswith("INCOMPLETE_")
    is_within = attempt_status.startswith("WITHIN_PROTOCOL_")
    return EpisodeAttemptV5(
        attempt_id=attempt_id,
        episode_id=episode_id,
        repeat_index=repeat_index,  # type: ignore[arg-type]
        run_config_sha256=RUN_SHA,
        attempt_status=attempt_status,  # type: ignore[arg-type]
        completed_assistant_generations=completed_assistant_generations,
        terminal_cause=terminal_cause,
        context_limit_cause=context_limit_cause,  # type: ignore[arg-type]
        tool_call_count=1 if not is_incomplete else 0,
        retry_request_count=retry_request_count,
        elapsed_wall_time_ms=elapsed_wall_time_ms,
        terminal_outcome_elapsed_ms=terminal_outcome_elapsed_ms,
        active_elapsed_ms=active_elapsed_ms,
        provider_requests=provider_requests,
        g_srr=None if is_incomplete else (not is_within),
        sem_srr_commit=None if is_incomplete else (not is_within),
        overgrant_count=None if is_incomplete else 0,
        avail_breach_count=None if is_incomplete else 0,
    )


def test_provider_fingerprint_is_stable_and_bound_to_role_and_endpoint() -> None:
    initial = _fingerprint()
    assert len(initial) == 64
    assert initial == _fingerprint()
    assert initial != _fingerprint("executor")
    assert initial != provider_fingerprint_sha256_v5(
        "unified", "openai_compatible", "test-model", "b" * 16
    )


def test_manifest_and_attestation_contracts_are_strict_and_unique_by_role() -> None:
    fingerprint = _fingerprint()
    evidence = {
        "role": "unified",
        "provider_kind": "openai_compatible",
        "model_id": "test-model",
        "provider_fingerprint_sha256": fingerprint,
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "evidence_sha256": "b" * 64,
        "adapter_retry_limit": 0,
    }
    parsed = UsageAccountingEvidenceV5(**evidence)
    assert parsed.evidence_sha256 == "b" * 64
    assert parsed.adapter_retry_limit == 0

    attestation = UsageAccountingAttestationV5(
        schema_version=V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        role="unified",
        provider_kind="openai_compatible",
        model_id="test-model",
        provider_fingerprint_sha256=fingerprint,
        aggregate_usage_scope=USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        attestation="operator_or_gateway",
    )
    assert attestation.attestation == "operator_or_gateway"

    entry = {**evidence, "evidence_path": "attestations/usage.json"}
    manifest = UsageAccountingManifestV5(
        schema_version=V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        entries=(entry,),
    )
    assert manifest.entries[0].runner_evidence() == parsed
    with pytest.raises(ValidationError):
        UsageAccountingEvidenceV5(**{**evidence, "adapter_retry_limit": True})
    with pytest.raises(ValidationError):
        UsageAccountingManifestV5(
            schema_version=V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
            entries=(entry, entry),
        )


def test_sdk_retry_contract_checks_adapter_and_client_without_creation() -> None:
    adapter_type = type(
        "OpenAICompatProvider",
        (),
        {"__module__": "identitybench.providers.openai_compat"},
    )
    lazy_provider = adapter_type()
    lazy_provider._max_retries = 0
    lazy_provider._client = None
    lazy_contract = effective_local_retry_contract_v5(lazy_provider)
    assert lazy_contract is not None
    assert lazy_contract.max_retries == 0
    assert lazy_contract.control_source == "sdk_adapter_lazy_client_max_retries"
    assert lazy_provider._client is None

    injected_provider = adapter_type()
    injected_provider._max_retries = 0
    injected_provider._client = SimpleNamespace(max_retries=0)
    injected_contract = effective_local_retry_contract_v5(injected_provider)
    assert injected_contract is not None
    assert injected_contract.max_retries == 0
    assert injected_contract.control_source == "sdk_adapter_and_injected_client_max_retries"

    injected_provider._client.max_retries = 2
    assert effective_local_retry_contract_v5(injected_provider) is None


def test_unrecognized_adapter_does_not_gain_retry_proof_from_a_similarly_named_field() -> None:
    provider = SimpleNamespace(_max_retries=0)
    assert effective_local_retry_contract_v5(provider) is None


def test_explicit_v5_adapter_contract_is_supported_but_does_not_attest_upstream_usage() -> None:
    class Provider:
        def v5_retry_accounting_contract(self) -> AdapterRetryAccountingContractV5:
            return AdapterRetryAccountingContractV5(
                policy_id="test-adapter-retry-v1",
                max_retries=0,
                control_source="test_adapter_contract",
            )

    contract = effective_local_retry_contract_v5(Provider())
    assert contract is not None
    assert contract.policy_id == "test-adapter-retry-v1"
    assert contract.max_retries == 0


def test_canonical_policy_registry_and_validation() -> None:
    expected_keys = {
        "openai_responses",
        "openai_chat_completions",
        "anthropic_messages",
        "google_gemini_vertex",
        "offline_replay",
        "unknown_mapping",
    }
    assert set(CANONICAL_PROVIDER_USAGE_POLICIES_V5) == expected_keys
    assert get_provider_usage_policy_v5("OPENAI_RESPONSES") == (
        CANONICAL_PROVIDER_USAGE_POLICIES_V5["openai_responses"]
    )
    assert (
        get_provider_usage_policy_v5("anthropic_messages").to_canonical_json()
        == get_provider_usage_policy_v5("ANTHROPIC_MESSAGES").to_canonical_json()
    )
    with pytest.raises(KeyError):
        get_provider_usage_policy_v5("mystery_provider")

    with pytest.raises(ValidationError):
        ProviderUsageAccountingPolicyV5(
            policy_id="bad-disjoint",
            provider_family="CUSTOM",
            policy_version="v1",
            documentation_source="doc",
            default_usage_scope="PHYSICAL_REQUEST",
            input_base_field="input_tokens",
            cached_input_field=None,
            cached_input_inclusion="NOT_APPLICABLE",
            cache_write_input_field=None,
            cache_write_input_inclusion="NOT_APPLICABLE",
            cache_billing_semantics="none",
            output_base_field="output_tokens",
            reasoning_tokens_field=None,
            reasoning_inclusion="DISJOINT_EXCLUDED",
            tool_serialization_tokens_field=None,
            tool_serialization_inclusion="NOT_APPLICABLE",
        )


def test_normalization_included_vs_disjoint_vs_unknown_and_contradictory() -> None:
    # 1. OpenAI Responses: INCLUDED cached & reasoning (no double-adding)
    raw_oai_resp = {
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 400},
        "output_tokens": 300,
        "output_tokens_details": {"reasoning_tokens": 120},
    }
    norm_oai_resp = normalize_raw_usage_v5(
        raw_oai_resp, get_provider_usage_policy_v5("openai_responses")
    )
    assert norm_oai_resp.input_tokens_total == 1200
    assert norm_oai_resp.cached_input_tokens == 400
    assert norm_oai_resp.output_tokens_total == 300
    assert norm_oai_resp.reasoning_tokens_subset == 120
    assert norm_oai_resp.tool_serialization_tokens_subset is None

    # 2. OpenAI Chat Completions: INCLUDED cached & reasoning
    raw_oai_chat = {
        "prompt_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 300},
        "completion_tokens": 250,
        "completion_tokens_details": {"reasoning_tokens": 100},
    }
    norm_oai_chat = normalize_raw_usage_v5(
        raw_oai_chat, get_provider_usage_policy_v5("openai_chat_completions")
    )
    assert norm_oai_chat.input_tokens_total == 900
    assert norm_oai_chat.cached_input_tokens == 300
    assert norm_oai_chat.output_tokens_total == 250
    assert norm_oai_chat.reasoning_tokens_subset == 100

    # 3. Anthropic Messages: DISJOINT_EXCLUDED cache_read + cache_creation, INCLUDED output
    raw_anth = {
        "input_tokens": 500,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 250,
        "output_tokens": 400,
        "cache_creation": {"ephemeral_5m_input_tokens": 250},
    }
    norm_anth = normalize_raw_usage_v5(
        raw_anth, get_provider_usage_policy_v5("anthropic_messages")
    )
    assert norm_anth.input_tokens_total == 1750
    assert norm_anth.cached_input_tokens == 1000
    assert norm_anth.output_tokens_total == 400
    assert norm_anth.reasoning_tokens_subset is None
    assert norm_anth.raw_usage_fields_json is not None
    assert json.loads(norm_anth.raw_usage_fields_json)["cache_creation_input_tokens"] == 250

    # Missing disjoint cache_creation_input_tokens -> input_tokens_total=None (no guessing!)
    norm_anth_missing = normalize_raw_usage_v5(
        {"input_tokens": 500, "cache_read_input_tokens": 100, "output_tokens": 400},
        get_provider_usage_policy_v5("anthropic_messages"),
    )
    assert norm_anth_missing.input_tokens_total is None
    assert norm_anth_missing.cached_input_tokens is None
    assert norm_anth_missing.output_tokens_total == 400

    # 4. Google Gemini / Vertex: INCLUDED cachedContentTokenCount, DISJOINT_EXCLUDED thoughts
    raw_gemini = {
        "promptTokenCount": 800,
        "cachedContentTokenCount": 200,
        "candidatesTokenCount": 150,
        "thoughtsTokenCount": 350,
        "totalTokenCount": 1300,
    }
    norm_gemini = normalize_raw_usage_v5(
        raw_gemini, get_provider_usage_policy_v5("google_gemini_vertex")
    )
    assert norm_gemini.input_tokens_total == 800
    assert norm_gemini.cached_input_tokens == 200
    assert norm_gemini.output_tokens_total == 500
    assert norm_gemini.reasoning_tokens_subset == 350

    # Missing DISJOINT_EXCLUDED thoughtsTokenCount -> output_tokens_total=None
    norm_gemini_missing = normalize_raw_usage_v5(
        {"promptTokenCount": 800, "candidatesTokenCount": 150},
        get_provider_usage_policy_v5("google_gemini_vertex"),
    )
    assert norm_gemini_missing.input_tokens_total == 800
    assert norm_gemini_missing.output_tokens_total is None
    assert norm_gemini_missing.reasoning_tokens_subset is None

    # 5. Offline Replay
    norm_replay = normalize_raw_usage_v5(
        {
            "input_tokens": 100,
            "cache_read_tokens": 200,
            "cache_write_tokens": 50,
            "output_tokens": 90,
            "reasoning_tokens": 40,
        },
        get_provider_usage_policy_v5("offline_replay"),
    )
    assert norm_replay.input_tokens_total == 350
    assert norm_replay.cached_input_tokens == 200
    assert norm_replay.output_tokens_total == 90
    assert norm_replay.reasoning_tokens_subset == 40

    # 6. Contradictory subset (> total) leaves total and subset None while keeping raw JSON
    contradictory = normalize_raw_usage_v5(
        {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 150},
            "output_tokens": 80,
            "output_tokens_details": {"reasoning_tokens": 90},
        },
        get_provider_usage_policy_v5("openai_responses"),
    )
    assert contradictory.input_tokens_total is None
    assert contradictory.cached_input_tokens is None
    assert contradictory.output_tokens_total is None
    assert contradictory.reasoning_tokens_subset is None
    assert contradictory.raw_usage_fields_json is not None
    assert json.loads(contradictory.raw_usage_fields_json)["output_tokens"] == 80

    # 7. UNKNOWN_MAPPING preserves raw fields verbatim and leaves normalized fields None
    unknown = normalize_raw_usage_v5(
        {"prompt_tokens": 100, "completion_tokens": 50, "mystery_reasoning": 20},
        get_provider_usage_policy_v5("unknown_mapping"),
    )
    assert unknown.usage_scope == "UNAVAILABLE"
    assert unknown.input_tokens_total is None
    assert unknown.output_tokens_total is None
    assert unknown.reasoning_tokens_subset is None
    assert unknown.raw_usage_fields_json is not None
    assert json.loads(unknown.raw_usage_fields_json)["mystery_reasoning"] == 20


def test_custom_tool_serialization_subset_policy() -> None:
    custom = ProviderUsageAccountingPolicyV5(
        policy_id="custom-tool-ser-v1",
        provider_family="CUSTOM",
        policy_version="v1",
        documentation_source="documented tool_serialization_tokens subset of output_tokens",
        default_usage_scope="PHYSICAL_REQUEST",
        input_base_field="input_tokens",
        cached_input_field=None,
        cached_input_inclusion="NOT_APPLICABLE",
        cache_write_input_field=None,
        cache_write_input_inclusion="NOT_APPLICABLE",
        cache_billing_semantics="none",
        output_base_field="output_tokens",
        reasoning_tokens_field="reasoning_tokens",
        reasoning_inclusion="INCLUDED",
        tool_serialization_tokens_field="tool_serialization_tokens",
        tool_serialization_inclusion="INCLUDED",
    )
    norm = normalize_raw_usage_v5(
        {
            "input_tokens": 300,
            "output_tokens": 200,
            "reasoning_tokens": 80,
            "tool_serialization_tokens": 45,
        },
        custom,
    )
    assert norm.output_tokens_total == 200
    assert norm.reasoning_tokens_subset == 80
    assert norm.tool_serialization_tokens_subset == 45


def test_usage_scope_prevents_cumulative_double_counting_and_supports_derivation() -> None:
    rate = FrozenPricingRateV5(
        pricing_manifest_sha256=PRICING_SHA,
        currency_code="USD",
        input_cost_per_million_micros=1_000_000,
        output_cost_per_million_micros=2_000_000,
    )
    policy = get_provider_usage_policy_v5("openai_responses")
    seg0 = _build_req(
        request_id="req_seg0",
        logical_request_id="lreq_1",
        request_kind="INITIAL",
        retry_index=0,
        decode_segment_index=0,
        raw_usage={"input_tokens": 1000, "output_tokens": 400},
        policy=policy,
        pricing_rate=rate,
    )
    seg1 = _build_req(
        request_id="req_seg1",
        logical_request_id="lreq_1",
        request_kind="SEGMENT_CONTINUATION",
        retry_index=None,
        segment_continuation_index=1,
        decode_segment_index=1,
        raw_usage={"input_tokens": 1400, "output_tokens": 600},
        policy=policy,
        pricing_rate=rate,
    )
    redundant_cum = _build_req(
        request_id="req_cum",
        logical_request_id="lreq_1",
        request_kind="SEGMENT_CONTINUATION",
        retry_index=None,
        segment_continuation_index=1,
        decode_segment_index=1,
        raw_usage={"input_tokens": 2400, "output_tokens": 1000},
        policy=policy,
        usage_scope_override="LOGICAL_RESPONSE_CUMULATIVE",
        pricing_rate=rate,
    )

    selected = select_contributing_usage_records_v5((seg0, seg1, redundant_cum))
    assert [r.request_id for r in selected] == ["req_seg0", "req_seg1"]

    attempt = _attempt(provider_requests=(seg0, seg1, redundant_cum))
    summary = summarize_descriptive_usage_v5(RUN_SHA, 1, (attempt,))
    assert summary.total_input_tokens == 2400
    assert summary.total_output_tokens == 1000
    assert summary.accounting_coverage_status == "COMPLETE"
    gate = evaluate_g_econ_01_v5((attempt,), summary)
    assert gate["passed"] is True

    # Documented cumulative-to-segment derivation
    cum0 = _build_req(
        request_id="cum_0",
        logical_request_id="lreq_2",
        request_kind="INITIAL",
        retry_index=0,
        decode_segment_index=0,
        raw_usage={"input_tokens": 1000, "output_tokens": 300},
        policy=policy,
        usage_scope_override="LOGICAL_RESPONSE_CUMULATIVE",
    )
    cum1 = _build_req(
        request_id="cum_1",
        logical_request_id="lreq_2",
        request_kind="SEGMENT_CONTINUATION",
        retry_index=None,
        segment_continuation_index=1,
        decode_segment_index=1,
        raw_usage={"input_tokens": 2500, "output_tokens": 900},
        policy=policy,
        usage_scope_override="LOGICAL_RESPONSE_CUMULATIVE",
    )
    derived = derive_physical_segment_usage_from_cumulative_v5((cum0, cum1))
    assert [d.usage_scope for d in derived] == ["PHYSICAL_REQUEST", "PHYSICAL_REQUEST"]
    assert [(d.input_tokens_total, d.output_tokens_total) for d in derived] == [
        (1000, 300),
        (1500, 600),
    ]

    # Non-monotonic cumulative series must fail loudly
    bad_cum1 = cum1.model_copy(update={"output_tokens_total": 100})
    with pytest.raises(ValueError, match="decreased"):
        derive_physical_segment_usage_from_cumulative_v5((cum0, bad_cum1))


def test_billed_vs_estimated_cost_and_currency_mismatch_guard() -> None:
    anth_policy = get_provider_usage_policy_v5("anthropic_messages")
    anth_rate = FrozenPricingRateV5(
        pricing_manifest_sha256=PRICING_SHA,
        currency_code="USD",
        input_cost_per_million_micros=3_000_000,
        cached_input_cost_per_million_micros=300_000,
        cache_write_cost_per_million_micros=3_750_000,
        output_cost_per_million_micros=15_000_000,
    )
    raw_anth = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    # Expected USD micros: 3_000_000 + 300_000 + 3_750_000 + 15_000_000 = 22_050_000
    est_req = _build_req(
        request_id="req_est",
        raw_usage=raw_anth,
        policy=anth_policy,
        pricing_rate=anth_rate,
    )
    assert est_req.cost_basis == "FROZEN_RATE_ESTIMATE"
    assert est_req.estimated_cost_micros == 22_050_000
    assert est_req.provider_reported_cost_micros is None
    assert est_req.pricing_manifest_sha256 == PRICING_SHA

    # If cached rate differs and cached_input_tokens is None, estimate must return None
    oai_norm_no_cache = normalize_raw_usage_v5(
        {"input_tokens": 1_000_000, "output_tokens": 500_000},
        get_provider_usage_policy_v5("openai_responses"),
    )
    assert estimate_frozen_cost_micros_v5(oai_norm_no_cache, anth_rate) is None

    billed_eur_req = _build_req(
        request_id="req_billed_eur",
        attempt_id="att_2",
        raw_usage={"input_tokens": 1000, "output_tokens": 500},
        policy=get_provider_usage_policy_v5("openai_responses"),
        provider_reported_cost_micros=2_000,
        currency_code="EUR",
    )
    assert billed_eur_req.cost_basis == "PROVIDER_BILLED"
    assert billed_eur_req.provider_reported_cost_micros == 2_000
    assert billed_eur_req.estimated_cost_micros is None

    a1 = _attempt(attempt_id="att_1", repeat_index=1, provider_requests=(est_req,))
    a2 = _attempt(attempt_id="att_2", repeat_index=2, provider_requests=(billed_eur_req,))

    # Mixed USD + EUR without conversion rate is forbidden
    with pytest.raises(ValueError, match="mismatched currencies"):
        summarize_descriptive_usage_v5(RUN_SHA, 2, (a1, a2))

    # Explicit frozen conversion rate allows conversion with retained original request records
    conv = CurrencyConversionRateV5(
        from_currency="EUR",
        to_currency="USD",
        rate_numerator=11,
        rate_denominator=10,
        rate_source="ECB_FROZEN_2026_11_01",
        pricing_manifest_sha256=RATE_SHA,
    )
    summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        2,
        (a1, a2),
        target_currency="USD",
        currency_conversions=(conv,),
    )
    assert summary.currency_code == "USD"
    assert summary.estimated_cost_micros == 22_050_000
    assert summary.provider_billed_cost_micros == 2_200
    assert evaluate_g_econ_01_v5(
        (a1, a2), summary, target_currency="USD", currency_conversions=(conv,)
    )["passed"] is True


def test_interval_union_active_elapsed_ms_and_attempt_timings() -> None:
    assert compute_active_elapsed_ms([]) == 0
    assert compute_active_elapsed_ms([(100, 100)]) == 0
    # Concurrent overlapping intervals + disjoint intervals (excluding [200, 500] quota pause)
    intervals = [
        (0, 100),
        (20, 120),
        (120, 200),
        (500, 650),
        (550, 700),
    ]
    assert compute_active_elapsed_ms(intervals) == 400

    wall, term, active = compute_attempt_timings_ms_v5(
        first_dispatch_ms=1_000,
        terminal_outcome_ms=1_800,
        final_persisted_ms=2_000,
        active_intervals_ms=[(1_000, 1_300), (1_100, 1_400), (1_700, 1_900)],
    )
    assert wall == 1_000
    assert term == 800
    assert active == 600

    with pytest.raises(ValueError):
        compute_active_elapsed_ms([(200, 100)])
    with pytest.raises(ValueError):
        compute_active_elapsed_ms([(-1, 10)])


def test_summarize_descriptive_usage_sc03_and_coverage_levels() -> None:
    rate = FrozenPricingRateV5(
        pricing_manifest_sha256=PRICING_SHA,
        currency_code="USD",
        input_cost_per_million_micros=1_000_000,
        output_cost_per_million_micros=2_000_000,
    )
    req_ok = _build_req(
        request_id="req_1",
        attempt_id="att_1",
        raw_usage={"input_tokens": 1000, "output_tokens": 500},
        pricing_rate=rate,
    )
    req_unknown = _build_req(
        request_id="req_2",
        attempt_id="att_2",
        request_status="TRANSIENT_FAILURE",
        raw_usage={"mystery_tokens": 50},
        policy=get_provider_usage_policy_v5("unknown_mapping"),
    )

    a_completed = _attempt(
        attempt_id="att_1", repeat_index=1, attempt_status="COMPLETED", provider_requests=(req_ok,)
    )
    a_within = _attempt(
        attempt_id="att_2",
        repeat_index=2,
        attempt_status="WITHIN_PROTOCOL_REFUSAL",
        provider_requests=(req_unknown,),
    )

    # 1. Partial coverage (1 known + 1 unknown) and 1 missing planned slot (SC-03)
    partial_summary = summarize_descriptive_usage_v5(
        RUN_SHA,
        planned_attempt_slots=3,
        attempts=(a_completed, a_within),
    )
    assert partial_summary.completed_attempts == 1
    assert partial_summary.incomplete_harness_attempts == 1
    assert partial_summary.accounting_coverage_status == "PARTIAL"
    assert partial_summary.total_input_tokens == 1000
    assert partial_summary.total_output_tokens == 500
    assert partial_summary.estimated_cost_micros == 2_000
    counts = json.loads(partial_summary.attempt_status_counts_json)
    assert set(counts) == set(CANONICAL_ATTEMPT_STATUSES_V5)
    assert counts["COMPLETED"] == 1
    assert counts["WITHIN_PROTOCOL_REFUSAL"] == 1
    assert counts["INCOMPLETE_HARNESS_FAILURE"] == 1
    assert sum(counts.values()) == 3

    with pytest.raises(ValueError, match="expected 3 materialized attempts"):
        summarize_descriptive_usage_v5(
            RUN_SHA,
            planned_attempt_slots=3,
            attempts=(a_completed, a_within),
            missing_slot_status=None,
        )

    # 2. Unavailable coverage leaves all token/cost fields None
    a_unavail = _attempt(
        attempt_id="att_3",
        repeat_index=3,
        attempt_status="INCOMPLETE_PROVIDER_FAILURE",
        completed_assistant_generations=0,
        provider_requests=(
            _build_req(
                request_id="req_3",
                attempt_id="att_3",
                request_status="AMBIGUOUS_FAILURE",
                raw_usage=None,
                policy=get_provider_usage_policy_v5("unknown_mapping"),
            ),
        ),
    )
    unavail_summary = summarize_descriptive_usage_v5(RUN_SHA, 1, (a_unavail,))
    assert unavail_summary.accounting_coverage_status == "UNAVAILABLE"
    assert unavail_summary.total_input_tokens is None
    assert unavail_summary.total_output_tokens is None
    assert unavail_summary.provider_billed_cost_micros is None
    assert unavail_summary.estimated_cost_micros is None
    assert evaluate_g_econ_01_v5((a_unavail,), unavail_summary)["passed"] is True


def test_unavailable_retry_and_segment_usage_preserve_known_billed_costs() -> None:
    retry = _build_req(
        request_id="req_retry_billed",
        logical_request_id="lreq_retry_billed",
        request_kind="TRANSIENT_RETRY",
        retry_index=1,
        raw_usage={"input_tokens": 1.5, "output_tokens": False},
        usage_scope_override="UNAVAILABLE",
        provider_reported_cost_micros=1_200,
        currency_code="USD",
    )
    segment = _build_req(
        request_id="req_segment_billed",
        logical_request_id="lreq_segment_billed",
        request_kind="SEGMENT_CONTINUATION",
        retry_index=None,
        segment_continuation_index=1,
        decode_segment_index=1,
        raw_usage={"input_tokens": "unknown", "output_tokens": -1},
        usage_scope_override="UNAVAILABLE",
        provider_reported_cost_micros=2_300,
        currency_code="USD",
    )
    attempt = _attempt(provider_requests=(retry, segment), retry_request_count=1)

    summary = summarize_descriptive_usage_v5(RUN_SHA, 1, (attempt,))

    assert summary.accounting_coverage_status == "PARTIAL"
    assert summary.total_input_tokens is None
    assert summary.total_output_tokens is None
    assert summary.provider_billed_cost_micros == 3_500
    assert summary.estimated_cost_micros is None


def test_evaluate_g_econ_01_has_no_spend_ceiling_and_catches_incompleteness() -> None:
    # Huge spend and token counts must pass G-ECON-01 (no budget/spend ceiling)
    huge_req = _build_req(
        request_id="req_huge",
        attempt_id="att_huge",
        raw_usage={"input_tokens": 100_000_000, "output_tokens": 50_000_000},
        provider_reported_cost_micros=10_000_000_000,  # $10,000
        currency_code="USD",
    )
    post_term_cancel = _build_req(
        request_id="req_cancel",
        logical_request_id="lreq_cancel",
        request_kind="TRANSIENT_RETRY",
        retry_index=1,
        attempt_id="att_huge",
        request_status="CANCELLED",
        post_terminal=True,
        raw_usage={"input_tokens": 500, "output_tokens": 0},
        provider_reported_cost_micros=5,
        currency_code="USD",
    )
    a_huge = _attempt(
        attempt_id="att_huge",
        retry_request_count=1,
        provider_requests=(huge_req, post_term_cancel),
    )
    summary = summarize_descriptive_usage_v5(RUN_SHA, 1, (a_huge,))
    result = evaluate_g_econ_01_v5((a_huge,), summary)
    assert result["passed"] is True
    assert result["spend_or_token_ceiling_applied"] is False
    assert result["accounting_coverage_status"] == "COMPLETE"

    # Tamper summary to claim double-counted tokens -> must FAIL G-ECON-01
    bad_summary = summary.model_copy(update={"total_input_tokens": 200_000_000})
    bad_result = evaluate_g_econ_01_v5((a_huge,), bad_summary)
    assert bad_result["passed"] is False
    assert any("total_input_tokens" in err for err in bad_result["errors"])

    # Missing retry record -> must FAIL G-ECON-01
    missing_retry_attempt = _attempt(
        attempt_id="att_huge",
        retry_request_count=2,
        provider_requests=(huge_req, post_term_cancel),
    )
    missing_retry_result = evaluate_g_econ_01_v5((missing_retry_attempt,), summary)
    assert missing_retry_result["passed"] is False
    assert any("retry_request_count" in err for err in missing_retry_result["errors"])

    # Non-contiguous decode_segment_index -> must FAIL G-ECON-01
    gap_seg = _build_req(
        request_id="req_gap",
        logical_request_id="lreq_1",
        request_kind="SEGMENT_CONTINUATION",
        retry_index=None,
        segment_continuation_index=2,
        decode_segment_index=2,
        attempt_id="att_huge",
        raw_usage={"input_tokens": 100, "output_tokens": 50},
        provider_reported_cost_micros=10,
        currency_code="USD",
    )
    gap_attempt = _attempt(
        attempt_id="att_huge",
        retry_request_count=0,
        provider_requests=(huge_req, gap_seg),
    )
    gap_summary = summarize_descriptive_usage_v5(RUN_SHA, 1, (gap_attempt,))
    gap_result = evaluate_g_econ_01_v5((gap_attempt,), gap_summary)
    assert gap_result["passed"] is False
    assert any("non-contiguous decode_segment_index" in err for err in gap_result["errors"])
