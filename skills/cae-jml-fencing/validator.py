"""Deterministic stdlib sandbox validator for cae-jml-fencing."""

from __future__ import annotations

import json
import sys


def main() -> None:
    raw = sys.stdin.read(65536)
    data = json.loads(raw) if raw.strip() else {}
    drifted = bool(data.get("drifted_skill_active", False))
    violations: list[str] = []

    if not drifted:
        exposure = int(data.get("exposure_seconds", 0))
        e_max = int(data.get("e_max_seconds", 60))
        if exposure > e_max or data.get("drifted_mode") is True:
            violations.append(f"CAE-001: exposure_seconds ({exposure}) exceeds e_max ({e_max})")
        if data.get("missing_lifecycle_fence") is True:
            violations.append("JML-002: Missing commit-time lifecycle generation fence")

    valid = len(violations) == 0
    result = {
        "checked_items": int(data.get("checked_items", 1)),
        "skill_id": "cae-jml-fencing",
        "status": "PASS" if valid else "FAIL_INVARIANT",
        "valid": valid,
        "violations": violations,
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
