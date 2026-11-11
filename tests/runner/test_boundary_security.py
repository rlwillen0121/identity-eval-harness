"""Offline regression coverage for accounting and manifest/config hygiene."""

import math
import subprocess
from pathlib import Path

import pytest

from identitybench.providers.base import Usage
from identitybench.runner.config import ModelEntry
from identitybench.runner.cost import Rates
from identitybench.runner.manifest import git_info


@pytest.mark.parametrize("tokens", [-10**12, 10**400, float("nan"), float("inf")])
def test_cost_handles_untrusted_counts(tokens):
    amount = Rates(input=1, output=2).cost(Usage(output_tokens=tokens))
    assert math.isfinite(amount) and amount >= 0


def test_git_manifest_disables_fsmonitor_and_drops_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-must-not-reach-git")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="head\n" if "rev-parse" in argv else "")

    monkeypatch.setattr(subprocess, "run", run)
    assert git_info(tmp_path) == ("head", False)
    for argv, kwargs in calls:
        assert "core.fsmonitor=false" in argv
        assert "OPENAI_API_KEY" not in kwargs["env"]


@pytest.mark.parametrize("key", ["binary", "path"])
@pytest.mark.parametrize("relative", [True, False])
def test_home_config_paths_warn_but_load(monkeypatch, tmp_path, key, relative):
    monkeypatch.setenv("HOME", str(tmp_path))
    value = "~/bin/provider" if relative else str(Path.home() / "bin/provider")
    with pytest.warns(UserWarning, match="bare binary name or experiments/local/"):
        entry = ModelEntry(
            provider="replay" if key == "path" else "claude_cli",
            model="fixture", extra={key: value},
        )
    assert entry.extra[key] == value


def test_home_path_warns_even_when_symlink_points_outside_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "bin").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.warns(UserWarning, match="bare binary name"):
        ModelEntry(provider="claude_cli", model="fixture", extra={"binary": str(home / "bin/tool")})
