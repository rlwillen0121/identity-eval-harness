"""Org profiles (SPEC §6.1): the size and shape knobs for each synthetic organization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from identitybench.world.ids import ACCOUNT, ASSIGNMENT, MEMBERSHIP, REQUEST, USAGE
from identitybench.world.specs import ENTERPRISE_TIER, MIDMARKET_TIER, STARTUP_TIER


@dataclass(frozen=True, slots=True)
class OrgProfile:
    name: str
    org_names: tuple[str, ...]
    """Fictional organization names; one is picked per seed so worlds don't all share a name."""
    identities: int
    apps: int
    """Application count. A startup or midmarket org has exactly its tiers' base apps (13 and
    47: the identity provider and its admin console are two applications, as are the production
    and non-production secrets vaults); an enterprise adds regional and UAT copies up to this."""
    entitlements: int
    """Target entitlement count; the catalog lands within a few percent of it."""
    roles: int
    """Target role count (approximate: hand-written roles are never dropped to hit it)."""
    tier: int
    """Include base apps and hand-written roles up to this catalog tier."""
    flat: bool
    """Flat hierarchy: no directors, few managers, department leads titled "Head of"."""
    countries: tuple[tuple[str, float], ...]
    """Office locations and headcount shares."""
    hq_country: str
    history_years: int
    executive_headcount: int
    module_actions: tuple[str, ...]
    """Actions generated per app module: ``view``, ``edit``, then ``approve`` and ``export``."""
    regional_roles: bool
    """Add region-specific department birthright roles for regional app instances."""
    generated_roles: bool
    """Add per-application viewer/editor business roles up to the role target."""
    service_account_rate: float
    """Service accounts per identity."""
    ticket_rate: float
    """Tickets per identity."""
    sod_conflict_rate: float
    """Share of identities given a deliberate, real SoD conflict."""
    telemetry_gap_share: float
    """Share of usage records without telemetry in apps that otherwise report it. Larger orgs
    also run legacy apps with no telemetry at all; a startup has none of those, so its whole
    ~5% gap (missing telemetry is not non-use, SPEC §6.4) comes from patchy connectors."""
    id_widths: Mapping[str, int] = field(default_factory=dict[str, int])
    """Wider ID digit counts for prefixes whose volume would crowd 5 digits."""


_MIDMARKET_COUNTRIES = (
    ("US", 0.52), ("CA", 0.04), ("GB", 0.10), ("DE", 0.06), ("FR", 0.03), ("ES", 0.03),
    ("IN", 0.10), ("JP", 0.02), ("SG", 0.02), ("AU", 0.02), ("BR", 0.03), ("MX", 0.03),
)  # fmt: skip
_ENTERPRISE_COUNTRIES = (
    ("US", 0.34), ("CA", 0.04), ("GB", 0.10), ("DE", 0.08), ("FR", 0.05), ("ES", 0.04),
    ("IN", 0.12), ("JP", 0.04), ("KR", 0.02), ("CN", 0.03), ("SG", 0.03), ("AU", 0.03),
    ("BR", 0.04), ("MX", 0.04),
)  # fmt: skip

PROFILES: dict[str, OrgProfile] = {
    "startup": OrgProfile(
        name="startup",
        org_names=("Quillfeather Labs", "Tessaline Software", "Orbweave Labs"),
        identities=150,
        apps=13,
        entitlements=120,
        roles=10,
        tier=STARTUP_TIER,
        flat=True,
        countries=(("US", 0.80), ("CA", 0.06), ("GB", 0.08), ("DE", 0.03), ("IN", 0.03)),
        hq_country="US",
        history_years=4,
        executive_headcount=3,
        module_actions=("view", "edit"),
        regional_roles=False,
        generated_roles=False,
        service_account_rate=0.04,
        ticket_rate=0.3,
        sod_conflict_rate=0.03,
        telemetry_gap_share=0.05,
    ),
    "midmarket": OrgProfile(
        name="midmarket",
        org_names=(
            "Corvane Logistics",
            "Pellridge Supply Co.",
            "Quenby Distribution Group",
            "Vantorra Freight Systems",
        ),
        identities=2000,
        apps=47,
        entitlements=900,
        roles=60,
        tier=MIDMARKET_TIER,
        flat=False,
        countries=_MIDMARKET_COUNTRIES,
        hq_country="US",
        history_years=10,
        executive_headcount=6,
        module_actions=("view", "edit"),
        regional_roles=False,
        generated_roles=False,
        service_account_rate=0.03,
        ticket_rate=0.2,
        sod_conflict_rate=0.02,
        telemetry_gap_share=0.02,
    ),
    "enterprise": OrgProfile(
        name="enterprise",
        org_names=(
            "Halvorsen-Achterberg Group",
            "Meridane Holdings",
            "Brannoch Industrial Partners",
        ),
        identities=10000,
        apps=120,
        entitlements=4000,
        roles=250,
        tier=ENTERPRISE_TIER,
        flat=False,
        countries=_ENTERPRISE_COUNTRIES,
        hq_country="US",
        history_years=12,
        executive_headcount=14,
        module_actions=("view", "edit", "approve", "export"),
        regional_roles=True,
        generated_roles=True,
        service_account_rate=0.06,
        ticket_rate=0.12,
        sod_conflict_rate=0.015,
        telemetry_gap_share=0.02,
        id_widths={MEMBERSHIP: 6, ACCOUNT: 6, ASSIGNMENT: 6, USAGE: 7, REQUEST: 6},
    ),
}
