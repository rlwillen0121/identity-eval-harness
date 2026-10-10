"""World entities (SPEC §6.2).

These pydantic models are the shared contract between the generator, the oracle, the task
builders, the prompt renderer and the scorers. Field names matter twice over: they are what
the code reads, and they are what models cite in evidence claims (``{id, field, value}``).

Conventions:
- All models are frozen; list-valued fields are tuples.
- IDs are random within a world (SPEC §6.2): nothing about an ID's digits correlates with a label.
- Fields marked ``exclude=True`` exist only in memory for the generator. They never appear in a
  serialized ``CaseView``, so neither the oracle nor the model can depend on them.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------------


class EmploymentType(StrEnum):
    employee = "employee"
    contractor = "contractor"
    intern = "intern"
    vendor = "vendor"


NON_EMPLOYEES: frozenset[EmploymentType] = frozenset(
    {EmploymentType.contractor, EmploymentType.intern, EmploymentType.vendor}
)


class IdentityStatus(StrEnum):
    active = "active"
    leave_of_absence = "leave_of_absence"
    terminated = "terminated"


class TerminationType(StrEnum):
    voluntary = "voluntary"
    involuntary = "involuntary"


class RiskTier(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


RISK_ORDER: dict[RiskTier, int] = {
    RiskTier.low: 0,
    RiskTier.medium: 1,
    RiskTier.high: 2,
    RiskTier.critical: 3,
}

RISK_WEIGHT: dict[RiskTier, float] = {
    RiskTier.low: 0.5,
    RiskTier.medium: 1.0,
    RiskTier.high: 2.0,
    RiskTier.critical: 4.0,
}
"""Risk weights *w* used by the loss matrix (SPEC §12.1)."""


def max_risk(tiers: list[RiskTier] | tuple[RiskTier, ...]) -> RiskTier:
    """Highest risk tier in a non-empty collection."""
    return max(tiers, key=lambda t: RISK_ORDER[t])


class DataClassification(StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    restricted = "restricted"


class Environment(StrEnum):
    prod = "prod"
    nonprod = "nonprod"


class Privilege(StrEnum):
    read = "read"
    write = "write"
    admin = "admin"


class RoleKind(StrEnum):
    birthright = "birthright"
    business = "business"


class MembershipSource(StrEnum):
    birthright = "birthright"
    request = "request"


class AssignmentSource(StrEnum):
    request = "request"
    direct = "direct"


class AccountKind(StrEnum):
    human = "human"
    service = "service"


class AccountStatus(StrEnum):
    active = "active"
    disabled = "disabled"
    suspended = "suspended"


ACCOUNT_PERMISSIVENESS: dict[AccountStatus, int] = {
    AccountStatus.disabled: 0,
    AccountStatus.suspended: 1,
    AccountStatus.active: 2,
}
"""active > suspended > disabled (SPEC §12.4, unsafe-plan definition)."""


class SuspensionReason(StrEnum):
    leave = "leave"
    security_hold = "security_hold"


class TelemetryStatus(StrEnum):
    available = "available"
    unavailable = "unavailable"


class TicketType(StrEnum):
    incident = "incident"
    change = "change"
    project = "project"


class TicketStatus(StrEnum):
    open = "open"
    closed = "closed"


class HrEventType(StrEnum):
    joiner = "joiner"
    mover = "mover"
    leaver = "leaver"
    rehire = "rehire"
    conversion = "conversion"
    leave_start = "leave_start"
    leave_end = "leave_end"


# --------------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------------


class Location(_Frozen):
    country: str
    """ISO 3166-1 alpha-2 code, e.g. ``US``, ``DE``."""
    region: str
    """Coarse region used by region-restricted apps: ``NA``, ``EU``, ``APAC`` or ``LATAM``."""


class Identity(_Frozen):
    id: str
    display_name: str
    title: str
    """Noisy surface form shown to models (e.g. ``Sr. Acct``). No rule reads titles."""
    department: str
    cost_center: str
    location: Location
    employment_type: EmploymentType
    status: IdentityStatus
    manager_id: str | None = None
    hire_date: date
    termination_date: date | None = None
    termination_type: TerminationType | None = None
    contract_end_date: date | None = None
    move_date: date | None = None
    prior_department: str | None = None
    leave_returned_on: date | None = None
    canonical_title: str | None = Field(default=None, exclude=True)
    """Generator-only: used to compute peer stats. Never serialized."""


class Application(_Frozen):
    id: str
    name: str
    risk_tier: RiskTier
    data_classification: DataClassification
    sox_in_scope: bool
    environment: Environment
    owner_id: str
    region_restriction: str | None = None
    """If set, only identities whose ``location.region`` equals this may hold access (ELIG-003)."""


class Entitlement(_Frozen):
    id: str
    app_id: str
    name: str
    description: str
    """Free text. Untrusted: may contradict structured fields in adversarial cases."""
    privilege: Privilege
    privileged: bool
    risk_tier: RiskTier
    data_classification: DataClassification
    requestable: bool
    eligible_employment_types: tuple[EmploymentType, ...]
    max_duration_days: int | None = None
    functions: tuple[str, ...] = Field(default=(), exclude=True)
    """Generator-only function tags (e.g. ``gl.je.post``). SoD sides are resolved from these into
    explicit entitlement ID lists in the ``PolicyPack``; the oracle never reads them."""


class AttributeRule(_Frozen):
    """Birthright assignment rule. ``None`` on a dimension means "any value"."""

    departments: tuple[str, ...] | None = None
    employment_types: tuple[EmploymentType, ...] | None = None
    regions: tuple[str, ...] | None = None

    def matches_attributes(
        self, *, department: str, employment_type: EmploymentType, region: str
    ) -> bool:
        if self.departments is not None and department not in self.departments:
            return False
        if self.employment_types is not None and employment_type not in self.employment_types:
            return False
        return self.regions is None or region in self.regions

    def matches(self, identity: Identity) -> bool:
        return self.matches_attributes(
            department=identity.department,
            employment_type=identity.employment_type,
            region=identity.location.region,
        )


class Role(_Frozen):
    id: str
    name: str
    kind: RoleKind
    entitlements: tuple[str, ...] = ()
    child_roles: tuple[str, ...] = ()
    attribute_rule: AttributeRule | None = None
    """Set for birthright roles only."""


class Membership(_Frozen):
    id: str
    identity_id: str
    role_id: str
    source: MembershipSource
    granted_on: date
    expires_on: date | None = None


class Account(_Frozen):
    id: str
    app_id: str
    identity_id: str | None
    """None means the account is orphaned (no owning identity)."""
    kind: AccountKind
    owner_id: str | None = None
    """Service accounts only: the human accountable for the account."""
    status: AccountStatus
    suspension_reason: SuspensionReason | None = None


class Assignment(_Frozen):
    id: str
    account_id: str
    entitlement_id: str
    source: AssignmentSource
    request_id: str | None = None
    granted_on: date
    expires_on: date | None = None


class UsageRecord(_Frozen):
    id: str
    account_id: str
    entitlement_id: str
    last_used_on: date | None
    """None with telemetry ``available`` means never used; with ``unavailable`` means unknown."""
    telemetry: TelemetryStatus


class MitigatingControl(_Frozen):
    id: str
    rule_id: str
    identity_id: str
    description: str
    approver_id: str
    granted_on: date
    expires_on: date


class Ticket(_Frozen):
    id: str
    type: TicketType
    status: TicketStatus
    assignee_id: str | None
    related_app_ids: tuple[str, ...]
    opened_on: date
    summary: str
    """Free text. Untrusted."""


class SodRuleSides(_Frozen):
    """A separation-of-duties rule's two sides as explicit entitlement ID lists (SPEC §7.4)."""

    rule_id: str
    title: str
    side_a: tuple[str, ...]
    side_b: tuple[str, ...]


class PolicyPack(_Frozen):
    """World-specific policy parameters. Thresholds live in rule code; this holds what varies by
    world. Rendered into the policy text the model reads, and passed to the oracle in every
    ``CaseView``."""

    org_name: str
    sod: tuple[SodRuleSides, ...]


class World(BaseModel):
    """A generated synthetic organization. Treat as immutable once built."""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    profile: str
    seed: int
    org_name: str
    anchor_date: date
    """Default as-of date for cases drawn from this world."""
    identities: dict[str, Identity]
    applications: dict[str, Application]
    entitlements: dict[str, Entitlement]
    roles: dict[str, Role]
    memberships: dict[str, Membership]
    accounts: dict[str, Account]
    assignments: dict[str, Assignment]
    usage: dict[str, UsageRecord]
    mitigating_controls: dict[str, MitigatingControl]
    tickets: dict[str, Ticket]
    policy_pack: PolicyPack
