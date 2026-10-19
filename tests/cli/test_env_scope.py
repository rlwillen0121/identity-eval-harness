"""The ``.env`` export scope through the command line: the root callback loads for a top-level
command, and for the ``v3`` and ``v4`` groups the group's own callback loads for its leaf, so
``v3 validate`` and ``v4 report`` never export a key while ``v3 run`` and ``v4 run`` would.

Every invocation here fails at the leaf's argument parsing (an unknown option or an
out-of-range value), which happens after both callbacks have run and before the leaf's body,
so no command does any work and no test can reach a model: what matters is what the callbacks
left in the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from identitybench.cli import ExitCode
from identitybench.envfile import env_names_for, is_live_command
from tests.cli.conftest import Invoke

SECRET_KEY = "placeholder-secret-for-tests"
SECRET_URL = "https://llm.example.invalid/v1"
NAMES = ("LOCAL_LLM_API_KEY", "LOCAL_LLM_BASE_URL", "IDENTITYBENCH_PRICING")


def write_env(repo: Path) -> None:
    (repo / ".env").write_text(
        f"LOCAL_LLM_API_KEY={SECRET_KEY}\n"
        f"LOCAL_LLM_BASE_URL={SECRET_URL}\n"
        "IDENTITYBENCH_PRICING=pricing.yaml\n",
        encoding="utf-8",
    )


def exported() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in NAMES}


@pytest.mark.parametrize(
    "args",
    [
        ("v3", "validate", "--no-such-option"),
        ("v3", "score", "--no-such-option"),
        ("v3", "generate", "--no-such-option"),
        ("v3", "report", "--no-such-option"),
        ("v4", "validate", "--no-such-option"),
        ("v4", "generate", "--no-such-option"),
        ("v4", "score", "--no-such-option"),
        ("v4", "report", "--n-boot", "1"),
        ("validate", "--no-such-option"),
        ("score", "--no-such-option"),
    ],
    ids=lambda args: " ".join(args[:2]),
)
def test_a_leaf_that_calls_no_model_never_exports_a_key(
    cli: Invoke, repo: Path, isolated_env: Any, args: tuple[str, ...]
) -> None:
    isolated_env(*NAMES)
    write_env(repo)
    result = cli(*args, env_file=True)
    assert result.exit_code == ExitCode.USAGE, result.output  # the leaf rejected its arguments
    assert exported() == {
        "LOCAL_LLM_API_KEY": None,
        "LOCAL_LLM_BASE_URL": None,
        "IDENTITYBENCH_PRICING": "pricing.yaml",
    }
    assert SECRET_KEY not in result.output and SECRET_URL not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ("v3", "run", "--no-such-option"),
        ("v4", "run", "--no-such-option"),
        ("run", "--no-such-option"),
    ],
    ids=lambda args: " ".join(args[:2]),
)
def test_a_live_leaf_gets_every_name_before_it_runs(
    cli: Invoke, repo: Path, isolated_env: Any, args: tuple[str, ...]
) -> None:
    """The group callback loads the whole file for ``v3 run`` / ``v4 run`` (the leaf itself is
    never reached: its arguments are rejected first, so no model is called)."""
    isolated_env(*NAMES)
    write_env(repo)
    result = cli(*args, env_file=True)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert exported() == {
        "LOCAL_LLM_API_KEY": SECRET_KEY,
        "LOCAL_LLM_BASE_URL": SECRET_URL,
        "IDENTITYBENCH_PRICING": "pricing.yaml",
    }
    assert SECRET_KEY not in result.output and SECRET_URL not in result.output


def test_the_name_selection_for_v3_run_and_v3_validate() -> None:
    """The decision the group callbacks rely on, checked directly so no live command is run."""
    assert is_live_command(("v3", "run")) and is_live_command(("v4", "run"))
    assert not is_live_command(("v3", "validate")) and not is_live_command(("v4", "report"))
    for name in NAMES:
        assert env_names_for(("v3", "run"))(name)
    assert not env_names_for(("v3", "validate"))("LOCAL_LLM_API_KEY")
    assert env_names_for(("v4", "report"))("IDENTITYBENCH_PRICING")


def test_no_env_file_applies_to_a_group_leaf_too(
    cli: Invoke, repo: Path, isolated_env: Any
) -> None:
    isolated_env(*NAMES)
    write_env(repo)
    result = cli("v3", "validate", "--no-such-option")  # --no-env-file is the default here
    assert result.exit_code == ExitCode.USAGE, result.output
    assert exported() == dict.fromkeys(NAMES)


def test_a_group_without_a_leaf_loads_nothing(cli: Invoke, repo: Path, isolated_env: Any) -> None:
    """``v3`` alone shows the group's help: the root callback leaves loading to the group
    callback, which never runs, so not even the harness's own settings are exported."""
    isolated_env(*NAMES)
    write_env(repo)
    result = cli("v3", env_file=True)
    assert "validate" in result.output
    assert exported() == dict.fromkeys(NAMES)
