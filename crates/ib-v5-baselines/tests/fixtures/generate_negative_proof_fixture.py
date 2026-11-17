#!/usr/bin/env python3
"""Build the Python Draft5 dev proof fixture with hash-randomized reprs canonicalized.

Only the two `OPT-V5-01` footprint mappings and their `frozenset` values are sorted.
That affects exactly three strings in the projection: the witness setup evidence and
the two attribution aliases. The restricted AST reader below accepts Python literals
only; it never evaluates code.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FrozenSetLiteral:
    items: tuple[Any, ...]


@dataclass(frozen=True)
class SetLiteral:
    items: tuple[Any, ...]


def parse_literal(node: ast.AST) -> Any:
    """Parse only the literal nodes emitted by Python's `repr` for this evidence."""
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (bool, int, float, str)):
            return node.value
        raise ValueError(f"unsupported literal constant: {type(node.value).__name__}")
    if isinstance(node, ast.Tuple):
        return tuple(parse_literal(item) for item in node.elts)
    if isinstance(node, ast.List):
        return [parse_literal(item) for item in node.elts]
    if isinstance(node, ast.Set):
        return SetLiteral(tuple(parse_literal(item) for item in node.elts))
    if isinstance(node, ast.Dict):
        return {
            parse_literal(key): parse_literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        items = parse_literal(node.args[0])
        if isinstance(items, SetLiteral):
            return FrozenSetLiteral(items.items)
        if isinstance(items, (tuple, list)):
            return FrozenSetLiteral(tuple(items))
    raise ValueError(f"unsupported evidence literal node: {type(node).__name__}")


def sort_key(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(sort_key(item) for item in value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise ValueError(f"unsupported sort key type: {type(value).__name__}")


def repr_literal(value: Any, *, sort_frozensets: bool = False) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{repr_literal(key)}: {repr_literal(item, sort_frozensets=sort_frozensets)}"
            for key, item in value.items()
        ) + "}"
    if isinstance(value, tuple):
        items = ", ".join(repr_literal(item, sort_frozensets=sort_frozensets) for item in value)
        if len(value) == 1:
            items += ","
        return f"({items})"
    if isinstance(value, list):
        return "[" + ", ".join(
            repr_literal(item, sort_frozensets=sort_frozensets) for item in value
        ) + "]"
    if isinstance(value, SetLiteral):
        if not value.items:
            return "set()"
        items = value.items
        return "{" + ", ".join(
            repr_literal(item, sort_frozensets=sort_frozensets) for item in items
        ) + "}"
    if isinstance(value, FrozenSetLiteral):
        if not value.items:
            return "frozenset()"
        items = (
            sorted(value.items, key=sort_key) if sort_frozensets else value.items
        )
        rendered = ", ".join(
            repr_literal(item, sort_frozensets=sort_frozensets) for item in items
        )
        return f"frozenset({{{rendered}}})"
    return repr(value)


def canonicalize_opt01_setup(evidence: str) -> str:
    marker = "details="
    if marker not in evidence:
        raise ValueError("OPT-V5-01 setup evidence is missing details")
    prefix, raw_details = evidence.rsplit(marker, 1)
    tree = ast.parse(raw_details, mode="eval")
    details = parse_literal(tree.body)
    if not isinstance(details, dict):
        raise ValueError("optimizer details must be a mapping")
    required = {"post_footprints", "post_account_footprints"}
    if not required <= details.keys():
        raise ValueError("optimizer details lack the two footprint mappings")

    # Preserve outer details insertion order and all other values. Sort only the
    # two unordered footprint maps and the frozensets directly inside them.
    rendered_items = []
    for key, value in details.items():
        if key in required:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a mapping")
            value_text = "{" + ", ".join(
                f"{repr_literal(item_key)}: {repr_literal(value[item_key], sort_frozensets=True)}"
                for item_key in sorted(value)
            ) + "}"
        else:
            value_text = repr_literal(value)
        rendered_items.append(f"{repr_literal(key)}: {value_text}")
    return prefix + marker + "{" + ", ".join(rendered_items) + "}"


def canonicalize_attribution(value: str) -> str:
    parts = value.split(" | ", 3)
    if len(parts) != 4:
        raise ValueError("unexpected RuleBoundary attribution format")
    parts[2] = canonicalize_opt01_setup(parts[2])
    return " | ".join(parts)


def canonicalize_projection(projection: dict[str, Any]) -> int:
    witnesses = [
        item for item in projection["rule_witnesses"] if item.get("rule_id") == "OPT-V5-01"
    ]
    if len(witnesses) != 1:
        raise ValueError("expected exactly one OPT-V5-01 witness")
    witnesses[0]["setup_evidence"] = canonicalize_opt01_setup(witnesses[0]["setup_evidence"])
    changed = 1
    for field in ("rule_boundary_attribution", "actual_rule_boundary_attribution"):
        attribution = projection[field].get("OPT-V5-01")
        if not isinstance(attribution, list) or len(attribution) != 1:
            raise ValueError(f"expected one {field} value for OPT-V5-01")
        attribution[0] = canonicalize_attribution(attribution[0])
        changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python_report", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("negative_proof_python_draft5_dev.json"),
    )
    args = parser.parse_args()
    report = json.loads(args.python_report.read_text())
    gate = next(
        item
        for item in report["gate_results"]
        if item.get("gate_id") == "NEGATIVE-MUTATION-V5"
    )
    projection = gate["evidence"]["directed_boundary_proof"]
    changed_values = canonicalize_projection(projection)
    args.output.write_text(json.dumps(projection, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.output}; canonicalized exactly {changed_values} OPT-V5-01 strings")


if __name__ == "__main__":
    main()
