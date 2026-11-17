"""v4.1 binary configuration preserves resume identity semantics."""

from pathlib import Path

from identitybench.v41.runner import V41ModelEntryConfig, compute_v41_config_fingerprint


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
