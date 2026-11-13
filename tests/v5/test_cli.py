from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import identitybench.cli as root_cli
from identitybench.v4.jcs import jcs_dumps
from identitybench.v5.validation import V5ValidationReport


def test_v5_offline_command_skips_env_file_stat_and_load(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def forbidden_env_load(*_args, **_kwargs):
        calls.append(tmp_path)
        raise AssertionError("offline v5 command touched env loader")

    monkeypatch.setattr(root_cli, "load_env_file", forbidden_env_load)
    absent_env_file = tmp_path / "missing.env"
    missing_dataset = tmp_path / "missing-dataset"

    result = CliRunner().invoke(
        root_cli.app,
        [
            "--root",
            str(tmp_path),
            "--env-file",
            str(absent_env_file),
            "v5",
            "validate",
            "--dataset",
            str(missing_dataset),
            "--skip-baselines",
        ],
    )

    assert result.exit_code == 3, result.output
    assert "incomplete v5 dataset" in result.output
    assert "does not exist" not in result.output
    assert calls == []


def test_v5_build_refuses_heldout_write_without_freeze(tmp_path: Path) -> None:
    output = tmp_path / "heldout"

    result = CliRunner().invoke(
        root_cli.app,
        [
            "--root",
            str(tmp_path),
            "--no-env-file",
            "v5",
            "build",
            "--suite",
            "v5_full",
            "--out",
            str(output),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "requires explicit freeze=True" in result.output
    assert not output.exists()


def test_v5_command_group_is_registered() -> None:
    result = CliRunner().invoke(root_cli.app, ["v5", "--help"])

    assert result.exit_code == 0, result.output
    for command in ("build", "validate", "score", "run", "report"):
        assert command in result.output

    validate_help = CliRunner().invoke(
        root_cli.app,
        ["--no-env-file", "v5", "validate", "--help"],
    )
    assert validate_help.exit_code == 0, validate_help.output
    assert "reports/v5/conformance/<suite>/receipt.json" in validate_help.output


def test_validate_uses_suite_specific_default_blind_receipt(tmp_path: Path, monkeypatch) -> None:
    import identitybench.v5.validation as validation

    received: list[Path | None] = []

    def fake_validate(_dataset: Path, **kwargs) -> V5ValidationReport:
        received.append(kwargs["blind_proof_path"])
        return V5ValidationReport(
            suite_id="v5_dev",
            dataset_dir=str(tmp_path / "datasets" / "v5_dev"),
            episode_count=0,
            gate_results=(),
        )

    monkeypatch.setattr(validation, "validate_v5_dataset", fake_validate)
    result = CliRunner().invoke(
        root_cli.app,
        ["--root", str(tmp_path), "--no-env-file", "v5", "validate", "--skip-baselines"],
    )

    assert result.exit_code == 0, result.output
    assert received == [tmp_path / "reports" / "v5" / "conformance" / "v5_dev" / "receipt.json"]


def test_usage_accounting_manifest_is_verified_bound_and_copied(tmp_path: Path) -> None:
    from identitybench.providers.factory import ModelSpec
    from identitybench.v5.cli import (
        _bind_usage_accounting_to_provider,
        _copy_usage_accounting_artifacts,
        _load_usage_accounting_manifest,
    )
    from identitybench.v5.provider_accounting import (
        USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        provider_fingerprint_sha256_v5,
    )
    from identitybench.v5.runner import RunConfigV5

    provider_kind = "openai_compatible"
    model_id = "example-model"
    endpoint_identity = "sha256:non-secret-endpoint-identity"
    fingerprint = provider_fingerprint_sha256_v5(
        "unified", provider_kind, model_id, endpoint_identity
    )
    evidence = {
        "schema_version": V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        "role": "unified",
        "provider_kind": provider_kind,
        "model_id": model_id,
        "provider_fingerprint_sha256": fingerprint,
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "attestation": "operator_or_gateway",
    }
    evidence_raw = jcs_dumps(evidence)
    evidence_sha = __import__("hashlib").sha256(evidence_raw).hexdigest()
    manifest = {
        "schema_version": V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        "entries": [
            {
                "role": "unified",
                "provider_kind": provider_kind,
                "model_id": model_id,
                "provider_fingerprint_sha256": fingerprint,
                "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
                "evidence_path": "evidence/safe-alias.json",
                "evidence_sha256": evidence_sha,
                "adapter_retry_limit": 0,
            }
        ],
    }
    manifest_raw = jcs_dumps(manifest)
    manifest_path = tmp_path / "input" / "manifest.json"
    evidence_path = manifest_path.parent / "evidence" / "unified.json"
    evidence_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest_raw)
    evidence_path.write_bytes(evidence_raw)
    (evidence_path.parent / "safe-alias.json").symlink_to(evidence_path)
    model_spec = ModelSpec(provider=provider_kind, model=model_id)

    (
        loaded_manifest_raw,
        manifest_sha,
        typed_manifest,
        evidence_by_role,
        verified,
    ) = _load_usage_accounting_manifest(
        tmp_path,
        manifest_path,
        run_config=RunConfigV5(),
        model_spec=model_spec,
    )
    assert loaded_manifest_raw == manifest_raw
    assert manifest_sha == __import__("hashlib").sha256(manifest_raw).hexdigest()
    assert verified is not None

    class ProviderStub:
        kind = provider_kind
        model = model_id

        def endpoint_identity(self) -> str:
            return endpoint_identity

    bound_config = _bind_usage_accounting_to_provider(
        ProviderStub(), RunConfigV5(), typed_manifest
    )
    assert bound_config.usage_accounting_evidence[0].provider_fingerprint_sha256 == fingerprint
    copied_descriptors = _copy_usage_accounting_artifacts(
        tmp_path / "run", loaded_manifest_raw, typed_manifest, evidence_by_role
    )
    assert copied_descriptors[0]["evidence_path"] == (
        f"usage_accounting/evidence/{evidence_sha}.json"
    )
    assert (tmp_path / "run" / "usage_accounting" / "manifest.json").read_bytes() == manifest_raw
    copied_evidence = tmp_path / "run" / copied_descriptors[0]["evidence_path"]
    assert copied_evidence.read_bytes() == evidence_raw


def test_model_tier_is_explicit_and_included_in_canonical_config_hash(
    tmp_path: Path,
) -> None:
    from identitybench.v5.cli import _config_identity, _load_run_config

    config_path = tmp_path / "model.yaml"
    config_path.write_text(
        "model:\n  provider: openai_compatible\n  model: example-model\n"
        "model_tier: frontier\n",
        encoding="utf-8",
    )
    model_spec, run_config, model_tier = _load_run_config(config_path)
    assert model_tier == "frontier"

    frontier_identity = _config_identity(
        model_spec,
        run_config,
        model_tier=model_tier,
        pricing_manifest_sha256="a" * 64,
    )
    fast_identity = _config_identity(
        model_spec,
        run_config,
        model_tier="fast",
        pricing_manifest_sha256="a" * 64,
    )
    unclassified_identity = _config_identity(
        model_spec,
        run_config,
        model_tier=None,
        pricing_manifest_sha256="a" * 64,
    )
    frontier_sha = hashlib.sha256(jcs_dumps(frontier_identity)).hexdigest()

    assert frontier_identity["model_tier"] == "frontier"
    assert hashlib.sha256(jcs_dumps(fast_identity)).hexdigest() != frontier_sha
    assert hashlib.sha256(jcs_dumps(unclassified_identity)).hexdigest() != frontier_sha
    assert unclassified_identity["model_tier"] is None


def test_config_identity_binds_effective_api_effort_and_safe_endpoint_identity() -> None:
    import json

    from identitybench.providers.factory import ModelSpec
    from identitybench.v5.cli import _config_identity
    from identitybench.v5.runner import RunConfigV5

    class SafeEndpoint:
        def __init__(self, identity: str) -> None:
            self.identity = identity

        def endpoint_identity(self) -> str:
            return self.identity

    common = {
        "provider": "openai_compatible",
        "model": "synthetic-frontier",
        "label": "synthetic",
    }
    chat_spec = ModelSpec(
        **common,
        extra={
            "api": "chat_completions",
            "reasoning_effort_param": True,
            "headers": {"x-test-route": "private-header-value"},
        },
    )
    responses_spec = ModelSpec(
        **common,
        extra={
            "api": "responses",
            "reasoning_effort_param": False,
            "headers": {"x-test-route": "private-header-value"},
        },
    )
    run_config = RunConfigV5()
    base_args = {
        "model_tier": "frontier",
        "pricing_manifest_sha256": "a" * 64,
    }
    chat_identity = _config_identity(
        chat_spec, run_config, **base_args, provider=SafeEndpoint("safe-endpoint-1")
    )
    responses_identity = _config_identity(
        responses_spec, run_config, **base_args, provider=SafeEndpoint("safe-endpoint-1")
    )
    other_endpoint_identity = _config_identity(
        chat_spec, run_config, **base_args, provider=SafeEndpoint("safe-endpoint-2")
    )

    chat_sha = hashlib.sha256(jcs_dumps(chat_identity)).hexdigest()
    assert hashlib.sha256(jcs_dumps(responses_identity)).hexdigest() != chat_sha
    assert hashlib.sha256(jcs_dumps(other_endpoint_identity)).hexdigest() != chat_sha
    assert chat_identity["provider_settings"]["api"] == "chat_completions"
    assert chat_identity["provider_settings"]["reasoning_effort_parameter_enabled"] is True
    assert responses_identity["provider_settings"]["api"] == "responses"
    assert responses_identity["provider_settings"]["reasoning_effort_parameter_enabled"] is False

    serialized = json.dumps(chat_identity, sort_keys=True)
    assert "private-header-value" not in serialized
    assert "headers" not in serialized


def test_agent_run_options_bind_matching_oracle_and_reset_only_when_requested(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    import identitybench.v5.runner as runner
    from identitybench.v5.cli import _agent_diagnostic_options
    from identitybench.v5.runner import RunConfigV5

    reset_state = object()
    episode = SimpleNamespace(episode_id="ep-01")
    oracle = SimpleNamespace(episode_id="ep-01", omniscient_s2_gold=reset_state)

    coupled = _agent_diagnostic_options(episode, oracle, RunConfigV5())
    assert coupled == {"diagnostic_oracle": oracle}

    reset = _agent_diagnostic_options(
        episode,
        oracle,
        RunConfigV5(wave_execution_mode="ORACLE_PHASE_RESET"),
    )
    assert reset == {
        "diagnostic_oracle": oracle,
        "phase2_reset_state": reset_state,
    }

    mismatched_oracle = SimpleNamespace(episode_id="other", omniscient_s2_gold=reset_state)
    with pytest.raises(ValueError, match="does not match the episode"):
        _agent_diagnostic_options(episode, mismatched_oracle, RunConfigV5())

    class RuntimeReached(Exception):
        pass

    class NoAccessProvider:
        calls = 0

        async def complete(self, _request):
            self.calls += 1
            raise AssertionError("provider must not be reached before reset runtime setup")

    provider = NoAccessProvider()
    captured: dict[str, object] = {}

    def stop_after_runtime_setup(
        _episode, _world, _config, _runtime_factory, supplied_reset, _diagnostic_oracle
    ):
        captured["reset_state"] = supplied_reset
        raise RuntimeReached

    monkeypatch.setattr(runner, "_make_runtime", stop_after_runtime_setup)
    with pytest.raises(RuntimeReached):
        asyncio.run(
            runner.run_v5_agent_episode(
                episode,
                object(),
                provider,
                run_config=RunConfigV5(wave_execution_mode="ORACLE_PHASE_RESET"),
                **reset,
            )
        )
    assert captured["reset_state"] is reset_state
    assert provider.calls == 0


def test_score_rows_injects_host_diagnostics_for_each_baseline(monkeypatch) -> None:
    from types import SimpleNamespace

    import identitybench.v5.baselines as baselines
    import identitybench.v5.runner as runner
    import identitybench.v5.scoring as scoring
    from identitybench.v5.baselines import ALL_V5_BASELINES
    from identitybench.v5.cli import _score_rows

    episode = SimpleNamespace(episode_id="ep-01")
    oracle = SimpleNamespace(episode_id="ep-01", omniscient_s2_gold=object())
    artifacts = SimpleNamespace(episodes=(episode,), oracles=(oracle,))
    factory_marker = object()
    factory_oracles: list[object] = []
    baseline_calls: list[tuple[str, object, object, object, object]] = []

    monkeypatch.setattr(
        runner,
        "make_v5_diagnostic_runtime_factory",
        lambda bound_oracle: factory_oracles.append(bound_oracle) or factory_marker,
    )
    monkeypatch.setattr("identitybench.v5.cli._initial_world", lambda _episode: "initial-world")

    async def fake_baseline(
        baseline_id,
        selected_episode,
        world,
        *,
        run_config,
        runtime_factory,
        phase2_reset_state=None,
    ):
        baseline_calls.append(
            (baseline_id, selected_episode, world, runtime_factory, phase2_reset_state)
        )
        return SimpleNamespace(to_json_dict=lambda: {"episode_id": selected_episode.episode_id})

    monkeypatch.setattr(baselines, "run_v5_baseline_on_episode", fake_baseline)
    monkeypatch.setattr(scoring, "score_execution_v5", lambda *_args, **_kwargs: ("input", "score"))
    monkeypatch.setattr(scoring, "episode_scoring_input_to_mapping", lambda _value: {})
    monkeypatch.setattr(scoring, "episode_score_to_mapping", lambda _value: {})

    rows = _score_rows(artifacts, ALL_V5_BASELINES)

    assert len(rows) == len(ALL_V5_BASELINES)
    assert factory_oracles == [oracle] * len(ALL_V5_BASELINES)
    assert [call[0] for call in baseline_calls] == list(ALL_V5_BASELINES)
    assert all(call[1] is episode and call[2] == "initial-world" for call in baseline_calls)
    assert all(call[3] is factory_marker and call[4] is None for call in baseline_calls)
    assert all("oracle" not in row for row in rows)


@pytest.mark.parametrize("model_tier", ["unknown", [], 7])
def test_run_config_rejects_unsupported_model_tier(tmp_path: Path, model_tier: object) -> None:
    import yaml

    from identitybench.v5.cli import _load_run_config

    config_path = tmp_path / "model.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "openai_compatible", "model": "example-model"},
                "model_tier": model_tier,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(typer.Exit):
        _load_run_config(config_path)


def test_usage_accounting_manifest_rejects_sensitive_symlink_alias(tmp_path: Path) -> None:
    import hashlib

    from identitybench.providers.factory import ModelSpec
    from identitybench.v5.cli import _load_usage_accounting_manifest
    from identitybench.v5.provider_accounting import (
        USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        provider_fingerprint_sha256_v5,
    )
    from identitybench.v5.runner import RunConfigV5

    provider_kind = "openai_compatible"
    model_id = "example-model"
    fingerprint = provider_fingerprint_sha256_v5(
        "unified", provider_kind, model_id, "sha256:public-test-endpoint"
    )
    evidence = {
        "schema_version": V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        "role": "unified",
        "provider_kind": provider_kind,
        "model_id": model_id,
        "provider_fingerprint_sha256": fingerprint,
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "attestation": "operator_or_gateway",
    }
    evidence_raw = jcs_dumps(evidence)
    evidence_sha = hashlib.sha256(evidence_raw).hexdigest()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    public_path = input_dir / "public.json"
    public_path.write_bytes(evidence_raw)
    (input_dir / ".env.attestation").symlink_to(public_path)
    manifest = {
        "schema_version": V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
        "entries": [
            {
                "role": "unified",
                "provider_kind": provider_kind,
                "model_id": model_id,
                "provider_fingerprint_sha256": fingerprint,
                "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
                "evidence_path": ".env.attestation",
                "evidence_sha256": evidence_sha,
                "adapter_retry_limit": 0,
            }
        ],
    }
    manifest_path = input_dir / "manifest.json"
    manifest_path.write_bytes(jcs_dumps(manifest))

    with pytest.raises(ValueError, match="evidence path is sensitive"):
        _load_usage_accounting_manifest(
            tmp_path,
            manifest_path,
            run_config=RunConfigV5(),
            model_spec=ModelSpec(provider=provider_kind, model=model_id),
        )


def test_sensitive_cli_input_path_checks_alias_and_resolved_target(tmp_path: Path) -> None:
    from identitybench.v5.cli import _path

    public_target = tmp_path / "public.json"
    public_target.write_text("{}", encoding="utf-8")
    (tmp_path / ".env.receipt").symlink_to(public_target)
    (tmp_path / "safe-alias.json").symlink_to(public_target)

    with pytest.raises(typer.BadParameter, match="sensitive input paths"):
        _path(tmp_path, Path(".env.receipt"), sensitive_input=True)
    assert _path(tmp_path, Path("safe-alias.json"), sensitive_input=True) == public_target

    sensitive_target = tmp_path / "public-secret.json"
    sensitive_target.write_text("{}", encoding="utf-8")
    (tmp_path / "target-alias.json").symlink_to(sensitive_target)
    with pytest.raises(typer.BadParameter, match="sensitive input paths"):
        _path(tmp_path, Path("target-alias.json"), sensitive_input=True)


def test_validate_cli_rejects_sensitive_blind_receipt_alias(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "public-receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    (tmp_path / ".env.receipt").symlink_to(receipt)

    result = CliRunner().invoke(
        root_cli.app,
        [
            "--root",
            str(tmp_path),
            "--no-env-file",
            "v5",
            "validate",
            "--blind-proof",
            ".env.receipt",
        ],
    )

    assert result.exit_code == 2
    assert "sensitive input paths are not allowed" in result.output


def test_run_cli_rejects_sensitive_usage_manifest_alias(tmp_path: Path) -> None:
    public_manifest = tmp_path / "public-manifest.json"
    public_manifest.write_text("{}", encoding="utf-8")
    (tmp_path / ".env.manifest").symlink_to(public_manifest)

    result = CliRunner().invoke(
        root_cli.app,
        [
            "--root",
            str(tmp_path),
            "--no-env-file",
            "v5",
            "run",
            "--config",
            str(tmp_path / "model.yaml"),
            "--pricing",
            str(tmp_path / "pricing.json"),
            "--usage-accounting-manifest",
            ".env.manifest",
        ],
    )

    assert result.exit_code == 2
    assert "sensitive input paths are not allowed" in result.output
