"""The judge's frozen instructions: the rubric and the system prompt (SPEC §13.2, §13.3).

Both live as versioned files under ``prompts/``: ``judge_rubric.md`` (the four dimensions, with a
written anchor and an example for every score) and ``judge_system.md`` (untrusted-content
handling, the scoring procedure and the JSON output). The judge's system prompt is the two
files joined, and ``Rubric.sha256`` hashes exactly that prompt together with
``JUDGE_REQUEST_FORMAT``. ``Rubric.rubric_file_sha256`` hashes the rubric part of that prompt
alone: ``judge_rubric.md`` with trailing whitespace stripped, the same normalization the prompt
applies, so an editor adding a final newline changes neither hash.

Why hash: SPEC §13.3 freezes the rubric before validation. A kappa gate is a statement about one
rubric text, so every judge score records the hash it was produced under, validation refuses to
mix hashes, and ``load_rubric(expected_sha256=...)`` lets a production run fail before calling
any judge if the rubric was edited after it was validated.

Why two hashes: the human labels (SPEC §13.3) are made under ``judge_rubric.md`` only, while the
judge also gets ``judge_system.md`` and the request layout. SPEC §13.3 lets the judge prompt be
developed on the dev labels, which changes ``sha256`` but leaves the labels valid; an edit to
the rubric file changes what a score means and invalidates them. So labels record
``rubric_file_sha256``, and validation requires the judges' scores to carry the same one.

``parse_rubric`` enforces the structure SPEC §13.2 asks of the rubric file, so an anchor that
goes missing in an edit is caught at load time rather than silently weakening the judge.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from identitybench.render import variants as _variants

__all__ = [
    "DIMENSIONS",
    "DIMENSION_TITLES",
    "JUDGE_REQUEST_FORMAT",
    "RUBRIC_FILE",
    "SCORES",
    "SOURCE_PROMPTS_DIR",
    "SYSTEM_FILE",
    "Rubric",
    "RubricError",
    "default_prompts_dir",
    "default_rubric",
    "load_rubric",
    "parse_rubric",
    "rubric_sha256",
]

DIMENSIONS: tuple[str, ...] = (
    "faithfulness",
    "decisive_coverage",
    "actionability",
    "auditability",
)
"""The four rubric dimensions, in the order every report lists them (SPEC §13.2)."""

DIMENSION_TITLES: Mapping[str, str] = MappingProxyType(
    {
        "faithfulness": "Faithfulness",
        "decisive_coverage": "Decisive coverage",
        "actionability": "Actionability",
        "auditability": "Auditability",
    }
)
"""Section heading of each dimension in ``judge_rubric.md``."""

SCORES: tuple[int, ...] = (1, 2, 3, 4)

JUDGE_REQUEST_FORMAT = 1
"""Version of the user-message layout built by ``judge.run.build_judge_request``. It is part of
the rubric hash because the layout also shapes what the judge does; bump it whenever that
layout changes, so scores from the old and new layouts can't be mixed in one validation."""

RUBRIC_FILE = "judge_rubric.md"
SYSTEM_FILE = "judge_system.md"

SOURCE_PROMPTS_DIR = _variants.SOURCE_PROMPTS_DIR
"""``prompts/`` of the source checkout (the prompt variants' own constant)."""

_SECTION_RE = re.compile(r"^## (?P<title>.+?)\s*$", re.MULTILINE)
_ANCHOR_RE = re.compile(r"^### (?P<score>.+?)\s*$", re.MULTILINE)
_EXAMPLE_RE = re.compile(r"^Example:\s*\S", re.MULTILINE)


class RubricError(ValueError):
    """The rubric files are malformed, or don't match the frozen hash."""


@dataclass(frozen=True)
class Rubric:
    """The judge's instructions as loaded from disk."""

    system_text: str
    rubric_text: str
    anchors: Mapping[str, Mapping[int, str]]
    """Dimension -> score -> anchor text (including its example), from ``parse_rubric``."""

    @property
    def system_prompt(self) -> str:
        """The system prompt every judge request carries: instructions, then the rubric."""
        return f"{self.system_text.rstrip()}\n\n{self.rubric_text.rstrip()}\n"

    @property
    def sha256(self) -> str:
        """Hex sha256 of the request format version and the exact system prompt."""
        payload = f"judge-request-format:{JUDGE_REQUEST_FORMAT}\n{self.system_prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def rubric_file_sha256(self) -> str:
        """Hex sha256 of ``judge_rubric.md`` exactly as it appears in ``system_prompt``: the
        instrument human labels are made under (see the module docstring). It strips trailing
        whitespace as the prompt does, so the two hashes change together on a rubric edit and
        neither changes when only the file's trailing whitespace does."""
        return hashlib.sha256(self.rubric_text.rstrip().encode("utf-8")).hexdigest()


def parse_rubric(text: str) -> dict[str, dict[int, str]]:
    """Anchors of a rubric file, checked against SPEC §13.2.

    The file must have one ``## <title>`` section per dimension (titles in
    ``DIMENSION_TITLES``) and nothing else at that level; each section must have exactly the
    anchors ``### 1`` to ``### 4``, and each anchor a line starting ``Example:``. Every problem
    is reported at once in a ``RubricError``.
    """
    by_title = {title.casefold(): dim for dim, title in DIMENSION_TITLES.items()}
    problems: list[str] = []
    anchors: dict[str, dict[int, str]] = {}
    sections = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(sections):
        title = match.group("title")
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        dim = by_title.get(title.casefold())
        if dim is None:
            problems.append(f"unknown section {title!r}")
            continue
        if dim in anchors:
            problems.append(f"duplicate section {title!r}")
            continue
        anchors[dim] = _parse_anchors(title, text[match.end() : end], problems)
    problems.extend(
        f"missing section {DIMENSION_TITLES[dim]!r}" for dim in DIMENSIONS if dim not in anchors
    )
    if problems:
        raise RubricError("malformed rubric: " + "; ".join(problems))
    return {dim: anchors[dim] for dim in DIMENSIONS}


def _parse_anchors(title: str, body: str, problems: list[str]) -> dict[int, str]:
    found: dict[int, str] = {}
    marks = list(_ANCHOR_RE.finditer(body))
    for index, mark in enumerate(marks):
        label = mark.group("score")
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        anchor = body[mark.end() : end].strip()
        if label not in {str(s) for s in SCORES}:
            problems.append(f"{title}: unexpected anchor {label!r}")
            continue
        score = int(label)
        if score in found:
            problems.append(f"{title}: duplicate anchor {score}")
            continue
        if not _EXAMPLE_RE.search(anchor):
            problems.append(f"{title}: anchor {score} has no 'Example:' line")
        found[score] = anchor
    for score in SCORES:
        if score not in found:
            problems.append(f"{title}: missing anchor {score}")
    return {score: found[score] for score in SCORES if score in found}


def default_prompts_dir() -> Path:
    """Where the judge files are read from: the same directory as the prompt variants
    (``render.variants.default_prompts_dir``), so the package data of an installed wheel
    (``identitybench/_prompts``) comes first and the checkout's ``prompts/`` second.

    One lookup for every prompt file means a wheel never pairs its own model prompts with a
    rubric from wherever it happens to run, and an editable checkout never reads a stale copy.
    Raises ``RubricError`` naming both locations when neither exists.
    """
    try:
        return _variants.default_prompts_dir()
    except _variants.VariantError as exc:
        raise RubricError(str(exc)) from None


def load_rubric(
    prompts_dir: Path | str | None = None, *, expected_sha256: str | None = None
) -> Rubric:
    """Read and check ``judge_system.md`` and ``judge_rubric.md``.

    With ``expected_sha256`` (the hash recorded when the rubric was validated), a rubric whose
    hash differs raises ``RubricError``: scores from an edited rubric are not covered by the
    validation that gates the leaderboard.
    """
    root = default_prompts_dir() if prompts_dir is None else Path(prompts_dir)
    system_text = (root / SYSTEM_FILE).read_text(encoding="utf-8")
    rubric_text = (root / RUBRIC_FILE).read_text(encoding="utf-8")
    if not system_text.strip():
        raise RubricError(f"{root / SYSTEM_FILE} is empty")
    anchors = parse_rubric(rubric_text)
    rubric = Rubric(
        system_text=system_text,
        rubric_text=rubric_text,
        anchors=MappingProxyType(
            {dim: MappingProxyType(dict(by_score)) for dim, by_score in anchors.items()}
        ),
    )
    if expected_sha256 is not None and rubric.sha256 != expected_sha256:
        raise RubricError(
            f"rubric hash {rubric.sha256} does not match the frozen hash {expected_sha256}; "
            "re-validate the judge before using an edited rubric"
        )
    return rubric


@functools.cache
def default_rubric() -> Rubric:
    """The rubric from ``default_prompts_dir()``, read once per process."""
    return load_rubric()


def rubric_sha256(prompts_dir: Path | str | None = None) -> str:
    """Hash of the rubric in ``prompts_dir`` (default: ``default_prompts_dir()``)."""
    return load_rubric(prompts_dir).sha256
