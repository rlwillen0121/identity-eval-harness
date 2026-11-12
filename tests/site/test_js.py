"""Run the site's JavaScript tests when Node.js and test files are available."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS_TESTS = ROOT / "tests" / "site" / "js"


def test_site_javascript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    files = sorted(str(path.relative_to(ROOT)) for path in JS_TESTS.glob("*.test.mjs"))
    if not files:
        pytest.skip("no site JavaScript *.test.mjs files are present")

    result = subprocess.run(
        [node, "--test", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"node --test {' '.join(files)} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
