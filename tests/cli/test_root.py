"""The root command: version, help, repo-root discovery, ``.env`` loading and exit codes."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from identitybench import __version__
from identitybench.cli import ExitCode, app, find_repo_root, parse_sample
from tests.cli.conftest import Invoke, finished_run

SECRET_URL = "https://llm.example.invalid/v1"
SECRET_KEY = "placeholder-secret-for-tests"


def test_version(cli: Invoke) -> None:
    result = cli("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"identitybench {__version__}"


def test_no_arguments_shows_help_as_a_usage_error(repo: Path) -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == ExitCode.USAGE
    assert "generate" in result.output and "audit" in result.output


def test_options_without_a_command_are_a_usage_error(cli: Invoke) -> None:
    result = cli("--quiet")
    assert result.exit_code == ExitCode.USAGE
    assert "Missing command" in result.output


def test_help_lists_every_command_and_the_exit_codes(cli: Invoke) -> None:
    result = cli("--help")
    assert result.exit_code == 0
    for command in ("generate", "validate", "run", "score", "judge", "report", "compare", "audit"):
        assert command in result.stdout
    assert "Exit codes" in result.stdout


def test_find_repo_root_picks_the_nearest_identitybench_project(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "identitybench"\n', encoding="utf-8")
    (repo / "a" / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    assert find_repo_root(nested) == repo.resolve()
    (repo / "a" / "b" / "pyproject.toml").write_text("not [toml", encoding="utf-8")
    assert find_repo_root(nested) == repo.resolve()


def test_find_repo_root_falls_back_to_the_start(tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert find_repo_root(lonely) == lonely.resolve()


@pytest.mark.parametrize(
    ("text", "expected"), [("0.25", 0.25), ("1.0", 1.0), ("1e-1", 0.1), ("30", 30), ("1", 1)]
)
def test_parse_sample(text: str, expected: float | int) -> None:
    value = parse_sample(text)
    assert value == expected and type(value) is type(expected)


def capture_environment(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> dict[str, Any]:
    """Make ``run`` record the named variables as the back end would see them."""
    seen: dict[str, Any] = {}

    async def fake_run(config: object, **kwargs: object) -> SimpleNamespace:
        seen.update({name: os.environ.get(name) for name in names})
        return finished_run(Path("runs/r1"))

    monkeypatch.setattr("identitybench.runner.run.run_experiment", fake_run)
    return seen


def write_experiment(repo: Path) -> Path:
    path = repo / "exp.yaml"
    path.write_text(
        "name: e\ndataset: datasets/identitybench-v1\ntasks: [requests]\nbaselines: [oracle]\n",
        encoding="utf-8",
    )
    return path


def test_env_file_is_loaded_without_overriding_and_never_echoed(
    cli: Invoke, repo: Path, monkeypatch: pytest.MonkeyPatch, isolated_env: Any
) -> None:
    names = ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "IDB_TEST_PRESET")
    isolated_env(*names)
    monkeypatch.setenv("IDB_TEST_PRESET", "from-shell")
    (repo / ".env").write_text(
        f"LOCAL_LLM_BASE_URL={SECRET_URL}\n"
        f"export LOCAL_LLM_API_KEY='{SECRET_KEY}'\n"
        "IDB_TEST_PRESET=from-file\n"
        f"broken line {SECRET_KEY}\n",
        encoding="utf-8",
    )
    seen = capture_environment(monkeypatch, names)
    result = cli("run", write_experiment(repo), env_file=True)
    assert result.exit_code == 0, result.output
    assert seen == {
        "LOCAL_LLM_BASE_URL": SECRET_URL,
        "LOCAL_LLM_API_KEY": SECRET_KEY,
        "IDB_TEST_PRESET": "from-shell",
    }
    assert "line 4" in result.stderr and "skipped" in result.stderr
    for secret in (SECRET_URL, SECRET_KEY, "from-file"):
        assert secret not in result.output


def test_no_env_file_and_explicit_env_file(
    cli: Invoke, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env: Any
) -> None:
    isolated_env("IDB_TEST_ROOT", "IDB_TEST_OTHER")
    (repo / ".env").write_text("IDB_TEST_ROOT=1\n", encoding="utf-8")
    other = tmp_path / "other.env"
    other.write_text("IDB_TEST_OTHER=2\n", encoding="utf-8")
    seen = capture_environment(monkeypatch, ("IDB_TEST_ROOT", "IDB_TEST_OTHER"))
    assert cli("run", write_experiment(repo)).exit_code == 0  # --no-env-file
    assert seen == {"IDB_TEST_ROOT": None, "IDB_TEST_OTHER": None}
    result = cli("--env-file", other, "run", write_experiment(repo), env_file=True)
    assert result.exit_code == 0, result.output
    assert seen == {"IDB_TEST_ROOT": None, "IDB_TEST_OTHER": "2"}


def test_missing_explicit_env_file_is_an_input_error(cli: Invoke, tmp_path: Path) -> None:
    result = cli("--env-file", tmp_path / "nope.env", "run", "x.yaml", env_file=True)
    assert result.exit_code == ExitCode.INPUT
    assert "does not exist" in result.stderr


def test_root_option_overrides_discovery(
    cli: Invoke, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run(config: object, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return finished_run(Path("r"))

    monkeypatch.setattr("identitybench.runner.run.run_experiment", fake_run)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert cli("--root", elsewhere, "run", write_experiment(repo)).exit_code == 0
    assert captured["repo_root"] == elsewhere.resolve()
    assert captured["runs_dir"] == elsewhere.resolve() / "runs"


def test_an_undecodable_env_file_warns_and_never_blocks_a_command(
    cli: Invoke, repo: Path, monkeypatch: pytest.MonkeyPatch, isolated_env: Any
) -> None:
    isolated_env("IDB_TEST_LATIN")
    (repo / ".env").write_bytes(b"IDB_TEST_LATIN=caf\xe9\n")
    seen = capture_environment(monkeypatch, ("IDB_TEST_LATIN",))
    result = cli("run", write_experiment(repo), env_file=True)
    assert result.exit_code == 0, result.output
    assert seen == {"IDB_TEST_LATIN": None}
    assert f"warning: {repo.resolve() / '.env'}: not valid UTF-8 text; file skipped" in (
        result.stderr
    )
