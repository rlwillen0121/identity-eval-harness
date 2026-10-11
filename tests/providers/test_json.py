from __future__ import annotations

import json
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from identitybench.providers._json import extract_json_object, load_object, strip_reasoning


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"decision": "DENY"}', {"decision": "DENY"}),
        ('  \n{"decision": "DENY"}\n\n', {"decision": "DENY"}),
        ('﻿{"a": 1}', {"a": 1}),
        ('```json\n{"decision": "APPROVE"}\n```', {"decision": "APPROVE"}),
        ('```JSON\n{"decision": "APPROVE"}\n```', {"decision": "APPROVE"}),
        ('```\n{"decision": "ESCALATE"}\n```', {"decision": "ESCALATE"}),
        ('Here you go:\n```json\n{"x": [1, 2]}\n```\nThanks!', {"x": [1, 2]}),
        (
            'My answer is {"decision": "DENY", "rules": ["SOD-001"]} as required.',
            {"decision": "DENY", "rules": ["SOD-001"]},
        ),
        ('Use the {name} placeholder... {"ok": true}', {"ok": True}),
        ('{"outer": {"inner": {"deep": 1}}} trailing', {"outer": {"inner": {"deep": 1}}}),
        ('{"s": "a } brace and a { brace"}', {"s": "a } brace and a { brace"}),
        ('{"first": 1} and then {"second": 2}', {"second": 2}),
        ('{"outer": {"inner": 1}} then {"last": {"nested": 2}}', {"last": {"nested": 2}}),
        ('{"s": "<think>not reasoning</think>"}', {"s": "<think>not reasoning</think>"}),
    ],
)
def test_extracts_objects(text: str, expected: dict[str, object]) -> None:
    assert extract_json_object(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A draft inside reasoning tags is never the answer.
        '<think>Draft: {"decision": "APPROVE"} ... no, SOD applies.</think>\n'
        '{"decision": "DENY", "rules": ["SOD-001"]}',
        '<thinking>{"decision": "APPROVE"}</thinking>{"decision": "DENY", "rules": ["SOD-001"]}',
        # Chat templates that open the tag in the prompt leave only the closing tag.
        'Considering {"decision": "APPROVE"}...</think>\n'
        '{"decision": "DENY", "rules": ["SOD-001"]}',
        # Visible reasoning with a draft, then the final answer.
        'First guess {"decision": "APPROVE"}; but the requester is terminated, so:\n'
        '{"decision": "DENY", "rules": ["SOD-001"]}',
        # An echoed schema example before the answer.
        'Schema example: {"decision": "APPROVE|DENY"}. My answer: '
        '{"decision": "DENY", "rules": ["SOD-001"]}',
        # Fenced drafts: the last fence wins too.
        '```json\n{"decision": "APPROVE"}\n```\nRevised:\n'
        '```json\n{"decision": "DENY", "rules": ["SOD-001"]}\n```',
    ],
)
def test_the_final_answer_wins_over_drafts_and_examples(text: str) -> None:
    assert extract_json_object(text) == {"decision": "DENY", "rules": ["SOD-001"]}


@pytest.mark.parametrize(
    "text",
    [
        '<think>{"decision": "APPROVE"}</think>',
        '<think>{"decision": "APPROVE"}',  # reasoning cut off before any answer
        '{"decision": "APPROVE"}</think>',
        'Draft {"decision": "APPROVE"}</think>  ',
    ],
)
def test_reasoning_alone_is_not_an_answer(text: str) -> None:
    assert extract_json_object(text) is None


def test_strip_reasoning() -> None:
    assert strip_reasoning("a<think>x</think>b<THINKING>y</THINKING>c") == "abc"
    assert strip_reasoning("reasoning</think>answer") == "answer"
    assert strip_reasoning("answer<think>unfinished") == "answer"
    assert strip_reasoning("no tags") == "no tags"


@pytest.mark.parametrize(
    "text",
    [
        'x {"a": "<think>"} y',
        'x {"a": "</think>"} y',
        'x {"a": "<thinking>open", "b": "close</thinking>"} y',
    ],
)
def test_strip_reasoning_ignores_tags_inside_a_decoded_object(text: str) -> None:
    assert strip_reasoning(text) == text


def test_strip_reasoning_still_strips_real_tags_next_to_an_object_with_a_tag() -> None:
    text = '<think>draft</think>{"a": "<think>"} tail<think>unfinished'
    assert strip_reasoning(text) == '{"a": "<think>"} tail'


def _regex_strip_reasoning(text: str) -> str:
    """The original regex implementation, the reference for texts without objects."""
    text = re.sub(r"<(think|thinking)>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    closes = list(re.finditer(r"</(?:think|thinking)>", text, flags=re.IGNORECASE))
    if closes:
        text = text[closes[-1].end() :]
    opened = re.search(r"<(?:think|thinking)>", text, flags=re.IGNORECASE)
    return text[: opened.start()] if opened is not None else text


@given(
    st.lists(
        st.sampled_from(
            ["<think>", "</think>", "<thinking>", "</thinking>", "<THINK>", "</Think>", "a", "b "]
        ),
        max_size=12,
    )
)
def test_strip_reasoning_matches_the_regex_semantics_without_objects(parts: list[str]) -> None:
    text = "".join(parts)
    assert strip_reasoning(text) == _regex_strip_reasoning(text)


DENY = {"decision": "DENY", "rules": ["SOD-001"]}
DENY_JSON = '{"decision": "DENY", "rules": ["SOD-001"]}'
APPROVE_JSON = '{"decision": "APPROVE", "rules": []}'


@pytest.mark.parametrize(
    "text",
    [
        # A fenced example or schema echo, then the bare final answer.
        f"Example format:\n```json\n{APPROVE_JSON}\n```\nMy decision: {DENY_JSON}",
        # A json-tagged echo, then the answer in an untagged fence.
        f"Schema:\n```json\n{APPROVE_JSON}\n```\nAnswer:\n```\n{DENY_JSON}\n```",
        # The reverse: an untagged draft fence, then a json-tagged final.
        f"Draft:\n```\n{APPROVE_JSON}\n```\nFinal:\n```json\n{DENY_JSON}\n```",
        # A bare draft, then a fenced final.
        f"First thought {APPROVE_JSON}, but no.\n```json\n{DENY_JSON}\n```",
    ],
)
def test_position_not_fencing_decides_which_object_is_last(text: str) -> None:
    assert extract_json_object(text) == DENY


@pytest.mark.parametrize(
    "text",
    [
        f'```python\nprint("hi")\n```\nresult: {DENY_JSON}',
        # An object inside a code block in another language is never the answer, even last.
        f"{DENY_JSON}\n```python\nexample = {APPROVE_JSON}\n```",
        f"{DENY_JSON}\n```yaml\n{APPROVE_JSON}\n```",
    ],
)
def test_objects_inside_code_blocks_in_other_languages_are_ignored(text: str) -> None:
    assert extract_json_object(text) == DENY


def test_a_code_block_alone_is_not_an_answer() -> None:
    assert extract_json_object(f"```python\nx = {APPROVE_JSON}\n```") is None


def test_a_whole_object_containing_a_code_fence_string_survives() -> None:
    value = {"note": "```python\nx = 1\n```"}
    assert extract_json_object(json.dumps(value)) == value


@pytest.mark.parametrize(
    "text",
    [
        "[" * 30000,
        '{"a": ' * 30000,
        f"Draft {APPROVE_JSON} then " + '{"a": ' * 30000,
        f'{DENY_JSON} then {{"a": ' + "[" * 30000 + "1" + "]" * 30000 + "}",
    ],
)
def test_nesting_too_deep_to_decode_is_unscorable_not_a_crash(text: str) -> None:
    assert extract_json_object(text) is None


@pytest.mark.parametrize("text", ["[" * 30000, b"[" * 30000, '{"a": ' * 30000, "[]", "1", "{"])
def test_load_object_never_raises(text: str | bytes) -> None:
    assert load_object(text) is None


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   \n ",
        "no json here",
        "[1, 2, 3]",
        '"just a string"',
        "42",
        '{"truncated": "valu',
        "{not: valid}",
        '```json\n{"broken": }\n```',
        "{'single': 'quotes'}",
    ],
)
def test_returns_none_without_a_valid_object(text: str | None) -> None:
    assert extract_json_object(text) is None


def test_does_not_repair_trailing_commas() -> None:
    assert extract_json_object('{"a": 1,}') is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('prose {"a": "has <think> inside"} end', {"a": "has <think> inside"}),
        ('prose {"a": "has </think> inside"} end', {"a": "has </think> inside"}),
        (
            '<think>Draft {"decision": "APPROVE"}</think> Final: '
            '{"decision": "DENY", "note": "no <think> here"} done',
            {"decision": "DENY", "note": "no <think> here"},
        ),
    ],
)
def test_a_think_tag_inside_a_decoded_object_is_not_reasoning(
    text: str, expected: dict[str, object]
) -> None:
    assert extract_json_object(text) == expected


# --------------------------------------------------------------------------- required keys

KEYS = ("decision", "rules")


def test_required_keys_rescue_an_answer_followed_by_a_small_object() -> None:
    text = 'Answer: {"decision":"DENY","rules":[]} Note: see {"rule":"LIFE-001"}'
    assert extract_json_object(text, KEYS) == {"decision": "DENY", "rules": []}
    # Without required keys the last object wins, aside or not.
    assert extract_json_object(text) == {"rule": "LIFE-001"}


def test_required_keys_keep_the_defence_against_an_earlier_example() -> None:
    text = (
        'Schema example: {"decision": "APPROVE", "rules": []}. My answer: '
        '{"decision": "DENY", "rules": ["SOD-001"]} (see {"rule": "SOD-001"})'
    )
    assert extract_json_object(text, KEYS) == DENY


def test_without_an_object_holding_the_required_keys_the_last_object_wins() -> None:
    text = '{"decision": "DENY"} and then {"rule": "LIFE-001"}'
    assert extract_json_object(text, KEYS) == {"rule": "LIFE-001"}
    assert extract_json_object('{"rule": "LIFE-001"}', KEYS) == {"rule": "LIFE-001"}


def test_required_keys_count_only_top_level_keys() -> None:
    text = f'{DENY_JSON} then {{"meta": {{"decision": "APPROVE", "rules": []}}}}'
    assert extract_json_object(text, KEYS) == DENY


@pytest.mark.parametrize("keys", [list(KEYS), set(KEYS), frozenset(KEYS), {"decision": 1}])
def test_required_keys_accept_any_collection(keys: object) -> None:
    text = f'{DENY_JSON} Note: {{"rule": "LIFE-001"}}'
    assert extract_json_object(text, keys) == DENY  # type: ignore[arg-type]


def test_empty_required_keys_mean_none() -> None:
    assert extract_json_object(f'{DENY_JSON} {{"x": 1}}', ()) == {"x": 1}


def test_required_keys_as_a_single_string_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match="collection"):
        extract_json_object(DENY_JSON, "decision")


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=10,
)


@given(
    obj=st.dictionaries(st.text(max_size=6), json_values, max_size=4),
    prefix=st.text(alphabet=st.characters(blacklist_characters="`<"), max_size=20),
    suffix=st.text(alphabet=st.characters(blacklist_characters="{}`<"), max_size=20),
)
def test_round_trips_an_object_wrapped_in_prose(
    obj: dict[str, object], prefix: str, suffix: str
) -> None:
    assert extract_json_object(f"{prefix}{json.dumps(obj)}{suffix}") == obj


@given(
    draft=st.dictionaries(st.text(max_size=6), json_values, min_size=1, max_size=3),
    final=st.dictionaries(st.text(max_size=6), json_values, max_size=4),
    between=st.text(alphabet=st.characters(blacklist_characters="{}`<"), max_size=20),
)
def test_a_later_object_always_beats_an_earlier_one(
    draft: dict[str, object], final: dict[str, object], between: str
) -> None:
    text = f"{json.dumps(draft)}{between} {json.dumps(final)}"
    assert extract_json_object(text) == final


@given(
    aside=st.dictionaries(
        st.text(max_size=6).filter(lambda key: key not in KEYS), json_values, max_size=3
    ),
    between=st.text(alphabet=st.characters(blacklist_characters="{}`<"), max_size=20),
)
def test_an_answer_with_the_required_keys_beats_any_trailing_aside(
    aside: dict[str, object], between: str
) -> None:
    assert extract_json_object(f"{DENY_JSON}{between} {json.dumps(aside)}", KEYS) == DENY
