"""The blind oracle audit: selection, export determinism and blindness, ingest and the
disagreement sheet (SPEC §9.3 item 5)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from identitybench.audit import (
    ANSWER_FIELDS,
    AUDIT_FORMAT,
    DISAGREEMENT_CSV_COLUMNS,
    NO_RULES,
    SHEET_CSV_COLUMNS,
    AuditIngestError,
    Classification,
    DisagreementKind,
    Drift,
    RowStatus,
    export_audit_sheets,
    ingest_audit,
    read_audit_keys,
    read_audit_manifest,
    remove_artifacts,
    select_audit_cases,
    staged_export,
    staged_exports,
    write_disagreements,
)
from identitybench.render.prompt import build_request
from identitybench.schemas.case import Case
from identitybench.schemas.enums import RequestDecision, ReviewDecision, Task, Tier
from identitybench.schemas.gold import JmlGold, RequestGold, ReviewGold
from tests.cli.cases import oracle_cases
from tests.cli.sheets import by_id, filled_csv, gold_answer, keys_by_id, read_csv, write_csv

SHA = "f" * 64
N = 24


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("audit") / "oracle"
    export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, dataset_path="d", n=N, seed=3)
    return out


# --------------------------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------------------------


def test_selection_is_balanced_over_task_and_tier() -> None:
    chosen = select_audit_cases(oracle_cases(), 24, seed=0)
    cells = Counter((c.task, c.tier) for c in chosen)
    available = Counter((c.task, c.tier) for c in oracle_cases())
    assert len(chosen) == 24
    # Every cell is represented; the two-case JML L1 cell gives its spare picks to the others.
    assert set(cells) == set(available)
    assert cells[(Task.jml, Tier.L1)] == 2
    assert max(cells.values()) - min(n for cell, n in cells.items() if available[cell] > 3) <= 1


def test_selection_is_deterministic_and_order_free() -> None:
    shuffled = list(oracle_cases())
    random.Random(7).shuffle(shuffled)
    first = {c.case_id for c in select_audit_cases(oracle_cases(), 30, seed=1)}
    assert first == {c.case_id for c in select_audit_cases(shuffled, 30, seed=1)}
    assert first != {c.case_id for c in select_audit_cases(oracle_cases(), 30, seed=2)}


def test_selection_takes_one_case_per_family_preferring_the_base() -> None:
    base = oracle_cases()[0]
    twin = base.model_copy(update={"case_id": "case_twin", "variant": "counterfactual"})
    twin = twin.model_copy(update={"twin_of": base.case_id})
    others = list(oracle_cases()[1:])
    chosen = select_audit_cases([twin, base, *others], len(others) + 1, seed=0)
    ids = {c.case_id for c in chosen}
    assert base.case_id in ids and "case_twin" not in ids
    alone = select_audit_cases([twin, *others], len(others) + 1, seed=0)
    assert "case_twin" in {c.case_id for c in alone}


def test_selection_rejects_impossible_sizes() -> None:
    with pytest.raises(ValueError, match="families are available"):
        select_audit_cases(oracle_cases(), len(oracle_cases()) + 1)
    with pytest.raises(ValueError, match="at least 1"):
        select_audit_cases(oracle_cases(), 0)


# --------------------------------------------------------------------------------------------
# Export: determinism and blindness
# --------------------------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_export_is_byte_identical_whatever_the_input_order(tmp_path: Path, exported: Path) -> None:
    shuffled = list(oracle_cases())
    random.Random(11).shuffle(shuffled)
    again = tmp_path / "again"
    export_audit_sheets(shuffled, again, dataset_sha256=SHA, dataset_path="d", n=N, seed=3)
    assert snapshot(again) == snapshot(exported)


def test_export_writes_the_documented_files(exported: Path) -> None:
    files = set(snapshot(exported))
    expected = {"sheet.jsonl", "sheet.csv", "key.jsonl", "audit.json", "INSTRUCTIONS.md"}
    assert expected <= files
    keys = read_audit_keys(exported / "key.jsonl")
    assert {f"messages/{k.audit_id}.md" for k in keys} <= files
    assert any(name.startswith("system/system_") for name in files)
    manifest = read_audit_manifest(exported / "audit.json")
    assert (manifest.n_cases, manifest.seed, manifest.dataset_sha256) == (N, 3, SHA)
    assert manifest.prompt_variant == "baseline"


def sheet_text(root: Path) -> str:
    """Everything a labeler is given (all but the key and the manifest)."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name not in ("key.jsonl", "audit.json")
    )


FORBIDDEN_FIELDS = frozenset(
    {"tier", "tags", "gold", "provenance", "family_id", "case_id", "world_id", "split"}
    | {"suite", "variant", "base_tier", "twin_of", "weight", "item_tier", "case_tier"}
)
"""Case metadata no sheet column or JSONL field may carry (SPEC §9.3: blind)."""


def test_sheets_carry_no_gold_or_case_metadata(exported: Path) -> None:
    text = sheet_text(exported)
    cases = by_id()
    for key in read_audit_keys(exported / "key.jsonl"):
        case = cases[key.case_id]
        for hidden in (case.case_id, case.family_id, case.world_id, *case.provenance.values()):
            assert hidden not in text
    jsonl = [json.loads(line) for line in (exported / "sheet.jsonl").read_text().splitlines()]
    for row in jsonl:
        fields = set(row) | {k for item in row.get("items", []) for k in item}
        assert not fields & FORBIDDEN_FIELDS
        assert fields <= {"audit_id", "task", "system_ref", "message_ref", "message", "items"} | {
            "item_id",
            *ANSWER_FIELDS,
        }
    # The literal layout, not the module's own constant: a column added there must fail here.
    header = tuple(read_csv(exported / "sheet.csv")[0])
    assert header == (
        "audit_id",
        "task",
        "item_id",
        "system_ref",
        "message_ref",
        "decision",
        "max_duration_days",
        "operations",
        "rules",
        "notes",
    )
    assert not set(header) & FORBIDDEN_FIELDS


def test_only_the_view_reaches_the_sheet(tmp_path: Path) -> None:
    """A positive control: a marker in the view reaches the sheet; markers in every field of the
    case around it (IDs, provenance, tags) don't."""
    base = next(c for c in oracle_cases() if c.task is Task.access_request)
    case = base.model_copy(
        update={
            "case_id": "case_WRAPPERMARK",
            "family_id": "fam_WRAPPERMARK",
            "world_id": "world_WRAPPERMARK",
            "tags": ("tag_WRAPPERMARK",),
            "provenance": {"scenario": "tmpl_WRAPPERMARK"},
            "view": base.view.model_copy(update={"org_name": "Viewmark Holdings"}),
        }
    )
    out = tmp_path / "marked"
    export_audit_sheets([case], out, dataset_sha256=SHA, n=1)
    text = sheet_text(out)
    assert "Viewmark Holdings" in text
    assert "WRAPPERMARK" not in text
    assert "case_WRAPPERMARK" in (out / "key.jsonl").read_text()  # the key alone maps it back


def test_answer_fields_start_blank(exported: Path) -> None:
    for row in read_csv(exported / "sheet.csv"):
        assert all(row[name] == "" for name in ANSWER_FIELDS)
    for line in (exported / "sheet.jsonl").read_text().splitlines():
        row = json.loads(line)
        for answers in row.get("items", [row]):
            assert all(answers.get(name) in (None, "") for name in ANSWER_FIELDS)


def test_rows_show_exactly_what_the_model_sees_in_opaque_order(exported: Path) -> None:
    keys = keys_by_id(exported)
    cases = by_id()
    rows = [json.loads(line) for line in (exported / "sheet.jsonl").read_text().splitlines()]
    assert [r["audit_id"] for r in rows] == sorted(keys)
    for row in rows:
        key = keys[row["audit_id"]]
        request = build_request(cases[key.case_id], "baseline")
        assert row["message"] == request.messages[-1].content
        assert (exported / row["message_ref"]).read_text(encoding="utf-8") == row["message"]
        assert (exported / row["system_ref"]).read_text(encoding="utf-8") == request.system
        assert row["task"] == key.task.value
        assert key.case_id not in row["audit_id"]
        if key.task is Task.access_review:
            assert [i["item_id"] for i in row["items"]] == list(key.item_ids)
            assert set(row["items"][0]) == {"item_id", "decision", "rules", "notes"}
        elif key.task is Task.jml:
            assert {"operations", "rules", "notes"} <= set(row) and "decision" not in row
        else:
            assert {"decision", "max_duration_days", "rules", "notes"} <= set(row)


def test_audit_ids_depend_on_seed_and_dataset(tmp_path: Path, exported: Path) -> None:
    other = tmp_path / "other"
    export_audit_sheets(oracle_cases(), other, dataset_sha256="0" * 64, n=N, seed=3)
    same_cases = {k.case_id for k in read_audit_keys(other / "key.jsonl")}
    assert same_cases == {k.case_id for k in read_audit_keys(exported / "key.jsonl")}
    assert set(keys_by_id(other)).isdisjoint(keys_by_id(exported))


# --------------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------------


def test_gold_answers_agree_everywhere(tmp_path: Path, exported: Path) -> None:
    sheet = tmp_path / "done.csv"
    rows = filled_csv(exported, sheet, rules=True)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    assert len(result.units) == len(rows)
    assert result.disagreements == ()
    assert result.n_unlabeled == 0
    summary = result.summary()
    assert summary["agree"] == len(rows) and summary["disagree"] == 0
    assert summary["labeled_cases"] == N
    assert sum(cell["labeled"] for cell in summary["by_task_tier"].values()) == len(rows)
    assert summary["classifications"] == [c.value for c in Classification]


def test_jsonl_sheet_with_nested_items_ingests_the_same(tmp_path: Path, exported: Path) -> None:
    keys = keys_by_id(exported)
    cases = by_id()
    lines = []
    for line in (exported / "sheet.jsonl").read_text().splitlines():
        row = json.loads(line)
        case = cases[keys[row["audit_id"]].case_id]
        if "items" in row:
            for item in row["items"]:
                item.update(gold_answer(case, item["item_id"]))
        else:
            answer = gold_answer(case, None)
            if "operations" in answer:
                answer["operations"] = json.loads(answer["operations"])
            if "max_duration_days" in answer:
                answer["max_duration_days"] = int(answer["max_duration_days"])
            row.update(answer)
        lines.append(json.dumps(row))
    sheet = tmp_path / "done.jsonl"
    sheet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = ingest_audit(sheet, exported / "key.jsonl", by_id())
    csv_sheet = tmp_path / "done.csv"
    filled_csv(exported, csv_sheet)
    assert result == ingest_audit(csv_sheet, exported / "key.jsonl", by_id())


def flip(decision: str, task: Task) -> str:
    """A different decision of the same task."""
    if task is Task.access_review:
        return {"CERTIFY": "REVOKE", "REVOKE": "CERTIFY", "ESCALATE": "CERTIFY"}[decision]
    return {"APPROVE": "DENY", "DENY": "APPROVE", "ESCALATE": "DENY"}[decision]


def test_each_kind_of_disagreement_is_found(tmp_path: Path, exported: Path) -> None:
    cases = by_id()
    keys = keys_by_id(exported)

    def other_cap(case: Case) -> str | None:
        """A cap that changes an approval's effective duration, if one can."""
        gold, request = case.gold, case.view.request
        if not isinstance(gold, RequestGold) or gold.decision is not RequestDecision.APPROVE:
            return None
        assert request is not None
        if gold.effective_duration_days != 1:
            return "1"
        requested = request.requested_duration_days
        return "2" if requested is None or requested > 1 else None

    approvals = {k.audit_id: cap for k in keys.values() if (cap := other_cap(cases[k.case_id]))}
    targets: dict[str, str] = {}

    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        audit_id = next(a for a, k in keys.items() if k.case_id == case.case_id)
        if case.task is Task.jml and "plan" not in targets.values():
            targets[audit_id] = "plan"
            answer["operations"] = "[]"
        elif case.task is Task.access_review and "decision" not in targets.values():
            targets[audit_id] = "decision"
            answer["decision"] = flip(answer["decision"], case.task)
        elif audit_id in approvals and "duration" not in targets.values():
            targets[audit_id] = "duration"
            answer["max_duration_days"] = approvals[audit_id]
        elif case.task is Task.access_request and "rules" not in targets.values():
            targets[audit_id] = "rules"
            answer["rules"] = "JUST-001 hyg-005"
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    found = {unit.audit_id: unit.disagreements for unit in result.disagreements}
    assert set(targets.values()) == {"plan", "decision", "duration", "rules"}
    for audit_id, kind in targets.items():
        assert DisagreementKind(kind) in found[audit_id]
    plan = next(u for u in result.units if targets.get(u.audit_id) == "plan")
    assert plan.human_operations == () and plan.gold_operations is not None
    rules = next(u for u in result.units if targets.get(u.audit_id) == "rules")
    assert rules.human_rules == ("HYG-005", "JUST-001")


def error_for(tmp_path: Path, exported: Path, edit: Callable[[list[dict[str, str]]], None]) -> str:
    sheet = tmp_path / "bad.csv"
    rows = filled_csv(exported, sheet)
    edit(rows)
    write_csv(sheet, rows, SHEET_CSV_COLUMNS)
    with pytest.raises(AuditIngestError) as caught:
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    return "\n".join(caught.value.problems)


def first(rows: list[dict[str, str]], task: Task) -> dict[str, str]:
    return next(r for r in rows if r["task"] == task.value)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda rows: rows[0].update(audit_id="OA-unknown"), "unknown audit_id"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate row"),
        (lambda rows: first(rows, Task.access_request).update(decision="MAYBE"), "decision must"),
        (lambda rows: first(rows, Task.access_review).update(decision="APPROVE"), "decision must"),
        (
            lambda rows: first(rows, Task.access_request).update(
                decision="APPROVE", max_duration_days="0"
            ),
            "at least 1",
        ),
        (
            lambda rows: first(rows, Task.access_request).update(
                decision="APPROVE", max_duration_days="ten"
            ),
            "whole number",
        ),
        (
            lambda rows: first(rows, Task.access_request).update(
                decision="DENY", max_duration_days="30"
            ),
            "applies only to an APPROVE",
        ),
        (lambda rows: first(rows, Task.access_request).update(rules="NOPE-001"), "unknown rule"),
        (lambda rows: first(rows, Task.jml).update(operations="[{"), "JSON list"),
        (lambda rows: first(rows, Task.jml).update(operations='{"op": "GRANT"}'), "JSON list"),
        (
            lambda rows: first(rows, Task.jml).update(
                operations='[{"op": "GRANT", "effective": "IMMEDIATE"}]'
            ),
            "operation 0",
        ),
        (
            lambda rows: first(rows, Task.jml).update(
                operations='[{"op": "REVOKE_ALL", "identity_id": "u_x", "effective": "soon"}]'
            ),
            "operation 0",
        ),
        (lambda rows: first(rows, Task.jml).update(decision="DENY"), "doesn't apply"),
        (lambda rows: first(rows, Task.access_request).update(operations="[]"), "doesn't apply"),
        (
            lambda rows: first(rows, Task.access_review).update(item_id="rv_nope"),
            "no campaign item",
        ),
        (lambda rows: first(rows, Task.access_request).update(item_id="x"), "leave item_id blank"),
        (lambda rows: first(rows, Task.access_request).update(task="jml"), "is a access_request"),
        (lambda rows: first(rows, Task.jml).update(operations=""), "not labeled"),
        (
            lambda rows: first(rows, Task.access_request).update(decision="", max_duration_days=""),
            "not labeled",
        ),
        (lambda rows: rows.remove(first(rows, Task.access_review)), "missing from the sheet"),
    ],
)
def test_ingest_reports_malformed_sheets(
    tmp_path: Path,
    exported: Path,
    edit: Callable[[list[dict[str, str]]], None],
    message: str,
) -> None:
    assert message in error_for(tmp_path, exported, edit)


def test_ingest_reports_every_problem_at_once(tmp_path: Path, exported: Path) -> None:
    def edit(rows: list[dict[str, str]]) -> None:
        rows[0]["audit_id"] = "OA-unknown"
        first(rows[1:], Task.access_request)["decision"] = "MAYBE"

    problems = error_for(tmp_path, exported, edit).splitlines()
    assert len(problems) >= 3  # the unknown row, the bad decision, the row now missing


def test_comma_only_rows_below_the_data_are_skipped(tmp_path: Path, exported: Path) -> None:
    """Spreadsheets write all-blank rows below the data once a cell there was touched."""
    sheet = tmp_path / "done.csv"
    rows = filled_csv(exported, sheet)
    with sheet.open("a", encoding="utf-8") as handle:
        handle.write("," * (len(SHEET_CSV_COLUMNS) - 1) + "\n" + " ,\t," + "\n")
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    assert len(result.units) == len(rows) and result.n_unlabeled == 0


def gold_rules(case: Case, item_id: str | None) -> tuple[str, ...]:
    gold = case.gold
    if isinstance(gold, ReviewGold):
        gold = next(item for item in gold.items if item.item_id == item_id)
    return tuple(sorted({finding.rule for finding in gold.findings}))


def test_none_asserts_that_no_rule_applies(tmp_path: Path, exported: Path) -> None:
    """A blank ``rules`` isn't compared; ``none`` is, in both directions."""
    written: dict[tuple[str, str | None], tuple[str, ...]] = {}

    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        answer["rules"] = " NONE " if len(written) % 2 else NO_RULES
        written[(case.case_id, item_id)] = gold_rules(case, item_id)
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    by_unit = {(u.case_id, u.item_id): u for u in result.units}
    with_findings = [unit for unit, rules in written.items() if rules]
    without = [unit for unit, rules in written.items() if not rules]
    assert with_findings and without
    for unit in with_findings:
        assert by_unit[unit].disagreements == (DisagreementKind.rules,)
        assert by_unit[unit].human_rules == ()
    assert all(by_unit[unit].agrees for unit in without)
    rows = read_csv(write_disagreements(result, tmp_path / "out").paths["disagreements"])
    assert rows and all(row["human_rules"] == NO_RULES and row["gold_rules"] for row in rows)


def test_none_stands_alone(tmp_path: Path, exported: Path) -> None:
    def edit(rows: list[dict[str, str]]) -> None:
        first(rows, Task.access_request).update(rules="none, JUST-001")

    assert "'none' means no rule applies, so it stands alone" in error_for(tmp_path, exported, edit)


def test_decisions_are_case_insensitive_and_jml_empty_plan_is_an_answer(
    tmp_path: Path, exported: Path
) -> None:
    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        if "decision" in answer:
            answer["decision"] = f"  {answer['decision'].lower()} "
        if case.task is Task.jml:
            answer["operations"] = "[]"
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    assert all(DisagreementKind.decision not in u.disagreements for u in result.units)
    plans = [u for u in result.units if u.task is Task.jml]
    assert plans and all(u.human_operations == () for u in plans)


def test_partial_ingest_skips_and_counts_unlabeled_units(tmp_path: Path, exported: Path) -> None:
    sheet = tmp_path / "partial.csv"
    rows = filled_csv(exported, sheet)
    blank = {name: "" for name in ANSWER_FIELDS}
    blanked = first(rows, Task.access_request)
    blanked.update(blank)
    dropped = first(rows, Task.jml)
    rows.remove(dropped)
    write_csv(sheet, rows, SHEET_CSV_COLUMNS)
    with pytest.raises(AuditIngestError):
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    assert (result.n_unlabeled, result.n_deferred) == (2, 0)
    labeled = {u.audit_id for u in result.units}
    assert blanked["audit_id"] not in labeled and dropped["audit_id"] not in labeled
    assert len(result.units) == len(rows) - 1
    assert result.summary()["deferred_units"] == 0


def test_partial_ingest_holds_back_a_partly_labeled_campaign(
    tmp_path: Path, exported: Path
) -> None:
    """Comparing some items of a campaign would show its tier and gold's findings while the
    labeler still has its other items to do."""
    sheet = tmp_path / "partial.csv"
    rows = filled_csv(exported, sheet)
    sizes = Counter(r["audit_id"] for r in rows if r["task"] == Task.access_review.value)
    campaign, size = sizes.most_common(1)[0]
    assert size > 1 and len(sizes) > 1
    items = [r for r in rows if r["audit_id"] == campaign]
    items[-1].update({name: "" for name in ANSWER_FIELDS})
    write_csv(sheet, rows, SHEET_CSV_COLUMNS)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    assert (result.n_unlabeled, result.n_deferred) == (1, len(items) - 1)
    assert campaign not in {u.audit_id for u in result.units}
    assert len(result.units) == len(rows) - len(items)
    assert any(u.task is Task.access_review for u in result.units)  # finished campaigns count
    assert result.summary()["deferred_units"] == len(items) - 1


def test_ingest_refuses_a_dataset_without_the_exported_cases(
    tmp_path: Path, exported: Path
) -> None:
    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet)
    keys = read_audit_keys(exported / "key.jsonl")
    cases = by_id()
    missing = {cid: c for cid, c in cases.items() if cid != keys[0].case_id}
    with pytest.raises(AuditIngestError, match="not in the dataset"):
        ingest_audit(sheet, keys, missing)
    review = next(k for k in keys if k.task is Task.access_review)
    changed: Mapping[str, Case] = {
        **cases,
        review.case_id: cases[review.case_id].model_copy(update={"task": Task.jml}),
    }
    with pytest.raises(AuditIngestError, match="changed since the export"):
        ingest_audit(sheet, keys, changed)


def test_ingest_rejects_other_file_types(tmp_path: Path, exported: Path) -> None:
    sheet = tmp_path / "done.txt"
    sheet.write_text("audit_id\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.csv or \.jsonl"):
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases())


def test_jsonl_rows_must_be_objects_with_item_lists(tmp_path: Path, exported: Path) -> None:
    sheet = tmp_path / "bad.jsonl"
    sheet.write_text('[1, 2]\n{"audit_id": "x", "items": 3}\n{"items": [5]}\n', encoding="utf-8")
    with pytest.raises(AuditIngestError) as caught:
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    problems = "\n".join(caught.value.problems)
    assert "not a JSON object" in problems and "items must be a list" in problems
    assert "item 0: not a JSON object" in problems


# --------------------------------------------------------------------------------------------
# The disagreement sheet
# --------------------------------------------------------------------------------------------


def disagreeing(tmp_path: Path, exported: Path, notes: str = "") -> Path:
    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        if case.task is Task.access_review:
            answer["decision"] = flip(answer["decision"], case.task)
            answer["notes"] = notes
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    return sheet


def test_disagreement_sheet_has_blank_classification(tmp_path: Path, exported: Path) -> None:
    sheet = disagreeing(tmp_path, exported, notes="=HYPERLINK(1)")
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    paths = write_disagreements(result, tmp_path / "out").paths
    rows = read_csv(paths["disagreements"])
    assert len(rows) == len(result.disagreements) > 0
    assert tuple(rows[0]) == DISAGREEMENT_CSV_COLUMNS
    assert all(row["classification"] == "" and row["resolution"] == "" for row in rows)
    assert all(row["status"] == "open" for row in rows)
    assert all(row["kinds"] == "decision" and row["task"] == "access_review" for row in rows)
    assert all(row["item_tier"] and row["case_id"] for row in rows)
    assert all(row["notes"] == "'=HYPERLINK(1)" for row in rows)  # never a live formula
    results = (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    assert len(results) == len(result.units)
    summary = json.loads(paths["summary"].read_text())
    assert summary["disagree"] == len(rows) and summary["by_kind"]["decision"] == len(rows)
    assert summary["disagreement_sheet"] == {
        "rows": len(rows),
        "by_status": {"open": len(rows), "resolved": 0, "unlabeled": 0},
        "by_classification": {"oracle_bug": 0, "ambiguous_text": 0, "human_error": 0},
        "open_unclassified": len(rows),
        "unknown_classifications": [],
    }


def test_rewriting_the_sheet_keeps_the_authors_classifications(
    tmp_path: Path, exported: Path
) -> None:
    sheet = disagreeing(tmp_path, exported)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    out = tmp_path / "out"
    paths = write_disagreements(result, out).paths
    rows = read_csv(paths["disagreements"])
    rows[0]["classification"] = Classification.ambiguous_text.value
    rows[0]["resolution"] = "reworded APPR-002"
    write_csv(paths["disagreements"], rows, DISAGREEMENT_CSV_COLUMNS)
    sheet_state = write_disagreements(result, out)
    again = read_csv(paths["disagreements"])
    assert again[0]["classification"] == "ambiguous_text"
    assert again[0]["resolution"] == "reworded APPR-002"
    assert all(row["classification"] == "" for row in again[1:])
    assert sheet_state.open_unclassified == len(again) - 1
    assert sheet_state.by_classification[Classification.ambiguous_text] == 1


def test_added_columns_and_author_text_survive_rewrites(tmp_path: Path, exported: Path) -> None:
    """Columns the author adds are kept, and their own text comes back as typed: the
    spreadsheet escape is for values the harness fills in, not for the author's resolution."""
    sheet = disagreeing(tmp_path, exported, notes="-labeler note")
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    out = tmp_path / "out"
    path = write_disagreements(result, out).paths["disagreements"]
    rows = read_csv(path)
    assert len(rows) > 2
    columns = (*DISAGREEMENT_CSV_COLUMNS, "errata_id", "fix_commit")
    for row in rows:
        row.update(errata_id="", fix_commit="")
    rows[0].update(classification="ambiguous_text", resolution="-> reworded APPR-002")
    rows[0].update(errata_id="=E-1", fix_commit="+abc123")
    rows[1].update(fix_commit="@pending")  # an added column alone marks the row as the author's
    write_csv(path, rows, columns)
    write_disagreements(result, out)
    first_rewrite = path.read_bytes()
    write_disagreements(result, out)
    assert path.read_bytes() == first_rewrite
    again = read_csv(path)
    assert tuple(again[0]) == columns
    assert (again[0]["resolution"], again[0]["errata_id"], again[0]["fix_commit"]) == (
        "-> reworded APPR-002",
        "=E-1",
        "+abc123",
    )
    assert again[1]["fix_commit"] == "@pending"
    assert all(row["notes"] == "'-labeler note" for row in again)  # harness values stay escaped


def classify(path: Path, edits: Mapping[int, tuple[str, str]]) -> list[dict[str, str]]:
    """Enter ``(classification, resolution)`` on the given rows of a disagreement sheet."""
    rows = read_csv(path)
    for index, (classification, resolution) in edits.items():
        rows[index].update(classification=classification, resolution=resolution)
    write_csv(path, rows, DISAGREEMENT_CSV_COLUMNS)
    return rows


def test_fixed_disagreements_stay_on_the_sheet_as_resolved(tmp_path: Path, exported: Path) -> None:
    """Classify, fix at the source, re-ingest: the record of what was found survives."""
    sheet = disagreeing(tmp_path, exported)
    out = tmp_path / "out"
    first_pass = write_disagreements(ingest_audit(sheet, exported / "key.jsonl", by_id()), out)
    rows = classify(first_pass.paths["disagreements"], {0: ("Human Error", "misread the item")})
    fixed = (rows[0]["audit_id"], rows[0]["item_id"])
    # The labeler corrects that one answer in the completed sheet and ingests it again.
    answers = read_csv(sheet)
    case_of = {k.audit_id: k.case_id for k in read_audit_keys(exported / "key.jsonl")}
    for answer in answers:
        if (answer["audit_id"], answer["item_id"]) == fixed:
            gold = gold_answer(by_id()[case_of[fixed[0]]], fixed[1])
            answer["decision"] = gold["decision"]
    write_csv(sheet, answers, SHEET_CSV_COLUMNS)
    second = write_disagreements(ingest_audit(sheet, exported / "key.jsonl", by_id()), out)
    again = read_csv(second.paths["disagreements"])
    assert len(again) == len(rows)
    resolved = next(r for r in again if (r["audit_id"], r["item_id"]) == fixed)
    assert resolved["status"] == "resolved" and resolved["kinds"] == ""
    assert resolved["human_decision"] == resolved["gold_decision"]
    assert (resolved["classification"], resolved["resolution"]) == (
        "human_error",
        "misread the item",
    )
    assert all(r["status"] == "open" for r in again if r is not resolved)
    summary = json.loads(second.paths["summary"].read_text())["disagreement_sheet"]
    assert summary["by_status"] == {"open": len(rows) - 1, "resolved": 1, "unlabeled": 0}
    assert summary["by_classification"]["human_error"] == 1
    assert summary["open_unclassified"] == len(rows) - 1


def test_classified_rows_a_partial_ingest_leaves_out_are_carried_over(
    tmp_path: Path, exported: Path
) -> None:
    sheet = disagreeing(tmp_path, exported)
    out = tmp_path / "out"
    first_pass = write_disagreements(ingest_audit(sheet, exported / "key.jsonl", by_id()), out)
    rows = classify(first_pass.paths["disagreements"], {0: ("oracle_bug", "fixed APPR-005")})
    blanked = rows[0]["audit_id"]
    answers = read_csv(sheet)
    for answer in answers:
        if answer["audit_id"] == blanked:
            answer.update({name: "" for name in ANSWER_FIELDS})
    write_csv(sheet, answers, SHEET_CSV_COLUMNS)
    partial = ingest_audit(sheet, exported / "key.jsonl", by_id(), require_complete=False)
    second = write_disagreements(partial, out)
    again = read_csv(second.paths["disagreements"])
    carried = [r for r in again if r["status"] == "unlabeled"]
    assert len(carried) == 1
    assert {k: v for k, v in carried[0].items() if k != "status"} == {
        k: v for k, v in rows[0].items() if k != "status"
    }
    assert all(r["audit_id"] != blanked for r in again if r["status"] != "unlabeled")
    assert second.by_status[RowStatus.unlabeled] == 1
    assert second.by_classification[Classification.oracle_bug] == 1


def test_classifications_are_normalized_and_unknown_ones_reported(
    tmp_path: Path, exported: Path
) -> None:
    sheet = disagreeing(tmp_path, exported)
    out = tmp_path / "out"
    result = ingest_audit(sheet, exported / "key.jsonl", by_id())
    first_pass = write_disagreements(result, out)
    edits = {0: ("  Oracle-Bug ", ""), 1: ("ambiguous text", ""), 2: ("typo", ""), 3: ("", "why")}
    rows = classify(first_pass.paths["disagreements"], edits)
    second = write_disagreements(result, out)
    again = read_csv(second.paths["disagreements"])
    assert [r["classification"] for r in again[:4]] == ["oracle_bug", "ambiguous_text", "typo", ""]
    assert again[3]["resolution"] == "why"
    assert second.unknown_classifications == (f"{rows[2]['audit_id']} item {rows[2]['item_id']}",)
    assert second.open_unclassified == len(again) - 2  # "typo" doesn't count as classified
    summary = json.loads(second.paths["summary"].read_text())["disagreement_sheet"]
    assert summary["unknown_classifications"] == list(second.unknown_classifications)


# --------------------------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------------------------


def test_jml_rules_without_operations_is_an_error(tmp_path: Path, exported: Path) -> None:
    def edit(rows: list[dict[str, str]]) -> None:
        first(rows, Task.jml).update(operations="", rules="JML-001")

    assert "operations is blank" in error_for(tmp_path, exported, edit)


def test_jsonl_answers_take_native_types_and_are_checked(tmp_path: Path, exported: Path) -> None:
    keys = keys_by_id(exported)
    cases = by_id()
    rows = [json.loads(line) for line in (exported / "sheet.jsonl").read_text().splitlines()]
    request = next(r for r in rows if r["task"] == "access_request")
    case = cases[keys[request["audit_id"]].case_id]
    assert isinstance(case.gold, RequestGold)
    request.update(decision="APPROVE", max_duration_days=True, rules=["temp-001", "TEMP-001"])
    sheet = tmp_path / "s.jsonl"
    sheet.write_text("\n\n".join(json.dumps(r) for r in rows) + "\n{broken\n", encoding="utf-8")
    with pytest.raises(AuditIngestError) as caught:
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    problems = "\n".join(caught.value.problems)
    assert "whole number of days, got True" in problems
    assert "not a JSON object" in problems  # the broken last line
    request["max_duration_days"] = 7
    sheet.write_text(json.dumps(request) + "\n", encoding="utf-8")
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    (unit,) = result.units
    assert unit.human_rules == ("TEMP-001",)


def test_plan_disagreements_show_both_plans(tmp_path: Path, exported: Path) -> None:
    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        if case.task is Task.jml:
            answer["operations"] = "[]"
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    paths = write_disagreements(result, tmp_path / "out").paths
    plans = [r for r in read_csv(paths["disagreements"]) if r["task"] == "jml"]
    assert plans and all(r["kinds"] == "plan" and r["human_operations"] == "[]" for r in plans)
    assert all(json.loads(r["gold_operations"]) for r in plans if r["gold_operations"] != "[]")


def test_unreadable_keys_and_manifests(tmp_path: Path, exported: Path) -> None:
    keys = tmp_path / "key.jsonl"
    keys.write_text('\n{"audit_id": "x", "tier": "L3-sentinel"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"key\.jsonl:2: not an audit key") as caught:
        read_audit_keys(keys)
    # One field and why, never the record's values (a labeler may be the one reading it).
    assert "L3-sentinel" not in str(caught.value) and "errors.pydantic.dev" not in str(caught.value)
    assert len(str(caught.value).splitlines()) == 1
    manifest = tmp_path / "audit.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not an audit manifest"):
        read_audit_manifest(manifest)
    older = json.loads((exported / "audit.json").read_text(encoding="utf-8"))
    older["format"] = AUDIT_FORMAT - 1
    manifest.write_text(json.dumps(older), encoding="utf-8")
    with pytest.raises(ValueError, match=f"audit format {AUDIT_FORMAT - 1}.*export the sheets"):
        read_audit_manifest(manifest)


# --------------------------------------------------------------------------------------------
# Drift: what changed between export and ingest
# --------------------------------------------------------------------------------------------


def test_keys_fingerprint_the_prompt_and_gold_per_unit(exported: Path) -> None:
    for key in read_audit_keys(exported / "key.jsonl"):
        assert len(key.gold_sha256) == max(1, len(key.item_ids))
        message = (exported / "messages" / f"{key.audit_id}.md").read_text(encoding="utf-8")
        assert key.message_sha256 == hashlib.sha256(message.encode("utf-8")).hexdigest()
        system = (exported / key.system_ref).read_text(encoding="utf-8")
        assert key.system_sha256 == hashlib.sha256(system.encode("utf-8")).hexdigest()


def _request_key(exported: Path) -> tuple[str, Case]:
    key = next(k for k in read_audit_keys(exported / "key.jsonl") if k.task is Task.access_request)
    return key.audit_id, by_id()[key.case_id]


def test_a_label_made_on_another_view_is_stale(tmp_path: Path, exported: Path) -> None:
    audit_id, case = _request_key(exported)
    assert case.view.request is not None
    request = case.view.request.model_copy(
        update={"justification": f"{case.view.request.justification} Regenerated."}
    )
    edited = case.model_copy(update={"view": case.view.model_copy(update={"request": request})})
    cases = {**by_id(), case.case_id: edited}
    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet)
    with pytest.raises(AuditIngestError) as caught:
        ingest_audit(sheet, exported / "key.jsonl", cases)
    (problem,) = caught.value.problems
    assert problem.startswith(f"{audit_id}: its case message changed since the export")
    result = ingest_audit(sheet, exported / "key.jsonl", cases, allow_stale=True)
    stale = [u for u in result.units if u.stale]
    assert [u.audit_id for u in stale] == [audit_id]
    assert Drift.message in stale[0].drift
    assert all(not u.drift for u in result.units if u.audit_id != audit_id)
    assert result.summary()["stale_units"] == 1
    # Unlabeled, a stale case is nothing to refuse: there is no label to be stale.
    rows = read_csv(sheet)
    for row in rows:
        if row["audit_id"] == audit_id:
            row.update({name: "" for name in ANSWER_FIELDS})
    write_csv(sheet, rows, SHEET_CSV_COLUMNS)
    partial = ingest_audit(sheet, exported / "key.jsonl", cases, require_complete=False)
    assert not any(u.drift for u in partial.units)


def test_a_changed_system_prompt_makes_every_label_stale(
    tmp_path: Path, exported: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reworded(case: Case, variant: str) -> Any:
        request = build_request(case, variant)
        return dataclasses.replace(request, system=request.system + "\nReworded APPR-002.\n")

    monkeypatch.setattr("identitybench.audit.build_request", reworded)
    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet)
    with pytest.raises(AuditIngestError) as caught:
        ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    assert len(caught.value.problems) == N
    assert all("its system prompt changed" in problem for problem in caught.value.problems)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), allow_stale=True)
    assert all(u.drift == (Drift.system,) and u.stale for u in result.units)


def test_new_gold_on_an_unchanged_prompt_is_an_oracle_change(
    tmp_path: Path, exported: Path
) -> None:
    """The label still applies: the comparison with the new gold says whether the fix resolved
    the disagreement, and the row says the oracle moved."""
    audit_id, case = _request_key(exported)
    assert isinstance(case.gold, RequestGold)
    flipped = flip(case.gold.decision.value, Task.access_request)
    new_gold = case.gold.model_copy(update={"decision": RequestDecision(flipped)})
    cases = {**by_id(), case.case_id: case.model_copy(update={"gold": new_gold})}
    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet)
    result = ingest_audit(sheet, exported / "key.jsonl", cases)  # nothing stale to refuse
    (changed,) = [u for u in result.units if u.drift]
    assert (changed.audit_id, changed.drift, changed.stale) == (audit_id, (Drift.gold,), False)
    assert DisagreementKind.decision in changed.disagreements
    assert result.summary()["by_drift"] == {"message": 0, "system": 0, "gold": 1}
    rows = read_csv(write_disagreements(result, tmp_path / "out").paths["disagreements"])
    assert [(row["audit_id"], row["drift"]) for row in rows] == [(audit_id, "gold")]


def test_a_campaign_records_gold_per_item(tmp_path: Path, exported: Path) -> None:
    keys = read_audit_keys(exported / "key.jsonl")
    key = max((k for k in keys if k.task is Task.access_review), key=lambda k: len(k.item_ids))
    case = by_id()[key.case_id]
    assert isinstance(case.gold, ReviewGold) and len(case.gold.items) > 1
    last = case.gold.items[-1]
    decision = "REVOKE" if last.decision.value != "REVOKE" else "CERTIFY"
    items = (*case.gold.items[:-1], last.model_copy(update={"decision": ReviewDecision(decision)}))
    cases = {
        **by_id(),
        case.case_id: case.model_copy(
            update={"gold": case.gold.model_copy(update={"items": items})}
        ),
    }
    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet)
    result = ingest_audit(sheet, exported / "key.jsonl", cases)
    assert [u.item_id for u in result.units if u.drift] == [last.item_id]


def test_colliding_audit_ids_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("identitybench.audit._audit_id", lambda salt, case_id: "OA-same")
    with pytest.raises(ValueError, match="collide"):
        export_audit_sheets(oracle_cases(), tmp_path / "o", dataset_sha256=SHA, n=3)


# --------------------------------------------------------------------------------------------
# Weights and the dataset-level estimate
# --------------------------------------------------------------------------------------------


def cell(case: Case) -> str:
    return f"{case.task.value}|{case.tier.value}"


def test_keys_record_inverse_inclusion_weights(exported: Path) -> None:
    keys = read_audit_keys(exported / "key.jsonl")
    available = Counter(cell(c) for c in oracle_cases())  # one family per conformance case
    picked = Counter(f"{k.task.value}|{k.tier.value}" for k in keys)
    for key in keys:
        stratum = f"{key.task.value}|{key.tier.value}"
        assert key.weight == pytest.approx(available[stratum] / picked[stratum])
    assert sum(k.weight for k in keys) == pytest.approx(sum(available[c] for c in picked))
    # Balanced selection over-samples small cells: their cases weigh less.
    small = min(picked, key=lambda c: available[c])
    large = max(picked, key=lambda c: available[c])
    assert available[small] / picked[small] < available[large] / picked[large]


def test_summary_weights_case_agreement_back_to_the_dataset(tmp_path: Path, exported: Path) -> None:
    sheet = disagreeing(tmp_path, exported)  # every review item flipped
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    summary = result.summary()
    keys = read_audit_keys(exported / "key.jsonl")
    agreeing = [k for k in keys if k.task is not Task.access_review]
    assert (summary["labeled_cases"], summary["agree_cases"]) == (N, len(agreeing))
    assert summary["case_agreement"]["sample"] == pytest.approx(len(agreeing) / N)
    weighted = sum(k.weight for k in agreeing) / sum(k.weight for k in keys)
    assert summary["case_agreement"]["weighted"] == pytest.approx(weighted)
    assert weighted != pytest.approx(len(agreeing) / N)
    reviews = summary["by_task"]["access_review"]
    assert reviews["agree"] == 0 and reviews["labeled"] > reviews["labeled_cases"]
    assert reviews["case_agreement"] == {"sample": 0.0, "weighted": 0.0}
    requests = summary["by_task"]["access_request"]
    assert requests["case_agreement"] == {"sample": 1.0, "weighted": 1.0}
    assert sum(t["labeled_cases"] for t in summary["by_task"].values()) == N
    for name, stratum in summary["by_task_tier"].items():
        assert stratum["labeled_cases"] == sum(
            1 for k in keys if f"{k.task.value}|{k.tier.value}" == name
        )


def test_an_empty_result_has_no_agreement_rates(tmp_path: Path, exported: Path) -> None:
    sheet = tmp_path / "blank.csv"
    write_csv(sheet, read_csv(exported / "sheet.csv"), SHEET_CSV_COLUMNS)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases(), require_complete=False)
    summary = result.summary()
    assert summary["labeled_cases"] == 0
    assert summary["case_agreement"] == {"sample": None, "weighted": None}
    assert summary["by_task"] == {} and summary["by_task_tier"] == {}


# --------------------------------------------------------------------------------------------
# What a disagreement row shows
# --------------------------------------------------------------------------------------------


def test_disagreement_rows_carry_what_classifying_needs(tmp_path: Path, exported: Path) -> None:
    cases = by_id()
    keys = keys_by_id(exported)

    def fill(case: Case, item_id: str | None) -> dict[str, Any]:
        answer = gold_answer(case, item_id)
        if case.task is Task.jml:
            answer["operations"] = "[]"
        elif case.task is Task.access_request and answer["decision"] == "APPROVE":
            answer["max_duration_days"] = "1"
        elif case.task is Task.access_request:
            answer["decision"] = "APPROVE"
            answer["max_duration_days"] = "5"
        return answer

    sheet = tmp_path / "done.csv"
    filled_csv(exported, sheet, fill)
    result = ingest_audit(sheet, exported / "key.jsonl", oracle_cases())
    rows = read_csv(write_disagreements(result, tmp_path / "out").paths["disagreements"])
    assert rows
    for row in rows:
        key = keys[row["audit_id"]]
        assert row["message_ref"] == f"messages/{key.audit_id}.md"
        assert row["system_ref"] == key.system_ref
        assert (exported / row["message_ref"]).is_file()
        assert (exported / row["system_ref"]).is_file()
        case = cases[key.case_id]
        gold = case.gold
        if isinstance(gold, RequestGold):
            assert case.view.request is not None
            requested = case.view.request.requested_duration_days
            assert row["requested_duration"] == ("" if requested is None else str(requested))
            assert row["human_cap"] in ("1", "5")
            assert row["gold_findings"] == " ".join(
                sorted({f"{f.rule}={f.effect.value}" for f in gold.findings})
            )
            assert (row["plan_unsafe"], row["plan_diff"]) == ("", "")
        if isinstance(gold, JmlGold):
            assert row["kinds"] == "plan" and row["plan_unsafe"] in ("yes", "no")
            assert row["plan_diff"] and all("=" in part for part in row["plan_diff"].split())
            assert row["human_cap"] == ""
    leaver = [r for r in rows if r["task"] == "jml" and r["plan_unsafe"] == "yes"]
    assert leaver  # an empty plan leaves access a removal plan takes away


def test_disagreement_rows_list_the_exceptions_gold_evaluated(tmp_path: Path) -> None:
    with_exceptions = [
        c for c in oracle_cases() if isinstance(c.gold, RequestGold) and c.gold.exceptions
    ]
    assert with_exceptions
    out = tmp_path / "audit"
    n = len(with_exceptions)
    export_audit_sheets(with_exceptions, out, dataset_sha256=SHA, n=n)

    def wrong(case: Case, item_id: str | None) -> dict[str, Any]:
        return {"decision": flip(gold_answer(case, item_id)["decision"], case.task)}

    sheet = tmp_path / "done.csv"
    filled_csv(out, sheet, wrong)
    result = ingest_audit(sheet, out / "key.jsonl", with_exceptions)
    rows = read_csv(write_disagreements(result, tmp_path / "res").paths["disagreements"])
    assert len(rows) == n
    keys = keys_by_id(out)
    cases = by_id()
    for row in rows:
        gold = cases[keys[row["audit_id"]].case_id].gold
        assert isinstance(gold, RequestGold)
        expected = sorted(
            f"{e.rule} for {e.for_rule}: {'applies' if e.applies else 'does not apply'}"
            for e in gold.exceptions
        )
        assert row["gold_exceptions"] == "; ".join(expected)


# --------------------------------------------------------------------------------------------
# Replacing an export
# --------------------------------------------------------------------------------------------


def test_export_refuses_a_directory_holding_an_export(tmp_path: Path, exported: Path) -> None:
    out = tmp_path / "copy"
    export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, n=5, seed=1)
    before = snapshot(out)
    with pytest.raises(FileExistsError, match="already holds an audit export"):
        export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, n=5, seed=2)
    assert snapshot(out) == before


def test_overwrite_leaves_exactly_the_new_export(tmp_path: Path) -> None:
    out = tmp_path / "audit"
    export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, n=15, seed=1)
    sheet = tmp_path / "done.csv"
    filled_csv(out, sheet)
    write_disagreements(ingest_audit(sheet, out / "key.jsonl", oracle_cases()), out)
    (out / "NOTES.md").write_text("the author's own notes\n", encoding="utf-8")
    (out / "messages" / "README.txt").write_text("not an export file\n", encoding="utf-8")
    with pytest.raises(ValueError, match="families are available"):  # a failed export...
        export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, n=10_000, overwrite=True)
    assert (out / "summary.json").is_file()  # ...replaces nothing
    export_audit_sheets(oracle_cases(), out, dataset_sha256=SHA, n=4, seed=2, overwrite=True)
    fresh = tmp_path / "fresh"
    export_audit_sheets(oracle_cases(), fresh, dataset_sha256=SHA, n=4, seed=2)
    kept = {"NOTES.md": b"the author's own notes\n", "messages/README.txt": b"not an export file\n"}
    assert snapshot(out) == snapshot(fresh) | kept


def test_remove_artifacts_drops_emptied_directories_only(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "y.md").write_text("y", encoding="utf-8")
    (tmp_path / "c" / "keep.txt").write_text("k", encoding="utf-8")
    (tmp_path / "c" / "dir.md").mkdir()  # matches a pattern, but only files are removed
    (tmp_path / "top.json").write_text("{}", encoding="utf-8")
    remove_artifacts(tmp_path, ("a/b/*.md", "c/*.md", "top.json", "missing/*.md"))
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "a",
        "c",
        "c/dir.md",
        "c/keep.txt",
    ]


def test_a_failed_staged_export_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "key.jsonl").write_text("old key\n", encoding="utf-8")
    (target / "labels.jsonl").write_text("old labels\n", encoding="utf-8")
    before = snapshot(target)
    with pytest.raises(RuntimeError, match="halfway"), staged_export(target, ("*.jsonl",)) as stage:
        (stage / "key.jsonl").write_text("new key\n", encoding="utf-8")
        raise RuntimeError("failed halfway")
    assert snapshot(target) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]  # the staging dir is gone


def test_a_staged_export_replaces_only_the_named_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "new" / "out"  # created on success, parents included
    with staged_export(target) as stage:
        (stage / "sub").mkdir()
        (stage / "sub" / "a.md").write_text("a\n", encoding="utf-8")
    assert snapshot(target) == {"sub/a.md": b"a\n"}
    (target / "mine.txt").write_text("mine\n", encoding="utf-8")
    (target / "sub" / "b.md").write_text("old b\n", encoding="utf-8")
    with staged_export(target, ("sub/*.md",)) as stage:
        (stage / "c.md").write_text("c\n", encoding="utf-8")
    assert snapshot(target) == {"c.md": b"c\n", "mine.txt": b"mine\n"}
    assert sorted(p.name for p in target.parent.iterdir()) == ["out"]


def test_staged_exports_move_every_target_or_none(tmp_path: Path) -> None:
    """The judge-label sheets and their key are written to two directories: a failure leaves
    both as they were, and success replaces both."""
    sheets, key = tmp_path / "judge", tmp_path / "judge-key"
    sheets.mkdir()
    key.mkdir()
    (sheets / "dev.csv").write_text("old sheet\n", encoding="utf-8")
    (key / "key.jsonl").write_text("old key\n", encoding="utf-8")
    before = snapshot(tmp_path)
    targets = [(sheets, ("dev.csv",)), (key, ("key.jsonl",))]
    with pytest.raises(RuntimeError, match="failed"), staged_exports(targets) as (s_dir, k_dir):
        (s_dir / "dev.csv").write_text("new sheet\n", encoding="utf-8")
        (k_dir / "key.jsonl").write_text("new key\n", encoding="utf-8")
        raise RuntimeError("failed after both were written")
    assert snapshot(tmp_path) == before
    with staged_exports(targets) as (s_dir, k_dir):
        (s_dir / "dev.csv").write_text("new sheet\n", encoding="utf-8")
        (k_dir / "key.jsonl").write_text("new key\n", encoding="utf-8")
    assert snapshot(tmp_path) == {
        "judge/dev.csv": b"new sheet\n",
        "judge-key/key.jsonl": b"new key\n",
    }
