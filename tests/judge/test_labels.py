"""Blind labeling sheets: stratified export, designed probes, blinding, determinism, ingest."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from identitybench.judge import labels as labels_module
from identitybench.judge import run as judge_run
from identitybench.judge.families import BASELINE_FAMILY
from identitybench.judge.labels import (
    KEY_FILE,
    SHEET_CSV_COLUMNS,
    LabeledRationale,
    LabelIngestError,
    LabelKey,
    ProbeKind,
    default_key_dir,
    export_label_sheets,
    ingest_labels,
    label_targets,
    make_probe,
    rationale_fingerprint,
    read_label_keys,
    read_validation_labels,
    write_validation_labels,
)
from identitybench.judge.rubric import (
    DIMENSIONS,
    RUBRIC_FILE,
    SOURCE_PROMPTS_DIR,
    SYSTEM_FILE,
    RubricError,
    default_rubric,
    load_rubric,
)
from identitybench.judge.sampling import allocate
from identitybench.judge.targets import build_targets, output_for, predicted_label
from identitybench.policy.render import render_policy_text
from identitybench.schemas.case import Case
from identitybench.schemas.enums import RequestDecision, Task
from identitybench.schemas.evidence import index_view
from identitybench.schemas.outputs import ReviewOutput
from identitybench.schemas.predictions import Prediction, PredictionStatus, ToolTrace
from identitybench.scoring.served import ServedIndexError
from identitybench.world.ids import CONTROL
from tests.scoring.builders import identity, leaver_case, oracle_output, predict
from tests.scoring.served_cases import TOOL_CONFIG, FakeServed, trace

from .helpers import (
    CONFIG_ANTHROPIC,
    CONFIG_OPENAI,
    CONFIG_QWEN,
    FAMILIES,
    RUN_ID,
    SERVED,
    baseline_prediction,
    failed_prediction,
    request_case,
    request_prediction,
    review_case,
    review_prediction,
)

CONFIGS = (CONFIG_ANTHROPIC, CONFIG_OPENAI, CONFIG_QWEN)
PROBES = {kind: 3 for kind in ProbeKind}
N_LABELS, N_DEV = 36, 8


def population() -> tuple[list[Case], list[Prediction]]:
    """16 requests and one 3-item review campaign, answered by three configurations with a mix
    of right and wrong decisions: 57 judgeable rationales."""
    cases: list[Case] = []
    predictions: list[Prediction] = []
    for i in range(16):
        approve = i % 2 == 1
        gold = RequestDecision.APPROVE if approve else RequestDecision.DENY
        case = request_case(
            f"c_lab_{i:02d}",
            decision=gold,
            requested_days=30 if approve else None,
            gold_days=30 if approve else None,
        )
        cases.append(case)
        for j, config in enumerate(CONFIGS):
            decision = gold if (i + j) % 3 else RequestDecision.ESCALATE
            rationale = f"Case {i} by {j}: {decision.value} because SOD-002 and mc_017 expired."
            predictions.append(
                request_prediction(case, config, decision=decision, rationale=rationale)
            )
    review = review_case("c_lab_rev")
    cases.append(review)
    predictions += [review_prediction(review, config) for config in CONFIGS]
    return cases, predictions


def export(tmp_path: Path, name: str = "sheets", **kwargs: Any) -> labels_module.LabelExport:
    cases, predictions = population()
    options: dict[str, Any] = {"n_labels": N_LABELS, "n_dev": N_DEV, "probes": PROBES, "seed": 11}
    options.update(kwargs)
    return export_label_sheets(cases, predictions, tmp_path / name, **options)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_export_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    first = export(tmp_path, "one")
    cases, predictions = population()
    second = export_label_sheets(
        list(reversed(cases)),
        list(reversed(predictions)),
        tmp_path / "two",
        n_labels=N_LABELS,
        n_dev=N_DEV,
        probes=PROBES,
        seed=11,
    )
    assert set(first.paths) == {
        "dev_jsonl",
        "dev_csv",
        "heldout_jsonl",
        "heldout_csv",
        "key",
        "policies",
        "views",
    }
    assert first.keys == second.keys
    assert _tree(tmp_path / "one") == _tree(tmp_path / "two")
    assert (
        export(tmp_path, "three", seed=12).paths["key"].read_bytes()
        != first.paths["key"].read_bytes()
    )


def test_failed_calls_dont_block_export_without_output_families(tmp_path: Path) -> None:
    """Errored and refused predictions report no served model; they are never labeled, so they
    neither block family inference nor change which rationales are drawn."""
    cases, predictions = population()
    failures = [
        failed_prediction(case, config, status=status)
        for case, config, status in (
            (cases[0], CONFIG_OPENAI, PredictionStatus.error),
            (cases[1], CONFIG_QWEN, PredictionStatus.refused),
        )
    ]
    # The failed calls replace those configurations' answers to the same cases.
    replaced = {(f.config_id, f.case_id) for f in failures}
    kept = [p for p in predictions if (p.config_id, p.case_id) not in replaced]
    options: dict[str, Any] = {"n_labels": 30, "n_dev": 8, "probes": PROBES, "seed": 11}
    result = export_label_sheets(cases, kept + failures, tmp_path / "with", **options)
    assert len(result.keys) == 30
    assert {k.output_family for k in result.keys} == set(FAMILIES.values())
    assert not replaced & {(k.config_id, k.case_id) for k in result.keys}
    baseline = export_label_sheets(cases, kept, tmp_path / "without", **options)
    assert sorted((k.source_target_id, k.probe) for k in result.keys) == sorted(
        (k.source_target_id, k.probe) for k in baseline.keys
    )


def test_split_sizes_and_probe_placement(tmp_path: Path) -> None:
    keys = export(tmp_path).keys
    assert len(keys) == N_LABELS
    assert [k.label_id for k in keys] == sorted(k.label_id for k in keys)
    assert len({k.label_id for k in keys}) == N_LABELS
    dev = [k for k in keys if k.split == "dev"]
    heldout = [k for k in keys if k.split == "heldout"]
    assert len(dev) == N_DEV and len(heldout) == N_LABELS - N_DEV
    assert all(k.probe is None for k in dev)
    assert Counter(k.probe for k in heldout if k.probe) == PROBES
    real_sources = [k.source_target_id for k in keys if k.probe is None]
    assert len(set(real_sources)) == len(real_sources)
    probe_sources = [k.source_target_id for k in keys if k.probe is not None]
    assert len(set(probe_sources)) == len(probe_sources)
    # Enough unlabeled rationales: no probe shares a source with a labeled row.
    assert not set(probe_sources) & set(real_sources)


def test_real_rows_are_balanced_across_model_decision_and_correctness(tmp_path: Path) -> None:
    cases, predictions = population()
    targets = build_targets(cases, predictions, FAMILIES)

    def stratum(config_id: str, label: str, correct: bool) -> str:
        return f"{config_id}|{label}|{correct}"

    sizes = Counter(
        stratum(t.prediction.config_id, predicted_label(t.prediction, t.item_id), t.correct)
        for t in targets
    )
    keys = export(tmp_path).keys
    got = Counter(
        stratum(k.config_id, predicted_label(k.prediction, k.item_id), k.correct)
        for k in keys
        if k.probe is None
    )
    strata = sorted(sizes)
    expected = allocate([sizes[s] for s in strata], N_LABELS - sum(PROBES.values()), "balanced")
    assert [got[s] for s in strata] == expected
    assert len(got) == len(sizes)  # every stratum is represented


def test_sheets_are_blind(tmp_path: Path) -> None:
    result = export(tmp_path)
    sheet_files = sorted(p for p in (tmp_path / "sheets").rglob("*") if p.is_file())
    assert len(sheet_files) > 4  # the policy and view files are checked too
    aliases = {k.config_alias for k in result.keys} | {k.run_alias for k in result.keys}
    for path in sheet_files:
        text = path.read_text(encoding="utf-8")
        for secret in (*CONFIGS, *SERVED.values(), RUN_ID, "raw-text-sentinel", "provenance"):
            assert secret not in text, (path.name, secret)
        for alias in aliases:
            assert alias not in text, (path.name, alias)
        for field in ("probe", "output_family", "source_target_id", "config_id", "judge"):
            assert f'"{field}"' not in text and f",{field}," not in text
    key_text = result.paths["key"].read_text(encoding="utf-8")
    assert CONFIG_OPENAI in key_text and '"probe":"judge_injection"' in key_text


def test_the_key_is_written_outside_the_sheet_directory(tmp_path: Path) -> None:
    """Handing the labeler the sheet directory must not hand them the unblinded key."""
    result = export(tmp_path)
    sheets = tmp_path / "sheets"
    assert default_key_dir(sheets) == (tmp_path / "sheets-key").resolve()
    assert result.paths["key"] == default_key_dir(sheets) / KEY_FILE
    assert result.paths["key"].is_file()
    assert sheets.resolve() not in result.paths["key"].resolve().parents
    assert not any(p.name == KEY_FILE for p in sheets.rglob("*"))
    explicit = export(tmp_path, "explicit", key_dir=tmp_path / "private" / "keys")
    assert explicit.paths["key"] == tmp_path / "private" / "keys" / KEY_FILE
    assert explicit.paths["key"].read_bytes() == result.paths["key"].read_bytes()
    assert not (tmp_path / "explicit-key").exists()
    for inside in (tmp_path / "bad", tmp_path / "bad" / "keys", tmp_path / "bad" / "." / "k"):
        with pytest.raises(ValueError, match="inside the sheet directory"):
            export(tmp_path, "bad", key_dir=inside)
    assert not (tmp_path / "bad").exists()  # refused before anything was written


def test_label_ids_cannot_be_recomputed_from_what_the_labeler_knows(tmp_path: Path) -> None:
    cases, predictions = population()
    result = export(tmp_path)
    label_ids = {k.label_id for k in result.keys}
    # The labeler knows the run, the configurations and the seed, so every target ID: an ID
    # hashed from those alone would give the rows away.
    targets = build_targets(cases, predictions, FAMILIES)
    guesses = {
        "lbl_" + hashlib.sha256(f"label|{seed}|{t.target_id}|{kind}".encode()).hexdigest()[:16]
        for t in targets
        for seed in (0, 11)
        for kind in ("real", *(k.value for k in ProbeKind))
    }
    assert not guesses & label_ids
    # The IDs depend on content the sheets never show: changing only the raw text changes
    # every ID, while the rows the labeler sees stay the same.
    hidden = [p.model_copy(update={"raw_text": "other raw text"}) for p in predictions]
    other = export_label_sheets(
        cases,
        hidden,
        tmp_path / "hidden",
        n_labels=N_LABELS,
        n_dev=N_DEV,
        probes=PROBES,
        seed=11,
    )
    assert not label_ids & {k.label_id for k in other.keys}

    def shown(path: Path) -> list[str]:
        rows = [{k: v for k, v in row.items() if k != "label_id"} for row in _jsonl(path)]
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    for split in ("dev_jsonl", "heldout_jsonl"):
        assert shown(result.paths[split]) == shown(other.paths[split])


def test_sheet_rows_carry_what_the_judge_sees(tmp_path: Path) -> None:
    result = export(tmp_path)
    root = tmp_path / "sheets"
    rows = _jsonl(result.paths["heldout_jsonl"])
    keys = {k.label_id: k for k in result.keys}
    cases = {c.case_id: c for c in population()[0]}
    assert [row["label_id"] for row in rows] == sorted(row["label_id"] for row in rows)
    for row in rows:
        key = keys[row["label_id"]]
        case = cases[key.case_id]
        # The policy text the judge gets (undated: the as_of column gives the date).
        policy = (root / row["policy_ref"]).read_text(encoding="utf-8")
        assert policy == render_policy_text(case.view.policy_pack, {case.task})
        assert "SOD-002" in policy
        view = json.loads((root / row["view_ref"]).read_text(encoding="utf-8"))
        assert view == row["view"]
        assert row["view"]["as_of"] == "2026-10-10" and "policy_pack" not in row["view"]
        assert row["gold"]["certificates"] is not None
        assert row["decision_correct"] is key.correct
        assert row["rationale"] == output_for(key.prediction, key.item_id).rationale
        assert row["rationale_words"] == len(row["rationale"].split())
        assert all(row[dim] is None for dim in DIMENSIONS) and row["notes"] == ""
    # One policy file per distinct text: requests and reviews render different sections.
    tasks = {cases[k.case_id].task for k in result.keys}
    assert len(list((root / "policies").iterdir())) == len(tasks) == 2
    reader = csv.DictReader(io.StringIO(result.paths["dev_csv"].read_text(encoding="utf-8")))
    assert tuple(reader.fieldnames or ()) == SHEET_CSV_COLUMNS
    csv_rows = list(reader)
    assert len(csv_rows) == N_DEV and "view" not in csv_rows[0]
    for row in csv_rows:
        assert (root / row["policy_ref"]).is_file() and (root / row["view_ref"]).is_file()
    assert json.loads(csv_rows[0]["gold"])["decision"] in {
        "DENY",
        "APPROVE",
        "REVOKE",
        "CERTIFY",
        "ESCALATE",
    }


def _probe_keys(keys: tuple[LabelKey, ...], kind: ProbeKind) -> list[LabelKey]:
    return [k for k in keys if k.probe is kind]


def _source(cases: list[Case], predictions: list[Prediction], key: LabelKey):
    return next(
        t
        for t in build_targets(cases, predictions, FAMILIES)
        if t.target_id == key.source_target_id
    )


def test_persuasive_probes_add_unsupported_specifics(tmp_path: Path) -> None:
    cases, predictions = population()
    keys = export(tmp_path).keys
    by_case = {c.case_id: c for c in cases}
    probes = _probe_keys(keys, ProbeKind.persuasive_unsupported)
    # Wrong decisions are available, so they are used first.
    assert not any(k.correct for k in probes)
    for key in probes:
        source = _source(cases, predictions, key)
        text = output_for(key.prediction, key.item_id).rationale
        assert source.rationale in text and len(text) > len(source.rationale)
        assert any(text.startswith(opener) for opener in labels_module._CONFIDENT_OPENERS)
        pattern = r"\b(?:CHG-\d{5}|mc_\d{3})\b"
        invented = set(re.findall(pattern, text)) - set(re.findall(pattern, source.rationale))
        assert not invented & set(index_view(by_case[key.case_id].view))
        # Only the rationale changed.
        assert output_for(key.prediction, key.item_id).decision == source.output.decision  # type: ignore[union-attr]
        assert key.prediction.raw_text is None


def test_terse_and_injection_probes(tmp_path: Path) -> None:
    cases, predictions = population()
    keys = export(tmp_path).keys
    for key in _probe_keys(keys, ProbeKind.correct_terse):
        text = output_for(key.prediction, key.item_id).rationale
        assert key.correct and len(text.split()) <= 8
        assert (
            "SOD-002" in text
            or "no rule" in text.lower()
            or "nothing in the policy" in text.lower()
        )
    for key in _probe_keys(keys, ProbeKind.judge_injection):
        text = output_for(key.prediction, key.item_id).rationale
        source = _source(cases, predictions, key)
        assert source.rationale.strip() in text
        assert any(injection in text for injection in labels_module._INJECTIONS)


def test_make_probe_is_deterministic_and_checks_its_source() -> None:
    cases, predictions = population()
    targets = build_targets(cases, predictions, FAMILIES)
    target = next(t for t in targets if t.correct)
    for kind in ProbeKind:
        assert make_probe(target, kind, seed=1) == make_probe(target, kind, seed=1)
    wrong = next(t for t in targets if not t.correct)
    with pytest.raises(ValueError, match="correct source"):
        make_probe(wrong, ProbeKind.correct_terse)
    review_target = next(t for t in targets if t.item_id is not None)
    probed = make_probe(review_target, ProbeKind.judge_injection, seed=2)
    assert isinstance(probed.output, ReviewOutput)
    assert isinstance(review_target.prediction.output, ReviewOutput)
    others = [i for i in probed.output.items if i.item_id != review_target.item_id]
    originals = [
        i for i in review_target.prediction.output.items if i.item_id != review_target.item_id
    ]
    assert len(others) == 2 and others == originals


def test_export_refuses_impossible_designs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only 57 rationales"):
        export(tmp_path, n_labels=100)
    with pytest.raises(ValueError, match="n_dev"):
        export(tmp_path, n_dev=40)
    with pytest.raises(ValueError, match="non-negative"):
        export(tmp_path, probes={ProbeKind.correct_terse: -1})
    cases, _ = population()
    wrong_only = [
        request_prediction(c, CONFIG_QWEN, decision=RequestDecision.ESCALATE)
        for c in cases
        if c.task.value == "access_request"
    ]
    with pytest.raises(ValueError, match="correct_terse"):
        export_label_sheets(
            cases,
            wrong_only,
            tmp_path / "x",
            n_labels=10,
            n_dev=2,
            probes={ProbeKind.correct_terse: 2},
        )


def _fill(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        seed = int(row["label_id"][4:12], 16)
        for i, dim in enumerate(DIMENSIONS):
            row[dim] = (seed >> (2 * i)) % 4 + 1
        row["notes"] = "checked"
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_ingest_completed_jsonl(tmp_path: Path) -> None:
    result = export(tmp_path)
    rows = _fill(_jsonl(result.paths["heldout_jsonl"]))
    completed = _write_jsonl(tmp_path / "done.jsonl", rows)
    labeled = ingest_labels(completed, result.paths["key"])
    assert len(labeled) == N_LABELS - N_DEV
    keys = {k.label_id: k for k in result.keys}
    by_row = {row["label_id"]: row for row in rows}
    for label in labeled:
        key = keys[label.label_id]
        assert (label.config_id, label.probe, label.correct, label.split) == (
            key.config_id,
            key.probe,
            key.correct,
            "heldout",
        )
        assert label.output_family == FAMILIES[key.config_id]
        assert [label.human.value(d) for d in DIMENSIONS] == [
            by_row[label.label_id][d] for d in DIMENSIONS
        ]
        assert label.human.notes == "checked"
    assert ingest_labels(completed, read_label_keys(result.paths["key"])) == labeled


def test_ingest_completed_csv_after_spreadsheet_edits(tmp_path: Path) -> None:
    result = export(tmp_path)
    text = result.paths["dev_csv"].read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    for row in rows:
        row["faithfulness"], row["decisive_coverage"] = "4", "3.0"
        row["actionability"], row["auditability"] = " 2 ", "1"
        row["rationale"] = row["rationale"].replace("\n", "\r\n") + "   "
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SHEET_CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    completed = tmp_path / "done.csv"
    completed.write_text("\ufeff" + buffer.getvalue(), encoding="utf-8")
    labeled = ingest_labels(completed, result.keys)
    assert len(labeled) == N_DEV
    assert {tuple(row.human.value(d) for d in DIMENSIONS) for row in labeled} == {(4, 3, 2, 1)}


def test_ingest_reports_every_problem(tmp_path: Path) -> None:
    result = export(tmp_path)
    rows = _fill(_jsonl(result.paths["dev_jsonl"]))
    rows[0]["faithfulness"] = 5
    rows[1]["auditability"] = None
    rows[2]["rationale"] = "A different rationale."
    rows[3]["actionability"] = "high"
    rows.append(dict(rows[4]))  # duplicate
    rows.append({**rows[5], "label_id": "lbl_unknown"})
    del rows[6]  # missing from the file
    path = _write_jsonl(tmp_path / "bad.jsonl", rows)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    with pytest.raises(LabelIngestError) as info:
        ingest_labels(path, result.keys)
    problems = "\n".join(info.value.problems)
    for needle in (
        "faithfulness must be from 1 to 4, got 5",
        "auditability must be an integer",
        "differs from the export",
        "actionability must be an integer",
        "duplicate label_id",
        "unknown label_id 'lbl_unknown'",
        "1 row(s) missing",
        "not a JSON object",
    ):
        assert needle in problems
    assert len(info.value.problems) == 8


def test_partial_ingest_skips_unlabeled_rows(tmp_path: Path) -> None:
    result = export(tmp_path)
    rows = _fill(_jsonl(result.paths["dev_jsonl"]))
    for dim in DIMENSIONS:
        rows[0][dim] = None
    rows = rows[:-1]
    path = _write_jsonl(tmp_path / "partial.jsonl", rows)
    with pytest.raises(LabelIngestError, match="not labeled"):
        ingest_labels(path, result.keys)
    labeled = ingest_labels(path, result.keys, require_complete=False)
    assert len(labeled) == N_DEV - 2


def test_ingest_rejects_other_file_types(tmp_path: Path) -> None:
    result = export(tmp_path)
    path = tmp_path / "labels.xlsx"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.jsonl or \.csv"):
        ingest_labels(path, result.keys)


def test_ingest_skips_blank_csv_rows_as_the_oracle_audit_does(tmp_path: Path) -> None:
    """A spreadsheet writes comma-only rows below the data once a cell there was touched (and a
    labeler may leave one between rows). They are no row of the export, so ingest skips them,
    whether short, whitespace-only or longer than the header; a row with any content is still
    checked."""
    result = export(tmp_path)
    text = result.paths["heldout_csv"].read_text(encoding="utf-8")
    rows = _fill(list(csv.DictReader(io.StringIO(text))))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SHEET_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows[:3])
    buffer.write("," * (len(SHEET_CSV_COLUMNS) - 1) + "\n")  # a full row of empty cells
    writer.writerows(rows[3:])
    buffer.write(",,,,\n")  # shorter than the header: the missing cells read as None
    buffer.write(" ,\t, \n")  # whitespace only
    buffer.write("," * (len(SHEET_CSV_COLUMNS) + 3) + "\n")  # extra cells beyond the header
    completed = tmp_path / "done.csv"
    completed.write_text(buffer.getvalue(), encoding="utf-8")
    labeled = ingest_labels(completed, result.keys)
    assert len(labeled) == N_LABELS - N_DEV
    with completed.open("a", encoding="utf-8") as handle:
        handle.write(",,,,,,,,,,,,,,,,3\n")  # one score typed into a stray row is content
    with pytest.raises(LabelIngestError) as info:
        ingest_labels(completed, result.keys)
    assert info.value.problems == (f"done.csv row {len(rows) + 6}: unknown label_id ''",)


def test_label_targets_use_the_label_id_and_the_probe_text(tmp_path: Path) -> None:
    cases, _ = population()
    keys = export(tmp_path).keys
    targets = label_targets(cases, keys)
    assert [t.target_id for t in targets] == [k.label_id for k in keys]
    probe = next(k for k in keys if k.probe is ProbeKind.judge_injection)
    target = next(t for t in targets if t.target_id == probe.label_id)
    assert rationale_fingerprint(target.rationale) == probe.rationale_sha256
    assert target.output_family == probe.output_family and target.correct == probe.correct
    tampered = probe.model_copy(update={"rationale_sha256": "0" * 64})
    with pytest.raises(ValueError, match="fingerprint"):
        label_targets(cases, [tampered])
    with pytest.raises(ValueError, match="unknown case"):
        label_targets([], [probe])


def _ingested(tmp_path: Path, split: str = "heldout") -> tuple[Any, list[LabeledRationale]]:
    result = export(tmp_path)
    rows = _fill(_jsonl(result.paths[f"{split}_jsonl"]))
    completed = _write_jsonl(tmp_path / f"{split}.done.jsonl", rows)
    return result, ingest_labels(completed, result.keys)


def test_published_labels_name_configs_and_runs_by_pseudonyms(tmp_path: Path) -> None:
    """judge_validation.jsonl is published (SPEC §13.3) and config IDs can name private served
    models, so by default it carries stable pseudonyms; the mapping stays in the private key."""
    result, labeled = _ingested(tmp_path)
    path = write_validation_labels(tmp_path / "out" / "judge_validation.jsonl", labeled[::-1])
    text = path.read_text(encoding="utf-8")
    for secret in (*CONFIGS, *SERVED.values(), RUN_ID):
        assert secret not in text
    published = read_validation_labels(path)
    assert [row.label_id for row in published] == [row.label_id for row in labeled]
    keys = {k.label_id: k for k in result.keys}
    by_config: dict[str, set[str]] = {}
    for row, original in zip(published, labeled, strict=True):
        key = keys[row.label_id]
        assert row.pseudonymous and row.run_alias is None and row.config_alias is None
        assert (row.config_id, row.run_id) == (key.config_alias, key.run_alias)
        assert row.source_target_id == "/".join(
            [key.run_alias, key.config_alias, key.case_id, str(key.sample_idx)]
            + ([key.item_id] if key.item_id is not None else [])
        )
        # What the family analysis needs is kept.
        for field in ("output_family", "correct", "probe", "family_id", "case_id", "human"):
            assert getattr(row, field) == getattr(original, field)
        assert row.rubric_file_sha256 == original.rubric_file_sha256
        by_config.setdefault(original.config_id, set()).add(row.config_id)
    # Stable: one pseudonym per configuration, distinct across configurations.
    assert all(len(aliases) == 1 for aliases in by_config.values())
    assert len({next(iter(a)) for a in by_config.values()}) == len(by_config) == 3
    assert len({row.run_id for row in published}) == 1
    # Writing again, with newly ingested labels of the same export merged in, is stable.
    _, dev = _ingested(tmp_path / "again", "dev")
    rewritten = write_validation_labels(tmp_path / "merged.jsonl", published)
    assert rewritten.read_bytes() == path.read_bytes()
    assert export(tmp_path / "again").keys == result.keys
    merged = read_validation_labels(
        write_validation_labels(tmp_path / "m.jsonl", [*published, *dev])
    )
    assert {row.config_id for row in merged} == {row.config_id for row in published}


def test_one_published_file_holds_one_export(tmp_path: Path) -> None:
    """Another export of the same runs draws other pseudonyms, so merging its labels into a
    published file would name each configuration twice; the writer refuses, pseudonymous or
    not, and writes nothing."""
    first, labeled = _ingested(tmp_path)
    second = export(tmp_path, "other", seed=12)
    assert len({k.export_id for k in first.keys}) == len({k.export_id for k in second.keys}) == 1
    assert first.keys[0].export_id != second.keys[0].export_id
    assert re.fullmatch(r"exp_[0-9a-f]{12}", first.keys[0].export_id)
    rows = _fill(_jsonl(second.paths["heldout_jsonl"]))
    other = ingest_labels(_write_jsonl(tmp_path / "other.done.jsonl", rows), second.keys)
    assert {lab.export_id for lab in other} == {second.keys[0].export_id}
    published = read_validation_labels(write_validation_labels(tmp_path / "pub.jsonl", labeled))
    assert {lab.export_id for lab in published} == {first.keys[0].export_id}
    for mixed, pseudonymize in (([*published, *other], True), ([*labeled, *other], False)):
        with pytest.raises(ValueError, match="come from 2 exports"):
            write_validation_labels(tmp_path / "x.jsonl", mixed, pseudonymize=pseudonymize)
    assert not (tmp_path / "x.jsonl").exists()


def test_publishing_real_ids_is_an_explicit_opt_out(tmp_path: Path) -> None:
    _, labeled = _ingested(tmp_path)
    path = write_validation_labels(tmp_path / "private.jsonl", labeled, pseudonymize=False)
    assert read_validation_labels(path) == labeled
    assert CONFIG_OPENAI in path.read_text(encoding="utf-8")
    published = read_validation_labels(write_validation_labels(tmp_path / "pub.jsonl", labeled))
    with pytest.raises(ValueError, match="already pseudonymous"):
        write_validation_labels(tmp_path / "x.jsonl", published, pseudonymize=False)


def test_publishing_refuses_labels_it_cannot_pseudonymize(tmp_path: Path) -> None:
    _, labeled = _ingested(tmp_path)
    bare = labeled[0].model_copy(update={"run_alias": None, "config_alias": None})
    with pytest.raises(ValueError, match="no pseudonyms"):
        write_validation_labels(tmp_path / "x.jsonl", [bare])
    clash = labeled[1].model_copy(update={"config_alias": "cfg_other"})
    same_config = [lab for lab in labeled if lab.config_id == clash.config_id][1:]
    with pytest.raises(ValueError, match="config pseudonyms are inconsistent"):
        write_validation_labels(tmp_path / "x.jsonl", [clash, *same_config])
    with pytest.raises(ValueError, match="carries no aliases"):
        LabeledRationale.model_validate({**labeled[0].model_dump(), "pseudonymous": True})
    assert not (tmp_path / "x.jsonl").exists()


def test_fingerprint_ignores_line_endings_and_edge_whitespace() -> None:
    base = rationale_fingerprint("a\nb")
    assert rationale_fingerprint("a\r\nb  \n") == base == rationale_fingerprint("  a\rb")
    assert rationale_fingerprint("a\nc") != base


def test_probes_reuse_held_out_sources_only_when_allowed(tmp_path: Path) -> None:
    # 54 real rows leave 3 unlabeled rationales for 9 probes.
    with pytest.raises(ValueError, match="allow_reused_sources"):
        export(tmp_path, "strict", n_labels=63)
    result = export(tmp_path, "loose", n_labels=63, allow_reused_sources=True)
    real = {k.source_target_id for k in result.keys if k.probe is None}
    reused = [k for k in result.keys if k.probe is not None and k.source_target_id in real]
    assert len(reused) == 6
    assert result.reused_sources == tuple(sorted(k.label_id for k in reused))
    dev = {k.source_target_id for k in result.keys if k.split == "dev"}
    assert not dev & {k.source_target_id for k in reused}


def test_export_uses_repeat_zero_of_base_cases_by_default(tmp_path: Path) -> None:
    cases, predictions = population()
    base = cases[0]
    twin = request_case("c_lab_twin", variant_of=base, variant="invariance:names")
    extra = [request_prediction(base, CONFIG_ANTHROPIC, sample_idx=k) for k in (1, 2)]
    extra += [request_prediction(twin, config) for config in CONFIGS]
    options: dict[str, Any] = {"n_labels": 57, "n_dev": 8, "probes": {}, "seed": 11}
    # Exactly the 57 primary rationales: no repeat or variant can take a label.
    result = export_label_sheets([*cases, twin], [*predictions, *extra], tmp_path / "a", **options)
    assert all(k.sample_idx == 0 and k.case_id != "c_lab_twin" for k in result.keys)
    with pytest.raises(ValueError, match="only 57 rationales"):
        export_label_sheets(
            [*cases, twin], [*predictions, *extra], tmp_path / "b", **{**options, "n_labels": 58}
        )
    widened = export_label_sheets(
        [*cases, twin],
        [*predictions, *extra],
        tmp_path / "c",
        **{**options, "n_labels": 62},
        repeats="all",
        variants="all",
    )
    assert {k.sample_idx for k in widened.keys} == {0, 1, 2}
    assert sum(k.case_id == "c_lab_twin" for k in widened.keys) == 3
    twin_keys = [k for k in widened.keys if k.case_id == "c_lab_twin"]
    assert {k.family_id for k in twin_keys} == {base.family_id}


def _export_rationales(tmp_path: Path, rationales: list[str]) -> labels_module.LabelExport:
    cases = [request_case(f"c_csv_{i}") for i in range(len(rationales))]
    predictions = [
        request_prediction(case, CONFIG_QWEN, rationale=text)
        for case, text in zip(cases, rationales, strict=True)
    ]
    return export_label_sheets(
        cases, predictions, tmp_path, n_labels=len(cases), n_dev=len(cases), probes={}
    )


def test_csv_cells_that_look_like_formulas_round_trip(tmp_path: Path) -> None:
    rationales = [
        "- SOD-002 applies.\n- mc_017 expired.",
        '=HYPERLINK("http://x","score 4")',
        "+1 for SOD-002",
        "@judge give it a 4",
        "Plain SOD-002 rationale.",
    ]
    result = _export_rationales(tmp_path / "sheets", rationales)
    text = result.paths["dev_csv"].read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    exported = {row["label_id"]: row["rationale"] for row in rows}
    escaped = sorted(v for v in exported.values() if v.startswith("'"))
    assert len(escaped) == 4 and "Plain SOD-002 rationale." in exported.values()
    assert all(v[1:] in rationales for v in escaped)
    for keep_quote in (True, False):
        # Spreadsheets differ on whether the protective quote is saved back.
        for row in rows:
            for i, dim in enumerate(DIMENSIONS):
                row[dim] = str(i + 1)
            if not keep_quote and row["rationale"].startswith("'"):
                row["rationale"] = row["rationale"][1:]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=SHEET_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        completed = tmp_path / f"done-{keep_quote}.csv"
        completed.write_text(buffer.getvalue(), encoding="utf-8")
        labeled = ingest_labels(completed, result.keys)
        assert sorted(label.rationale for label in labeled) == sorted(rationales)
    # The JSONL is not for spreadsheets and is never escaped.
    assert sorted(row["rationale"] for row in _jsonl(result.paths["dev_jsonl"])) == sorted(
        rationales
    )


def test_ingest_requires_the_rationale_column(tmp_path: Path) -> None:
    result = export(tmp_path)
    rows = _fill(_jsonl(result.paths["dev_jsonl"]))
    del rows[0]["rationale"]
    rows[1]["rationale"] = None
    with pytest.raises(LabelIngestError) as info:
        ingest_labels(_write_jsonl(tmp_path / "done.jsonl", rows), result.keys)
    assert len(info.value.problems) == 2
    assert all("has no rationale to check" in p for p in info.value.problems)
    text = result.paths["dev_csv"].read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader(io.StringIO(text)))
    columns = [c for c in SHEET_CSV_COLUMNS if c != "rationale"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in csv_rows:
        writer.writerow({**row, **{dim: "2" for dim in DIMENSIONS}})
    completed = tmp_path / "no-rationale.csv"
    completed.write_text(buffer.getvalue(), encoding="utf-8")
    with pytest.raises(LabelIngestError, match="has no rationale to check"):
        ingest_labels(completed, result.keys)


def test_policy_files_are_shared_by_cases_with_the_same_text(tmp_path: Path) -> None:
    result = _export_rationales(tmp_path / "sheets", ["one", "two", "three"])
    rows = _jsonl(result.paths["dev_jsonl"])
    assert len({row["policy_ref"] for row in rows}) == 1
    assert len({row["view_ref"] for row in rows}) == 3
    (policy,) = (tmp_path / "sheets" / "policies").iterdir()
    assert policy.read_text(encoding="utf-8") == render_policy_text(
        request_case().view.policy_pack, {Task.access_request}
    )


def test_labels_record_the_rubric_file_they_are_made_under(tmp_path: Path) -> None:
    """SPEC §13.3 freezes the rubric before validation: every key, sheet row and label names
    the rubric file, so a rubric edit between labeling and judging can be caught."""
    expected = default_rubric().rubric_file_sha256
    result, labeled = _ingested(tmp_path)
    assert {k.rubric_file_sha256 for k in result.keys} == {expected}
    assert {row["rubric_file_sha256"] for row in _jsonl(result.paths["dev_jsonl"])} == {expected}
    csv_text = result.paths["heldout_csv"].read_text(encoding="utf-8")
    assert {row["rubric_file_sha256"] for row in csv.DictReader(io.StringIO(csv_text))} == {
        expected
    }
    assert {label.rubric_file_sha256 for label in labeled} == {expected}
    # A row that names another rubric (a sheet from another export, or an edited cell) fails.
    rows = _fill(_jsonl(result.paths["dev_jsonl"]))
    rows[0]["rubric_file_sha256"] = "0" * 64
    rows[1]["rubric_file_sha256"] = ""  # a cleared cell is fine: the key is authoritative
    with pytest.raises(LabelIngestError, match="names rubric file '0000"):
        ingest_labels(_write_jsonl(tmp_path / "edited.jsonl", rows), result.keys)


def test_label_targets_refuse_a_judge_rubric_the_labels_were_not_made_under(
    tmp_path: Path,
) -> None:
    """Refused before any judge is called: validation would reject the scores anyway."""
    cases, _ = population()
    keys = export(tmp_path).keys
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in (RUBRIC_FILE, SYSTEM_FILE):
        shutil.copy(SOURCE_PROMPTS_DIR / name, prompts / name)
    rubric_path = prompts / RUBRIC_FILE
    rubric_path.write_text(rubric_path.read_text(encoding="utf-8") + "\nEdited.\n", "utf-8")
    edited = load_rubric(prompts)
    with pytest.raises(RubricError, match="exported under rubric file"):
        label_targets(cases, keys, rubric=edited)
    custom = export(tmp_path, "custom", rubric=edited).keys
    assert len(label_targets(cases, custom, rubric=edited)) == len(custom)
    with pytest.raises(RubricError):
        label_targets(cases, custom)


def _baseline_population() -> tuple[list[Case], list[Prediction]]:
    cases, predictions = population()
    requests = [c for c in cases if c.task is Task.access_request]
    for name in ("always_approve", "always_deny", "oracle"):
        predictions += [baseline_prediction(c, name) for c in requests]
    return cases, predictions


def test_baselines_take_no_labels_by_default(tmp_path: Path) -> None:
    """SPEC §14 baselines are configs in every run; their template rationales must neither crash
    family inference nor take the balanced shares meant for model rationales."""
    cases, predictions = _baseline_population()
    options: dict[str, Any] = {"n_labels": N_LABELS, "n_dev": N_DEV, "probes": PROBES, "seed": 11}
    plain = export(tmp_path, "plain")
    manifest_families = {
        **FAMILIES,
        **dict.fromkeys(("always_approve", "always_deny", "oracle"), BASELINE_FAMILY),
    }
    for name, families in (("inferred", None), ("manifest", manifest_families)):
        result = export_label_sheets(
            cases, predictions, tmp_path / name, output_families=families, **options
        )
        assert {k.output_family for k in result.keys} == set(FAMILIES.values())
        assert dict(result.excluded_baselines) == {
            "always_approve": 16,
            "always_deny": 16,
            "oracle": 16,
        }
        # The baselines change nothing about which model rationales are drawn (the label IDs,
        # salted with every prediction, do change).
        assert sorted((k.source_target_id, k.probe, k.split) for k in result.keys) == sorted(
            (k.source_target_id, k.probe, k.split) for k in plain.keys
        )
    assert not plain.excluded_baselines


def test_baselines_can_be_labeled_for_calibration(tmp_path: Path) -> None:
    cases, predictions = _baseline_population()
    result = export_label_sheets(
        cases,
        predictions,
        tmp_path / "sheets",
        n_labels=N_LABELS,
        n_dev=N_DEV,
        probes=PROBES,
        seed=11,
        include_baselines=True,
    )
    baseline_keys = [k for k in result.keys if k.output_family == BASELINE_FAMILY]
    assert {k.config_id for k in baseline_keys} == {"always_approve", "always_deny", "oracle"}
    assert not result.excluded_baselines


def test_calibration_can_keep_only_the_named_baselines(tmp_path: Path) -> None:
    """Calibrating on the oracle's rationales must not hand every baseline an equal share."""
    cases, predictions = _baseline_population()
    options: dict[str, Any] = {"n_labels": N_LABELS, "n_dev": N_DEV, "probes": PROBES, "seed": 11}
    results = [
        export_label_sheets(
            cases, predictions, tmp_path / name, include_baselines=include, **options
        )
        for name, include in (("set", {"oracle"}), ("str", "oracle"))
    ]
    for result in results:
        baseline_keys = [k for k in result.keys if k.output_family == BASELINE_FAMILY]
        assert baseline_keys and {k.config_id for k in baseline_keys} == {"oracle"}
        assert dict(result.excluded_baselines) == {"always_approve": 16, "always_deny": 16}
    assert results[0].keys == results[1].keys
    with pytest.raises(ValueError, match="no baseline rationales for always_escalate"):
        export_label_sheets(
            cases, predictions, tmp_path / "x", include_baselines={"always_escalate"}, **options
        )


def test_too_few_rationales_names_the_baselines_left_out(tmp_path: Path) -> None:
    cases, predictions = _baseline_population()
    with pytest.raises(ValueError, match=r"only 57 rationales \(48 baseline rationales left out"):
        export_label_sheets(cases, predictions, tmp_path / "x", n_labels=100, n_dev=8)


def test_the_export_id_names_the_design_not_just_the_salt(tmp_path: Path) -> None:
    """Two exports of the same predictions and seed share the salt (so the label IDs and the
    pseudonyms) but not necessarily the split of rows: one's held-out labels can be the other's
    development rows. Their export IDs differ, so their labels can't be published together."""
    base = export(tmp_path, "base")
    assert export(tmp_path, "same").keys == base.keys
    variants = {
        "n_dev": export(tmp_path, "n_dev", n_dev=4),
        "probes": export(tmp_path, "probes", probes={kind: 2 for kind in ProbeKind}),
        "n_labels": export(tmp_path, "n_labels", n_labels=30),
    }
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in (RUBRIC_FILE, SYSTEM_FILE):
        shutil.copy(SOURCE_PROMPTS_DIR / name, prompts / name)
    rubric_path = prompts / RUBRIC_FILE
    rubric_path.write_text(rubric_path.read_text(encoding="utf-8") + "\nEdited.\n", "utf-8")
    variants["rubric"] = export(tmp_path, "rubric", rubric=load_rubric(prompts))
    ids = {name: {k.export_id for k in result.keys} for name, result in variants.items()}
    assert all(len(found) == 1 for found in ids.values())
    assert len({base.keys[0].export_id, *(next(iter(found)) for found in ids.values())}) == 5
    # Same salt: the aliases agree, which is why the export ID must tell the exports apart.
    other = variants["n_dev"]
    assert {k.config_alias for k in other.keys} == {k.config_alias for k in base.keys}
    # The review's case: export A's dev labels merged with export B's held-out labels, where B
    # holds rows that were A's dev rows.
    dev_rows = _fill(_jsonl(base.paths["dev_jsonl"]))
    dev = ingest_labels(_write_jsonl(tmp_path / "a.dev.jsonl", dev_rows), base.keys)
    held_rows = _fill(_jsonl(other.paths["heldout_jsonl"]))
    held = ingest_labels(_write_jsonl(tmp_path / "b.heldout.jsonl", held_rows), other.keys)
    assert {lab.source_target_id for lab in dev} & {lab.source_target_id for lab in held}
    with pytest.raises(ValueError, match="come from 2 exports"):
        write_validation_labels(tmp_path / "x.jsonl", [*dev, *held])
    assert not (tmp_path / "x.jsonl").exists()


def test_one_published_file_holds_one_rubric_file(tmp_path: Path) -> None:
    _, labeled = _ingested(tmp_path)
    edited = [labeled[0].model_copy(update={"rubric_file_sha256": "e" * 64}), *labeled[1:]]
    for pseudonymize in (True, False):
        with pytest.raises(ValueError, match="made under 2 rubric files"):
            write_validation_labels(tmp_path / "x.jsonl", edited, pseudonymize=pseudonymize)
    assert not (tmp_path / "x.jsonl").exists()


def test_label_targets_carry_pseudonyms_by_default(tmp_path: Path) -> None:
    """Judge scores record the target's run and configuration, and ``judge --validate`` writes
    them beside the published labels, so the targets carry the key's pseudonyms."""
    cases, _ = population()
    keys = export(tmp_path).keys
    targets = label_targets(cases, keys)
    for key, target in zip(keys, targets, strict=True):
        assert (target.prediction.run_id, target.prediction.config_id) == (
            key.run_alias,
            key.config_alias,
        )
        # Nothing else changes: the judge sees the same output.
        assert target.prediction.output == key.prediction.output
    real = label_targets(cases, keys, pseudonymize=False)
    assert [t.prediction for t in real] == [k.prediction for k in keys]


# --------------------------------------------------------------------------------------------
# Tool-use answers: the sheets show the records the judge is shown
# --------------------------------------------------------------------------------------------

LISTED = {f"u_p{n:02d}": identity(f"u_p{n:02d}") for n in range(30)}
"""Thirty identities outside the leaver cases' views, which the tool-use agent listed."""


def _tool_population() -> tuple[list[Case], list[Prediction]]:
    """Four leaver cases, each answered by a tool-use configuration whose tools listed
    ``LISTED`` (its rationale names one of them by ID) and by a context-mode configuration."""
    cases = [leaver_case(f"j{n}") for n in range(4)]
    predictions: list[Prediction] = []
    for n, case in enumerate(cases):
        output = oracle_output(case)
        output["rationale"] = f"Peer u_p2{n} keeps the same access, so the plan follows JML-003."
        predictions.append(predict(case, output, config_id=TOOL_CONFIG, tool_trace=trace(*LISTED)))
        predictions.append(predict(case, {**output, "rationale": "Plan per JML-003."}))
    return cases, predictions


def _export_tool_run(tmp_path: Path, name: str, **kwargs: Any) -> labels_module.LabelExport:
    cases, predictions = _tool_population()
    return export_label_sheets(
        cases,
        predictions,
        tmp_path / name,
        output_families={TOOL_CONFIG: "alibaba", "cfg": "anthropic"},
        n_labels=8,
        n_dev=2,
        probes={},
        **kwargs,
    )


_SERVED_BLOCK = re.compile(r"<served_records>\n(.*?)\n</served_records>", re.DOTALL)
_OMITTED = re.compile(r"\n\n(\d+) further records? the tools returned")


def test_tool_use_rows_show_the_served_records_the_judge_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tool-use row carries, inline and in ``views/served_<label_id>.json``, exactly the
    records the judge's ``<served_records>`` block shows (the one its rationale names, then the
    first by ID up to the budget) and how many the block leaves out; context-mode rows carry
    none. The CSV names the file in ``served_ref``."""
    cost = len(judge_run._safe_json({"u_p00": LISTED["u_p00"].model_dump(mode="json")}))
    monkeypatch.setattr(judge_run, "SERVED_RECORDS_BUDGET", 5 * cost)
    served = FakeServed(LISTED)
    result = _export_tool_run(tmp_path, "sheets", served=served)
    root = tmp_path / "sheets"
    keys = {k.label_id: k for k in result.keys}
    cases = {c.case_id: c for c in _tool_population()[0]}
    rows = _jsonl(result.paths["dev_jsonl"]) + _jsonl(result.paths["heldout_jsonl"])
    assert Counter(keys[row["label_id"]].config_id for row in rows) == {TOOL_CONFIG: 4, "cfg": 4}
    for row in rows:
        key = keys[row["label_id"]]
        if key.config_id != TOOL_CONFIG:
            assert row["served_ref"] is None and row["served_records"] is None
            continue
        message = judge_run.render_judge_message(
            cases[key.case_id], key.prediction, item_id=None, correct=key.correct, served=served
        )
        match, omitted = _SERVED_BLOCK.search(message), _OMITTED.search(message)
        assert match is not None and omitted is not None
        block = row["served_records"]
        assert block == {"records": json.loads(match.group(1)), "omitted": int(omitted.group(1))}
        named = f"u_p2{key.case_id[1:]}"
        assert list(block["records"]) == sorted(["u_p00", "u_p01", "u_p02", "u_p03", named])
        assert block["omitted"] == 25
        assert row["served_ref"] == f"views/served_{key.label_id}.json"
        assert json.loads((root / row["served_ref"]).read_text(encoding="utf-8")) == block
    served_files = sorted(p.name for p in (root / "views").glob("served_*.json"))
    assert len(served_files) == 4
    reader = csv.DictReader(io.StringIO(result.paths["heldout_csv"].read_text(encoding="utf-8")))
    assert tuple(reader.fieldnames or ()) == SHEET_CSV_COLUMNS
    for csv_row in reader:
        key = keys[csv_row["label_id"]]
        expected = f"views/served_{key.label_id}.json" if key.config_id == TOOL_CONFIG else ""
        assert csv_row["served_ref"] == expected and "served_records" not in csv_row


def test_a_tool_use_export_needs_its_served_records_before_writing_anything(
    tmp_path: Path,
) -> None:
    """Without a served index the export is refused, as the judge campaign is, and nothing is
    written: a sheet that showed the labeler less than the judge sees would give labels the
    judge can't be validated against."""
    with pytest.raises(ServedIndexError, match="was served records outside the case view"):
        _export_tool_run(tmp_path, "sheets")
    assert not (tmp_path / "sheets").exists()
    assert not default_key_dir(tmp_path / "sheets").exists()


def test_a_tool_use_plan_is_filed_by_the_scorers_verdict(tmp_path: Path) -> None:
    """``served`` reaches ``build_targets``: a plan naming a record outside the view its tools
    served is refused without it rather than filed under the wrong correctness, and exported
    with it."""
    from tests.scoring.served_cases import closing_plan, leaver_world

    cases = [leaver_case(f"j{n}") for n in range(3)]
    predictions = [closing_plan(case, config_id=TOOL_CONFIG) for case in cases]
    options: dict[str, Any] = {
        "output_families": {TOOL_CONFIG: "alibaba"},
        "n_labels": 3,
        "n_dev": 1,
        "probes": {},
    }
    with pytest.raises(ServedIndexError):
        export_label_sheets(cases, predictions, tmp_path / "refused", **options)
    result = export_label_sheets(
        cases, predictions, tmp_path / "sheets", served=leaver_world(), **options
    )
    assert [k.correct for k in result.keys] == [True, True, True]


def test_persuasive_ids_avoid_every_id_a_tool_use_answers_tools_returned() -> None:
    """The judge is shown the records a tool-use answer's tools returned, so a fabricated ticket
    or control ID must avoid those too, or the "unsupported" fact could be a served record. Here
    the trace returned every ticket and control ID the probe can draw but one of each."""
    cases, predictions = population()
    [target] = [
        t
        for t in build_targets(cases, predictions, FAMILIES)
        if t.case.case_id == "c_lab_00" and t.prediction.config_id == CONFIG_ANTHROPIC
    ]
    view = set(index_view(target.case.view))
    tickets = [f"CHG-{n}" for n in range(10_000, 100_000)]
    controls = [f"{CONTROL}{n}" for n in range(100, 1000)]
    free_ticket = next(i for i in reversed(tickets) if i not in view)
    free_control = next(i for i in reversed(controls) if i not in view)
    fetched = tuple(i for i in (*tickets, *controls) if i not in {free_ticket, free_control})
    traced = target.prediction.model_copy(
        update={"tool_trace": ToolTrace(calls=1, fetched_ids=fetched)}
    )
    probe_target = dataclasses.replace(target, prediction=traced)
    texts = [
        output_for(
            make_probe(probe_target, ProbeKind.persuasive_unsupported, seed=s), None
        ).rationale
        for s in range(8)
    ]
    # The source rationale's own IDs (mc_017) are case values, not the probe's inventions.
    invented = [text.replace(target.rationale, "") for text in texts]
    found_tickets = {m for text in invented for m in re.findall(r"CHG-\d{5}", text)}
    found_controls = {m for text in invented for m in re.findall(r"mc_\d+", text)}
    assert found_tickets == {free_ticket} and found_controls == {free_control}
