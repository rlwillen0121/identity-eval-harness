"""``audit export`` and ``audit ingest``: the oracle audit through the CLI, and the
``--judge-labels`` mode's dispatch to ``judge.labels``."""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from identitybench.audit import (
    DISAGREEMENT_CSV_COLUMNS,
    SHEET_CSV_COLUMNS,
    read_audit_keys,
    read_audit_manifest,
)
from identitybench.cli import ExitCode
from identitybench.judge.labels import (
    KEY_FILE,
    LabeledRationale,
    LabelExport,
    LabelIngestError,
    LabelKey,
    ProbeKind,
    default_key_dir,
    read_validation_labels,
)
from identitybench.judge.schema import RubricScores
from identitybench.schemas.enums import Suite, Task
from tests.cli.conftest import Invoke, usage_text
from tests.cli.sheets import filled_csv, gold_answer, read_csv, write_csv

DATASET = Path("datasets") / "identitybench-v1"


def export(cli: Invoke, *args: str | Path) -> Path:
    result = cli("audit", "export", "--n", "15", *args)
    assert result.exit_code == 0, result.output
    return Path(result.stdout.strip()).parent


# --------------------------------------------------------------------------------------------
# Oracle audit: export
# --------------------------------------------------------------------------------------------


def test_export_defaults(cli: Invoke, repo: Path) -> None:
    result = cli("audit", "export")
    assert result.exit_code == ExitCode.OK, result.output
    out = repo.resolve() / "audit" / "oracle"
    assert result.stdout.strip() == str(out / "sheet.csv")
    manifest = read_audit_manifest(out / "audit.json")
    assert manifest.n_cases == 100 and manifest.dataset_path == DATASET.as_posix()
    assert manifest.suites == (Suite.requests, Suite.reviews, Suite.jml)
    assert "without opening key.jsonl" in result.stderr


def test_export_is_deterministic_through_the_cli(cli: Invoke, repo: Path) -> None:
    first = export(cli, "--out", "a", "--seed", "5")
    second = export(cli, "--out", "b", "--seed", "5")
    for name in ("sheet.csv", "sheet.jsonl", "key.jsonl", "audit.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_refuses_to_replace_an_export_unless_forced(cli: Invoke) -> None:
    out = export(cli, "--out", "x")
    keys = (out / "key.jsonl").read_text()
    refused = cli("audit", "export", "--out", "x", "--n", "10")
    assert refused.exit_code == ExitCode.INPUT
    assert "already holds an export" in refused.stderr
    assert (out / "key.jsonl").read_text() == keys
    assert cli("audit", "export", "--out", "x", "--n", "10", "--force").exit_code == 0
    assert len(read_audit_keys(out / "key.jsonl")) == 10


def files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_a_forced_export_leaves_exactly_the_new_export(cli: Invoke, repo: Path) -> None:
    out = export(cli, "--out", "x")
    filled_csv(out, out / "sheet.csv")
    assert cli("audit", "ingest", out / "sheet.csv").exit_code == 0
    (out / "NOTES.md").write_text("mine\n", encoding="utf-8")
    forced = cli("audit", "export", "--out", "x", "--n", "6", "--seed", "2", "--force")
    assert forced.exit_code == 0, forced.output
    fresh = export(cli, "--out", "fresh", "--n", "6", "--seed", "2")
    assert files(out) == files(fresh) | {"NOTES.md": b"mine\n"}


def test_export_suite_split_and_dataset_options(cli: Invoke, repo: Path) -> None:
    out = export(cli, "--out", "req", "--suite", "requests", "--dataset", repo / DATASET)
    assert {k.task for k in read_audit_keys(out / "key.jsonl")} == {Task.access_request}
    missing = cli("audit", "export", "--out", "dev", "--split", "dev")
    assert missing.exit_code == ExitCode.INPUT and "missing suite file" in missing.stderr
    too_many = cli("audit", "export", "--out", "big", "--n", "10000")
    assert too_many.exit_code == ExitCode.INPUT and "families are available" in too_many.stderr
    no_data = cli("audit", "export", "--out", "o", "--dataset", "nowhere")
    assert no_data.exit_code == ExitCode.INPUT


@pytest.mark.parametrize(
    "args",
    [
        ("audit", "export", "--run", "runs/x"),
        ("audit", "export", "--n-dev", "5"),
        ("audit", "export", "--probes", "1"),
        ("audit", "export", "--allow-reused-sources"),
        ("audit", "export", "--family", "a=b"),
        ("audit", "export", "--include-baselines", "oracle"),
        ("audit", "export", "--key-dir", "keys"),
        ("audit", "export", "--judge-labels"),
        (
            "audit",
            "export",
            "--judge-labels",
            "--run",
            "r",
            "--include-baselines",
            "all",
            "--include-baselines",
            "oracle",
        ),
        ("audit", "export", "--judge-labels", "--run", "r", "--key-dir", "audit/judge/key"),
        ("audit", "export", "--judge-labels", "--run", "r", "--out", "j", "--key-dir", "j"),
        ("audit", "export", "--judge-labels", "--run", "r", "--split", "test"),
        ("audit", "export", "--judge-labels", "--run", "r", "--suite", "requests"),
        ("audit", "export", "--n", "0"),
        ("audit", "ingest", "s.csv", "--judge-labels", "--dataset", "d"),
        ("audit", "ingest", "s.csv", "--judge-labels", "--allow-stale"),
        ("audit",),
    ],
)
def test_audit_usage_errors(cli: Invoke, args: tuple[str]) -> None:
    assert cli(*args).exit_code == ExitCode.USAGE


def test_a_key_dir_inside_the_sheets_is_refused_with_the_reason(cli: Invoke) -> None:
    args = ("audit", "export", "--judge-labels", "--run", "r", "--out", "j", "--key-dir", "j/k")
    result = cli(*args)
    assert result.exit_code == ExitCode.USAGE
    assert "the labeler would get the unblinding key with the sheets" in usage_text(result.stderr)


@pytest.mark.parametrize(
    "args", [("--n", "100", "--out", "audit/"), ("--out", "x"), ("--n", "5", "export")]
)
def test_the_spec_audit_form_points_at_the_subcommands(cli: Invoke, args: tuple[str]) -> None:
    result = cli("audit", *args)
    assert result.exit_code == ExitCode.USAGE
    message = usage_text(result.stderr)
    assert "audit takes a subcommand, export or ingest" in message
    assert "audit export --n 100 --out audit/oracle" in message
    assert "audit export --judge-labels --run RUN_DIR --out audit/judge" in message


def test_audit_help_shows_the_spec_form(cli: Invoke) -> None:
    result = cli("audit", "--help")
    assert result.exit_code == 0
    assert "SPEC §16's `audit --n 100 --out audit/` is two exports" in " ".join(
        result.stdout.split()
    )


# --------------------------------------------------------------------------------------------
# Oracle audit: ingest
# --------------------------------------------------------------------------------------------


def test_ingest_gold_answers(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv", rules=True)
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(out / "disagreements.csv")
    assert read_csv(out / "disagreements.csv") == []
    assert (
        "0 disagree" in result.stderr and "oracle_bug, ambiguous_text, human_error" in result.stderr
    )
    assert "15 of 15 cases agree (weighted to the dataset: 1.000)" in result.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert summary["disagree"] == 0 and summary["labeled_cases"] == 15


def test_ingest_writes_disagreements_to_out(cli: Invoke, tmp_path: Path) -> None:
    out = export(cli, "--out", "audit")

    def deny_everything(case: Any, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        if case.task is Task.access_request:
            answer["decision"] = "ESCALATE" if answer["decision"] != "ESCALATE" else "DENY"
            answer.pop("max_duration_days", None)
        return answer

    done = tmp_path / "done.csv"
    filled_csv(out, done, deny_everything)
    result = cli("audit", "ingest", done, "--key", out / "key.jsonl", "--out", tmp_path / "res")
    assert result.exit_code == 0, result.output
    rows = read_csv(tmp_path / "res" / "disagreements.csv")
    requests = [k for k in read_audit_keys(out / "key.jsonl") if k.task is Task.access_request]
    assert len(rows) == len(requests) > 0
    assert {row["kinds"] for row in rows} == {"decision"}


def test_ingest_warns_when_the_dataset_changed(cli: Invoke, repo: Path) -> None:
    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv")
    (repo / DATASET / "NOTES.txt").write_text("changes the fingerprint\n", encoding="utf-8")
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == 0, result.output
    assert "changed since the export" in result.stderr


def test_ingest_problems_are_listed_one_per_line(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    rows = filled_csv(out, out / "sheet.csv")
    rows[0]["audit_id"] = "OA-bogus"
    next(r for r in rows[1:] if r["task"] == "access_request")["decision"] = "MAYBE"
    write_csv(out / "sheet.csv", rows, SHEET_CSV_COLUMNS)
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == ExitCode.INPUT
    lines = result.stderr.splitlines()
    assert lines[0].startswith("error: ") and "problem(s) in the audit sheet:" in lines[0]
    assert any("unknown audit_id 'OA-bogus'" in line for line in lines[1:])
    assert any("decision must be one of" in line for line in lines[1:])


def test_ingest_skips_rows_a_spreadsheet_left_blank(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv")
    with (out / "sheet.csv").open("a", encoding="utf-8") as handle:
        handle.write("," * (len(SHEET_CSV_COLUMNS) - 1) + "\n")
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == 0, result.output


def test_ingest_refuses_stale_labels_unless_allowed(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A label made on a prompt that has changed since the export (here: the system prompt) is
    refused, and with --allow-stale compared and marked."""
    from identitybench.render.prompt import build_request

    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv")

    def reworded(case: Any, variant: str) -> Any:
        request = build_request(case, variant)
        return dataclasses.replace(request, system=request.system + "Reworded.\n")

    monkeypatch.setattr("identitybench.audit.build_request", reworded)
    refused = cli("audit", "ingest", out / "sheet.csv")
    assert refused.exit_code == ExitCode.INPUT
    assert "15 problem(s) in the audit sheet" in refused.stderr
    assert "its system prompt changed since the export" in refused.stderr
    allowed = cli("audit", "ingest", out / "sheet.csv", "--allow-stale")
    assert allowed.exit_code == 0, allowed.output
    # Filled from gold, every stale label agrees, so the disagreement sheet holds none of them:
    # the warning names them and points at results.jsonl, where their drift is recorded.
    assert read_csv(out / "disagreements.csv") == []
    ids = list(dict.fromkeys(k.audit_id for k in read_audit_keys(out / "key.jsonl")))
    message = " ".join(allowed.stderr.split())
    assert (
        "warning: since the export: 15 labeled case(s) now show another prompt "
        f"(stale: {', '.join(ids[:5])} ... and 10 more), 0 unit(s) have new gold on an "
        f"unchanged prompt (oracle changes); every unit's drift is in {out / 'results.jsonl'} "
        "(field drift)"
    ) in message
    summary = json.loads((out / "summary.json").read_text())
    assert summary["by_drift"]["system"] == summary["stale_units"] == summary["labeled_units"]


def test_ingest_names_units_whose_gold_changed_even_when_they_agree(cli: Invoke) -> None:
    """An oracle change under an unchanged prompt is compared (no --allow-stale needed); a
    unit that still agrees appears in no disagreement row, so the warning names its case."""
    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv")
    lines = (out / "key.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["gold_sha256"] = ["0" * 64 for _ in first["gold_sha256"]]  # as if gold had changed
    (out / "key.jsonl").write_text(
        "\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8"
    )
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == 0, result.output
    assert read_csv(out / "disagreements.csv") == []
    changed = len(first["gold_sha256"])
    message = " ".join(result.stderr.split())
    assert (
        f"since the export: 0 labeled case(s) now show another prompt (stale), {changed} "
        f"unit(s) have new gold on an unchanged prompt (oracle changes, in {first['audit_id']})"
    ) in message
    assert "the disagreement sheet's drift column marks only the units that disagree" in message
    results = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert {r["audit_id"] for r in results if r["drift"] == ["gold"]} == {first["audit_id"]}


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_ingesting_in_the_wrong_mode_says_so_briefly(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path, fmt: str
) -> None:
    """One line naming the fix, never a page of schema errors or an oracle key's fields."""
    oracle = scored_cli("audit", "export", "--n", "15", "--out", "oracle")
    assert oracle.exit_code == 0, oracle.output
    base = ("audit", "export", "--judge-labels", "--run", run_dir, "--allow-reused-sources")
    judge = scored_cli(
        *base, "--include-baselines", "all", "--n", "12", "--n-dev", "4", "--probes", "1"
    )
    assert judge.exit_code == 0, judge.output
    labels = scored_repo / "audit" / "judge" / f"heldout.{fmt}"
    as_oracle = scored_cli("audit", "ingest", labels)
    assert as_oracle.exit_code == ExitCode.INPUT
    assert as_oracle.stderr.splitlines() == [
        f"error: {labels} is a judge-label sheet; add --judge-labels"
    ]
    sheet = scored_repo / "oracle" / f"sheet.{fmt}"
    as_labels = scored_cli("audit", "ingest", sheet, "--judge-labels")
    assert as_labels.exit_code == ExitCode.INPUT
    assert as_labels.stderr.splitlines() == [
        f"error: {sheet} is an oracle-audit sheet, not judge labels; drop --judge-labels"
    ]
    # A sheet with the other kind of export's key, in either mode: the pair is the problem, so
    # the advice is the key (advice about the flag would only move the error to the other file).
    oracle_key = scored_repo / "oracle" / "key.jsonl"
    judge_key = scored_repo / "audit" / "judge-key" / "key.jsonl"
    mixed = [
        (labels, oracle_key, "a judge-label", "an oracle-audit"),
        (sheet, judge_key, "an oracle-audit", "a judge-label"),
    ]
    results = [as_oracle, as_labels]
    for sheet_path, key, sheet_kind, key_kind in mixed:
        for mode in ((), ("--judge-labels",)):
            result = scored_cli("audit", "ingest", sheet_path, *mode, "--key", key)
            assert result.exit_code == ExitCode.INPUT
            assert result.stderr.splitlines() == [
                f"error: {sheet_path} is {sheet_kind} sheet but {key} is {key_kind} key; pass "
                "the key exported with this sheet (--key)"
            ]
            results.append(result)
    # A key of the right kind for the sheet but not for the mode: the flag is the fix.
    both_judge = scored_cli("audit", "ingest", labels, "--key", judge_key)
    assert both_judge.stderr.splitlines() == [
        f"error: {labels} is a judge-label sheet; add --judge-labels"
    ]
    for result in results:
        for hidden in ("tier", "world_id", "weight", "errors.pydantic.dev", "Traceback"):
            assert hidden not in result.stderr


def test_ingest_partial_and_missing_inputs(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    assert cli("audit", "ingest", out / "sheet.csv").exit_code == ExitCode.INPUT  # blank sheet
    partial = cli("audit", "ingest", out / "sheet.csv", "--allow-partial")
    assert partial.exit_code == 0, partial.output
    assert (
        "0 units compared" in partial.stderr and "(weighted to the dataset: n/a)" in partial.stderr
    )
    missing = cli("audit", "ingest", out / "nope.csv")
    assert missing.exit_code == ExitCode.INPUT and "sheet not found" in missing.stderr
    no_key = cli("audit", "ingest", out / "sheet.csv", "--key", out / "nokey.jsonl")
    assert no_key.exit_code == ExitCode.INPUT


# --------------------------------------------------------------------------------------------
# --judge-labels
# --------------------------------------------------------------------------------------------


@pytest.fixture
def label_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake_export(
        cases: Sequence[Any], predictions: Sequence[Any], out_dir: Path, **kwargs: Any
    ) -> LabelExport:
        calls.update(cases=cases, predictions=predictions, out_dir=Path(out_dir), **kwargs)
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        keys = Path(kwargs.get("key_dir") or default_key_dir(root))
        keys.mkdir(parents=True, exist_ok=True)
        (keys / KEY_FILE).write_text(f"key of {len(calls['cases'])} cases\n", encoding="utf-8")
        paths = {"dev_csv": root / "dev.csv", "heldout_csv": root / "heldout.csv"}
        for path in paths.values():
            path.write_text("label_id\n", encoding="utf-8")
        return LabelExport(
            keys=(cast(LabelKey, object()),),  # one row exported: the counts are a note
            paths=MappingProxyType(paths),
            excluded_baselines=MappingProxyType({"oracle": 5, "always_approve": 4}),
        )

    monkeypatch.setattr("identitybench.judge.labels.export_label_sheets", fake_export)
    return calls


def test_judge_label_export_dispatches_with_defaults(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path, label_export: dict[str, Any]
) -> None:
    result = scored_cli("audit", "export", "--judge-labels", "--run", run_dir)
    assert result.exit_code == 0, result.output
    out = scored_repo.resolve() / "audit" / "judge"
    key_dir = scored_repo.resolve() / "audit" / "judge-key"
    assert result.stdout.splitlines() == [str(out / "dev.csv"), str(out / "heldout.csv")]
    # Sheets and key were staged beside their targets, then moved in together.
    staging: Path = label_export["out_dir"]
    key_staging = Path(label_export["key_dir"])
    assert staging.parent == out.parent and not staging.exists()
    assert key_staging.parent == key_dir.parent and not key_staging.exists()
    assert (key_dir / KEY_FILE).is_file() and not (out / KEY_FILE).exists()
    assert sorted(p.name for p in out.parent.iterdir()) == ["judge", "judge-key"]
    message = " ".join(result.stderr.split())
    assert f"warning: the unblinding key is {key_dir / KEY_FILE}: keep it from the labeler" in (
        message
    )
    assert "9 baseline rationale(s) left out (always_approve 4, oracle 5)" in message
    assert "warning: no model rationale" not in message
    assert (label_export["n_labels"], label_export["n_dev"], label_export["seed"]) == (200, 50, 0)
    assert label_export["probes"] == {kind: 10 for kind in ProbeKind}
    assert label_export["allow_reused_sources"] is False
    assert label_export["include_baselines"] is False
    assert set(label_export["output_families"].values()) == {"baseline"}
    assert len(label_export["predictions"]) > len(label_export["cases"]) > 0
    before = files(out.parent)
    again = scored_cli("audit", "export", "--judge-labels", "--run", run_dir, "--n", "40")
    assert again.exit_code == ExitCode.INPUT and "already holds an export" in again.stderr
    assert files(out.parent) == before  # sheets being filled and their key, byte for byte
    # The key alone is enough to refuse: a new export elsewhere would overwrite it.
    elsewhere = ("audit", "export", "--judge-labels", "--run", run_dir, "--out", "other")
    key_clash = scored_cli(*elsewhere, "--key-dir", key_dir)
    assert key_clash.exit_code == ExitCode.INPUT
    assert f"{key_dir} already holds an export (key.jsonl)" in " ".join(key_clash.stderr.split())


def test_judge_label_export_options(
    scored_cli: Invoke, run_dir: Path, label_export: dict[str, Any]
) -> None:
    result = scored_cli(
        "audit", "export", "--judge-labels", "--run", run_dir, "--run", run_dir,
        "--n", "40", "--n-dev", "0", "--probes", "2", "--allow-reused-sources",
        "--family", "oracle=baseline", "--seed", "7", "--out", "labels",
        "--include-baselines", "oracle", "--include-baselines", "always_approve",
        "--key-dir", "private/keys",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (label_export["n_labels"], label_export["n_dev"], label_export["seed"]) == (40, 0, 7)
    assert label_export["probes"] == {kind: 2 for kind in ProbeKind}
    assert label_export["allow_reused_sources"] is True
    assert label_export["include_baselines"] == frozenset({"oracle", "always_approve"})
    assert Path(label_export["key_dir"]).parent == Path("private").resolve()
    assert Path("private", "keys", KEY_FILE).is_file()
    everything = scored_cli(
        "audit", "export", "--judge-labels", "--run", run_dir, "--out", "all",
        "--include-baselines", "all",
    )  # fmt: skip
    assert everything.exit_code == 0, everything.output
    assert label_export["include_baselines"] is True
    assert len({c.case_id for c in label_export["cases"]}) == len(label_export["cases"])
    missing = scored_cli("audit", "export", "--judge-labels", "--run", "runs/none", "--out", "z")
    assert missing.exit_code == ExitCode.INPUT and "no such run directory" in missing.stderr


def labeled(label_id: str, split: str = "heldout") -> LabeledRationale:
    return LabeledRationale(
        rubric_file_sha256="r" * 64,
        export_id="export-1",
        label_id=label_id,
        split=split,  # type: ignore[arg-type]
        source_target_id=f"t-{label_id}",
        run_id="r",
        config_id="oracle",
        case_id="case_1",
        family_id="fam_1",
        task=Task.access_request,
        sample_idx=0,
        correct=True,
        output_family="baseline",
        run_alias="run-alias-1",
        config_alias="config-alias-1",
        rationale="Approved under the policy.",
        human=RubricScores(faithfulness=4, decisive_coverage=3, actionability=2, auditability=4),
    )


def test_judge_label_ingest_merges_into_the_labels_file(
    cli: Invoke, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    batches = iter([[labeled("L1", "dev"), labeled("L2")], [labeled("L2"), labeled("L3")]])

    def fake_ingest(completed: Path, keys: Path, **kwargs: Any) -> list[LabeledRationale]:
        calls.append({"completed": Path(completed), "keys": Path(keys), **kwargs})
        return next(batches)

    monkeypatch.setattr("identitybench.judge.labels.ingest_labels", fake_ingest)
    sheets = repo / "audit" / "judge"
    sheets.mkdir(parents=True)
    for name in ("dev.csv", "heldout.csv"):
        (sheets / name).write_text("label_id\n", encoding="utf-8")
    first = cli("audit", "ingest", sheets / "dev.csv", "--judge-labels")
    assert first.exit_code == 0, first.output
    target = sheets / "judge_validation.jsonl"
    assert first.stdout.strip() == str(target)
    assert calls[0] == {
        "completed": sheets / "dev.csv",
        "keys": default_key_dir(sheets) / KEY_FILE,  # beside the sheets, never among them
        "require_complete": True,
    }
    second = cli("audit", "ingest", sheets / "heldout.csv", "--judge-labels", "--allow-partial")
    assert second.exit_code == 0, second.output
    assert calls[1]["require_complete"] is False
    assert [row.label_id for row in read_validation_labels(target)] == ["L1", "L2", "L3"]
    assert "1 earlier labels kept" in second.stderr


def test_judge_label_ingest_errors(
    cli: Invoke, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(*args: Any, **kwargs: Any) -> None:
        raise LabelIngestError(["dev.csv row 2: unknown label_id 'x'", "dev.csv row 3: bad score"])

    monkeypatch.setattr("identitybench.judge.labels.ingest_labels", reject)
    sheet = repo / "dev.csv"
    sheet.write_text("label_id\n", encoding="utf-8")
    result = cli("audit", "ingest", sheet, "--judge-labels", "--out", "labels.jsonl")
    assert result.exit_code == ExitCode.INPUT
    assert "2 problem(s) in the label sheet" in result.stderr
    assert "  dev.csv row 3: bad score" in result.stderr.splitlines()
    assert not (repo / "labels.jsonl").exists()
    missing = cli("audit", "ingest", repo / "none.csv", "--judge-labels")
    assert missing.exit_code == ExitCode.INPUT and "sheet not found" in missing.stderr


def test_judge_label_export_refuses_conflicting_families(
    scored_cli: Invoke,
    scored_repo: Path,
    run_dir: Path,
    label_export: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = iter([{"oracle": "baseline"}, {"oracle": "anthropic"}])
    monkeypatch.setattr(
        "identitybench.judge.families.output_families_from_manifest",
        lambda manifest, overrides: next(families),
    )
    other = scored_repo / "runs" / "copy"
    shutil.copytree(run_dir, other)
    result = scored_cli("audit", "export", "--judge-labels", "--run", run_dir, "--run", other)
    assert result.exit_code == ExitCode.INPUT
    assert "config oracle has different families in different runs" in result.stderr
    assert not label_export


# The toy run holds baselines only, which judge labels leave out unless named.
LABEL_EXPORT = ("--allow-reused-sources", "--include-baselines", "all")


def test_a_forced_judge_label_export_drops_the_earlier_labels(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path
) -> None:
    """With the real ``judge.labels`` back end: nothing of the first export, its key, its
    ingested labels or its validation report survives a forced re-export."""
    base = ("audit", "export", "--judge-labels", "--run", run_dir, *LABEL_EXPORT)
    first = scored_cli(*base, "--n", "12", "--n-dev", "4", "--probes", "1", "--out", "lab")
    assert first.exit_code == 0, first.output
    lab, lab_key = scored_repo / "lab", scored_repo / "lab-key"
    assert (lab_key / KEY_FILE).is_file() and not (lab / KEY_FILE).exists()
    (lab / "judge_validation.jsonl").write_text("{}\n", encoding="utf-8")
    (lab_key / "validation").mkdir()
    for name in ("scores.jsonl", "report.json", "report.md"):
        (lab_key / "validation" / name).write_text("old\n", encoding="utf-8")
    second = ("--n", "6", "--n-dev", "2", "--probes", "0", "--seed", "3")
    forced = scored_cli(*base, *second, "--out", "lab", "--force")
    assert forced.exit_code == 0, forced.output
    fresh = scored_cli(*base, *second, "--out", "fresh")
    assert fresh.exit_code == 0, fresh.output
    assert files(lab) == files(scored_repo / "fresh")
    assert files(lab_key) == files(scored_repo / "fresh-key")
    assert not (lab_key / "validation").exists()


def test_a_failed_forced_judge_label_export_keeps_the_earlier_one(
    scored_cli: Invoke, scored_repo: Path, run_dir: Path
) -> None:
    """With the real back end: an export that fails (too few rationales) after --force must not
    take the earlier export, its key, its ingested labels or its validation results with it."""
    base = ("audit", "export", "--judge-labels", "--run", run_dir, *LABEL_EXPORT)
    first = scored_cli(*base, "--n", "12", "--n-dev", "4", "--probes", "1", "--out", "lab")
    assert first.exit_code == 0, first.output
    lab, lab_key = scored_repo / "lab", scored_repo / "lab-key"
    (lab / "judge_validation.jsonl").write_text("{}\n", encoding="utf-8")
    (lab_key / "validation").mkdir()
    (lab_key / "validation" / "report.json").write_text("{}\n", encoding="utf-8")
    before = files(lab), files(lab_key)
    failed = scored_cli(*base, "--n", "100000", "--out", "lab", "--force")
    assert failed.exit_code == ExitCode.INPUT
    assert "real rows requested" in failed.stderr
    assert (files(lab), files(lab_key)) == before
    assert sorted(p.name for p in scored_repo.iterdir() if p.name.startswith(".lab")) == []


def test_without_baselines_the_toy_run_has_nothing_to_label(
    scored_cli: Invoke, run_dir: Path
) -> None:
    result = scored_cli("audit", "export", "--judge-labels", "--run", run_dir, "--out", "lab")
    assert result.exit_code == ExitCode.INPUT
    message = " ".join(result.stderr.split())
    assert "only 0 rationales" in message and "baseline rationales left out" in message
    assert "pass --include-baselines NAME (or all) to label them" in message
    assert "include_baselines" not in message


def test_ingest_explains_a_dataset_exported_from_outside_the_repo(
    cli: Invoke, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-ds"
    shutil.copytree(repo / DATASET, outside)
    out = export(cli, "--out", "audit", "--dataset", outside)
    assert read_audit_manifest(out / "audit.json").dataset_path == "outside-ds"
    filled_csv(out, out / "sheet.csv")
    result = cli("audit", "ingest", out / "sheet.csv")
    assert result.exit_code == ExitCode.INPUT
    assert "changed since the export" not in result.stderr
    (line,) = result.stderr.splitlines()
    assert line == (
        f"error: dataset directory not found: {repo.resolve() / 'outside-ds'} "
        "(exported from 'outside-ds'); pass --dataset DIR"
    )
    given = cli("audit", "ingest", out / "sheet.csv", "--dataset", outside)
    assert given.exit_code == 0, given.output
    assert "changed since the export" not in given.stderr
    missing = cli("audit", "ingest", out / "sheet.csv", "--dataset", "nowhere")
    assert missing.exit_code == ExitCode.INPUT
    assert missing.stderr.strip() == (
        "error: dataset directory not found: nowhere; pass --dataset DIR"
    )


def flip_reviews(case: Any, item_id: str | None) -> dict[str, Any]:
    answer = gold_answer(case, item_id)
    if case.task is Task.access_review:
        answer["decision"] = "REVOKE" if answer["decision"] != "REVOKE" else "CERTIFY"
    return answer


def test_ingest_reports_the_classifying_state(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    filled_csv(out, out / "sheet.csv", flip_reviews)
    first = cli("audit", "ingest", out / "sheet.csv")
    assert first.exit_code == 0, first.output
    rows = read_csv(out / "disagreements.csv")
    assert len(rows) > 2
    rows[0]["classification"] = "oracle bug"
    rows[1]["classification"] = "not sure"
    write_csv(out / "disagreements.csv", rows, DISAGREEMENT_CSV_COLUMNS)
    again = cli("audit", "ingest", out / "sheet.csv")
    assert again.exit_code == 0, again.output
    n = len(rows)
    assert f"disagreement sheet: {n} open ({n - 1} to classify), 0 resolved, 0 carried" in (
        " ".join(again.stderr.split())
    )
    unit = f"{rows[1]['audit_id']} item {rows[1]['item_id']}"
    assert f"warning: {out / 'disagreements.csv'}: {unit}: unknown classification" in again.stderr
    assert read_csv(out / "disagreements.csv")[0]["classification"] == "oracle_bug"


def test_partial_ingest_through_the_cli_says_what_it_held_back(cli: Invoke) -> None:
    out = export(cli, "--out", "audit")
    rows = filled_csv(out, out / "sheet.csv")
    reviews = Counter(r["audit_id"] for r in rows if r["task"] == Task.access_review.value)
    campaign, size = reviews.most_common(1)[0]
    assert size > 1
    next(r for r in rows if r["audit_id"] == campaign).update(decision="")
    write_csv(out / "sheet.csv", rows, SHEET_CSV_COLUMNS)
    result = cli("audit", "ingest", out / "sheet.csv", "--allow-partial")
    assert result.exit_code == 0, result.output
    assert f"(1 unlabeled, {size - 1} more held back until their campaign is fully labeled)" in (
        " ".join(result.stderr.split())
    )
