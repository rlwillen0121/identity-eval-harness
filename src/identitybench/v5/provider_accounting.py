"""Explicit v5-only provider retry and token-accounting provenance contracts.

The shared provider adapters remain version-neutral. This module records the narrower evidence
that a v5 run needs before it can price tokens as exhaustive: local retry control must be
verifiably disabled, and an operator/gateway attestation must cover billed usage across all
completions and upstream retries. Neither condition alone establishes complete accounting.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, StrictInt, StrictStr, model_validator

from identitybench.v5.schemas import StrictV5Model

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
