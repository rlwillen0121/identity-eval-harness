"""``run``'s advice after a config whose every call failed, checked against the real runner.

The exit-3 message tells the user how to finish the run. Its two branches are both real: a
resume retries every failed call when the provider's endpoint is unchanged, and is refused
when the fix changed the endpoint identity (here, setting the base-URL variable that was
unset, which ``openai_compatible`` hashes as ``unset:VAR``). Nothing reaches the network: the
unset variable fails every call before a request is made, and the refused resume stops before
any call.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from identitybench.cli import ExitCode
from identitybench.runner.run import PREDICTIONS_FILE, load_predictions
from identitybench.schemas.predictions import PredictionStatus
from tests.cli.conftest import Invoke

URL_VAR = "IB_TEST_DEAD_BASE_URL"
EXPERIMENT = f"""\
name: dead
dataset: datasets/identitybench-v1
split: test
tasks: [requests]
models:
  - provider: openai_compatible
    model: m1
    label: dead
    base_url_env: {URL_VAR}
    cost_mode: unmetered
"""


@pytest.fixture
def dead_experiment(repo: Path, isolated_env: Callable[..., None]) -> Path:
    isolated_env(URL_VAR)
    path = repo / "experiments" / "dead.yaml"
    path.parent.mkdir()
    path.write_text(EXPERIMENT, encoding="utf-8")
    return path


def test_a_dead_provider_exit_names_both_ways_to_finish_and_each_holds(
    cli: Invoke, repo: Path, dead_experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = cli("--quiet", "run", dead_experiment)
    assert first.exit_code == ExitCode.INPUT, first.output
    run_id = first.stdout.strip()
    message = " ".join(first.stderr.split())
    assert f"resume with: identitybench run {dead_experiment} --resume {run_id}" in message
    assert "if the fix changes the endpoint itself" in message
    assert "start a new run instead" in message
    predictions = load_predictions(repo / "runs" / run_id / PREDICTIONS_FILE)
    assert predictions and all(p.status is PredictionStatus.error for p in predictions)

    # Unchanged endpoint: the resume is accepted and retries every failed call.
    again = cli("run", dead_experiment, "--resume", run_id)
    assert again.exit_code == ExitCode.INPUT, again.output
    assert again.stdout.strip() == run_id
    assert f"run {run_id}: {len(predictions)} predictions written ->" in again.stderr

    # The fix sets the variable: the endpoint identity changed, so the resume is refused, as
    # the message warned, and the user is told to start a new run.
    monkeypatch.setenv(URL_VAR, "http://127.0.0.1:9/v1")
    fixed = cli("--quiet", "run", dead_experiment, "--resume", run_id)
    assert fixed.exit_code == ExitCode.INPUT
    refusal = " ".join(fixed.stderr.split())
    assert f"cannot resume run {run_id}" in refusal and "endpoint identity" in refusal
    assert "start a new run" in refusal
