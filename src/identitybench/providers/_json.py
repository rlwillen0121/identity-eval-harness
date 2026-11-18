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
import math
import re
import sys
from collections.abc import Collection, Iterator, Mapping
from dataclasses import fields
from typing import Any, cast

from identitybench.providers.base import ModelResponse, ToolCall, Usage

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_DATA_FENCE_TAGS = frozenset({"", "json", "jsonc", "json5"})
"""Fence tags whose content counts as output; any other tag marks a code block."""
_THINK_TAG_RE = re.compile(r"<(/?)(think|thinking)>", re.IGNORECASE)
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_DECODER = json.JSONDecoder()
MAX_JSON_TEXT = 1024 * 1024
MAX_TOKEN_COUNT = 1_000_000_000
MAX_RESPONSE_NODES = 10_000
MAX_RESPONSE_DEPTH = 64
MAX_USAGE_DEPTH = 48
MAX_USAGE_NODES = 4_096
MAX_USAGE_STRING = 16_384
_SCAN_WORK_BUDGET = 256 * MAX_JSON_TEXT
"""Cumulative characters the embedded-object scan may revisit across failed parses."""
_USAGE_MARKER = "__identitybench_usage_marker__"
_MAX_FINITE_FLOAT = sys.float_info.max


def bounded_token_count(value: object) -> int:
    """Clamp untrusted usage counts before arithmetic or cache serialization."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return min(max(value, 0), MAX_TOKEN_COUNT)
    if isinstance(value, float) and math.isfinite(value):
        return int(min(max(value, 0), MAX_TOKEN_COUNT))
    return 0


def safe_nonnegative_float_add(total: float, addition: float) -> tuple[float, bool]:
    """Add finite nonnegative costs without overflowing; return the saturation signal."""
    if not math.isfinite(total) or total < 0:
        raise ValueError("total must be a finite nonnegative float")
    if not math.isfinite(addition) or addition < 0:
        raise ValueError("addition must be a finite nonnegative float")
    if addition > _MAX_FINITE_FLOAT - total:
        return _MAX_FINITE_FLOAT, True
    return total + addition, False


class _UnsafeResponseData(ValueError):
    """A response field exceeds the JSON-safe boundary's resource or type limits."""


def _safe_tree(
    value: Any,
    *,
    usage: bool = False,
    excluded_keys: frozenset[str] = frozenset(),
    active: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
) -> Any:
    """Boundedly copy JSON-like values; usage mode emits compact markers on bad values."""
    if active is None:
        active = set()
    if budget is None:
        budget = [MAX_USAGE_NODES, MAX_JSON_TEXT] if usage else [MAX_RESPONSE_NODES, MAX_JSON_TEXT]
    depth_limit = MAX_USAGE_DEPTH if usage else MAX_RESPONSE_DEPTH
    budget[0] -= 1
    if budget[0] < 0 or depth > depth_limit:
        if usage:
            return {_USAGE_MARKER: "node_or_depth_limit"}
        raise _UnsafeResponseData("response data exceeds structural limits")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        budget[1] -= len(value)
        if len(value) > (MAX_USAGE_STRING if usage else MAX_JSON_TEXT) or budget[1] < 0:
            if usage:
                return {_USAGE_MARKER: "string_limit", "length": len(value)}
            raise _UnsafeResponseData("response data exceeds string limits")
        return value
    if isinstance(value, int):
        if abs(value) <= MAX_TOKEN_COUNT:
            return value
        if usage:
            try:
                if value.bit_length() <= 4096:
                    return {"type": "integer", "value": str(value)}
            except ValueError:
                pass
            return {_USAGE_MARKER: "integer_limit"}
        raise _UnsafeResponseData("response integer exceeds limits")
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"type": "float", "value": repr(value)} if usage else None
    if isinstance(value, Mapping | list | tuple):
        identity = id(value)
        if identity in active:
            if usage:
                return {_USAGE_MARKER: "cycle"}
            raise _UnsafeResponseData("cyclic response data")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                result: dict[str, Any] = {}
                try:
                    for index, (key, child) in enumerate(value.items()):
                        if usage and index >= MAX_USAGE_NODES:
                            result["__truncated__"] = {_USAGE_MARKER: "node_limit"}
                            break
                        if isinstance(key, str) and key in excluded_keys:
                            continue
                        if not isinstance(key, str):
                            if usage:
                                key = f"<non-string-key:{type(key).__name__}>"
                            else:
                                raise _UnsafeResponseData("response object keys must be strings")
                        budget[1] -= len(key)
                        if (
                            len(key) > (MAX_USAGE_STRING if usage else MAX_JSON_TEXT)
                            or budget[1] < 0
                        ):
                            if usage:
                                result["<oversized-key>"] = {_USAGE_MARKER: "string_limit"}
                                continue
                            raise _UnsafeResponseData("response data exceeds string limits")
                        result[key] = _safe_tree(
                            child,
                            usage=usage,
                            excluded_keys=excluded_keys,
                            active=active,
                            budget=budget,
                            depth=depth + 1,
                        )
                except _UnsafeResponseData:
                    raise
                except Exception as exc:
                    if usage:
                        return {_USAGE_MARKER: "mapping_error"}
                    raise _UnsafeResponseData("invalid response mapping") from exc
                return result
            result_list = []
            for index, child in enumerate(value):
                if usage and index >= MAX_USAGE_NODES:
                    result_list.append({_USAGE_MARKER: "node_limit"})
                    break
                result_list.append(
                    _safe_tree(
                        child,
                        usage=usage,
                        excluded_keys=excluded_keys,
                        active=active,
                        budget=budget,
                        depth=depth + 1,
                    )
                )
            return result_list
        except _UnsafeResponseData:
            raise
        except Exception as exc:
            if usage:
                return {_USAGE_MARKER: "container_error"}
            raise _UnsafeResponseData("invalid response container") from exc
        finally:
            active.remove(identity)
    if usage:
        return {"type": type(value).__name__}
    raise _UnsafeResponseData(f"unsupported response value: {type(value).__name__}")


def _finite_json(value: Any, *, budget: list[int] | None = None) -> Any:
    """Copy a finite JSON tree within response-specific resource bounds."""
    return _safe_tree(value, budget=budget)


def json_safe_usage_value(value: Any, *, excluded_keys: frozenset[str] = frozenset()) -> Any:
    """Preserve bounded usage evidence in strict-JSON-safe form."""
    return _safe_tree(value, usage=True, excluded_keys=excluded_keys)


def raw_usage_payload(value: Any) -> dict[str, Any] | None:
    """Bound and copy an API usage block before adapter-side normalization."""
    if isinstance(value, Mapping):
        captured = json_safe_usage_value(value)
        return captured if isinstance(captured, dict) else None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="python", exclude_none=True)
        except (TypeError, ValueError, RecursionError):
            return None
        if isinstance(result, Mapping):
            captured = json_safe_usage_value(result)
            return captured if isinstance(captured, dict) else None
    return None


def _usage_count_problem(value: Any) -> str | None:
    if type(value) is not int:
        return "not_integer"
    if value < 0:
        return "negative"
    if value > MAX_TOKEN_COUNT:
        return "clamped"
    return None


def _normalized_usage_key(value: str) -> str:
    """Normalize documented camelCase token-count names to snake_case."""
    value = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", value)
    return _CAMEL_CASE_BOUNDARY_RE.sub(r"\1_\2", value).lower()


def _provider_usage_problems(value: Any, path: str = "raw_usage") -> dict[str, str]:
    """Find malformed token counts with cycle, depth, and node bounds."""
    problems: dict[str, str] = {}
    seen: set[int] = set()
    pending = [(value, path, 0)]
    nodes = 0
    while pending and len(problems) < 128:
        current, current_path, depth = pending.pop()
        nodes += 1
        if nodes > MAX_USAGE_NODES:
            problems[current_path[:512]] = "node_limit"
            break
        if depth > MAX_USAGE_DEPTH:
            problems[current_path[:512]] = "depth_limit"
            continue
        if isinstance(current, dict | list | tuple):
            identity = id(current)
            if identity in seen:
                problems[current_path[:512]] = "cycle_or_repeated_container"
                continue
            seen.add(identity)
            if isinstance(current, dict):
                marker = current.get(_USAGE_MARKER)
                if isinstance(marker, str):
                    problems[current_path[:512]] = marker
                    continue
                for index, (key, child) in enumerate(current.items()):
                    if index >= MAX_USAGE_NODES:
                        problems[current_path[:512]] = "node_limit"
                        break
                    if not isinstance(key, str):
                        problems[current_path[:512]] = "invalid_key"
                        continue
                    if len(key) > MAX_USAGE_STRING:
                        problems[current_path[:512]] = "string_limit"
                        continue
                    child_path = f"{current_path}.{key}"
                    normalized_key = _normalized_usage_key(key)
                    if (
                        normalized_key == "tokens"
                        or normalized_key.endswith("_tokens")
                        or normalized_key.endswith("_token_count")
                    ):
                        if isinstance(child, dict) and isinstance(child.get(_USAGE_MARKER), str):
                            problems[child_path[:512]] = child[_USAGE_MARKER]
                        elif isinstance(child, dict | list | tuple):
                            if isinstance(child, dict) and child.get("type") in {
                                "integer",
                                "float",
                                "string",
                                "omitted",
                            }:
                                problems[child_path[:512]] = "not_integer"
                            else:
                                pending.append((child, child_path, depth + 1))
                        else:
                            problem = _usage_count_problem(child)
                            if problem is not None:
                                problems[child_path[:512]] = problem
                    else:
                        pending.append((child, child_path, depth + 1))
            else:
                for index, child in enumerate(current):
                    if index >= MAX_USAGE_NODES:
                        problems[current_path[:512]] = "node_limit"
                        break
                    pending.append((child, f"{current_path}[{index}]", depth + 1))
        elif isinstance(current, str) and len(current) > MAX_USAGE_STRING:
            problems[current_path[:512]] = "string_limit"
    return problems


def sanitize_response(response: ModelResponse, kind: str) -> ModelResponse:
    """Validate and normalize untrusted provider/replay response data at the boundary."""
    usage_anomalies: dict[str, str] = {}
    original_usage: dict[str, Any] = {}
    if isinstance(response.usage, Usage):
        for field in fields(Usage):
            value = getattr(response.usage, field.name, None)
            original_usage[field.name] = json_safe_usage_value(value)
            problem = _usage_count_problem(value)
            if problem is not None:
                usage_anomalies[field.name] = problem
    else:
        usage_anomalies["usage"] = "missing_or_malformed"
    original_raw = response.raw
    if isinstance(original_raw, dict) and "raw_usage" in original_raw:
        provider_problems = _provider_usage_problems(original_raw["raw_usage"])
        usage_anomalies.update(provider_problems)
        if provider_problems:
            original_usage["provider_raw_usage"] = json_safe_usage_value(original_raw["raw_usage"])

    response.usage = Usage(
        **{
            field.name: bounded_token_count(getattr(response.usage, field.name, None))
            for field in fields(Usage)
        }
    )
    malformed = False
    for name in ("text", "served_model", "stop_reason", "error"):
        value = getattr(response, name)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_JSON_TEXT):
            setattr(response, name, None)
            malformed = True
    budget = [MAX_RESPONSE_NODES, MAX_JSON_TEXT]
    for name in ("raw", "parsed"):
        value = getattr(response, name)
        if value is None:
            continue
        if not isinstance(value, dict):
            setattr(response, name, None)
            malformed = True
            continue
        try:
            setattr(response, name, _finite_json(value, budget=budget))
        except _UnsafeResponseData:
            setattr(response, name, None)
            malformed = True
    normalized_calls: list[ToolCall] = []
    if not isinstance(response.tool_calls, list) or len(response.tool_calls) > MAX_RESPONSE_NODES:
        malformed = True
    else:
        for call in response.tool_calls:
            try:
                if (
                    not isinstance(call.id, str)
                    or not isinstance(call.name, str)
                    or len(call.id) > MAX_JSON_TEXT
                    or len(call.name) > MAX_JSON_TEXT
                    or not isinstance(call.arguments, dict)
                ):
                    malformed = True
                    continue
                budget[1] -= len(call.id) + len(call.name)
                if budget[1] < 0:
                    raise _UnsafeResponseData("response data exceeds string limits")
                arguments = _finite_json(call.arguments, budget=budget)
                normalized_calls.append(ToolCall(id=call.id, name=call.name, arguments=arguments))
            except (AttributeError, _UnsafeResponseData):
                malformed = True
    response.tool_calls = normalized_calls
    if usage_anomalies:
        raw = response.raw if isinstance(response.raw, dict) else {}
        if "raw_usage" in raw and "provider_raw_usage" not in original_usage:
            original_usage["provider_raw_usage"] = json_safe_usage_value(raw["raw_usage"])
        raw["usage_original"] = original_usage
        raw["usage_anomaly"] = usage_anomalies
        response.raw = raw
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
    """Nesting deeper than the decoder supports was found during the embedded-object scan."""


def load_object(text: str | bytes) -> dict[str, Any] | None:
    """``text`` decoded as one JSON object, or ``None`` (invalid, not an object, or too deep)."""
    if not isinstance(text, str | bytes) or len(text) > MAX_JSON_TEXT:
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
    costing quadratic time (one deep decode attempt per brace).
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
        objects = [value for _, _, value in _top_level_objects(_blank_code_fences(stripped))]
    except _TooDeep:
        return None
    if not objects:
        return None
    if schema_keys is not None:
        answer_like = frozenset(schema_keys) | frozenset(required_keys or ())
        for value in reversed(objects):
            if answer_like & value.keys():
                return cast(dict[str, Any], value)
    elif required_keys:
        required = frozenset(required_keys)
        for value in reversed(objects):
            if required <= value.keys():
                return cast(dict[str, Any], value)
    return objects[-1]
