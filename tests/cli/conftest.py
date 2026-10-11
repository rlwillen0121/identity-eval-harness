"""Fixtures for the CLI tests: a toy repo per test, a scored baseline run, and an invoker.

Every invocation runs in a temporary repo (its own ``pyproject.toml`` and dataset), with the
working directory set to it, so the real repo's ``.env``, ``runs/`` and caches are never
touched. ``.env`` loading is off unless a test turns it on. No test may run a model CLI: the
CLI providers' version probe (the one place they spawn their binary outside a call) fails.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner, Result

from identitybench.cli import app
from tests.cli.cases import make_repo

Invoke = Callable[..., Result]

BASELINE_EXPERIMENT = """\
name: toy
dataset: datasets/identitybench-v1
split: test
tasks: [requests, reviews, jml]
baselines: [oracle, always_escalate, always_approve, noop_plan]
"""


def usage_text(stderr: str) -> str:
    """A usage error's message with typer's box drawing and line wrapping taken out."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", stderr).split())


def finished_run(run_dir: Path, run_id: str = "r1") -> SimpleNamespace:
    """What a faked ``run_experiment`` returns: a finished run over every case whose directory
    exists, with an empty ``predictions.jsonl`` (which ``run`` reads to report failed calls)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "predictions.jsonl").write_text("", encoding="utf-8")
    return SimpleNamespace(
        run_id=run_id,
        run_dir=run_dir,
        manifest=SimpleNamespace(sample=None),
        written=0,
        resumed=0,
        skipped=(),
        complete=True,
    )


def make_invoker() -> Invoke:
    """``invoke(*args, env_file=False)``: run the CLI in-process; ``.env`` loading is off
    unless ``env_file`` is set."""
    runner = CliRunner()

    def invoke(*args: str | Path, env_file: bool = False) -> Result:
        prefix = [] if env_file else ["--no-env-file"]
        return runner.invoke(app, [*prefix, *(str(a) for a in args)])

    return invoke


@pytest.fixture(autouse=True)
def no_model_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(binary: str) -> str:
        raise AssertionError(f"a test tried to run {binary} --version")

    monkeypatch.setattr("identitybench.providers._cli.probe_version", refuse)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_repo(tmp_path_factory.mktemp("template") / "repo")


@pytest.fixture(scope="session")
def scored_template(tmp_path_factory: pytest.TempPathFactory, repo_template: Path) -> Path:
    """A repo holding one finished, scored run of four baselines (real runner and scorer)."""
    root = tmp_path_factory.mktemp("scored") / "repo"
    shutil.copytree(repo_template, root)
    (root / "experiments").mkdir()
    (root / "experiments" / "toy.yaml").write_text(BASELINE_EXPERIMENT, encoding="utf-8")
    invoke = make_invoker()
    ran = invoke("--root", root, "run", root / "experiments" / "toy.yaml")
    assert ran.exit_code == 0, ran.output
    scored = invoke("--root", root, "score", root / "runs" / ran.stdout.strip(), "--n-boot", "50")
    assert scored.exit_code == 0, scored.output
    return root


def _copy(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(template, root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def repo(repo_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh toy repo, the working directory for the test."""
    return _copy(repo_template, tmp_path, monkeypatch)


@pytest.fixture
def scored_repo(scored_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh copy of the repo with a scored baseline run, the working directory."""
    return _copy(scored_template, tmp_path, monkeypatch)


@pytest.fixture
def run_dir(scored_repo: Path) -> Path:
    (run,) = (scored_repo / "runs").iterdir()
    return run


@pytest.fixture
def cli(repo: Path) -> Invoke:
    del repo  # requested for its working directory
    return make_invoker()


@pytest.fixture
def scored_cli(scored_repo: Path) -> Invoke:
    del scored_repo  # requested for its working directory
    return make_invoker()


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """``isolate(*names)``: make each name absent now and restore it after the test, even when
    the code under test sets it directly in ``os.environ`` (as ``.env`` loading does)."""

    def isolate(*names: str) -> None:
        for name in names:
            monkeypatch.setenv(name, "placeholder")
            monkeypatch.delenv(name)

    return isolate
