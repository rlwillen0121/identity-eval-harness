"""Total cross-wave state reducer Apply(W_k, M_k) and `PriorWaveLedgerV4_1` (SPEC-v4.1 §1.3).

Pure and oracle-free: it only replays *committed* mutations onto the Wave-1 directory tables.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from identitybench.v4.jcs import jcs_sha256
from identitybench.v41.mechanics import Target
from identitybench.v41.schemas import (
    AccountReconMutationV4_1,
    AnyMutation,
    PriorAccountOutcomeV4_1,
    PriorWaveLedgerV4_1,
    UARCampaignMutationV4_1,
    WaveTelemetrySnapshotV4_1,
)


def custodian_grant_id(campaign_id: str, account_id: str) -> str:
    return f"cust__{campaign_id}__{account_id}"


def custodian_holder(gid: str) -> str | None:
    if not gid.startswith("cust__"):
        return None
    parts = gid.split("__")
    return parts[2] if len(parts) >= 3 else None


def role_of(gid: str, snap: WaveTelemetrySnapshotV4_1) -> str | None:
    """Role resolution helper across waves (synthetic `cust__*` grants resolve via the ledger)."""
    if snap.wave_index >= 2 and gid.startswith("cust__"):
        holder = custodian_holder(gid)
        for outcome in snap.prior_wave_ledger.account_outcomes:
            if outcome.account_id == holder:
                return dict(outcome.synthetic_grant_roles).get(gid)
        return None
    for g in snap.grant_assignments:
        if g.grant_id == gid:
            return g.role_id
    return None


def apply_wave1(
    s1: WaveTelemetrySnapshotV4_1,
    required: Sequence[Target],
    committed: Sequence[AnyMutation] | None,
) -> PriorWaveLedgerV4_1:
    """Ledger entering Wave 2 (`through_wave_index = 1`). `committed=None` = Wave 1 expired."""
    req = set(required)
    by_target: dict[str, AccountReconMutationV4_1] = {}
    for cm in committed or ():
        if isinstance(cm, AccountReconMutationV4_1):
            by_target[cm.target_entity_id] = cm
    modes = {g.grant_id: g.grant_mode for g in s1.grant_assignments}
    outcomes: list[PriorAccountOutcomeV4_1] = []
    for a in sorted(s1.idp_accounts, key=lambda x: x.account_id):
        m = by_target.get(a.account_id)
        if committed is not None and ("ACCOUNT_RECON_MUTATION", a.account_id) in req and m:
            if m.account_action == "KEEP_ACTIVE":
                standing = tuple(
                    sorted(g for g in m.retained_grant_ids if modes.get(g) == "standing")
                )
                eligible = tuple(
                    sorted(g for g in m.retained_grant_ids if modes.get(g) == "eligible")
                )
            else:
                standing, eligible = (), ()
            if m.account_action == "KEEP_ACTIVE" and m.retained_grant_ids == tuple(
                sorted(a.direct_grant_ids)
            ):
                gen, etag = a.account_gen, a.etag
            else:
                gen = a.account_gen + 1
                etag = jcs_sha256(
                    {
                        "account_id": a.account_id,
                        "account_gen": gen,
                        "wave": 1,
                        "w1_action": m.account_action,
                        "standing": list(standing),
                        "eligible": list(eligible),
                    }
                )[:16]
            outcomes.append(
                PriorAccountOutcomeV4_1(
                    account_id=a.account_id,
                    w1_action=m.account_action,
                    bound_person_id=m.bound_person_id,
                    w1_retained_grant_ids=m.retained_grant_ids,
                    effective_standing_grant_ids=standing,
                    effective_eligible_grant_ids=eligible,
                    synthetic_grant_roles=(),
                    current_account_gen=gen,
                    current_etag=etag,
                )
            )
        else:
            outcomes.append(
                PriorAccountOutcomeV4_1(
                    account_id=a.account_id,
                    w1_action="NOT_GOVERNED",
                    bound_person_id=None,
                    w1_retained_grant_ids=(),
                    effective_standing_grant_ids=(),
                    effective_eligible_grant_ids=(),
                    synthetic_grant_roles=(),
                    current_account_gen=a.account_gen,
                    current_etag=a.etag,
                )
            )
    return PriorWaveLedgerV4_1(through_wave_index=1, account_outcomes=tuple(outcomes))


def apply_wave2(
    s2: WaveTelemetrySnapshotV4_1,
    committed: Sequence[AnyMutation] | None,
) -> PriorWaveLedgerV4_1:
    """Ledger entering Wave 3 (`through_wave_index = 2`) from `s2.prior_wave_ledger`."""
    prev_map: Mapping[str, PriorAccountOutcomeV4_1] = {
        o.account_id: o for o in s2.prior_wave_ledger.account_outcomes
    }
    standing: dict[str, set[str]] = {
        aid: set(o.effective_standing_grant_ids) for aid, o in prev_map.items()
    }
    eligible: dict[str, set[str]] = {
        aid: set(o.effective_eligible_grant_ids) for aid, o in prev_map.items()
    }
    synthetic: dict[str, set[tuple[str, str]]] = {
        aid: set(o.synthetic_grant_roles) for aid, o in prev_map.items()
    }
    campaigns = {c.campaign_id: c for c in s2.uar_campaigns}
    uar = sorted(
        (m for m in committed or () if isinstance(m, UARCampaignMutationV4_1)),
        key=lambda m: m.target_entity_id,
    )
    for m in uar:
        camp = campaigns.get(m.target_entity_id)
        if camp is None:
            continue
        items = {i.item_id: i for i in camp.items}
        for item_id, dec in m.uar_decisions:
            item = items.get(item_id)
            if item is None or item.account_id not in prev_map:
                continue
            aid, gid = item.account_id, item.grant_id
            prev = prev_map[aid]
            if dec in ("REVOKE", "ESCALATE"):
                standing[aid].discard(gid)
                eligible[aid].discard(gid)
            elif dec == "CONVERT_TO_JIT":
                if gid in set(prev.effective_standing_grant_ids) | set(
                    prev.effective_eligible_grant_ids
                ):
                    standing[aid].discard(gid)
                    eligible[aid].add(gid)
            # CERTIFY: no-op
        cust = m.emergency_custodian_account_id
        if cust is not None and cust in prev_map:
            gid = custodian_grant_id(camp.campaign_id, cust)
            standing[cust].add(gid)
            synthetic[cust].add((gid, camp.admin_role_id))
    outcomes: list[PriorAccountOutcomeV4_1] = []
    for aid in sorted(prev_map):
        prev = prev_map[aid]
        st = tuple(sorted(standing[aid]))
        el = tuple(sorted(eligible[aid]))
        sy = tuple(sorted(synthetic[aid]))
        if (st, el, sy) != (
            prev.effective_standing_grant_ids,
            prev.effective_eligible_grant_ids,
            prev.synthetic_grant_roles,
        ):
            gen = prev.current_account_gen + 1
            etag = jcs_sha256(
                {
                    "account_id": aid,
                    "account_gen": gen,
                    "wave": 2,
                    "standing": list(st),
                    "eligible": list(el),
                    "synthetic": [list(x) for x in sy],
                }
            )[:16]
        else:
            gen, etag = prev.current_account_gen, prev.current_etag
        outcomes.append(
            prev.model_copy(
                update={
                    "effective_standing_grant_ids": st,
                    "effective_eligible_grant_ids": el,
                    "synthetic_grant_roles": sy,
                    "current_account_gen": gen,
                    "current_etag": etag,
                }
            )
        )
    return PriorWaveLedgerV4_1(through_wave_index=2, account_outcomes=tuple(outcomes))
