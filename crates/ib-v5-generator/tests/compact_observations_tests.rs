use std::collections::{BTreeMap, BTreeSet};

use ib_v5_generator::{
    COMPACT_OBSERVATION_PROFILE_V1, DEFAULT_V5_SEED, V5_FAMILIES, V5_REGIMES, V5Suite,
    build_v5_compact_candidate, build_v5_episode, plan_v5_cells,
};
use ib_v5_runtime::{obs_strategy_q1, obs_strategy_q2};
use ib_v5_schemas::{AuthorityTierV5, EpisodeV5, StrictV5Model, TableRowV5, decode_compact_table_v5};
use serde_json::Value;

fn collect_ev(val: &Value, out: &mut BTreeSet<String>) {
    match val {
        Value::Object(map) => {
            for (k, v) in map {
                if k == "evidence_id"
                    && let Some(s) = v.as_str()
                {
                    out.insert(s.to_owned());
                } else {
                    collect_ev(v, out);
                }
            }
        }
        Value::Array(arr) => {
            for v in arr {
                collect_ev(v, out);
            }
        }
        _ => {}
    }
}

fn source_evidence_ids(ep: &EpisodeV5) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for slice in ep.phase1_slices.values().chain(ep.phase2_slices.values()) {
        for table in &slice.tables {
            for row in decode_compact_table_v5(table).expect("decode") {
                collect_ev(&row.to_strict_json_value().expect("json"), &mut out);
            }
        }
    }
    out
}

fn q2_cited_evidence_by_slice(
    ep: &EpisodeV5,
    oracle: &ib_v5_schemas::SealedEpisodeOracleV5,
    phase_index: i64,
) -> BTreeMap<String, BTreeSet<String>> {
    let (manifest, payloads, phase_mutations) = if phase_index == 1 {
        (&ep.phase1_manifest, &ep.phase1_slices, &oracle.gold_p1_mutations)
    } else {
        (
            &ep.phase2_base_manifest,
            &ep.phase2_slices,
            &oracle.gold_p2_mutations,
        )
    };
    let q1 = obs_strategy_q1(manifest, &ep.policy_card, phase_index).expect("q1");
    let q1_slices: Vec<_> = q1.iter().map(|sid| payloads[sid].clone()).collect();
    let q2 = obs_strategy_q2(
        manifest,
        &q1_slices,
        &manifest.work_queue,
        &ep.policy_card,
        phase_index,
    )
    .expect("q2");

    let mut cited = BTreeSet::new();
    for m in phase_mutations {
        let v = m.to_strict_json_value().expect("m json");
        if let Some(arr) = v.get("cite").and_then(|c| c.as_array()) {
            for item in arr {
                if let Some(s) = item.as_str()
                    && let Some((_, ev)) = s.rsplit_once(':')
                {
                    cited.insert(ev.to_owned());
                }
            }
        }
    }

    let mut by_slice = BTreeMap::new();
    for sid in q2 {
        let mut slice_evs = BTreeSet::new();
        for table in &payloads[&sid].tables {
            for row in decode_compact_table_v5(table).expect("decode") {
                collect_ev(&row.to_strict_json_value().expect("json"), &mut slice_evs);
            }
        }
        let intersection: BTreeSet<String> = slice_evs.intersection(&cited).cloned().collect();
        by_slice.insert(sid, intersection);
    }
    by_slice
}

#[test]
fn test_compact_observation_candidate_preserves_semantics_and_obs_witness() {
    for cell in plan_v5_cells(V5Suite::V5Dev, DEFAULT_V5_SEED) {
        let (baseline_ep, baseline_or) = build_v5_episode(&cell).expect("baseline");
        let (cand_ep, cand_or) = build_v5_compact_candidate(&cell).expect("compact");

        assert_eq!(cand_ep.episode_id, baseline_ep.episode_id);
        assert_eq!(cand_ep.family_id, baseline_ep.family_id);
        assert_eq!(cand_ep.policy_card, baseline_ep.policy_card);
        assert_eq!(cand_or.internal_regime_id, baseline_or.internal_regime_id);

        cand_ep.validate().expect("cand ep validates");
        cand_or.validate().expect("cand oracle validates");

        let conn_id = &cand_ep.phase1_manifest.work_queue.target_connector_ids[0];
        let conn_scope = format!("connector_id={conn_id}");
        let conn_desc = cand_ep
            .phase1_manifest
            .source_directory
            .iter()
            .find(|d| d.scope_filter == conn_scope)
            .expect("conn desc");
        let conn_slice = &cand_ep.phase1_slices[&conn_desc.slice_id];
        assert_eq!(conn_slice.tables[0].table_name, "connector_pages");
        assert_eq!(conn_desc.authority_tier, AuthorityTierV5::AuthoritativePrimary);

        let cand_q1 = obs_strategy_q1(&cand_ep.phase1_manifest, &cand_ep.policy_card, 1).expect("q1");
        for sid in &cand_q1 {
            assert!(
                cand_ep.phase1_slices[sid]
                    .tables
                    .iter()
                    .all(|t| t.table_name != "connector_pages")
            );
        }

        for phase_index in [1, 2] {
            let (base_m, base_p) = if phase_index == 1 {
                (&baseline_ep.phase1_manifest, &baseline_ep.phase1_slices)
            } else {
                (&baseline_ep.phase2_base_manifest, &baseline_ep.phase2_slices)
            };
            let (cand_m, cand_p) = if phase_index == 1 {
                (&cand_ep.phase1_manifest, &cand_ep.phase1_slices)
            } else {
                (&cand_ep.phase2_base_manifest, &cand_ep.phase2_slices)
            };
            let bq1 = obs_strategy_q1(base_m, &baseline_ep.policy_card, phase_index).expect("bq1");
            let cq1 = obs_strategy_q1(cand_m, &cand_ep.policy_card, phase_index).expect("cq1");
            assert_eq!(cq1, bq1);
            let bq1_s: Vec<_> = bq1.iter().map(|s| base_p[s].clone()).collect();
            let cq1_s: Vec<_> = cq1.iter().map(|s| cand_p[s].clone()).collect();
            let bq2 = obs_strategy_q2(
                base_m,
                &bq1_s,
                &base_m.work_queue,
                &baseline_ep.policy_card,
                phase_index,
            )
            .expect("bq2");
            let cq2 = obs_strategy_q2(
                cand_m,
                &cq1_s,
                &cand_m.work_queue,
                &cand_ep.policy_card,
                phase_index,
            )
            .expect("cq2");
            assert_eq!(cq2, bq2);

            let by_slice = q2_cited_evidence_by_slice(&cand_ep, &cand_or, phase_index);
            if phase_index == 1 || cell.family_id != "F09" {
                assert!(by_slice.values().any(|s| !s.is_empty()));
            } else {
                assert!(by_slice.values().all(|s| s.is_empty()));
            }
        }

        let cand_evs = source_evidence_ids(&cand_ep);
        assert!(
            cand_evs
                .iter()
                .all(|ev| ev.starts_with("ev_") && ev.len() == 6 && ev[3..].bytes().all(|b| b.is_ascii_digit()))
        );
        let mut policy_evs = BTreeSet::new();
        collect_ev(
            &cand_ep.policy_card.to_strict_json_value().expect("pc"),
            &mut policy_evs,
        );
        let allowed_evs: BTreeSet<String> = cand_evs.union(&policy_evs).cloned().collect();
        for m in cand_or
            .gold_p1_mutations
            .iter()
            .chain(cand_or.gold_p2_mutations.iter())
        {
            let v = m.to_strict_json_value().expect("m");
            if let Some(arr) = v.get("cite").and_then(|c| c.as_array()) {
                for item in arr {
                    if let Some(s) = item.as_str()
                        && let Some((_, ev)) = s.rsplit_once(':')
                    {
                        assert!(allowed_evs.contains(ev), "missing cited ev {ev}");
                    }
                }
            }
        }

        // Verify dept_prefix=/d/p has grants table
        let dept_desc = cand_ep
            .phase1_manifest
            .source_directory
            .iter()
            .find(|d| d.scope_filter == "dept_prefix=/d/p")
            .expect("dept /d/p");
        let dept_rows =
            decode_compact_table_v5(&cand_ep.phase1_slices[&dept_desc.slice_id].tables[0])
                .expect("decode");
        assert!(matches!(dept_rows[0], TableRowV5::Grant(_)));
    }
}

#[test]
fn test_compact_observation_candidate_keeps_dev_family_regime_and_mutation_coverage() {
    let cells = plan_v5_cells(V5Suite::V5Dev, DEFAULT_V5_SEED);
    let fams: BTreeSet<&str> = cells.iter().map(|c| c.family_id.as_str()).collect();
    assert_eq!(fams, V5_FAMILIES.into_iter().collect());
    for r in V5_REGIMES {
        assert_eq!(cells.iter().filter(|c| c.regime_id == r).count(), 3);
    }
    let mut mutation_kinds = BTreeSet::new();
    let mut decisive_p1 = 0usize;
    let mut decisive_p2 = 0usize;
    let mut missing_decisive_q2 = Vec::new();
    for cell in &cells {
        let (ep, or) = build_v5_compact_candidate(cell).expect("compact");
        for m in or
            .gold_p1_mutations
            .iter()
            .chain(or.gold_p2_mutations.iter())
        {
            mutation_kinds.insert(m.mutation_kind().to_owned());
        }
        let b1 = q2_cited_evidence_by_slice(&ep, &or, 1);
        if b1.values().any(|s| !s.is_empty()) {
            decisive_p1 += 1;
        } else {
            missing_decisive_q2.push((cell.family_id.clone(), 1));
        }
        let b2 = q2_cited_evidence_by_slice(&ep, &or, 2);
        if b2.values().any(|s| !s.is_empty()) {
            decisive_p2 += 1;
        } else {
            missing_decisive_q2.push((cell.family_id.clone(), 2));
        }
    }
    assert_eq!(mutation_kinds.len(), 12);
    assert_eq!(decisive_p1, 12);
    assert_eq!(decisive_p2, 11);
    assert_eq!(missing_decisive_q2, vec![("F09".to_owned(), 2)]);
}

#[test]
fn test_compact_observation_profile_rejects_held_out_suites() {
    let cell = plan_v5_cells(V5Suite::V5Core, DEFAULT_V5_SEED)
        .into_iter()
        .next()
        .expect("core cell");
    let err = build_v5_compact_candidate(&cell).expect_err("must reject v5_core");
    assert!(err.to_string().contains(COMPACT_OBSERVATION_PROFILE_V1));
}
