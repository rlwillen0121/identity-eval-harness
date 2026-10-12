"""``judge``: the judges YAML, production judging of a run and judge validation (fakes stand in
for the judge calls; nothing reaches a model)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from identitybench.cli import CliError, ExitCode, load_judges
from identitybench.judge.aggregate import JudgeInfo, JudgeRun, JudgeSelection, build_judge_run
from identitybench.judge.families import FamilyError
from identitybench.judge.run import Judge
from identitybench.judge.validate import GateStatus
from identitybench.runner.cache import ResponseCache
from identitybench.runner.manifest import ParamsRecord
from tests.cli.conftest import Invoke

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------------
# The judges file
# --------------------------------------------------------------------------------------------


def test_committed_judges_file_is_a_two_family_panel() -> None:
    judges = load_judges(REPO_ROOT / "experiments" / "judges.yaml")
    assert [(j.name, j.family) for j in judges] == [
        ("claude_cli:claude-sonnet-5-5", "anthropic"),
        ("codex_cli:gpt-6-astra", "openai"),
    ]
    assert all(j.params.temperature == 0.0 and j.params.max_output_tokens == 2048 for j in judges)
    assert judges[1].params.effort == "medium"


def test_judge_entries_can_name_a_family_and_override_settings(tmp_path: Path) -> None:
    path = write(
        tmp_path / "j.yaml",
        "- provider: anthropic_compatible\n  model: served-model\n  label: local\n"
        "  base_url_env: LOCAL_LLM_BASE_URL\n  family: qwen\n  max_output_tokens: 512\n"
        "- provider: claude_cli\n  model: claude-haiku-5-5\n  temperature: null\n",
    )
    local, claude = load_judges(path)
    assert (local.name, local.family, local.params.max_output_tokens) == ("local", "qwen", 512)
    assert claude.params.temperature is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[unclosed", "invalid YAML"),
        ("{}", "non-empty YAML list"),
        ("[]", "non-empty YAML list"),
        ("- just a string\n", "judge 0 is not a mapping"),
        ("- provider: claude_cli\n  model: m\n  family: 3\n", "family must be a string"),
        ("- provider: claude_cli\n  model: m\n  concurrency: 2\n", "use judge --concurrency"),
        ("- provider: claude_cli\n  model: m\n  cost_mode: metered\n", "doesn't apply"),
        (
            "- provider: openai_compatible\n  model: m\n  base_url_env: https://host/v1\n",
            "looks like a URL",
        ),
        ("- provider: nope\n  model: m\n", "judge 0"),
        (
            "- provider: claude_cli\n  model: m\n  label: x\n- provider: codex_cli\n"
            "  model: n\n  label: x\n",
            "unique",
        ),
    ],
)
def test_invalid_judges_files(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(CliError, match=message):
        load_judges(write(tmp_path / "j.yaml", text))


# --------------------------------------------------------------------------------------------
# Judging a run
# --------------------------------------------------------------------------------------------


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    async def fake_judge(cases: Any, predictions: Any, judges: Any, **kwargs: Any) -> JudgeRun:
        calls.update(cases=cases, predictions=predictions, judges=judges, **kwargs)
        return build_judge_run(
            [], judges={j.name: j.family for j in judges}, rubric_sha256="0" * 64
        )

    monkeypatch.setattr("identitybench.judge.run.judge_predictions", fake_judge)
    return calls


@pytest.fixture
def judges_file(scored_repo: Path) -> Path:
    target = scored_repo / "experiments" / "judges.yaml"
    target.write_text((REPO_ROOT / "experiments" / "judges.yaml").read_text(), encoding="utf-8")
    return target


def test_judge_a_run_with_defaults(
    scored_cli: Invoke, run_dir: Path, judges_file: Path, judged: dict[str, Any]
) -> None:
    result = scored_cli("judge", run_dir)  # the judges file defaults to experiments/judges.yaml
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(run_dir / "judge" / "summary.json")
    assert json.loads((run_dir / "judge" / "summary.json").read_text())["exploratory"] is True
    assert len(judged["cases"]) > 0 and len(judged["predictions"]) > 0
    assert [j.name for j in judged["judges"]] == [
        "claude_cli:claude-sonnet-5-5",
        "codex_cli:gpt-6-astra",
    ]
    assert judged["output_families"] == {
        "always_approve": "baseline",
        "always_escalate": "baseline",
        "noop_plan": "baseline",
        "oracle": "baseline",
    }
    assert (judged["sample"], judged["items_per_campaign"], judged["seed"]) == (None, 6, 0)
    assert judged["include_baselines"] is False
    assert judged["concurrency"] == 4
    cache: ResponseCache = judged["cache"]
    assert cache.root == run_dir.parent.parent.resolve() / ".cache" / "responses"
    assert cache.refresh is False
    assert "rationales judged" in result.stderr


def test_judge_options_reach_the_back_end(
    scored_cli: Invoke, run_dir: Path, judges_file: Path, judged: dict[str, Any], tmp_path: Path
) -> None:
    out = tmp_path / "judged"
    result = scored_cli(
        "judge",
        run_dir,
        "--judges",
        judges_file,
        "--sample",
        "0.5",
        "--all-items",
        "--concurrency",
        "2",
        "--seed",
        "9",
        "--refresh",
        "--out",
        out,
    )
    assert result.exit_code == 0, result.output
    assert (judged["sample"], judged["items_per_campaign"]) == (0.5, None)
    assert (judged["concurrency"], judged["seed"], judged["cache"].refresh) == (2, 9, True)
    assert (out / "scores.jsonl").is_file()
    assert scored_cli("judge", run_dir, "--items-per-campaign", "3").exit_code == 0
    assert judged["items_per_campaign"] == 3
    named = scored_cli("judge", run_dir, "--include-baselines", "oracle")
    assert named.exit_code == 0, named.output
    assert judged["include_baselines"] == frozenset({"oracle"})
    assert scored_cli("judge", run_dir, "--include-baselines", "all").exit_code == 0
    assert judged["include_baselines"] is True


def _run_with_excluded(judges: Any, judged_configs: dict[str, int]) -> JudgeRun:
    """What ``judge_predictions`` returns when it left the baselines out."""
    selection = JudgeSelection(
        repeats="first",
        variants="base",
        items_per_campaign=6,
        sample=None,
        seed=0,
        n_predictions=10,
        n_selected_predictions=10,
        eligible=judged_configs,
        judged=judged_configs,
        excluded_baselines={"oracle": 7, "always_approve": 3},
    )
    run = build_judge_run([], judges={j.name: j.family for j in judges}, rubric_sha256="0" * 64)
    return dataclasses.replace(run, selection=selection)


def test_judge_says_how_many_baseline_rationales_it_left_out(
    scored_cli: Invoke, run_dir: Path, judges_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the toy run, all baselines, nothing is judged: that must not read as success."""

    async def fake_judge(cases: Any, predictions: Any, judges: Any, **kwargs: Any) -> JudgeRun:
        return _run_with_excluded(judges, {})

    monkeypatch.setattr("identitybench.judge.run.judge_predictions", fake_judge)
    result = scored_cli("--quiet", "judge", run_dir)
    assert result.exit_code == 0, result.output
    message = " ".join(result.stderr.split())
    assert "warning: no model rationale to work on; 10 baseline rationale(s) left out" in message
    assert "(always_approve 3, oracle 7)" in message and "--include-baselines NAME" in message


@pytest.mark.parametrize(
    "args",
    [
        ("judge",),
        ("judge", "runs/x", "--validate", "labels.jsonl"),
        ("judge", "runs/x", "--key", "key.jsonl"),
        ("judge", "runs/x", "--split", "dev"),
        ("judge", "runs/x", "--all-items", "--items-per-campaign", "2"),
        ("judge", "runs/x", "--family", "no-equals-sign"),
        ("judge", "runs/x", "--sample", "2.5"),
        ("judge", "--validate", "labels.jsonl", "--sample", "0.5"),
        ("judge", "--validate", "labels.jsonl", "--split", "train"),
        ("judge", "--validate", "labels.jsonl", "--include-baselines", "oracle"),
        ("judge", "runs/x", "--include-baselines", "all", "--include-baselines", "oracle"),
    ],
)
def test_judge_usage_errors(scored_cli: Invoke, judged: dict[str, Any], args: tuple[str]) -> None:
    result = scored_cli(*args)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert not judged


def test_a_missing_judges_file_says_how_to_name_one(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path, judged: dict[str, Any]
) -> None:
    result = scored_cli("judge", run_dir)  # no experiments/judges.yaml in this repo
    assert result.exit_code == ExitCode.INPUT
    default = scored_repo.resolve() / "experiments" / "judges.yaml"
    assert result.stderr.strip() == (
        f"error: judges file not found: {default} (pass --judges FILE)"
    )
    assert not judged


def test_judge_input_errors(
    scored_cli: Invoke,
    scored_repo: Path,
    run_dir: Path,
    judges_file: Path,
    judged: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = scored_cli("judge", "runs/none")
    assert missing.exit_code == ExitCode.INPUT and "no such run directory" in missing.stderr
    bad = write(scored_repo / "bad.yaml", "- provider: claude_cli\n  model: m\n  concurrency: 2\n")
    result = scored_cli("judge", run_dir, "--judges", bad)
    assert result.exit_code == ExitCode.INPUT and "concurrency" in result.stderr
    unknown = write(
        scored_repo / "unknown.yaml",
        "- provider: openai_compatible\n  model: mystery-model\n"
        "  base_url_env: LOCAL_LLM_BASE_URL\n",
    )
    result = scored_cli("judge", run_dir, "--judges", unknown)
    assert result.exit_code == ExitCode.INPUT and "cannot infer the family" in result.stderr

    def no_family(*args: Any, **kwargs: Any) -> None:
        raise FamilyError("cannot infer the model family of x (openai_compatible:m)")

    monkeypatch.setattr("identitybench.judge.families.output_families_from_manifest", no_family)
    result = scored_cli("judge", run_dir, "--family", "x=qwen")
    assert result.exit_code == ExitCode.INPUT and "cannot infer the model family" in result.stderr
    assert not judged


def test_judge_warns_about_incomplete_runs(
    scored_cli: Invoke,
    run_dir: Path,
    judges_file: Path,
    judged: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "identitybench.scoring.summary.run_completeness",
        lambda run: SimpleNamespace(complete=False),
    )
    result = scored_cli("judge", run_dir)
    assert result.exit_code == 0
    assert "is incomplete; judging what it answered" in result.stderr


# --------------------------------------------------------------------------------------------
# Validating the judges
# --------------------------------------------------------------------------------------------


@pytest.fixture
def validation(monkeypatch: pytest.MonkeyPatch, repo: Path) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    labels = [
        SimpleNamespace(label_id="L1", split="heldout"),
        SimpleNamespace(label_id="L2", split="heldout"),
        SimpleNamespace(label_id="L3", split="dev"),
    ]
    keys = [SimpleNamespace(label_id=f"L{i}", case_id=f"case_{i}") for i in (1, 2, 3)]
    monkeypatch.setattr("identitybench.judge.labels.read_validation_labels", lambda p: labels)

    def read_keys(path: Path) -> list[SimpleNamespace]:
        calls["key_path"] = Path(path)
        return keys

    def targets(cases: Any, chosen: Any) -> list[str]:
        calls["target_keys"] = [k.label_id for k in chosen]
        return [k.label_id for k in chosen]

    async def judge_targets(targets: Any, judges: Any, **kwargs: Any) -> list[Any]:
        calls.update(targets=targets, judges=judges, **kwargs)
        return []

    def validate(labels: Any, scores: Any, **kwargs: Any) -> SimpleNamespace:
        calls["validate"] = kwargs
        return SimpleNamespace(
            to_dict=lambda: {"gate": {"faithfulness": "leaderboard"}},
            to_markdown=lambda: "# Judge validation\n",
            gate={"faithfulness": GateStatus.leaderboard, "auditability": GateStatus.experimental},
            gated_rater="panel",
        )

    monkeypatch.setattr("identitybench.judge.labels.read_label_keys", read_keys)
    monkeypatch.setattr("identitybench.judge.labels.label_targets", targets)
    monkeypatch.setattr("identitybench.judge.run.judge_targets", judge_targets)
    monkeypatch.setattr("identitybench.judge.validate.validate_judge", validate)
    (repo / "labels").mkdir()
    (repo / "labels" / "judge_validation.jsonl").write_text("", encoding="utf-8")
    write(repo / "judges.yaml", (REPO_ROOT / "experiments" / "judges.yaml").read_text())
    return calls


def test_judge_validation_writes_the_report(
    cli: Invoke, repo: Path, validation: dict[str, Any]
) -> None:
    result = cli(
        "judge",
        "--validate",
        "labels/judge_validation.jsonl",
        "--judges",
        "judges.yaml",
    )
    assert result.exit_code == 0, result.output
    # The key and the report sit beside the labeler's directory, never in it: the labeler
    # stays blind to judge scores (SPEC §13.3).
    key_dir = repo.resolve() / "labels-key"
    out = key_dir / "validation"
    assert result.stdout.strip() == str(out / "report.json")
    assert json.loads((out / "report.json").read_text())["gate"]["faithfulness"] == "leaderboard"
    assert (out / "report.md").read_text() == "# Judge validation\n"
    assert (out / "scores.jsonl").read_text() == ""
    assert sorted(p.name for p in (repo / "labels").iterdir()) == ["judge_validation.jsonl"]
    assert validation["key_path"] == key_dir / "key.jsonl"
    assert validation["target_keys"] == ["L1", "L2"]  # only the held-out split
    assert validation["validate"]["split"] == "heldout"
    assert validation["validate"]["panel"] == {
        "claude_cli:claude-sonnet-5-5": "anthropic",
        "codex_cli:gpt-6-astra": "openai",
    }
    assert (
        "faithfulness=leaderboard" in result.stderr and "auditability=experimental" in result.stderr
    )


def test_judge_validation_on_dev_with_explicit_key_and_out(
    cli: Invoke, repo: Path, validation: dict[str, Any]
) -> None:
    result = cli(
        "judge",
        "--validate",
        "labels/judge_validation.jsonl",
        "--judges",
        "judges.yaml",
        "--split",
        "dev",
        "--key",
        "elsewhere/key.jsonl",
        "--out",
        "val",
        "--concurrency",
        "1",
    )
    assert result.exit_code == 0, result.output
    assert validation["key_path"] == Path("elsewhere/key.jsonl")
    assert validation["target_keys"] == ["L3"]
    assert validation["validate"]["split"] == "dev"
    assert validation["concurrency"] == 1
    assert (repo / "val" / "report.json").is_file()


def test_judge_validation_reports_beside_an_explicit_key(
    cli: Invoke, repo: Path, validation: dict[str, Any]
) -> None:
    """With --key and no --out, the report goes beside that key, where a forced re-export into
    the key's directory clears it, never into the labels' default key directory."""
    args = ("judge", "--validate", "labels/judge_validation.jsonl", "--judges", "judges.yaml")
    result = cli(*args, "--key", "secrets/judge-key/key.jsonl")
    assert result.exit_code == 0, result.output
    report = repo.resolve() / "secrets" / "judge-key" / "validation" / "report.json"
    assert Path(result.stdout.strip()).resolve() == report and report.is_file()
    assert not (repo / "labels-key").exists()


def test_judge_validation_input_errors(
    cli: Invoke, repo: Path, validation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "identitybench.judge.labels.read_validation_labels",
        lambda p: [SimpleNamespace(label_id="L9", split="dev")],
    )
    args = ("judge", "--validate", "labels/judge_validation.jsonl", "--judges", "judges.yaml")
    result = cli(*args)
    assert result.exit_code == ExitCode.INPUT and "has no heldout labels" in result.stderr
    result = cli(*args, "--split", "dev")
    assert result.exit_code == ExitCode.INPUT and "lacks 1 labeled rows" in result.stderr


def sent_settings(judge: Judge) -> JudgeInfo:
    """``Judge.info()`` without its endpoint identity, which would run the provider's CLI binary
    (``--version``) from PATH: the settings a judge sends, temperature dropped where the
    provider doesn't take one."""
    takes_temperature = judge.provider.capabilities().supports_temperature
    sent = (
        judge.params if takes_temperature else dataclasses.replace(judge.params, temperature=None)
    )
    return JudgeInfo(
        name=judge.name,
        family=judge.family,
        kind=judge.provider.kind,
        model=judge.provider.model,
        endpoint_identity="test-endpoint",
        params=ParamsRecord.from_params(sent),
    )


def test_judge_flags_judges_not_at_temperature_zero(
    scored_cli: Invoke, run_dir: Path, judges_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_judge(cases: Any, predictions: Any, judges: Any, **kwargs: Any) -> JudgeRun:
        return build_judge_run(
            [],
            judges={j.name: j.family for j in judges},
            rubric_sha256="0" * 64,
            judge_info=[sent_settings(j) for j in judges],  # CLI providers take no temperature
        )

    monkeypatch.setattr("identitybench.judge.run.judge_predictions", fake_judge)
    result = scored_cli("--quiet", "judge", run_dir)
    assert result.exit_code == 0, result.output
    assert "warning: judge claude_cli:claude-sonnet-5-5 did not run at temperature 0" in (
        result.stderr
    )
