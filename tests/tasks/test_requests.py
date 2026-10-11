"""Tests for the access-request builder (``tasks.requests``) and its scenario templates
(``tasks.request_scenarios``), on the session's midmarket and startup worlds (``conftest``), on
``build_world("midmarket", 101)`` and on a fleet of fresh midmarket and startup worlds.

A 300-case build is made once per module for each of two midmarket worlds (it is also the timing
test) and shared by every test that reads a whole build: the gold mix and each decision's tiers,
coverage of every request rule and of the named structures (nested roles, multi-source access,
expired exceptions, break-glass valid and failing each way, every kind of requester, every type
an eligibility rule bars, cap-binding and request-binding approvals), answerability through a
JSON round trip, faithfulness to the world, free text and nuisance balance. Two worlds keep the
expectations from fitting one world's material.

The fleet (``fleet``) is built the way a v1 split is: 60-case builds of midmarket worlds and
30-case builds of startup worlds, plus dev splits of 30 + 30 + 15 cases. Its worlds are seeds no
release split uses and that no builder setting was tuned or measured on (``FLEET_WORLDS``), so
the tests that read it check that the shortcut defenses carry to fresh worlds instead of fitting
the worlds measured while tuning: no nuisance feature leans toward a label across worlds, no two
labels differ in closure sizes or description lengths within L1, L2 and their break-glass and
other requests, employment types spread over the labels as far as the templates allow, and
shortcut probes trained on pooled dev worlds pass the SPEC §9.4 gate on held-out worlds. Their
bounds are fixed numbers set from measurements on other fleets, of this builder and of the
earlier versions each test is meant to catch; none is estimated from the cases under test, since
a worse builder that spread its cases more would then widen its own bound. Expectations are
restated from SPEC §6.3, §6.4, §7.4, §8.1, §9.1-§9.4 and §10, not read back from the builder's
code.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import identitybench.tasks.requests as requests_module
from identitybench.dates import is_valid_on
from identitybench.policy.access import held_access
from identitybench.policy.constants import BREAK_GLASS_CAP_DAYS
from identitybench.policy.oracle import decide_request
from identitybench.policy.rules import request_rules
from identitybench.probe.features import REQUEST_FEATURES, request_features
from identitybench.probe.shortcut import MAX_DELTA_POINTS, evaluate_probe, train_probe
from identitybench.schemas.case import AccessRequest, Case, CaseView
from identitybench.schemas.enums import Effect, RequestDecision, Split, Suite, Task, Tier
from identitybench.schemas.gold import RequestGold
from identitybench.tasks.request_scenarios import (
    BARRED_TYPES,
    CLOSURE_SIZES,
    CONDITION_RULES,
    DEFAULT_MAX_ENTITLEMENT_USES,
    DEFAULT_MAX_USES,
    DENY_RULES,
    DURATION_CHOICES,
    ESCALATE_RULES,
    GLASS_DURATIONS,
    SCARCE_CAP_FACTOR,
    SCENARIOS,
    STANDING_SHARE,
    Ask,
    Draft,
    Material,
    Requester,
    Scenario,
    approval_duration,
    glass_duration,
    placed_duration,
    scenario_named,
)
from identitybench.tasks.requests import (
    BALANCE_MIN_CASES,
    BALANCE_SMD_LIMIT,
    BREAK_GLASS_SHARE,
    BREAK_GLASS_TIERS,
    COVERAGE_SHARE,
    DEFAULT_MIX,
    DURATION_FEATURES,
    RETIRE_AFTER_NONES,
    Cell,
    RequestBuildError,
    RequestBuildReport,
    Shortfall,
    balance_report,
    build_request_cases,
    build_request_cases_with_report,
    first_name,
    plan_cells,
)
from identitybench.tasks.text import JustificationKind
from identitybench.tasks.text.justifications import BANK
from identitybench.tasks.tiers import compute_tags, compute_tier
from identitybench.tasks.view_builder import ViewBuilder
from identitybench.world.entities import (
    NON_EMPLOYEES,
    AccountKind,
    AccountStatus,
    EmploymentType,
    IdentityStatus,
    TicketStatus,
    TicketType,
    World,
)
from identitybench.world.generator import build_world

SEED = 7
BIG = 300
WORLD_SEEDS = (7, 101)
"""The session's midmarket world, and the v1 spec's first dev world, which unlike it holds open
incidents nobody is assigned to (the unassigned break-glass failure)."""
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_BUILD_SECONDS = 60.0
MIX_TOLERANCE = 0.05
TIER_FLOORS: Mapping[RequestDecision, Mapping[Tier, float]] = {
    RequestDecision.APPROVE: {Tier.L1: 0.50, Tier.L2: 0.15, Tier.L3: 0.08},
    RequestDecision.DENY: {Tier.L1: 0.15, Tier.L2: 0.30, Tier.L3: 0.30},
    RequestDecision.ESCALATE: {Tier.L1: 0.20, Tier.L2: 0.35, Tier.L3: 0.12},
}
"""Least share of each decision's cases per tier in a 300-case midmarket build. Midmarket worlds
approve at L2 and L3 almost only through break-glass and the catalog's two approvable privileged
entitlements, and escalate at L3 only over conflicts a control mitigates (``tasks.requests``
docstring), so these are what the material supports (with a margin), not the 30/35/35 plan."""
TIER_MIN_SHARE = 0.18
"""Least share of a midmarket build in each tier."""
TIER_ACCURACY_CEILING = 0.6
"""Most share of a midmarket build whose gold decision its tier alone gives (the tier's most
common decision). The exchanges that keep the tier mix near plan pile a short decision's cases
into other tiers, entangling tiers with labels; 300-case midmarket builds sit at 0.51 to 0.55
against a majority class of 0.35 (60-case builds, as a v1 split makes, at about 0.46), and
per-tier results are confounded with the label that far."""
RULE_FLOOR = 4
"""Least firings of every request rule in a 300-case midmarket build. The plan reserves five
cases for each eligibility rule's template and rotates SoD draws over the rules, so each rule
fires five times or more in these worlds; the floor leaves a case of margin."""

REQUEST_RULE_IDS = frozenset(rule.id for rule in request_rules())
"""Every rule with a request predicate: LIFE-001..004, ELIG-001..005, SOD-001..006, MIT-003,
APPR-001..004, TEMP-001, TEMP-002 and JUST-001 (MIT-001 is an exception, not a finding)."""

COLLECTIONS = (
    "identities",
    "applications",
    "entitlements",
    "roles",
    "memberships",
    "accounts",
    "assignments",
    "usage",
    "mitigating_controls",
    "tickets",
)

WORLD_DEPENDENT = frozenset(
    {
        ("approval_on_the_edge", Tier.L3),
        ("escalation_on_the_edge", Tier.L3),
        ("contractor_term", Tier.L3),
        ("lapsed_conflict_approval", Tier.L1),
        ("mitigated_approval", Tier.L3),
        ("break_glass_sod", Tier.L2),
        ("break_glass_sod", Tier.L3),
        ("break_glass_unassigned", Tier.L1),
        ("break_glass_on_leave", Tier.L2),
        ("time_boxed_approval", Tier.L1),
        ("time_boxed_approval", Tier.L2),
    }
)
"""(template, tier) cells whose material ``build_world("midmarket", 7)`` lacks or holds once at
most (no contract ends within a week, no approvable SoD side lapsed, no valid control mitigating
a conflict that nothing else stands in the way of, no incident assignee who could complete an
unmitigated SoD conflict on the incident's applications, no open incident without an assignee or
assigned to someone on leave, no low- or medium-risk entitlement with a maximum duration): these
may only report that their material ran out. ``test_templates_for_material_other_worlds_hold``
gives the last two theirs. Two kinds are world-dependent among non-release midmarket worlds:
approvable mitigated conflicts (one or two beneficiaries in 11 of 27 worlds;
``build_world("midmarket", 101)`` holds one) and incident assignees who could complete an
unmitigated conflict (one in 5 of 16 worlds, 101 among them)."""

PRESENCE_FEATURES = (
    "has_ticket_ref",
    "has_tickets",
    "has_controls",
    "on_behalf",
    "has_requested_duration",
    "is_break_glass",
)

FLEET_WORLDS: tuple[int, ...] = tuple(range(601, 613))
"""Midmarket worlds of the fleet's test-like builds. No release split uses them (the v1 spec's
dev worlds are 101-103, its test worlds 2201-2206), and no builder setting was tuned or measured
on them or on the fleet's other worlds. The builder was measured on other non-release worlds
(11-43, an earlier version of this fleet, shifted copies of it, and a few hundred more), and the
bounds below were set from four fleets of eight of those worlds before this fleet was first
built."""
FLEET_SEEDS: tuple[int, ...] = (1, 2, 3, 4)
"""Build seeds per fleet world: four builds of each world, 48 builds of 60 cases in all. Twelve
worlds rather than eight because worlds, not builds, are the independent draws a release makes,
and a lean's standard error over the fleet is what the fixed bounds below leave room for."""
FLEET_N = 60
"""Cases per midmarket fleet build: a v1 test split takes 60 request cases from each midmarket
world (``datasets/specs/identitybench-v1.yaml``)."""
FLEET_STARTUP_WORLDS: tuple[int, ...] = (631, 632)
FLEET_STARTUP_N = 30
"""Cases per startup fleet build, as a v1 test split takes from each startup world."""
DEV_POOLS: tuple[tuple[int, int, int], ...] = (
    (621, 622, 623),
    (624, 625, 626),
    (627, 628, 629),
    (641, 642, 643),
)
"""(midmarket, midmarket, startup) worlds of each shortcut probe's dev split, built with 30, 30
and 15 cases as the v1 dev split is."""

NUISANCE_TOLERANCE = 0.1
"""Largest lean (signed SMD, APPROVE minus the other labels, averaged over each world's builds and
then over the fleet's worlds) a nuisance feature may have, unless ``LEAN_BOUNDS`` says otherwise.
A feature that leans the same way in every world carries from dev to test (SPEC §9.4), but one
that leans by 0.1 standard deviations moves a threshold on it past the majority class by well
under a point of accuracy.

On five fresh fleets of the same shape and size the features this bound covers lean by 0.07 at
most: the requested entitlement's description length -0.002 to +0.070 (mean +0.036, a fleet
mean's standard error 0.017 to 0.026, so the bound sits about three standard errors beyond the
mean), the description total -0.01 to -0.05, the closure counts 0 to -0.06. The two description
features had bounds of their own (0.25 and 0.12) while catalog text was fixed per profile:
approvals' descriptions then ran 0.14 to 0.21 standard deviations shorter in every world, since
the catalog's longest descriptions belonged to high-risk, administrative and SoD-side
entitlements. Each world now words its catalog afresh (``world.descriptions``), so they share
this bound. The one closure-driven feature that leans further gets its own (``LEAN_BOUNDS``).
The builder this test was written to catch, which balanced closure sizes only by their bands and
description length only by its band, leaned -0.12 to -0.20 in ``view_role_entitlement_links``,
-0.11 to -0.19 in ``view_usage_records`` and -0.12 to -0.19 in the description total on four
fleets of eight worlds."""
NOISY_FEATURES: tuple[str, ...] = (
    "view_identities",
    "view_tickets",
    "view_controls",
    "ticket_summary_chars",
    "justification_chars",
    "justification_words",
)
"""Features whose lean swings widely from build to build (a standard deviation of 0.14 to 0.26
per 60-case build, against 0.06 to 0.08 for closure sizes): free text drawn from template kinds
that run from a few words to a long paragraph (``tasks.text``), and the distractor identities,
tickets and controls a view gets. Builds of one world share what it offers (its names, its
departments, its distractors), so a fleet's mean swings by a standard deviation of 0.06 to 0.07
between fleets for the widest of them, and they get ``NOISY_TOLERANCE``. Measured on five fresh
fleets: ``view_identities`` -0.12 to +0.02, ``justification_words`` -0.11 to +0.06,
``justification_chars`` -0.06 to +0.09 (+0.05 over 120 fresh worlds: approvals request
entitlements with longer names, which justifications quote; ``tasks.requests``, residual
signals), distractor tickets and controls and ticket summaries within 0.05."""
NOISY_TOLERANCE = 0.2
"""About three standard deviations of a fleet's mean beyond the mean of the measured leans of
``NOISY_FEATURES``. At 0.15 (and 0.18 for justification length) the bound sat about two standard
deviations out: one of the five fresh fleets reached -0.12 in ``view_identities`` and another
-0.11 in ``justification_words``."""
LEAN_BOUNDS: Mapping[str, float] = {
    **dict.fromkeys(NOISY_FEATURES, NOISY_TOLERANCE),
    "view_assignments": 0.12,
    "has_requested_duration": 0.07,
}
"""Per-feature bounds other than ``NUISANCE_TOLERANCE``, each from measurements on fresh fleets of
this one's shape (twelve worlds, four 60-case builds each), set about three standard deviations
of a fleet's mean beyond the mean of the measured leans, so that an upstream change that
reshuffles the worlds' draws doesn't fail them by chance.

- ``view_assignments``: -0.05 to -0.09 over five fleets (mean -0.07, a standard deviation of 0.013
  between fleets), negative in nearly every world: the residual ``tasks.requests`` describes
  (L3's SoD material is granted directly more often than anything else, and approvals sit at L3
  least).
- ``has_requested_duration``: +0.005 to +0.011 over five fleets, now that every break-glass
  request names a duration (``request_scenarios.GLASS_DURATIONS``); -0.014 to -0.025 before, and
  -0.10 to -0.12 when ``_balance_flag`` rounded each (tier, label) cell to the nearest count,
  which gave denials and escalations more durations than the approvals they copy."""
CLOSURE_FEATURES: tuple[str, ...] = (
    "view_accounts",
    "view_memberships",
    "view_assignments",
    "view_roles",
    "view_role_child_links",
    "view_role_entitlement_links",
    "view_entitlements",
    "view_applications",
    "view_usage_records",
    "entitlement_description_chars",
    "entitlement_description_chars_total",
)
"""The probe features a request's beneficiary and entitlement decide: one per count of
``request_scenarios.CLOSURE_SIZES`` (a request view holds the beneficiary's closure and little
else) and the requested entitlement's description lengths. Draws balance these by their
profiles; ticket, control and identity counts come from distractors drawn independently of the
label, and in a break-glass stratum of 48 cases a label chance alone moves them by 0.2 standard
deviations."""
PAIR_MIN_CASES = 20
"""Least cases each of two labels needs in a stratum for the within-stratum check to compare
them."""
OTHER_REQUESTS_TOLERANCE = 0.6
"""Largest standardized difference between two gold labels in a ``CLOSURE_FEATURES`` feature
among a tier's requests other than break-glass (L1 and L2, pooled over the fleet): the strata
draws balance most directly (SPEC §6.4 tier x tags), with 140 to 480 cases a label, so a standard
error near 0.1. Measured on four fleets of eight fresh worlds the largest is 0.45 (L1's denials
against its escalations in role-child links, 0.30 to 0.45). The builder whose draws didn't see
their tier's shortfall reached 0.67 to 1.06 between L2's denials and escalations on five fleets,
this one included."""
WITHIN_TIER_TOLERANCE = 0.7
"""The same for a tier as a whole and for its break-glass requests, each looser for a measured
reason. A whole tier mixes its two kinds of request unevenly by label (break-glass is about a
tenth of L1's approvals, a fifth of its denials and none of its escalations), and a break-glass
stratum holds 48 cases a label, a standard error near 0.2. Measured on four fleets: whole tiers
at most 0.53 (L1's denials against its escalations), break-glass strata 0.37. The builder whose
draws measured a cell's break-glass and other requests together reached 0.91 to 1.64 between L1's
break-glass approvals and denials on five fleets, this one included (``tasks.requests``,
``_Filler._wanted``)."""
TYPE_POOLED_GAPS: Mapping[str, float] = {
    "employee": 0.30,
    "contractor": 0.38,
    "intern": 0.12,
    "vendor": 0.08,
}
"""Largest gap between two labels' shares of each employment type over the whole fleet, set about
three standard deviations of a fleet's gap beyond the gaps measured on five fresh fleets of its
shape (standard errors near 0.01 to 0.02 over 2,880 cases), so a drift of a few points fails.

- Contractors: approvals hold them 0.44 to 0.47 of the time against 0.20 to 0.21 for denials and
  0.13 to 0.15 for escalations (``tasks.requests``, residual signals: a midmarket approval at L2
  without break-glass needs a contractor's cap next to privileged access's, and a contractor's
  denial or escalation is never L1), gaps of 0.29 to 0.33 (0.25 to 0.32 for earlier builders on
  other fleets).
- Employees, their complement: gaps of 0.22 to 0.24.
- Interns: 0.01 to 0.02 of approvals against 0.09 to 0.11 of denials, whose eligibility templates
  rotate through the types ELIG-001 and ELIG-002 bar (``request_scenarios.BARRED_TYPES``), and
  0.04 to 0.06 of escalations: gaps of 0.07 to 0.09.
- Vendors: 0.035 to 0.05 of approvals against 0.06 to 0.07 of denials and 0.08 to 0.10 of
  escalations: gaps of 0.04 to 0.06.

Interns and vendors hold small closures, so their shares are part of what the view sizes tell a
probe trained on far more dev cases than a release holds (``tasks.requests``, residual
signals)."""
PROBE_SPREAD_POINTS = 3
"""How far one probe may exceed the SPEC §9.4 gate. A probe trained on a v1-sized dev split (75
cases) swings with its sample: on five fresh fleets of this one's shape the twenty probes ranged
from -1.8 to +1.6 points, and thirty v1-shaped stand-in splits gave deltas of -0.3 on average
(standard deviation 2.3, at most +4.3). With catalog text fixed per profile, sixteen dev splits
on each of two held-out pools gave means of 0.4 and 1.1 points (maxima 3.0 and 4.8), and one of
four probes on a shifted fleet reached +6.7. The spread leaves room for a worse sample than any
seen with per-world catalog text while the mean of four still has to pass."""

# --------------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    world: World
    cases: list[Case]
    report: RequestBuildReport
    seconds: float


@pytest.fixture(scope="module", params=WORLD_SEEDS, ids=[f"midmarket-{s}" for s in WORLD_SEEDS])
def build300(request: pytest.FixtureRequest, midmarket_world: World) -> Build:
    world_seed: int = request.param
    world = midmarket_world if world_seed == midmarket_world.seed else build_world("midmarket", 101)
    start = time.perf_counter()
    cases, report = build_request_cases_with_report(world, split=Split.test, n=BIG, seed=SEED)
    return Build(world, cases, report, time.perf_counter() - start)


def _request(case: Case) -> AccessRequest:
    request = case.view.request
    assert request is not None
    return request


def _gold(case: Case) -> RequestGold:
    assert isinstance(case.gold, RequestGold)
    return case.gold


def _findings(case: Case) -> set[str]:
    return {f.rule for f in _gold(case).findings}


def _world_ids(world: World) -> set[str]:
    ids: set[str] = set()
    for name in COLLECTIONS:
        ids.update(getattr(world, name))
    ids.update(a.request_id for a in world.assignments.values() if a.request_id is not None)
    return ids


def _jsonl(cases: Sequence[Case]) -> str:
    return "\n".join(case.model_dump_json() for case in cases)


def _shares(cases: Sequence[Case]) -> dict[RequestDecision, float]:
    counts = Counter(_gold(c).decision for c in cases)
    return {d: counts[d] / len(cases) for d in RequestDecision}


def _rows(report: RequestBuildReport) -> dict[tuple[object, object], dict[str, object]]:
    return {(row["stratum"], row["feature"]): row for row in report.balance}


def fingerprint(world: World, n: int, seed: int) -> str:
    """SHA-256 of a build's JSONL (imported by the hash-order test's subprocesses)."""
    cases = build_request_cases(world, split=Split.test, n=n, seed=seed)
    return hashlib.sha256(_jsonl(cases).encode()).hexdigest()


# --------------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------------


def test_plan_counts_are_exact_and_each_slot_names_a_template_for_its_cell() -> None:
    slots = plan_cells(BIG)
    assert len(slots) == BIG
    decisions = Counter(cell.decision for cell, _ in slots)
    assert decisions == {
        RequestDecision.APPROVE: 105,
        RequestDecision.DENY: 105,
        RequestDecision.ESCALATE: 90,
    }
    for decision, count in decisions.items():
        tiers = Counter(cell.tier for cell, _ in slots if cell.decision is decision)
        for tier, share in requests_module.TIER_MIX.items():
            assert abs(tiers[tier] - share * count) <= 1
    for cell, scenario in slots:
        assert scenario.decision is cell.decision
        assert cell.tier in scenario.tiers
    assert plan_cells(BIG) == slots


def test_plan_gives_every_decision_the_same_break_glass_share() -> None:
    """``is_break_glass`` is a probe feature (SPEC §9.4), so every decision plans the same share
    of break-glass requests, in the tiers its break-glass templates can reach; escalations only
    at L2, since APPR-004 caps every valid break-glass request (a second finding)."""
    slots = plan_cells(BIG)
    for decision in RequestDecision:
        mine = [(cell, s) for cell, s in slots if cell.decision is decision]
        glass = [(cell, s) for cell, s in mine if s.break_glass]
        assert abs(len(glass) / len(mine) - BREAK_GLASS_SHARE) <= 1 / len(mine)
        tiers = {tier for tier, weight in BREAK_GLASS_TIERS[decision].items() if weight > 0}
        assert {cell.tier for cell, _ in glass} == tiers
    assert BREAK_GLASS_TIERS[RequestDecision.ESCALATE] == {Tier.L2: 1.0}


def test_plan_reserves_slots_for_rules_few_templates_trigger() -> None:
    for n in (BIG, 120):
        slots = plan_cells(n)
        denials = sum(1 for cell, _ in slots if cell.decision is RequestDecision.DENY)
        coverage = [s for s in SCENARIOS if s.coverage]
        assert {s.name for s in coverage} >= {
            "prod_admin",
            "restricted_data",
            "region_mismatch",
            "not_requestable",
            "sod_conflict",
        }
        for scenario in coverage:
            planned = sum(1 for _, s in slots if s is scenario)
            assert planned >= round(COVERAGE_SHARE * denials), (n, scenario.name, planned)


def test_plan_normalizes_a_custom_mix_and_rejects_bad_input() -> None:
    slots = plan_cells(10, {"APPROVE": 2.0, "DENY": 2.0})
    assert plan_cells(10, {RequestDecision.APPROVE: 2.0, RequestDecision.DENY: 2.0}) == slots
    assert Counter(cell.decision for cell, _ in slots) == {
        RequestDecision.APPROVE: 5,
        RequestDecision.DENY: 5,
    }
    assert plan_cells(0) == []
    with pytest.raises(ValueError):
        plan_cells(-1)
    with pytest.raises(ValueError):
        plan_cells(10, {RequestDecision.APPROVE: -1.0})
    with pytest.raises(ValueError):
        plan_cells(10, {RequestDecision.APPROVE: 0.0})
    with pytest.raises(ValueError):
        plan_cells(10, {"MAYBE": 1.0})


def test_scenario_catalog_is_consistent() -> None:
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert {s.decision for s in SCENARIOS} == set(RequestDecision)
    for decision in RequestDecision:
        for tier in (Tier.L1, Tier.L2, Tier.L3):
            assert any(
                s.decision is decision and tier in s.tiers and not s.break_glass for s in SCENARIOS
            )
        for tier, weight in BREAK_GLASS_TIERS[decision].items():
            if weight > 0:
                assert any(
                    s.decision is decision and tier in s.tiers and s.break_glass for s in SCENARIOS
                )
    for scenario in SCENARIOS:
        assert scenario.weight > 0 and scenario.tiers
        assert scenario_named(scenario.name) is scenario
        assert scenario.break_glass is ("break_glass" in scenario.tags)
        assert not (scenario.coverage and scenario.break_glass)
    with pytest.raises(KeyError):
        scenario_named("no_such_template")


def test_shortfalls_read_as_where_each_slot_went() -> None:
    approve_l3 = Cell(RequestDecision.APPROVE, Tier.L3)
    approve_l2 = Cell(RequestDecision.APPROVE, Tier.L2)
    deny_l1 = Cell(RequestDecision.DENY, Tier.L1)
    deny_l3 = Cell(RequestDecision.DENY, Tier.L3)
    assert str(Shortfall(approve_l3, approve_l2)) == "APPROVE/L3 -> APPROVE/L2"
    assert str(Shortfall(approve_l3, None)) == "APPROVE/L3 -> nowhere"
    exchange = Shortfall(approve_l3, deny_l3, deny_l1)
    assert str(exchange) == "APPROVE/L3 -> DENY/L3, DENY/L1 -> APPROVE/L1"
    glass = Shortfall(approve_l3, approve_l2, glass=True)
    assert str(glass) == "APPROVE/L3 break-glass -> APPROVE/L2"
    assert (
        str(Shortfall(approve_l3, approve_l3, glass=True)) == "APPROVE/L3 break-glass -> ordinary"
    )
    # Only a slot that left its decision without an exchange changes the decision mix.
    assert not RequestBuildReport(n=5, shortfalls=[glass, exchange]).mix_changed
    assert RequestBuildReport(n=5, shortfalls=[Shortfall(approve_l3, deny_l3)]).mix_changed
    report = RequestBuildReport(n=5, shortfalls=[glass])
    assert report.summary().splitlines()[0] == "0/5 request cases built in 0 draws"
    assert report.tier_mix() == dict.fromkeys((Tier.L1, Tier.L2, Tier.L3), 0.0)
    assert report.tier_accuracy() == (0.0, 0.0)


# --------------------------------------------------------------------------------------------
# 300-case builds
# --------------------------------------------------------------------------------------------


def test_300_cases_build_in_under_a_minute(build300: Build) -> None:
    assert len(build300.cases) == BIG
    assert build300.seconds < MAX_BUILD_SECONDS


def test_gold_mix_holds_for_120_cases(midmarket_world: World) -> None:
    cases = build_request_cases(midmarket_world, split=Split.test, n=120, seed=11)
    assert len(cases) == 120
    for decision, share in _shares(cases).items():
        assert abs(share - DEFAULT_MIX[decision]) <= MIX_TOLERANCE, decision


def test_300_cases_hold_the_mix_and_every_decision_spans_the_tiers(build300: Build) -> None:
    """Tiers are spread within each decision, not just over the build, so per-tier results
    aren't confounded with the label (SPEC §10.1-§10.3)."""
    cases, report = build300.cases, build300.report
    for decision, share in _shares(cases).items():
        assert abs(share - DEFAULT_MIX[decision]) <= MIX_TOLERANCE, decision
    tiers = Counter(c.tier for c in cases)
    assert set(tiers) == {Tier.L1, Tier.L2, Tier.L3}
    assert all(count / BIG >= TIER_MIN_SHARE for count in tiers.values()), tiers
    shares = report.tier_shares()
    for decision, floors in TIER_FLOORS.items():
        group = [c for c in cases if _gold(c).decision is decision]
        for tier, floor in floors.items():
            share = sum(1 for c in group if c.tier is tier) / len(group)
            assert share == pytest.approx(shares[decision][tier])
            assert share >= floor, (decision, tier, share)
    by_tier, majority = report.tier_accuracy()
    assert majority < by_tier <= TIER_ACCURACY_CEILING, (by_tier, majority)
    summary = report.summary()
    assert "tiers L1/L2/L3 by decision: APPROVE" in summary
    assert "break-glass by decision: APPROVE" in summary
    assert "(planned 0.30/0.35/0.35)" in summary


def test_every_request_rule_fires_several_times(build300: Build) -> None:
    fired = Counter(rule for c in build300.cases for rule in _findings(c))
    low = {rule: fired[rule] for rule in REQUEST_RULE_IDS if fired[rule] < RULE_FLOOR}
    assert not low, low
    exceptions = Counter((x.rule, x.applies) for c in build300.cases for x in _gold(c).exceptions)
    assert exceptions[("MIT-001", True)] > 0
    assert exceptions[("MIT-001", False)] > 0


def test_eligibility_rules_reach_every_type_they_bar(build300: Build) -> None:
    """ELIG-001 bars every non-employee from admin on prod, ELIG-002 interns and vendors from
    restricted data (SPEC §7.4). Each barred type must be among their beneficiaries, so neither
    rule is taught through one type (a vendor asking for admin on prod, not only a contractor),
    and none of them may hold most of its rule's cases while another has material."""
    assert {
        "ELIG-001": tuple(sorted(NON_EMPLOYEES)),
        "ELIG-002": (EmploymentType.intern, EmploymentType.vendor),
    } == BARRED_TYPES
    kinds: dict[str, Counter[EmploymentType]] = defaultdict(Counter)
    for case in build300.cases:
        beneficiary = case.view.identities[_request(case).beneficiary_id]
        for rule in BARRED_TYPES:
            if rule in _findings(case):
                kinds[rule][beneficiary.employment_type] += 1
    for rule, barred in BARRED_TYPES.items():
        assert set(kinds[rule]) == set(barred), (rule, kinds[rule])
        assert max(kinds[rule].values()) <= 0.6 * sum(kinds[rule].values()), (rule, kinds[rule])


def test_named_structures_and_near_misses_appear(build300: Build) -> None:
    cases = build300.cases
    tags = Counter(tag for c in cases for tag in c.tags)
    for tag in (
        "nested_role",
        "multi_source",
        "expired_exception",
        "temporal_boundary",
        "near_miss",
        "on_behalf_of",
        "break_glass",
        "multi_rule",
        "distractor_heavy",
    ):
        assert tags[tag] > 0, tag
    # Nested-role SoD: some certificate needs a child-role hop in every alternative.
    assert any(
        cert.rule.startswith("SOD-")
        and all(any(cl.field == "child_roles" for cl in cs) for cs in cert.claim_sets)
        for c in cases
        for cert in _gold(c).certificates
    )
    # An unmitigated conflict next to a control that has lapsed, cited by its date.
    assert any(
        cert.rule.startswith("SOD-")
        and any(cl.id in c.view.mitigating_controls for cs in cert.claim_sets for cl in cs)
        for c in cases
        for cert in _gold(c).certificates
    )


def test_requesters_of_every_kind(build300: Build) -> None:
    kinds: Counter[str] = Counter()
    for case in build300.cases:
        request = _request(case)
        beneficiary = case.view.identities[request.beneficiary_id]
        app = case.view.applications[case.view.entitlements[request.entitlement_id].app_id]
        if request.requester_id == request.beneficiary_id:
            kind = "self"
        elif request.requester_id == beneficiary.manager_id:
            kind = "manager"
        elif request.requester_id == app.owner_id:
            kind = "owner"
        else:
            kind = "peer"
        kinds[kind] += 1
        # APPR-002 applies exactly when the requester has no authority to ask (SPEC §7.4).
        assert ("APPR-002" in _findings(case)) == (kind == "peer"), case.case_id
    assert set(kinds) == {"self", "manager", "owner", "peer"}


def _unassigned_incidents(world: World) -> set[str]:
    return {
        t.id
        for t in world.tickets.values()
        if t.type is TicketType.incident and t.status is TicketStatus.open and t.assignee_id is None
    }


def test_break_glass_valid_and_failing_each_way(build300: Build) -> None:
    cases = build300.cases
    valid = [c for c in cases if _request(c).is_break_glass and "APPR-004" in _findings(c)]
    assert any(_gold(c).decision is RequestDecision.APPROVE for c in valid)
    # Break-glass bypasses APPR-001 only: high- and critical-risk access approved for a day.
    assert any(
        _gold(c).decision is RequestDecision.APPROVE
        and _gold(c).risk_tier.value in ("high", "critical")
        for c in valid
    )
    unassigned = _unassigned_incidents(build300.world)
    ways: set[str] = set()
    for case in cases:
        for cert in _gold(case).certificates:
            if cert.rule != "APPR-003":
                continue
            for claim_set in cert.claim_sets:
                fields = {claim.field for claim in claim_set}
                if fields == {"is_break_glass"}:
                    ways.add("no ticket")
                if fields == {"is_break_glass", "ticket_ref"}:
                    ways.add("unassigned")
                ways.update(fields & {"status", "type", "assignee_id", "related_app_ids"})
    expected = {"no ticket", "status", "type", "assignee_id", "related_app_ids"}
    assert ways >= expected, ways
    # An incident nobody is assigned to: the assignee can't be cited, so only the request is.
    assert ("unassigned" in ways) == bool(unassigned)
    if build300.world.seed == 101:
        assert unassigned  # the world under test holds that material


def test_an_unassigned_incident_denies_break_glass_on_the_request_alone(build300: Build) -> None:
    unassigned = _unassigned_incidents(build300.world)
    named = [
        c
        for c in build300.cases
        if _request(c).is_break_glass and _request(c).ticket_ref in unassigned
    ]
    assert bool(named) == bool(unassigned)
    for case in named:
        assert _gold(case).decision is RequestDecision.DENY and "APPR-003" in _findings(case)
    lone = [c for c in named if c.provenance["scenario"] == "break_glass_unassigned"]
    assert bool(lone) == bool(unassigned)
    for case in lone:
        assert case.tier is Tier.L1 and _findings(case) == {"APPR-003"}
        (cert,) = _gold(case).certificates
        request_id = _request(case).request_id
        assert all({claim.id for claim in cs} == {request_id} for cs in cert.claim_sets)


def test_break_glass_marks_no_label(build300: Build) -> None:
    """Every label holds the same share of break-glass requests. Within L2 they balance; an
    escalation can hold break-glass only at L2 (APPR-004 always adds a finding), and at L3
    denials hold about as many as approvals, so break-glass there is no sign of approval."""
    cases, report = build300.cases, build300.report
    totals = Counter(_gold(c).decision for c in cases)
    shares = report.glass_shares()
    glass = Counter(_gold(c).decision for c in cases if _request(c).is_break_glass)
    assert glass == report.glass
    for decision, share in shares.items():
        assert abs(share - BREAK_GLASS_SHARE) <= 1.5 / totals[decision], (decision, share)
    cells = Counter((_gold(c).decision, c.tier) for c in cases if _request(c).is_break_glass)
    assert {tier for decision, tier in cells if decision is RequestDecision.ESCALATE} == {Tier.L2}
    approve_l3 = cells[(RequestDecision.APPROVE, Tier.L3)]
    assert approve_l3 > 0 and cells[(RequestDecision.DENY, Tier.L3)] >= approve_l3 / 2
    assert not _rows(report)[("tier=L2", "is_break_glass")]["unbalanced"]
    # Only a denial can lack a ticket (a valid request names its incident), so few do.
    ticketless = [c for c in cases if _request(c).is_break_glass and _request(c).ticket_ref is None]
    assert all(_gold(c).decision is RequestDecision.DENY for c in ticketless)
    assert len(ticketless) <= glass[RequestDecision.DENY] / 4
    # Without incidents assigned to anyone on leave, a colleague files every break-glass
    # escalation, and the others are filed on the beneficiary's behalf as often: the report
    # says so, since then every break-glass case carries the on_behalf_of tag.
    assert not Material(build300.world).incident_assignees_on_leave
    glass_cases = [c for c in cases if _request(c).is_break_glass]
    assert all("on_behalf_of" in c.tags for c in glass_cases)
    assert report.glass_on_behalf == len(glass_cases)
    assert f"break-glass filed on the beneficiary's behalf: {len(glass_cases)}/" in (
        report.summary()
    )


def test_break_glass_at_l1_asks_for_the_day_break_glass_grants(build300: Build) -> None:
    """A valid break-glass request stays at L1 only by asking for exactly the one day APPR-004
    grants (more makes the cap bind, which takes two records), so a break-glass denial at L1
    asking for anything else would mark the approvals: every L1 break-glass request asks for
    that day. Escalations hold break-glass only at L2, where no approval can ask for the day, so
    no break-glass request elsewhere does, and none asks for standing access: each asks for an
    emergency window the cap shortens (``request_scenarios.GLASS_DURATIONS``), so every
    break-glass request names a duration, and within each tier the day says nothing about the
    label."""
    glass = [c for c in build300.cases if _request(c).is_break_glass]
    at_l1 = [c for c in glass if c.tier is Tier.L1]
    assert {_gold(c).decision for c in at_l1} == {RequestDecision.APPROVE, RequestDecision.DENY}
    assert all(_request(c).requested_duration_days is not None for c in glass)
    for case in glass:
        days = _request(case).requested_duration_days
        if case.tier is Tier.L1:
            assert days == BREAK_GLASS_CAP_DAYS, (case.case_id, days)
        else:
            assert days in GLASS_DURATIONS and days > BREAK_GLASS_CAP_DAYS, (case.case_id, days)
    elsewhere = {(_gold(c).decision, c.tier) for c in glass if c.tier is not Tier.L1}
    assert {d for d, _ in elsewhere} == set(RequestDecision)
    # Valid ones outside L1 are capped at the day whatever window they asked for.
    for case in glass:
        if case.tier is not Tier.L1 and _gold(case).decision is RequestDecision.APPROVE:
            assert _gold(case).effective_duration_days == BREAK_GLASS_CAP_DAYS, case.case_id


def test_cap_binding_and_request_binding_approvals(build300: Build) -> None:
    approvals = [c for c in build300.cases if _gold(c).decision is RequestDecision.APPROVE]
    binding = [c for c in approvals if _gold(c).certificates]
    assert binding, "an approval whose cap binds"
    assert all(_gold(c).effective_duration_days is not None for c in binding)
    capped_but_request_binds = [
        c
        for c in approvals
        if not _gold(c).certificates
        and _request(c).requested_duration_days is not None
        and any(f.effect is Effect.CONDITION for f in _gold(c).findings)
    ]
    assert capped_but_request_binds
    assert any(_request(c).requested_duration_days is None and not _findings(c) for c in approvals)
    # Near misses on both sides of a cap: one day over it, and a request no longer than it.
    assert any(
        _request(c).requested_duration_days == (_gold(c).effective_duration_days or 0) + 1
        for c in binding
    )
    assert any(
        _request(c).requested_duration_days == _gold(c).effective_duration_days
        for c in capped_but_request_binds
    )
    caps = Counter(cert.rule for c in binding for cert in _gold(c).certificates)
    assert {"LIFE-004", "APPR-004", "TEMP-001"} <= set(caps)


COPIED_FLOOR = BIG // 6
"""Least denials and escalations other than break-glass with a copied duration in a 300-case
build, so the copy check below is never vacuous. Measured: 61 to 102 over 48 builds of eight
non-release worlds and over 40 builds of the two fixture worlds (seeds 1 to 20, a stand-in for an
upstream change reshuffling the draws). The previous floor, a quarter of the build, sat about two
standard deviations below the median and failed one reseeded build in thirty-four."""


def test_requested_durations_are_round_and_carry_no_label(build300: Build) -> None:
    """A duration's value is a surface fact too: always one of the round durations people ask
    for (a contract end rarely is one, and an odd number would point at the cap), and within
    each tier's break-glass and other requests no label asks for longer or shorter ones than the
    others (``DURATION_FEATURES``, SPEC §6.4: balance within tier x tags strata). L2 approvals
    are capped by construction, so their durations run short; denials and escalations there
    must too. Break-glass windows are dealt to every label evenly from one multiset, so those
    strata balance even with six approvals in L2. L2 as a whole balances too, its break-glass
    windows sitting in the range of its capped approvals. L3 as a whole is left out: its
    approvals are nearly all break-glass and its denials and escalations mostly not, and
    break-glass windows run shorter than other requests' durations, so there the duration
    follows ``is_break_glass`` (``balance_report`` flagged it in 10 of 48 300-case builds of
    eight non-release worlds), while within L3's break-glass and other requests it balances."""
    durations = [_request(c).requested_duration_days for c in build300.cases]
    assert {d for d in durations if d is not None} <= set(DURATION_CHOICES)
    rows = _rows(build300.report)
    assert not rows[("all", "has_requested_duration")]["unbalanced"]
    strata = (
        "all",
        "tier=L1",
        "tier=L2",
        *(f"tier=L{i} break_glass=no" for i in (1, 2, 3)),
        *(f"tier=L{i} break_glass=yes" for i in (2, 3)),
    )
    for stratum in strata:
        row = rows[(stratum, "requested_duration_days")]
        assert not row["unbalanced"], row
    # Denials and escalations other than break-glass copy the durations approvals ask for: those
    # of their own tier's other requests wherever one asks for a duration, else those of the
    # build's other requests, never a break-glass window
    # (``test_break_glass_at_l1_asks_for_the_day_break_glass_grants`` covers those).
    asked: dict[Tier | None, list[int]] = defaultdict(list)
    for case in build300.cases:
        days = _request(case).requested_duration_days
        approved = _gold(case).decision is RequestDecision.APPROVE
        if approved and days is not None and not _request(case).is_break_glass:
            asked[None].append(days)
            asked[case.tier].append(days)
    copied = 0
    for case in build300.cases:
        days = _request(case).requested_duration_days
        if _gold(case).decision is RequestDecision.APPROVE or days is None:
            continue
        if _request(case).is_break_glass:
            continue
        copied += 1
        pool = asked[case.tier] or asked[None]
        assert days in pool, (case.case_id, case.tier, days)
    assert copied >= COPIED_FLOOR, copied


def test_each_case_carries_its_template_and_lands_in_its_cell(build300: Build) -> None:
    for case in build300.cases:
        assert case.task is Task.access_request
        assert case.suite is Suite.requests
        assert case.split is Split.test
        assert case.variant == "base" and case.twin_of is None
        assert set(case.provenance) == {"scenario", "justification_family"}
        scenario = scenario_named(case.provenance["scenario"])
        assert scenario.accepts(_gold(case).decision, case.tier, case.tags), case.case_id
        assert scenario.break_glass is _request(case).is_break_glass
    used = Counter(c.provenance["scenario"] for c in build300.cases)
    assert len(used) >= 28
    assert used == build300.report.scenarios


# --------------------------------------------------------------------------------------------
# Integrity: answerability, faithfulness, uniqueness, no gold metadata in views
# --------------------------------------------------------------------------------------------


def test_cases_are_answerable_after_a_json_round_trip(build300: Build) -> None:
    for case in build300.cases:
        stored = case.model_dump_json()
        parsed = Case.model_validate_json(stored)
        assert parsed.model_dump_json() == stored
        gold = decide_request(parsed.view)
        assert gold == case.gold, case.case_id
        assert compute_tier(parsed.view, gold, Task.access_request) is case.tier
        # distractor_heavy depends on a count no case records (validate leaves it out too).
        recomputed = set(compute_tags(parsed.view, gold, Task.access_request))
        assert recomputed == set(case.tags) - {"distractor_heavy"}, case.case_id


def test_views_are_faithful_slices_of_the_world(build300: Build) -> None:
    world = build300.world
    world_ids = _world_ids(world)
    for case in build300.cases:
        view = case.view
        assert view.as_of == world.anchor_date
        assert view.policy_pack == world.policy_pack
        for name in COLLECTIONS:
            records = getattr(world, name)
            for record_id, record in getattr(view, name).items():
                assert records[record_id] == record, (case.case_id, record_id)
        request = _request(case)
        assert request.request_id not in world_ids
        for identity_id in (request.requester_id, request.beneficiary_id):
            assert identity_id in world.identities
        assert request.entitlement_id in world.entitlements
        if request.ticket_ref is not None:
            assert request.ticket_ref in world.tickets
            assert request.ticket_ref in view.tickets
        assert request.submitted_on <= view.as_of


def _held_in_world(world: World, identity_id: str) -> set[str]:
    """The identity's held access on the world's anchor date, from the world's own records:
    memberships valid that day, expanded through child roles at any depth, and assignments valid
    that day on the identity's human accounts (SPEC §6.3, §7.3)."""
    on = world.anchor_date
    held: set[str] = set()
    stack = [
        m.role_id
        for m in world.memberships.values()
        if m.identity_id == identity_id and is_valid_on(m.granted_on, m.expires_on, on)
    ]
    seen: set[str] = set()
    while stack:
        role = world.roles[stack.pop()]
        if role.id not in seen:
            seen.add(role.id)
            held.update(role.entitlements)
            stack.extend(role.child_roles)
    accounts = {
        a.id
        for a in world.accounts.values()
        if a.identity_id == identity_id and a.kind is AccountKind.human
    }
    held.update(
        a.entitlement_id
        for a in world.assignments.values()
        if a.account_id in accounts and is_valid_on(a.granted_on, a.expires_on, on)
    )
    return held


def test_beneficiaries_never_already_hold_the_requested_entitlement(build300: Build) -> None:
    """SPEC §7.4 SoD scope: the generator guarantees a beneficiary never already holds the
    requested entitlement. Held access is read from the world itself, so a view that left out a
    source would still be caught; the view agrees."""
    world = build300.world
    for case in build300.cases:
        request = _request(case)
        assert request.entitlement_id not in _held_in_world(world, request.beneficiary_id)
        held = held_access(case.view, request.beneficiary_id, case.view.as_of)
        assert request.entitlement_id not in held, case.case_id


def test_pairs_ids_and_use_caps(build300: Build) -> None:
    cases = build300.cases
    pairs = [(_request(c).beneficiary_id, _request(c).entitlement_id) for c in cases]
    assert len(set(pairs)) == len(pairs)
    for ids in (
        [c.case_id for c in cases],
        [c.family_id for c in cases],
        [_request(c).request_id for c in cases],
    ):
        assert len(set(ids)) == len(ids)
    assert max(Counter(b for b, _ in pairs).values()) <= DEFAULT_MAX_USES
    # One entitlement, one gold decision: at most once per 100 cases of the build, and in all
    # at most DEFAULT_MAX_ENTITLEMENT_USES; SCARCE_CAP_FACTOR times both for scarce ones.
    scarce = Material(build300.world).scarce_entitlements
    assert scarce
    labelled = Counter((_request(c).entitlement_id, _gold(c).decision) for c in cases)
    for (entitlement, _), count in labelled.items():
        factor = SCARCE_CAP_FACTOR if entitlement in scarce else 1
        assert count <= BIG // 100 * factor, entitlement
    for entitlement, count in Counter(e for _, e in pairs).items():
        factor = SCARCE_CAP_FACTOR if entitlement in scarce else 1
        assert count <= DEFAULT_MAX_ENTITLEMENT_USES * factor, entitlement
    assert [c.case_id for c in cases] == sorted(c.case_id for c in cases)


def test_views_carry_no_gold_metadata(build300: Build) -> None:
    forbidden = {"tier", "tags", "family_id", "provenance", "gold", "case_id", "scenario"}
    assert not forbidden & set(CaseView.model_fields)
    for case in build300.cases:
        text = case.view.model_dump_json()
        for value in (case.case_id, case.family_id, *case.provenance.values()):
            # As a whole JSON string: a template name may be part of an entitlement ID.
            assert f'"{value}"' not in text, (case.case_id, value)


# --------------------------------------------------------------------------------------------
# Free text
# --------------------------------------------------------------------------------------------

_TICKET_ID = re.compile(r"\b(?:INC|CHG|PRJ)-\d+")


def test_justifications_use_the_split_families_and_fit_the_request(build300: Build) -> None:
    test_families = {f.id for f in BANK.select(split=Split.test)}
    for case in build300.cases:
        request = _request(case)
        text = request.justification
        assert text.strip()
        assert case.provenance["justification_family"] in test_families
        # A ticket ID in clean text is always the request's own ticket_ref (else it would be
        # the fake_ticket attack, SPEC §8.4).
        assert set(_TICKET_ID.findall(text)) <= {request.ticket_ref}, (case.case_id, text)
        beneficiary = case.view.identities[request.beneficiary_id]
        if request.requester_id != request.beneficiary_id:
            # on_behalf names the beneficiary; ticket_mention names only the ticket.
            mentions = (first_name(beneficiary.display_name), request.ticket_ref or "\0")
            assert any(m in text for m in mentions), (case.case_id, text)


@dataclass(frozen=True)
class _Rendered:
    text: str
    slots: dict[str, object]
    kind: JustificationKind | None


def test_justification_slots_and_kinds_come_from_the_case(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    """The team is the beneficiary's department, the app and entitlement names are the view's,
    a colleague's name is someone else's in the view, and the kind fits the structure
    (``tasks.text.STRUCTURE_DEPENDENT_KINDS``): ``on_behalf`` or ``ticket_mention`` when someone
    else files the request, ``ticket_mention`` only with the request's own ticket,
    ``temporary`` only with a duration."""
    real = requests_module.render_justification
    rendered: list[_Rendered] = []

    def recording(
        rng: random.Random,
        *,
        split: Split | str,
        slots: Mapping[str, object],
        kind: JustificationKind | str | None = None,
    ) -> tuple[str, str]:
        text, family = real(rng, split=split, slots=slots, kind=kind)
        resolved = None if kind is None else JustificationKind(kind)
        rendered.append(_Rendered(text, dict(slots), resolved))
        return text, family

    monkeypatch.setattr(requests_module, "render_justification", recording)
    cases = build_request_cases(startup_world, split=Split.test, n=40, seed=5)
    assert len(rendered) == len(cases)
    colleagues = 0
    for case in cases:
        # Two cases may get the same text (a template without the entitlement's name, say), so
        # the case's own call is one of the calls that rendered its text.
        calls = [r for r in rendered if r.text == _request(case).justification]
        assert calls, case.case_id
        problems = [_justification_problems(case, call) for call in calls]
        assert [] in problems, (case.case_id, problems)
        colleagues += "colleague_first" in calls[problems.index([])].slots
    assert colleagues > 0


def _justification_problems(case: Case, call: _Rendered) -> list[str]:
    """How a recorded ``render_justification`` call fails to fit the case (empty if it fits)."""
    request = _request(case)
    view = case.view
    beneficiary = view.identities[request.beneficiary_id]
    entitlement = view.entitlements[request.entitlement_id]
    others = {
        first_name(i.display_name)
        for i in view.identities.values()
        if i.id not in (request.beneficiary_id, request.requester_id)
        and i.status is IdentityStatus.active
    }
    expected: dict[str, object] = {
        "team": beneficiary.department,
        "app": view.applications[entitlement.app_id].name,
        "entitlement_name": entitlement.name,
    }
    problems = [
        f"{key}={call.slots.get(key)!r}" for key, v in expected.items() if call.slots.get(key) != v
    ]
    colleague = call.slots.get("colleague_first")
    if (colleague not in others) if others else colleague is not None:
        problems.append(f"colleague_first={colleague!r}")
    on_behalf_kinds = (JustificationKind.on_behalf, JustificationKind.ticket_mention)
    if request.requester_id != request.beneficiary_id and call.kind not in on_behalf_kinds:
        problems.append(f"kind {call.kind} for a request filed by someone else")
    if call.kind is JustificationKind.ticket_mention and (
        request.ticket_ref is None or call.slots.get("ticket_id") != request.ticket_ref
    ):
        problems.append("ticket_mention without the request's own ticket")
    if call.kind is JustificationKind.temporary and request.requested_duration_days is None:
        problems.append("temporary without a duration")
    return problems


def test_a_dev_build_uses_dev_families(startup_world: World) -> None:
    cases = build_request_cases(startup_world, split="dev", n=20, seed=2)
    dev_families = {f.id for f in BANK.select(split=Split.dev)}
    assert {c.provenance["justification_family"] for c in cases} <= dev_families
    assert {c.split for c in cases} == {Split.dev}


def test_first_name_handles_compound_given_names() -> None:
    assert first_name("María José Pérez García") == "María José"
    assert first_name("Juan Carlos Rojas") == "Juan Carlos"
    assert first_name("Wei Zhou") == "Wei"
    assert first_name("Ye-jun Moon") == "Ye-jun"


# --------------------------------------------------------------------------------------------
# Nuisance balance and the shortcut probe
# --------------------------------------------------------------------------------------------


def test_balance_report_shows_no_strong_label_predictor(build300: Build) -> None:
    rows = balance_report(build300.cases)
    assert rows == build300.report.balance
    strata = {row["stratum"] for row in rows}
    assert {"all", "tier=L1", "tier=L2", "tier=L3"} <= strata
    assert {f"tier={t} break_glass={g}" for t in ("L2", "L3") for g in ("yes", "no")} <= strata
    overall = {row["feature"]: row for row in rows if row["stratum"] == "all"}
    assert set(overall) == {*REQUEST_FEATURES, *DURATION_FEATURES}
    for feature, row in overall.items():
        assert not row["unbalanced"], row
        assert isinstance(row["smd"], float) and row["smd"] <= BALANCE_SMD_LIMIT, feature
    for feature in PRESENCE_FEATURES:
        smd = overall[feature]["smd"]
        assert isinstance(smd, float) and smd < 0.1, (feature, smd)
    # Who files a request and whether it names a duration balance within every stratum the
    # builder balances: each tier split by the break_glass tag (SPEC §6.4 strata are tier x
    # tags), and so each tier. At L3 alone, both differ with the tier's mix of break-glass
    # requests: approvals reach L3 almost only through break-glass, and every break-glass request
    # is filed on the beneficiary's behalf (as every break-glass escalation is) and names a
    # duration (as every valid one outside L1 does), so there they repeat ``is_break_glass``.
    filed_or_timed = ("has_requested_duration", "on_behalf")
    for row in rows:
        if row["feature"] in filed_or_timed and row["stratum"] != "tier=L3":
            assert not row["unbalanced"], row
    by_stratum = _rows(build300.report)
    for feature in filed_or_timed:
        for glass in ("yes", "no"):
            assert not by_stratum[(f"tier=L3 break_glass={glass}", feature)]["unbalanced"]


@dataclass(frozen=True)
class Fleet:
    """Builds of fresh worlds shaped like a v1 split (module docstring)."""

    midmarket: list[list[Case]]
    """One list per 60-case build of a ``FLEET_WORLDS`` world."""
    startup: list[Case]
    dev_pools: list[list[Case]]
    """One dev split per ``DEV_POOLS`` entry."""

    @property
    def held_out(self) -> list[Case]:
        """Every test-split case of the fleet."""
        return [case for build in self.midmarket for case in build] + self.startup


@pytest.fixture(scope="module")
def fleet() -> Fleet:
    midmarket: list[list[Case]] = []
    for world_seed in FLEET_WORLDS:
        world = build_world("midmarket", world_seed)
        midmarket += [
            build_request_cases(world, split=Split.test, n=FLEET_N, seed=seed)
            for seed in FLEET_SEEDS
        ]
    startup = [
        case
        for world_seed in FLEET_STARTUP_WORLDS
        for seed in FLEET_SEEDS
        for case in build_request_cases(
            build_world("startup", world_seed), split=Split.test, n=FLEET_STARTUP_N, seed=seed
        )
    ]
    dev_pools = [
        [
            case
            for profile, world_seed, n in (
                ("midmarket", first, 30),
                ("midmarket", second, 30),
                ("startup", third, 15),
            )
            for case in build_request_cases(
                build_world(profile, world_seed), split=Split.dev, n=n, seed=1
            )
        ]
        for first, second, third in DEV_POOLS
    ]
    return Fleet(midmarket, startup, dev_pools)


def _signed_smd(approve: Sequence[float], others: Sequence[float]) -> float:
    """The standardized mean difference, APPROVE minus the other labels (0 for two constant
    samples)."""
    pooled = math.sqrt((statistics.pvariance(approve) + statistics.pvariance(others)) / 2)
    gap = statistics.fmean(approve) - statistics.fmean(others)
    return gap / pooled if pooled else 0.0


def test_no_nuisance_feature_leans_toward_a_label_across_worlds(fleet: Fleet) -> None:
    """A feature that leans the same way in every world carries from dev to test, though each
    build alone stays far below ``balance_report``'s flag (SPEC §6.4, §9.4): before draws
    balanced description length by its mean, approvals requested descriptions about 0.3 standard
    deviations shorter in eight of eight fresh worlds. Each surface feature's lean
    (signed SMD, APPROVE minus the other labels) is averaged over each world's builds, then over
    the fleet's worlds (builds of one world share its material, so worlds are the independent
    draws a release makes), and must stay within its fixed bound (``NUISANCE_TOLERANCE``,
    ``LEAN_BOUNDS``)."""
    builds_per_world = len(FLEET_SEEDS)
    leans: dict[str, list[float]] = defaultdict(list)
    for first in range(0, len(fleet.midmarket), builds_per_world):
        per_build: dict[str, list[float]] = defaultdict(list)
        for build in fleet.midmarket[first : first + builds_per_world]:
            rows = [
                (_gold(c).decision is RequestDecision.APPROVE, request_features(c)) for c in build
            ]
            for feature in REQUEST_FEATURES:
                approve = [row[feature] for is_approve, row in rows if is_approve]
                others = [row[feature] for is_approve, row in rows if not is_approve]
                per_build[feature].append(_signed_smd(approve, others))
        for feature, values in per_build.items():
            leans[feature].append(statistics.fmean(values))
    assert set(LEAN_BOUNDS) <= set(REQUEST_FEATURES)
    assert all(len(values) == len(FLEET_WORLDS) for values in leans.values())
    too_far = {
        feature: round(statistics.fmean(values), 3)
        for feature, values in leans.items()
        if abs(statistics.fmean(values)) > LEAN_BOUNDS.get(feature, NUISANCE_TOLERANCE)
    }
    assert not too_far, too_far


def test_closures_and_descriptions_balance_within_tiers(fleet: Fleet) -> None:
    """SPEC §6.4 balances nuisance features within strata that exclude the label: tier x tags, and
    break-glass is the tag that changes who a case's beneficiary can be. Draws aim at one stratum's
    cell and see its profile shortfall against the other labels' cases of the same kind in the tier
    (``tasks.requests``), so within L1 and L2, and within each one's break-glass and other requests,
    no two gold labels differ much in the view sizes the beneficiary's closure decides or in the
    requested entitlement's description (``CLOSURE_FEATURES``, ``OTHER_REQUESTS_TOLERANCE``,
    ``WITHIN_TIER_TOLERANCE``). Every pair of labels counts, not only APPROVE against the rest: a
    probe that tells denials from escalations is a shortcut too. L3 is the documented exception:
    there a midmarket world approves almost only through break-glass, whose incident assignees'
    closures the holders of nested SoD sides outgrow."""
    cases = [case for build in fleet.midmarket for case in build]
    too_far: dict[str, float] = {}
    for tier in (Tier.L1, Tier.L2):
        for glass in (None, False, True):
            labels: dict[RequestDecision, list[Mapping[str, float]]] = defaultdict(list)
            for case in cases:
                if case.tier is tier and (glass is None or _request(case).is_break_glass is glass):
                    labels[_gold(case).decision].append(request_features(case))
            compared = sorted(d for d, rows in labels.items() if len(rows) >= PAIR_MIN_CASES)
            name = f"{tier.value} break_glass={glass}"
            assert len(compared) >= 2, (name, {d: len(r) for d, r in labels.items()})
            bound = OTHER_REQUESTS_TOLERANCE if glass is False else WITHIN_TIER_TOLERANCE
            for first, second in itertools.combinations(compared, 2):
                for feature in CLOSURE_FEATURES:
                    gap = _signed_smd(
                        [row[feature] for row in labels[first]],
                        [row[feature] for row in labels[second]],
                    )
                    if abs(gap) > bound:
                        too_far[f"{name} {first.value}-{second.value} {feature}"] = round(gap, 3)
    assert not too_far, too_far


def test_employment_types_spread_over_labels_in_v1_sized_builds(fleet: Fleet) -> None:
    """Employment type is a rule input a model reads (LIFE-004, ELIG-001, ELIG-002), so its
    spread over the gold labels is a shortcut the probe can't see (SPEC §6.4). In 60-case builds,
    the size a v1 split uses, approvals hold contractors more often than the other labels by
    construction, and fewer interns and vendors, whom the eligibility rules bar and denials
    therefore hold; pooled over the fleet each type's gap stays where it is measured
    (``TYPE_POOLED_GAPS``)."""
    cases = [case for build in fleet.midmarket for case in build]
    assert set(TYPE_POOLED_GAPS) == {t.value for t in EmploymentType}
    shares: dict[RequestDecision, dict[str, float]] = {}
    for decision in RequestDecision:
        group = [c for c in cases if _gold(c).decision is decision]
        kinds = Counter(
            c.view.identities[_request(c).beneficiary_id].employment_type.value for c in group
        )
        shares[decision] = {k: kinds[k] / len(group) for k in TYPE_POOLED_GAPS}
    for kind, bound in TYPE_POOLED_GAPS.items():
        values = [shares[d][kind] for d in RequestDecision]
        assert max(values) - min(values) <= bound, (kind, shares)


def test_break_glass_durations_mark_no_label_over_the_fleet(fleet: Fleet) -> None:
    """A requested duration is a surface fact a model reads, and whether one is set is a probe
    feature (SPEC §6.4, §9.4). In the 60-case builds a v1 split uses, as in larger ones, every
    break-glass request names a duration, exactly those at L1 ask for the one day APPR-004
    grants, and the others ask for an emergency window the cap shortens
    (``request_scenarios.GLASS_DURATIONS``). Escalations can't be break-glass at L1, so the
    day's share differs by label only as the plan spreads break-glass over the tiers
    (``BREAK_GLASS_TIERS``): a quarter of approvals' and denials' break-glass requests in a
    60-case build, none of escalations'. When L3's break-glass approvals could ask for the day and
    L2's break-glass denials and escalations copied the tier's other durations, the shares were
    0.37, 0.30 and 0.07 to 0.09 on this fleet."""
    glass = [c for build in fleet.midmarket for c in build if _request(c).is_break_glass]
    assert {_gold(c).decision for c in glass} == set(RequestDecision)
    for case in glass:
        days = _request(case).requested_duration_days
        assert days is not None, case.case_id
        assert (days == BREAK_GLASS_CAP_DAYS) is (case.tier is Tier.L1), (case.case_id, days)
        assert case.tier is Tier.L1 or days in GLASS_DURATIONS, (case.case_id, days)


DURATION_OTHER_TOLERANCE = 0.25
"""Largest lean (signed SMD, APPROVE minus the other labels, pooled over the fleet's midmarket
builds) of the requested duration's value among L1's and L2's requests other than break-glass,
which hold about 290 and 500 approvals in a fleet, so a standard error near 0.07. Measured on
eight fresh fleets of this shape: -0.03 to +0.06. Placing each case's duration around its own
caps, as an early builder did, left differences of 0.5 to 0.8 within L2 in single 300-case
builds."""
DURATION_SPARSE_TOLERANCE = 0.6
"""The same for the sparse strata: L2's and L3's break-glass requests (48 and 96 approvals in a
fleet, standard errors near 0.16 and 0.14) and L3's other requests, whose approvals are mostly
mitigated approvals asking for months, which some worlds have no material for: 4 to 48 per fleet
over eight fresh fleets, so that stratum counts only when it holds ``PAIR_MIN_CASES``. Measured
on those fleets: L2's break-glass -0.07 to +0.31, L3's -0.16 to +0.06, L3's other requests -0.22
to +0.19 where counted. Before L3's other denials and escalations copied L3's own approvals,
approvals there asked for durations 0.49 standard deviations longer, pooled over 60 builds of
thirty worlds with the catalog then current."""


def test_requested_durations_balance_within_strata_over_the_fleet(fleet: Fleet) -> None:
    """In a 60-case build a stratum holds a handful of approvals at most (about three of L2's
    other requests, fewer than one of L3's), too few for ``balance_report`` to compare, so
    duration balance within SPEC §6.4's tier x tags strata is checked over the fleet: the
    requested duration's value (``DURATION_FEATURES``, standing access as 0) leans toward no
    label within each tier's other requests and within L2's and L3's break-glass requests
    (``DURATION_OTHER_TOLERANCE``, ``DURATION_SPARSE_TOLERANCE``). Denials and escalations copy
    the approvals among their stratum's other requests however few ask for a duration, and
    break-glass windows are dealt to every label from one multiset (``tasks.requests``,
    ``_DurationPools``). L3 as a whole is left out, as in
    ``test_requested_durations_are_round_and_carry_no_label``, and L3's other requests count only
    in a fleet whose worlds hold enough approvals there."""
    cases = [case for build in fleet.midmarket for case in build]
    dense = [(Tier.L1, False), (Tier.L2, False), (Tier.L2, True), (Tier.L3, True)]
    too_far: dict[str, float] = {}
    for tier, glass in [*dense, (Tier.L3, False)]:
        approve: list[float] = []
        others: list[float] = []
        for case in cases:
            request = _request(case)
            if case.tier is tier and request.is_break_glass is glass:
                days = float(request.requested_duration_days or 0)
                is_approve = _gold(case).decision is RequestDecision.APPROVE
                (approve if is_approve else others).append(days)
        if min(len(approve), len(others)) < PAIR_MIN_CASES:
            assert (tier, glass) not in dense, (tier, glass, len(approve), len(others))
            continue
        sparse = glass or tier is Tier.L3
        bound = DURATION_SPARSE_TOLERANCE if sparse else DURATION_OTHER_TOLERANCE
        lean = _signed_smd(approve, others)
        if abs(lean) > bound:
            too_far[f"{tier.value} break_glass={glass}"] = round(lean, 3)
    assert not too_far, too_far


def test_shortcut_probe_passes_the_gate_on_held_out_worlds(fleet: Fleet) -> None:
    """SPEC §9.4 as a release runs it: a probe trained on a dev split pooled from two midmarket
    worlds and a startup world (75 cases) may beat the majority class on held-out test worlds by
    at most ``MAX_DELTA_POINTS``. One such probe swings by a couple of points with its training
    sample, so four probes on four dev splits are scored on the whole held-out fleet (2,880
    midmarket and 240 startup cases): their mean must pass the gate, and none may pass it by more
    than ``PROBE_SPREAD_POINTS``. Builds of different worlds share no family IDs (``_stream``),
    so ``evaluate_probe`` accepts the held-out cases."""
    held_out = fleet.held_out
    deltas = [
        100 * evaluate_probe(train_probe(pool), held_out)[Task.access_request].delta
        for pool in fleet.dev_pools
    ]
    assert statistics.fmean(deltas) <= MAX_DELTA_POINTS, deltas
    assert max(deltas) <= MAX_DELTA_POINTS + PROBE_SPREAD_POINTS, deltas


def test_balance_report_flags_a_planted_imbalance(build300: Build) -> None:
    def padded(case: Case) -> Case:
        request = _request(case)
        longer = request.model_copy(update={"justification": request.justification * 4})
        return case.model_copy(update={"view": case.view.model_copy(update={"request": longer})})

    skewed = [
        padded(c) if _gold(c).decision is RequestDecision.APPROVE else c for c in build300.cases
    ]
    rows = {row["feature"]: row for row in balance_report(skewed) if row["stratum"] == "all"}
    row = rows["justification_chars"]
    assert row["unbalanced"]
    approve, deny = row["mean_APPROVE"], row["mean_DENY"]
    assert isinstance(approve, float) and isinstance(deny, float) and approve > deny


def test_balance_report_on_no_or_small_inputs(build300: Build) -> None:
    assert balance_report([]) == []
    per_label = BALANCE_MIN_CASES - 1
    few = [
        case
        for decision in RequestDecision
        for case in [c for c in build300.cases if _gold(c).decision is decision][:per_label]
    ]
    rows = balance_report(few)
    assert rows
    for row in rows:
        # Labels with too few cases aren't compared, so nothing can be flagged.
        assert row["unbalanced"] is False and row["smd"] == 0.0


TYPE_SHARE_GAP = 0.2
"""Largest gap between two labels' shares of employees, or of contractors, in a 300-case
midmarket build. Employment type is a rule input a model reads, and approvals hold contractors
more often by construction: a midmarket approval at L2 without break-glass needs a contractor's
cap next to privileged access's, and a contractor's denial or escalation is never L1 (LIFE-004
adds a finding). Contractor gaps run 0.09 to 0.15 over sixteen fresh worlds (mean 0.12),
employee gaps 0.03 to 0.11. Before ELIG-001 rotated the types it bars, its denials were nearly
all contractors, which held the gap near 0.11 for the wrong reason. One build's gap moves by
about 0.04 with its draws, so this bound only guards against a gross change;
``TYPE_POOLED_GAPS``, over the fleet's 2,880 cases, is the precise check."""


def test_balanced_profiles_across_labels(build300: Build) -> None:
    """Beneficiaries' employment types are spread over the gold labels as evenly as the
    templates allow (``TYPE_SHARE_GAP``): a model shouldn't learn that a contractor's request is
    approved."""
    shares: dict[RequestDecision, dict[str, float]] = {}
    for decision in RequestDecision:
        group = [c for c in build300.cases if _gold(c).decision is decision]
        kinds = Counter(
            c.view.identities[_request(c).beneficiary_id].employment_type.value for c in group
        )
        shares[decision] = {k: kinds[k] / len(group) for k in ("employee", "contractor")}
    for kind in ("employee", "contractor"):
        values = [shares[d][kind] for d in RequestDecision]
        assert max(values) - min(values) <= TYPE_SHARE_GAP, (kind, shares)
    assert statistics.fmean(s["employee"] for s in shares.values()) > 0.5


# --------------------------------------------------------------------------------------------
# Determinism, other worlds, options and errors
# --------------------------------------------------------------------------------------------


def test_same_seed_gives_identical_jsonl(midmarket_world: World) -> None:
    first = build_request_cases(midmarket_world, split=Split.test, n=60, seed=21)
    second = build_request_cases(midmarket_world, split=Split.test, n=60, seed=21)
    other = build_request_cases(midmarket_world, split=Split.test, n=60, seed=22)
    assert _jsonl(first) == _jsonl(second)
    assert _jsonl(first) != _jsonl(other)


def test_builds_of_different_worlds_with_one_seed_share_no_ids(startup_world: World) -> None:
    """Every stage's RNG is keyed by the world as well as the seed, so builds of two worlds with
    the same seed don't replay one sequence (a family shared across worlds would merge unrelated
    cases in the probe's family split, SPEC §9.4). Keyed by the seed alone, these two builds
    shared all 24 request IDs and two case and family IDs. Case and family IDs are 12-character
    random tokens, so a chance collision is negligible; request IDs are five digits, unique only
    within a world, so two other builds could share one by chance (about 4% for two 60-case
    builds, ``_stream``). These fixed builds share none, so the check stays deterministic."""
    other = build_world("startup", 21)
    first = build_request_cases(startup_world, split=Split.test, n=24, seed=5)
    second = build_request_cases(other, split=Split.test, n=24, seed=5)
    for ids in (
        lambda c: c.case_id,
        lambda c: c.family_id,
        lambda c: _request(c).request_id,
    ):
        assert not {ids(c) for c in first} & {ids(c) for c in second}


def test_builds_do_not_depend_on_hash_order(startup_world: World) -> None:
    """Same inputs, same bytes, in fresh interpreters whose string hashing (so set iteration
    order) differs."""
    expected = fingerprint(startup_world, 24, 5)
    script = (
        "from identitybench.world.generator import build_world\n"
        "from tests.tasks.test_requests import fingerprint\n"
        f"world = build_world({startup_world.profile!r}, {startup_world.seed})\n"
        "print(fingerprint(world, 24, 5))\n"
    )
    for hash_seed in ("0", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, f"PYTHONHASHSEED={hash_seed}"


@pytest.mark.parametrize("n", [24, 40])
def test_startup_world_supplies_smaller_builds(startup_world: World, n: int) -> None:
    cases, report = build_request_cases_with_report(startup_world, split="test", n=n, seed=5)
    assert len(cases) == n
    assert not report.mix_changed
    planned = Counter(cell.decision for cell in report.planned.elements())
    assert Counter(_gold(c).decision for c in cases) == planned
    for case in cases:
        parsed = Case.model_validate_json(case.model_dump_json())
        assert decide_request(parsed.view) == case.gold
        request = _request(case)
        assert request.entitlement_id not in held_access(
            case.view, request.beneficiary_id, case.view.as_of
        )
    # Startup worlds lack L2 and L3 approval material (no approvable privileged entitlement,
    # two open incidents), and the report says where the slots went.
    assert report.shortfalls
    assert all(s.moved_to is not None for s in report.shortfalls)
    assert "request cases built" in report.summary()
    # Break-glass shares stay equal across labels however few incidents the world has.
    shares = report.glass_shares()
    totals = report.decisions()
    assert max(shares.values()) - min(shares.values()) <= max(1 / t for t in totals.values())


def test_report_accounts_for_every_case(midmarket_world: World) -> None:
    cases, report = build_request_cases_with_report(
        midmarket_world, split=Split.test, n=90, seed=31
    )
    assert sum(report.built.values()) == len(cases) == 90
    assert Counter(Cell(_gold(c).decision, c.tier) for c in cases) == report.built
    assert sum(report.scenarios.values()) == 90
    assert Counter(c.provenance["scenario"] for c in cases) == report.scenarios
    assert report.glass == Counter(_gold(c).decision for c in cases if _request(c).is_break_glass)
    assert report.decisions() == Counter(_gold(c).decision for c in cases)
    assert report.draws >= 90
    delegated = [c for c in cases if _request(c).requester_id != _request(c).beneficiary_id]
    assert report.on_behalf == len(delegated) == sum("on_behalf_of" in c.tags for c in cases)
    assert report.glass_on_behalf == sum(_request(c).is_break_glass for c in delegated)
    tiers = Counter(c.tier for c in cases)
    assert set(tiers) == {Tier.L1, Tier.L2, Tier.L3}
    assert report.tier_mix() == {tier: tiers[tier] / 90 for tier in tiers}
    planned = Counter(cell.tier for cell in report.planned.elements())
    assert report.tier_mix(report.planned) == {t: planned[t] / 90 for t in tiers}
    # The tier alone: each tier's most common decision, against the most common one overall.
    by_tier = sum(
        max(Counter(_gold(c).decision for c in cases if c.tier is tier).values()) for tier in tiers
    )
    majority = max(Counter(_gold(c).decision for c in cases).values())
    assert report.tier_accuracy() == pytest.approx((by_tier / 90, majority / 90))
    summary = report.summary()
    assert f"the tier alone gives the decision of {by_tier / 90:.2f} of cases" in summary
    assert f"break-glass filed on the beneficiary's behalf: {report.glass_on_behalf}/" in summary


def test_excluded_identities_are_never_beneficiaries(midmarket_world: World) -> None:
    excluded = frozenset(sorted(midmarket_world.identities)[:800])
    cases = build_request_cases(midmarket_world, split=Split.test, n=45, seed=4, exclude=excluded)
    assert len(cases) == 45
    assert not {_request(c).beneficiary_id for c in cases} & excluded


def test_a_world_that_cannot_supply_the_cases_raises_with_a_report(startup_world: World) -> None:
    everyone = frozenset(startup_world.identities)
    with pytest.raises(RequestBuildError) as raised:
        build_request_cases(startup_world, split=Split.test, n=5, seed=1, exclude=everyone)
    report = raised.value.report
    assert sum(report.built.values()) == 0
    assert report.mix_changed
    assert any(s.moved_to is None for s in report.shortfalls)
    assert "supplied 0 of 5" in str(raised.value)


def test_zero_cases_and_bad_arguments(startup_world: World) -> None:
    assert build_request_cases(startup_world, split=Split.test, n=0, seed=1) == []
    with pytest.raises(ValueError):
        build_request_cases(startup_world, split=Split.test, n=-1, seed=1)
    with pytest.raises(ValueError):
        build_request_cases(startup_world, split="holdout", n=3, seed=1)


# --------------------------------------------------------------------------------------------
# Scenario templates and the material they search
# --------------------------------------------------------------------------------------------


def test_every_template_lands_in_its_cells(midmarket_world: World) -> None:
    """Each template's drafts, checked by the oracle and ``tiers``, land in the cell it aimed at
    (the builder rejects any that don't, but a template that mostly misses would waste its
    material). Templates the world has no material for only say so."""
    views = ViewBuilder(midmarket_world)
    rng = random.Random(9)
    for scenario in SCENARIOS:
        # Each template on its own, with all its material and caps no draw here can reach.
        material = Material(midmarket_world, max_entitlement_uses=99, max_label_uses=99)
        for tier in scenario.tiers:
            ask = Ask(scenario.decision, tier)
            drafts = hits = 0
            for _ in range(6):
                draft = scenario.draw(material, rng, ask)
                if draft is None:
                    break
                drafts += 1
                assert draft.is_break_glass is scenario.break_glass
                view = views.build_view(draft.request(submitted_on=midmarket_world.anchor_date))
                gold = decide_request(view)
                landed = compute_tier(view, gold, Task.access_request)
                tags = compute_tags(view, gold, Task.access_request)
                if landed is tier and scenario.accepts(gold.decision, landed, tags):
                    hits += 1
                    material.note_use(draft.beneficiary_id, draft.entitlement_id, gold.decision)
                else:
                    material.block(draft.beneficiary_id, draft.entitlement_id)
            if drafts:
                assert hits >= drafts / 2, (scenario.name, tier, hits, drafts)
            elif (scenario.name, tier) not in WORLD_DEPENDENT:
                pytest.fail(f"{scenario.name} found no material for {tier}")


def _with_rarer_material(world: World) -> World:
    """A copy of ``world`` holding material generated midmarket worlds lack: open incidents
    reassigned to people on leave (``break_glass_on_leave``), and approvable unprivileged
    entitlements given a maximum duration, as enterprise catalogs have (``time_boxed_approval``).
    The session's world itself is left untouched."""
    material = Material(world)
    tickets = dict(world.tickets)
    for person, incident in zip(material.on_leave[::7], material.open_incidents[::3], strict=False):
        tickets[incident.id] = incident.model_copy(update={"assignee_id": person})
    entitlements = dict(world.entitlements)
    plain = [
        e
        for e in world.entitlements.values()
        if e.requestable
        and not e.privileged
        and e.max_duration_days is None
        and e.risk_tier.value in ("low", "medium")
    ]
    chosen = sorted(plain, key=lambda e: e.id)[::9]
    for days, entitlement in zip(itertools.cycle((14, 30, 60, 90)), chosen, strict=False):
        entitlements[entitlement.id] = entitlement.model_copy(update={"max_duration_days": days})
    return world.model_copy(update={"tickets": tickets, "entitlements": entitlements})


def test_templates_for_material_other_worlds_hold(midmarket_world: World) -> None:
    """``break_glass_on_leave`` and ``time_boxed_approval`` find nothing in generated midmarket
    worlds (``WORLD_DEPENDENT``). Given the material, their drafts land in their cells: a valid
    break-glass request someone on leave files for themselves is escalated by LIFE-002 with
    APPR-004 alongside, and TEMP-002 caps approvals, binding some. In a build, the self-filed
    break-glass escalations let break-glass approvals and denials be self-filed too, while who
    files still matches across labels within the break-glass requests."""
    world = _with_rarer_material(midmarket_world)
    material = Material(world)
    assert material.incident_assignees_on_leave
    assert not Material(midmarket_world).incident_assignees_on_leave
    views = ViewBuilder(world)
    rng = random.Random(6)
    for name in ("break_glass_on_leave", "time_boxed_approval"):
        scenario = scenario_named(name)
        for tier in scenario.tiers:
            for _ in range(4):
                draft = scenario.draw(material, rng, Ask(scenario.decision, tier))
                assert draft is not None, (name, tier)
                view = views.build_view(draft.request(submitted_on=world.anchor_date))
                gold = decide_request(view)
                landed = compute_tier(view, gold, Task.access_request)
                tags = compute_tags(view, gold, Task.access_request)
                assert scenario.accepts(gold.decision, landed, tags), (name, tier, gold)
                material.note_use(draft.beneficiary_id, draft.entitlement_id, gold.decision)
                findings = {f.rule for f in gold.findings}
                if name == "break_glass_on_leave":
                    # LIFE-002 escalates it and APPR-004 caps it; any other finding is a cap
                    # too (TEMP-001 for privileged access an assignee may need).
                    assert draft.requester_id == draft.beneficiary_id and draft.is_break_glass
                    assert {"LIFE-002", "APPR-004"} <= findings
                    assert findings - {"LIFE-002"} <= CONDITION_RULES, findings
                else:
                    assert "TEMP-002" in findings and findings <= {"TEMP-002", "LIFE-004"}

    cases, report = build_request_cases_with_report(world, split=Split.test, n=150, seed=3)
    used = Counter(c.provenance["scenario"] for c in cases)
    assert used["break_glass_on_leave"] and used["time_boxed_approval"], used
    glass = [c for c in cases if _request(c).is_break_glass]
    self_filed = [c for c in glass if _request(c).requester_id == _request(c).beneficiary_id]
    assert {_gold(c).decision for c in self_filed} == set(RequestDecision)
    assert report.glass_on_behalf == len(glass) - len(self_filed)
    for row in report.balance:
        if row["feature"] == "on_behalf" and str(row["stratum"]).endswith("break_glass=yes"):
            assert not row["unbalanced"], row
    binding = Counter(
        cert.rule
        for c in cases
        if _gold(c).decision is RequestDecision.APPROVE
        for cert in _gold(c).certificates
    )
    assert binding["TEMP-002"] > 0, binding


def test_predict_mirrors_the_oracle(midmarket_world: World) -> None:
    """``Material.predict`` steers every draw, so it must name exactly the rules the oracle finds
    for a self-service request without break-glass, with and without a usable ticket."""
    material = Material(midmarket_world)
    views = ViewBuilder(midmarket_world)
    rng = random.Random(4)
    people = sorted(midmarket_world.identities)
    entitlements = sorted(midmarket_world.entitlements)
    request_dependent = {"APPR-002", "APPR-003", "APPR-004"}
    checked = 0
    while checked < 400:
        beneficiary, entitlement = rng.choice(people), rng.choice(entitlements)
        if entitlement in material.held(beneficiary):
            continue
        usable = material.usable_tickets(entitlement)
        for ticket in (None, usable[0].id if usable else None):
            request = AccessRequest(
                request_id="req_check",
                requester_id=beneficiary,
                beneficiary_id=beneficiary,
                entitlement_id=entitlement,
                ticket_ref=ticket,
                justification="",
                submitted_on=midmarket_world.anchor_date,
            )
            gold = decide_request(views.build_view(request))
            found = {f.rule for f in gold.findings} - request_dependent
            expected = material.predict(beneficiary, entitlement, ticket=ticket is not None)
            assert found == expected, (beneficiary, entitlement, ticket)
        checked += 1


def test_rule_sets_come_from_the_registry() -> None:
    assert {"LIFE-001", "LIFE-003", "ELIG-004", "SOD-002", "APPR-003"} <= DENY_RULES
    assert {"LIFE-002", "APPR-001", "APPR-002", "JUST-001"} == ESCALATE_RULES
    assert {
        "LIFE-004",
        "MIT-003",
        "APPR-004",
        "TEMP-001",
        "TEMP-002",
    } == CONDITION_RULES
    assert not (DENY_RULES & ESCALATE_RULES) and not (DENY_RULES & CONDITION_RULES)


def test_requested_durations_straddle_the_binding_cap() -> None:
    rng = random.Random(0)
    draws = [approval_duration(rng, [7, 30]) for _ in range(4000)]
    standing = sum(d is None for d in draws) / len(draws)
    assert abs(standing - STANDING_SHARE) < 0.03
    values = Counter(d for d in draws if d is not None)
    assert set(values) <= set(DURATION_CHOICES)
    # The longest choice the cap leaves alone (the cap itself here) and the shortest it shortens.
    assert values[7] > 0 and values[10] > 0
    assert any(d < 7 for d in values) and any(d > 10 for d in values)
    # A contract ending in 29 days: 21 days binds the request, 30 the cap, by one day; 29 days
    # itself, an odd number, is never asked for.
    contract = {placed_duration(rng, [29]) for _ in range(500)}
    assert {21, 30} <= contract <= set(DURATION_CHOICES)
    assert {placed_duration(rng, []) for _ in range(500)} <= set(DURATION_CHOICES)
    assert {1, 2} <= {placed_duration(rng, [1]) for _ in range(500)}
    # No choice is longer than a cap of 400 days: only the cap binding a standing request can.
    assert max(placed_duration(rng, [400]) for _ in range(200)) == 365


def test_break_glass_windows_are_short_and_shortened_by_the_cap() -> None:
    """A valid break-glass request outside L1 asks for an emergency window the one-day cap
    shortens: round, two weeks at most, two days (the near miss by a day) half the time, never
    the day itself (only L1's ask for it) and never standing access."""
    assert list(GLASS_DURATIONS) == sorted(GLASS_DURATIONS)
    assert set(GLASS_DURATIONS) <= set(DURATION_CHOICES)
    assert min(GLASS_DURATIONS) == BREAK_GLASS_CAP_DAYS + 1 and max(GLASS_DURATIONS) <= 14
    assert GLASS_DURATIONS.count(BREAK_GLASS_CAP_DAYS + 1) * 2 == len(GLASS_DURATIONS)
    rng = random.Random(0)
    draws = Counter(glass_duration(rng) for _ in range(4000))
    assert set(draws) == set(GLASS_DURATIONS)
    assert abs(draws[BREAK_GLASS_CAP_DAYS + 1] / 4000 - 0.5) < 0.03


def test_material_bookkeeping(midmarket_world: World) -> None:
    approve, deny = RequestDecision.APPROVE, RequestDecision.DENY
    material = Material(midmarket_world, max_uses=2, max_entitlement_uses=2, max_label_uses=1)
    person = material.current[0]
    held = material.held(person)
    assert all(not material.free(person, e, approve) for e in held)
    plain = [
        e
        for e in sorted(midmarket_world.entitlements)
        if e not in held and e not in material.scarce_entitlements
    ]
    wanted, spare = plain[0], plain[1]
    assert material.free(person, wanted, approve) and material.available(person)
    assert material.entitlement_rank(wanted, deny) == (0, 0)
    material.note_use(person, wanted, approve)
    assert not material.free(person, wanted, deny)  # the pair is taken
    # Popular under APPROVE: ranked first for the other decisions, last for APPROVE.
    assert (
        material.entitlement_rank(wanted, deny)
        < (0, 0)
        < material.entitlement_rank(wanted, approve)
    )
    others = [p for p in material.current[1:] if wanted not in material.held(p)]
    assert not material.free(others[0], wanted, approve)  # one APPROVE use per build
    assert material.free(others[0], wanted, deny)
    material.note_use(others[0], wanted, deny)
    assert not material.free(others[1], wanted, RequestDecision.ESCALATE)  # two uses in all
    # A released case gives its uses back; its pair stays blocked.
    material.release(others[0], wanted, deny)
    assert material.uses[others[0]] == 0 and material.entitlement_labels[wanted][deny] == 0
    assert material.free(others[1], wanted, RequestDecision.ESCALATE)
    assert not material.free(others[0], wanted, deny)
    material.note_use(person, spare, RequestDecision.ESCALATE)
    assert not material.available(person)  # the beneficiary reached their cap
    material.block(others[1], spare)
    assert not material.free(others[1], spare, approve)
    with pytest.raises(ValueError):
        Material(midmarket_world, max_label_uses=0)


def test_scarce_entitlements_get_larger_caps(midmarket_world: World) -> None:
    material = Material(midmarket_world, max_entitlement_uses=2, max_label_uses=1)
    scarce = material.scarce_entitlements
    approvable_privileged = {
        e.id
        for e in midmarket_world.entitlements.values()
        if e.requestable and e.privileged and e.risk_tier.value in ("low", "medium")
    }
    assert approvable_privileged and approvable_privileged <= scarce
    for rule_id in material.sides:
        requested = {e for _, e in material.side_pairs(rule_id)}
        if len(requested) == 1:
            assert requested <= scarce, rule_id
    some = sorted(scarce)[0]
    assert material.label_cap(some) == SCARCE_CAP_FACTOR
    assert material.entitlement_cap(some) == 2 * SCARCE_CAP_FACTOR
    plain = next(e for e in sorted(midmarket_world.entitlements) if e not in scarce)
    assert material.label_cap(plain) == 1 and material.entitlement_cap(plain) == 2


def test_material_people_and_conflicts(midmarket_world: World) -> None:
    material = Material(midmarket_world)
    as_of = midmarket_world.anchor_date
    for identity_id in material.current:
        person = midmarket_world.identities[identity_id]
        assert person.status.value == "active" and not material.contract_lapsed(person)
    for identity_id in material.lapsed_contractors:
        person = midmarket_world.identities[identity_id]
        assert person.employment_type is EmploymentType.contractor
        assert person.contract_end_date is not None and person.contract_end_date < as_of
    # Terminated people who can still sign in: some, not all.
    signed_in = set(material.terminated_signed_in)
    assert signed_in and signed_in < set(material.terminated)
    for identity_id in material.terminated:
        accounts = [a for a in midmarket_world.accounts.values() if a.identity_id == identity_id]
        active = any(a.status is AccountStatus.active for a in accounts)
        assert active is (identity_id in signed_in), identity_id
    for identity_id in material.notice_period:
        end = midmarket_world.identities[identity_id].termination_date
        assert end is not None and end > as_of
    for incident in material.open_incidents:
        assert incident.status.value == "open" and incident.assignee_id is not None
    for incident in material.unassigned_incidents:
        assert incident.status.value == "open" and incident.assignee_id is None
    # Every mitigated pair introduces a conflict a valid control mitigates (MIT-001).
    for beneficiary, entitlement in material.mitigated_pairs:
        conflicts = material.conflicts(beneficiary, entitlement)
        assert conflicts and any(mitigated for _, mitigated in conflicts)
    # Lapsed side pairs complete no conflict: the other side is on file but not held.
    for beneficiary, entitlement in material.lapsed_side_pairs(recent=False):
        assert entitlement not in material.held(beneficiary)
    keys = material.person_keys(material.current[0])
    assert [k.split(":")[0] for k in keys] == [
        "type",
        "department",
        "accounts",
        "memberships",
        "assignments",
    ]
    assert {k.split(":")[1] for k in keys[2:]} <= {"0", "1", "2", "3", "4"}
    some_entitlement = sorted(midmarket_world.entitlements)[0]
    ent_keys = material.entitlement_keys(some_entitlement)
    assert [k.split(":")[0] for k in ent_keys] == ["risk", "privileged", "description"]
    # Description bands: quartiles, with the longest twentieth apart.
    bands = Counter(material.entitlement_keys(e)[2] for e in sorted(midmarket_world.entitlements))
    n = len(midmarket_world.entitlements)
    assert len(bands) == 5 and bands["description:4"] <= n / 10
    assert all(bands[f"description:{b}"] > n / 8 for b in range(4)), bands


def test_closure_sizes_count_what_a_request_view_holds(midmarket_world: World) -> None:
    """``Material.closure_sizes`` (what ``Ask.wanted`` balances by its mean) counts the
    beneficiary's share of each view-size probe feature, as ``ViewBuilder`` cuts a request view:
    the requester is the beneficiary, so only the requested entitlement, its application and
    the application's owner come in beside the closure."""
    material = Material(midmarket_world)
    views = ViewBuilder(midmarket_world)
    assert len(CLOSURE_SIZES) == 9
    for person in material.current[::150]:
        sizes = dict(zip(CLOSURE_SIZES, material.closure_sizes(person), strict=True))
        entitlement = next(
            e for e in sorted(midmarket_world.entitlements) if e not in material.held(person)
        )
        view = views.build_view(
            AccessRequest(
                request_id="req_check",
                requester_id=person,
                beneficiary_id=person,
                entitlement_id=entitlement,
                justification="",
                submitted_on=midmarket_world.anchor_date,
            )
        )
        accounts = [a for a in view.accounts.values() if a.identity_id == person]
        assert sizes["accounts"] == len(accounts)
        assert sizes["memberships"] == len(view.memberships)
        assert sizes["assignments"] == len(view.assignments)
        assert sizes["roles"] == len(view.roles)
        assert sizes["child_links"] == sum(len(r.child_roles) for r in view.roles.values())
        assert sizes["role_links"] == sum(len(r.entitlements) for r in view.roles.values())
        assert sizes["entitlements"] in (len(view.entitlements) - 1, len(view.entitlements))
        assert sizes["applications"] <= len(view.applications) <= sizes["applications"] + 1
        assert sizes["usage"] == len(view.usage)
        # Profiles carry the counts standardized over the world, next to the 0/1 keys.
        profile = material.person_profile(person)
        assert {f"size:{name}" for name in CLOSURE_SIZES} <= set(profile)
        assert all(profile[key] == 1.0 for key in material.person_keys(person))
    means = [
        statistics.fmean(
            material.person_profile(p)[f"size:{name}"] for p in midmarket_world.identities
        )
        for name in CLOSURE_SIZES
    ]
    assert all(abs(m) < 1e-9 for m in means), means
    description = material.entitlement_profile(sorted(midmarket_world.entitlements)[0])
    assert "size:description" in description
    # Every build of one world shares the world's lookups.
    first = material.current[0]
    assert Material(midmarket_world).held(first) is material.held(first)


def test_templates_behave_as_their_names_say(build300: Build) -> None:
    by_scenario: dict[str, list[Case]] = {}
    for case in build300.cases:
        by_scenario.setdefault(case.provenance["scenario"], []).append(case)

    def each(name: str) -> list[Case]:
        found = by_scenario.get(name, [])
        assert found, name
        return found

    for case in each("terminated_holder"):
        assert "LIFE-001" in _findings(case)
        # Offboarding missed them: an account still active, so they can file it themselves.
        beneficiary = _request(case).beneficiary_id
        accounts = [a for a in case.view.accounts.values() if a.identity_id == beneficiary]
        assert any(a.status is AccountStatus.active for a in accounts), case.case_id
    for case in each("contract_lapsed"):
        assert {"LIFE-003", "LIFE-004"} <= _findings(case)
    for case in each("on_leave"):
        assert "LIFE-002" in _findings(case)
    for case in each("break_glass_bypass"):
        assert _request(case).is_break_glass
        assert "APPR-004" in _findings(case) and "APPR-001" not in _findings(case)
        assert _gold(case).risk_tier.value in ("high", "critical")
    for name in ("break_glass_invalid", "break_glass_no_ticket", "break_glass_invalid_sod"):
        for case in each(name):
            assert _request(case).is_break_glass and "APPR-003" in _findings(case)
    for case in each("break_glass_no_ticket"):
        assert _request(case).ticket_ref is None and case.tier is Tier.L1
    for case in each("break_glass_invalid_sod"):
        assert case.tier is Tier.L3 and any(r.startswith("SOD-") for r in _findings(case))
        assert _gold(case).decision is RequestDecision.DENY
    for case in by_scenario.get("break_glass_unassigned", []):
        ticket = case.view.tickets[_request(case).ticket_ref or ""]
        assert ticket.assignee_id is None and ticket.status is TicketStatus.open
    for case in each("peer_on_behalf"):
        assert "APPR-002" in _findings(case)
    # Approvable mitigated conflicts are world-dependent (``WORLD_DEPENDENT``); escalations and
    # denials past a valid control are in every midmarket world.
    mitigated = [*by_scenario.get("mitigated_approval", [])]
    mitigated += each("mitigated_escalation") + each("mitigated_denial")
    for case in mitigated:
        applies = {x.applies for x in _gold(case).exceptions if x.rule == "MIT-001"}
        assert True in applies and "MIT-003" in _findings(case)
    for case in each("sod_expired_control"):
        assert "expired_exception" in case.tags
        assert any(r.startswith("SOD-") for r in _findings(case))
    for case in each("sod_nested"):
        assert "nested_role" in case.tags
    for case in each("missing_ticket"):
        assert "JUST-001" in _findings(case)
    for case in each("high_risk"):
        assert "APPR-001" in _findings(case)
    for case in each("time_boxed_escalation"):
        assert {"APPR-001", "TEMP-002"} <= _findings(case)
    for case in each("clean_approval"):
        assert _findings(case) <= {"LIFE-004"}
    for name, rule in (
        ("prod_admin", "ELIG-001"),
        ("restricted_data", "ELIG-002"),
        ("region_mismatch", "ELIG-003"),
        ("not_requestable", "ELIG-004"),
    ):
        for case in each(name):
            assert rule in _findings(case), name
    assert all(
        _gold(c).decision is RequestDecision.APPROVE
        for name in ("clean_approval", "privileged_grant", "contractor_term")
        for c in by_scenario.get(name, [])
    )


# --------------------------------------------------------------------------------------------
# Bounded work, shortfalls and the builder's own guards
# --------------------------------------------------------------------------------------------


def test_a_template_that_keeps_missing_is_retired_and_its_slots_move(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    """Drafts that land outside every open cell, or that the world can't give a complete view
    (an entitlement it doesn't have), are misses; after ``MAX_MISSES`` the template is retired
    and its slots go elsewhere instead of looping forever."""
    clean = scenario_named("clean_approval")
    calls = 0

    def wander(material: Material, rng: random.Random, ask: Ask) -> Draft | None:
        nonlocal calls
        calls += 1
        draft = clean.draw(material, rng, Ask(RequestDecision.APPROVE, Tier.L1, ask.wanted))
        if draft is None:
            return None
        if calls % 2:
            return replace(draft, scenario="wanderer", entitlement_id="ent_not_in_world")
        return replace(draft, scenario="wanderer", requested_duration_days=None)

    wanderer = Scenario(
        "wanderer", RequestDecision.APPROVE, (Tier.L2, Tier.L3), frozenset(), 1000.0, wander
    )
    monkeypatch.setattr(requests_module, "SCENARIOS", (*SCENARIOS, wanderer))
    monkeypatch.setattr(requests_module, "MAX_MISSES", 4)
    cases, report = build_request_cases_with_report(startup_world, split="test", n=30, seed=3)
    assert len(cases) == 30
    assert ("wanderer", Cell(RequestDecision.APPROVE, Tier.L2)) in report.retired
    assert "wanderer" not in report.scenarios
    assert {s.cell.decision for s in report.shortfalls} >= {RequestDecision.APPROVE}
    assert report.draws > 30


def test_a_template_is_retired_only_after_draws_in_a_row_find_nothing(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    """A draw samples candidates, so one that finds nothing may be bad luck: a template is
    retired from a cell after ``RETIRE_AFTER_NONES`` empty draws in a row, not at the first."""
    clean = scenario_named("clean_approval")
    calls: Counter[str] = Counter()

    def flaky(material: Material, rng: random.Random, ask: Ask) -> Draft | None:
        calls["flaky"] += 1
        if calls["flaky"] % RETIRE_AFTER_NONES:  # empty draws, one short of retiring, then one
            return None
        draft = clean.draw(material, rng, Ask(RequestDecision.APPROVE, Tier.L1, ask.wanted))
        return None if draft is None else replace(draft, scenario="flaky")

    def empty(material: Material, rng: random.Random, ask: Ask) -> Draft | None:
        del material, rng, ask
        calls["empty"] += 1
        return None

    approve_l1 = (Tier.L1,)
    catalog = (
        *SCENARIOS,
        Scenario("flaky", RequestDecision.APPROVE, approve_l1, frozenset(), 1000.0, flaky),
        Scenario("empty", RequestDecision.APPROVE, approve_l1, frozenset(), 1000.0, empty),
    )
    monkeypatch.setattr(requests_module, "SCENARIOS", catalog)
    cases, report = build_request_cases_with_report(startup_world, split="test", n=30, seed=3)
    assert len(cases) == 30
    cell = Cell(RequestDecision.APPROVE, Tier.L1)
    assert ("empty", cell) in report.retired and calls["empty"] == RETIRE_AFTER_NONES
    assert ("flaky", cell) not in report.retired
    assert report.scenarios["flaky"] > 0


def test_a_decision_short_of_break_glass_evens_out_the_others(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    """When one decision can't fill its break-glass slots, the others release break-glass cases
    until every decision holds the same share, and other templates refill those slots."""

    def nothing(material: Material, rng: random.Random, ask: Ask) -> Draft | None:
        del material, rng, ask
        return None

    catalog = tuple(
        replace(s, draw=nothing) if s.name == "break_glass_escalated" else s for s in SCENARIOS
    )
    monkeypatch.setattr(requests_module, "SCENARIOS", catalog)
    cases, report = build_request_cases_with_report(startup_world, split="test", n=40, seed=5)
    assert len(cases) == 40 and not report.mix_changed
    assert not any(_request(c).is_break_glass for c in cases)
    assert sum(report.released.values()) > 0
    assert any(s.glass and s.moved_to == s.cell for s in report.shortfalls)
    assert "break-glass released for equal shares" in report.summary()


def test_a_decision_without_material_changes_the_mix_and_says_so(
    monkeypatch: pytest.MonkeyPatch, startup_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    def nothing(material: Material, rng: random.Random, ask: Ask) -> Draft | None:
        del material, rng, ask
        return None

    catalog = (
        *(s for s in SCENARIOS if s.decision is not RequestDecision.ESCALATE),
        Scenario(
            "nobody",
            RequestDecision.ESCALATE,
            (Tier.L1, Tier.L2, Tier.L3),
            frozenset(),
            1.0,
            nothing,
        ),
    )
    monkeypatch.setattr(requests_module, "SCENARIOS", catalog)
    with caplog.at_level(logging.WARNING, logger=requests_module.__name__):
        cases = build_request_cases(startup_world, split="test", n=20, seed=2)
    assert len(cases) == 20
    assert not any(_gold(c).decision is RequestDecision.ESCALATE for c in cases)
    assert "changed its decision mix" in caplog.text


def test_plan_refuses_a_cell_no_template_reaches(monkeypatch: pytest.MonkeyPatch) -> None:
    approvals = tuple(s for s in SCENARIOS if s.decision is RequestDecision.APPROVE)
    monkeypatch.setattr(requests_module, "SCENARIOS", approvals)
    assert len(plan_cells(10, {RequestDecision.APPROVE: 1.0})) == 10
    with pytest.raises(ValueError, match="no request template reaches"):
        plan_cells(10)


def test_finishing_that_moved_a_case_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    """Finishing only touches gold-neutral fields; if it ever changed a tier, the build must
    stop rather than store a case in the wrong cell. Drafts carry no justification, finished
    requests do, so a patched ``compute_tier`` can drift on finished views alone."""
    real = requests_module.compute_tier

    def drifting(view: CaseView, gold: RequestGold, task: Task) -> Tier:
        tier = real(view, gold, task)
        if view.request is not None and view.request.justification:
            return Tier.L2 if tier is Tier.L1 else Tier.L1
        return tier

    monkeypatch.setattr(requests_module, "compute_tier", drifting)
    with pytest.raises(RuntimeError, match="finishing moved"):
        build_request_cases(startup_world, split="test", n=6, seed=1)


def test_finishing_that_dropped_required_tags_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, startup_world: World
) -> None:
    real = requests_module.compute_tags

    def forgetful(
        view: CaseView, gold: RequestGold, task: Task, *, distractors: int = 0
    ) -> tuple[str, ...]:
        tags = real(view, gold, task, distractors=distractors)
        if view.request is not None and view.request.justification:
            return ()
        return tags

    monkeypatch.setattr(requests_module, "compute_tags", forgetful)
    with pytest.raises(RuntimeError, match="dropped required tags"):
        build_request_cases(startup_world, split="test", n=12, seed=1)


def test_templates_report_when_their_material_runs_out(startup_world: World) -> None:
    material = Material(startup_world, exclude=frozenset(startup_world.identities))
    rng = random.Random(1)
    for scenario in SCENARIOS:
        for tier in scenario.tiers:
            assert scenario.draw(material, rng, Ask(scenario.decision, tier)) is None, (
                scenario.name,
                tier,
            )


def test_requesters_of_each_kind(midmarket_world: World) -> None:
    material = Material(midmarket_world)
    rng = random.Random(2)
    world = midmarket_world
    person = next(p for p in material.current if world.identities[p].manager_id is not None)
    manager = world.identities[person].manager_id
    entitlement = next(
        e.id for e in world.entitlements.values() if world.applications[e.app_id].owner_id != person
    )
    owner = world.applications[world.entitlements[entitlement].app_id].owner_id
    assert material.requester(rng, person, entitlement, Requester.self) == person
    assert material.requester(rng, person, entitlement, Requester.manager) == manager
    assert material.requester(rng, person, entitlement, Requester.owner) == owner
    peer = material.requester(rng, person, entitlement, Requester.peer)
    assert peer is not None and peer not in (person, manager, owner)
    assert world.identities[peer].status is IdentityStatus.active
    # No manager: nobody files as the manager, and a peer comes from the same department.
    top = next(i for i in world.identities.values() if i.manager_id is None)
    assert material.requester(rng, top.id, entitlement, Requester.manager) is None
    colleague = material.requester(rng, top.id, entitlement, Requester.peer)
    assert colleague is not None and world.identities[colleague].department == top.department
    # The owner asking for their own application's access is self-service, not on behalf.
    app = next(a for a in world.applications.values() if a.owner_id in world.identities)
    owned = next(e.id for e in world.entitlements.values() if e.app_id == app.id)
    assert material.requester(rng, app.owner_id, owned, Requester.owner) is None
