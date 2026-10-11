"""Prompt variants: versioned markdown files in ``prompts/`` (SPEC §11.2).

A variant file is markdown with a YAML front matter and marked sections. Only the sections are
sent to a model; everything outside them documents the variant for humans::

    ---
    name: checklist        # must equal the file stem
    version: 1             # bump on any change to the sent text
    extends: baseline      # optional: take role and output format from this variant
    fewshot: 0             # worked examples the variant expects (fewshot-3: 3)
    ---
    <!-- begin: additions:access_request -->
    ...text sent to the model...
    <!-- end: additions:access_request -->

Section names: ``role``, ``output:<task>`` and ``additions`` or ``additions:<task>`` (a plain
``additions`` applies to every task; a task-specific one replaces it for that task).

**Variants differ only by their additions.** SPEC §11.2 makes the policy text identical across
variants; going further, a variant that ``extends`` another must not define ``role`` or
``output`` sections, so the role, policy and output format are byte-identical and the measured
effect of a variant is the effect of its additions alone. Only a root variant (one that extends
nothing) defines the role and an output format for every task, and it may not extend further.

The parsing (``_parse``: the front matter and marker patterns, where the body starts and how it
splits into sections; the per-task override; which file supplies the role and output format)
decides the text that is sent, so it is versioned by ``render.prompt.TEMPLATE_VERSION``. Finding
and reading the file and the checks that only reject one (``_read``'s name, key and section-name
checks) can't change the text of a variant that loads, and aren't.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml

from identitybench.schemas.enums import Task

__all__ = [
    "PACKAGE_PROMPTS_DIR",
    "SOURCE_PROMPTS_DIR",
    "PromptVariant",
    "VariantError",
    "default_prompts_dir",
    "load_variant",
]

PACKAGE_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "_prompts"
"""``identitybench/_prompts``: where an installed wheel carries ``prompts/`` as package data
(the wheel build force-includes the repository's ``prompts/`` there). It is next to the imported
package, so an editable checkout, whose package is ``src/identitybench``, never has one and
can't pick up a stale copy."""

SOURCE_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
"""The repository checkout's ``prompts/`` directory, for an editable install."""

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_MARKER = re.compile(r"^<!-- (begin|end): ([a-z_:]+) -->$", re.MULTILINE)
_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
_KEYS = frozenset({"name", "version", "extends", "fewshot"})


class VariantError(ValueError):
    """A malformed or inconsistent prompt variant file."""


@dataclass(frozen=True)
class PromptVariant:
    """A loaded prompt variant, with its root variant's role and output format resolved."""

    name: str
    version: int
    role: str
    outputs: Mapping[Task, str]
    """The output-format instructions for each task."""
    additions: Mapping[Task, str]
    """This variant's own additions per task; a task without additions is absent."""
    fewshot: int
    """How many worked examples ``build_request`` must be given (0 for most variants)."""
    files: tuple[tuple[str, str], ...]
    """(file name, full file text) for every file the variant was built from, root first.
    ``prompt_sha256`` hashes these, so editing any of them changes the hash."""


def _sections(name: str, body: str) -> dict[str, str]:
    """Section name -> stripped text, checking that markers pair up and don't nest."""
    sections: dict[str, str] = {}
    open_name: str | None = None
    start = 0
    for marker in _MARKER.finditer(body):
        kind, section = marker.group(1), marker.group(2)
        if kind == "begin":
            if open_name is not None:
                raise VariantError(f"{name}: section {section!r} opens inside {open_name!r}")
            if section in sections:
                raise VariantError(f"{name}: duplicate section {section!r}")
            open_name, start = section, marker.end()
        else:
            if section != open_name:
                raise VariantError(f"{name}: 'end: {section}' doesn't close {open_name!r}")
            text = body[start : marker.start()].strip()
            if not text:
                raise VariantError(f"{name}: section {section!r} is empty")
            sections[section] = text
            open_name = None
    if open_name is not None:
        raise VariantError(f"{name}: section {open_name!r} is never closed")
    return sections


def _per_task(sections: Mapping[str, str], prefix: str) -> dict[Task, str]:
    """``prefix:<task>`` sections by task, with a bare ``prefix`` section as the default."""
    shared = sections.get(prefix)
    found: dict[Task, str] = {}
    for task in Task:
        text = sections.get(f"{prefix}:{task.value}", shared)
        if text is not None:
            found[task] = text
    return found


def _check_section_names(name: str, sections: Mapping[str, str]) -> None:
    allowed = {"role", "additions"}
    allowed |= {f"{prefix}:{task.value}" for prefix in ("output", "additions") for task in Task}
    unknown = sorted(set(sections) - allowed)
    if unknown:
        raise VariantError(f"{name}: unknown sections {unknown}")


def _parse(name: str, text: str) -> tuple[dict[str, object], dict[str, str]]:
    """(front matter, sections) of a variant file's text. Everything that decides which bytes
    reach a section happens here (where the front matter ends and the body begins, and how the
    body splits into sections), so ``render.prompt.TEMPLATE_VERSION`` covers it: a change that
    reshaped the body would send different sections under an unchanged file hash."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise VariantError(f"{name}: missing YAML front matter")
    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise VariantError(f"{name}: front matter must be a mapping")
    front = {str(k): v for k, v in cast(dict[object, object], meta).items()}
    return front, _sections(name, text[match.end() :])


def _read(name: str, prompts_dir: Path) -> tuple[dict[str, object], dict[str, str], str, str]:
    """(front matter, sections, file name, file text) of one variant file: finding and reading
    it, ``_parse``, and checks that reject a file without changing what it sends."""
    if not _NAME.match(name):
        raise VariantError(f"invalid variant name {name!r}")
    path = prompts_dir / f"{name}.md"
    if not path.is_file():
        raise VariantError(f"no prompt variant {name!r} in {prompts_dir}")
    text = path.read_text(encoding="utf-8")
    front, sections = _parse(name, text)
    unknown = sorted(set(front) - _KEYS)
    if unknown:
        raise VariantError(f"{name}: unknown front matter keys {unknown}")
    if front.get("name") != name:
        raise VariantError(f"{name}: front matter name {front.get('name')!r} != file stem")
    _check_section_names(name, sections)
    return front, sections, path.name, text


def _int(name: str, front: Mapping[str, object], key: str, default: int | None) -> int:
    value = front.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VariantError(f"{name}: {key} must be a non-negative integer, got {value!r}")
    return value


@lru_cache(maxsize=32)
def _load(name: str, prompts_dir: Path) -> PromptVariant:
    front, sections, file_name, text = _read(name, prompts_dir)
    files = [(file_name, text)]
    base_name = front.get("extends")
    if base_name is None:
        root_sections = sections
    else:
        if not isinstance(base_name, str):
            raise VariantError(f"{name}: extends must be a variant name")
        if {"role"} & set(sections) or any(s.startswith("output") for s in sections):
            raise VariantError(f"{name}: a variant that extends another defines additions only")
        base_front, root_sections, base_file, base_text = _read(base_name, prompts_dir)
        if base_front.get("extends") is not None:
            raise VariantError(f"{name}: base {base_name!r} must not extend another variant")
        files.insert(0, (base_file, base_text))
    role = root_sections.get("role")
    outputs = _per_task(root_sections, "output")
    if role is None or set(outputs) != set(Task):
        raise VariantError(f"{name}: the root variant needs a role and an output for every task")
    return PromptVariant(
        name=name,
        version=_int(name, front, "version", None),
        role=role,
        outputs=MappingProxyType(outputs),
        additions=MappingProxyType(_per_task(sections, "additions")),
        fewshot=_int(name, front, "fewshot", 0),
        files=tuple(files),
    )


def default_prompts_dir() -> Path:
    """The directory variant names are looked up in: the package data
    (``PACKAGE_PROMPTS_DIR``) when it exists, else the checkout's ``prompts/``
    (``SOURCE_PROMPTS_DIR``). Package data comes first because an installed wheel's files are
    the ones its code was released with.

    Raises ``VariantError`` naming both locations when neither exists.
    """
    for directory in (PACKAGE_PROMPTS_DIR, SOURCE_PROMPTS_DIR):
        if directory.is_dir():
            return directory
    raise VariantError(
        f"no prompts directory: neither the package data at {PACKAGE_PROMPTS_DIR} nor the "
        f"checkout's {SOURCE_PROMPTS_DIR} exists; pass prompts_dir explicitly"
    )


def load_variant(name: str, prompts_dir: Path | str | None = None) -> PromptVariant:
    """Load the prompt variant ``name`` (a file stem) from ``prompts_dir`` (default
    ``default_prompts_dir()``). Loaded variants are cached per (name, directory) for the
    process, so one run can't mix two versions of a file.

    Raises ``VariantError`` for a missing or malformed file, or when no prompts directory is
    found.
    """
    directory = Path(prompts_dir) if prompts_dir is not None else default_prompts_dir()
    return _load(name, directory.resolve())
