"""Role nesting and separation-of-duties helpers used while generating and checking worlds.

The oracle has its own role expansion in ``policy.access`` (it works on a ``CaseView``); this
module serves the generator, which needs the same semantics over a whole catalog plus
cycle/depth diagnostics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from identitybench.world.entities import PolicyPack, Role

MAX_ROLE_DEPTH = 3
"""Roles nest up to three levels: a role, its child, and its grandchild (SPEC §6.2)."""


class RoleCycleError(ValueError):
    """The role graph contains a cycle."""


class RoleGraph:
    """Memoized expansion and depth over a fixed set of roles.

    Unknown child IDs are ignored here (referential integrity is reported by ``checks``), so
    the graph can be inspected even when a catalog is broken.
    """

    def __init__(self, roles: Mapping[str, Role]) -> None:
        self._roles = roles
        self._expansion: dict[str, frozenset[str]] = {}
        self._depth: dict[str, int] = {}

    def expansion(self, role_id: str) -> frozenset[str]:
        """Every entitlement the role confers, through children at any depth.

        Raises ``RoleCycleError`` if the role sits on a cycle.
        """
        return self._expand(role_id, ())

    def depth(self, role_id: str) -> int:
        """Levels in the longest nesting chain starting at ``role_id`` (a leaf role is 1)."""
        self.expansion(role_id)
        return self._depth.get(role_id, 0)

    def _expand(self, role_id: str, path: tuple[str, ...]) -> frozenset[str]:
        if role_id in path:
            raise RoleCycleError(" -> ".join((*path, role_id)))
        cached = self._expansion.get(role_id)
        if cached is not None:
            return cached
        role = self._roles.get(role_id)
        if role is None:
            return frozenset()
        entitlements = set(role.entitlements)
        depth = 1
        for child in role.child_roles:
            if child not in self._roles:
                continue
            entitlements |= self._expand(child, (*path, role_id))
            depth = max(depth, 1 + self._depth[child])
        result = frozenset(entitlements)
        self._expansion[role_id] = result
        self._depth[role_id] = depth
        return result


class SodIndex:
    """Which SoD rule sides each entitlement sits on, for fast conflict tests."""

    def __init__(self, pack: PolicyPack) -> None:
        self.rules = tuple(rule.rule_id for rule in pack.sod)
        self.sides: dict[str, tuple[frozenset[str], frozenset[str]]] = {
            rule.rule_id: (frozenset(rule.side_a), frozenset(rule.side_b)) for rule in pack.sod
        }
        self.rules_of: dict[str, tuple[str, ...]] = {}
        for rule in pack.sod:
            for ent in (*rule.side_a, *rule.side_b):
                self.rules_of[ent] = (*self.rules_of.get(ent, ()), rule.rule_id)

    def conflicts(self, entitlements: Iterable[str]) -> tuple[str, ...]:
        """Rule IDs whose two sides are both present in ``entitlements``."""
        held = set(entitlements)
        return tuple(
            rule for rule in self.rules if held & self.sides[rule][0] and held & self.sides[rule][1]
        )

    def introduces_conflict(self, held: set[str] | frozenset[str], added: Iterable[str]) -> bool:
        """True if adding ``added`` to ``held`` completes a rule that ``added`` touches."""
        added_set = set(added)
        touched = {rule for ent in added_set for rule in self.rules_of.get(ent, ())}
        if not touched:
            return False
        combined = held | added_set
        return any(
            combined & self.sides[rule][0] and combined & self.sides[rule][1] for rule in touched
        )
