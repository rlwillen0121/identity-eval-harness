"""Justification bank: volume, split isolation, determinism and content hygiene."""

from __future__ import annotations

import random
import re

import pytest

from identitybench.policy.registry import ALL_RULE_IDS, FAMILY_ORDER
from identitybench.schemas.enums import Split
from identitybench.tasks.text._bank import families_from_mapping, fill, template_fields
from identitybench.tasks.text._pools import COMMON_DEFAULTS
from identitybench.tasks.text.justifications import (
    BANK,
    DEFAULT_KIND_WEIGHTS,
    STRUCTURE_DEPENDENT_KINDS,
    JustificationKind,
    render_justification,
)
from identitybench.tasks.text.noise import noisify_segments

SLOTS: dict[str, object] = {
    "app": "Northwind Ledger",
    "entitlement_name": "AP Invoice Entry",
    "beneficiary_first": "Wanjiru",
    "ticket_id": "INC-20417",
}
BASE_SLOTS = {k: v for k, v in SLOTS.items() if k in ("app", "entitlement_name")}

# Approval, authority, direction and override language belongs to attacks, never to clean
# justifications: a manager "asking", "suggesting" or "pointing" someone to request access is an
# implied endorsement (SPEC §7.3).
AUTHORITY = re.compile(
    r"approv|sign(?:ed)?[- ]?off|authori[sz]|\bok'?d\b|okayed|green[- ]?l|bless|cleared|"
    r"CISO|CFO|CEO|override|polic|ignore|system|exception|waive|pre-?vetted|"
    r"asked me|suggest|told me|said (?:I|we)\b|wants? me|pointed me|at the request of|"
    r"\bon \S+(?: \S+)?'?s? (?:instruction|direction|request)|\bmanager|\bmgr\b|\bboss\b|"
    r"supervisor|director|endorse|\bsigned|sanction|mandat",
    re.IGNORECASE,
)
VP = re.compile(r"\bVP\b")
RULE_ID = re.compile(rf"\b(?:{'|'.join(FAMILY_ORDER)})-\d{{3}}\b", re.IGNORECASE)
RULE_FAMILY_WORD = re.compile(r"\bSoD\b", re.IGNORECASE)
TICKET_ID = re.compile(r"\b[A-Z]{2,6}-\d{3,}\b")
# Clean text must not assert structured facts it could contradict.
STRUCTURED_CLAIM = re.compile(
    r"read[- ]only|view[- ]only|admin|sandbox|non-?prod|\bprod\b|(?<![a-z]-)sensitive|confidential|"
    r"restricted|public|\bPII\b",
    re.IGNORECASE,
)


def all_templates() -> list[tuple[str, str, str]]:
    return [(f.id, kind, t) for f in BANK.families for kind, ts in f.variants.items() for t in ts]


FIXED_SLOTS = {**SLOTS, **{k: k.upper() for k in COMMON_DEFAULTS}}


def kind_of(text: str, family_id: str) -> str:
    """Which kind produced ``text`` (rendered with ``FIXED_SLOTS`` and ``noise=False``)."""
    family = next(f for f in BANK.families if f.id == family_id)
    for kind, templates in family.variants.items():
        if text in {t.format(**{k: str(v) for k, v in FIXED_SLOTS.items()}) for t in templates}:
            return kind
    raise AssertionError(f"{text!r} matches no template of {family_id}")


def assert_clean(text: str, *, allow_ticket: bool) -> None:
    assert "{" not in text and "}" not in text, text
    assert not AUTHORITY.search(text), text
    assert not VP.search(text), text
    assert not RULE_ID.search(text), text
    assert not RULE_FAMILY_WORD.search(text), text
    assert not any(rule in text.upper() for rule in ALL_RULE_IDS), text
    assert not STRUCTURED_CLAIM.search(text), text
    if not allow_ticket:
        assert not TICKET_ID.search(text), text


def test_volume() -> None:
    """About 200 templates in at least 20 families (SPEC §6.4), with room to hold out fresh."""
    assert BANK.template_count() >= 200
    assert len(BANK.families) >= 20
    for split in Split:
        assert len(BANK.select(split=split)) >= 7


def test_templates_are_unique() -> None:
    templates = [t for _, _, t in all_templates()]
    assert len(templates) == len(set(templates))


def test_every_kind_available_in_every_family() -> None:
    assert set(BANK.variants) == {k.value for k in JustificationKind}
    for split in Split:
        families = BANK.select(split=split)
        assert len(families) >= 2
        for family in families:
            for kind in JustificationKind:
                assert family.templates_for(kind.value), (family.id, kind)


def test_family_choice_ignores_kind() -> None:
    """Same seed, any kind: same family. The ID can't reveal requester != beneficiary etc."""
    for seed in range(60):
        fids = {
            render_justification(random.Random(seed), split=Split.test, slots=SLOTS, kind=kind)[1]
            for kind in JustificationKind
        }
        assert len(fids) == 1


def test_spec_kinds_are_covered() -> None:
    required = {
        "routine",
        "project",
        "coverage",
        "onboarding",
        "audit_prep",
        "temporary",
        "vague",
        "terse",
        "verbose",
        "urgency",
    }
    assert required <= set(BANK.variants)


def test_default_and_structure_dependent_kinds_partition_the_bank() -> None:
    """``kind=None`` never asserts a case fact (recent hire, time-bound, on-behalf, ticket)."""
    default = set(DEFAULT_KIND_WEIGHTS)
    dependent = set(STRUCTURE_DEPENDENT_KINDS)
    assert default.isdisjoint(dependent)
    assert default | dependent == set(JustificationKind)
    assert {"onboarding", "coverage", "temporary", "on_behalf", "ticket_mention"} == dependent


@pytest.mark.parametrize("split", list(Split))
def test_split_never_yields_another_splits_family(split: Split) -> None:
    rng = random.Random(11)
    allowed = {f.id for f in BANK.families if f.split == split}
    for kind in [None, *JustificationKind]:
        for _ in range(60):
            _, family_id = render_justification(rng, split=split, slots=SLOTS, kind=kind)
            assert family_id in allowed


def test_test_split_never_sees_dev_families() -> None:
    dev = {f.id for f in BANK.families if f.split == Split.dev}
    rng = random.Random(5)
    seen = {render_justification(rng, split="test", slots=SLOTS)[1] for _ in range(2000)}
    assert seen.isdisjoint(dev)


def test_determinism() -> None:
    def run(seed: int) -> list[tuple[str, str]]:
        rng = random.Random(seed)
        return [render_justification(rng, split=Split.dev, slots=SLOTS) for _ in range(200)]

    assert run(42) == run(42)
    assert run(42) != run(43)


@pytest.mark.parametrize(("family_id", "kind", "template"), all_templates())
def test_every_template_fills_cleanly(family_id: str, kind: str, template: str) -> None:
    del family_id
    allow_ticket = kind == JustificationKind.ticket_mention
    for seed in range(6):
        rng = random.Random(seed)
        clean = fill(template, SLOTS, rng, defaults=COMMON_DEFAULTS)
        noisy = fill(template, SLOTS, rng, defaults=COMMON_DEFAULTS, noise=noisify_segments)
        assert_clean(clean, allow_ticket=allow_ticket)
        assert_clean(noisy, allow_ticket=allow_ticket)


def test_ticket_ids_only_in_ticket_mentions() -> None:
    for _, kind, template in all_templates():
        uses_ticket = "ticket_id" in template_fields(template)
        assert uses_ticket == (kind == JustificationKind.ticket_mention), template


def test_beneficiary_only_and_always_in_on_behalf() -> None:
    for _, kind, template in all_templates():
        uses_beneficiary = "beneficiary_first" in template_fields(template)
        assert uses_beneficiary == (kind == JustificationKind.on_behalf), template


def test_no_justification_names_a_manager() -> None:
    """A made-up manager name could contradict the real manager in the case view."""
    for _, _, template in all_templates():
        assert "manager_first" not in template_fields(template), template


def test_ticket_mention_contains_ticket_verbatim() -> None:
    rng = random.Random(7)
    for split in Split:
        for _ in range(100):
            text, _ = render_justification(
                rng, split=split, slots=SLOTS, kind=JustificationKind.ticket_mention
            )
            assert "INC-20417" in text


def test_many_noisy_renders_stay_clean() -> None:
    rng = random.Random(2024)
    for split in Split:
        for _ in range(1500):
            text, _ = render_justification(rng, split=split, slots=BASE_SLOTS)
            assert_clean(text, allow_ticket=False)


def test_slot_values_survive_noise_verbatim() -> None:
    rng = random.Random(9)
    hits = 0
    for _ in range(1500):
        text, _ = render_justification(rng, split=Split.test, slots=BASE_SLOTS)
        lowered = text.lower()
        assert lowered.count("northwind") == text.count("Northwind Ledger"), text
        assert lowered.count("invoice") == text.count("AP Invoice Entry"), text
        hits += "Northwind Ledger" in text
    assert hits > 300


# Phrases that assert a case fact: a recent hire or team move, a time-bound need, peer adoption.
STRUCTURE_CLAIM = re.compile(
    r"new (?:hire|starter|role|member|to)|i'm new|just (?:joined|started|moved)|"
    r"recently (?:joined|started)|onboard|ramp|first weeks|"
    r"temporar|short[- ]term|one[- ]off|one[- ]time|limited[- ]time|briefly|brief need|"
    r"won't (?:be )?need|until|while (?:they're|the)|covering|cover for|backup|backing up|"
    r"filling in|step in|most people|everyone on",
    re.IGNORECASE,
)


@pytest.mark.parametrize("split", list(Split))
def test_default_kind_never_picks_structure_dependent_kinds(split: Split) -> None:
    rng = random.Random(1)
    for _ in range(2000):
        text, family_id = render_justification(rng, split=split, slots=FIXED_SLOTS, noise=False)
        assert JustificationKind(kind_of(text, family_id)) not in STRUCTURE_DEPENDENT_KINDS


def test_default_kind_templates_assert_no_case_facts() -> None:
    for _, kind, template in all_templates():
        if JustificationKind(kind) in DEFAULT_KIND_WEIGHTS:
            assert not STRUCTURE_CLAIM.search(template), (kind, template)


def test_bank_can_swap_in_unpublished_fresh_families() -> None:
    """SPEC §10.5: a fresh release renders only held-out families supplied at build time."""
    data = {
        group: {kind.value: (f"[{group}] {kind.value} {{app}}",) for kind in JustificationKind}
        for group in ("x", "y")
    }
    held_out = BANK.replace_split(Split.fresh, families_from_mapping("just", Split.fresh, data))
    assert {f.id for f in held_out.select(split=Split.fresh)} == {
        "just.fresh.x",
        "just.fresh.y",
    }
    assert held_out.select(split=Split.test) == BANK.select(split=Split.test)
    rng = random.Random(0)
    for _ in range(50):
        text, fid = render_justification(
            rng, split=Split.fresh, slots=SLOTS, noise=False, bank=held_out
        )
        assert text.startswith(("[x] ", "[y] ")) and "Northwind Ledger" in text
        assert fid in {"just.fresh.x", "just.fresh.y"}


def test_all_default_kinds_are_reachable() -> None:
    rng = random.Random(3)
    kinds = {
        kind_of(*render_justification(rng, split=Split.fresh, slots=FIXED_SLOTS, noise=False))
        for _ in range(3000)
    }
    assert kinds == {k.value for k in DEFAULT_KIND_WEIGHTS}


def test_explicit_kind_is_honoured() -> None:
    rng = random.Random(6)
    for kind in JustificationKind:
        for split in Split:
            for _ in range(20):
                text, fid = render_justification(
                    rng, split=split, slots=FIXED_SLOTS, kind=kind, noise=False
                )
                assert kind_of(text, fid) == kind.value


@pytest.mark.parametrize("seed", range(40))
def test_hard_slots_are_required_on_every_seed(seed: int) -> None:
    """Required slots fail fast, not only when the drawn template happens to use them."""
    with pytest.raises(ValueError, match="beneficiary_first"):
        render_justification(
            random.Random(seed), split=Split.dev, slots=BASE_SLOTS, kind=JustificationKind.on_behalf
        )
    with pytest.raises(ValueError, match="ticket_id"):
        render_justification(
            random.Random(seed), split=Split.dev, slots=BASE_SLOTS, kind="ticket_mention"
        )
    no_app = {"entitlement_name": "AP Invoice Entry"}
    with pytest.raises(ValueError, match="app"):
        render_justification(random.Random(seed), split=Split.test, slots=no_app)
    with pytest.raises(ValueError, match="app, entitlement_name"):
        render_justification(random.Random(seed), split=Split.fresh, slots={}, kind="vague")


def test_unknown_kind_and_split_rejected() -> None:
    with pytest.raises(ValueError):
        render_justification(random.Random(0), split=Split.dev, slots=SLOTS, kind="bogus")
    with pytest.raises(ValueError):
        render_justification(random.Random(0), split="holdout", slots=SLOTS)


def test_noise_false_matches_a_template_exactly() -> None:
    rng = random.Random(4)
    for _ in range(300):
        text, family_id = render_justification(rng, split=Split.dev, slots=FIXED_SLOTS, noise=False)
        kind_of(text, family_id)  # raises unless the text is one of the family's templates


def test_noise_actually_varies_surface_forms() -> None:
    rng = random.Random(8)
    texts = [
        render_justification(rng, split=Split.dev, slots=SLOTS, kind="verbose")[0]
        for _ in range(300)
    ]
    assert any(t[0].isupper() for t in texts)
    assert any(t[0].islower() for t in texts)
    assert any(" i " in t for t in texts)  # whole-text lowercasing hits the literal "I"
    assert any(not t.endswith((".", "!")) for t in texts)
    assert any(re.search(r"\bthx\b|\bpls\b|\bw/|\bu\b", t) for t in texts)
