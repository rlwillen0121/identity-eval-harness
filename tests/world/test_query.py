"""WorldIndex agrees with brute-force scans of the world."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from identitybench.world.entities import AccountKind, IdentityStatus, Role, World
from identitybench.world.generator import build_world
from identitybench.world.query import PeerStatsUnavailable, WorldIndex, index_for, peer_share


def _expand(roles: dict[str, Role], role_id: str) -> set[str]:
    result = set(roles[role_id].entitlements)
    for child in roles[role_id].child_roles:
        result |= _expand(roles, child)
    return result


def _sample(world: World, k: int = 60) -> list[str]:
    return random.Random(0).sample(sorted(world.identities), k)


def test_reverse_indexes(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    world, index = midmarket_world, midmarket_index
    for identity_id in _sample(world):
        accounts = {
            a.id
            for a in world.accounts.values()
            if a.identity_id == identity_id and a.kind is AccountKind.human
        }
        assert {a.id for a in index.accounts_by_identity.get(identity_id, ())} == accounts
        memberships = {m.id for m in world.memberships.values() if m.identity_id == identity_id}
        assert {m.id for m in index.memberships_by_identity.get(identity_id, ())} == memberships
        reports = {i.id for i in world.identities.values() if i.manager_id == identity_id}
        assert set(index.direct_reports.get(identity_id, ())) == reports
        owned = {a.id for a in world.accounts.values() if a.owner_id == identity_id}
        assert {a.id for a in index.service_accounts_owned_by.get(identity_id, ())} == owned
        controls = {
            c.id for c in world.mitigating_controls.values() if c.identity_id == identity_id
        }
        assert {c.id for c in index.controls_by_identity.get(identity_id, ())} == controls
        for account_id in accounts:
            expected = {s.id for s in world.assignments.values() if s.account_id == account_id}
            assert {s.id for s in index.assignments_by_account.get(account_id, ())} == expected
    for usage in list(world.usage.values())[:500]:
        key = (usage.account_id, usage.entitlement_id)
        assert index.usage_by_account_entitlement[key] == usage
    for (identity_id, app_id), account in list(index.account_by_identity_app.items())[:500]:
        assert account.identity_id == identity_id and account.app_id == app_id


def test_manager_chain(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    ceo = next(i.id for i in midmarket_world.identities.values() if i.manager_id is None)
    assert midmarket_index.manager_chain(ceo) == ()
    for identity_id in _sample(midmarket_world):
        chain = midmarket_index.manager_chain(identity_id)
        if identity_id != ceo:
            assert chain[0] == midmarket_world.identities[identity_id].manager_id
            assert chain[-1] == ceo
        assert len(set(chain)) == len(chain)


def test_held_entitlements(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    world = midmarket_world
    for on in (world.anchor_date, world.anchor_date - timedelta(days=200)):
        for identity_id in _sample(world, 40):
            expected: set[str] = set()
            for m in world.memberships.values():
                valid = m.granted_on <= on and (m.expires_on is None or m.expires_on >= on)
                if m.identity_id == identity_id and valid:
                    expected |= _expand(world.roles, m.role_id)
            for s in world.assignments.values():
                account = world.accounts[s.account_id]
                valid = s.granted_on <= on and (s.expires_on is None or s.expires_on >= on)
                if account.identity_id == identity_id and valid:
                    expected.add(s.entitlement_id)
            assert midmarket_index.held_entitlements(identity_id, on) == expected


def test_peers(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    world = midmarket_world
    for identity_id in _sample(world):
        me = world.identities[identity_id]
        expected = {
            i.id
            for i in world.identities.values()
            if i.id != identity_id
            and i.status is not IdentityStatus.terminated
            and i.department == me.department
            and i.canonical_title == me.canonical_title
        }
        assert set(midmarket_index.peers(identity_id)) == expected


def test_peer_share_matches_brute_force(
    midmarket_world: World, midmarket_index: WorldIndex
) -> None:
    world, index = midmarket_world, midmarket_index
    on = world.anchor_date
    rng = random.Random(1)
    checked = 0
    for identity_id in _sample(world, 40):
        peers = index.peers(identity_id)
        held = index.held_entitlements(identity_id, on)
        candidates = sorted(held) + rng.sample(sorted(world.entitlements), 5)
        for ent_id in candidates:
            share = peer_share(world, identity_id, ent_id, on)
            if not peers:
                assert share == 0.0
                continue
            holders = sum(1 for p in peers if ent_id in index.held_entitlements(p, on))
            assert share == holders / len(peers)
            assert 0.0 <= share <= 1.0
            checked += 1
    assert checked > 100


def test_peer_share_for_terminated_identity(midmarket_world: World) -> None:
    index = index_for(midmarket_world)
    leaver = next(
        i for i in midmarket_world.identities.values() if i.status is IdentityStatus.terminated
    )
    peers = index.peers(leaver.id)
    assert leaver.id not in peers
    ent = next(iter(index.held_entitlements(leaver.id, midmarket_world.anchor_date)))
    share = index.peer_share(leaver.id, ent, midmarket_world.anchor_date)
    holders = sum(
        1 for p in peers if ent in index.held_entitlements(p, midmarket_world.anchor_date)
    )
    assert share == (holders / len(peers) if peers else 0.0)


def test_index_for_caches(startup_world: World, midmarket_world: World) -> None:
    first = index_for(startup_world)
    assert index_for(startup_world) is first
    assert index_for(midmarket_world) is not first
    assert index_for(startup_world) is first


def test_peer_stats_need_canonical_titles(startup_world: World) -> None:
    """Canonical titles aren't serialized. A reloaded world must refuse peer stats rather
    than silently regroup peers by noisy titles; every other query still works."""
    reloaded = World.model_validate_json(startup_world.model_dump_json())
    index = WorldIndex(reloaded)
    identity_id = next(iter(reloaded.identities))
    ent_id = next(iter(reloaded.entitlements))
    on = reloaded.anchor_date
    fresh = WorldIndex(startup_world)
    assert index.held_entitlements(identity_id, on) == fresh.held_entitlements(identity_id, on)
    assert index.manager_chain(identity_id) == fresh.manager_chain(identity_id)
    with pytest.raises(PeerStatsUnavailable, match="canonical title"):
        index.peers(identity_id)
    with pytest.raises(PeerStatsUnavailable, match="canonical title"):
        index.peer_share(identity_id, ent_id, on)
    with pytest.raises(PeerStatsUnavailable, match="reloaded from JSON"):
        peer_share(reloaded, identity_id, ent_id, on)
    assert issubclass(PeerStatsUnavailable, ValueError)


def test_peer_stats_error_names_the_rebuild(startup_world: World) -> None:
    """The error tells the caller exactly how to get peer stats back: the ``build_world`` call
    for the world's profile, seed and anchor, which reproduces the same world."""
    reloaded = World.model_validate_json(startup_world.model_dump_json())
    identity_id = next(iter(reloaded.identities))
    with pytest.raises(PeerStatsUnavailable) as caught:
        peer_share(reloaded, identity_id, next(iter(reloaded.entitlements)), reloaded.anchor_date)
    call = f"build_world('startup', {reloaded.seed}, date.fromisoformat('2026-10-10'))"
    assert call in str(caught.value)
    rebuilt = build_world(reloaded.profile, reloaded.seed, reloaded.anchor_date)
    assert rebuilt.model_dump_json() == reloaded.model_dump_json()
    assert index_for(rebuilt).peers(identity_id) == index_for(startup_world).peers(identity_id)
