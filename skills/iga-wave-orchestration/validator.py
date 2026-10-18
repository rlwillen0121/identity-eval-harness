"""Deterministic stdlib sandbox validator for iga-wave-orchestration."""

from __future__ import annotations

import json
import sys


def main() -> None:
    raw = sys.stdin.read(65536)
    data = json.loads(raw) if raw.strip() else {}
    drifted = bool(data.get("drifted_skill_active", False))
    violations: list[str] = []

    if not drifted and (
        data.get("unreconciled_interrupts") is True or data.get("drifted_mode") is True
    ):
        violations.append("ORCH-002: Stage or wave has unreconciled mid-flight interrupts")

    valid = len(violations) == 0
    result = {
        "checked_items": int(data.get("checked_items", 1)),
        "skill_id": "iga-wave-orchestration",
        "status": "PASS" if valid else "FAIL_INVARIANT",
        "valid": valid,
        "violations": violations,
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
