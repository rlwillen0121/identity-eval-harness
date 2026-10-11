"""Shared bank mechanics and cross-bank split isolation for the text banks."""

from __future__ import annotations

import itertools
import random
import re
from collections.abc import Mapping
from difflib import SequenceMatcher

import pytest

from identitybench.schemas.enums import Split
from identitybench.tasks.text import attacks, justifications, tickets
from identitybench.tasks.text._bank import (
    Bank,
    Family,
    families_from_mapping,
    fill,
    grouped_families,
    require_slots,
    template_fields,
    template_problems,
)
from identitybench.tasks.text._pools import COMMON_DEFAULTS, pick_name

ALL_BANKS: tuple[Bank, ...] = (
    justifications.BANK,
    *attacks.BANKS.values(),
    attacks.MDESC_BANK,
    tickets.BANK,
)


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_family_ids_unique_and_each_family_in_one_split(bank: Bank) -> None:
    ids = [f.id for f in bank.families]
    assert len(ids) == len(set(ids))
    for family in bank.families:
        assert isinstance(family.split, Split)


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_every_family_covers_every_variant(bank: Bank) -> None:
    """Family choice can't depend on the variant, so family IDs never encode case structure."""
    for family in bank.families:
        assert tuple(family.variants) == bank.variants
        assert all(family.variants.values()), family.id


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_every_split_has_families(bank: Bank) -> None:
    for split in Split:
        assert bank.select(split=split), (bank.name, split)


SLOT = re.compile(r"\{[a-z_]+\}")
NEAR_DUPLICATE = 0.85
"""SequenceMatcher ratio at which two templates count as the same prose ("Work purposes." vs
"For work purposes." is 0.875)."""


def prose(template: str) -> str:
    """A template with every slot replaced by one placeholder, so "pls add {app}" and
    "pls add {entitlement_name}" compare equal, and with case and spacing normalized."""
    return " ".join(SLOT.sub("{}", template).lower().split())


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_no_template_prose_shared_across_splits(bank: Bank) -> None:
    """SPEC §9.4, §10.5: test never sees dev prose, and fresh neither, not even reworded slots."""
    by_split = {
        split: sorted({prose(t) for f in bank.select(split=split) for t in f.templates})
        for split in Split
    }
    for a, b in itertools.combinations(Split, 2):
        shared = set(by_split[a]) & set(by_split[b])
        assert not shared, (a, b, shared)
        for x, y in itertools.product(by_split[a], by_split[b]):
            matcher = SequenceMatcher(None, x, y)
            if (
                matcher.real_quick_ratio() < NEAR_DUPLICATE
                or matcher.quick_ratio() < NEAR_DUPLICATE
            ):
                continue
            assert matcher.ratio() < NEAR_DUPLICATE, f"[{a}] {x!r} ~ [{b}] {y!r}"


def test_prose_normalization_catches_slot_renames() -> None:
    assert prose("pls add {entitlement_name}") == prose("Pls  add {app}")
    assert SequenceMatcher(None, prose("Work purposes."), prose("For work purposes.")).ratio() >= (
        NEAR_DUPLICATE
    )


def test_family_ids_unique_across_banks() -> None:
    ids = [f.id for bank in ALL_BANKS for f in bank.families]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_templates_use_plain_slots_only(bank: Bank) -> None:
    for family in bank.families:
        for template in family.templates:
            for name in template_fields(template):
                assert name.isidentifier(), (family.id, template)


def test_bank_rejects_duplicate_empty_and_mismatched_families() -> None:
    fam = Family("x.a", Split.dev, {"v": ("hi",)})
    with pytest.raises(ValueError, match="duplicate"):
        Bank("b", [fam, fam])
    with pytest.raises(ValueError, match="no templates"):
        Bank("b", [Family("x.b", Split.dev, {"v": ()})])
    with pytest.raises(ValueError, match="no templates"):
        Bank("b", [Family("x.b", Split.dev, {})])
    with pytest.raises(ValueError, match="covers"):
        Bank("b", [fam, Family("x.c", Split.test, {"w": ("yo",)})])
    with pytest.raises(ValueError, match="no families"):
        Bank("b", [])


def test_family_templates_for_unknown_variant_raises() -> None:
    fam = Family("x.a", Split.dev, {"v": ("hi",), "w": ("yo",)})
    assert fam.templates == ("hi", "yo")
    assert fam.templates_for("w") == ("yo",)
    with pytest.raises(ValueError, match="no variant"):
        fam.templates_for("z")


def test_grouped_families_keeps_alternatives_in_one_family() -> None:
    families = grouped_families(
        "x",
        {("v", Split.dev): (("v1", "v1b"), "v2"), ("w", Split.dev): ("w1", ("w2", "w2b"))},
        {Split.dev: ("a", "b")},
    )
    assert families[0].variants == {"v": ("v1", "v1b"), "w": ("w1",)}
    assert families[1].variants == {"v": ("v2",), "w": ("w2", "w2b")}


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_every_shipped_bank_validates_its_slots(bank: Bank) -> None:
    assert bank.allowed_slots is not None
    for template in (t for f in bank.families for t in f.templates):
        assert template_problems(template, bank.allowed_slots) == [], template


def test_template_problems() -> None:
    allowed = {"app", "entitlement_name"}
    assert template_problems("need {app} / {entitlement_name}", allowed) == []
    assert template_problems("{{literal}} braces are fine", allowed) == []
    assert template_problems("need {entitlment_name}", allowed) == [
        "unknown slot {entitlment_name}"
    ]
    assert template_problems("need {}", allowed) == ["unknown slot {}"]
    assert template_problems("need {app.name}", allowed) == ["unknown slot {app.name}"]
    assert template_problems("need {app:>9}", allowed) == [
        "slot {app} must not use a format spec or conversion"
    ]
    assert template_problems("need {app!r}", allowed) == [
        "slot {app} must not use a format spec or conversion"
    ]
    assert template_problems("need {app", allowed)[0].startswith("malformed template")


def test_bank_rejects_unknown_slots_and_runs_family_check() -> None:
    good = Family("x.dev.a", Split.dev, {"v": ("need {app}",)})
    typo = Family("x.dev.b", Split.dev, {"v": ("need {ap}",)})
    Bank("b", [good], allowed_slots={"app"})
    with pytest.raises(ValueError, match=r"x\.dev\.b.*unknown slot \{ap\}"):
        Bank("b", [good, typo], allowed_slots={"app"})

    def no_shouting(family: Family) -> None:
        if any(t.isupper() for t in family.templates):
            raise ValueError(f"{family.id}: shouting")

    with pytest.raises(ValueError, match="shouting"):
        Bank("b", [Family("x.dev.c", Split.dev, {"v": ("NEED",)})], check=no_shouting)


def test_replace_split_applies_the_banks_checks_to_held_out_families() -> None:
    """A typo in an unpublished fresh family fails when it is loaded, not mid-release."""
    bank = Bank(
        "toy",
        [
            Family("toy.dev.a", Split.dev, {"x": ("{app}",)}),
            Family("toy.fresh.a", Split.fresh, {"x": ("{app}!",)}),
        ],
        allowed_slots={"app"},
    )
    ok = bank.replace_split(
        Split.fresh, families_from_mapping("toy", Split.fresh, {"h": {"x": ["{app}?"]}})
    )
    assert ok.allowed_slots == bank.allowed_slots
    with pytest.raises(ValueError, match="unknown slot"):
        bank.replace_split(
            Split.fresh, families_from_mapping("toy", Split.fresh, {"h": {"x": ["{aap}?"]}})
        )


def test_grouped_families_deals_round_robin() -> None:
    families = grouped_families(
        "x",
        {("v", Split.dev): ("v1", "v2", "v3"), ("w", Split.dev): ("w1", "w2")},
        {Split.dev: ("a", "b")},
    )
    assert [f.id for f in families] == ["x.dev.a", "x.dev.b"]
    assert families[0].variants == {"v": ("v1", "v3"), "w": ("w1",)}
    assert families[1].variants == {"v": ("v2",), "w": ("w2",)}


def test_require_slots() -> None:
    require_slots({"app": "A", "x": None}, ("app",), what="t")
    with pytest.raises(ValueError, match=r"t: missing required slot.*x, y"):
        require_slots({"app": "A", "x": None}, ("app", "x", "y"), what="t")


def test_bank_choose_without_candidates_raises() -> None:
    bank = Bank("b", [Family("x.a", Split.dev, {"v": ("hi",)})])
    with pytest.raises(ValueError, match="no family"):
        bank.choose(random.Random(0), split=Split.test)


def test_fill_missing_slot_raises() -> None:
    with pytest.raises(ValueError, match="missing slot 'app'"):
        fill("need {app}", {}, random.Random(0), defaults={})


def test_fill_rejects_format_spec() -> None:
    with pytest.raises(ValueError, match="format spec"):
        fill("need {app:>10}", {"app": "X"}, random.Random(0), defaults={})


def test_fill_unescapes_braces_and_keeps_repeated_slot_consistent() -> None:
    out = fill(
        '{{"a": "{colleague_first}"}} / {colleague_first}',
        {},
        random.Random(3),
        defaults=COMMON_DEFAULTS,
    )
    name = out.split('"')[3]
    assert out == f'{{"a": "{name}"}} / {name}'


def test_fill_noise_never_touches_slot_values() -> None:
    def shout(rng: random.Random, segments: list[str]) -> list[str]:
        del rng
        return [s.upper() for s in segments]

    out = fill(
        "need {app} now", {"app": "MixedCase App"}, random.Random(0), defaults={}, noise=shout
    )
    assert out == "NEED MixedCase App NOW"


def test_name_defaults_avoid_names_already_in_text() -> None:
    for seed in range(300):
        known: Mapping[str, str] = {"colleague_first": "Priya", "manager_first": "Marcus"}
        assert pick_name(random.Random(seed), known) not in {"Priya", "Marcus"}


def _toy_bank() -> Bank:
    return Bank(
        "toy",
        [
            Family("toy.dev.a", Split.dev, {"x": ("dx",), "y": ("dy",)}),
            Family("toy.fresh.a", Split.fresh, {"x": ("fx",), "y": ("fy",)}),
        ],
    )


def test_replace_split_swaps_only_that_split_and_reorders_variants() -> None:
    bank = _toy_bank()
    new = bank.replace_split(
        Split.fresh, families_from_mapping("toy", Split.fresh, {"h": {"y": ["hy"], "x": ["hx"]}})
    )
    assert [f.id for f in new.families] == ["toy.dev.a", "toy.fresh.h"]
    assert tuple(new.families[1].variants) == ("x", "y")
    assert [f.id for f in bank.families] == ["toy.dev.a", "toy.fresh.a"]  # original untouched


def test_replace_split_validates_replacements() -> None:
    bank = _toy_bank()
    with pytest.raises(ValueError, match="no replacement"):
        bank.replace_split(Split.fresh, [])
    with pytest.raises(ValueError, match="not in split"):
        bank.replace_split(
            Split.fresh, [Family("toy.test.z", Split.test, {"x": ("a",), "y": ("b",)})]
        )
    with pytest.raises(ValueError, match="covers"):
        bank.replace_split(Split.fresh, [Family("toy.fresh.z", Split.fresh, {"x": ("a",)})])
    with pytest.raises(ValueError, match="duplicate"):
        bank.replace_split(
            Split.dev, [Family("toy.fresh.a", Split.dev, {"x": ("a",), "y": ("b",)})]
        )
