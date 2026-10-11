"""JSON decoding for providers: safe object decoding and tolerant extraction (SPEC §11.3).

``load_object`` is the one place provider code decodes JSON it didn't produce. It never raises:
invalid JSON, a non-object and nesting deeper than the decoder supports (``RecursionError``,
which a repetition loop like ``[[[[…`` from a small model triggers) all come back as ``None``.

``extract_json_object`` is *extraction*, not repair: it finds a JSON object the model actually
wrote (bare, inside a Markdown code fence, or surrounded by prose) and never edits the text to
make it parse. Anything that doesn't contain a complete, valid object yields ``None`` and is
scored INVALID.

Three rules decide *which* object when a reply holds several:

- **Reasoning is not output.** ``<think>``/``<thinking>`` spans are removed first (open-weight
  reasoning models served without a reasoning parser put them in the content, sometimes with
  only the closing tag), so a draft answer inside them is never scored.
- **Code is not output.** Objects inside a fenced block tagged with another language
  (```` ```python ````, ```` ```yaml ````) are ignored; untagged and ``json`` fences count like
  any other text.
- **The last object wins, by position.** Models that reason in the visible reply, or echo the
  schema's example before answering, put the final answer last; taking the first object, or
  preferring fenced objects wherever they are, would score a draft or the example (possibly an
  APPROVE) as the model's decision.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_DATA_FENCE_TAGS = frozenset({"", "json", "jsonc", "json5"})
"""Fence tags whose content counts as output; any other tag marks a code block."""
_THINK_SPAN_RE = re.compile(r"<(think|thinking)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</(?:think|thinking)>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking)>", re.IGNORECASE)
_DECODER = json.JSONDecoder()


class _TooDeep(Exception):
    """Nesting deeper than the decoder supports was found during the embedded-object scan."""


def load_object(text: str | bytes) -> dict[str, Any] | None:
    """``text`` decoded as one JSON object, or ``None`` (invalid, not an object, or too deep)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def strip_reasoning(text: str) -> str:
    """Remove reasoning spans from a reply.

    Complete ``<think>…</think>`` spans are dropped; a stray closing tag means everything before
    it was reasoning (chat templates that open the tag in the prompt); a stray opening tag means
    everything after it is unfinished reasoning, so nothing after it counts as an answer.
    """
    text = _THINK_SPAN_RE.sub("", text)
    closes = list(_THINK_CLOSE_RE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    opened = _THINK_OPEN_RE.search(text)
    if opened is not None:
        text = text[: opened.start()]
    return text


def _blank_code_fences(text: str) -> str:
    """``text`` with every fenced block in another language replaced by spaces.

    Offsets are preserved, so the embedded-object scan still sees every other object at its
    original position.
    """

    def blank(match: re.Match[str]) -> str:
        if match.group(1).lower() in _DATA_FENCE_TAGS:
            return match.group(0)
        return " " * len(match.group(0))

    return _FENCE_RE.sub(blank, text)


def _last_embedded_object(text: str) -> dict[str, Any] | None:
    """Last top-level object found by decoding from each ``{`` in turn.

    After a successful decode the scan resumes past the object's end, so the objects nested
    inside it are never mistaken for top-level ones; a brace that doesn't start a valid object
    (prose like ``{name}``) is skipped.

    Raises ``_TooDeep`` on nesting the decoder can't handle: where that object would end is then
    unknown, so no object can be called the last one. Stopping there also keeps a degenerate
    reply from costing quadratic time (one deep decode attempt per brace).
    """
    last: dict[str, Any] | None = None
    start = text.find("{")
    while start != -1:
        try:
            value, end = _DECODER.raw_decode(text, start)
        except RecursionError as exc:
            raise _TooDeep from exc
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            last = value
            start = text.find("{", end)
        else:
            start = text.find("{", start + 1)
    return last


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """Return the JSON object in ``text``, or ``None`` if there isn't a valid one.

    In order of preference:

    1. the whole text (after trimming whitespace and a byte-order mark) is one object, checked
       before anything else so a string value that happens to contain ``<think>`` or a code
       fence survives;
    2. the same, after removing reasoning spans (``strip_reasoning``), which also applies to
       the last step;
    3. the last complete top-level object anywhere in the text outside code blocks in other
       languages, whether bare, in prose or in a ``json``/untagged fence.

    Arrays and scalars are never returned: every task schema's root is an object. Nesting too
    deep to decode makes the reply unscorable (``None``).
    """
    if not text:
        return None
    stripped = text.strip().lstrip("﻿").strip()
    whole = load_object(stripped)
    if whole is not None:
        return whole

    stripped = strip_reasoning(stripped).strip()
    if not stripped:
        return None
    whole = load_object(stripped)
    if whole is not None:
        return whole

    try:
        return _last_embedded_object(_blank_code_fences(stripped))
    except _TooDeep:
        return None
