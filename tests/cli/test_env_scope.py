"""V4.1 CLI environment scope, including the runner's provider-creation boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from identitybench.cli import ExitCode, app

SECRET_KEY = "placeholder-secret-for-tests"
SECRET_URL = "https://llm.example.invalid/v1"
NAMES = ("LOCAL_LLM_API_KEY", "LOCAL_LLM_BASE_URL", "IDENTITYBENCH_PRICING")
ROOT = Path(__file__).resolve().parents[2]


def write_env(repo: Path) -> None:
    (repo / ".env").write_text(
        f"LOCAL_LLM_API_KEY={SECRET_KEY}\n"
        f"LOCAL_LLM_BASE_URL={SECRET_URL}\n"
        "IDENTITYBENCH_PRICING=pricing.yaml\n",
        encoding="utf-8",
    )


def exported() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in NAMES}


def clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        monkeypatch.setenv(name, "inherited-placeholder")
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", ["generate", "validate", "score", "report", "verify"])
def test_offline_leaf_loads_only_harness_settings(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_environment(monkeypatch)
    write_env(tmp_path)
    leaf_args = ["--run", str(tmp_path / "run")] if command == "verify" else []
    result = CliRunner().invoke(
        app,
        ["--root", str(tmp_path), "v41", command, *leaf_args, "--no-such-option"],
    )

    assert result.exit_code == ExitCode.USAGE, result.output
    assert exported() == {
        "LOCAL_LLM_API_KEY": None,
        "LOCAL_LLM_BASE_URL": None,
        "IDENTITYBENCH_PRICING": "pricing.yaml",
    }
    assert SECRET_KEY not in result.output and SECRET_URL not in result.output


def test_live_leaf_loads_provider_settings_before_argument_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_environment(monkeypatch)
    write_env(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--root", str(tmp_path), "v41", "run", "--no-such-option"],
    )

    assert result.exit_code == ExitCode.USAGE, result.output
    assert exported() == {
        "LOCAL_LLM_API_KEY": SECRET_KEY,
        "LOCAL_LLM_BASE_URL": SECRET_URL,
        "IDENTITYBENCH_PRICING": "pricing.yaml",
    }
    assert SECRET_KEY not in result.output and SECRET_URL not in result.output


def test_no_env_file_prevents_runner_from_loading_provider_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from identitybench.v41 import runner

    clear_environment(monkeypatch)
    write_env(tmp_path)
    config_path = tmp_path / "experiment.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "env-scope",
                "suite": "v41_dev",
                "dataset_dir": str(ROOT / "datasets/v41_dev"),
                "max_episodes": 1,
                "models": [
                    {
                        "provider": "openai_compatible",
                        "model": "test-model",
                        "base_url_env": "LOCAL_LLM_BASE_URL",
                        "api_key_env": "LOCAL_LLM_API_KEY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    observed: list[tuple[str | None, str | None]] = []

    def stop_before_contact(spec):
        observed.append((os.environ.get("LOCAL_LLM_API_KEY"), os.environ.get("LOCAL_LLM_BASE_URL")))
        raise ValueError("stubbed before model contact")

    monkeypatch.setattr(runner, "make_provider", stop_before_contact)
    result = CliRunner().invoke(
        app,
        [
            "--root",
            str(tmp_path),
            "--no-env-file",
            "v41",
            "run",
            "--config",
            str(config_path),
            "--out",
            str(tmp_path / "run"),
        ],
    )

    assert result.exit_code == ExitCode.INPUT, result.output
    assert observed == [(None, None)]
    assert SECRET_KEY not in result.output and SECRET_URL not in result.output
