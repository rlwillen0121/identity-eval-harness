"""Oracle regression: the three-model-agreed probe world and hand-checked UAR / CAE / connector
scenarios (SPEC-v4.1 §3)."""

from __future__ import annotations

from identitybench.v41.ledger import apply_wave1, apply_wave2
from identitybench.v41.mechanics import req_star, with_ledger
from identitybench.v41.oracle import (
    cae_outcome,
    connector_outcome,
    derive_wave_gold,
    gold_chain,
    session_end,
)
from identitybench.v41.schemas import (
    CAEIncidentRecordV4_1,
    ConnectorPageRecordV4_1,
    ConnectorSchemaV4_1,
    HistoricalSessionRecordV4_1,
)

from .probe_fixture import (
    GOLD_CITATIONS,
    GOLD_MUTATIONS,
    GOLD_PRECONDITION_ENTITIES,
    probe_episode,
)


def test_probe_gold_matches_hand_checked_reference() -> None:
    ep = probe_episode()
    golds, _ledgers = gold_chain(ep)
    for k, gold in enumerate(golds, start=1):
        assert {tid for _, tid in gold.required} == set(GOLD_MUTATIONS[k])
        for (_kind, tid), m in gold.mutations.items():
            dumped = m.model_dump()
            for field, expected in GOLD_MUTATIONS[k][tid].items():
                assert dumped[field] == expected, (k, tid, field)
        assert gold.cite_star() == GOLD_CITATIONS[k], k
        assert {p.entity_id for p in gold.precond_set()} == GOLD_PRECONDITION_ENTITIES[k], k


def test_probe_ledger_etag_rules() -> None:
    ep = probe_episode()
    s1 = ep.waves[0]
    g1 = derive_wave_gold(1, s1, req_star(1, s1, ()))
    l1 = apply_wave1(s1, g1.required, g1.mutation_list())
    by_id = {o.account_id: o for o in l1.account_outcomes}
    # KEEP_ACTIVE retaining exactly all direct grants keeps etag/gen.
    assert by_id["acct_alee"].current_etag == "etag_acct_alee"
    assert by_id["acct_alee"].current_account_gen == 1
    # Pruned / disabled / escalated accounts get a new gen and etag.
    for aid in ("acct_rkim", "acct_bwu", "acct_aws_achen"):
        assert by_id[aid].current_account_gen == 2
        assert by_id[aid].current_etag != f"etag_{aid}"
    # Disable/escalate quarantine effective grants.
    assert by_id["acct_aws_achen"].effective_standing_grant_ids == ()
    assert by_id["acct_ctr_mjones"].effective_eligible_grant_ids == ("grant_mj_elig",)
    s2 = with_ledger(ep.waves[1], l1)
    g2 = derive_wave_gold(2, s2, req_star(2, s2, ()))
    l2 = apply_wave2(s2, g2.mutation_list())
    # No UAR in the probe -> wave-2 reducer leaves every outcome unchanged.
    assert [o.current_etag for o in l2.account_outcomes] == [
        o.current_etag for o in l1.account_outcomes
    ]
    assert l2.through_wave_index == 2


def test_session_end_post_revocation_leak_and_clean_revocation() -> None:
    leak = HistoricalSessionRecordV4_1(
        session_id="sess_1",
        account_id="acct_a",
        entitlement_id="role_x",
        start_s=0,
        scheduled_end_s=100,
        revoked_at_s=50,
        audit_events_count=2,
        last_audit_event_s=70,
        evidence_id="ev_sess_1",
    )
    assert session_end(leak) == 100  # last_audit > revoked -> max(scheduled, last)
    clean = leak.model_copy(update={"last_audit_event_s": 40})
    assert session_end(clean) == 50  # max(last, min(scheduled, revoked))


def test_cae_union_and_flags() -> None:
    inc = CAEIncidentRecordV4_1.model_validate(
        {
            "incident_id": "inc_01",
            "account_id": "acct_a",
            "hr_effective_at_s": 1000,
            "event_received_at_s": 1010,
            "sla_max_exposure_s": 100,
            "etag": "e",
            "evidence_id": "ev",
            "tokens": [
                {
                    "token_id": "tok_1",
                    "app_id": "a",
                    "issued_at_s": 900,
                    "token_exp_s": 2000,
                    "connector_supports_push_revoke": True,
                    "revoke_ack_at_s": 1050,
                    "idp_global_block_at_s": 1011,
                    "next_spoke_poll_sync_at_s": 1500,
                    "edge_cache_ttl_s": 10,
                    "clock_skew_allowance_s": 5,
                    "evidence_id": "ev1",
                },
                {
                    "token_id": "tok_2",
                    "app_id": "a",
                    "issued_at_s": 1060,
                    "token_exp_s": 1100,
                    "connector_supports_push_revoke": False,
                    "next_spoke_poll_sync_at_s": 1200,
                    "edge_cache_ttl_s": 0,
                    "clock_skew_allowance_s": 0,
                    "evidence_id": "ev2",
                },
            ],
        }
    )
    # [1010, 1065) U [1060, 1100) = [1010, 1100) -> 90 s.
    assert cae_outcome(inc) == (True, 90, False)


def test_connector_429_tombstone_and_version_rollback() -> None:
    conn = ConnectorSchemaV4_1(connector_id="conn_01", etag="e", evidence_id="ev_c")

    def page(
        idx: int, cin: str, status: int, cout: str, recs: list[dict[str, object]]
    ) -> ConnectorPageRecordV4_1:
        rows = []
        for r in recs:
            full = {"login_email": "x@y.z", "etag": "w", **r}
            rows.append(tuple(full.items()))
        return ConnectorPageRecordV4_1.model_validate(
            {
                "connector_id": "conn_01",
                "page_index": idx,
                "cursor_in": cin,
                "cursor_out": cout,
                "http_status": status,
                "records": rows,
                "evidence_id": f"ev_p{idx}",
            }
        )

    pages = [
        page(0, "cursor_0", 429, "cursor_0", []),
        page(
            1,
            "cursor_0",
            200,
            "c1",
            [
                {"account_id": "acct_x1", "status": "ACTIVE", "version": 2},
                {"account_id": "acct_x2", "status": "ACTIVE"},
            ],
        ),
        page(
            2,
            "c1",
            200,
            "cursor_end",
            [
                {"account_id": "acct_x2", "status": "TOMBSTONE"},
                {"account_id": "acct_x1", "status": "ACTIVE", "version": 1},
            ],
        ),
    ]
    halted, recon, inspected = connector_outcome(conn, pages)
    assert halted is True
    assert recon == ("acct_x1", "acct_x2")  # whole second page rolled back
    assert [p.page_index for p in inspected] == [0, 1, 2]
