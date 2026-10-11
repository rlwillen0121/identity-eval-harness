"""Markdown escaping for report text, including untrusted case data and model output.

Justifications, reviewer notes and rationales can carry attacks (SPEC §8.4), and a report is
read on GitHub and in editors that render Markdown and inline HTML. So text never reaches the
Markdown as markup:

- inline text has every character that could start inline Markdown or HTML syntax
  backslash-escaped (an underscore only at a word boundary, where it could open emphasis;
  inside a word, as in ``always_escalate``, it is literal and left alone so tables stay
  readable as plain text). The report never starts a line with such text (it always follows a
  table pipe or a bullet's label), so line-start syntax such as ``#`` needs no escape;
- table cells are inline text on one line, with ``|`` escaped;
- verbatim text goes in a fenced code block whose fence is longer than any backtick run inside
  it, so the text can't close the block early.
"""

from __future__ import annotations

import re

__all__ = ["escape_cell", "escape_inline", "fenced"]

_SPECIAL = re.compile(r"([\\`*\[\]<>|~]|(?<!\w)_|_(?!\w))")
_AMP = re.compile(r"&(?=#?\w+;)")
_BACKTICKS = re.compile(r"`+")


def escape_inline(text: str) -> str:
    """``text`` as literal Markdown inline content: special characters escaped and line
    breaks folded to spaces. An ``&`` that would start an entity becomes ``&amp;``."""
    flat = " ".join(text.split())
    return _SPECIAL.sub(r"\\\1", _AMP.sub("&amp;", flat))


def escape_cell(text: str) -> str:
    """``text`` as one table cell (inline escaping; ``|`` included)."""
    return escape_inline(text) or " "


def fenced(text: str, info: str = "text") -> str:
    """``text`` in a fenced code block that it cannot break out of."""
    longest = max((len(m.group()) for m in _BACKTICKS.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text.replace("\r\n", "\n").rstrip("\n")
    return f"{fence}{info}\n{body}\n{fence}"
