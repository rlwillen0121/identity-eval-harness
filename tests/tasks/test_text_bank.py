"""Shared bank mechanics and cross-bank split isolation for the text banks."""

from __future__ import annotations

import random
from collections.abc import Mapping

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


@pytest.mark.parametrize("bank", ALL_BANKS, ids=lambda b: b.name)
def test_no_template_text_shared_across_splits(bank: Bank) -> None:
    owner: dict[str, Split] = {}
    for family in bank.families:
        for template in family.templates:
            normalized = template.strip().lower()
            if normalized in owner:
                assert owner[normalized] == family.split, f"{template!r} appears in two splits"
            owner[normalized] = family.split


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
