"""``run``'s advice after a config whose every call failed, checked against the real runner.

The exit-3 message says how to finish. When the provider's endpoint identity records it as
not set up (``openai_compatible`` hashes an unset base-URL variable as ``unset:VAR``), setting
it up changes that identity, a resume is refused, and the message says to start a new run
instead. For any other cause it says to resume, and a resume retries every failed call. Each
test follows the advice and checks that the runner does what the message said.

Nothing reaches the network: an unset base-URL variable or API key fails every call before a
client exists, and where a call would otherwise be made the provider's client is replaced by
one that fails as an unreachable server does.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import openai
import pytest

from identitybench.cli import ExitCode
from identitybench.providers.openai_compat import OpenAICompatProvider
from identitybench.runner.run import PREDICTIONS_FILE, load_predictions
from identitybench.schemas.predictions import Prediction, PredictionStatus
from tests.cli.conftest import Invoke

URL_VAR = "IB_TEST_DEAD_BASE_URL"
CUSTOM_ENDPOINT = f"""\
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
VENDOR_ENDPOINT = """\
name: vendor
dataset: datasets/identitybench-v1
split: test
tasks: [requests]
models:
  - provider: openai_compatible
    model: m1
    label: vendor
    base_url_env: OPENAI_BASE_URL
"""
"""OpenAI itself, naming its conventional variable: with ``OPENAI_BASE_URL`` unset the provider
falls back to the vendor URL (as it does with no ``base_url_env``), so the identity doesn't
record the variable as unset, and only the missing key fails the calls."""


def write_experiment(repo: Path, name: str, text: str) -> Path:
    path = repo / "experiments" / f"{name}.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every call that gets as far as a client fail, as a server that is down does."""

    def refuse(self: OpenAICompatProvider) -> None:
        raise openai.OpenAIError("connection refused (test server is down)")

    monkeypatch.setattr(OpenAICompatProvider, "_get_client", refuse)


def failed(repo: Path, run_id: str) -> list[Prediction]:
    predictions = load_predictions(repo / "runs" / run_id / PREDICTIONS_FILE)
    assert predictions and all(p.status is PredictionStatus.error for p in predictions)
    return predictions


def advice(stderr: str) -> str:
    """The exit-3 message, unwrapped."""
    [line] = [line for line in stderr.splitlines() if line.startswith("error: ")]
    return line


@pytest.fixture
def custom_endpoint(repo: Path, isolated_env: Callable[..., None]) -> Path:
    isolated_env(URL_VAR)
    return write_experiment(repo, "dead", CUSTOM_ENDPOINT)


def test_an_unset_base_url_variable_calls_for_a_new_run_which_then_resumes(
    cli: Invoke, repo: Path, custom_endpoint: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = cli("--quiet", "run", custom_endpoint)
    assert first.exit_code == ExitCode.INPUT, first.output
    run_id = first.stdout.strip()
    failed(repo, run_id)
    message = advice(first.stderr)
    gap = f"dead__baseline__context: its base-URL variable {URL_VAR} is unset"
    assert "every call of dead__baseline__context failed (see the warnings above), and an " in (
        message
    )
    assert f"endpoint isn't set up ({gap}). Setting it up changes the endpoint " in message
    assert f"identity run {run_id} recorded, which a resume must match" in message
    assert message.endswith(
        f"start a new run with: identitybench run {custom_endpoint} (the response cache replays "
        "every call already answered, at no cost)"
    )
    assert "--resume" not in message

    # The advice holds: with the variable set, a resume of that run is refused.
    monkeypatch.setenv(URL_VAR, "http://127.0.0.1:9/v1")
    unreachable_server(monkeypatch)
    refused = cli("--quiet", "run", custom_endpoint, "--resume", run_id)
    assert refused.exit_code == ExitCode.INPUT
    refusal = advice(refused.stderr)
    assert f"cannot resume run {run_id}" in refusal and "endpoint identity" in refusal

    # The new run it calls for starts. Its server is still down, but the endpoint is now set
    # up, so the advice is a resume, and that resume is accepted and retries every call.
    fresh = cli("--quiet", "run", custom_endpoint)
    assert fresh.exit_code == ExitCode.INPUT, fresh.output
    fresh_id = fresh.stdout.strip()
    assert fresh_id != run_id
    calls = len(failed(repo, fresh_id))
    resume = f"identitybench run {custom_endpoint} --resume {fresh_id}"
    assert advice(fresh.stderr).endswith(
        f"; fix the provider, then resume with: {resume} (a resume retries every failed call "
        "and keeps every answer; a fix that edits the experiment or changes a CLI's version "
        "needs a new run instead)"
    )
    resumed = cli("run", custom_endpoint, "--resume", fresh_id)
    assert resumed.exit_code == ExitCode.INPUT, resumed.output
    assert resumed.stdout.strip() == fresh_id
    assert f"run {fresh_id}: {calls} predictions written ->" in resumed.stderr


def test_a_missing_api_key_calls_for_a_resume_which_the_key_does_not_block(
    cli: Invoke, repo: Path, isolated_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_env("OPENAI_BASE_URL", "OPENAI_API_KEY")
    experiment = write_experiment(repo, "vendor", VENDOR_ENDPOINT)
    first = cli("--quiet", "run", experiment)
    assert first.exit_code == ExitCode.INPUT, first.output
    run_id = first.stdout.strip()
    calls = len(failed(repo, run_id))
    message = advice(first.stderr)
    assert "environment variable OPENAI_API_KEY is not set" in first.stderr
    assert message.endswith(
        f"fix the provider, then resume with: identitybench run {experiment} --resume {run_id} "
        "(a resume retries every failed call and keeps every answer; a fix that edits the "
        "experiment or changes a CLI's version needs a new run instead)"
    )
    assert "isn't set up" not in message

    # Setting the key leaves the endpoint identity alone, so the resume is accepted (the
    # server, replaced here, still fails, so every call is retried and fails again).
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-key")
    unreachable_server(monkeypatch)
    resumed = cli("run", experiment, "--resume", run_id)
    assert resumed.exit_code == ExitCode.INPUT, resumed.output
    assert "cannot resume" not in resumed.stderr
    assert f"run {run_id}: {calls} predictions written ->" in resumed.stderr
    assert "connection refused (test server is down)" in resumed.stderr
