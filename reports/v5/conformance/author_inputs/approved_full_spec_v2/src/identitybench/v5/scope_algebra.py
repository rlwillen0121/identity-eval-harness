"""Finite leaf-scope capability and common identity predicates for v5.

These helpers implement the shared algebra in SPEC-v5.md §3.0.  They are pure
functions over delivered state and do not import the oracle or verifier.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .schemas import (
    CompensatingControlV5,
    EngagementV5,
    GrantV5,
    GroupV5,
    HRPersonV5,
    IdPAccountV5,
    RoleV5,
    ScopeCatalogV5,
    ScopedSoDRuleV5,
)

CapabilityLeaf = tuple[str, str]
CapabilityFootprint = frozenset[CapabilityLeaf]
SoDWitness = tuple[str, str, str, str, str]


def resolve_scope(scope_catalog: ScopeCatalogV5, scope_path: str) -> frozenset[str]:
    """Resolve a path using the catalog's exact-or-descendant semantics."""

    return scope_catalog.leaves(scope_path)


def scope_leq(scope_catalog: ScopeCatalogV5, left_path: str, right_path: str) -> bool:
    """Return whether every leaf in ``left_path`` is also in ``right_path``."""

    return resolve_scope(scope_catalog, left_path) <= resolve_scope(scope_catalog, right_path)


def scope_meet(scope_catalog: ScopeCatalogV5, *scope_paths: str) -> frozenset[str]:
    """Return the intersection of resolved paths; zero paths meet to the universe."""

    leaves = frozenset(scope_catalog.leaf_scopes)
    for scope_path in scope_paths:
        leaves &= resolve_scope(scope_catalog, scope_path)
    return leaves


def normalize_footprint(
    footprint: Iterable[CapabilityLeaf], scope_catalog: ScopeCatalogV5
) -> CapabilityFootprint:
    """Freeze a footprint and reject leaves outside the finite catalog."""

    universe = frozenset(scope_catalog.leaf_scopes)
    normalized: set[CapabilityLeaf] = set()
    for capability, leaf in footprint:
        if leaf not in universe:
            raise ValueError(f"Unknown leaf scope {leaf!r}")
        normalized.add((capability, leaf))
    return frozenset(normalized)


def capability_footprint(
    scope_catalog: ScopeCatalogV5,
    capabilities: Iterable[str],
    scope_paths: Iterable[str],
) -> CapabilityFootprint:
    """Expand capabilities over the union of the supplied scope paths."""

    capability_set = set(capabilities)
    leaves = frozenset().union(*(resolve_scope(scope_catalog, path) for path in scope_paths))
    return frozenset((capability, leaf) for capability in capability_set for leaf in leaves)


def footprint_leq(left: CapabilityFootprint, right: CapabilityFootprint) -> bool:
    """Return whether ``left`` is a subset of ``right``."""

    return left <= right


def footprint_meet(*footprints: CapabilityFootprint) -> CapabilityFootprint:
    """Intersect footprints; with no operands, return the empty finite footprint."""

    if not footprints:
        return frozenset()
    result = set(footprints[0])
    for footprint in footprints[1:]:
        result.intersection_update(footprint)
    return frozenset(result)


def footprint_join(*footprints: CapabilityFootprint) -> CapabilityFootprint:
    """Union footprints."""

    return frozenset().union(*footprints)


def group_closure(
    account: IdPAccountV5, groups_by_id: Mapping[str, GroupV5]
) -> frozenset[str]:
    """Return the cycle-safe breadth-first closure of delivered account groups."""

    visited: set[str] = set()
    queue = deque(group_id for group_id in account.group_ids if group_id in groups_by_id)
    while queue:
        group_id = queue.popleft()
        if group_id in visited:
            continue
        visited.add(group_id)
        queue.extend(
            parent_id
            for parent_id in groups_by_id[group_id].member_of_group_ids
            if parent_id in groups_by_id and parent_id not in visited
        )
    return frozenset(visited)


def group_footprints(
    account: IdPAccountV5,
    groups_by_id: Mapping[str, GroupV5],
    roles_by_id: Mapping[str, RoleV5],
    scope_catalog: ScopeCatalogV5,
) -> tuple[CapabilityFootprint, CapabilityFootprint]:
    """Return ``(F_grp, F_deny)`` for cycle-safe group closure and deny masking."""

    allow: set[CapabilityLeaf] = set()
    deny: set[CapabilityLeaf] = set()
    for group_id in group_closure(account, groups_by_id):
        group = groups_by_id[group_id]
        capabilities = set(group.capabilities)
        for role_id in group.granted_role_ids:
            capabilities.update(roles_by_id[role_id].capabilities)
        target = deny if group.effect == "DENY" else allow
        target.update(capability_footprint(scope_catalog, capabilities, (group.scope_path,)))
    return frozenset(allow - deny), frozenset(deny)


def effective_grant_footprint(
    grant: GrantV5,
    roles_by_id: Mapping[str, RoleV5],
    group_deny: CapabilityFootprint,
    scope_catalog: ScopeCatalogV5,
) -> CapabilityFootprint:
    """Expand a grant role over its scope and subtract the account's group DENYs."""

    role = roles_by_id[grant.role_id]
    raw = capability_footprint(scope_catalog, role.capabilities, (grant.scope_path,))
    return raw - group_deny


def sod_witnesses(
    first: CapabilityFootprint,
    second: CapabilityFootprint,
    rules: Iterable[ScopedSoDRuleV5],
) -> frozenset[SoDWitness]:
    """Return symmetric, canonical witnesses for all violated scoped SoD rules."""

    witnesses: set[SoDWitness] = set()
    for rule in rules:
        for cap_a, leaf_a in first:
            for cap_b, leaf_b in second:
                if cap_a == cap_b or {cap_a, cap_b} != {rule.cap_a, rule.cap_b}:
                    continue
                if rule.scope_mode == "SAME_LEAF_SCOPE" and leaf_a != leaf_b:
                    continue
                (cap_1, leaf_1), (cap_2, leaf_2) = sorted(
                    ((cap_a, leaf_a), (cap_b, leaf_b))
                )
                witnesses.add((rule.rule_code, cap_1, leaf_1, cap_2, leaf_2))
    return frozenset(witnesses)


def syntactically_mitigates(
    control: CompensatingControlV5,
    person_id: str,
    witness: SoDWitness,
    sod_rule: ScopedSoDRuleV5,
    scope_catalog: ScopeCatalogV5,
) -> bool:
    """Check all §3.0 control conditions, including GLOBAL full-universe coverage."""

    rule_code, cap_1, leaf_1, cap_2, leaf_2 = witness
    if control.monitored_person_id != person_id or rule_code != sod_rule.rule_code:
        return False
    if {control.cap_a, control.cap_b} != {cap_1, cap_2}:
        return False
    covered = resolve_scope(scope_catalog, control.covered_scope_path)
    if not {leaf_1, leaf_2} <= covered:
        return False
    return sod_rule.scope_mode != "GLOBAL" or covered == resolve_scope(scope_catalog, "/*")


def unmitigated_witnesses(
    first: CapabilityFootprint,
    second: CapabilityFootprint,
    person_id: str,
    controls: Iterable[CompensatingControlV5],
    rules: Iterable[ScopedSoDRuleV5],
    scope_catalog: ScopeCatalogV5,
) -> frozenset[SoDWitness]:
    """Return SoD witnesses not syntactically covered by any supplied control."""

    rules_tuple = tuple(rules)
    controls_tuple = tuple(controls)
    unmitigated: set[SoDWitness] = set()
    for witness in sod_witnesses(first, second, rules_tuple):
        rule_code, cap_1, leaf_1, cap_2, leaf_2 = witness
        matching_rules = tuple(
            rule
            for rule in rules_tuple
            if rule.rule_code == rule_code
            and {rule.cap_a, rule.cap_b} == {cap_1, cap_2}
            and (rule.scope_mode == "GLOBAL" or leaf_1 == leaf_2)
        )
        if any(
            not any(
                syntactically_mitigates(
                    control, person_id, witness, rule, scope_catalog
                )
                for control in controls_tuple
            )
            for rule in matching_rules
        ):
            unmitigated.add(witness)
    return frozenset(unmitigated)


def engagement_is_live(engagement: EngagementV5, clock_s: int) -> bool:
    """Evaluate ACTIVE and CONTRACTOR_TO_FTE_PENDING engagement liveness at a clock."""

    retro_live = (
        engagement.retro_terminated_effective_s is None
        or clock_s < engagement.retro_terminated_effective_s
    )
    if engagement.eng_status == "ACTIVE":
        return (
            engagement.start_s <= clock_s
            and (engagement.end_s is None or clock_s < engagement.end_s)
            and retro_live
        )
    if engagement.eng_status == "CONTRACTOR_TO_FTE_PENDING":
        return (
            engagement.conversion_start_s is not None
            and engagement.conversion_grace_deadline_s is not None
            and clock_s < engagement.conversion_grace_deadline_s
            and retro_live
        )
    return False


def has_live_engagement(person: HRPersonV5, clock_s: int) -> bool:
    """Return whether a person has any engagement live at ``clock_s``."""

    return any(engagement_is_live(engagement, clock_s) for engagement in person.engagements)


def effective_department(engagement: EngagementV5, clock_s: int) -> str:
    """Resolve the engagement department before or after its effective transfer."""

    if (
        engagement.dept_transfer_effective_at_s is not None
        and engagement.prior_dept_path is not None
        and clock_s < engagement.dept_transfer_effective_at_s
    ):
        return engagement.prior_dept_path
    return engagement.dept_path


def is_subordinate(
    subordinate_person_id: str,
    manager_person_id: str,
    clock_s: int,
    people_by_id: Mapping[str, HRPersonV5],
) -> bool:
    """Follow manager edges on live engagements; require at least one hop."""

    if subordinate_person_id == manager_person_id:
        return False
    visited = {subordinate_person_id}
    queue = deque([subordinate_person_id])
    while queue:
        person_id = queue.popleft()
        person = people_by_id.get(person_id)
        if person is None:
            continue
        managers = {
            engagement.manager_person_id
            for engagement in person.engagements
            if engagement_is_live(engagement, clock_s)
            and engagement.manager_person_id is not None
        }
        for next_manager_id in managers:
            if next_manager_id == manager_person_id:
                return True
            if next_manager_id not in visited:
                visited.add(next_manager_id)
                queue.append(next_manager_id)
    return False


@dataclass(frozen=True, slots=True)
class ScopeAlgebraV5:
    """Convenience object for the finite scope and footprint lattice."""

    scope_catalog: ScopeCatalogV5

    def leaves(self, scope_path: str) -> frozenset[str]:
        return resolve_scope(self.scope_catalog, scope_path)

    def scope_leq(self, left_path: str, right_path: str) -> bool:
        return scope_leq(self.scope_catalog, left_path, right_path)

    def scope_meet(self, *scope_paths: str) -> frozenset[str]:
        return scope_meet(self.scope_catalog, *scope_paths)

    def capability_footprint(
        self, capabilities: Iterable[str], scope_paths: Iterable[str]
    ) -> CapabilityFootprint:
        return capability_footprint(self.scope_catalog, capabilities, scope_paths)

    def normalize(self, footprint: Iterable[CapabilityLeaf]) -> CapabilityFootprint:
        return normalize_footprint(footprint, self.scope_catalog)
