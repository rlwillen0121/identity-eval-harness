"""Security regressions for subprocesses used to record v4.1 run provenance."""

from __future__ import annotations

import subprocess

from identitybench.providers._cli import base_child_env
from identitybench.v41 import runner


def test_git_state_disables_fsmonitor_and_drops_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-must-not-reach-git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "injected-git-command")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "head\n" if "rev-parse" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", run)

    assert runner._git_state(tmp_path) == ("head", False)
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv[:3] == ["git", "-c", "core.fsmonitor=false"]
        assert kwargs["env"] == base_child_env()
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert not any(name.startswith("GIT_") for name in kwargs["env"])
