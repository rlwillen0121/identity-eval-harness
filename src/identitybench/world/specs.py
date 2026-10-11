"""Declarative specs for the application, entitlement and role catalog.

The catalog data (``apps_data``, ``roles_data``) is written as these frozen specs; ``catalog``
turns them into contract entities for a given profile. Keeping the data declarative means the
realism lives in reviewable tables rather than in generator control flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from identitybench.world.entities import (
    DataClassification,
    EmploymentType,
    Environment,
    Privilege,
    RiskTier,
    RoleKind,
)

EMP: tuple[EmploymentType, ...] = (EmploymentType.employee,)
EMP_CON: tuple[EmploymentType, ...] = (EmploymentType.employee, EmploymentType.contractor)
STAFF: tuple[EmploymentType, ...] = (
    EmploymentType.employee,
    EmploymentType.contractor,
    EmploymentType.intern,
)
ALL_TYPES: tuple[EmploymentType, ...] = (*STAFF, EmploymentType.vendor)
VENDOR_ONLY: tuple[EmploymentType, ...] = (EmploymentType.vendor,)
EXTENDED: tuple[EmploymentType, ...] = (EmploymentType.contractor, EmploymentType.intern)
"""Contractors and interns share "extended workforce" birthright roles."""

EVERYONE = "*"
"""``AppSpec.user_depts`` / ``RoleSpec.audience`` wildcard: used across the whole company."""

STARTUP_TIER = 0
MIDMARKET_TIER = 1
ENTERPRISE_TIER = 2


@dataclass(frozen=True, slots=True)
class EntSpec:
    """A hand-written entitlement. Unset fields inherit from the application."""

    id: str
    name: str
    description: str
    privilege: Privilege
    privileged: bool = False
    risk: RiskTier | None = None
    classification: DataClassification | None = None
    requestable: bool = True
    eligible: tuple[EmploymentType, ...] | None = None
    max_duration_days: int | None = None
    functions: tuple[str, ...] = ()
    min_tier: int = STARTUP_TIER
    """Smallest profile tier that includes this entitlement. Raising it on a startup app's
    privileged entitlements keeps the startup catalog's privileged share low (SPEC §6.1)."""


@dataclass(frozen=True, slots=True)
class AppSpec:
    """A base application. ``modules`` are object types that get generated view/edit (and, in
    larger profiles, approve/export) entitlements; the profile decides how many are used."""

    key: str
    name: str
    prefix: str
    """Prefix of generated module entitlement IDs (``ent_<prefix>_<module>_<action>``)."""
    risk: RiskTier
    classification: DataClassification
    sox: bool
    environment: Environment
    owner_dept: str
    user_depts: tuple[str, ...]
    entitlements: tuple[EntSpec, ...]
    modules: tuple[tuple[str, str], ...] = ()
    """(slug, plural phrase) pairs, e.g. ``("reconciliations", "account reconciliations")``."""
    tier: int = MIDMARKET_TIER
    """Smallest profile that includes the app: 0 startup, 1 midmarket, 2 enterprise only."""
    region: str | None = None
    telemetry: bool = True
    """False for apps that emit no usage telemetry at all (legacy and on-prem systems)."""
    eligible: tuple[EmploymentType, ...] = STAFF
    privileged_writes: bool = False
    """Generated edit entitlements are privileged (production infrastructure)."""
    service_accounts: bool = False
    """Integrations and automation commonly run service accounts here."""
    variants: tuple[str, ...] = ()
    """Enterprise-only copies: ``uat`` (non-production) or a region code (``eu``, ``apac``,
    ``latam``) for a region-restricted regional instance."""


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """A hand-written role. Entitlements and children missing from a profile are dropped."""

    id: str
    name: str
    kind: RoleKind
    entitlements: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    departments: tuple[str, ...] | None = None
    employment_types: tuple[EmploymentType, ...] | None = None
    regions: tuple[str, ...] | None = None
    audience: tuple[str, ...] = ()
    """Business roles: departments whose members typically request the role."""
    leaders_only: bool = False
    """Business roles: requested mostly by managers and above (approver roles)."""
    min_tier: int = MIDMARKET_TIER
    max_tier: int = ENTERPRISE_TIER
    derive_extended: bool = False
    """Department birthright roles: also derive contractor/intern and vendor variants holding
    the subset of access those employment types may have."""


@dataclass(frozen=True, slots=True)
class SodDefinition:
    """A separation-of-duties rule as function tags; resolved to entitlement IDs per world."""

    rule_id: str
    title: str
    function_a: str
    function_b: str
    departments: tuple[str, ...]
    """Departments where people plausibly end up holding both sides."""
    fallback_departments: tuple[str, ...] = ()
    """Tried in order when none of ``departments`` exists in the org (a startup has no
    Procurement or Operations team, so its finance staff raise and receive purchase orders)."""


SOD_DEFINITIONS: tuple[SodDefinition, ...] = (
    SodDefinition(
        "SOD-001",
        "Create or modify vendors vs. approve vendor payments",
        "ap.vendor.write",
        "ap.payment.approve",
        ("Accounting", "Finance", "Procurement"),
    ),
    SodDefinition(
        "SOD-002",
        "Post journal entries vs. approve journal entries",
        "gl.je.post",
        "gl.je.approve",
        ("Finance", "Accounting"),
    ),
    SodDefinition(
        "SOD-003",
        "Approve change requests vs. deploy to production (SOX-scoped apps)",
        "chg.approve",
        "deploy.prod.sox",
        ("Engineering", "IT", "Data"),
    ),
    SodDefinition(
        "SOD-004",
        "Administer identity-provider accounts vs. approve access requests",
        "idp.admin",
        "iga.approve",
        ("IT", "Security"),
    ),
    SodDefinition(
        "SOD-005",
        "Edit payroll vs. approve payroll runs",
        "payroll.edit",
        "payroll.approve",
        ("HR", "Accounting", "Finance"),
    ),
    SodDefinition(
        "SOD-006",
        "Create purchase orders vs. record goods receipt",
        "po.create",
        "po.receive",
        ("Procurement", "Operations"),
        fallback_departments=("Finance", "IT"),
    ),
)

SOD_FUNCTIONS: frozenset[str] = frozenset(
    f for d in SOD_DEFINITIONS for f in (d.function_a, d.function_b)
)
