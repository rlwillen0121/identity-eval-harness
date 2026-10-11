"""Turn a provider response into a validated task output, once (SPEC §11.3, §8.2).

INVALID is decided here and nowhere else: the runner stores the ``ParseResult`` in a
``schemas.predictions.Prediction`` and every scorer reads that record instead of re-reading raw
text. The rules:

- A provider error (after the provider's own transport retries) is ``error``; a refusal is
  ``refused``. Both are INVALID and neither is ever retried on another model.
- The output is ``response.parsed`` when the provider returned structured output natively,
  else the JSON object extracted from ``response.text`` (``providers._json``: extraction, never
  repair). It is validated with the ``schemas.outputs`` models; anything that fails is
  ``invalid``, so a malformed answer can never be read as an approval.
- Extraction is schema-aware: the last object that shares a top-level property name with the
  task's output schema is the answer, complete or not, and objects sharing none (an aside such
  as ``{"rule": "LIFE-001"}``) are skipped. A broken final answer therefore fails validation
  instead of an earlier draft, or an echoed example, being scored in its place.
- Reviews are validated item by item. An item that fails is dropped and its reason kept in
  ``item_errors``; the rest of the campaign still counts. Duplicated and unknown ``item_id``s
  are kept as written, because the scorer (not the parser) decides that a missing or
  duplicated campaign item is INVALID (SPEC §8.2) and needs to see them to do so.
- An item whose ``item_id`` isn't an item of the campaign (a model that invents
  ``rv_11909_placeholder``) answers nothing the scorer reads, so it would otherwise vanish
  without a trace. ``unknown_items`` lists those IDs, and the prediction records each in its
  ``item_errors`` (``ParseResult.prediction_item_errors``) with ``UNKNOWN_ITEM``. That changes
  no score: the scorer and the judge look ``item_errors`` up only by campaign item IDs.

There are no content retries by default. With the experiment's ``repair: true`` flag the runner
re-asks once (``repair_request``) when ``needs_repair`` says the answer failed validation: the
whole answer was invalid, or a review lost items to per-item validation (each lost item would
be scored INVALID). It sends the re-ask through its normal cache, budget and cost path, keeps
the re-asked answer only when ``repair_improves`` says it is better, and only then marks the
prediction ``repaired``.

**Interface.** ``parse_response(case, response) -> ParseResult``, two positional arguments and
nothing else. This differs from the wave 2 interface table, which gave
``parse_response(case, response, *, schema_keys)`` (the requirement had ``repair=None``):

- Repair isn't a parameter: it is a second call, which needs the runner's cache, budget, cost
  and recording, so the runner drives it with ``needs_repair``, ``repair_request`` and
  ``repair_improves``, and ``parse_response`` stays a pure function of one response.
- The extraction keys aren't a parameter either: they follow from ``case.task``.
  ``REQUIRED_KEYS`` (the output models' required fields) and ``SCHEMA_KEYS`` (the output
  schema's property names) are used instead of the structured-output schema's ``required``
  list, which names every property (see ``REQUIRED_KEYS`` for why that would lose answers).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from identitybench.providers._json import extract_json_object
from identitybench.providers.base import Message, ModelRequest, ModelResponse, Role
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Task
from identitybench.schemas.json_schemas import OUTPUT_SCHEMAS
from identitybench.schemas.outputs import ReviewItemOutput, ReviewOutput
from identitybench.schemas.predictions import OUTPUT_MODELS, Output, PredictionStatus

__all__ = [
    "EMPTY_REPLY",
    "MAX_REASON_ERRORS",
    "REQUIRED_KEYS",
    "SCHEMA_KEYS",
    "UNKNOWN_ITEM",
    "ParseResult",
    "answer_text",
    "describe_errors",
    "needs_repair",
    "parse_response",
    "repair_improves",
    "repair_request",
    "unknown_items",
]

MAX_REASON_ERRORS = 3
"""Validation errors quoted in ``invalid_reason``; the rest are counted, not listed, so one
deeply broken answer can't bloat every prediction line."""

UNKNOWN_ITEM = "not an item of this campaign (kept as written; it answers no campaign item)"
"""What a prediction's ``item_errors`` records for each item ID the answer wrote that the
campaign doesn't hold (``ParseResult.unknown_items``)."""


def _required_keys(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(name for name, info in model.model_fields.items() if info.is_required())


REQUIRED_KEYS: dict[Task, tuple[str, ...]] = {
    task: _required_keys(model) for task, model in OUTPUT_MODELS.items()
}
"""Top-level keys an answer object must have, per task (``decision``, ``items``,
``operations``).

These are the keys the output *models* require, not the structured-output JSON schemas'
``required`` lists: those mark every property required because strict structured-output
implementations demand it, so a model that omits the optional ``confidence`` in free text
would otherwise lose its answer to a trailing aside during extraction."""

SCHEMA_KEYS: dict[Task, tuple[str, ...]] = {
    task: tuple(schema["properties"]) for task, schema in OUTPUT_SCHEMAS.items()
}
"""Top-level property names of each task's output schema: an extracted object sharing any of
them is an answer attempt (see the module docstring)."""


@dataclass(frozen=True)
class ParseResult:
    """The parser's verdict on one response.

    ``output`` is set exactly when ``status`` is ``ok``. ``item_errors`` (reviews only) maps an
    ``item_id``, or ``#<index>`` when the item had no usable ID, to why that item was dropped.
    ``unknown_items`` (reviews only) are the IDs of kept items that aren't items of the
    campaign, in first-written order.
    """

    status: PredictionStatus
    output: Output | None = None
    invalid_reason: str | None = None
    item_errors: Mapping[str, str] = field(default_factory=dict[str, str])
    unknown_items: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is PredictionStatus.ok

    @property
    def prediction_item_errors(self) -> dict[str, str]:
        """What the prediction records in ``item_errors``: every dropped item's reason, plus
        ``UNKNOWN_ITEM`` for each unknown item (after the reason when a dropped copy shares
        its ID). Repair (``needs_repair``) reads only the dropped items: an unknown item lost
        no campaign item, so re-asking for it can't settle more of them."""
        errors = dict(self.item_errors)
        for item_id in self.unknown_items:
            dropped = errors.get(item_id)
            errors[item_id] = UNKNOWN_ITEM if dropped is None else f"{dropped}; {UNKNOWN_ITEM}"
        return errors


def parse_response(case: Case, response: ModelResponse) -> ParseResult:
    """Validate ``response`` against the output model for ``case.task``, extracting the answer
    with ``REQUIRED_KEYS[case.task]`` and ``SCHEMA_KEYS[case.task]`` when the provider gave no
    native structured output. Never raises, never re-asks (see the module docstring)."""
    if response.error is not None:
        return ParseResult(PredictionStatus.error, invalid_reason=response.error)
    if response.refused:
        return ParseResult(
            PredictionStatus.refused, invalid_reason=_with_stop("model refused", response)
        )
    # Typed as a dict, but a hand-edited or foreign replay recording (``response_from_dict``
    # doesn't check types) or a buggy provider can hand over anything.
    data: object = response.parsed
    if data is None:
        data = extract_json_object(
            response.text,
            required_keys=REQUIRED_KEYS[case.task],
            schema_keys=SCHEMA_KEYS[case.task],
        )
    if data is None:
        empty = not (response.text or "").strip()
        reason = "empty response" if empty else "no JSON object in the response"
        return _invalid(reason, response)
    if not isinstance(data, dict):
        return _invalid(f"expected a JSON object, found a {type(data).__name__}", response)
    data = cast(dict[str, Any], data)
    if case.task is Task.access_review:
        return _parse_review(case, data, response)
    try:
        output = OUTPUT_MODELS[case.task].model_validate(data)
    except ValidationError as exc:
        return _invalid(describe_errors(exc), response)
    return ParseResult(PredictionStatus.ok, output=output)


def _parse_review(case: Case, data: dict[str, Any], response: ModelResponse) -> ParseResult:
    """Keep every item that validates, in the order written, say why each other one didn't,
    and name the kept items the campaign doesn't hold."""
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        found = "missing" if raw_items is None else f"a {type(raw_items).__name__}"
        return _invalid(f"items: expected a list, found {found}", response)
    items: list[ReviewItemOutput] = []
    errors: dict[str, str] = {}
    for index, raw in enumerate(cast(list[Any], raw_items)):
        try:
            items.append(ReviewItemOutput.model_validate(raw))
        except ValidationError as exc:
            key = _item_key(raw, index)
            reason = describe_errors(exc)
            # Two broken items can claim the same ID; keep both reasons under it.
            errors[key] = f"{errors[key]}; #{index}: {reason}" if key in errors else reason
    output = ReviewOutput(items=items)
    return ParseResult(
        PredictionStatus.ok,
        output=output,
        item_errors=errors,
        unknown_items=unknown_items(case, output),
    )


def unknown_items(case: Case, output: ReviewOutput) -> tuple[str, ...]:
    """The distinct ``item_id``s of ``output`` that aren't items of ``case``'s campaign, in
    first-written order (the module docstring says why they are recorded)."""
    campaign = case.view.campaign
    known = {item.item_id for item in campaign.items} if campaign is not None else set[str]()
    return tuple(dict.fromkeys(i.item_id for i in output.items if i.item_id not in known))


def _item_key(raw: Any, index: int) -> str:
    """The item's own ID when it wrote a usable one, else its position (``#3``)."""
    if isinstance(raw, dict):
        item_id = cast(dict[str, Any], raw).get("item_id")
        if isinstance(item_id, str) and item_id.strip():
            return item_id
    return f"#{index}"


def describe_errors(exc: ValidationError) -> str:
    """A short, stable summary of a validation error (``decision: Input should be ...``)."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in errors[:MAX_REASON_ERRORS]
    ]
    if len(errors) > MAX_REASON_ERRORS:
        parts.append(f"(+{len(errors) - MAX_REASON_ERRORS} more)")
    return "; ".join(parts)


def _with_stop(reason: str, response: ModelResponse) -> str:
    """Append the stop reason: ``max_tokens``/``length`` explains most truncated answers."""
    return f"{reason} (stop_reason: {response.stop_reason})" if response.stop_reason else reason


def _invalid(reason: str, response: ModelResponse) -> ParseResult:
    return ParseResult(PredictionStatus.invalid, invalid_reason=_with_stop(reason, response))


def answer_text(response: ModelResponse) -> str | None:
    """The model's answer as text: ``response.text`` when it has any, else the native
    structured output as canonical JSON.

    A provider that returns ``parsed`` natively often leaves ``text`` empty, and an answer that
    then fails validation would otherwise leave nothing to show in the failure gallery or to
    quote back in a repair re-ask.
    """
    if response.text and response.text.strip():
        return response.text
    if response.parsed is not None:
        return json.dumps(response.parsed, ensure_ascii=False, sort_keys=True)
    return response.text


def needs_repair(result: ParseResult) -> bool:
    """Whether ``repair: true`` re-asks after ``result``: the answer was ``invalid``, or it is a
    review that lost items to per-item validation.

    Refusals and provider errors never are: they are not content problems, and re-asking after
    a refusal would turn "declined" into a scored answer. A review's missing or duplicated
    items are not either: the parser didn't reject anything there, and the scorer judges them.
    """
    return result.status is PredictionStatus.invalid or (result.ok and bool(result.item_errors))


def repair_improves(case: Case, first: ParseResult, retry: ParseResult) -> bool:
    """Whether the re-asked answer ``retry`` replaces the ``first`` one.

    It must parse. Then it beats an invalid first answer outright, and a review's first answer
    only when it settles more campaign items (each answered by exactly one valid item): a
    re-ask that fixes the broken item but leaves out two others is worse, not better.
    """
    if not retry.ok:
        return False
    if not first.ok:
        return True
    return _settled(case, retry) > _settled(case, first)


def _settled(case: Case, result: ParseResult) -> int:
    """Campaign items answered by exactly one valid item (the scorer's unit, SPEC §8.2)."""
    campaign = case.view.campaign
    if campaign is None or not isinstance(result.output, ReviewOutput):
        return 0
    written = Counter(item.item_id for item in result.output.items)
    return sum(1 for item in campaign.items if written[item.item_id] == 1)


_REPAIR_PROMPT = (
    "Your previous reply could not be used: {reason}. Reply again with exactly one JSON object "
    "that follows the output format in the instructions, and nothing else."
)

EMPTY_REPLY = "(empty reply)"
"""Stand-in for an empty assistant turn in a transcript (a repair re-ask's, a tool loop's):
Messages-style APIs reject empty content. The text is part of the requests the response cache
keys on, so every transcript uses this one definition."""


def repair_request(
    request: ModelRequest, response: ModelResponse, result: ParseResult
) -> ModelRequest:
    """The one re-ask ``repair: true`` allows: the original conversation, the model's reply, and
    what failed validation (SPEC §11.3). Only a result ``needs_repair`` accepts qualifies."""
    if not needs_repair(result):
        raise ValueError(
            "only answers that failed validation can be repaired "
            f"(status {result.status}, {len(result.item_errors)} dropped items)"
        )
    reply = answer_text(response)
    turns: Sequence[Message] = (
        Message(Role.assistant, reply if reply and reply.strip() else EMPTY_REPLY),
        Message(Role.user, _REPAIR_PROMPT.format(reason=_repair_reason(result))),
    )
    return replace(request, messages=(*request.messages, *turns))


def _repair_reason(result: ParseResult) -> str:
    """The whole answer's validation error, or the first few dropped review items' errors."""
    if not result.ok:
        return result.invalid_reason or "the reply failed validation"
    errors = list(result.item_errors.items())
    parts = [f"{key} ({reason})" for key, reason in errors[:MAX_REASON_ERRORS]]
    if len(errors) > MAX_REASON_ERRORS:
        parts.append(f"(+{len(errors) - MAX_REASON_ERRORS} more)")
    return f"these items failed validation and were dropped: {'; '.join(parts)}"
