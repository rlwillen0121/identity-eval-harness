"""Oracle-decided cases and a toy repo for the CLI and audit tests.

Cases come from the conformance scenarios (``tests/conformance``): realistic views the real
oracle decides, covering every task and several tiers, with no world generator involved. IDs
and provenance are derived from the scenario ID by hashing, so they are distinctive strings a
blindness test can search for, and the same on every run. The provenance keys are ones the
shortcut probe classifies (``probe.features``: a scenario, a justification family), so the
toy dataset is one ``identitybench validate`` and a ``surface_probe`` baseline can read.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from identitybench.policy.oracle import decide
from identitybench.runner.dataset import suite_file, write_jsonl
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Split, Suite, Task
from identitybench.tasks.tiers import compute_tags, compute_tier
from tests.conformance.loader import ErrorExpect, SimulationExpect, load_all

SUITES: dict[Task, Suite] = {
    Task.access_request: Suite.requests,
    Task.access_review: Suite.reviews,
    Task.jml: Suite.jml,
}
DATASET = Path("datasets") / "identitybench-v1"
PYPROJECT = '[project]\nname = "identitybench"\n'


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@functools.cache
def oracle_cases() -> tuple[Case, ...]:
    """Every decided conformance scenario as a test-split case with real gold, tier and tags."""
    cases: list[Case] = []
    for scenario in load_all():
        if isinstance(scenario.expect, ErrorExpect | SimulationExpect):
            continue
        gold = decide(scenario.view, scenario.task)
        h = _digest(scenario.id)
        cases.append(
            Case(
                case_id=f"case_{h[:14]}",
                task=scenario.task,
                suite=SUITES[scenario.task],
                split=Split.test,
                world_id=f"world_{h[14:26]}",
                family_id=f"fam_{h[26:40]}",
                tier=compute_tier(scenario.view, gold, scenario.task),
                tags=compute_tags(scenario.view, gold, scenario.task),
                view=scenario.view,
                gold=gold,
                provenance={
                    "scenario": f"tmpl_{h[40:54]}",
                    "justification_family": f"txt_{h[54:]}",
                },
            )
        )
    return tuple(sorted(cases, key=lambda c: c.case_id))


def write_dataset(directory: Path, cases: tuple[Case, ...] | None = None) -> Path:
    """Write ``cases`` (default: every oracle case) as a dataset's suite files."""
    chosen = oracle_cases() if cases is None else cases
    for suite in Suite:
        members = [c for c in chosen if c.suite is suite]
        if members:
            write_jsonl(suite_file(directory, suite, members[0].split), members)
    return directory


def make_repo(root: Path) -> Path:
    """A minimal repo: a ``pyproject.toml`` declaring the project, and the toy dataset."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    write_dataset(root / DATASET)
    return root
