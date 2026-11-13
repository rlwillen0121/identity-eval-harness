from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from identitybench.v5.provider_accounting import (
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
    V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
    V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
    AdapterRetryAccountingContractV5,
    UsageAccountingAttestationV5,
    UsageAccountingEvidenceV5,
    UsageAccountingManifestV5,
    effective_local_retry_contract_v5,
    provider_fingerprint_sha256_v5,
)


def _fingerprint(role: str = "unified") -> str:
    return provider_fingerprint_sha256_v5(
        role,
        "openai_compatible",
        "test-model",
        "a" * 16,
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
