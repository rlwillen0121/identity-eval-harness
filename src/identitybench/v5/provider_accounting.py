"""Explicit v5-only provider retry, usage-normalization, timing, and cost accounting.

The shared provider adapters remain version-neutral. This module preserves v5 retry-accounting
attestations (`UsageAccountingManifestV5`, `effective_local_retry_contract_v5`) and implements
``SPEC-v5.0-draft.5.md`` §2.2, §4.2 item 6, and §4.3 ``G-ECON-01`` descriptive usage,
interval-union active timing, currency-safe billed/estimated cost separation, and completeness
checks without any spend or token-volume ceiling.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, StrictInt, StrictStr, model_validator

from identitybench.v4.jcs import jcs_dumps
from identitybench.v5.schemas import (
    _CANONICAL_ATTEMPT_STATUSES_V5,
    DescriptiveUsageSummaryV5,
    EpisodeAttemptV5,
    ProviderRequestUsageV5,
    StrictV5Model,
)

V5_USAGE_ACCOUNTING_POLICY_ID = "identitybench-v5-all-attempt-usage-v1"
V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA = "identitybench-v5-usage-accounting-manifest-v1"
V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA = "identitybench-v5-provider-usage-attestation-v1"
V5_PROVIDER_FINGERPRINT_SCHEMA = "identitybench-v5-provider-fingerprint-v1"
USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5 = (
    "all_billed_input_output_tokens_for_every_completion_and_upstream_retry"
)

ProviderRoleV5 = Literal["unified", "planner", "executor", "auditor"]
UsageAccountingStatusV5 = Literal[
    "ATTESTED", "PROVIDER_DECLARED", "UNKNOWN", "NOT_APPLICABLE"
]

UsageInclusionSemanticsV5 = Literal[
    "INCLUDED",
    "DISJOINT_EXCLUDED",
    "UNKNOWN",
    "NOT_APPLICABLE",
]
UsageScopeV5 = Literal["PHYSICAL_REQUEST", "LOGICAL_RESPONSE_CUMULATIVE", "UNAVAILABLE"]
CostBasisV5 = Literal["PROVIDER_BILLED", "FROZEN_RATE_ESTIMATE", "UNAVAILABLE"]
AccountingCoverageStatusV5 = Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]
IncompleteAttemptStatusV5 = Literal[
    "INCOMPLETE_PROVIDER_QUOTA",
    "INCOMPLETE_PROVIDER_FAILURE",
    "INCOMPLETE_CONTEXT_INCOMPATIBLE",
    "INCOMPLETE_HARNESS_FAILURE",
]

CANONICAL_ATTEMPT_STATUSES_V5: tuple[str, ...] = (
    "COMPLETED",
    "WITHIN_PROTOCOL_REFUSAL",
    "WITHIN_PROTOCOL_PARSE_FAILURE",
    "WITHIN_PROTOCOL_STEP_LIMIT",
    "WITHIN_PROTOCOL_OUTPUT_LIMIT",
    "WITHIN_PROTOCOL_CONTEXT_LIMIT",
    "INCOMPLETE_PROVIDER_QUOTA",
    "INCOMPLETE_PROVIDER_FAILURE",
    "INCOMPLETE_CONTEXT_INCOMPATIBLE",
    "INCOMPLETE_HARNESS_FAILURE",
)


def provider_fingerprint_sha256_v5(
    role: str,
    provider_kind: str,
    model_id: str,
    endpoint_identity: str,
) -> str:
    """Hash a role and the provider's already non-secret identity, never a raw endpoint URL."""
    payload = {
        "schema_version": V5_PROVIDER_FINGERPRINT_SCHEMA,
        "role": role,
        "provider_kind": provider_kind,
        "model_id": model_id,
        "endpoint_identity": endpoint_identity,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class UsageAccountingEvidenceV5(StrictV5Model):
    """Validated per-role evidence passed from the v5 CLI to the offline runner."""

    role: ProviderRoleV5
    provider_kind: StrictStr = Field(min_length=1, max_length=80)
    model_id: StrictStr = Field(min_length=1, max_length=200)
    provider_fingerprint_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    aggregate_usage_scope: Literal[
        "all_billed_input_output_tokens_for_every_completion_and_upstream_retry"
    ]
    evidence_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    adapter_retry_limit: StrictInt = Field(ge=0, le=0)


class UsageAccountingAttestationV5(StrictV5Model):
    """The immutable, non-secret operator/gateway evidence document."""

    schema_version: Literal["identitybench-v5-provider-usage-attestation-v1"]
    role: ProviderRoleV5
    provider_kind: StrictStr = Field(min_length=1, max_length=80)
    model_id: StrictStr = Field(min_length=1, max_length=200)
    provider_fingerprint_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    aggregate_usage_scope: Literal[
        "all_billed_input_output_tokens_for_every_completion_and_upstream_retry"
    ]
    attestation: Literal["operator_or_gateway"]


class UsageAccountingManifestEntryV5(StrictV5Model):
    """One manifest entry; the CLI verifies and copies ``evidence_path`` before provider setup."""

    role: ProviderRoleV5
    provider_kind: StrictStr = Field(min_length=1, max_length=80)
    model_id: StrictStr = Field(min_length=1, max_length=200)
    provider_fingerprint_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    aggregate_usage_scope: Literal[
        "all_billed_input_output_tokens_for_every_completion_and_upstream_retry"
    ]
    evidence_path: StrictStr = Field(min_length=1, max_length=512)
    evidence_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    adapter_retry_limit: StrictInt = Field(ge=0, le=0)

    def runner_evidence(self) -> UsageAccountingEvidenceV5:
        """Drop the input path after the CLI has verified and copied its immutable bytes."""
        return UsageAccountingEvidenceV5(
            role=self.role,
            provider_kind=self.provider_kind,
            model_id=self.model_id,
            provider_fingerprint_sha256=self.provider_fingerprint_sha256,
            aggregate_usage_scope=self.aggregate_usage_scope,
            evidence_sha256=self.evidence_sha256,
            adapter_retry_limit=self.adapter_retry_limit,
        )


class UsageAccountingManifestV5(StrictV5Model):
    schema_version: Literal["identitybench-v5-usage-accounting-manifest-v1"]
    entries: tuple[UsageAccountingManifestEntryV5, ...]

    @model_validator(mode="after")
    def _unique_roles(self) -> UsageAccountingManifestV5:
        if not self.entries:
            raise ValueError("usage-accounting manifest must contain at least one role")
        roles = tuple(entry.role for entry in self.entries)
        if len(roles) != len(set(roles)):
            raise ValueError("usage-accounting manifest roles must be unique")
        return self


@dataclass(frozen=True)
class AdapterRetryAccountingContractV5:
    """Explicit extension contract for a provider adapter's effective local retry setting."""

    policy_id: str
    max_retries: int
    control_source: str


@runtime_checkable
class RetryAccountingAdapterV5(Protocol):
    """Optional v5 contract for provider adapters added without changing shared provider types."""

    def v5_retry_accounting_contract(self) -> AdapterRetryAccountingContractV5: ...


@dataclass(frozen=True)
class ProviderAccountingTraceV5:
    """Safe per-role accounting provenance serialized with one episode."""

    role: str
    provider_kind: str
    model_id: str
    provider_fingerprint_sha256: str
    local_retry_limit: int | None
    local_retry_policy_id: str | None
    retry_control_source: str
    evidence_sha256: str | None
    aggregate_usage_scope: str | None
    status: UsageAccountingStatusV5
    basis: str


_BUILTIN_RETRY_ADAPTERS_V5: dict[tuple[str, str], str] = {
    ("identitybench.providers.openai_compat", "OpenAICompatProvider"): "sdk_max_retries",
    ("identitybench.providers.anthropic_api", "AnthropicProvider"): "sdk_max_retries",
    ("identitybench.providers.claude_cli", "ClaudeCliProvider"): "cli_max_retries",
    ("identitybench.providers.codex_cli", "CodexCliProvider"): "cli_max_retries",
    ("identitybench.providers.grok_cli", "GrokCliProvider"): "cli_max_retries",
    ("identitybench.providers.agy_cli", "AgyCliProvider"): "cli_max_retries",
    ("identitybench.providers.cursor_cli", "CursorCliProvider"): "cli_max_retries",
    ("identitybench.providers.muse_cli", "MuseCliProvider"): "cli_max_retries",
}
_CONTRACT_LABEL_RE_V5 = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _safe_contract_label_v5(value: object) -> bool:
    return isinstance(value, str) and _CONTRACT_LABEL_RE_V5.fullmatch(value) is not None


def effective_local_retry_contract_v5(
    provider: object,
) -> AdapterRetryAccountingContractV5 | None:
    """Return a proven adapter retry setting without creating clients, probing networks or env.

    Known shared adapters are read through the exact `_max_retries` field used by their request
    path. Lazy SDK clients inherit that value at construction. If a caller injected an SDK
    client, its own public `max_retries` must match. Future adapters may provide the explicit
    ``v5_retry_accounting_contract`` method instead.
    """
    declared = getattr(provider, "v5_retry_accounting_contract", None)
    if callable(declared):
        try:
            contract = declared()
        except Exception:
            return None
        if (
            isinstance(contract, AdapterRetryAccountingContractV5)
            and type(contract.max_retries) is int
            and contract.max_retries >= 0
            and _safe_contract_label_v5(contract.policy_id)
            and _safe_contract_label_v5(contract.control_source)
        ):
            return contract
        return None

    adapter_kind = _BUILTIN_RETRY_ADAPTERS_V5.get(
        (type(provider).__module__, type(provider).__name__)
    )
    if adapter_kind is None:
        return None
    max_retries = getattr(provider, "_max_retries", None)
    if type(max_retries) is not int or max_retries < 0:
        return None

    if adapter_kind == "sdk_max_retries":
        client = getattr(provider, "_client", None)
        if client is not None:
            client_max_retries = getattr(client, "max_retries", None)
            if type(client_max_retries) is not int or client_max_retries != max_retries:
                return None
            control_source = "sdk_adapter_and_injected_client_max_retries"
        else:
            control_source = "sdk_adapter_lazy_client_max_retries"
    else:
        control_source = "cli_adapter_max_retries"

    return AdapterRetryAccountingContractV5(
        policy_id=f"identitybench-v5-{adapter_kind}-max-retries-v1",
        max_retries=max_retries,
        control_source=control_source,
    )


class ProviderUsageAccountingPolicyV5(StrictV5Model):
    """Frozen documented usage-normalization contract for a provider/model adapter."""

    policy_id: StrictStr = Field(min_length=1, max_length=128)
    provider_family: Literal[
        "OPENAI_RESPONSES",
        "OPENAI_CHAT_COMPLETIONS",
        "ANTHROPIC_MESSAGES",
        "GOOGLE_GEMINI_VERTEX",
        "OFFLINE_REPLAY",
        "UNKNOWN_MAPPING",
        "CUSTOM",
    ]
    policy_version: StrictStr = Field(min_length=1, max_length=64)
    documentation_source: StrictStr = Field(min_length=1, max_length=512)
    default_usage_scope: UsageScopeV5
    input_base_field: StrictStr | None = None
    cached_input_field: StrictStr | None = None
    cached_input_inclusion: UsageInclusionSemanticsV5
    cache_write_input_field: StrictStr | None = None
    cache_write_input_inclusion: UsageInclusionSemanticsV5
    cache_billing_semantics: StrictStr = Field(min_length=1, max_length=256)
    output_base_field: StrictStr | None = None
    reasoning_tokens_field: StrictStr | None = None
    reasoning_inclusion: UsageInclusionSemanticsV5
    tool_serialization_tokens_field: StrictStr | None = None
    tool_serialization_inclusion: UsageInclusionSemanticsV5

    @model_validator(mode="after")
    def _check_policy_consistency(self) -> ProviderUsageAccountingPolicyV5:
        if (
            self.cached_input_inclusion == "DISJOINT_EXCLUDED"
            and self.cached_input_field is None
        ):
            raise ValueError(
                "DISJOINT_EXCLUDED cached_input_inclusion requires cached_input_field"
            )
        if (
            self.cache_write_input_inclusion == "DISJOINT_EXCLUDED"
            and self.cache_write_input_field is None
        ):
            raise ValueError(
                "DISJOINT_EXCLUDED cache_write_input_inclusion requires cache_write_input_field"
            )
        if (
            self.reasoning_inclusion == "DISJOINT_EXCLUDED"
            and self.reasoning_tokens_field is None
        ):
            raise ValueError(
                "DISJOINT_EXCLUDED reasoning_inclusion requires reasoning_tokens_field"
            )
        if (
            self.tool_serialization_inclusion == "DISJOINT_EXCLUDED"
            and self.tool_serialization_tokens_field is None
        ):
            raise ValueError(
                "DISJOINT_EXCLUDED tool_serialization_inclusion requires "
                "tool_serialization_tokens_field"
            )
        if self.provider_family == "UNKNOWN_MAPPING" and any(
            inc != "UNKNOWN"
            for inc in (
                self.cached_input_inclusion,
                self.cache_write_input_inclusion,
                self.reasoning_inclusion,
                self.tool_serialization_inclusion,
            )
        ):
            raise ValueError("UNKNOWN_MAPPING policy must keep all inclusion rules UNKNOWN")
        return self

    def to_canonical_json(self) -> str:
        """Serialize this policy deterministically for ``RunConfigurationV5``."""
        return jcs_dumps(self.model_dump(mode="json")).decode("utf-8")


CANONICAL_PROVIDER_USAGE_POLICIES_V5: Mapping[str, ProviderUsageAccountingPolicyV5] = (
    MappingProxyType(
        {
            "openai_responses": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-openai-responses-v1",
                provider_family="OPENAI_RESPONSES",
                policy_version="v1",
                documentation_source=(
                    "OpenAI Responses API usage object documentation "
                    "(input_tokens includes input_tokens_details.cached_tokens; "
                    "output_tokens includes output_tokens_details.reasoning_tokens)"
                ),
                default_usage_scope="PHYSICAL_REQUEST",
                input_base_field="input_tokens",
                cached_input_field="input_tokens_details.cached_tokens",
                cached_input_inclusion="INCLUDED",
                cache_write_input_field=None,
                cache_write_input_inclusion="NOT_APPLICABLE",
                cache_billing_semantics=(
                    "cached_tokens_documented_subset_of_input_tokens;"
                    "no_standard_cache_write_field"
                ),
                output_base_field="output_tokens",
                reasoning_tokens_field="output_tokens_details.reasoning_tokens",
                reasoning_inclusion="INCLUDED",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="NOT_APPLICABLE",
            ),
            "openai_chat_completions": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-openai-chat-completions-v1",
                provider_family="OPENAI_CHAT_COMPLETIONS",
                policy_version="v1",
                documentation_source=(
                    "OpenAI Chat Completions API usage object documentation "
                    "(prompt_tokens includes prompt_tokens_details.cached_tokens; "
                    "completion_tokens includes completion_tokens_details.reasoning_tokens)"
                ),
                default_usage_scope="PHYSICAL_REQUEST",
                input_base_field="prompt_tokens",
                cached_input_field="prompt_tokens_details.cached_tokens",
                cached_input_inclusion="INCLUDED",
                cache_write_input_field=None,
                cache_write_input_inclusion="NOT_APPLICABLE",
                cache_billing_semantics=(
                    "cached_tokens_documented_subset_of_prompt_tokens;"
                    "no_standard_cache_write_field"
                ),
                output_base_field="completion_tokens",
                reasoning_tokens_field="completion_tokens_details.reasoning_tokens",
                reasoning_inclusion="INCLUDED",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="NOT_APPLICABLE",
            ),
            "anthropic_messages": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-anthropic-messages-v1",
                provider_family="ANTHROPIC_MESSAGES",
                policy_version="v1",
                documentation_source=(
                    "Anthropic Messages API usage object documentation "
                    "(total_input = input_tokens + cache_creation_input_tokens + "
                    "cache_read_input_tokens; output_tokens includes thinking tokens)"
                ),
                default_usage_scope="PHYSICAL_REQUEST",
                input_base_field="input_tokens",
                cached_input_field="cache_read_input_tokens",
                cached_input_inclusion="DISJOINT_EXCLUDED",
                cache_write_input_field="cache_creation_input_tokens",
                cache_write_input_inclusion="DISJOINT_EXCLUDED",
                cache_billing_semantics=(
                    "input_tokens_excludes_cache_read_and_cache_creation;"
                    "cache_read_discounted;cache_creation_surcharged_preserved_in_raw"
                ),
                output_base_field="output_tokens",
                reasoning_tokens_field="output_tokens_details.thinking_tokens",
                reasoning_inclusion="INCLUDED",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="NOT_APPLICABLE",
            ),
            "google_gemini_vertex": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-google-gemini-vertex-v1",
                provider_family="GOOGLE_GEMINI_VERTEX",
                policy_version="v1",
                documentation_source=(
                    "Google Gemini / Vertex AI GenerateContentResponse.usageMetadata "
                    "(promptTokenCount includes cachedContentTokenCount; "
                    "output = candidatesTokenCount + thoughtsTokenCount disjoint)"
                ),
                default_usage_scope="PHYSICAL_REQUEST",
                input_base_field="promptTokenCount",
                cached_input_field="cachedContentTokenCount",
                cached_input_inclusion="INCLUDED",
                cache_write_input_field=None,
                cache_write_input_inclusion="NOT_APPLICABLE",
                cache_billing_semantics=(
                    "cachedContentTokenCount_documented_subset_of_promptTokenCount;"
                    "no_cache_write_field"
                ),
                output_base_field="candidatesTokenCount",
                reasoning_tokens_field="thoughtsTokenCount",
                reasoning_inclusion="DISJOINT_EXCLUDED",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="NOT_APPLICABLE",
            ),
            "offline_replay": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-offline-replay-v1",
                provider_family="OFFLINE_REPLAY",
                policy_version="v1",
                documentation_source=(
                    "IdentityBench providers.base.Usage contract "
                    "(input_tokens excludes cache_read_tokens and cache_write_tokens; "
                    "output_tokens includes reasoning_tokens)"
                ),
                default_usage_scope="PHYSICAL_REQUEST",
                input_base_field="input_tokens",
                cached_input_field="cache_read_tokens",
                cached_input_inclusion="DISJOINT_EXCLUDED",
                cache_write_input_field="cache_write_tokens",
                cache_write_input_inclusion="DISJOINT_EXCLUDED",
                cache_billing_semantics=(
                    "input_tokens_excludes_cache_read_tokens_and_cache_write_tokens;"
                    "disjoint_additive"
                ),
                output_base_field="output_tokens",
                reasoning_tokens_field="reasoning_tokens",
                reasoning_inclusion="INCLUDED",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="NOT_APPLICABLE",
            ),
            "unknown_mapping": ProviderUsageAccountingPolicyV5(
                policy_id="identitybench-v5-usage-unknown-mapping-v1",
                provider_family="UNKNOWN_MAPPING",
                policy_version="v1",
                documentation_source="UNSPECIFIED_OR_UNDOCUMENTED_MAPPING",
                default_usage_scope="UNAVAILABLE",
                input_base_field=None,
                cached_input_field=None,
                cached_input_inclusion="UNKNOWN",
                cache_write_input_field=None,
                cache_write_input_inclusion="UNKNOWN",
                cache_billing_semantics="UNKNOWN",
                output_base_field=None,
                reasoning_tokens_field=None,
                reasoning_inclusion="UNKNOWN",
                tool_serialization_tokens_field=None,
                tool_serialization_inclusion="UNKNOWN",
            ),
        }
    )
)


def get_provider_usage_policy_v5(key_or_family: str) -> ProviderUsageAccountingPolicyV5:
    """Return a frozen documented usage-normalization policy by registry key or family."""
    normalized = key_or_family.strip().lower()
    if normalized in CANONICAL_PROVIDER_USAGE_POLICIES_V5:
        return CANONICAL_PROVIDER_USAGE_POLICIES_V5[normalized]
    upper = key_or_family.strip().upper()
    for policy in CANONICAL_PROVIDER_USAGE_POLICIES_V5.values():
        if policy.provider_family == upper:
            return policy
    raise KeyError(f"unknown v5 provider usage accounting policy: {key_or_family!r}")


class FrozenPricingRateV5(StrictV5Model):
    """Frozen per-million-token pricing rate anchored to a pricing manifest SHA-256."""

    pricing_manifest_sha256: StrictStr
    currency_code: StrictStr = Field(min_length=1, max_length=16)
    input_cost_per_million_micros: StrictInt = Field(ge=0)
    cached_input_cost_per_million_micros: StrictInt | None = Field(default=None, ge=0)
    cache_write_cost_per_million_micros: StrictInt | None = Field(default=None, ge=0)
    output_cost_per_million_micros: StrictInt = Field(ge=0)
    reasoning_cost_per_million_micros: StrictInt | None = Field(default=None, ge=0)


class CurrencyConversionRateV5(StrictV5Model):
    """Explicit frozen currency conversion ratio with provenance source."""

    from_currency: StrictStr = Field(min_length=1, max_length=16)
    to_currency: StrictStr = Field(min_length=1, max_length=16)
    rate_numerator: StrictInt = Field(ge=1)
    rate_denominator: StrictInt = Field(ge=1)
    rate_source: StrictStr = Field(min_length=1, max_length=256)
    pricing_manifest_sha256: StrictStr | None = None


class NormalizedUsageBreakdownV5(StrictV5Model):
    """Normalized inclusive token totals, verified subsets, scope, and verbatim raw JSON."""

    input_tokens_total: StrictInt | None = Field(default=None, ge=0)
    cached_input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens_total: StrictInt | None = Field(default=None, ge=0)
    reasoning_tokens_subset: StrictInt | None = Field(default=None, ge=0)
    tool_serialization_tokens_subset: StrictInt | None = Field(default=None, ge=0)
    usage_scope: UsageScopeV5
    raw_usage_fields_json: StrictStr | None = None


def _lookup_raw_field(raw_usage: Mapping[str, Any], dotted_path: str) -> tuple[bool, Any]:
    current: Any = raw_usage
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _as_non_neg_int(value: Any) -> tuple[bool, int | None]:
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False, None
    return True, value


def _normalize_input_tokens(
    raw_usage: Mapping[str, Any],
    policy: ProviderUsageAccountingPolicyV5,
) -> tuple[int | None, int | None]:
    if (
        policy.input_base_field is None
        or policy.cached_input_inclusion == "UNKNOWN"
        or policy.cache_write_input_inclusion == "UNKNOWN"
    ):
        return None, None

    found_base, raw_base_obj = _lookup_raw_field(raw_usage, policy.input_base_field)
    valid_base, raw_base = _as_non_neg_int(raw_base_obj)
    if not found_base or not valid_base or raw_base is None:
        return None, None

    cached_val, add_cached, cached_ok = _resolve_component(
        raw_usage=raw_usage,
        field_path=policy.cached_input_field,
        inclusion=policy.cached_input_inclusion,
    )
    if not cached_ok:
        return None, None

    cache_write_val, add_cache_write, cache_write_ok = _resolve_component(
        raw_usage=raw_usage,
        field_path=policy.cache_write_input_field,
        inclusion=policy.cache_write_input_inclusion,
    )
    if not cache_write_ok:
        return None, None

    total_in = raw_base + add_cached + add_cache_write
    if cached_val is not None and cached_val > total_in:
        return None, None
    if cache_write_val is not None and cache_write_val > total_in:
        return None, None
    if (
        policy.cached_input_inclusion == "INCLUDED"
        and policy.cache_write_input_inclusion == "INCLUDED"
        and cached_val is not None
        and cache_write_val is not None
        and cached_val + cache_write_val > total_in
    ):
        return None, None

    return total_in, cached_val


def _normalize_output_tokens(
    raw_usage: Mapping[str, Any],
    policy: ProviderUsageAccountingPolicyV5,
) -> tuple[int | None, int | None, int | None]:
    if (
        policy.output_base_field is None
        or policy.reasoning_inclusion == "UNKNOWN"
        or policy.tool_serialization_inclusion == "UNKNOWN"
    ):
        return None, None, None

    found_base, raw_base_obj = _lookup_raw_field(raw_usage, policy.output_base_field)
    valid_base, raw_base = _as_non_neg_int(raw_base_obj)
    if not found_base or not valid_base or raw_base is None:
        return None, None, None

    reasoning_val, add_reasoning, reasoning_ok = _resolve_component(
        raw_usage=raw_usage,
        field_path=policy.reasoning_tokens_field,
        inclusion=policy.reasoning_inclusion,
    )
    if not reasoning_ok:
        return None, None, None

    tool_ser_val, add_tool_ser, tool_ser_ok = _resolve_component(
        raw_usage=raw_usage,
        field_path=policy.tool_serialization_tokens_field,
        inclusion=policy.tool_serialization_inclusion,
    )
    if not tool_ser_ok:
        return None, None, None

    total_out = raw_base + add_reasoning + add_tool_ser
    if reasoning_val is not None and reasoning_val > total_out:
        return None, None, None
    if tool_ser_val is not None and tool_ser_val > total_out:
        return None, None, None

    return total_out, reasoning_val, tool_ser_val


def _resolve_component(
    *,
    raw_usage: Mapping[str, Any],
    field_path: str | None,
    inclusion: UsageInclusionSemanticsV5,
) -> tuple[int | None, int, bool]:
    if inclusion == "NOT_APPLICABLE":
        return None, 0, True
    if inclusion == "UNKNOWN" or field_path is None:
        return None, 0, inclusion == "INCLUDED" and field_path is None

    found, raw_obj = _lookup_raw_field(raw_usage, field_path)
    valid, parsed = _as_non_neg_int(raw_obj)
    if not valid:
        return None, 0, False

    if inclusion == "INCLUDED":
        if not found or parsed is None:
            return None, 0, True
        return parsed, 0, True

    # DISJOINT_EXCLUDED requires the documented disjoint component to be explicitly present.
    if not found or parsed is None:
        return None, 0, False
    return parsed, parsed, True


def normalize_raw_usage_v5(
    raw_usage: Mapping[str, Any] | None,
    policy: ProviderUsageAccountingPolicyV5,
    *,
    usage_scope_override: UsageScopeV5 | None = None,
) -> NormalizedUsageBreakdownV5:
    """Normalize a raw provider usage payload strictly from a frozen documented policy."""
    if raw_usage is None:
        return NormalizedUsageBreakdownV5(
            input_tokens_total=None,
            cached_input_tokens=None,
            output_tokens_total=None,
            reasoning_tokens_subset=None,
            tool_serialization_tokens_subset=None,
            usage_scope=usage_scope_override or "UNAVAILABLE",
            raw_usage_fields_json=None,
        )

    raw_json = jcs_dumps(dict(raw_usage)).decode("utf-8")
    scope = usage_scope_override if usage_scope_override is not None else policy.default_usage_scope
    if scope == "UNAVAILABLE":
        return NormalizedUsageBreakdownV5(
            input_tokens_total=None,
            cached_input_tokens=None,
            output_tokens_total=None,
            reasoning_tokens_subset=None,
            tool_serialization_tokens_subset=None,
            usage_scope="UNAVAILABLE",
            raw_usage_fields_json=raw_json,
        )

    input_total, cached_input = _normalize_input_tokens(raw_usage, policy)
    output_total, reasoning_subset, tool_ser_subset = _normalize_output_tokens(raw_usage, policy)
    return NormalizedUsageBreakdownV5(
        input_tokens_total=input_total,
        cached_input_tokens=cached_input,
        output_tokens_total=output_total,
        reasoning_tokens_subset=reasoning_subset,
        tool_serialization_tokens_subset=tool_ser_subset,
        usage_scope=scope,
        raw_usage_fields_json=raw_json,
    )


def estimate_frozen_cost_micros_v5(
    usage: NormalizedUsageBreakdownV5 | ProviderRequestUsageV5,
    pricing_rate: FrozenPricingRateV5,
    *,
    raw_usage: Mapping[str, Any] | None = None,
    policy: ProviderUsageAccountingPolicyV5 | None = None,
) -> int | None:
    """Estimate cost in integer micros without double-charging subsets or guessing."""
    if usage.input_tokens_total is None or usage.output_tokens_total is None:
        return None

    remaining_input = usage.input_tokens_total
    cached_tokens = 0
    if pricing_rate.cached_input_cost_per_million_micros is not None:
        if usage.cached_input_tokens is None:
            return None
        cached_tokens = usage.cached_input_tokens
        remaining_input -= cached_tokens

    cache_write_tokens = 0
    if pricing_rate.cache_write_cost_per_million_micros is not None:
        if policy is None:
            return None
        if policy.cache_write_input_inclusion == "NOT_APPLICABLE":
            cache_write_tokens = 0
        elif (
            policy.cache_write_input_inclusion in {"INCLUDED", "DISJOINT_EXCLUDED"}
            and policy.cache_write_input_field is not None
            and raw_usage is not None
        ):
            found, raw_cw = _lookup_raw_field(raw_usage, policy.cache_write_input_field)
            valid, parsed_cw = _as_non_neg_int(raw_cw)
            if not found or not valid or parsed_cw is None:
                return None
            cache_write_tokens = parsed_cw
            remaining_input -= cache_write_tokens
        else:
            return None

    if remaining_input < 0:
        return None

    remaining_output = usage.output_tokens_total
    reasoning_tokens = 0
    if pricing_rate.reasoning_cost_per_million_micros is not None:
        if usage.reasoning_tokens_subset is None:
            return None
        reasoning_tokens = usage.reasoning_tokens_subset
        remaining_output -= reasoning_tokens

    if remaining_output < 0:
        return None

    input_num = (
        remaining_input * pricing_rate.input_cost_per_million_micros
        + cached_tokens * (pricing_rate.cached_input_cost_per_million_micros or 0)
        + cache_write_tokens * (pricing_rate.cache_write_cost_per_million_micros or 0)
    )
    output_num = (
        remaining_output * pricing_rate.output_cost_per_million_micros
        + reasoning_tokens * (pricing_rate.reasoning_cost_per_million_micros or 0)
    )
    return (input_num + output_num + 500_000) // 1_000_000


def build_provider_request_usage_v5(
    *,
    request_id: str,
    logical_request_id: str,
    request_kind: Literal[
        "INITIAL",
        "TRANSIENT_RETRY",
        "QUOTA_CONTINUATION",
        "RECOVERY_CONTINUATION",
        "SEGMENT_CONTINUATION",
    ],
    retry_index: int | None = None,
    quota_continuation_index: int | None = None,
    recovery_continuation_index: int | None = None,
    segment_continuation_index: int | None = None,
    decode_segment_index: int = 0,
    attempt_id: str,
    agent_id: str,
    role: str,
    dispatch_round: int,
    reservation_ordinal: tuple[int, int, int, str, int],
    request_status: Literal[
        "COMPLETED",
        "TRANSIENT_FAILURE",
        "QUOTA_INTERRUPTION",
        "NONTRANSIENT_FAILURE",
        "AMBIGUOUS_FAILURE",
        "CANCELLED",
    ],
    post_terminal: bool = False,
    provider_stop_reason: str | None = None,
    provider_http_status: int | None = None,
    provider_error_code: str | None = None,
    provider_decode_status_raw: str | None = None,
    normalized_decode_status: Literal[
        "SEGMENT_REQUIRES_CONTINUATION",
        "TERMINAL_NORMAL",
        "TERMINAL_REFUSAL",
        "TERMINAL_OUTPUT_LIMIT",
        "NO_RESPONSE",
        "UNKNOWN",
    ],
    provider_refusal_signal: bool | None = None,
    actual_native_numeric_budget_tokens: int | None = None,
    native_generation_parameters_json: str | None = None,
    rendered_input_context_tokens: int | None = None,
    effective_output_allowance_tokens: int | None = None,
    raw_usage: Mapping[str, Any] | None,
    policy: ProviderUsageAccountingPolicyV5,
    usage_scope_override: UsageScopeV5 | None = None,
    provider_reported_cost_micros: int | None = None,
    currency_code: str | None = None,
    pricing_rate: FrozenPricingRateV5 | None = None,
) -> ProviderRequestUsageV5:
    """Normalize raw usage and cost provenance into a validated ``ProviderRequestUsageV5``."""
    normalized = normalize_raw_usage_v5(
        raw_usage,
        policy,
        usage_scope_override=usage_scope_override,
    )

    cost_basis: CostBasisV5
    billed_micros: int | None = None
    estimated_micros: int | None = None
    pricing_sha256: str | None = None
    resolved_currency: str | None = currency_code

    if provider_reported_cost_micros is not None:
        if (
            isinstance(provider_reported_cost_micros, bool)
            or not isinstance(provider_reported_cost_micros, int)
            or provider_reported_cost_micros < 0
        ):
            raise ValueError("provider_reported_cost_micros must be a non-negative integer")
        if not resolved_currency:
            raise ValueError("PROVIDER_BILLED cost requires a non-empty currency_code")
        cost_basis = "PROVIDER_BILLED"
        billed_micros = provider_reported_cost_micros
    elif pricing_rate is not None:
        if resolved_currency is not None and resolved_currency != pricing_rate.currency_code:
            raise ValueError(
                f"currency_code {resolved_currency!r} conflicts with pricing_rate currency "
                f"{pricing_rate.currency_code!r}"
            )
        estimated_micros = estimate_frozen_cost_micros_v5(
            normalized,
            pricing_rate,
            raw_usage=raw_usage,
            policy=policy,
        )
        if estimated_micros is not None:
            cost_basis = "FROZEN_RATE_ESTIMATE"
            pricing_sha256 = pricing_rate.pricing_manifest_sha256
            resolved_currency = pricing_rate.currency_code
        else:
            cost_basis = "UNAVAILABLE"
    else:
        cost_basis = "UNAVAILABLE"

    return ProviderRequestUsageV5(
        request_id=request_id,
        logical_request_id=logical_request_id,
        request_kind=request_kind,
        retry_index=retry_index,
        quota_continuation_index=quota_continuation_index,
        recovery_continuation_index=recovery_continuation_index,
        segment_continuation_index=segment_continuation_index,
        decode_segment_index=decode_segment_index,
        attempt_id=attempt_id,
        agent_id=agent_id,
        role=role,
        dispatch_round=dispatch_round,
        reservation_ordinal=reservation_ordinal,
        request_status=request_status,
        post_terminal=post_terminal,
        provider_stop_reason=provider_stop_reason,
        provider_http_status=provider_http_status,
        provider_error_code=provider_error_code,
        provider_decode_status_raw=provider_decode_status_raw,
        normalized_decode_status=normalized_decode_status,
        provider_refusal_signal=provider_refusal_signal,
        actual_native_numeric_budget_tokens=actual_native_numeric_budget_tokens,
        native_generation_parameters_json=native_generation_parameters_json,
        rendered_input_context_tokens=rendered_input_context_tokens,
        effective_output_allowance_tokens=effective_output_allowance_tokens,
        input_tokens_total=normalized.input_tokens_total,
        cached_input_tokens=normalized.cached_input_tokens,
        output_tokens_total=normalized.output_tokens_total,
        reasoning_tokens_subset=normalized.reasoning_tokens_subset,
        tool_serialization_tokens_subset=normalized.tool_serialization_tokens_subset,
        provider_reported_cost_micros=billed_micros,
        estimated_cost_micros=estimated_micros,
        cost_basis=cost_basis,
        pricing_manifest_sha256=pricing_sha256,
        raw_usage_fields_json=normalized.raw_usage_fields_json,
        usage_scope=normalized.usage_scope,
        currency_code=resolved_currency,
    )


def compute_active_elapsed_ms(intervals_ms: Sequence[tuple[int, int]]) -> int:
    """Compute the exact union length in ms across ``(start_ms, end_ms)`` intervals.

    Overlapping concurrent intervals (parallel requests or overlapping tool calls) are merged
    before summing so concurrency is never double-counted; quota pauses, harness queueing, and
    idle operator intervals are excluded by omitting them from ``intervals_ms``.
    """
    if not intervals_ms:
        return 0
    normalized: list[tuple[int, int]] = []
    for start_ms, end_ms in intervals_ms:
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
        ):
            raise ValueError("interval endpoints must be integer milliseconds")
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError(f"invalid active interval [{start_ms}, {end_ms}]")
        normalized.append((start_ms, end_ms))

    normalized.sort()
    total_ms = 0
    cur_start, cur_end = normalized[0]
    for start_ms, end_ms in normalized[1:]:
        if start_ms <= cur_end:
            if end_ms > cur_end:
                cur_end = end_ms
        else:
            total_ms += cur_end - cur_start
            cur_start, cur_end = start_ms, end_ms
    total_ms += cur_end - cur_start
    return total_ms


def compute_attempt_timings_ms_v5(
    *,
    first_dispatch_ms: int,
    terminal_outcome_ms: int | None,
    final_persisted_ms: int | None,
    active_intervals_ms: Sequence[tuple[int, int]],
) -> tuple[int | None, int | None, int]:
    """Compute ``(elapsed_wall_time_ms, terminal_outcome_elapsed_ms, active_elapsed_ms)``."""
    if isinstance(first_dispatch_ms, bool) or first_dispatch_ms < 0:
        raise ValueError("first_dispatch_ms must be a non-negative integer")
    active_ms = compute_active_elapsed_ms(active_intervals_ms)
    terminal_elapsed: int | None = None
    if terminal_outcome_ms is not None:
        if isinstance(terminal_outcome_ms, bool) or terminal_outcome_ms < first_dispatch_ms:
            raise ValueError("terminal_outcome_ms must be >= first_dispatch_ms")
        terminal_elapsed = terminal_outcome_ms - first_dispatch_ms
    wall_elapsed: int | None = None
    if final_persisted_ms is not None:
        if isinstance(final_persisted_ms, bool) or final_persisted_ms < first_dispatch_ms:
            raise ValueError("final_persisted_ms must be >= first_dispatch_ms")
        if terminal_outcome_ms is not None and final_persisted_ms < terminal_outcome_ms:
            raise ValueError("final_persisted_ms must be >= terminal_outcome_ms")
        wall_elapsed = final_persisted_ms - first_dispatch_ms
        if active_ms > wall_elapsed:
            raise ValueError("active_elapsed_ms cannot exceed elapsed_wall_time_ms")
    return wall_elapsed, terminal_elapsed, active_ms


def _delta_optional(cur: int | None, prev: int | None, label: str) -> int | None:
    if cur is None or prev is None:
        return None
    delta = cur - prev
    if delta < 0:
        raise ValueError(f"cumulative {label} decreased from {prev} to {cur}")
    return delta


def derive_physical_segment_usage_from_cumulative_v5(
    cumulative_segments: Sequence[ProviderRequestUsageV5],
) -> tuple[ProviderRequestUsageV5, ...]:
    """Derive per-segment ``PHYSICAL_REQUEST`` deltas from a cumulative series."""
    if not cumulative_segments:
        return ()
    ordered = sorted(cumulative_segments, key=lambda item: item.decode_segment_index)
    logical_ids = {item.logical_request_id for item in ordered}
    if len(logical_ids) != 1:
        raise ValueError("cumulative derivation requires a single logical_request_id")
    seen_indices: set[int] = set()
    derived: list[ProviderRequestUsageV5] = []
    prev: ProviderRequestUsageV5 | None = None
    for row in ordered:
        if row.usage_scope != "LOGICAL_RESPONSE_CUMULATIVE":
            raise ValueError("expected LOGICAL_RESPONSE_CUMULATIVE rows for cumulative derivation")
        if row.decode_segment_index in seen_indices:
            raise ValueError(f"duplicate decode_segment_index {row.decode_segment_index}")
        seen_indices.add(row.decode_segment_index)
        if prev is None:
            derived.append(row.model_copy(update={"usage_scope": "PHYSICAL_REQUEST"}))
        else:
            if row.currency_code != prev.currency_code or row.cost_basis != prev.cost_basis:
                raise ValueError("cumulative segment cost_basis/currency_code must match")
            derived.append(
                row.model_copy(
                    update={
                        "usage_scope": "PHYSICAL_REQUEST",
                        "input_tokens_total": _delta_optional(
                            row.input_tokens_total, prev.input_tokens_total, "input_tokens_total"
                        ),
                        "cached_input_tokens": _delta_optional(
                            row.cached_input_tokens,
                            prev.cached_input_tokens,
                            "cached_input_tokens",
                        ),
                        "output_tokens_total": _delta_optional(
                            row.output_tokens_total,
                            prev.output_tokens_total,
                            "output_tokens_total",
                        ),
                        "reasoning_tokens_subset": _delta_optional(
                            row.reasoning_tokens_subset,
                            prev.reasoning_tokens_subset,
                            "reasoning_tokens_subset",
                        ),
                        "tool_serialization_tokens_subset": _delta_optional(
                            row.tool_serialization_tokens_subset,
                            prev.tool_serialization_tokens_subset,
                            "tool_serialization_tokens_subset",
                        ),
                        "provider_reported_cost_micros": _delta_optional(
                            row.provider_reported_cost_micros,
                            prev.provider_reported_cost_micros,
                            "provider_reported_cost_micros",
                        ),
                        "estimated_cost_micros": _delta_optional(
                            row.estimated_cost_micros,
                            prev.estimated_cost_micros,
                            "estimated_cost_micros",
                        ),
                    }
                )
            )
        prev = row
    return tuple(derived)


@dataclass(frozen=True)
class _ScopedUsageSelection:
    contributing: tuple[ProviderRequestUsageV5, ...]
    skipped_redundant_cumulative: tuple[ProviderRequestUsageV5, ...]
    ambiguous_cumulative_logical_ids: tuple[str, ...]


def _partition_scoped_usage(requests: Sequence[ProviderRequestUsageV5]) -> _ScopedUsageSelection:
    by_logical: dict[str, list[ProviderRequestUsageV5]] = {}
    for req in requests:
        by_logical.setdefault(req.logical_request_id, []).append(req)

    contributing: list[ProviderRequestUsageV5] = []
    skipped_redundant: list[ProviderRequestUsageV5] = []
    ambiguous_logical_ids: list[str] = []

    for logical_id, group in by_logical.items():
        physical_or_unavail = [
            r for r in group if r.usage_scope in {"PHYSICAL_REQUEST", "UNAVAILABLE"}
        ]
        cumulative = [r for r in group if r.usage_scope == "LOGICAL_RESPONSE_CUMULATIVE"]

        if physical_or_unavail:
            contributing.extend(physical_or_unavail)
            skipped_redundant.extend(cumulative)
        elif len(cumulative) == 1:
            contributing.append(cumulative[0])
        elif len(cumulative) > 1:
            # Multiple cumulative rows without physical segment rows cannot be summed
            # without guessing; retain one placeholder as unnormalized or flag ambiguity.
            ambiguous_logical_ids.append(logical_id)
            skipped_redundant.extend(cumulative)

    return _ScopedUsageSelection(
        contributing=tuple(contributing),
        skipped_redundant_cumulative=tuple(skipped_redundant),
        ambiguous_cumulative_logical_ids=tuple(sorted(ambiguous_logical_ids)),
    )


def select_contributing_usage_records_v5(
    requests: Sequence[ProviderRequestUsageV5],
) -> tuple[ProviderRequestUsageV5, ...]:
    """Return non-redundant usage rows for aggregation without cumulative double counting."""
    return _partition_scoped_usage(requests).contributing


def _convert_micros(
    amount_micros: int,
    from_currency: str,
    target_currency: str,
    conversion_index: Mapping[tuple[str, str], CurrencyConversionRateV5],
) -> int:
    if from_currency == target_currency:
        return amount_micros
    rate = conversion_index.get((from_currency, target_currency))
    if rate is None:
        raise ValueError(
            f"cannot sum costs across mismatched currencies ({from_currency!r} vs "
            f"{target_currency!r}) without an explicit frozen conversion rate and source"
        )
    return (
        amount_micros * rate.rate_numerator + rate.rate_denominator // 2
    ) // rate.rate_denominator


def _build_conversion_index(
    conversions: Sequence[CurrencyConversionRateV5],
) -> dict[tuple[str, str], CurrencyConversionRateV5]:
    index: dict[tuple[str, str], CurrencyConversionRateV5] = {}
    for rate in conversions:
        key = (rate.from_currency, rate.to_currency)
        if key in index:
            raise ValueError(f"duplicate currency conversion rate for {key!r}")
        index[key] = rate
    return index


def _sum_optional_ints(values: Sequence[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


@dataclass(frozen=True)
class _AggregatedSummaryValues:
    coverage_status: AccountingCoverageStatusV5
    total_input_tokens: int | None
    cached_input_tokens: int | None
    total_output_tokens: int | None
    reasoning_tokens_subset: int | None
    tool_serialization_tokens_subset: int | None
    provider_billed_cost_micros: int | None
    estimated_cost_micros: int | None
    currency_code: str | None
    elapsed_wall_time_ms: int | None
    active_elapsed_ms: int | None
    ambiguous_cumulative_logical_ids: tuple[str, ...]


def _aggregate_attempts_usage(
    attempts: Sequence[EpisodeAttemptV5],
    *,
    has_missing_slots: bool,
    target_currency: str | None,
    currency_conversions: Sequence[CurrencyConversionRateV5],
) -> _AggregatedSummaryValues:
    all_requests = [req for attempt in attempts for req in attempt.provider_requests]
    scoped = _partition_scoped_usage(all_requests)
    contributing = scoped.contributing

    conversion_index = _build_conversion_index(currency_conversions)
    cost_rows = [
        req
        for req in contributing
        if (req.cost_basis == "PROVIDER_BILLED" and req.provider_reported_cost_micros is not None)
        or (req.cost_basis == "FROZEN_RATE_ESTIMATE" and req.estimated_cost_micros is not None)
    ]
    currencies = {req.currency_code for req in cost_rows}
    if None in currencies or "" in currencies:
        raise ValueError("cost-bearing usage records require a non-empty currency_code")

    resolved_currency: str | None = target_currency
    if resolved_currency is None:
        if len(currencies) > 1:
            currency_list = sorted(c for c in currencies if c)
            raise ValueError(
                f"cannot sum costs across mismatched currencies {currency_list} "
                "without an explicit frozen conversion rate and target_currency"
            )
        if len(currencies) == 1:
            resolved_currency = next(iter(currencies))

    billed_values: list[int] = []
    estimated_values: list[int] = []
    for req in cost_rows:
        assert resolved_currency is not None
        assert req.currency_code is not None
        if req.cost_basis == "PROVIDER_BILLED" and req.provider_reported_cost_micros is not None:
            billed_values.append(
                _convert_micros(
                    req.provider_reported_cost_micros,
                    req.currency_code,
                    resolved_currency,
                    conversion_index,
                )
            )
        if req.cost_basis == "FROZEN_RATE_ESTIMATE" and req.estimated_cost_micros is not None:
            estimated_values.append(
                _convert_micros(
                    req.estimated_cost_micros,
                    req.currency_code,
                    resolved_currency,
                    conversion_index,
                )
            )

    def _has_full_token_and_cost(req: ProviderRequestUsageV5) -> bool:
        token_ok = (
            req.usage_scope == "PHYSICAL_REQUEST"
            and req.input_tokens_total is not None
            and req.output_tokens_total is not None
        )
        cost_ok = (
            req.cost_basis == "PROVIDER_BILLED" and req.provider_reported_cost_micros is not None
        ) or (
            req.cost_basis == "FROZEN_RATE_ESTIMATE" and req.estimated_cost_micros is not None
        )
        return token_ok and cost_ok

    def _has_any_known_metric(req: ProviderRequestUsageV5) -> bool:
        return any(
            v is not None
            for v in (
                req.input_tokens_total,
                req.output_tokens_total,
                req.provider_reported_cost_micros,
                req.estimated_cost_micros,
            )
        )

    any_known = any(_has_any_known_metric(req) for req in contributing)
    all_complete = (
        len(contributing) > 0
        and not has_missing_slots
        and not scoped.ambiguous_cumulative_logical_ids
        and all(_has_full_token_and_cost(req) for req in contributing)
    )

    coverage: AccountingCoverageStatusV5
    if not any_known:
        coverage = "UNAVAILABLE"
    elif all_complete:
        coverage = "COMPLETE"
    else:
        coverage = "PARTIAL"

    wall_ms = _sum_optional_ints([a.elapsed_wall_time_ms for a in attempts])
    active_ms = _sum_optional_ints([a.active_elapsed_ms for a in attempts])

    if coverage == "UNAVAILABLE":
        return _AggregatedSummaryValues(
            coverage_status="UNAVAILABLE",
            total_input_tokens=None,
            cached_input_tokens=None,
            total_output_tokens=None,
            reasoning_tokens_subset=None,
            tool_serialization_tokens_subset=None,
            provider_billed_cost_micros=None,
            estimated_cost_micros=None,
            currency_code=resolved_currency,
            elapsed_wall_time_ms=wall_ms,
            active_elapsed_ms=active_ms,
            ambiguous_cumulative_logical_ids=scoped.ambiguous_cumulative_logical_ids,
        )

    valid_scope_rows = [r for r in contributing if r.usage_scope != "UNAVAILABLE"]
    return _AggregatedSummaryValues(
        coverage_status=coverage,
        total_input_tokens=_sum_optional_ints([r.input_tokens_total for r in valid_scope_rows]),
        cached_input_tokens=_sum_optional_ints([r.cached_input_tokens for r in valid_scope_rows]),
        total_output_tokens=_sum_optional_ints([r.output_tokens_total for r in valid_scope_rows]),
        reasoning_tokens_subset=_sum_optional_ints(
            [r.reasoning_tokens_subset for r in valid_scope_rows]
        ),
        tool_serialization_tokens_subset=_sum_optional_ints(
            [r.tool_serialization_tokens_subset for r in valid_scope_rows]
        ),
        provider_billed_cost_micros=sum(billed_values) if billed_values else None,
        estimated_cost_micros=sum(estimated_values) if estimated_values else None,
        currency_code=resolved_currency,
        elapsed_wall_time_ms=wall_ms,
        active_elapsed_ms=active_ms,
        ambiguous_cumulative_logical_ids=scoped.ambiguous_cumulative_logical_ids,
    )


def _compute_status_counts(
    *,
    planned_attempt_slots: int,
    attempts: Sequence[EpisodeAttemptV5],
    missing_slot_status: IncompleteAttemptStatusV5 | None,
) -> tuple[dict[str, int], bool]:
    if planned_attempt_slots < 0:
        raise ValueError("planned_attempt_slots must be non-negative")
    if len(attempts) > planned_attempt_slots:
        raise ValueError(
            f"received {len(attempts)} attempts for only {planned_attempt_slots} planned slots"
        )
    has_missing_slots = len(attempts) < planned_attempt_slots
    if has_missing_slots and missing_slot_status is None:
        raise ValueError(
            f"expected {planned_attempt_slots} materialized attempts, got {len(attempts)}"
        )

    counts: dict[str, int] = {status: 0 for status in CANONICAL_ATTEMPT_STATUSES_V5}
    for attempt in attempts:
        counts[attempt.attempt_status] += 1
    if has_missing_slots:
        assert missing_slot_status is not None
        counts[missing_slot_status] += planned_attempt_slots - len(attempts)
    return counts, has_missing_slots


def summarize_descriptive_usage_v5(
    run_config_sha256: str,
    planned_attempt_slots: int,
    attempts: Sequence[EpisodeAttemptV5],
    *,
    missing_slot_status: IncompleteAttemptStatusV5 | None = "INCOMPLETE_HARNESS_FAILURE",
    target_currency: str | None = None,
    currency_conversions: Sequence[CurrencyConversionRateV5] = (),
) -> DescriptiveUsageSummaryV5:
    """Aggregate ``EpisodeAttemptV5`` records into a ``DescriptiveUsageSummaryV5``."""
    seen_attempt_ids: set[str] = set()
    seen_slots: set[tuple[str, int]] = set()
    for attempt in attempts:
        if attempt.run_config_sha256 != run_config_sha256:
            raise ValueError(
                f"attempt {attempt.attempt_id!r} run_config_sha256 does not match summary"
            )
        if attempt.attempt_id in seen_attempt_ids:
            raise ValueError(f"duplicate attempt_id: {attempt.attempt_id!r}")
        seen_attempt_ids.add(attempt.attempt_id)
        slot_key = (attempt.episode_id, attempt.repeat_index)
        if slot_key in seen_slots:
            raise ValueError(f"duplicate (episode_id, repeat_index) slot: {slot_key!r}")
        seen_slots.add(slot_key)

    status_counts, has_missing_slots = _compute_status_counts(
        planned_attempt_slots=planned_attempt_slots,
        attempts=attempts,
        missing_slot_status=missing_slot_status,
    )
    aggregated = _aggregate_attempts_usage(
        attempts,
        has_missing_slots=has_missing_slots,
        target_currency=target_currency,
        currency_conversions=currency_conversions,
    )

    return DescriptiveUsageSummaryV5(
        run_config_sha256=run_config_sha256,
        planned_attempt_slots=planned_attempt_slots,
        completed_attempts=status_counts["COMPLETED"],
        incomplete_provider_quota_attempts=status_counts["INCOMPLETE_PROVIDER_QUOTA"],
        incomplete_provider_failure_attempts=status_counts["INCOMPLETE_PROVIDER_FAILURE"],
        incomplete_context_attempts=status_counts["INCOMPLETE_CONTEXT_INCOMPATIBLE"],
        incomplete_harness_attempts=status_counts["INCOMPLETE_HARNESS_FAILURE"],
        attempt_status_counts_json=jcs_dumps(status_counts).decode("utf-8"),
        elapsed_wall_time_ms=aggregated.elapsed_wall_time_ms,
        active_elapsed_ms=aggregated.active_elapsed_ms,
        total_input_tokens=aggregated.total_input_tokens,
        cached_input_tokens=aggregated.cached_input_tokens,
        total_output_tokens=aggregated.total_output_tokens,
        reasoning_tokens_subset=aggregated.reasoning_tokens_subset,
        tool_serialization_tokens_subset=aggregated.tool_serialization_tokens_subset,
        provider_billed_cost_micros=aggregated.provider_billed_cost_micros,
        estimated_cost_micros=aggregated.estimated_cost_micros,
        currency_code=aggregated.currency_code,
        accounting_coverage_status=aggregated.coverage_status,
    )


def _check_attempt_completeness_errors(attempts: Sequence[EpisodeAttemptV5]) -> list[str]:
    errors: list[str] = []
    seen_request_ids: set[str] = set()
    for attempt in attempts:
        req_ids_in_attempt = attempt.provider_requests
        transient_retries = sum(
            1 for r in req_ids_in_attempt if r.request_kind == "TRANSIENT_RETRY"
        )
        if attempt.retry_request_count != transient_retries:
            errors.append(
                f"attempt {attempt.attempt_id!r} retry_request_count={attempt.retry_request_count} "
                f"does not match TRANSIENT_RETRY records ({transient_retries})"
            )
        if attempt.completed_assistant_generations > 0 and not req_ids_in_attempt:
            errors.append(
                f"attempt {attempt.attempt_id!r} has {attempt.completed_assistant_generations} "
                "completed generations but zero provider_requests"
            )
        if (
            attempt.terminal_cause == "PROVIDER_REQUEST_POLICY_REJECTION"
            or attempt.context_limit_cause == "PROVIDER_REJECTION_CONFIRMED_LIMIT"
        ) and not req_ids_in_attempt:
            errors.append(
                f"attempt {attempt.attempt_id!r} records provider rejection without "
                "provider_requests telemetry"
            )
        if (
            attempt.elapsed_wall_time_ms is not None
            and attempt.active_elapsed_ms is not None
            and attempt.active_elapsed_ms > attempt.elapsed_wall_time_ms
        ):
            errors.append(
                f"attempt {attempt.attempt_id!r} active_elapsed_ms exceeds elapsed_wall_time_ms"
            )
        if (
            attempt.elapsed_wall_time_ms is not None
            and attempt.terminal_outcome_elapsed_ms is not None
            and attempt.terminal_outcome_elapsed_ms > attempt.elapsed_wall_time_ms
        ):
            errors.append(
                f"attempt {attempt.attempt_id!r} terminal_outcome_elapsed_ms exceeds "
                "elapsed_wall_time_ms"
            )

        by_logical: dict[str, list[ProviderRequestUsageV5]] = {}
        for req in req_ids_in_attempt:
            if req.attempt_id != attempt.attempt_id:
                errors.append(
                    f"request {req.request_id!r} attempt_id {req.attempt_id!r} != "
                    f"{attempt.attempt_id!r}"
                )
            if req.request_id in seen_request_ids:
                errors.append(f"duplicate physical request_id: {req.request_id!r}")
            seen_request_ids.add(req.request_id)
            by_logical.setdefault(req.logical_request_id, []).append(req)
            errors.extend(_check_request_row_invariants(req))

        for logical_id, group in by_logical.items():
            phys = [r for r in group if r.usage_scope == "PHYSICAL_REQUEST"]
            if any(r.request_kind == "SEGMENT_CONTINUATION" for r in phys):
                segs = sorted({r.decode_segment_index for r in phys})
                if segs != list(range(segs[-1] + 1)):
                    errors.append(
                        f"logical request {logical_id!r} has non-contiguous decode_segment_index "
                        f"coverage: {segs}"
                    )
    return errors


def _check_request_row_invariants(req: ProviderRequestUsageV5) -> list[str]:
    errors: list[str] = []
    if req.usage_scope == "UNAVAILABLE" and any(
        val is not None
        for val in (
            req.input_tokens_total,
            req.cached_input_tokens,
            req.output_tokens_total,
            req.reasoning_tokens_subset,
            req.tool_serialization_tokens_subset,
        )
    ):
        errors.append(
            f"request {req.request_id!r} has usage_scope=UNAVAILABLE with non-null tokens"
        )
    if req.raw_usage_fields_json is not None:
        try:
            decoded = json.loads(req.raw_usage_fields_json)
        except json.JSONDecodeError:
            errors.append(f"request {req.request_id!r} raw_usage_fields_json is invalid JSON")
        else:
            if not isinstance(decoded, dict):
                errors.append(
                    f"request {req.request_id!r} raw_usage_fields_json must decode to JSON object"
                )
    if req.cost_basis == "PROVIDER_BILLED":
        if req.provider_reported_cost_micros is None or not req.currency_code:
            errors.append(
                f"request {req.request_id!r} PROVIDER_BILLED requires cost and currency_code"
            )
    elif req.cost_basis == "FROZEN_RATE_ESTIMATE":
        if (
            req.estimated_cost_micros is None
            or not req.pricing_manifest_sha256
            or not req.currency_code
        ):
            errors.append(
                f"request {req.request_id!r} FROZEN_RATE_ESTIMATE requires cost, "
                "pricing_manifest_sha256, and currency_code"
            )
    elif req.cost_basis == "UNAVAILABLE" and (
        req.provider_reported_cost_micros is not None or req.estimated_cost_micros is not None
    ):
        errors.append(
            f"request {req.request_id!r} cost_basis=UNAVAILABLE must have null cost amounts"
        )
    return errors


def evaluate_g_econ_01_v5(
    attempts: Sequence[EpisodeAttemptV5],
    summary: DescriptiveUsageSummaryV5,
    *,
    target_currency: str | None = None,
    currency_conversions: Sequence[CurrencyConversionRateV5] = (),
    require_complete_coverage: bool = False,
) -> dict[str, Any]:
    """Evaluate ``SPEC-v5.0-draft.5.md`` §4.3 ``G-ECON-01`` completeness without spend caps."""
    errors: list[str] = []

    if len(attempts) != summary.planned_attempt_slots:
        errors.append(
            f"materialized attempts ({len(attempts)}) != planned_attempt_slots "
            f"({summary.planned_attempt_slots})"
        )
    for attempt in attempts:
        if attempt.run_config_sha256 != summary.run_config_sha256:
            errors.append(
                f"attempt {attempt.attempt_id!r} run_config_sha256 mismatches summary"
            )

    seen_attempts = {a.attempt_id for a in attempts}
    if len(seen_attempts) != len(attempts):
        errors.append("duplicate attempt_id detected in attempts")
    seen_slots = {(a.episode_id, a.repeat_index) for a in attempts}
    if len(seen_slots) != len(attempts):
        errors.append("duplicate (episode_id, repeat_index) slot detected in attempts")

    parsed_counts = json.loads(summary.attempt_status_counts_json)
    if set(parsed_counts) != set(_CANONICAL_ATTEMPT_STATUSES_V5):
        errors.append("attempt_status_counts_json must contain all 10 canonical attempt statuses")
    actual_counts = {
        status: sum(1 for a in attempts if a.attempt_status == status)
        for status in CANONICAL_ATTEMPT_STATUSES_V5
    }
    for status in CANONICAL_ATTEMPT_STATUSES_V5:
        if parsed_counts.get(status) != actual_counts[status]:
            errors.append(
                f"attempt_status_counts_json[{status!r}]={parsed_counts.get(status)!r} "
                f"!= actual count {actual_counts[status]}"
            )
    expected_completed = actual_counts["COMPLETED"]
    if summary.completed_attempts != expected_completed:
        errors.append(
            f"SC-03 violation: completed_attempts={summary.completed_attempts} != "
            f"count(COMPLETED)={expected_completed}"
        )

    errors.extend(_check_attempt_completeness_errors(attempts))

    has_missing_slots = len(attempts) < summary.planned_attempt_slots
    try:
        recomputed = _aggregate_attempts_usage(
            attempts,
            has_missing_slots=has_missing_slots,
            target_currency=target_currency,
            currency_conversions=currency_conversions,
        )
    except ValueError as exc:
        errors.append(str(exc))
        recomputed = None

    if recomputed is not None:
        if recomputed.ambiguous_cumulative_logical_ids:
            errors.append(
                "multiple LOGICAL_RESPONSE_CUMULATIVE rows without PHYSICAL_REQUEST rows for "
                f"logical_request_id(s): {recomputed.ambiguous_cumulative_logical_ids}"
            )
        checks_pairs = (
            (
                "accounting_coverage_status",
                summary.accounting_coverage_status,
                recomputed.coverage_status,
            ),
            ("total_input_tokens", summary.total_input_tokens, recomputed.total_input_tokens),
            ("cached_input_tokens", summary.cached_input_tokens, recomputed.cached_input_tokens),
            ("total_output_tokens", summary.total_output_tokens, recomputed.total_output_tokens),
            (
                "reasoning_tokens_subset",
                summary.reasoning_tokens_subset,
                recomputed.reasoning_tokens_subset,
            ),
            (
                "tool_serialization_tokens_subset",
                summary.tool_serialization_tokens_subset,
                recomputed.tool_serialization_tokens_subset,
            ),
            (
                "provider_billed_cost_micros",
                summary.provider_billed_cost_micros,
                recomputed.provider_billed_cost_micros,
            ),
            (
                "estimated_cost_micros",
                summary.estimated_cost_micros,
                recomputed.estimated_cost_micros,
            ),
            ("currency_code", summary.currency_code, recomputed.currency_code),
            ("elapsed_wall_time_ms", summary.elapsed_wall_time_ms, recomputed.elapsed_wall_time_ms),
            ("active_elapsed_ms", summary.active_elapsed_ms, recomputed.active_elapsed_ms),
        )
        for field_name, actual, expected in checks_pairs:
            if actual != expected:
                errors.append(
                    f"summary {field_name}={actual!r} mismatches scoped recomputation {expected!r}"
                )

    if summary.accounting_coverage_status == "UNAVAILABLE" and any(
        val is not None
        for val in (
            summary.total_input_tokens,
            summary.cached_input_tokens,
            summary.total_output_tokens,
            summary.reasoning_tokens_subset,
            summary.tool_serialization_tokens_subset,
            summary.provider_billed_cost_micros,
            summary.estimated_cost_micros,
        )
    ):
        errors.append("accounting_coverage_status=UNAVAILABLE must leave token/cost totals null")

    if require_complete_coverage and summary.accounting_coverage_status != "COMPLETE":
        errors.append(
            f"accounting_coverage_status={summary.accounting_coverage_status!r} != 'COMPLETE'"
        )

    passed = len(errors) == 0
    return {
        "gate_id": "G-ECON-01",
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "accounting_coverage_status": summary.accounting_coverage_status,
        "spend_or_token_ceiling_applied": False,
        "errors": errors,
        "descriptive_metrics": {
            "planned_attempt_slots": summary.planned_attempt_slots,
            "completed_attempts": summary.completed_attempts,
            "total_input_tokens": summary.total_input_tokens,
            "cached_input_tokens": summary.cached_input_tokens,
            "total_output_tokens": summary.total_output_tokens,
            "reasoning_tokens_subset": summary.reasoning_tokens_subset,
            "tool_serialization_tokens_subset": summary.tool_serialization_tokens_subset,
            "provider_billed_cost_micros": summary.provider_billed_cost_micros,
            "estimated_cost_micros": summary.estimated_cost_micros,
            "currency_code": summary.currency_code,
            "elapsed_wall_time_ms": summary.elapsed_wall_time_ms,
            "active_elapsed_ms": summary.active_elapsed_ms,
        },
    }
