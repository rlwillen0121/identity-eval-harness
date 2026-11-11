"""Only the v4.1 command group is live; offline leaves never export model secrets."""

import os
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from identitybench import __version__
from identitybench.cli import ExitCode, app, find_repo_root

COMMANDS = {"generate", "validate", "score", "run", "report", "verify"}


def test_only_v41_commands_are_registered():
    root = get_command(app)
    assert set(root.commands) == {"v41"}
    assert set(root.commands["v41"].commands) == COMMANDS
    assert CliRunner().invoke(app, ["--version"]).stdout.strip() == f"identitybench {__version__}"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_leaf_environment_scope(tmp_path, monkeypatch, command):
    key = "LOCAL_LLM_API_KEY"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("IDENTITYBENCH_PRICING", raising=False)
    (tmp_path / ".env").write_text(
        f"{key}=placeholder-test-value\nIDENTITYBENCH_PRICING=pricing.yaml\n"
    )
    result = CliRunner().invoke(app, ["--root", str(tmp_path), "v41", command, "--no-such-option"])
    assert result.exit_code == ExitCode.USAGE
    assert os.environ.get(key) == ("placeholder-test-value" if command == "run" else None)
    assert os.environ.get("IDENTITYBENCH_PRICING") == "pricing.yaml"
    assert "placeholder-test-value" not in result.output


def test_repo_root_discovery(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == nested.resolve()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "identitybench"\n')
    (tmp_path / "a" / "pyproject.toml").write_text('[project]\nname = "other"\n')
    assert find_repo_root(nested) == tmp_path.resolve()
