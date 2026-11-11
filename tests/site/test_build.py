"""Contract checks for the committed static leaderboard data."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
DATA = SITE / "data" / "leaderboard.json"
FORBIDDEN = ("provider", "_cli", "http", "local_llm", "api_key")
BUILD_SPEC = importlib.util.spec_from_file_location("identitybench_site_build", SITE / "build.py")
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
BUILD = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(BUILD)


def load_data() -> dict[str, Any]:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def model_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    models = data.get("models")
    assert isinstance(models, list) and models
    assert all(isinstance(model, dict) for model in models)
    return models


def test_committed_data_matches_build() -> None:
    subprocess.run(
        [sys.executable, "site/build.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_history_uses_newest_continuous_complete_presence_run() -> None:
    def arm(arm_id: str, *, completed: int = 3, label: str | None = None) -> dict[str, Any]:
        return {
            "arm_id": arm_id,
            "n_completed": completed,
            "label": label or arm_id,
        }

    revisions = [
        (
            "2026-01-01",
            3,
            {
                "steady": arm("steady"),
                "returning": arm("returning"),
                "label-gap": arm("label-gap"),
                "count-gap": arm("count-gap"),
                "stale": arm("stale"),
                "latest-count-partial": arm("latest-count-partial"),
                "latest-label-partial": arm("latest-label-partial"),
            },
        ),
        (
            "2026-01-02",
            3,
            {
                "steady": arm("steady"),
                "label-gap": arm("label-gap", label="Label Gap (1/3)"),
                "count-gap": arm("count-gap", completed=2),
                "stale": arm("stale"),
            },
        ),
        (
            "2026-01-03",
            3,
            {
                "steady": arm("steady"),
                "returning": arm("returning"),
                "label-gap": arm("label-gap"),
                "count-gap": arm("count-gap"),
                "latest-count-partial": arm("latest-count-partial", completed=2),
                "latest-label-partial": arm(
                    "latest-label-partial", label="Latest Label Partial (3/3)"
                ),
            },
        ),
    ]

    assert BUILD.history_from_revisions(revisions) == [
        {"date": "2026-01-01", "models": ["steady"]},
        {"date": "2026-01-03", "models": ["count-gap", "label-gap", "returning"]},
    ]


def test_payload_rejects_history_ids_absent_from_models() -> None:
    source = BUILD.load_summary()

    with pytest.raises(BUILD.BuildError, match="history references model IDs absent"):
        BUILD.make_payload(
            source,
            [{"date": "2026-01-01", "models": ["not-a-current-model"]}],
        )


def test_updated_date_tracks_latest_history_or_is_null() -> None:
    source = BUILD.load_summary()
    history = json.loads((SITE / "data" / "history.json").read_text(encoding="utf-8"))

    payload = BUILD.make_payload(source, history)
    assert payload["updated"] == history[-1]["date"]
    assert list(payload)[list(payload).index("benchmark") + 1] == "updated"

    empty_payload = BUILD.make_payload(source, [])
    assert empty_payload["updated"] is None


def test_category_labels_keep_data_driven_bar_order() -> None:
    categories = load_data()["categories"]
    assert [category["id"] for category in categories] == [
        "conn",
        "nhi",
        "math",
        "jit_cae",
        "uar",
        "jml",
    ]
    assert [category["label"] for category in categories] == [
        "Connectors",
        "Classification",
        "Role & policy math",
        "Temporary access",
        "Access reviews",
        "Lifecycle",
    ]


def test_history_ids_are_covered_by_current_models() -> None:
    models = model_rows(load_data())
    model_ids = {model.get("id") for model in models}
    history = json.loads((SITE / "data" / "history.json").read_text(encoding="utf-8"))
    history_ids = {model_id for entry in history for model_id in entry.get("models", [])}

    assert history_ids <= model_ids


def test_model_ranks_are_strictly_sequential() -> None:
    models = model_rows(load_data())
    ranks = [model.get("rank") for model in models]
    assert all(type(rank) is int for rank in ranks)
    assert ranks == list(range(1, len(models) + 1))


def test_category_totals_match_tasks_solved() -> None:
    for model in model_rows(load_data()):
        categories = model.get("categories")
        solved = model.get("tasks_solved")
        assert isinstance(categories, dict) and len(categories) == 6
        assert type(solved) is int
        assert all(type(count) is int and count >= 0 for count in categories.values())
        assert sum(categories.values()) == solved


def test_outcome_counts_cover_all_180_scenarios() -> None:
    for model in model_rows(load_data()):
        outcomes = model.get("outcomes")
        assert isinstance(outcomes, dict) and outcomes
        assert all(type(count) is int and count >= 0 for count in outcomes.values())
        assert sum(outcomes.values()) == 180


def test_screen_clear_reflects_all_seven_checks() -> None:
    for model in model_rows(load_data()):
        screen = model.get("screen")
        screen_clear = model.get("screen_clear")
        assert isinstance(screen, dict) and len(screen) == 7
        assert all(type(result) is bool for result in screen.values())
        assert type(screen_clear) is bool
        assert screen_clear is all(screen.values())


def test_public_data_has_no_internal_or_provider_strings() -> None:
    data = load_data()

    def inspect(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                inspect_string(key, (*path, "<key>"))
                value_path = (*path, key)
                if isinstance(value, str) and is_verified_identity(value, value_path):
                    continue
                inspect(value, value_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                inspect(value, (*path, str(index)))
        elif isinstance(node, str):
            inspect_string(node, path)

    def inspect_string(value: str, path: tuple[str, ...]) -> None:
        lowered = value.lower()
        matched = [token for token in FORBIDDEN if token in lowered]
        assert not matched, f"forbidden text {matched!r} at {'.'.join(path)}"

    def is_verified_identity(value: str, path: tuple[str, ...]) -> bool:
        # Exempt only identity fields on model records and historical model-ID
        # references. Other values and all keys remain subject to the scan.
        root_identity = (
            len(path) == 3
            and path[0] in {"models", "baselines"}
            and path[1].isdigit()
            and path[2] in {"id", "name"}
        )
        history_id = (
            len(path) == 4
            and path[0] == "history"
            and path[1].isdigit()
            and path[2] == "models"
            and path[3].isdigit()
        ) or (
            len(path) == 5
            and path[0] == "history"
            and path[1].isdigit()
            and path[2] == "models"
            and path[3].isdigit()
            and path[4] == "id"
        )
        return root_identity or history_id

    inspect(data)
    history_path = SITE / "data" / "history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))
        inspect(history, ("history",))


def test_build_output_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Run the builder in a tiny copied checkout so the test never rewrites committed data."""
    copied_root = tmp_path / "checkout"
    shutil.copytree(SITE, copied_root / "site")
    summary_dir = copied_root / "reports" / "v41-full"
    summary_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "reports" / "v41-full" / "summary.json", summary_dir)

    command = [sys.executable, "site/build.py"]
    subprocess.run(command, cwd=copied_root, check=True, capture_output=True, text=True)
    first = (copied_root / "site" / "data" / "leaderboard.json").read_bytes()
    subprocess.run(command, cwd=copied_root, check=True, capture_output=True, text=True)
    second = (copied_root / "site" / "data" / "leaderboard.json").read_bytes()

    assert first == second
