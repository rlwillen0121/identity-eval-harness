"""Export ordered public v5 mutation type metadata for the native Rust validator.

Development-time compatibility asset only. No episodes, gold outcomes, or sealed artifacts
are read; the Rust executable embeds the generated JSON and never imports Python.
"""

import json
import types
from pathlib import Path
from typing import Annotated, Literal, Union, get_args, get_origin

from identitybench.v5.schemas import AtomicMutationV5, StrictV5Model


def generate() -> dict:
    models: dict[str, dict] = {}

    def node(annotation):
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is Annotated:
            return node(args[0])
        if annotation is type(None):
            return {"kind": "none"}
        if annotation in (str, bool, int):
            return {"kind": {str: "string", bool: "bool", int: "int"}[annotation]}
        if origin is Literal:
            return {"kind": "literal", "values": list(args)}
        if origin in (Union, types.UnionType):
            return {"kind": "union", "members": [node(arg) for arg in args]}
        if origin is tuple:
            variadic = len(args) == 2 and args[1] is Ellipsis
            return {
                "kind": "tuple",
                "items": [node(arg) for arg in (args[:1] if variadic else args)],
                "variadic": variadic,
            }
        if isinstance(annotation, type) and issubclass(annotation, StrictV5Model):
            name = annotation.__name__
            if name not in models:
                # Register before descending so self-references cannot recurse forever.
                models[name] = {"name": name, "fields": []}
                models[name]["fields"] = [
                    {"name": field_name, "annotation": node(field.annotation)}
                    for field_name, field in annotation.model_fields.items()
                ]
            return {"kind": "model", "name": name}
        raise TypeError(f"unsupported public annotation: {annotation!r}")

    root = node(AtomicMutationV5)
    assert root["kind"] == "union"
    assert all(member["kind"] == "model" for member in root["members"])
    variants = []
    for member in root["members"]:
        discriminator = next(
            field["annotation"]
            for field in models[member["name"]]["fields"]
            if field["name"] == "mutation_kind"
        )
        assert discriminator["kind"] == "literal" and len(discriminator["values"]) == 1
        variants.append({"model": member["name"], "mutation_kind": discriminator["values"][0]})
    return {
        "schema": "identitybench.v5.atomic_mutation_annotations.v1",
        "variants": variants,
        "models": list(models.values()),
    }


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[2] / "src/mutation_annotation_v5.json"
    target.write_text(json.dumps(generate(), indent=2) + "\n", encoding="utf-8")
