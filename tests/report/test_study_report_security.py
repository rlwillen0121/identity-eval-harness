"""Local-only regression checks for the study HTML builder and chart destinations."""

import importlib.util
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType

import pytest

REPORTS = Path(__file__).resolve().parents[2] / "reports" / "v41-lite"
ATTACK = "</script><img src=x onerror=\"alert(1)\"> & 'quoted'"


def _load(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(REPORTS))
    spec = importlib.util.spec_from_file_location(name, REPORTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs.extend(k for k, _ in attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def test_study_payload_cannot_close_inline_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load("build_skill_orchestration_report", monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STUDY_DIR", tmp_path)
    monkeypatch.setattr(module, "RECORDS_DIR", tmp_path / "absent")
    monkeypatch.setattr(module, "_load_dataset_oracles", lambda: ({}, {}))
    monkeypatch.setattr(module, "ARM_DISPLAY", {"attack": ATTACK})
    result = module.build_report().read_text()
    parser = _Tags()
    parser.feed(result)
    assert parser.tags.count("script") == 1
    assert "img" not in parser.tags
    script = result.split("<script>", 1)[1].split("</script>", 1)[0]
    payload = json.loads(script.split("const PAYLOAD = ", 1)[1].split(";\n", 1)[0])
    assert payload["arms_meta"]["attack"] == ATTACK


def test_study_dom_interpolations_treat_payload_fields_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("local Node runtime required to exercise report JavaScript")
    module = _load("build_skill_orchestration_report", monkeypatch)
    numeric_fields = [
        "gsrr_pct",
        "tasks_passed",
        "tasks_total",
        "task_pass_pct",
        "w1_pass_pct",
        "w2_pass_pct",
        "w3_pass_pct",
        "u_exec_total",
        "turns_mean",
        "lat_mean_s",
        "cost_usd",
        "chk_calls_mean",
        "checker_repaired_waves",
        "checker_reject_count",
        "worker_direct_commits",
        "total_waves",
        "worker_direct_pct",
        "boss_escalations",
        "boss_esc_pct",
        "worker_lat_mean_s",
        "boss_lat_mean_s",
        "worker_cost_usd",
        "boss_cost_usd",
    ]
    cell = {key: 1 for key in numeric_fields}
    cell.update(
        {
            "cell_id": "claude-opus-5-5__A1_solo_no_skill",
            "arm_id": "A1_solo_no_skill",
            "display_title": ATTACK,
            "arm_badge": ATTACK,
            "arm_color": ATTACK,
            "boss_key": "claude-opus-5-5",
            "is_team": True,
            "cat_pass_12": {},
            "cost_basis": "est",
        }
    )
    payload = {
        "cells": [cell],
        "models_meta": {"claude-opus-5-5": {"name": ATTACK}},
        "arms_meta": {"a": dict.fromkeys(["color", "badge", "short", "desc"], ATTACK)},
    }
    # Protect the script boundary separately so this test isolates DOM insertion.
    encoded = json.dumps(payload).replace("<", "\\u003c")
    page = module._HTML_TEMPLATE.replace("__PAYLOAD_JSON__", encoded)
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
const writes = [];
const element = () => ({
  set innerHTML(value) { writes.push(value); },
  appendChild() {}, setAttribute() {}, addEventListener() {}
});
global.document = {
  querySelectorAll: () => [], getElementById: element, createElement: element,
  documentElement: element()
};
"""
    result = subprocess.run(
        [node, "-e", harness + script + "\nconsole.log(JSON.stringify(writes));"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    parser = _Tags()
    for fragment in json.loads(result.stdout):
        parser.feed(fragment)
    assert "img" not in parser.tags
    assert not any(attr.startswith("on") for attr in parser.attrs)
    assert ATTACK in "".join(parser.text)


@pytest.mark.parametrize("name", ["build_simple_optimal_chart", "build_skill_orchestration_charts"])
def test_chart_builders_have_no_personal_export_directory(name: str) -> None:
    source = (REPORTS / f"{name}.py").read_text()
    assert not re.search(r"/(?:Users|home)/[A-Za-z0-9._-]+", source)


@pytest.mark.parametrize("name", ["build_simple_optimal_chart", "build_skill_orchestration_charts"])
def test_chart_builders_only_write_report_outputs(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(name, monkeypatch)
    monkeypatch.setattr(module, "CHARTS_DIR", tmp_path / "charts")
    monkeypatch.setattr(module, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(module, "write_png", lambda path: None)
    for fn in (
        "build_simple_optimal_chart",
        "build_gantt_leaderboard",
        "build_pareto_and_progression",
        "build_swarm_wave_delegation",
        "build_category_heatmap_scorecard",
    ):
        if hasattr(module, fn):
            monkeypatch.setattr(module, fn, lambda *args: "<svg/>")
    if hasattr(module, "_load_summary"):
        monkeypatch.setattr(module, "_load_summary", lambda: {})

    def no_copy(*args: object, **kwargs: object) -> None:
        pytest.fail("report builder copied output outside CHARTS_DIR")

    monkeypatch.setattr(shutil, "copy2", no_copy)
    module.main()
    assert list((tmp_path / "charts").glob("*.svg"))
