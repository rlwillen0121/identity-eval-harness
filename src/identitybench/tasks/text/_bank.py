"""Template families, banks and slot filling shared by every text bank.

A *family* is a group of templates assigned to exactly one split, which is how the dev split
never sees test prose and ``fresh`` keeps families that neither public split has seen (SPEC §6.4,
§9.4, §10.5).

Inside a family, templates are grouped by *variant*: the justification kind, ticket type,
conflicting field or description lie. The variant is usually dictated by case structure (an
``on_behalf`` justification exists because requester != beneficiary; a ``read_only`` lie exists
because privilege is write/admin). Every family in a bank therefore carries templates for *every*
variant, and a family is chosen before, and independently of, the variant. That keeps family IDs
free of structured facts: the shortcut probe uses family IDs as surface features (SPEC §9.4), and
an ID that encoded the variant would hand it rule inputs. Family IDs never reach a prompt.
"""

from __future__ import annotations

import random
import string
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from identitybench.schemas.enums import Split

SlotDefault = Callable[[random.Random, Mapping[str, str]], str]
"""Produces a value for a slot the caller didn't supply.

Receives the RNG and every slot value known so far (caller-supplied plus already-resolved
defaults), so name defaults can avoid repeating a name already in the text.
"""

NoiseFn = Callable[[random.Random, list[str]], list[str]]
"""Rewrites a template's literal segments (never slot values) to add surface noise."""

_FORMATTER = string.Formatter()


@dataclass(frozen=True, slots=True)
class Family:
    """A group of templates assigned to exactly one split, with templates for each variant."""

    id: str
    split: Split
    variants: Mapping[str, tuple[str, ...]]

    @property
    def templates(self) -> tuple[str, ...]:
        """Every template in the family, variants in definition order."""
        return tuple(t for templates in self.variants.values() for t in templates)

    def templates_for(self, variant: str) -> tuple[str, ...]:
        """Templates of one variant; raises ``ValueError`` for an unknown variant."""
        try:
            return self.variants[variant]
        except KeyError:
            raise ValueError(f"family {self.id!r} has no variant {variant!r}") from None


FamilyCheck = Callable[[Family], None]
"""Bank-specific validation of one family (e.g. "ticket IDs only in ticket mentions"); raises
``ValueError`` naming the family and template. Runs whenever a bank is built, including on the
held-out families a fresh release swaps in."""


class Bank:
    """An ordered, immutable collection of families with split-aware selection.

    All families must cover the same variants, so choosing a family never depends on the
    variant (see the module docstring). ``allowed_slots`` (the bank's slot defaults plus the slots
    callers supply) and ``check`` validate every template when the bank is built, so a typo such
    as ``{entitlment_name}`` fails on import or when held-out families are loaded, not on the
    seeds that happen to draw that template. Both carry over to :meth:`replace_split`.
    """

    def __init__(
        self,
        name: str,
        families: Sequence[Family],
        *,
        allowed_slots: Collection[str] | None = None,
        check: FamilyCheck | None = None,
    ) -> None:
        if not families:
            raise ValueError(f"{name}: no families")
        variants = tuple(families[0].variants)
        allowed = None if allowed_slots is None else frozenset(allowed_slots)
        seen: set[str] = set()
        for family in families:
            if family.id in seen:
                raise ValueError(f"{name}: duplicate family id {family.id!r}")
            if not family.variants or not all(family.variants.values()):
                raise ValueError(f"{name}: family {family.id!r} has no templates for a variant")
            if tuple(family.variants) != variants:
                raise ValueError(
                    f"{name}: family {family.id!r} covers {sorted(family.variants)}, "
                    f"expected {sorted(variants)}"
                )
            if allowed is not None:
                for template in family.templates:
                    problems = template_problems(template, allowed)
                    if problems:
                        raise ValueError(
                            f"{name}: family {family.id!r}, template {template!r}: "
                            + "; ".join(problems)
                        )
            if check is not None:
                check(family)
            seen.add(family.id)
        self.name = name
        self.families: tuple[Family, ...] = tuple(families)
        self.variants: tuple[str, ...] = variants
        self.allowed_slots: frozenset[str] | None = allowed
        self.check: FamilyCheck | None = check

    def select(self, *, split: Split) -> list[Family]:
        """Families in ``split``, in definition order."""
        return [f for f in self.families if f.split == split]

    def choose(self, rng: random.Random, *, split: Split) -> Family:
        """Pick one family uniformly among those in ``split``."""
        candidates = self.select(split=split)
        if not candidates:
            raise ValueError(f"{self.name}: no family for split={split}")
        return rng.choice(candidates)

    def template_count(self) -> int:
        return sum(len(f.templates) for f in self.families)

    def replace_split(self, split: Split, families: Sequence[Family]) -> Bank:
        """A copy of this bank whose ``split`` families are replaced by ``families``.

        This is how a fresh release uses held-out prose that was never published (SPEC §10.5):
        the maintainer loads unpublished families (e.g. with :func:`families_from_mapping`) and
        swaps them in for the placeholder ``fresh`` families shipped in the source tree. The new
        families must belong to ``split`` and cover exactly this bank's variants (in any order;
        they are reordered to match), and they pass the same slot and family checks as the
        shipped ones, so a bad held-out template fails here rather than mid-build.
        """
        if not families:
            raise ValueError(f"{self.name}: no replacement families for split={split}")
        replacements: list[Family] = []
        for family in families:
            if family.split != split:
                raise ValueError(f"{self.name}: family {family.id!r} is not in split={split}")
            if set(family.variants) != set(self.variants):
                raise ValueError(
                    f"{self.name}: family {family.id!r} covers {sorted(family.variants)}, "
                    f"expected {sorted(self.variants)}"
                )
            ordered = {variant: family.variants[variant] for variant in self.variants}
            replacements.append(Family(family.id, split, ordered))
        kept = [f for f in self.families if f.split != split]
        return Bank(
            self.name,
            [*kept, *replacements],
            allowed_slots=self.allowed_slots,
            check=self.check,
        )


def coerce_split(split: Split | str) -> Split:
    """Accept a ``Split`` or its string value; reject anything else early."""
    return Split(split)


def template_fields(template: str) -> tuple[str, ...]:
    """Slot names a template uses, in order of first appearance."""
    names = (field for _, field, _, _ in _FORMATTER.parse(template) if field is not None)
    return tuple(dict.fromkeys(names))


def template_problems(template: str, allowed: Collection[str]) -> list[str]:
    """Why ``template`` can't be filled from ``allowed`` slot names (empty when it can).

    Flags unbalanced braces, slots outside ``allowed`` (typos, positional ``{}``, attribute or
    index access) and format specs or conversions, which :func:`fill` refuses.
    """
    try:
        parts = list(_FORMATTER.parse(template))
    except ValueError as exc:
        return [f"malformed template ({exc})"]
    problems: list[str] = []
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if field not in allowed:
            problems.append(f"unknown slot {{{field}}}")
        if spec or conversion:
            problems.append(f"slot {{{field}}} must not use a format spec or conversion")
    return problems


def require_slots(slots: Mapping[str, object], required: Sequence[str], *, what: str) -> None:
    """Raise ``ValueError`` unless every ``required`` slot is present and not ``None``.

    Callers check before drawing any randomness, so a missing slot fails on every seed instead
    of only on the seeds whose template happens to use it.
    """
    missing = [name for name in required if slots.get(name) is None]
    if missing:
        raise ValueError(f"{what}: missing required slot(s) {', '.join(missing)}")


def fill(
    template: str,
    slots: Mapping[str, object],
    rng: random.Random,
    *,
    defaults: Mapping[str, SlotDefault],
    noise: NoiseFn | None = None,
) -> str:
    """Render ``template`` with ``slots``, falling back to ``defaults`` for missing slots.

    Noise touches only the literal text between slots, so app and entitlement names (which a
    model may need to match against structured fields) always come through verbatim. A slot that
    is neither supplied nor defaulted raises ``ValueError`` rather than leaking ``{slot}`` text.
    """
    parts = list(_FORMATTER.parse(template))
    literals = [literal for literal, _, _, _ in parts]
    if noise is not None:
        literals = noise(rng, literals)
    known = {k: str(v) for k, v in slots.items() if v is not None}
    out: list[str] = []
    for literal, (_, field, spec, conversion) in zip(literals, parts, strict=True):
        out.append(literal)
        if field is None:
            continue
        if spec or conversion:
            raise ValueError(f"template slot {field!r} must not use a format spec: {template!r}")
        if field not in known:
            default = defaults.get(field)
            if default is None:
                raise ValueError(f"missing slot {field!r} for template {template!r}")
            known[field] = default(rng, known)
        out.append(known[field])
    return "".join(out)


def families_from_mapping(
    prefix: str,
    split: Split,
    data: Mapping[str, Mapping[str, Sequence[str]]],
) -> list[Family]:
    """Families from plain data, ``{group: {variant: [template, ...]}}``, e.g. parsed YAML.

    Family IDs are ``{prefix}.{split}.{group}`` like those of :func:`grouped_families`;
    :meth:`Bank.replace_split` checks the variants, slot names and bank rules against the bank.
    """
    return [
        Family(
            f"{prefix}.{split.value}.{group}",
            split,
            {variant: tuple(templates) for variant, templates in variants.items()},
        )
        for group, variants in data.items()
    ]


def grouped_families[V: str](
    prefix: str,
    templates: Mapping[tuple[V, Split], Sequence[str | tuple[str, ...]]],
    groups: Mapping[Split, Sequence[str]],
) -> list[Family]:
    """Deal each ``(variant, split)`` entry list round-robin into that split's groups.

    ``groups[split]`` names the families of a split; family IDs are ``{prefix}.{split}.{group}``.
    An entry is one template, or a tuple of alternatives that all go to the same family (e.g. a
    template naming a team plus a sibling that doesn't, for callers who can't supply the team).
    Round-robin dealing keeps every family covering every variant as long as each variant has at
    least as many entries per split as the split has groups (checked by ``Bank``).
    """
    dealt: dict[tuple[Split, str], dict[str, list[str]]] = {}
    for split, names in groups.items():
        for name in names:
            dealt[split, name] = {}
    for (variant, split), entries in templates.items():
        names = groups[split]
        for i, entry in enumerate(entries):
            alternatives = (entry,) if isinstance(entry, str) else entry
            dealt[split, names[i % len(names)]].setdefault(str(variant), []).extend(alternatives)
    return [
        Family(
            f"{prefix}.{split.value}.{name}",
            split,
            {variant: tuple(items) for variant, items in variants.items()},
        )
        for (split, name), variants in dealt.items()
    ]
