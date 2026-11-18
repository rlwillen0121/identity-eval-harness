from __future__ import annotations

from typing import Any

import pytest
from scripts import check_reference_pyright as gate


def diagnostic(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": "src/identitybench/v5/runner.py",
        "severity": "error",
        "rule": "reportReturnType",
        "range": {
            "start": {"line": 2326, "character": 15},
            "end": {"line": 2326, "character": 35},
        },
        "message": 'Type "StrictStr" is not assignable to return type "UsageAccountingStatusV5"',
    }
    record.update(overrides)
    return record


def test_diagnostic_comparison_accepts_exact_records() -> None:
    expected = [diagnostic()]

    assert gate.diagnostic_difference(expected, [diagnostic()]) == ([], [])


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("path", "src/identitybench/v5/runtime.py"),
        ("rule", "reportArgumentType"),
        (
            "range",
            {"start": {"line": 2327, "character": 15}, "end": {"line": 2327, "character": 35}},
        ),
        ("message", 'Type "str" is not assignable to return type "UsageAccountingStatusV5"'),
    ],
)
def test_changed_diagnostic_is_rejected(field: str, changed_value: Any) -> None:
    expected = [diagnostic()]
    observed = [diagnostic(**{field: changed_value})]

    missing, unexpected = gate.diagnostic_difference(expected, observed)

    assert missing == expected
    assert unexpected == observed


def test_changed_added_and_removed_reference_sources_are_rejected() -> None:
    expected = {
        "src/identitybench/v5/runner.py": "a" * 64,
        "src/identitybench/v5/runtime.py": "b" * 64,
    }
    observed = {
        "src/identitybench/v5/runner.py": "c" * 64,
        "src/identitybench/v5/new_module.py": "d" * 64,
    }

    missing, added, changed = gate.source_hash_difference(expected, observed)

    assert missing == ["src/identitybench/v5/runtime.py"]
    assert added == ["src/identitybench/v5/new_module.py"]
    assert changed == ["src/identitybench/v5/runner.py"]
