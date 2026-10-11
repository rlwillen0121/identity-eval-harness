"""Application, entitlement and role catalog for one org profile (SPEC §6.1, §6.2, §7.4).

``build_catalog`` turns the declarative tables in ``apps_data`` and ``roles_data`` into contract
entities sized for a profile:

- **Apps.** The profile's base apps, plus (enterprise only) UAT and regional instances of base
  apps until the app target is reached.
- **Entitlements.** Each app's hand-written entitlements that the profile's tier includes
  (startups get no per-app admin seats) plus generated module entitlements (view/edit, and
  approve/export in larger orgs), with the module count chosen so the total lands near the
  profile's entitlement target. Regional and UAT copies leave out the base app's SoD-function
  entitlements, so every SoD side stays one to three explicit entitlements without leaving
  lookalike copies (a SOX-scoped regional "Payroll Run Approver") off the rule's side.
- **Roles.** Hand-written roles for the profile's tier, a few sampled module view entitlements
  in department birthright roles, derived contractor/intern and vendor variants of department
  birthright roles, and (enterprise) regional birthright roles and per-app business roles.

The result is checked before it is returned: SoD sides are non-empty and small, role nesting
is acyclic and at most three deep, and no birthright combination is ineligible or SoD-conflicted.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass

from identitybench.world.apps_data import APP_SPECS
from identitybench.world.checks import (
    birthright_problems,
    entitlement_problems,
    role_problems,
    sod_problems,
)
from identitybench.world.departments import DEPARTMENTS, Department
from identitybench.world.eligibility import ineligibility
from identitybench.world.entities import (
    RISK_ORDER,
    Application,
    AttributeRule,
    DataClassification,
    EmploymentType,
    Entitlement,
    Environment,
    Privilege,
    RiskTier,
    Role,
    RoleKind,
    SodRuleSides,
    max_risk,
)
from identitybench.world.names import REGIONS
from identitybench.world.profiles import OrgProfile
from identitybench.world.rolegraph import RoleGraph
from identitybench.world.roles_data import ROLE_SPECS
from identitybench.world.specs import (
    EMP,
    EMP_CON,
    ENTERPRISE_TIER,
    EVERYONE,
    EXTENDED,
    MIDMARKET_TIER,
    SOD_DEFINITIONS,
    SOD_FUNCTIONS,
    STARTUP_TIER,
    VENDOR_ONLY,
    AppSpec,
    EntSpec,
    RoleSpec,
)

MIN_ENTITLEMENTS_PER_APP = 3
MAX_ENTITLEMENTS_PER_APP = 40
VENDOR_ROLE_AFFINITY = 2.0
"""Departments at least this vendor-heavy get a derived vendor birthright role."""

_CLASS_ORDER: dict[DataClassification, int] = {
    DataClassification.public: 0,
    DataClassification.internal: 1,
    DataClassification.confidential: 2,
    DataClassification.restricted: 3,
}
_RISKS: tuple[RiskTier, ...] = (RiskTier.low, RiskTier.medium, RiskTier.high, RiskTier.critical)

_ACTIONS: dict[str, tuple[Privilege, str, str]] = {
    "view": (Privilege.read, "Viewer", "Read-only access to {phrase} in {app}."),
    "edit": (Privilege.write, "Editor", "Create, update and delete {phrase} in {app}."),
    "approve": (Privilege.write, "Approver", "Approve pending changes to {phrase} in {app}."),
    "export": (Privilege.read, "Exporter", "Export {phrase} from {app} to CSV files."),
}
_SMALL_WORDS = frozenset({"of", "and", "to", "for", "the", "a", "in", "on"})


@dataclass(frozen=True, slots=True)
class AppDef:
    """A catalog application before an owner is assigned, plus generator-only facts."""

    id: str
    name: str
    risk_tier: RiskTier
    data_classification: DataClassification
    sox_in_scope: bool
    environment: Environment
    region_restriction: str | None
    owner_dept: str
    user_depts: tuple[str, ...]
    telemetry: bool
    """False: the app emits no usage telemetry (every usage record is ``unavailable``)."""
    service_accounts: bool

    def application(self, owner_id: str) -> Application:
        return Application(
            id=self.id,
            name=self.name,
            risk_tier=self.risk_tier,
            data_classification=self.data_classification,
            sox_in_scope=self.sox_in_scope,
            environment=self.environment,
            owner_id=owner_id,
            region_restriction=self.region_restriction,
        )

    def used_by(self, department: str) -> bool:
        return EVERYONE in self.user_depts or department in self.user_depts


@dataclass(frozen=True, slots=True)
class RoleAudience:
    """Who typically requests a business role."""

    departments: tuple[str, ...]
    leaders_only: bool

    def includes(self, department: str) -> bool:
        return EVERYONE in self.departments or department in self.departments


@dataclass(frozen=True)
class Catalog:
    departments: tuple[Department, ...]
    apps: dict[str, AppDef]
    entitlements: dict[str, Entitlement]
    roles: dict[str, Role]
    audiences: dict[str, RoleAudience]
    """Business roles only."""
    sod: tuple[SodRuleSides, ...]


class CatalogError(ValueError):
    """The catalog data violates an invariant (a bug in ``apps_data``/``roles_data``)."""


def profile_departments(profile: OrgProfile) -> tuple[Department, ...]:
    """Departments that exist in an org of this profile."""
    if profile.tier == STARTUP_TIER:
        return tuple(d for d in DEPARTMENTS if d.startup_weight > 0)
    return tuple(d for d in DEPARTMENTS if d.weight > 0)


def build_catalog(profile: OrgProfile, rng: random.Random) -> Catalog:
    """Build and validate the app, entitlement and role catalog for ``profile``."""
    departments = profile_departments(profile)
    apps, entitlements = _build_apps(profile, rng)
    sod = resolve_sod(entitlements)
    roles, audiences = _build_roles(profile, rng, departments, apps, entitlements)
    problems = [
        *_app_size_problems(apps, entitlements),
        *entitlement_problems(entitlements, apps),
        *sod_problems(sod, entitlements),
        *role_problems(roles, entitlements),
        *birthright_problems(roles, entitlements, apps, [d.name for d in departments], sod),
    ]
    if problems:
        raise CatalogError("; ".join(problems[:10]))
    return Catalog(departments, apps, entitlements, roles, audiences, sod)


def _app_size_problems(apps: dict[str, AppDef], entitlements: dict[str, Entitlement]) -> list[str]:
    counts = Counter(e.app_id for e in entitlements.values())
    return [
        f"{app_id} has {counts[app_id]} entitlements"
        for app_id in apps
        if not MIN_ENTITLEMENTS_PER_APP <= counts[app_id] <= MAX_ENTITLEMENTS_PER_APP
    ]


def resolve_sod(entitlements: dict[str, Entitlement]) -> tuple[SodRuleSides, ...]:
    """Resolve each SoD rule's function tags into sorted entitlement ID lists."""
    rules: list[SodRuleSides] = []
    for definition in SOD_DEFINITIONS:
        side_a = sorted(e.id for e in entitlements.values() if definition.function_a in e.functions)
        side_b = sorted(e.id for e in entitlements.values() if definition.function_b in e.functions)
        rules.append(
            SodRuleSides(
                rule_id=definition.rule_id,
                title=definition.title,
                side_a=tuple(side_a),
                side_b=tuple(side_b),
            )
        )
    return tuple(rules)


# --------------------------------------------------------------------------------------------
# Applications and entitlements
# --------------------------------------------------------------------------------------------


def _build_apps(
    profile: OrgProfile, rng: random.Random
) -> tuple[dict[str, AppDef], dict[str, Entitlement]]:
    base_specs = [spec for spec in APP_SPECS if spec.tier <= profile.tier]
    variants = [(spec, variant) for spec in base_specs for variant in spec.variants]
    extra = profile.apps - len(base_specs)
    if extra < 0 or (extra > 0 and profile.tier < ENTERPRISE_TIER) or extra > len(variants):
        raise CatalogError(
            f"profile {profile.name!r} wants {profile.apps} apps; catalog has "
            f"{len(base_specs)} base apps and {len(variants)} variants"
        )
    rng.shuffle(variants)
    chosen_variants = sorted(variants[:extra], key=lambda pair: (pair[0].key, pair[1]))
    copies = Counter(spec.key for spec, _ in chosen_variants)
    module_counts = _allocate_modules(base_specs, copies, profile, rng)

    apps: dict[str, AppDef] = {}
    entitlements: dict[str, Entitlement] = {}
    base_ents: dict[str, list[Entitlement]] = {}
    for spec in base_specs:
        app = _base_app(spec)
        ents = [
            _core_entitlement(e, spec, app.id)
            for e in spec.entitlements
            if e.min_tier <= profile.tier
        ]
        picked = sorted(rng.sample(range(len(spec.modules)), module_counts[spec.key]))
        ents += _module_entitlements(spec, app, picked, profile.module_actions)
        apps[app.id] = app
        base_ents[spec.key] = ents
        _add_all(entitlements, ents)
    for spec, variant in chosen_variants:
        app = _variant_app(spec, variant)
        apps[app.id] = app
        copied = [e for e in base_ents[spec.key] if not _on_sod_side(e)]
        _add_all(entitlements, [_variant_ent(e, variant, app.id) for e in copied])
    return apps, entitlements


def _on_sod_side(ent: Entitlement | EntSpec) -> bool:
    return bool(set(ent.functions) & SOD_FUNCTIONS)


def _allocate_modules(
    specs: list[AppSpec], copies: Counter[str], profile: OrgProfile, rng: random.Random
) -> dict[str, int]:
    """How many modules each base app uses so the catalog lands near the entitlement target.

    Modules are dealt round-robin (in a seeded random order) to apps with spare modules, so
    apps grow evenly rather than a few apps absorbing the whole target. A base app's modules
    are also copied into each of its regional/UAT variants, which the weights account for
    (variants copy the core entitlements minus the SoD ones).
    """
    actions = len(profile.module_actions)
    weight = {spec.key: 1 + copies[spec.key] for spec in specs}
    core = {
        spec.key: [e for e in spec.entitlements if e.min_tier <= profile.tier] for spec in specs
    }
    capacity = {
        spec.key: min(
            len(spec.modules), (MAX_ENTITLEMENTS_PER_APP - len(core[spec.key])) // actions
        )
        for spec in specs
    }
    fixed = sum(
        len(core[spec.key])
        + copies[spec.key] * sum(1 for e in core[spec.key] if not _on_sod_side(e))
        for spec in specs
    )
    need = profile.entitlements - fixed
    counts = dict.fromkeys(weight, 0)
    order = [spec.key for spec in specs]
    rng.shuffle(order)
    progressed = True
    while need > 0 and progressed:
        progressed = False
        for key in order:
            if need <= 0:
                break
            if counts[key] < capacity[key]:
                counts[key] += 1
                need -= actions * weight[key]
                progressed = True
    return counts


def _add_all(into: dict[str, Entitlement], ents: list[Entitlement]) -> None:
    for ent in ents:
        if ent.id in into:
            raise CatalogError(f"duplicate entitlement id {ent.id}")
        into[ent.id] = ent


def _base_app(spec: AppSpec) -> AppDef:
    return AppDef(
        id=f"app_{spec.key}",
        name=spec.name,
        risk_tier=spec.risk,
        data_classification=spec.classification,
        sox_in_scope=spec.sox,
        environment=spec.environment,
        region_restriction=spec.region,
        owner_dept=spec.owner_dept,
        user_depts=spec.user_depts,
        telemetry=spec.telemetry,
        service_accounts=spec.service_accounts,
    )


def _core_entitlement(spec: EntSpec, app: AppSpec, app_id: str) -> Entitlement:
    return Entitlement(
        id=spec.id,
        app_id=app_id,
        name=spec.name,
        description=spec.description,
        privilege=spec.privilege,
        privileged=spec.privileged,
        risk_tier=spec.risk or app.risk,
        data_classification=spec.classification or app.classification,
        requestable=spec.requestable,
        eligible_employment_types=spec.eligible or app.eligible,
        max_duration_days=spec.max_duration_days,
        functions=spec.functions or (_default_function(spec.id, app.prefix),),
    )


def _default_function(ent_id: str, prefix: str) -> str:
    """Internal function tag for a hand-written entitlement without an explicit one
    (``ent_gl_ledger_view`` -> ``gl.ledger_view``)."""
    return f"{prefix}.{ent_id.removeprefix('ent_').removeprefix(f'{prefix}_')}"


def _title_case(phrase: str) -> str:
    words = phrase.split(" ")
    return " ".join(
        w if (i > 0 and w in _SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words)
    )


def _module_entitlements(
    spec: AppSpec, app: AppDef, picked: list[int], actions: tuple[str, ...]
) -> list[Entitlement]:
    """Generated entitlements for the picked modules: one per (module, action)."""
    result: list[Entitlement] = []
    for index in picked:
        slug, phrase = spec.modules[index]
        for action in actions:
            privilege, label, template = _ACTIONS[action]
            risk = spec.risk
            eligible = spec.eligible
            max_days: int | None = None
            if action == "approve":
                risk = max_risk([risk, RiskTier.medium])
                eligible = EMP
            elif action == "export":
                sensitive = _CLASS_ORDER[spec.classification] >= 2
                risk = max_risk([risk, RiskTier.high if sensitive else RiskTier.medium])
                eligible = tuple(t for t in spec.eligible if t in EMP_CON)
                max_days = 30
            result.append(
                Entitlement(
                    id=f"ent_{spec.prefix}_{slug}_{action}",
                    app_id=app.id,
                    name=f"{_title_case(phrase)} {label}",
                    description=template.format(phrase=phrase, app=spec.name),
                    privilege=privilege,
                    privileged=action == "edit" and spec.privileged_writes,
                    risk_tier=risk,
                    data_classification=spec.classification,
                    requestable=True,
                    eligible_employment_types=eligible,
                    max_duration_days=max_days,
                    functions=(f"{spec.prefix}.{slug}.{action}",),
                )
            )
    return result


def _lower_risk(risk: RiskTier) -> RiskTier:
    return _RISKS[max(0, RISK_ORDER[risk] - 1)]


def _cap_classification(c: DataClassification) -> DataClassification:
    """UAT environments run on masked copies, so nothing there is above confidential."""
    if _CLASS_ORDER[c] > _CLASS_ORDER[DataClassification.confidential]:
        return DataClassification.confidential
    return c


def _variant_app(spec: AppSpec, variant: str) -> AppDef:
    base = _base_app(spec)
    if variant == "uat":
        return AppDef(
            id=f"{base.id}_uat",
            name=f"{spec.name} (UAT)",
            risk_tier=_lower_risk(spec.risk),
            data_classification=_cap_classification(spec.classification),
            sox_in_scope=False,
            environment=Environment.nonprod,
            region_restriction=None,
            owner_dept=spec.owner_dept,
            user_depts=spec.user_depts,
            telemetry=spec.telemetry,
            service_accounts=spec.service_accounts,
        )
    region = variant.upper()
    return AppDef(
        id=f"{base.id}_{variant}",
        name=f"{spec.name} ({region})",
        risk_tier=spec.risk,
        data_classification=spec.classification,
        sox_in_scope=spec.sox,
        environment=spec.environment,
        region_restriction=region,
        owner_dept=spec.owner_dept,
        user_depts=spec.user_depts,
        telemetry=spec.telemetry,
        service_accounts=spec.service_accounts,
    )


def _variant_ent(ent: Entitlement, variant: str, app_id: str) -> Entitlement:
    if variant == "uat":
        return ent.model_copy(
            update={
                "id": f"{ent.id}_uat",
                "app_id": app_id,
                "name": f"{ent.name} (UAT)",
                "description": f"{ent.description} Scope: UAT (non-production) environment.",
                "risk_tier": _lower_risk(ent.risk_tier),
                "data_classification": _cap_classification(ent.data_classification),
            }
        )
    region = variant.upper()
    return ent.model_copy(
        update={
            "id": f"{ent.id}_{variant}",
            "app_id": app_id,
            "name": f"{ent.name} ({region})",
            "description": f"{ent.description} Scope: {region} regional instance.",
        }
    )


# --------------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------------


def _build_roles(
    profile: OrgProfile,
    rng: random.Random,
    departments: tuple[Department, ...],
    apps: dict[str, AppDef],
    entitlements: dict[str, Entitlement],
) -> tuple[dict[str, Role], dict[str, RoleAudience]]:
    dept_names = {d.name for d in departments}
    specs = [
        s
        for s in ROLE_SPECS
        if s.min_tier <= profile.tier <= s.max_tier
        and (s.departments is None or any(d in dept_names for d in s.departments))
    ]
    roles = _resolve_specs(specs, entitlements)
    audiences = {
        s.id: RoleAudience(s.audience, s.leaders_only)
        for s in specs
        if s.kind is RoleKind.business and s.id in roles
    }
    if profile.tier >= MIDMARKET_TIER:
        _add_birthright_extras(roles, specs, rng, apps, entitlements)
        _derive_variants(roles, specs, departments, apps, entitlements)
    if profile.regional_roles:
        _add_regional_roles(roles, rng, departments, apps, entitlements)
    if profile.generated_roles:
        _add_app_roles(roles, audiences, profile.roles, rng, apps, entitlements)
    return roles, audiences


def _resolve_specs(specs: list[RoleSpec], entitlements: dict[str, Entitlement]) -> dict[str, Role]:
    """Materialize specs, dropping missing entitlements/children and then empty roles.

    Dropping an empty role can empty its parent, so this repeats until nothing changes.
    """
    present = {s.id for s in specs}
    while True:
        roles: dict[str, Role] = {}
        for s in specs:
            if s.id not in present:
                continue
            ents = tuple(e for e in s.entitlements if e in entitlements)
            children = tuple(c for c in s.children if c in present)
            if not ents and not children:
                continue
            rule = None
            if s.kind is RoleKind.birthright:
                rule = AttributeRule(
                    departments=s.departments,
                    employment_types=s.employment_types,
                    regions=s.regions,
                )
            roles[s.id] = Role(
                id=s.id,
                name=s.name,
                kind=s.kind,
                entitlements=ents,
                child_roles=children,
                attribute_rule=rule,
            )
        if set(roles) == present:
            return roles
        present = set(roles)


def _clean_for(
    ent: Entitlement, app: AppDef, types: tuple[EmploymentType, ...], regions: tuple[str, ...]
) -> bool:
    """True if every listed employment type, in every listed region, may hold ``ent``."""
    return all(ineligibility(ent, app, t, r) is None for t in types for r in regions)


def _bundleable(ent: Entitlement, privilege: Privilege) -> bool:
    """Plain access suitable for bundling into generated roles: standing, requestable,
    non-privileged and outside every SoD side."""
    return (
        ent.privilege is privilege
        and not ent.privileged
        and ent.requestable
        and ent.max_duration_days is None
        and not set(ent.functions) & SOD_FUNCTIONS
    )


def _add_birthright_extras(
    roles: dict[str, Role],
    specs: list[RoleSpec],
    rng: random.Random,
    apps: dict[str, AppDef],
    entitlements: dict[str, Entitlement],
) -> None:
    """Give each department birthright role a few read entitlements of its apps.

    Hand-written roles alone would make every department's birthright look identical across
    worlds; sampling adds realistic variety without touching SoD.
    """
    for s in specs:
        if not s.derive_extended or s.id not in roles or s.departments is None:
            continue
        candidates = sorted(
            e.id
            for e in entitlements.values()
            if _bundleable(e, Privilege.read)
            and apps[e.app_id].region_restriction is None
            and any(apps[e.app_id].used_by(d) for d in s.departments)
            and EVERYONE not in apps[e.app_id].user_depts
            and _clean_for(e, apps[e.app_id], EMP, REGIONS)
        )
        if not candidates:
            continue
        extra = rng.sample(candidates, min(len(candidates), rng.randint(1, 3)))
        role = roles[s.id]
        merged = (*role.entitlements, *(e for e in sorted(extra) if e not in role.entitlements))
        roles[s.id] = role.model_copy(update={"entitlements": merged})


def _derive_variants(
    roles: dict[str, Role],
    specs: list[RoleSpec],
    departments: tuple[Department, ...],
    apps: dict[str, AppDef],
    entitlements: dict[str, Entitlement],
) -> None:
    """Derive contractor/intern and vendor birthright roles from department employee roles.

    A derived role keeps only the access its employment types may hold (eligibility rules,
    no region-restricted apps) and never any SoD side, so extended-workforce birthright can't
    create conflicts or eligibility violations on its own.
    """
    by_name = {d.name: d for d in departments}
    graph = RoleGraph(roles)

    def clean(ent_id: str, types: tuple[EmploymentType, ...]) -> bool:
        ent = entitlements[ent_id]
        return not (set(ent.functions) & SOD_FUNCTIONS) and _clean_for(
            ent, apps[ent.app_id], types, REGIONS
        )

    for s in specs:
        if not s.derive_extended or s.id not in roles or s.departments is None:
            continue
        dept = by_name.get(s.departments[0])
        if dept is None:
            continue
        base = roles[s.id]
        wanted: list[tuple[str, str, tuple[EmploymentType, ...]]] = []
        if dept.contractor_affinity > 0 or dept.intern_affinity > 0:
            wanted.append((f"role_{dept.code}_ext", "Extended Workforce", EXTENDED))
        if dept.vendor_affinity >= VENDOR_ROLE_AFFINITY:
            wanted.append((f"role_{dept.code}_vendor", "Vendor Staff", VENDOR_ONLY))
        for role_id, label, types in wanted:
            ents = tuple(e for e in base.entitlements if clean(e, types))
            children = tuple(
                c for c in base.child_roles if all(clean(e, types) for e in graph.expansion(c))
            )
            if not ents and not children:
                continue
            roles[role_id] = Role(
                id=role_id,
                name=f"{base.name} - {label}",
                kind=RoleKind.birthright,
                entitlements=ents,
                child_roles=children,
                attribute_rule=AttributeRule(departments=(dept.name,), employment_types=types),
            )


def _add_regional_roles(
    roles: dict[str, Role],
    rng: random.Random,
    departments: tuple[Department, ...],
    apps: dict[str, AppDef],
    entitlements: dict[str, Entitlement],
) -> None:
    """Enterprise: employees in a region get read access to their regional app instances."""
    by_app: dict[str, list[Entitlement]] = {}
    for ent in entitlements.values():
        by_app.setdefault(ent.app_id, []).append(ent)
    for dept in departments:
        if dept.name == "Executive":
            continue
        for region in REGIONS:
            candidates = sorted(
                ent.id
                for app in apps.values()
                if app.region_restriction == region and app.used_by(dept.name)
                for ent in by_app[app.id]
                if _bundleable(ent, Privilege.read) and _clean_for(ent, app, EMP, (region,))
            )
            if not candidates:
                continue
            picked = sorted(rng.sample(candidates, min(len(candidates), rng.randint(2, 4))))
            role_id = f"role_{dept.code}_core_{region.lower()}"
            roles[role_id] = Role(
                id=role_id,
                name=f"{dept.name} ({region})",
                kind=RoleKind.birthright,
                entitlements=tuple(picked),
                attribute_rule=AttributeRule(
                    departments=(dept.name,), employment_types=EMP, regions=(region,)
                ),
            )


def _add_app_roles(
    roles: dict[str, Role],
    audiences: dict[str, RoleAudience],
    target: int,
    rng: random.Random,
    apps: dict[str, AppDef],
    entitlements: dict[str, Entitlement],
) -> None:
    """Enterprise: per-app viewer and editor business roles (editors nest viewers)."""
    by_app: dict[str, list[Entitlement]] = {}
    for ent in entitlements.values():
        by_app.setdefault(ent.app_id, []).append(ent)
    order = sorted(apps)
    rng.shuffle(order)
    for app_id in order:
        if len(roles) >= target:
            return
        app = apps[app_id]
        views = sorted(e.id for e in by_app[app_id] if _bundleable(e, Privilege.read))
        edits = sorted(e.id for e in by_app[app_id] if _bundleable(e, Privilege.write))
        if len(views) < 2:
            continue
        slug = app_id.removeprefix("app_")
        viewers = f"role_{slug}_viewers"
        roles[viewers] = Role(
            id=viewers,
            name=f"{app.name} Viewers",
            kind=RoleKind.business,
            entitlements=tuple(sorted(rng.sample(views, min(len(views), rng.randint(2, 5))))),
        )
        audiences[viewers] = RoleAudience(app.user_depts, False)
        if len(edits) >= 2 and len(roles) < target:
            editors = f"role_{slug}_editors"
            roles[editors] = Role(
                id=editors,
                name=f"{app.name} Editors",
                kind=RoleKind.business,
                entitlements=tuple(sorted(rng.sample(edits, min(len(edits), rng.randint(2, 4))))),
                child_roles=(viewers,),
            )
            audiences[editors] = RoleAudience(app.user_depts, False)
