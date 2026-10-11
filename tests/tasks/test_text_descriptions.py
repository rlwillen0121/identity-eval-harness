"""Accurate entitlement descriptions never contradict the structured fields."""

from __future__ import annotations

import itertools
import random
import re

import pytest

from identitybench.tasks.text.descriptions import (
    KNOWN_FUNCTIONS,
    describe_entitlement,
    humanize_function,
)
from identitybench.world.apps_data import APP_SPECS

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
CHANGE_WORDS = re.compile(
    r"admin|write|edit|modif|creat|updat|manage|change|add |enter", re.IGNORECASE
)
READ_ONLY_WORDS = re.compile(r"read[- ]only|view[- ]only", re.IGNORECASE)
PRIVILEGED_WORDS = re.compile(r"privileged|elevated", re.IGNORECASE)


@pytest.mark.parametrize(
    ("privilege", "privileged", "classification"),
    list(itertools.product(("read", "write", "admin"), (True, False), CLASSIFICATIONS)),
)
def test_descriptions_are_accurate(privilege: str, privileged: bool, classification: str) -> None:
    rng = random.Random(f"{privilege}-{privileged}-{classification}")
    functions = ("reporting.view",) if privilege == "read" else ("gl.je.post",)
    for _ in range(60):
        text = describe_entitlement(
            rng,
            name="Ledger Access",
            app_name="Northwind Ledger",
            privilege=privilege,
            privileged=privileged,
            data_classification=classification,
            functions=functions,
        )
        assert "{" not in text and "}" not in text
        assert "Northwind Ledger" in text
        if privilege == "read":
            assert not CHANGE_WORDS.search(text), text
        else:
            assert not READ_ONLY_WORDS.search(text), text
        if privilege == "admin":
            assert re.search(r"admin", text, re.IGNORECASE), text
        if privileged:
            assert PRIVILEGED_WORDS.search(text), text
        else:
            assert not PRIVILEGED_WORDS.search(text), text
        for other in CLASSIFICATIONS:
            if other != classification:
                assert other not in text.lower(), text


def test_classification_and_functions_sometimes_mentioned() -> None:
    rng = random.Random(1)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="A",
            privilege="write",
            privileged=False,
            data_classification="confidential",
            functions=("ap.vendor.create",),
        )
        for _ in range(200)
    ]
    assert any("confidential" in t for t in texts)
    assert any("confidential" not in t for t in texts)
    assert any("create accounts payable vendor" in t for t in texts)


WORLD_FUNCTION_TAGS = sorted(
    {f for app in APP_SPECS for e in app.entitlements for f in e.functions}
)
BARE_ABBREVIATION = re.compile(r"\b(?:gl|je|ap|ar|po|idp|iga|chg|sox|prod)\b")


def test_humanize_function() -> None:
    assert humanize_function("gl.je.post") == "post general ledger journal entries"
    assert humanize_function("deploy.prod.sox") == "deploy changes to production systems"
    assert humanize_function("idp.admin") == "administer identity-provider accounts"
    assert humanize_function("chg.approve") == "approve change requests"
    assert humanize_function("iga.approve") == "approve access requests"
    assert humanize_function("ap_vendor_create") == "create accounts payable vendor"
    assert humanize_function("payroll") == "payroll"
    assert humanize_function("") == ""


def test_determinism() -> None:
    def run(seed: int) -> list[str]:
        rng = random.Random(seed)
        return [
            describe_entitlement(
                rng,
                name="N",
                app_name="A",
                privilege="admin",
                privileged=True,
                data_classification="restricted",
            )
            for _ in range(50)
        ]

    assert run(3) == run(3)
    assert run(3) != run(4)


def test_unknown_privilege_rejected() -> None:
    with pytest.raises(ValueError):
        describe_entitlement(
            random.Random(0),
            name="N",
            app_name="A",
            privilege="owner",
            privileged=False,
            data_classification="public",
        )


def test_read_entitlement_never_lists_change_functions() -> None:
    """A stray write/approve tag on a read entitlement must not contradict "read access"."""
    rng = random.Random(2)
    listed_read_verb = False
    for _ in range(300):
        text = describe_entitlement(
            rng,
            name="Payments Viewer",
            app_name="Northwind Ledger",
            privilege="read",
            privileged=False,
            data_classification="internal",
            functions=("ap.payment.approve", "gl.je.post", "reporting.view"),
        )
        assert not re.search(r"approve|post\b", text, re.IGNORECASE), text
        listed_read_verb |= "view reporting" in text
    assert listed_read_verb


def test_write_entitlement_keeps_change_functions() -> None:
    rng = random.Random(3)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="A",
            privilege="write",
            privileged=False,
            data_classification="internal",
            functions=("ap.payment.approve",),
        )
        for _ in range(100)
    ]
    assert any("approve vendor payments" in t for t in texts)


def test_every_world_function_tag_has_a_hand_written_phrase() -> None:
    assert WORLD_FUNCTION_TAGS, "apps_data defines no function tags"
    assert set(WORLD_FUNCTION_TAGS) <= set(KNOWN_FUNCTIONS)


@pytest.mark.parametrize("tag", WORLD_FUNCTION_TAGS)
def test_world_function_tags_render_as_plain_english(tag: str) -> None:
    phrase, obj = KNOWN_FUNCTIONS[tag]
    assert humanize_function(tag) == phrase
    rng = random.Random(tag)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="App",
            privilege="write",
            privileged=False,
            data_classification="internal",
            functions=(tag,),
        )
        for _ in range(80)
    ]
    for text in texts:
        assert not BARE_ABBREVIATION.search(text), text
    assert any(phrase in t for t in texts)
    assert any(obj in t for t in texts)


def test_usage_sentence_does_not_single_out_tagged_entitlements() -> None:
    """Untagged entitlements get a generic usage sentence at the same rate as tagged ones."""

    def usage_rate(functions: tuple[str, ...]) -> float:
        rng = random.Random(5)
        texts = [
            describe_entitlement(
                rng,
                name="N",
                app_name="App",
                privilege="write",
                privileged=False,
                data_classification="internal",
                functions=functions,
            )
            for _ in range(2000)
        ]
        return sum(bool(re.search(r"Used |Supports: ", t)) for t in texts) / len(texts)

    tagged, untagged = usage_rate(("gl.je.post",)), usage_rate(())
    assert abs(tagged - untagged) < 0.05
    assert 0.6 < untagged < 0.8
