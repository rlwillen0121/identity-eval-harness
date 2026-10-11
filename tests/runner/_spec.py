"""The SPEC §16 example experiment config, read verbatim from SPEC.md."""

from __future__ import annotations

import re
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[2] / "SPEC.md"


def spec_example() -> str:
    """The example experiment config, read verbatim from SPEC §16 (not a hand-edited copy)."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    after = text.split("Example experiment config:", 1)[1]
    match = re.search(r"```yaml\n(.*?)```", after, flags=re.DOTALL)
    assert match is not None, "SPEC §16 example config block not found"
    return match.group(1)
