"""Per-world entitlement descriptions (``world.descriptions``; SPEC §6.4, §9.4).

The point of wording descriptions per world is that their length carries nothing: within a
world it is independent of every policy-relevant attribute, and across worlds it doesn't name
the entitlement. These tests check the mechanism (every kind of entitlement lands on every
target), the outcome (no attribute moves the length, no entitlement keeps its length across
worlds) and the content rules (no policy facts, SoD sides always say what they do).
"""

from __future__ import annotations

import math
import random
import re
import statistics
from collections.abc import Callable

import pytest

from identitybench.world import catalog as catalog_module
from identitybench.world.apps_data import APP_SPECS
from identitybench.world.catalog import build_catalog
from identitybench.world.descriptions import (
    _CONTEXTS,
    _GENERIC_CORES,
    _MODULE_PHRASES,
    _MODULE_SENTENCES,
    _VERB_FRAMES,
    SOD_PHRASES,
    TARGET_MAX,
    TARGET_MIN,
    Wording,
    describe,
    describe_catalog,
    target_length,
)
from identitybench.world.entities import Entitlement, Privilege, RiskTier
from identitybench.world.profiles import PROFILES
from identitybench.world.specs import SOD_DEFINITIONS, SOD_FUNCTIONS


def _kind(ent: Entitlement, wording: Wording) -> str:
    if set(ent.functions) & SOD_FUNCTIONS:
        return "sod"
    if wording.action is not None:
        return "module"
    return "admin" if ent.privilege is Privilege.admin else "hand-written"


@pytest.mark.parametrize("profile", ["startup", "midmarket", "enterprise"])
def test_every_kind_of_entitlement_lands_on_every_target(profile: str) -> None:
    """The mechanism behind independence: whatever the entitlement (a generated module
    entitlement, a hand-written one, an administrator seat, an SoD side), its description lands
    within a few characters of any target the distribution can draw, with no kind running
    systematically long or short."""
    apps, ents, wordings = catalog_module._build_apps(PROFILES[profile], random.Random(5))
    names = {app_id: app.name for app_id, app in apps.items()}
    rng = random.Random(1)
    misses: dict[str, list[int]] = {}
    for ent in ents.values():
        for target in range(TARGET_MIN, TARGET_MAX + 1, 10):
            text = describe(ent, wordings[ent.id], names[ent.app_id], target, rng)
            misses.setdefault(_kind(ent, wordings[ent.id]), []).append(len(text) - target)
    # A startup's only administrator seats are the IdP's, which sit on SOD-004's side.
    assert {"sod", "module", "hand-written"} <= set(misses)
    for kind, values in misses.items():
        assert abs(statistics.fmean(values)) < 0.6, kind
        assert sum(abs(v) <= 4 for v in values) / len(values) > 0.9, kind
        assert max(abs(v) for v in values) <= 16, kind


def test_target_lengths_follow_the_documented_distribution() -> None:
    rng = random.Random(3)
    targets = [target_length(rng) for _ in range(20000)]
    assert min(targets) >= TARGET_MIN and max(targets) <= TARGET_MAX
    assert 62 < statistics.fmean(targets) < 70
    assert 40 < statistics.median(targets) < 65


def _smd(a: list[int], b: list[int]) -> tuple[float, float]:
    """Standardized mean difference and its standard error under no difference."""
    pooled = math.sqrt((statistics.pvariance(a) + statistics.pvariance(b)) / 2)
    return (statistics.fmean(a) - statistics.fmean(b)) / pooled, math.sqrt(1 / len(a) + 1 / len(b))


SPLITS: dict[str, Callable[[Entitlement], bool]] = {
    "high or critical risk": lambda e: e.risk_tier in (RiskTier.high, RiskTier.critical),
    "critical risk": lambda e: e.risk_tier is RiskTier.critical,
    "admin privilege": lambda e: e.privilege is Privilege.admin,
    "read privilege": lambda e: e.privilege is Privilege.read,
    "privileged": lambda e: e.privileged,
    "requestable": lambda e: e.requestable,
    "maximum duration": lambda e: e.max_duration_days is not None,
    "SoD side": lambda e: bool(set(e.functions) & SOD_FUNCTIONS),
}
"""Attributes a policy rule reads (APPR-001, ELIG-001, TEMP-001/002, ELIG-004, SOD-*)."""


@pytest.mark.parametrize("profile", ["startup", "midmarket"])
def test_length_is_independent_of_policy_attributes(profile: str) -> None:
    """Pooled over several worlds' catalogs, no policy attribute shifts the mean description
    length by more than three standard errors. With fixed catalog text the shifts were 0.4 to
    3.8 standard deviations (administrator seats longest, non-requestable entitlements
    longer, SoD sides longer)."""
    ents = [
        e
        for seed in range(12)
        for e in build_catalog(PROFILES[profile], random.Random(seed)).entitlements.values()
    ]
    for name, split in SPLITS.items():
        inside = [len(e.description) for e in ents if split(e)]
        outside = [len(e.description) for e in ents if not split(e)]
        if len(inside) < 20 or len(outside) < 20:
            continue
        smd, se = _smd(inside, outside)
        assert abs(smd) < 3 * se, (name, round(smd, 3), round(se, 3))


def test_wording_and_length_change_between_worlds() -> None:
    """The same entitlement gets other words and another length in another world, so a length
    learned on dev worlds names nothing in test worlds."""
    first = build_catalog(PROFILES["midmarket"], random.Random(101)).entitlements
    second = build_catalog(PROFILES["midmarket"], random.Random(102)).entitlements
    shared = sorted(set(first) & set(second))
    assert len(shared) > 600
    same_text = sum(first[e].description == second[e].description for e in shared)
    assert same_text / len(shared) < 0.05
    a = [len(first[e].description) for e in shared]
    b = [len(second[e].description) for e in shared]
    assert abs(statistics.correlation(a, b)) < 0.1


def test_descriptions_are_deterministic_and_drawn_in_id_order() -> None:
    """Same stream, same words; and the order the catalog was built in doesn't matter."""
    apps, ents, wordings = catalog_module._build_apps(PROFILES["startup"], random.Random(2))
    names = {app_id: app.name for app_id, app in apps.items()}
    once = describe_catalog(ents, wordings, names, random.Random(9))
    reversed_ents = dict(reversed(list(ents.items())))
    twice = describe_catalog(reversed_ents, wordings, names, random.Random(9))
    assert once == twice
    assert list(twice) == list(reversed_ents)
    assert all(e.description for e in once.values())


def test_sod_sides_always_lead_with_their_function() -> None:
    """An SoD-side entitlement's description always says what its function does, whatever
    the target: it leads with one of the function's phrases (possibly after its name)."""
    assert set(SOD_PHRASES) == {f for d in SOD_DEFINITIONS for f in (d.function_a, d.function_b)}
    for seed in range(10):
        catalog = build_catalog(PROFILES["midmarket"], random.Random(seed))
        for rule in catalog.sod:
            for ent_id in (*rule.side_a, *rule.side_b):
                ent = catalog.entitlements[ent_id]
                lead = ent.description.split(". ")[0].lower()
                phrases = [p for f in ent.functions for p in SOD_PHRASES.get(f, ())]
                assert any(p in lead for p in phrases), (ent_id, ent.description)


POLICY_WORDS = re.compile(
    r"\b(privileged|confidential|restricted|critical|high[- ]risk|low[- ]risk|requestable|"
    r"sox|time-boxed|temporary|expires?|\d+[- ]days?|employees? only|contractors?|interns?|"
    r"vendor staff|eligible|approval required)\b",
    re.IGNORECASE,
)
"""Words that would state a fact a structured field carries (risk tier, classification, the
privileged flag, requestability, duration, eligibility, SOX scope, SoD membership)."""


def test_no_frame_states_a_policy_fact() -> None:
    frames = [
        *_VERB_FRAMES,
        *(f for pool in _CONTEXTS for f in pool),
        *(f for group in _GENERIC_CORES.values() for f in group),
        *(f for group in _MODULE_PHRASES.values() for f in group),
        *(f for group in _MODULE_SENTENCES.values() for f in group),
        *(p for group in SOD_PHRASES.values() for p in group),
    ]
    for frame in frames:
        assert not POLICY_WORDS.search(frame), frame


def test_hand_written_sentences_state_no_policy_fact() -> None:
    """The catalog's own sentences say what the access does, not who may hold it, how long,
    how sensitive or whether it is in SOX scope (those are structured fields). An application's
    name ("Privileged Access Management") is a name, not a claim."""
    for spec in APP_SPECS:
        for ent in spec.entitlements:
            match = POLICY_WORDS.search(ent.description.replace(spec.name, spec.key))
            assert match is None, (ent.id, match.group(0) if match else None)


def test_frames_match_the_privilege_they_describe() -> None:
    """A read entitlement is never described as changing anything, and an approver never as
    creating or editing what it approves (that would read like the other side of an approval).
    Context sentences and verb frames fit any privilege."""
    changes = re.compile(
        r"\b(create|update|delete|edit|change|maintain|add|remove|write|admin\w*|modify)\b", re.I
    )
    read_frames = [
        *_GENERIC_CORES[Privilege.read],
        *_MODULE_PHRASES["view"],
        *_MODULE_PHRASES["export"],
        *_MODULE_SENTENCES["view"],
        *_MODULE_SENTENCES["export"],
    ]
    neutral = [*_VERB_FRAMES, *(f for pool in _CONTEXTS for f in pool)]
    for frame in read_frames + neutral:
        assert not changes.search(frame), frame
    for frame in (*_MODULE_PHRASES["approve"], *_MODULE_SENTENCES["approve"]):
        assert not re.search(r"\b(create|edit|update|delete)\b", frame, re.I), frame
