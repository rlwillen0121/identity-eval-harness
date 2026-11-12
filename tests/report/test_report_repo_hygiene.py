"""Portable Muse configuration must preserve the versions' resume identities."""

from pathlib import Path

import pytest

from identitybench.providers import _cli
from identitybench.providers.muse_cli import MuseCliProvider
from identitybench.v3.runner import compute_config_fingerprint, load_v3_experiment_config
from identitybench.v41.runner import V41ModelEntryConfig, compute_v41_config_fingerprint

REPO = Path(__file__).resolve().parents[2]


def test_v3_muse_config_is_portable() -> None:
    entry = load_v3_experiment_config(REPO / "experiments/v3-full-muse.yaml").models[0]
    assert entry.extra["binary"] == "muse"


def test_binary_path_is_excluded_from_muse_endpoint_and_v3_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CLI invocation or real credentials: equal executable versions, empty private homes.
    monkeypatch.setattr(_cli, "probe_version", lambda *args: "test-version")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    absolute = MuseCliProvider("muse-spark-1.3", binary=str(tmp_path / "bin/muse"))
    portable = MuseCliProvider("muse-spark-1.3", binary="muse")
    assert absolute.endpoint_identity() == portable.endpoint_identity()
    fingerprints = [
        compute_config_fingerprint(
            provider_kind="muse_cli",
            model=provider.model,
            effort=provider.effort,
            temperature=None,
            max_output_tokens=4096,
            max_turns=10,
            max_actions_per_turn=4,
            max_tool_calls_per_episode=40,
            endpoint_identity=provider.endpoint_identity(),
        )
        for provider in (absolute, portable)
    ]
    assert fingerprints[0] == fingerprints[1]


def test_v41_binary_path_changes_resume_fingerprint(tmp_path: Path) -> None:
    absolute = V41ModelEntryConfig(
        provider="muse_cli",
        model="muse-spark-1.3",
        extra={"binary": str(tmp_path / "bin/muse")},
    )
    portable = absolute.model_copy(update={"extra": {"binary": "muse"}})
    fingerprints = [
        compute_v41_config_fingerprint(
            entry,
            entry.to_run_config(),
            seed=1,
            dataset_sha256="test",
            endpoint_identity="same-endpoint",
        )
        for entry in (absolute, portable)
    ]
    assert fingerprints[0] != fingerprints[1]
