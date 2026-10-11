"""JSON decoding for providers: safe object decoding and tolerant extraction (SPEC §11.3).

``load_object`` is the one place provider code decodes JSON it didn't produce. It never raises:
invalid JSON, a non-object and nesting deeper than the decoder supports (``RecursionError``,
which a repetition loop like ``[[[[…`` from a small model triggers) all come back as ``None``.

``extract_json_object`` is *extraction*, not repair: it finds a JSON object the model actually
wrote (bare, inside a Markdown code fence, or surrounded by prose) and never edits the text to
make it parse. Anything that doesn't contain a complete, valid object yields ``None`` and is
scored INVALID.

Five rules decide *which* object when a reply holds several:

- **Reasoning is not output.** ``<think>``/``<thinking>`` spans are removed first (open-weight
  reasoning models served without a reasoning parser put them in the content, sometimes with
  only the closing tag), so a draft answer inside them is never scored. A tag inside an object
  that decodes (a string value that mentions ``<think>``) is text, not a tag.
- **Code is not output.** Objects inside a fenced block tagged with another language
  (```` ```python ````, ```` ```yaml ````) are ignored; untagged and ``json`` fences count like
  any other text.
- **The last object wins, by position.** Models that reason in the visible reply, or echo the
  schema's example before answering, put the final answer last; taking the first object, or
  preferring fenced objects wherever they are, would score a draft or the example (possibly an
  APPROVE) as the model's decision.
- **An answer beats a trailing aside.** Given ``required_keys`` (the schema's top-level
  ``required``), the last object holding all of them wins, so a small object a model adds after
  its answer (``Note: see {"rule": "LIFE-001"}``) doesn't turn a valid answer INVALID. Without
  such an object the last object wins as before.
- **A later answer attempt is never passed over** (schema-aware extraction). Given
  ``schema_keys`` (the schema's top-level property names), the last object that shares at
  least one of them wins, whether or not it is complete; objects sharing none, like the aside
  above, are skipped. The last complete object shares them too, so this keeps the previous
  rule's rescue of an answer followed by an aside, but an incomplete answer-like object after
  a complete one (a final answer that lost its ``decision``, a schema echo after a draft) is
  chosen and fails validation (INVALID) instead of letting the earlier draft or example be
  scored as the model's decision. With no object sharing a key, the last object wins.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterator
from typing import Any

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_DATA_FENCE_TAGS = frozenset({"", "json", "jsonc", "json5"})
"""Fence tags whose content counts as output; any other tag marks a code block."""
_THINK_TAG_RE = re.compile(r"<(/?)(think|thinking)>", re.IGNORECASE)
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


def _top_level_objects(text: str) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """``(start, end, object)`` for every top-level object in ``text``, in order, found by
    decoding from each ``{`` in turn.

    After a successful decode the scan resumes past the object's end, so the objects nested
    inside it are never mistaken for top-level ones; a brace that doesn't start a valid object
    (prose like ``{name}``) is skipped.

    Raises ``_TooDeep`` on nesting the decoder can't handle: where that object would end is then
    unknown, so no later object can be placed. Stopping there also keeps a degenerate reply from
    costing quadratic time (one deep decode attempt per brace).
    """
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
            yield start, end, value
            start = text.find("{", end)
        else:
            start = text.find("{", start + 1)


def _object_spans(text: str) -> list[tuple[int, int]]:
    """Where the top-level objects of ``text`` are; the scan stops at nesting too deep to decode
    (the extraction scan then reports the reply as unscorable)."""
    spans: list[tuple[int, int]] = []
    try:
        for start, end, _ in _top_level_objects(text):
            spans.append((start, end))
    except _TooDeep:
        pass
    return spans


def strip_reasoning(text: str) -> str:
    """Remove reasoning spans from a reply.

    Complete ``<think>…</think>`` spans are dropped; a stray closing tag means everything before
    it was reasoning (chat templates that open the tag in the prompt); a stray opening tag means
    everything after it is unfinished reasoning, so nothing after it counts as an answer.

    A tag inside a top-level object that decodes is part of a string value, not a tag, so it
    neither cuts nor hides that object (``{"note": "has <think> inside"}`` in prose survives).
    """
    found = list(_THINK_TAG_RE.finditer(text))
    if not found:
        return text
    spans = _object_spans(text)
    tags = [tag for tag in found if not any(start < tag.start() < end for start, end in spans)]

    # Complete spans: each opening tag pairs with the next closing tag of the same name, and the
    # tags in between are part of the span.
    removed: list[tuple[int, int]] = []
    stray: list[re.Match[str]] = []
    index = 0
    while index < len(tags):
        tag = tags[index]
        if not tag.group(1):
            name = tag.group(2).lower()
            close = next(
                (
                    later
                    for later in range(index + 1, len(tags))
                    if tags[later].group(1) and tags[later].group(2).lower() == name
                ),
                None,
            )
            if close is not None:
                removed.append((tag.start(), tags[close].end()))
                index = close + 1
                continue
        stray.append(tag)
        index += 1

    closes = [tag for tag in stray if tag.group(1)]
    begin = closes[-1].end() if closes else 0
    opens = [tag for tag in stray if not tag.group(1) and tag.start() >= begin]
    finish = opens[0].start() if opens else len(text)
    pieces: list[str] = []
    position = begin
    for start, end in removed:
        if start >= begin and end <= finish:
            pieces.append(text[position:start])
            position = end
    pieces.append(text[position:finish])
    return "".join(pieces)


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


def extract_json_object(
    text: str | None,
    required_keys: Collection[str] | None = None,
    *,
    schema_keys: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """Return the JSON object in ``text``, or ``None`` if there isn't a valid one.

    In order of preference:

    1. the whole text (after trimming whitespace and a byte-order mark) is one object, checked
       before anything else so a string value that happens to contain ``<think>`` or a code
       fence survives;
    2. the same, after removing reasoning spans (``strip_reasoning``), which also applies to
       the last step;
    3. among the complete top-level objects anywhere in the text outside code blocks in other
       languages (bare, in prose or in a ``json``/untagged fence): with ``schema_keys``, the
       last one sharing at least one of them or ``required_keys``; otherwise the last one that
       has every key in ``required_keys``; in both cases the last object when none qualifies
       (or no keys are given).

    Arrays and scalars are never returned: every task schema's root is an object. Nesting too
    deep to decode makes the reply unscorable (``None``).
    """
    if isinstance(required_keys, str) or isinstance(schema_keys, str):
        raise TypeError("key arguments must be collections of key names, not one string")
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
        objects = [value for _, _, value in _top_level_objects(_blank_code_fences(stripped))]
    except _TooDeep:
        return None
    if not objects:
        return None
    if schema_keys is not None:
        answer_like = frozenset(schema_keys) | frozenset(required_keys or ())
        for value in reversed(objects):
            if answer_like & value.keys():
                return value
    elif required_keys:
        required = frozenset(required_keys)
        for value in reversed(objects):
            if required <= value.keys():
                return value
    return objects[-1]
