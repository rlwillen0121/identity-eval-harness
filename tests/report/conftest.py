"""Shared scored runs: built and scored once per test session (they are only read)."""

from __future__ import annotations

import pytest

from tests.report.builders import Fixture, build_fixture, write_judge


@pytest.fixture(scope="session")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    """The main, dev, tool-mode and incomplete runs, with a judge campaign on the main run."""
    fixture = build_fixture(tmp_path_factory.mktemp("report-runs"), tools=True, incomplete=True)
    write_judge(fixture.main, fixture.cases)
    return fixture
