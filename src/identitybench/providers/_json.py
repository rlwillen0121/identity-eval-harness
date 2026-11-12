"""JSON decoding for providers: safe object decoding and tolerant extraction
(``archive/specs/SPEC-v1.md`` §11.3).

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
import math
import re
from collections.abc import Collection, Iterator
from dataclasses import fields
from typing import Any, cast

from identitybench.providers.base import ModelResponse, Usage

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_DATA_FENCE_TAGS = frozenset({"", "json", "jsonc", "json5"})
"""Fence tags whose content counts as output; any other tag marks a code block."""
_THINK_TAG_RE = re.compile(r"<(/?)(think|thinking)>", re.IGNORECASE)
_DECODER = json.JSONDecoder()
MAX_JSON_TEXT = 1024 * 1024
MAX_TOKEN_COUNT = 1_000_000_000
_SCAN_WORK_BUDGET = 256 * MAX_JSON_TEXT
"""Characters the embedded-object scan may re-read across failed decode attempts."""


def bounded_token_count(value: object) -> int:
    """Untrusted counts are finite, non-negative and capped before any arithmetic."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return min(max(value, 0), MAX_TOKEN_COUNT)
    if isinstance(value, float) and math.isfinite(value):
        return int(min(max(value, 0), MAX_TOKEN_COUNT))
    return 0


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_json(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_finite_json(child) for child in value]
    return value


def sanitize_response(response: ModelResponse, kind: str) -> ModelResponse:
    """Enforce the response contract before untrusted API/replay data reaches a runner."""
    response.usage = Usage(
        **{
            field.name: bounded_token_count(getattr(response.usage, field.name, None))
            for field in fields(Usage)
        }
    )
    malformed = False
    for name in ("text", "served_model", "stop_reason", "error"):
        value = getattr(response, name)
        if value is not None and not isinstance(value, str):
            setattr(response, name, None)
            malformed = True
    for name in ("raw", "parsed"):
        value = getattr(response, name)
        if value is not None and not isinstance(value, dict):
            setattr(response, name, None)
            malformed = True
    try:
        response.raw = _finite_json(response.raw)
        response.parsed = _finite_json(response.parsed)
        for call in response.tool_calls:
            if not isinstance(call.id, str) or not isinstance(call.name, str):
                malformed = True
            if not isinstance(call.arguments, dict):
                malformed = True
            else:
                arguments = _finite_json(call.arguments)
                call.arguments.clear()
                call.arguments.update(arguments)
    except (AttributeError, TypeError, RecursionError):
        response.raw = None
        malformed = True
    for name in ("latency_s", "reported_cost_usd"):
        value = getattr(response, name)
        try:
            number = (
                float(value)
                if isinstance(value, int | float) and not isinstance(value, bool)
                else -1.0
            )
        except OverflowError:
            number = -1.0
        if not math.isfinite(number) or number < 0:
            setattr(response, name, 0.0 if name == "latency_s" else None)
    response.refused = response.refused is True
    if malformed:
        response.text = None
        response.parsed = None
        response.tool_calls = []
        response.error = f"{kind}: malformed response"
    return response


class _TooDeep(Exception):
    """The embedded-object scan exceeded its nesting or decoding-work limit."""


def load_object(text: str | bytes) -> dict[str, Any] | None:
    """``text`` decoded as one JSON object, or ``None`` (invalid, not an object, or too deep)."""
    if not isinstance(text, str | bytes):
        return None
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
    costing quadratic time (one deep decode attempt per brace). Failed attempts also draw on
    a cumulative work budget: a failure costs the offset it stopped at, since building a
    ``JSONDecodeError`` scans the text up to there. The budget is far above what prose with
    many stray braces spends, and a reply that exhausts it is unscorable.
    """
    start = text.find("{")
    remaining = _SCAN_WORK_BUDGET
    while start != -1:
        try:
            value, end = _DECODER.raw_decode(text, start)
        except RecursionError as exc:
            raise _TooDeep from exc
        except ValueError as exc:
            remaining -= getattr(exc, "pos", start) + 1
            if remaining < 0:
                raise _TooDeep from exc
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            yield start, end, cast(dict[str, Any], value)
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
    if len(text) > MAX_JSON_TEXT:
        return ""
    found = list(_THINK_TAG_RE.finditer(text))
    if not found:
        return text
    spans = _object_spans(text)
    tags: list[re.Match[str]] = []
    span_index = 0
    for tag in found:
        while span_index < len(spans) and spans[span_index][1] <= tag.start():
            span_index += 1
        if span_index == len(spans) or tag.start() <= spans[span_index][0]:
            tags.append(tag)

    # Index the next matching close once, instead of rescanning the remaining tags for
    # every unmatched opening tag. Keep the historical first-matching-close semantics.
    next_closes: dict[str, int] = {}
    pairs: dict[int, int] = {}
    for index in range(len(tags) - 1, -1, -1):
        tag = tags[index]
        name = tag.group(2).lower()
        if tag.group(1):
            next_closes[name] = index
        elif name in next_closes:
            pairs[index] = next_closes[name]

    # Complete spans: each opening tag pairs with the next closing tag of the same name, and the
    # tags in between are part of the span.
    removed: list[tuple[int, int]] = []
    stray: list[re.Match[str]] = []
    index = 0
    while index < len(tags):
        tag = tags[index]
        if not tag.group(1):
            close = pairs.get(index)
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
    if not isinstance(text, str) or not text or len(text) > MAX_JSON_TEXT:
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
        objects: list[dict[str, Any]] = [
            value for _, _, value in _top_level_objects(_blank_code_fences(stripped))
        ]
    except _TooDeep:
        return None
    if not objects:
        return None
    if schema_keys is not None:
        answer_like = frozenset(schema_keys) | frozenset(required_keys or ())
        for value in objects[::-1]:
            if answer_like & value.keys():
                return value
    elif required_keys:
        required = frozenset(required_keys)
        for value in objects[::-1]:
            if required <= value.keys():
                return value
    return objects[-1]
