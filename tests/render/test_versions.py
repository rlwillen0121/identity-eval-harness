"""Version-bump enforcement for the constants that feed ``prompt_sha256``.

A run manifest identifies a prompt by ``render.prompt.prompt_sha256``, which hashes the variant
files plus three hand-bumped versions: ``TEMPLATE_VERSION`` (how variant files are parsed into
sections, system prompt assembly, the case message template, the few-shot turn format),
``SELECTION_VERSION`` (which worked examples are chosen, and the budget they must fit) and
``IDEAL_VERSION`` (the answers they show). A code change without a bump would let
one hash cover two different prompts, so this module fingerprints the code each version covers
and compares it with ``FINGERPRINTS``, a table keyed by version.

The fingerprint is the AST of the covered top-level definitions (without positions) with
docstrings, bare string statements and imports removed, so comments, docstrings and formatting
can change freely, while any change to code, including constants, needs a bump. Fields left at
their empty value (None or an empty list) are left out of the canonical form, so the
fingerprint doesn't depend on the Python version: new releases add optional AST fields (3.13
added ``TypeVar.default_value``), and an unused one is always empty.

When the test fails: bump the named constant, then add the new version with the digest the
failure prints. Keep the old entries; the table is the record of what each version meant.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from collections.abc import Iterable
from types import ModuleType

import pytest

from identitybench.render import fewshot as fewshot_module
from identitybench.render import prompt as prompt_module
from identitybench.render import variants as variants_module
from identitybench.render import view as view_module
from identitybench.tasks import ideal as ideal_module

Covered = tuple[ModuleType, frozenset[str] | None, frozenset[str]]
"""(module, top-level names to include (None: all), top-level names to leave out)."""

COVERAGE: dict[str, tuple[Covered, ...]] = {
    "TEMPLATE_VERSION": (
        (
            variants_module,
            frozenset({"_FRONT_MATTER", "_MARKER", "_parse", "_sections", "_per_task", "_load"}),
            frozenset(),
        ),
        (
            view_module,
            None,
            frozenset(
                {
                    "parse_view",
                    "_blocks",
                    "_preamble_value",
                    "_org_name",
                    "_FIELD_BY_HEADING",
                    "TemplateError",
                    "__all__",
                }
            ),
        ),
        (
            prompt_module,
            frozenset({"build_system", "_example_turns", "build_request"}),
            frozenset(),
        ),
        (fewshot_module, frozenset({"example_turns"}), frozenset()),
    ),
    "SELECTION_VERSION": (
        (fewshot_module, None, frozenset({"SELECTION_VERSION", "example_turns", "__all__"})),
        (prompt_module, frozenset({"_check_budget"}), frozenset()),
    ),
    "IDEAL_VERSION": ((ideal_module, None, frozenset({"IDEAL_VERSION", "__all__"})),),
}
"""What each version constant covers: the code that decides the bytes of a prompt, and nothing
else, so a bump always means a prompt can differ. ``TEMPLATE_VERSION`` covers the parsing in
``render.variants`` that turns a variant file into the sent sections (the front matter and
marker patterns, ``_parse``, which decides where the body starts and passes it on, section
extraction and stripping, the per-task override of a shared section, and which file supplies
the role and output format), the rendering half of ``render.view`` (not ``parse_view`` and its
helpers, which read messages back and send nothing), and the functions of ``render.prompt``
that assemble a request. Finding and reading a variant file and the checks that only reject
one (``variants._read``), ``prompt_sha256`` and the example checks change no prompt and aren't
covered.
``SELECTION_VERSION`` also covers ``render.prompt._check_budget``, which enforces its caps."""

OWNERS: dict[str, ModuleType] = {
    "TEMPLATE_VERSION": prompt_module,
    "SELECTION_VERSION": fewshot_module,
    "IDEAL_VERSION": ideal_module,
}
"""The module that defines each constant."""

FINGERPRINTS: dict[str, dict[object, str]] = {
    "TEMPLATE_VERSION": {
        "context-2": "1d6486ab050350b08ad8f4ec0c2163e005ebf30dc9bab556cfaf11939f50d66f",
    },
    "SELECTION_VERSION": {
        3: "cd6e4d6ebf4bf48174de599f451c1c3756da66ebb37758e1ee7d0a028185154f",
        4: "54945a73ce3af479a2a78907bc6e9078ceaf7a3e718960009b72eac5fd679d09",
        5: "d20d24a894ab4426ae1506afbb70565496ec07b71b2a67cb935eabc13d4c81cb",
    },
    "IDEAL_VERSION": {
        1: "3aae94070df3631d19cede3e671466ddcf93005001f996bc6cfb661040f4ef72",
    },
}
"""Constant -> version -> fingerprint of the covered code at that version. Every entry uses the
version-independent canonical form; the entries recorded before it existed were recomputed
(``SELECTION_VERSION`` 3 from its code, with the coverage it had then). An entry is re-recorded
without a bump only when the coverage changes and every prompt stays byte-identical:
``context-2`` was, when ``variants._parse`` was split out of ``_read`` and covered. Version 5
of ``SELECTION_VERSION`` raised the review cap to 340k and reworded the budget error."""


def _name(node: ast.stmt) -> str | None:
    """The name a top-level statement defines, if it defines exactly one."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


class _StripDocs(ast.NodeTransformer):
    """Removes bare string statements (docstrings, attribute docstrings) from every body."""

    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        body = getattr(node, "body", None)
        if isinstance(body, list):
            kept = [
                stmt
                for stmt in body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            # A body can't be empty; a lone docstring becomes ``pass``.
            setattr(node, "body", kept or [ast.Pass()])  # noqa: B010
        return node


def _canonical(value: object) -> str:
    """A text form of an AST value that is the same on every supported Python version: node
    type and every field that isn't empty (None or an empty list), recursively. Written out
    rather than ``ast.dump``, whose handling of empty fields changed in Python 3.13; leaving
    empty fields out makes optional fields added by a later Python invisible until code uses
    them (see the module docstring). Naming each kept field keeps two nodes that differ only in
    which field is set apart."""
    if isinstance(value, ast.AST):
        fields = ", ".join(
            f"{k}={_canonical(v)}" for k, v in ast.iter_fields(value) if v is not None and v != []
        )
        return f"{type(value).__name__}({fields})"
    if isinstance(value, list):
        return f"[{', '.join(_canonical(v) for v in value)}]"
    return repr(value)


def fingerprint_source(
    source: str, include: frozenset[str] | None = None, exclude: Iterable[str] = ()
) -> str:
    """The normalized AST dump of ``source``'s top-level definitions (see the module
    docstring), restricted to ``include`` when given and without ``exclude``."""
    tree = _StripDocs().visit(ast.parse(source))
    assert isinstance(tree, ast.Module)
    excluded = set(exclude)
    kept: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom | ast.Pass):
            continue
        name = _name(node)
        if include is not None and name not in include:
            continue
        if name in excluded:
            continue
        kept.append(_canonical(node))
    if include is not None:
        found = {_name(node) for node in tree.body}
        missing = include - found
        assert not missing, f"covered definitions not found: {sorted(missing)}"
    return "\n".join(kept)


def fingerprint(constant: str) -> str:
    """Hex sha256 of everything ``constant`` covers, in ``COVERAGE`` order."""
    digest = hashlib.sha256()
    for module, include, exclude in COVERAGE[constant]:
        digest.update(module.__name__.encode())
        digest.update(b"\x1f")
        digest.update(fingerprint_source(inspect.getsource(module), include, exclude).encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


@pytest.mark.parametrize("constant", list(COVERAGE))
def test_covered_code_matches_its_version(constant: str) -> None:
    owner = OWNERS[constant]
    version = getattr(owner, constant)
    actual = fingerprint(constant)
    recorded = FINGERPRINTS[constant].get(version)
    where = f"{owner.__name__}.{constant}"
    if recorded is None:
        pytest.fail(
            f"{where} = {version!r} has no recorded fingerprint. If this is a new version, add "
            f"{version!r}: {actual!r} to FINGERPRINTS[{constant!r}] in {__name__}."
        )
    assert actual == recorded, (
        f"code covered by {where} changed without a version bump. Bump {constant} (now "
        f"{version!r}), then add the new version with fingerprint {actual!r} to "
        f"FINGERPRINTS[{constant!r}] in {__name__}."
    )


def test_every_version_constant_is_covered_and_owned() -> None:
    assert set(COVERAGE) == set(OWNERS) == set(FINGERPRINTS)
    assert prompt_module.SELECTION_VERSION == fewshot_module.SELECTION_VERSION
    assert prompt_module.IDEAL_VERSION == ideal_module.IDEAL_VERSION


BASE = '''
"""Module docstring."""
import json

VERSION = 1
"""Attribute docstring."""

LIMIT = 10


def render(x):
    """Doc."""
    # a comment
    return json.dumps(x)[:LIMIT]
'''


@pytest.mark.parametrize(
    "edited",
    [
        BASE.replace('"""Doc."""', '"""Another doc, longer."""'),
        BASE.replace("# a comment", "# another comment"),
        BASE.replace("import json", "import json\nimport os"),
        BASE.replace(
            "return json.dumps(x)[:LIMIT]", "return json.dumps(\n        x\n    )[:LIMIT]"
        ),
        BASE.replace("VERSION = 1", "VERSION = 2"),
    ],
)
def test_docs_comments_imports_formatting_and_the_version_do_not_count(edited: str) -> None:
    assert fingerprint_source(edited, exclude={"VERSION"}) == fingerprint_source(
        BASE, exclude={"VERSION"}
    )


@pytest.mark.parametrize(
    "edited",
    [
        BASE.replace("LIMIT = 10", "LIMIT = 11"),
        BASE.replace("[:LIMIT]", "[: LIMIT - 1]"),
        BASE + "\n\ndef helper():\n    return 1\n",
    ],
)
def test_code_and_constant_changes_count(edited: str) -> None:
    assert fingerprint_source(edited, exclude={"VERSION"}) != fingerprint_source(
        BASE, exclude={"VERSION"}
    )


def _variants_template_fingerprint(source: str) -> str:
    """The ``TEMPLATE_VERSION`` fingerprint of ``source`` standing in for ``render.variants``."""
    ((_, include, exclude),) = [c for c in COVERAGE["TEMPLATE_VERSION"] if c[0] is variants_module]
    return fingerprint_source(source, include, exclude)


@pytest.mark.parametrize(
    ("before", "after", "counts"),
    [
        # Reshaping the body before it is split into sections changes what is sent.
        ("text[match.end() :]", "text[match.end() :].expandtabs(4)", True),
        ("_FRONT_MATTER.match(text)", "_FRONT_MATTER.match(text.lstrip())", True),
        # Reading and rejecting a file can't change what a loadable file sends.
        ("if not path.is_file():", "if not path.exists():", False),
        ("!= file stem", "differs from the file stem", False),
    ],
)
def test_variant_parsing_counts_and_file_checks_do_not(
    before: str, after: str, counts: bool
) -> None:
    """``_parse`` decides which bytes of a variant file reach its sections, so editing it needs
    a ``TEMPLATE_VERSION`` bump; ``_read``'s lookup and checks don't."""
    source = inspect.getsource(variants_module)
    assert source.count(before) == 1, before
    edited = source.replace(before, after)
    changed = _variants_template_fingerprint(edited) != _variants_template_fingerprint(source)
    assert changed is counts


def test_include_restricts_and_requires_the_names() -> None:
    only = frozenset({"render"})
    assert fingerprint_source(BASE.replace("LIMIT = 10", "LIMIT = 9"), only) == (
        fingerprint_source(BASE, only)
    )
    with pytest.raises(AssertionError, match="not found"):
        fingerprint_source(BASE, frozenset({"missing"}))


PEP_695 = """
def first[T: (int, str)](values: list[T]) -> T:
    return values[0]


class Box[T]:
    item: T


type Pair[K, V] = tuple[K, V]
"""

PEP_695_FINGERPRINT = "858b6d88506cdc340d312b072c6e6ccd7000a1cf7512e5f048e1d238984fc81c"
"""``fingerprint_source(PEP_695)`` hashed, as recorded on Python 3.12 and checked to be the same
on 3.13 and 3.14."""


def test_fingerprint_is_the_same_on_every_python_version() -> None:
    """Type parameters are where Python 3.13 added an AST field (``default_value``); a canonical
    form that wrote every field gave this snippet a different digest there."""
    digest = hashlib.sha256(fingerprint_source(PEP_695).encode()).hexdigest()
    assert digest == PEP_695_FINGERPRINT


def _type_var(fields: tuple[str, ...], **values: object) -> ast.AST:
    """A stand-in ``TypeVar`` node whose type has the field list ``fields``, as one Python
    version or another defines it, with ``values`` set."""
    node = type("TypeVar", (ast.AST,), {"_fields": fields})()
    for field, value in values.items():
        setattr(node, field, value)
    return node


def _int() -> ast.Name:
    return ast.Name(id="int", ctx=ast.Load())


def test_an_unset_field_added_by_a_later_python_does_not_count() -> None:
    old = _type_var(("name", "bound"), name="T", bound=None)
    new = _type_var(("name", "bound", "default_value"), name="T", bound=None, default_value=None)
    assert _canonical(old) == _canonical(new)
    used = _type_var(("name", "bound", "default_value"), name="T", bound=None, default_value=_int())
    assert _canonical(used) != _canonical(old)


def test_which_field_is_set_counts() -> None:
    fields = ("name", "bound", "default_value")
    bound = _type_var(fields, name="T", bound=_int(), default_value=None)
    default = _type_var(fields, name="T", bound=None, default_value=_int())
    assert _canonical(bound) != _canonical(default)
