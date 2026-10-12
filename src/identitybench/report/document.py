"""The report's content model: sections of text, tables, facts, verbatim text and figures.

The Markdown and HTML reports must say the same thing (SPEC §15: M7 is "the same content as a
static HTML report"), so neither renderer decides what to say. ``report.sections`` builds one
:class:`Document` from the runs, and each renderer only turns its blocks into its own syntax.

Every string here is plain text, never markup: a renderer escapes it for its format. That is
what lets case data and model output (justifications, reviewer notes, rationales, all of them
untrusted and possibly adversarial, SPEC §8.4) flow into a report without being able to inject
Markdown or HTML. :class:`Verbatim` marks text that must be shown exactly as written, in a
code block or ``<pre>``, rather than reflowed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "Block",
    "Cell",
    "Document",
    "Facts",
    "Figure",
    "Group",
    "Section",
    "SectionLabel",
    "Table",
    "Text",
    "Tone",
    "Verbatim",
]

Tone = Literal["plain", "note", "warning"]
SectionLabel = Literal["primary", "exploratory", "mixed"]


@dataclass(frozen=True, slots=True)
class Text:
    """A paragraph. ``warning`` is for what changes how numbers may be read (an incomplete run,
    datasets that differ); ``note`` for explanations of method."""

    text: str
    tone: Tone = "plain"


@dataclass(frozen=True, slots=True)
class Cell:
    """A table cell with a secondary line: ``text`` is the value and ``sub`` says what it is (an
    interval, a bound, its counts). The Markdown report prints ``text (sub)``, as ``str()``
    does; the HTML report puts ``sub`` on a smaller second line, so a table with many such
    columns keeps each value on one line instead of wrapping it word by word."""

    text: str
    sub: str | None = None

    def __str__(self) -> str:
        return f"{self.text} ({self.sub})" if self.sub else self.text


@dataclass(frozen=True, slots=True)
class Table:
    """Rows of already-formatted cells (a string, or a :class:`Cell` with a secondary line).
    ``numeric`` right-aligns a column."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str | Cell, ...], ...]
    numeric: tuple[bool, ...] = ()
    caption: str | None = None

    def __post_init__(self) -> None:
        width = len(self.columns)
        if self.numeric and len(self.numeric) != width:
            raise ValueError(f"numeric has {len(self.numeric)} flags for {width} columns")
        for row in self.rows:
            if len(row) != width:
                raise ValueError(f"row {row!r} has {len(row)} cells for {width} columns")

    def is_numeric(self, index: int) -> bool:
        return bool(self.numeric) and self.numeric[index]


@dataclass(frozen=True, slots=True)
class Facts:
    """Labeled values, one per line (a definition list)."""

    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Verbatim:
    """Untrusted text shown exactly as written (a code block / ``<pre>``), under ``label``."""

    label: str
    text: str


@dataclass(frozen=True, slots=True)
class Figure:
    """A chart. ``svg`` is always present (the Markdown report links it as a file, the HTML
    report inlines it); ``vega`` is a Vega-Lite spec the HTML report renders instead when its
    scripts load, with the SVG as the fallback."""

    key: str
    """Unique within a document: the SVG file stem and the HTML element IDs."""
    title: str
    description: str
    """Alt text: what the chart shows, in words."""
    svg: str
    vega: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Group:
    """A titled subsection."""

    title: str
    blocks: tuple[Block, ...]


Block = Text | Table | Facts | Verbatim | Figure | Group


@dataclass(frozen=True, slots=True)
class Section:
    """A top-level section. ``key`` is its stable anchor; ``label`` says whether its numbers are
    SPEC §12.8 primary metrics, exploratory, or a mix (primary ones marked in place)."""

    key: str
    title: str
    blocks: tuple[Block, ...]
    label: SectionLabel | None = None


@dataclass(frozen=True, slots=True)
class Document:
    title: str
    intro: tuple[Block, ...]
    sections: tuple[Section, ...] = field(default=())

    def figures(self) -> Iterator[Figure]:
        """Every figure, in document order."""

        def walk(blocks: tuple[Block, ...]) -> Iterator[Figure]:
            for block in blocks:
                if isinstance(block, Figure):
                    yield block
                elif isinstance(block, Group):
                    yield from walk(block.blocks)

        yield from walk(self.intro)
        for section in self.sections:
            yield from walk(section.blocks)

    def __post_init__(self) -> None:
        keys = [s.key for s in self.sections]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate section keys: {keys}")
        figures = [f.key for f in self.figures()]
        if len(set(figures)) != len(figures):
            raise ValueError("duplicate figure keys")
