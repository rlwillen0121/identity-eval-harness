"""Only the v4.1 command group is live; offline leaves never export model secrets."""

import os
from pathlib import Path
from types import SimpleNamespace

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
    url = "LOCAL_LLM_BASE_URL"
    monkeypatch.setenv(key, "inherited-api-key")
    monkeypatch.setenv(url, "https://inherited.example.invalid/v1")
    monkeypatch.setenv("IDENTITYBENCH_PRICING", "inherited-pricing.yaml")
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(url, raising=False)
    monkeypatch.delenv("IDENTITYBENCH_PRICING", raising=False)
    (tmp_path / ".env").write_text(
        f"{key}=placeholder-test-value\n{url}=https://llm.example.invalid/v1\n"
        "IDENTITYBENCH_PRICING=pricing.yaml\n"
    )
    result = CliRunner().invoke(app, ["--root", str(tmp_path), "v41", command, "--no-such-option"])
    assert result.exit_code == ExitCode.USAGE
    assert os.environ.get(key) == ("placeholder-test-value" if command == "run" else None)
    assert os.environ.get(url) == ("https://llm.example.invalid/v1" if command == "run" else None)
    assert os.environ.get("IDENTITYBENCH_PRICING") == "pricing.yaml"
    assert "placeholder-test-value" not in result.output


def test_repo_root_discovery(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == nested.resolve()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "identitybench"\n')
    (tmp_path / "a" / "pyproject.toml").write_text('[project]\nname = "other"\n')
    assert find_repo_root(nested) == tmp_path.resolve()


def test_validate_allow_unpinned_flag_is_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from identitybench.v41 import generator

    report = SimpleNamespace(
        passed=True,
        seal_valid=True,
        seal_checked=True,
        suite=SimpleNamespace(value="v41_dev"),
        episode_count=1,
        oracle_replay_g_srr=1.0,
        oracle_replay_u_exec_mean=1.0,
        oracle_replay_max_turns=1,
        noop_g_srr=0.0,
        constant_policy_g_srr={},
        negatives_ok=True,
        double_build_identical=False,
        token_budget={},
        errors=(),
    )
    seen: list[bool] = []

    def validate(dataset_dir: Path | None, **kwargs):
        seen.append(kwargs["allow_unpinned"])
        return report

    monkeypatch.setattr(generator, "validate_v41_dataset", validate)
    cli = CliRunner()
    base = ["--root", str(tmp_path), "--no-env-file", "v41", "validate"]

    strict = cli.invoke(app, base)
    allowed = cli.invoke(app, [*base, "--allow-unpinned-dataset"])

    assert strict.exit_code == allowed.exit_code == ExitCode.OK
    assert seen == [False, True]


def test_verify_cli_exit_codes_and_unverifiable_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from identitybench.v41 import verify

    cli = CliRunner()
    args = ["--root", str(tmp_path), "--no-env-file", "v41", "verify", "--run", str(tmp_path)]

    def invoke(counts: dict[str, int], *, allow: bool = False):
        details = (
            [
                {
                    "model_id": "sample",
                    "episode_id": "ep-mismatch",
                    "trial_index": 0,
                    "status": "mismatch",
                    "reason": "scorecard disagrees with replay",
                }
            ]
            if counts["mismatch"]
            else []
        )
        unverifiable_reasons = (
            "execution configuration missing from manifest",
            "trace[0] lacks replay fields",
        )
        details.extend(
            {
                "model_id": "sample",
                "episode_id": f"ep-unverifiable-{index + 1}",
                "trial_index": 0,
                "status": "unverifiable",
                "reason": unverifiable_reasons[index % len(unverifiable_reasons)],
            }
            for index in range(counts["unverifiable"])
        )
        result = {
            "models": {"sample": counts},
            "details": details,
        }
        monkeypatch.setattr(verify, "verify_run", lambda *a, **kw: (None, result))
        return cli.invoke(app, [*args, *(["--allow-unverifiable"] if allow else [])])

    success = invoke({"verified": 1, "mismatch": 0, "unverifiable": 0, "recovered": 0})
    unverifiable = invoke({"verified": 0, "mismatch": 0, "unverifiable": 2, "recovered": 0})
    allowed = invoke({"verified": 0, "mismatch": 0, "unverifiable": 2, "recovered": 0}, allow=True)
    mismatch = invoke({"verified": 0, "mismatch": 1, "unverifiable": 2, "recovered": 0}, allow=True)

    assert success.exit_code == ExitCode.OK
    assert unverifiable.exit_code == ExitCode.USAGE
    assert "execution configuration missing from manifest" in unverifiable.output
    assert "trace[0] lacks replay fields" in unverifiable.output
    assert allowed.exit_code == ExitCode.OK
    assert "execution configuration missing from manifest" in allowed.output
    assert "trace[0] lacks replay fields" in allowed.output
    assert mismatch.exit_code == ExitCode.FAILED
