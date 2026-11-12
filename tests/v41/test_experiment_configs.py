"""Committed v4.1 configs contain portable references, never endpoints or credentials."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def committed_configs() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "experiments/v41-*.yaml"],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    return [REPO / name for name in result.stdout.decode().split("\0") if name]


@pytest.mark.parametrize("path", committed_configs(), ids=lambda p: p.name)
def test_no_endpoints_or_secrets_in_committed_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "://" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "localhost" not in text.lower()
    assert not re.search(r"\b(sk|key)-[A-Za-z0-9]{8,}", text)


def test_local_configs_are_gitignored() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "experiments/local/" in ignored
