"""Deterministic family-specific fixture adjustments for IdentityBench v5.

The generator owns the cell planner and its ID/evidence codebooks. This module
only adjusts rows already allocated by that planner so each named F07--F12
coupling has a causal witness in the generated family.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from identitybench.v5 import schemas as s

if TYPE_CHECKING:
    from identitybench.v5.generator import V5Cell


def _etag(cell: V5Cell, label: str) -> str:
    """Return a stable, row-specific initial ETag without importing generator."""
    digest = hashlib.sha256(
        f"v5-etag\0{cell.seed}\0family-couplings:{label}".encode("ascii")
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 10**16:016d}"


def prepare_initial_family_world(
    cell: V5Cell, world: s.WorldStateV5, ids: Mapping[str, Any]
) -> s.WorldStateV5:
    """Seed family behaviors that belong to the Phase-1 source snapshot."""
    family = cell.family_id

    if family == "F07":
        bridge_id = ids["bridge"]["mover"]
        bridge = world.transfer_bridges[bridge_id]
        reviewer_account_id = ids["account_names"]["reviewer"]
        reviewer_person_id = ids["person_names"]["reviewer"]
        reviewer_grant_id = ids["grant_names"]["reviewer_review"]
        reviewer = world.hr_persons[reviewer_person_id]
        reviewer = reviewer.model_copy(
            update={
                "engagements": tuple(
                    engagement.model_copy(
                        update={
                            "prior_dept_path": "/d/l",
                            "dept_transfer_effective_at_s": world.clock_epoch_s - 600,
                        }
                    )
                    for engagement in reviewer.engagements
                ),
                "etag": _etag(cell, f"F07:reviewer-mover:{reviewer_person_id}"),
            }
        )
        grant = world.grants[reviewer_grant_id].model_copy(
            update={"scope_path": "/d/l"}
        )
        # The bridge belongs to a still-active reviewer who moved from /d/l to
        # /d/p, while the shocked boomerang approved it after the retro date.
        # This lets P2 taint the bridge and strip a grant that survived P1.
        bridge = bridge.model_copy(
            update={
                "account_id": reviewer_account_id,
                "grant_id": reviewer_grant_id,
                "approved_by_account_id": ids["account_names"]["mover"],
                "etag": _etag(cell, f"F07:bridge:{bridge_id}"),
            }
        )
        return world.model_copy(
            update={
                "hr_persons": {**world.hr_persons, reviewer_person_id: reviewer},
                "grants": {**world.grants, reviewer_grant_id: grant},
                "transfer_bridges": {**world.transfer_bridges, bridge_id: bridge},
            }
        )

    if family == "F09":
        grants = dict(world.grants)
        manager_review_id = ids["grant_names"]["manager_review"]
        manager_review = grants[manager_review_id].model_copy(
            update={"grant_mode": "eligible"}
        )
        reviewer_eligible_id = ids["grant_names"]["reviewer_deploy_eligible"]
        reviewer_eligible = grants[reviewer_eligible_id].model_copy(
            update={"role_id": ids["role_names"]["review"]}
        )
        manager_reserve_id = ids["grant_names"]["manager_admin"]
        manager_reserve = grants[manager_reserve_id].model_copy(
            update={
                "role_id": ids["role_names"]["review"],
                "grant_mode": "eligible",
            }
        )
        grants[manager_review_id] = manager_review
        grants[reviewer_eligible_id] = reviewer_eligible
        grants[manager_reserve_id] = manager_reserve

        reviewer_account_id = ids["account_names"]["reviewer"]
        deny_group_id = ids["group"]["deny_wire"]
        deny_group = s.GroupV5(
            group_id=deny_group_id,
            effect="DENY",
            scope_path="/d/p",
            capabilities=("review",),
            evidence_id=ids["evidence_names"]["group:deny_wire"],
        )
        reviewer_account = world.idp_accounts[reviewer_account_id].model_copy(
            update={
                "group_ids": tuple(
                    sorted((*world.idp_accounts[reviewer_account_id].group_ids, deny_group_id))
                ),
                "etag": _etag(cell, f"F09:review-deny-group:{reviewer_account_id}"),
            }
        )
        return world.model_copy(
            update={
                "grants": grants,
                "groups": {**world.groups, deny_group_id: deny_group},
                "idp_accounts": {
                    **world.idp_accounts,
                    reviewer_account_id: reviewer_account,
                },
            }
        )

    if family == "F10":
        grant_id = ids["grant_names"]["manager_review"]
        grant = world.grants[grant_id].model_copy(update={"grant_mode": "eligible"})
        return world.model_copy(
            update={"grants": {**world.grants, grant_id: grant}}
        )

    if family == "F12":
        # The candidate sponsor remains the Alice account so R1--R3 still
        # exercise sponsor SoD. Make the existing reviewer independent of both
        # sponsor and actor so R4 can exercise the required HITL approval path.
        person_id = ids["person_names"]["reviewer"]
        person = world.hr_persons[person_id]
        engagements = tuple(
            engagement.model_copy(update={"manager_person_id": None})
            for engagement in person.engagements
        )
        person = person.model_copy(
            update={
                "engagements": engagements,
                "etag": _etag(cell, f"F12:independent-reviewer:{person_id}"),
            }
        )
        return world.model_copy(
            update={"hr_persons": {**world.hr_persons, person_id: person}}
        )

    return world


def prepare_phase2_family_world(
    cell: V5Cell, phase2_seed_world: s.WorldStateV5, ids: Mapping[str, Any]
) -> s.WorldStateV5:
    """Seed family-specific Phase-2 requests, campaigns, and token timing."""
    family = cell.family_id

    if family == "F08":
        incident_id = ids["incident"]["incident"]
        incident = phase2_seed_world.cae_incidents[incident_id]
        token_ids = ids["token"]
        tokens = {token.token_id: token for token in incident.tokens}
        root = tokens[token_ids["root"]]
        access = tokens[token_ids["access"]]
        obo = tokens[token_ids["obo"]]
        # The access child is minted exactly at the inherited IdP cutoff. The
        # OBO child is minted just before it: a DPoP child inherits the root
        # push ACK, while an unbound bearer counterfactual runs to its poll.
        mint_cutoff = root.revoke_ack_at_s
        if mint_cutoff is None:
            raise ValueError("F08 requires a root refresh-token CAE acknowledgement")
        access = access.model_copy(
            update={
                "binding_kind": "BEARER_UNBOUND",
                "issued_at_s": mint_cutoff,
            }
        )
        obo = obo.model_copy(
            update={
                "binding_kind": "BOUND_DPOP",
                "issued_at_s": phase2_seed_world.clock_epoch_s - 200,
                "next_spoke_poll_sync_at_s": phase2_seed_world.clock_epoch_s + 50,
            }
        )
        updated_tokens = tuple(
            access
            if token.token_id == access.token_id
            else obo
            if token.token_id == obo.token_id
            else token
            for token in incident.tokens
        )
        incident = incident.model_copy(update={"tokens": updated_tokens})
        return phase2_seed_world.model_copy(
            update={"cae_incidents": {**phase2_seed_world.cae_incidents, incident_id: incident}}
        )

    if family == "F09":
        campaigns = dict(phase2_seed_world.uar_campaigns)
        campaign_ids = sorted(campaigns)
        if len(campaign_ids) != 2:
            raise ValueError("F09 requires exactly two ordered UAR campaigns")
        early_id, late_id = campaign_ids
        campaign_name_by_id = {
            campaign_id: name for name, campaign_id in ids["campaign"].items()
        }
        early_name = campaign_name_by_id[early_id]
        late_name = campaign_name_by_id[late_id]
        item_templates = {
            item.item_id: item
            for campaign in campaigns.values()
            for item in campaign.items
        }
        early_item_id = ids["item"][early_name]
        early_reserve_item_id = ids["item"]["third"]
        late_item_id = ids["item"][late_name]
        manager = ids["account_names"]["manager"]
        reviewer = ids["account_names"]["reviewer"]
        early_standing_item = item_templates[early_item_id].model_copy(
            update={
                "account_id": reviewer,
                "grant_id": ids["grant_names"]["reviewer_review"],
                "assigned_reviewer_account_id": reviewer,
                "used_within_lookback": True,
            }
        )
        early_eligible_item = item_templates[early_item_id].model_copy(
            update={
                "item_id": early_reserve_item_id,
                "evidence_id": ids["evidence_names"][
                    f"uar:item:third:{early_name}"
                ],
                "account_id": manager,
                "grant_id": ids["grant_names"]["manager_review"],
                "assigned_reviewer_account_id": manager,
                "used_within_lookback": False,
            }
        )
        late_eligible_item = item_templates[late_item_id].model_copy(
            update={
                "account_id": manager,
                "grant_id": ids["grant_names"]["manager_admin"],
                "assigned_reviewer_account_id": manager,
                "used_within_lookback": False,
            }
        )
        items = {
            early_id: (early_standing_item, early_eligible_item),
            late_id: (late_eligible_item,),
        }
        for campaign_id in (early_id, late_id):
            campaign = campaigns[campaign_id]
            campaign = campaign.model_copy(
                update={
                    "items": items[campaign_id],
                    "m_standing": 1,
                    "m_eligible": 1,
                    "backup_reviewer_account_ids": (manager, reviewer),
                    "candidate_custodian_account_ids": (manager,),
                    "etag": _etag(cell, f"F09:campaign:{campaign_id}"),
                }
            )
            campaigns[campaign_id] = campaign
        return phase2_seed_world.model_copy(update={"uar_campaigns": campaigns})

    if family == "F11":
        session_id = ids["session"]["prior"]
        sessions = tuple(
            session.model_copy(
                update={
                    "account_id": ids["account_names"]["manager"],
                    "role_id": ids["role_names"]["read"],
                }
            )
            if session.session_id == session_id
            else session
            for session in phase2_seed_world.historical_sessions
        )
        replay_id = ids["request"]["replay"]
        replay = phase2_seed_world.jit_requests[replay_id].model_copy(
            update={
                "requester_account_id": ids["account_names"]["manager"],
                "requested_role_id": ids["role_names"]["deploy_admin"],
                "requested_scope_path": "/d/p",
                "proposed_approver_account_id": ids["account_names"]["primary"],
                "etag": _etag(cell, f"F11:cooldown-request:{replay_id}"),
            }
        )
        return phase2_seed_world.model_copy(
            update={
                "historical_sessions": sessions,
                "jit_requests": {**phase2_seed_world.jit_requests, replay_id: replay},
            }
        )

    if family == "F12":
        request_id = ids["delegation"]["root"]
        request = phase2_seed_world.agent_delegation_requests[request_id].model_copy(
            update={
                "proposed_hitl_approver_account_id": ids["account_names"]["reviewer"],
                "etag": _etag(cell, f"F12:root-cosigner:{request_id}"),
            }
        )
        return phase2_seed_world.model_copy(
            update={
                "agent_delegation_requests": {
                    **phase2_seed_world.agent_delegation_requests,
                    request_id: request,
                }
            }
        )

    if family == "F10":
        campaign_id = next(iter(phase2_seed_world.uar_campaigns))
        campaign = phase2_seed_world.uar_campaigns[campaign_id].model_copy(
            update={
                # The reviewer item remains inherited-locked, while the only
                # other item is eligible and cannot replenish the standing
                # floor. The reviewer account is a valid, non-conflicting
                # emergency custodian and therefore requires one co-sign.
                "candidate_custodian_account_ids": (ids["account_names"]["reviewer"],),
                "preserve_standing_floor_override": "TRUE",
                "etag": _etag(cell, f"F10:campaign-custodian:{campaign_id}"),
            }
        )
        return phase2_seed_world.model_copy(
            update={"uar_campaigns": {campaign_id: campaign}}
        )

    return phase2_seed_world
