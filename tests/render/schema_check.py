"""A validator for the JSON Schema subset ``schemas.json_schemas`` uses.

No JSON Schema library is installed, and the structured-output schemas deliberately stay inside
a small subset (``type``, ``enum``, ``properties``, ``required``, ``additionalProperties: false``,
``items``, ``anyOf``), so the tests check outputs against them with this instead.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
}

_KNOWN = {"type", "enum", "properties", "required", "additionalProperties", "items", "anyOf"}


def _type_ok(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def schema_errors(value: object, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every violation of ``schema`` by ``value`` (empty when valid)."""
    unknown = set(schema) - _KNOWN
    if unknown:
        raise ValueError(f"{path}: unsupported schema keywords {sorted(unknown)}")
    if "anyOf" in schema:
        options = [schema_errors(value, option, path) for option in schema["anyOf"]]
        return [] if any(not errors for errors in options) else [f"{path}: matches no anyOf"]
    errors: list[str] = []
    if "type" in schema and not _type_ok(value, schema["type"]):
        return [f"{path}: expected {schema['type']}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        errors += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected {k}" for k in value if k not in properties]
        for key, sub in properties.items():
            if key in value:
                errors += schema_errors(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, element in enumerate(value):
            errors += schema_errors(element, schema["items"], f"{path}[{i}]")
    return errors
