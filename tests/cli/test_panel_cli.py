"""``audit panel`` and ``audit adjudicate``: the model-panel oracle audit through the CLI, on a
scored run of baselines. Adjudicators are ``openai_compatible`` entries whose endpoints are
never set: no test here calls a model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from identitybench.cli import ExitCode
from identitybench.panel.adjudication import ADJUDICATION_FILE, MAINTAINER_FILE
from identitybench.panel.flags import (
    LEDGER_FILE,
    PANEL_ARTIFACTS,
    SUMMARY_JSON,
    ClassifiedBy,
    read_ledger,
    read_panel_manifest,
)
from identitybench.providers.base import ModelRequest, ModelResponse
from identitybench.providers.factory import ModelSpec
from tests.cli.conftest import Invoke, usage_text
from tests.judge.helpers import FakeJudgeProvider
from tests.panel.helpers import PANEL as TOY_PANEL
from tests.panel.helpers import toy_run

PANEL = ("always_escalate", "always_approve", "noop_plan")
CONFIG = """\
retries: 1
adjudicators:
  - {provider: openai_compatible, model: m1, label: one, base_url_env: PANEL_TEST_URL}
  - {provider: openai_compatible, model: m2, label: two, base_url_env: PANEL_TEST_URL}
  - {provider: openai_compatible, model: m3, label: three, base_url_env: PANEL_TEST_URL}
"""


def panel_args(*configs: str) -> list[str]:
    return [arg for config in configs for arg in ("--config", config)]


def blind_pass(cli: Invoke, run_dir: Path, *args: str) -> Path:
    result = cli("audit", "panel", run_dir, *panel_args(*PANEL), *args)
    assert result.exit_code == ExitCode.OK, result.output
    return Path(result.stdout.strip()).parent


def write_config(root: Path) -> Path:
    path = root / "panel.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_panel_writes_the_ledger_and_counts(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path
) -> None:
    out = blind_pass(scored_cli, run_dir)
    assert out == scored_repo.resolve() / "audit" / "panel"
    assert {p.name for p in out.iterdir()} == set(PANEL_ARTIFACTS)
    manifest = read_panel_manifest(out / "panel.json")
    assert [c.config_id for c in manifest.panel] == list(PANEL)
    units = read_ledger(out / LEDGER_FILE)
    assert len(units) == manifest.n_units
    summary = json.loads((out / SUMMARY_JSON).read_text(encoding="utf-8"))
    assert summary["units"]["units"] == len(units)
    assert summary["units"]["flagged"] == sum(u.flagged for u in units)


def test_panel_is_deterministic_and_keeps_an_existing_ledger(
    scored_cli: Invoke, run_dir: Path
) -> None:
    out = blind_pass(scored_cli, run_dir)
    first = {name: (out / name).read_bytes() for name in PANEL_ARTIFACTS}
    again = scored_cli("audit", "panel", run_dir, *panel_args(*PANEL))
    assert again.exit_code == ExitCode.INPUT
    assert "already holds a panel audit" in again.stderr
    blind_pass(scored_cli, run_dir, "--force")
    assert {name: (out / name).read_bytes() for name in PANEL_ARTIFACTS} == first


def test_panel_refuses_a_panel_the_run_lacks(scored_cli: Invoke, run_dir: Path) -> None:
    default = scored_cli("audit", "panel", run_dir)
    assert default.exit_code == ExitCode.INPUT
    assert "no configuration 'claude_cli_claude-opus-5-5__baseline__context'" in default.stderr
    short = scored_cli("audit", "panel", run_dir, *panel_args(*PANEL[:2]))
    assert short.exit_code == ExitCode.INPUT and "3 distinct" in short.stderr
    missing = scored_cli("audit", "panel", run_dir.parent / "nope", *panel_args(*PANEL))
    assert missing.exit_code == ExitCode.INPUT and "no such run directory" in missing.stderr


def toy_audit(cli: Invoke, root: Path) -> Path:
    """The blind pass over ``tests.panel.helpers``' toy run (six flagged units), with ``root``
    as the repo root."""
    run = toy_run(root)
    result = cli("--root", root, "audit", "panel", run, *panel_args(*TOY_PANEL))
    assert result.exit_code == ExitCode.OK, result.output
    return root / "audit" / "panel"


def test_adjudicate_dry_run_counts_the_calls(cli: Invoke, tmp_path: Path) -> None:
    out = toy_audit(cli, tmp_path)
    config = write_config(tmp_path)
    result = cli(
        "--root", tmp_path, "audit", "adjudicate", out, "--adjudicators", config, "--dry-run",
        "--limit", "2",
    )  # fmt: skip
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout.startswith("6 flagged units, 2 to adjudicate: 6 calls")
    assert "(up to 12 with retries)" in result.stdout
    assert not (out / ADJUDICATION_FILE).exists()


class Scripted:
    """``make_provider`` for the adjudication config: fake providers answering by label."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.calls = 0

    def __call__(self, spec: ModelSpec) -> FakeJudgeProvider:
        classification = self.replies[spec.model]

        def respond(_request: ModelRequest) -> ModelResponse:
            self.calls += 1
            body = {"classification": classification, "cited_rules": [], "reason": "traced"}
            return ModelResponse(text=json.dumps(body))

        return FakeJudgeProvider(kind=spec.provider, model=spec.model, responder=respond)


def test_adjudicate_records_votes_ties_and_the_protocol(
    cli: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = toy_audit(cli, tmp_path)
    config = write_config(tmp_path)
    scripted = Scripted({"m1": "oracle_bug", "m2": "model_error", "m3": "ambiguous_text"})
    monkeypatch.setattr("identitybench.providers.factory.make_provider", scripted)
    args = ("--root", tmp_path, "audit", "adjudicate", out, "--adjudicators", config)

    first = cli(*args, "--unit", "rq_flag", "--unit", "jml_flag")
    assert first.exit_code == ExitCode.OK, first.output
    assert scripted.calls == 6
    units = {u.unit_id: u for u in read_ledger(out / LEDGER_FILE)}
    assert [len(units[u].votes) for u in ("rq_flag", "jml_flag", "rq_three")] == [3, 3, 0]
    assert units["rq_flag"].classification is None  # a three-way split
    record = json.loads((out / ADJUDICATION_FILE).read_text(encoding="utf-8"))
    assert record["retries"] == 1 and [a["label"] for a in record["adjudicators"]] == [
        "one",
        "two",
        "three",
    ]
    sheet = (out / MAINTAINER_FILE).read_text(encoding="utf-8").splitlines()
    assert [row.split(",")[0] for row in sheet[1:]] == ["jml_flag", "rq_flag"]
    summary = json.loads((out / SUMMARY_JSON).read_text(encoding="utf-8"))["adjudication"]
    assert (summary["adjudicated"], summary["pending"], summary["awaiting_maintainer"]) == (
        2,
        4,
        2,
    )

    # The maintainer classifies one tie; the next pass adjudicates the rest and keeps it.
    (out / MAINTAINER_FILE).write_text(
        "\n".join([sheet[0], "rq_flag,access_request,,model_error,traced APPR-001", sheet[1]])
        + "\n",
        encoding="utf-8",
    )
    scripted.replies["m3"] = "model_error"
    second = cli(*args)
    assert second.exit_code == ExitCode.OK, second.output
    units = {u.unit_id: u for u in read_ledger(out / LEDGER_FILE)}
    assert units["rq_flag"].classified_by is ClassifiedBy.maintainer
    assert units["rq_three"].classified_by is ClassifiedBy.panel
    assert all(u.votes for u in units.values() if u.flagged)
    assert "6 of 6 flagged units adjudicated" in (out / "SUMMARY.md").read_text(encoding="utf-8")

    # Votes under another protocol configuration don't mix with these.
    write_config(tmp_path).write_text(CONFIG.replace("retries: 1", "retries: 0"), "utf-8")
    refused = cli(*args)
    assert refused.exit_code == ExitCode.INPUT and "retries" in refused.stderr
    restarted = cli(*args, "--restart", "--limit", "1")
    assert restarted.exit_code == ExitCode.OK, restarted.output
    units = {u.unit_id: u for u in read_ledger(out / LEDGER_FILE)}
    assert sum(bool(u.votes) for u in units.values()) == 1


def test_adjudicate_refuses_bad_input(scored_cli: Invoke, scored_repo: Path, run_dir: Path) -> None:
    out = blind_pass(scored_cli, run_dir)
    config = write_config(scored_repo)
    both = scored_cli(
        "audit", "adjudicate", out, "--adjudicators", config, "--limit", "1", "--unit", "x"
    )
    assert both.exit_code == ExitCode.USAGE
    assert "--limit and --unit are exclusive" in usage_text(both.stderr)
    unknown = scored_cli(
        "audit", "adjudicate", out, "--adjudicators", config, "--unit", "x", "--dry-run"
    )
    assert unknown.exit_code == ExitCode.INPUT and "not a flagged unit" in unknown.stderr
    not_audit = scored_cli("audit", "adjudicate", scored_repo, "--adjudicators", config)
    assert not_audit.exit_code == ExitCode.INPUT and "not a panel audit" in not_audit.stderr
    no_config = scored_cli("audit", "adjudicate", out, "--adjudicators", scored_repo / "none.yaml")
    assert no_config.exit_code == ExitCode.INPUT and "not found" in no_config.stderr


def test_audit_without_a_subcommand_names_all_five(cli: Invoke) -> None:
    result = cli("audit", "--out", "x")
    assert result.exit_code == ExitCode.USAGE
    assert "export, ingest, panel, adjudicate or missed" in usage_text(result.stderr)


def test_missed_ranks_the_units_beside_the_panel_audit(cli: Invoke, tmp_path: Path) -> None:
    out = toy_audit(cli, tmp_path)
    run = tmp_path / "runs" / "run_1"
    result = cli("--root", tmp_path, "audit", "missed", run, "--top", "3")
    assert result.exit_code == ExitCode.OK, result.output
    assert Path(result.stdout.strip()) == out / "MISSED.md"
    summary = json.loads((out / "missed.json").read_text(encoding="utf-8"))
    assert summary["configs"] == sorted(TOY_PANEL)
    assert summary["missed"][0]["unit_id"] == "rq_split"
    assert (out / LEDGER_FILE).is_file()
    some = cli(
        "--root", tmp_path, "audit", "missed", run, "--config", "opus", "--out", tmp_path / "m"
    )
    assert some.exit_code == ExitCode.OK, some.output
    picked = json.loads((tmp_path / "m" / "missed.json").read_text(encoding="utf-8"))
    assert picked["configs"] == ["opus"]
    bad = cli("--root", tmp_path, "audit", "missed", run, "--config", "sonnet")
    assert bad.exit_code == ExitCode.INPUT and "no configuration 'sonnet'" in bad.stderr
