//! Real Draft 5 receipt, hash, artifact, and tamper-rejection parity tests.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use ib_v5_conformance::process::{
    SharedRuntimeError, audit_profile_python_hygiene, check_python_worker_hygiene, make_readonly,
    remove_readonly_tree, resolve_source_profile, verify_blind_freeze_receipt,
};
use ib_v5_conformance::shared::{
    P1_EPISODE_KEYS, PINNED_PUBLIC_SCHEMA_SHA256, PROTOCOL, canonical_json_bytes, freeze_case,
    load_family_map, serialize_recipe_case, sha256_bytes, strict_json_loads, strict_jsonl_loads,
};
use serde_json::{Map, Value, json};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates")
        .parent()
        .expect("repo root")
        .to_path_buf()
}

struct TempDirGuard(PathBuf);

impl TempDirGuard {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "ib_v5_conformance_{name}_{}",
            std::process::id()
        ));
        remove_readonly_tree(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir temp");
        Self(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDirGuard {
    fn drop(&mut self) {
        remove_readonly_tree(&self.0);
    }
}

fn write_readonly(root: &Path, rel: &str, bytes: &[u8]) -> (PathBuf, String) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("mkdir parent");
    }
    if path.exists() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644));
        }
    }
    std::fs::write(&path, bytes).expect("write file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o444)).expect("chmod");
    }
    (path, sha256_bytes(bytes))
}

#[test]
fn pinned_public_schema_hash_matches_disk() {
    let root = repo_root();
    let schemas_bytes =
        std::fs::read(root.join("src/identitybench/v5/schemas.py")).expect("read schemas.py");
    assert_eq!(sha256_bytes(&schemas_bytes), PINNED_PUBLIC_SCHEMA_SHA256);
}

#[test]
fn freeze_case_and_recipe_case_match_python_bit_for_bit() {
    let root = repo_root();
    let ep_raw = std::fs::read(
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/selected-episodes.jsonl"),
    )
    .expect("read selected-episodes.jsonl");
    let rows = strict_jsonl_loads(&ep_raw).expect("parse jsonl");
    let ep = rows[0].as_object().expect("first row object");
    let card = std::fs::read_to_string(
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/rendered-card.txt"),
    )
    .expect("read rendered-card.txt");

    let mut p1_ep = Map::new();
    for k in P1_EPISODE_KEYS {
        p1_ep.insert(k.to_owned(), ep[k].clone());
    }
    let case = json!({
        "protocol": PROTOCOL,
        "case_id": "parity_p1_derive",
        "operation": "derive_phase",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": Value::Object(p1_ep),
        "rendered_card": card,
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {},
    });
    let fc = freeze_case(&case).expect("freeze_case");
    assert_eq!(
        fc.input_sha256,
        "d8321457d325a00ee4d653622f40f8d19f67f0140d60cd58bf4e440d222b3a98"
    );
    assert_eq!(fc.canonical_bytes.len(), 164_775);

    let rec = json!({
        "recipe_id": "parity_p1_val",
        "input_ref": "parity_p1_derive",
        "operation": "validate_submission",
        "stimulus": {
            "recipe": {
                "candidate_source": "independent_base",
                "base_scope": "phase_plan",
                "patches": [{"op": "replace", "path": "/0/if_match", "value": ["x"]}]
            }
        }
    });
    let rc = serialize_recipe_case(&case, &rec).expect("serialize_recipe_case");
    assert_eq!(
        rc.input_sha256,
        "ccf070df8bbb6d36d2317bd716694024443e7dfb49e787dc990ff53c520b797b"
    );
    assert_eq!(rc.canonical_bytes.len(), 164_917);
}

#[test]
fn real_family_maps_load_with_python_exact_hashes() {
    let root = repo_root();
    let fm3_raw =
        std::fs::read(root.join("reports/v5/conformance/shared_inputs/v3/family_map.json"))
            .expect("read v3 family_map");
    assert_eq!(
        sha256_bytes(&fm3_raw),
        "51c92e4217f40d7699d7eaa9ced0eab71eeca51d10b018ea5eb448c171efc762"
    );
    let mut expected_hashes = BTreeMap::new();
    expected_hashes.insert(
        "v5_dev".to_owned(),
        "0ce39ff37c5defecfca324a1e0d87b28aa6c4fb51754f2666a1ca6ed4bf4faec".to_owned(),
    );
    expected_hashes.insert(
        "v5_lite".to_owned(),
        "d35a1c164a1c3700ed180aa0d14dcb5a4c4aa7c93fa301284f0138e54f057974".to_owned(),
    );
    expected_hashes.insert(
        "v5_core".to_owned(),
        "cc0eceaae31f1e80fc1a16460d2210d2ecf9c51758a5648c40a484f9583a0038".to_owned(),
    );
    expected_hashes.insert(
        "v5_full".to_owned(),
        "9ebfd92ccb1ca3a37f9184962d3e5de434d27082810f9598775ebdb068476464".to_owned(),
    );
    let (map, digest) = load_family_map(&fm3_raw, &expected_hashes).expect("load v3 family map");
    assert_eq!(
        digest,
        "51c92e4217f40d7699d7eaa9ced0eab71eeca51d10b018ea5eb448c171efc762"
    );
    assert_eq!(map.len(), 4);
}

#[test]
fn all_4_draft5_receipts_match_python_verify_blind_freeze_and_bind_disk_sources() {
    let root = repo_root();
    for suite in ["dev", "lite", "core", "full"] {
        let rel = format!("reports/v5/conformance/v5_draft5_{suite}/receipt.json");
        let full = root.join(&rel);
        let raw = std::fs::read(&full).expect("read draft5 receipt");
        let receipt_sha = sha256_bytes(&raw);

        // 1. Parity with Python verify_blind_freeze_receipt: Draft 5 bound-reference receipts
        //    live under reports/v5/conformance/v5_draft5_*/receipt.json (not shared_results/),
        //    so both Python and Rust reject with the exact same SharedRuntimeError.
        let err = verify_blind_freeze_receipt(Path::new(&rel), &receipt_sha, &root)
            .expect_err("bound-reference receipt is outside shared_results root");
        assert_eq!(
            err,
            SharedRuntimeError("artifact path is outside a versioned shared-results root".to_owned())
        );

        // 2. Verify strict JSON parsing and every bound reference + source_sha256 in the receipt!
        let parsed = strict_json_loads(&raw).expect("strict json parse receipt");
        let obj = parsed.as_object().expect("receipt object");
        assert_eq!(
            obj["schema_version"].as_str(),
            Some("identitybench.v5.bound-reference-receipt.v1")
        );
        assert_eq!(
            obj["suite_id"].as_str(),
            Some(format!("v5_{suite}").as_str())
        );
        let refs = obj["references"].as_object().expect("references");
        for slot in ["full_spec_blind", "agent_view_only"] {
            let entry = refs[slot].as_object().expect("ref entry");
            let art_rel = entry["artifact_path"].as_str().expect("artifact_path");
            let art_sha = entry["artifact_sha256"].as_str().expect("artifact_sha256");
            let art_bytes =
                std::fs::read(full.parent().unwrap().join(art_rel)).expect("read ref artifact");
            assert_eq!(sha256_bytes(&art_bytes), art_sha, "{suite}/{slot}");
        }
        let rev = obj["review_provenance"]
            .as_object()
            .expect("review_provenance");
        let rev_bytes = std::fs::read(
            full.parent()
                .unwrap()
                .join(rev["artifact_path"].as_str().unwrap()),
        )
        .expect("read review_provenance");
        assert_eq!(
            sha256_bytes(&rev_bytes),
            rev["artifact_sha256"].as_str().unwrap()
        );
        let src_hashes = obj["source_sha256"].as_object().expect("source_sha256");
        for spec_or_conf in [
            "SPEC-v5.md",
            "SPEC-v5-ADDENDUM-01.md",
            "SPEC-v5-DIRECTIVE-01.md",
            "SPEC-v5.0-draft.5.md",
            "src/identitybench/v5/schemas.py",
            "src/identitybench/v5/conformance_shared.py",
            "src/identitybench/v5/conformance_safety_domain.py",
            "src/identitybench/v5/conformance_process.py",
            "tests/conformance/v5/promoted_draft5_full_spec_v1/reference.py",
            "tests/conformance/v5/promoted_draft5_card_only_v1/worker.py",
        ] {
            if let Some(expected) = src_hashes.get(spec_or_conf).and_then(Value::as_str) {
                let disk_bytes = std::fs::read(root.join(spec_or_conf)).expect("read bound source");
                assert_eq!(
                    sha256_bytes(&disk_bytes),
                    expected,
                    "source mismatch {suite}/{spec_or_conf}"
                );
            }
        }
    }
}

#[test]
fn real_committed_workers_pass_rust_python_hygiene_scanner() {
    let root = repo_root();
    for (slot, profile_id) in [
        ("full_spec", "promoted_draft5_full_spec_v1"),
        ("full_spec", "approved_full_spec_v1"),
        ("full_spec", "bounded_full_spec_v1"),
        ("full_spec", "fresh_full_spec_v3"),
        ("agent_view", "promoted_draft5_card_only_v1"),
        ("agent_view", "fresh_card_only_v2"),
    ] {
        let profile = resolve_source_profile(slot, Some(profile_id)).expect("resolve profile");
        audit_profile_python_hygiene(&root, &profile)
            .unwrap_or_else(|e| panic!("{slot}/{profile_id} failed hygiene: {e}"));
    }
}

#[test]
fn tamper_rejection_and_synthetic_v4_blind_freeze_verification() {
    let tmp = TempDirGuard::new("v4_tamper");
    let root = tmp.path();
    let real_root = repo_root();

    // Load 1 real public episode from Draft 5 dev to construct valid canonical public cases
    let ep_raw = std::fs::read(
        real_root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/selected-episodes.jsonl"),
    )
    .expect("read ep");
    let rows = strict_jsonl_loads(&ep_raw).expect("jsonl");
    let ep = rows[0].as_object().expect("ep0");
    let card = std::fs::read_to_string(
        real_root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/rendered-card.txt"),
    )
    .expect("card");
    let mut p1_ep = Map::new();
    for k in P1_EPISODE_KEYS {
        p1_ep.insert(k.to_owned(), ep[k].clone());
    }

    let make_case = |cid: &str, op: &str, phase: i64, stim: Value| -> Value {
        let ep_val = if phase == 1 {
            Value::Object(p1_ep.clone())
        } else {
            let mut m = Map::new();
            for k in ib_v5_conformance::shared::P2_EPISODE_KEYS {
                m.insert(k.to_owned(), ep[k].clone());
            }
            Value::Object(m)
        };
        json!({
            "protocol": PROTOCOL,
            "case_id": cid,
            "operation": op,
            "suite_id": "v5_dev",
            "phase_index": phase,
            "public_episode": ep_val,
            "rendered_card": card,
            "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
            "stimulus": stim,
        })
    };

    let c_p1 = make_case("ep0/p1/derive", "derive_phase", 1, json!({}));
    let c_p2 = make_case("ep0/p2/derive", "derive_phase", 2, json!({}));
    let recipe_val = json!({
        "recipe_id": "ep0/p1/val",
        "input_ref": "ep0/p1/derive",
        "operation": "validate_submission",
        "stimulus": {"submitted_plan": []}
    });
    let c_rec = serialize_recipe_case(&c_p1, &recipe_val).expect("recipe");
    let safety_rec = json!({
        "recipe_id": "ep0/p1/safety",
        "input_ref": "ep0/p1/derive",
        "operation": "safety",
        "stimulus": {
            "target_selector": {"mutation_kind": "ACCOUNT_RECON_MUTATION", "target_entity_id": "acct_01"},
            "submitted_plan": []
        }
    });
    let c_safety = serialize_recipe_case(&c_p1, &safety_rec).expect("safety");
    let trace_rec = json!({
        "recipe_id": "ep0/p1/trace",
        "input_ref": "ep0/p1/derive",
        "operation": "score_trace",
        "stimulus": {"mode": "COUPLED", "events": []}
    });
    let c_trace = serialize_recipe_case(&c_p1, &trace_rec).expect("trace");

    let all_cases = vec![
        freeze_case(&c_p1).unwrap(),
        freeze_case(&c_p2).unwrap(),
        c_rec.clone(),
        c_safety.clone(),
        c_trace.clone(),
    ];

    let mut public_raw = Vec::new();
    for c in &all_cases {
        public_raw.extend_from_slice(&c.canonical_bytes);
        public_raw.push(b'\n');
    }
    let result_root = "reports/v5/conformance/shared_results/approved_addendum_v1";
    let (_, pub_sha) = write_readonly(root, &format!("{result_root}/public-inputs.jsonl"), &public_raw);

    let mut phase_raw = Vec::new();
    for c in &all_cases[..2] {
        phase_raw.extend_from_slice(&c.canonical_bytes);
        phase_raw.push(b'\n');
    }
    let (_, phase_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/stimuli/phase_inputs.jsonl",
        &phase_raw,
    );
    let mut recipes_raw = Vec::new();
    recipes_raw.extend_from_slice(&canonical_json_bytes(&recipe_val).unwrap());
    recipes_raw.push(b'\n');
    recipes_raw.extend_from_slice(&canonical_json_bytes(&safety_rec).unwrap());
    recipes_raw.push(b'\n');
    let (_, recipes_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/stimuli/recipes.jsonl",
        &recipes_raw,
    );
    let mut traces_raw = canonical_json_bytes(&trace_rec).unwrap();
    traces_raw.push(b'\n');
    let (_, traces_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/stimuli/traces.jsonl",
        &traces_raw,
    );

    let schemas_sha = PINNED_PUBLIC_SCHEMA_SHA256;
    let spec_sha = ib_v5_conformance::shared::FROZEN_SPEC_SHA256;
    let normative_inputs = json!({
        "schemas_path": "src/identitybench/v5/schemas.py",
        "schemas_sha256": schemas_sha,
        "spec_path": "SPEC-v5.md",
        "spec_sha256": spec_sha,
    });
    let phase_case_index: Vec<Value> = all_cases[..2]
        .iter()
        .map(|c| {
            json!({
                "case_id": c.case_id(),
                "canonical_case_sha256": c.input_sha256,
                "canonical_case_bytes": c.canonical_bytes.len(),
                "episode_id": c.public_case.episode_id(),
                "phase_index": c.public_case.phase_index,
                "suite_id": c.public_case.suite_id,
            })
        })
        .collect();
    let checkpoint_json = json!({
        "protocol": PROTOCOL,
        "corpus_version": "v3",
        "normative_inputs": normative_inputs,
        "phase_input_artifact": {
            "path": "stimuli/phase_inputs.jsonl",
            "sha256": phase_sha,
            "row_count": 2,
            "byte_length": phase_raw.len(),
            "canonicalization": "UTF-8 JSON; sorted keys; compact separators; trailing LF per record",
            "case_digest": "SHA-256 over each full canonical envelope without trailing LF",
        },
        "phase_case_index": phase_case_index,
        "phase_target_index": [],
    });
    let (_, cp_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/phase_checkpoint.json",
        &(canonical_json_bytes(&checkpoint_json).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
    );
    let (_, fam_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/family_map.json",
        b"{}\n",
    );

    let mut case_index = Vec::new();
    for c in &all_cases[..2] {
        case_index.push(json!({
            "case_id": c.case_id(),
            "canonical_case_sha256": c.input_sha256,
            "canonical_case_bytes": c.canonical_bytes.len(),
            "episode_id": c.public_case.episode_id(),
            "operation": c.public_case.operation,
            "phase_index": c.public_case.phase_index,
            "suite_id": c.public_case.suite_id,
            "source_file": "stimuli/phase_inputs.jsonl",
        }));
    }
    for (idx, (c, rec, sfile)) in [
        (&all_cases[2], &recipe_val, "stimuli/recipes.jsonl"),
        (&all_cases[3], &safety_rec, "stimuli/recipes.jsonl"),
        (&all_cases[4], &trace_rec, "stimuli/traces.jsonl"),
    ]
    .into_iter()
    .enumerate()
    {
        let rec_idx = if idx == 2 { 0 } else { idx };
        case_index.push(json!({
            "case_id": c.case_id(),
            "canonical_case_sha256": c.input_sha256,
            "canonical_case_bytes": c.canonical_bytes.len(),
            "episode_id": c.public_case.episode_id(),
            "operation": c.public_case.operation,
            "phase_index": c.public_case.phase_index,
            "suite_id": c.public_case.suite_id,
            "input_ref": rec["input_ref"],
            "stimulus_record_id": rec["recipe_id"],
            "stimulus_record_index": rec_idx,
            "stimulus_source_file": sfile,
        }));
    }
    let manifest_json = json!({
        "artifact_status": "synthetic",
        "corpus_version": "v3",
        "protocol": PROTOCOL,
        "coverage_intent_only": true,
        "envelope_contract": {},
        "observed_coverage": {},
        "packet_sources": {},
        "normative_inputs": normative_inputs,
        "phase_checkpoint": {
            "path": "phase_checkpoint.json",
            "sha256": cp_sha,
            "phase_case_count": 2,
            "phase_inputs_path": "stimuli/phase_inputs.jsonl",
            "phase_inputs_sha256": phase_sha,
            "preserved_checkpoint_bytes": true,
        },
        "stimulus_sources": [
            {"path": "stimuli/recipes.jsonl", "sha256": recipes_sha, "record_count": 2, "copied_byte_exact_from": "test"},
            {"path": "stimuli/traces.jsonl", "sha256": traces_sha, "record_count": 1, "copied_byte_exact_from": "test"},
        ],
        "family_metadata": {"path": "family_map.json", "sha256": fam_sha},
        "case_index": case_index,
        "counts": {
            "episodes": 1,
            "operations": {"derive_phase": 2, "safety": 1, "score_trace": 1, "validate_submission": 1},
            "phase_cases": 2,
            "phase_target_tuples": 0,
            "recipe_cases": 2,
            "total_cases": 5,
            "trace_cases": 1,
        }
    });
    let (_, manifest_sha) = write_readonly(
        root,
        "reports/v5/conformance/shared_inputs/v3/manifest.json",
        &(canonical_json_bytes(&manifest_json).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
    );

    let case_inventory: Vec<(String, String)> = all_cases
        .iter()
        .map(|c| (c.case_id().to_owned(), c.input_sha256.clone()))
        .collect();
    let idx_sha = ib_v5_conformance::process::case_index_sha256(&case_inventory).unwrap();
    let case_manifest_sha = sha256_bytes(
        &canonical_json_bytes(&json!({
            "protocol": PROTOCOL,
            "canonicalization": "canonical-json-v1",
            "cases": case_inventory.iter().map(|(cid, sha)| json!({"case_id": cid, "input_sha256": sha})).collect::<Vec<_>>(),
        }))
        .unwrap(),
    );

    let scope_id = "synthetic-v4";
    let sidecar_val = json!({
        "protocol": PROTOCOL,
        "materialization_contract_id": ib_v5_conformance::shared::MATERIALIZATION_CONTRACT_ID,
        "score_metric_nullability_contract_id": ib_v5_conformance::shared::SCORE_METRIC_NULLABILITY_CONTRACT_ID,
        "scope_id": scope_id,
    });
    let (_, sidecar_sha) = write_readonly(
        root,
        &format!("{result_root}/sidecar.json"),
        &(canonical_json_bytes(&sidecar_val).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
    );

    let normative_contract = ib_v5_schemas::normative_contract::approved_v5_normative_contract();
    let mut evaluators_obj = Map::new();
    for (ev_id, wire_id, prof_id) in [
        ("full_spec", "full_spec_blind", "approved_full_spec_v1"),
        ("agent_view", "agent_view_only", "fresh_card_only_v2"),
    ] {
        let prof = resolve_source_profile(ev_id, Some(prof_id)).unwrap();
        let cap_dir = format!("{result_root}/capture/{ev_id}");
        let mut source_rows = Vec::new();
        for src_rel in &prof.source_files {
            let src_bytes = std::fs::read(real_root.join(src_rel)).expect("read real source");
            let (_, sha) = write_readonly(root, &format!("{cap_dir}/{src_rel}"), &src_bytes);
            source_rows.push(json!({"path": src_rel, "sha256": sha, "size_bytes": src_bytes.len()}));
        }
        let mut cap_manifest = json!({
            "format": "identitybench.v5.shared-conformance-source-checkpoint.v1",
            "evaluator_id": ev_id,
            "source_profile": prof.profile_id,
            "author_origin": prof.author_origin,
            "entrypoint": prof.entrypoint,
            "entrypoint_module": prof.entrypoint_module,
            "source_files": source_rows,
            "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
            "capture_status": "verified_source_copy",
        });
        let cap_m_sha = sha256_bytes(&canonical_json_bytes(&cap_manifest).unwrap());
        cap_manifest
            .as_object_mut()
            .unwrap()
            .insert("manifest_sha256".to_owned(), Value::String(cap_m_sha));
        let (_, cap_sha) = write_readonly(
            root,
            &format!("{cap_dir}/capture-manifest.json"),
            &(canonical_json_bytes(&cap_manifest).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
        );

        let dep_bytes = b"pass\n";
        let dep_rel_dir = format!("{result_root}/deps/{ev_id}");
        let (_, dep_file_sha) =
            write_readonly(root, &format!("{dep_rel_dir}/frozen-dependencies/dep.py"), dep_bytes);
        let dep_files = vec![json!({"path": "dep.py", "sha256": dep_file_sha, "size_bytes": dep_bytes.len()})];
        let dep_snap = json!({
            "file_count": 1,
            "files_sha256": sha256_bytes(&canonical_json_bytes(&Value::Array(dep_files.clone())).unwrap()),
            "packages": ["typing-extensions"],
            "staged_readonly": true,
        });

        let inv_paths = ib_v5_conformance::process::runtime_inventory_for_profile(&prof);
        let mut inv_rows = Vec::new();
        let arch_dir = format!("{result_root}/archive/{ev_id}");
        let mut arch_src_rows = source_rows.clone();
        for p in inv_paths {
            let b = b"# runtime\n";
            let (_, s) = write_readonly(root, &format!("{arch_dir}/{p}"), b);
            let r = json!({"path": p, "sha256": s, "size_bytes": b.len()});
            inv_rows.push(r.clone());
            arch_src_rows.push(r);
        }
        for src_rel in &prof.source_files {
            let src_bytes = std::fs::read(real_root.join(src_rel)).expect("read real source");
            write_readonly(root, &format!("{arch_dir}/{src_rel}"), &src_bytes);
        }

        let mut closure_payload = json!({
            "format": if prof.normative_contract_id.is_some() {
                "identitybench.v5.evaluator-closure.v2"
            } else {
                "identitybench.v5.evaluator-closure.v1"
            },
            "evaluator_id": ev_id,
            "entrypoint": prof.entrypoint,
            "approved_source_files": source_rows,
            "runtime_inventory": inv_rows,
            "dependency_files": dep_files,
            "python_runtime": {},
            "source_profile": prof.profile_id,
            "author_origin": prof.author_origin,
            "entrypoint_module": prof.entrypoint_module,
        });
        if prof.normative_contract_id.is_some() {
            closure_payload
                .as_object_mut()
                .unwrap()
                .insert("normative_contract".to_owned(), normative_contract.clone());
        }
        let closure_sha = sha256_bytes(&canonical_json_bytes(&closure_payload).unwrap());

        let mut rt_manifest = closure_payload.as_object().unwrap().clone();
        rt_manifest.insert("source_closure_sha256".to_owned(), Value::String(closure_sha.clone()));
        rt_manifest.insert("case_count".to_owned(), Value::Number(5.into()));
        rt_manifest.insert("dependency_snapshot".to_owned(), dep_snap.clone());
        let (_, rt_sha) = write_readonly(
            root,
            &format!("{result_root}/{ev_id}-runtime.json"),
            &(canonical_json_bytes(&Value::Object(rt_manifest)).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
        );

        let mut arch_manifest = json!({
            "format": "identitybench.v5.shared-conformance-runtime-source-archive.v1",
            "evaluator_id": ev_id,
            "source_profile": prof.profile_id,
            "author_origin": prof.author_origin,
            "source_closure_sha256": closure_sha,
            "case_manifest_sha256": case_manifest_sha,
            "source_files": arch_src_rows,
            "dependency_snapshot": dep_snap,
            "python_runtime": {},
            "runtime_inventory": inv_rows,
        });
        let arch_sha_val = sha256_bytes(&canonical_json_bytes(&arch_manifest).unwrap());
        arch_manifest
            .as_object_mut()
            .unwrap()
            .insert("archive_manifest_sha256".to_owned(), Value::String(arch_sha_val));
        let (_, rt_arch_sha) = write_readonly(
            root,
            &format!("{arch_dir}/source-manifest.json"),
            &(canonical_json_bytes(&arch_manifest).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
        );

        let mut dep_manifest = json!({
            "format": "identitybench.v5.phase-only-frozen-dependencies.v1",
            "evaluator_slot": ev_id,
            "source_profile": prof.profile_id,
            "author_origin": prof.author_origin,
            "source_closure_sha256": closure_sha,
            "dependency_snapshot": dep_snap,
            "dependency_files": dep_files,
        });
        let dep_m_sha = sha256_bytes(&canonical_json_bytes(&dep_manifest).unwrap());
        dep_manifest
            .as_object_mut()
            .unwrap()
            .insert("manifest_sha256".to_owned(), Value::String(dep_m_sha));
        let (_, dep_sha) = write_readonly(
            root,
            &format!("{dep_rel_dir}/dep-manifest.json"),
            &(canonical_json_bytes(&dep_manifest).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
        );

        let mut raw_rows = Vec::new();
        let mut audit_rows = Vec::new();
        for c in &all_cases {
            let res = json!({
                "protocol": PROTOCOL,
                "evaluator_id": wire_id,
                "case_id": c.case_id(),
                "input_sha256": c.input_sha256,
                "status": "underdefined",
                "payload": if c.public_case.operation == "score_trace" {
                    json!({"metrics": {
                        "p1_first_stage_snapshot_ok": null, "G-SRR@first_stage": null, "Sem-SRR@commit": null,
                        "Action-SRR@last_stage": null, "G-PROP": null, "G-SRR": null, "p2_observed": null,
                        "both_phases_staged": null, "p1_staged": null, "P1-Action-SRR@last_stage": null, "repair_lift": null
                    }, "snapshot_derivations": []})
                } else if c.public_case.operation == "safety" {
                    json!({"overgrant": false, "availability_breach": false})
                } else if c.public_case.operation == "validate_submission" {
                    json!({"first_failing_stage": null, "diagnostics": []})
                } else {
                    json!({"mutations": [], "policy_card_anchor_map": {}})
                },
                "materialization": Value::Null,
                "coverage": {"phase_targets": [], "diagnostics": [], "rules": [], "safety_cases": [], "snapshots": []},
                "gaps": [{"code": "OTHER", "operation": c.public_case.operation, "case_id": c.case_id(), "path": "", "detail": "synthetic"}],
            });
            let audit = json!({
                "audit_protocol": "shared-audit-v1",
                "evaluator_id": ev_id,
                "read_paths": [],
                "import_names": [],
                "import_paths": [],
                "write_attempts": [],
                "blocked_events": [],
                "network_attempts": [],
                "author_stdout_bytes": 0,
                "author_stderr_bytes": 0,
            });
            let stdout = canonical_json_bytes(&res).unwrap();
            let stderr = canonical_json_bytes(&audit).unwrap();
            let row = json!({
                "protocol": PROTOCOL,
                "evaluator_id": ev_id,
                "source_profile": prof.profile_id,
                "author_origin": prof.author_origin,
                "case_id": c.case_id(),
                "input_sha256": c.input_sha256,
                "source_closure_sha256": closure_sha,
                "case_manifest_sha256": case_manifest_sha,
                "execution_status": "ok",
                "result": res,
                "audit": audit,
                "result_sha256": sha256_bytes(&stdout),
                "stdout_sha256": sha256_bytes(&stdout),
                "stderr_sha256": sha256_bytes(&stderr),
                "raw_stdout_b64": ib_v5_conformance::shared::base64_encode(&stdout),
                "raw_stderr_b64": ib_v5_conformance::shared::base64_encode(&stderr),
            });
            let a_row = json!({
                "audit": row["audit"],
                "case_id": row["case_id"],
                "execution_status": row["execution_status"],
                "input_sha256": row["input_sha256"],
                "raw_stderr_b64": row["raw_stderr_b64"],
                "raw_stdout_b64": row["raw_stdout_b64"],
                "source_closure_sha256": row["source_closure_sha256"],
                "stderr_sha256": row["stderr_sha256"],
                "stdout_sha256": row["stdout_sha256"],
            });
            raw_rows.extend_from_slice(&canonical_json_bytes(&row).unwrap());
            raw_rows.push(b'\n');
            audit_rows.extend_from_slice(&canonical_json_bytes(&a_row).unwrap());
            audit_rows.push(b'\n');
        }
        let (_, raw_sha) = write_readonly(root, &format!("{result_root}/{ev_id}-receipts.jsonl"), &raw_rows);
        let (_, aud_sha) = write_readonly(root, &format!("{result_root}/{ev_id}-audits.jsonl"), &audit_rows);

        let raw_ref = json!({"path": format!("{result_root}/{ev_id}-receipts.jsonl"), "sha256": raw_sha, "row_count": 5});
        let aud_ref = json!({"path": format!("{result_root}/{ev_id}-audits.jsonl"), "sha256": aud_sha, "row_count": 5});
        let run_man = json!({
            "source_closure_sha256": closure_sha,
            "case_manifest_sha256": case_manifest_sha,
            "raw_receipts": raw_ref,
            "raw_audits": aud_ref,
        });
        let (_, run_sha) = write_readonly(
            root,
            &format!("{result_root}/{ev_id}-run.json"),
            &(canonical_json_bytes(&run_man).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
        );
        let (_, qual_sha) = write_readonly(
            root,
            &format!("reports/v5/conformance/review/{ev_id}-qual.json"),
            b"{\"status\":\"QUALIFIED\"}\n",
        );

        evaluators_obj.insert(
            ev_id.to_owned(),
            json!({
                "wire_evaluator_id": wire_id,
                "source_profile": prof.profile_id,
                "author_origin": prof.author_origin,
                "source_closure_sha256": closure_sha,
                "source_capture_manifest": {"path": format!("{cap_dir}/capture-manifest.json"), "sha256": cap_sha},
                "runtime_manifest": {"path": format!("{result_root}/{ev_id}-runtime.json"), "sha256": rt_sha},
                "runtime_archive": {"path": format!("{arch_dir}/source-manifest.json"), "sha256": rt_arch_sha},
                "dependency_manifest": {"path": format!("{dep_rel_dir}/dep-manifest.json"), "sha256": dep_sha},
                "raw_receipts": raw_ref,
                "raw_audits": aud_ref,
                "run_manifest": {"path": format!("{result_root}/{ev_id}-run.json"), "sha256": run_sha},
                "qualification": {"path": format!("reports/v5/conformance/review/{ev_id}-qual.json"), "sha256": qual_sha},
            }),
        );
    }

    let receipt = json!({
        "schema_version": "identitybench.v5.blind-output-freeze.v4",
        "proof_scope": "full_shared_conformance",
        "scope": {
            "scope_id": scope_id,
            "case_count": 5,
            "case_index_sha256": idx_sha,
            "operations": ["derive_phase", "safety", "score_trace", "validate_submission"],
            "phase_indices": [1, 2],
            "turn_context": "T0",
            "steering": "UNSTEERED",
        },
        "case_inventory": case_inventory.iter().map(|(cid, sha)| json!({"case_id": cid, "input_sha256": sha})).collect::<Vec<_>>(),
        "public_inputs": {"path": format!("{result_root}/public-inputs.jsonl"), "sha256": pub_sha},
        "input_artifacts": {
            "manifest": {"path": "reports/v5/conformance/shared_inputs/v3/manifest.json", "sha256": manifest_sha},
            "phase_inputs": {"path": "reports/v5/conformance/shared_inputs/v3/stimuli/phase_inputs.jsonl", "sha256": phase_sha},
            "phase_checkpoint": {"path": "reports/v5/conformance/shared_inputs/v3/phase_checkpoint.json", "sha256": cp_sha},
            "recipes": {"path": "reports/v5/conformance/shared_inputs/v3/stimuli/recipes.jsonl", "sha256": recipes_sha},
            "traces": {"path": "reports/v5/conformance/shared_inputs/v3/stimuli/traces.jsonl", "sha256": traces_sha},
            "family_map": {"path": "reports/v5/conformance/shared_inputs/v3/family_map.json", "sha256": fam_sha},
        },
        "protocol_sidecar": {"path": format!("{result_root}/sidecar.json"), "sha256": sidecar_sha},
        "evaluators": Value::Object(evaluators_obj),
        "runtime_attestation": "diagnostic_unaccepted",
        "author_independence": "pending_qualified_adjudication",
        "execution_classification": "blind_full_shared_conformance_observation",
        "normative_contract": normative_contract,
        "frozen_at_utc": "2099-01-01T00:00:00Z",
    });
    let receipt_rel = format!("{result_root}/freeze.json");
    let (_, receipt_sha) = write_readonly(
        root,
        &receipt_rel,
        &(canonical_json_bytes(&receipt).unwrap().into_iter().chain(Some(b'\n')).collect::<Vec<_>>()),
    );

    // Full v4 blind freeze verifies cleanly!
    let token = verify_blind_freeze_receipt(Path::new(&receipt_rel), &receipt_sha, root)
        .expect("v4 freeze receipt must verify");
    assert_eq!(token.proof_scope, "full_shared_conformance");
    assert_eq!(token.case_inventory.len(), 5);
    token.assert_current().expect("assert_current");

    // Tamper 1: hash mismatch
    let bad_hash = "0".repeat(64);
    assert!(
        verify_blind_freeze_receipt(Path::new(&receipt_rel), &bad_hash, root)
            .unwrap_err()
            .to_string()
            .contains("hash differs")
    );

    // Tamper 2: path traversal
    assert!(
        verify_blind_freeze_receipt(
            Path::new(&format!("{result_root}/../../freeze.json")),
            &receipt_sha,
            root
        )
        .unwrap_err()
        .to_string()
        .contains("unsafe artifact path")
    );

    // Tamper 3: duplicate JSON key
    assert!(
        strict_json_loads(br#"{"a":1,"a":2}"#)
            .unwrap_err()
            .to_string()
            .contains("duplicate JSON object key")
    );

    // Tamper 4: banned import & path literal in temp worker copy
    let err = check_python_worker_hygiene(
        "from identitybench.v5 import oracle\n",
        "tests/conformance/v5/fresh_card_only_v2/worker.py",
        "agent_view",
    )
    .unwrap_err();
    assert!(err.to_string().contains("forbidden module import"));

    let err2 = check_python_worker_hygiene(
        "PATH = 'datasets/v5_dev/oracles.jsonl'\n",
        "tests/conformance/v5/fresh_full_spec_v2/worker.py",
        "full_spec",
    )
    .unwrap_err();
    assert!(err2.to_string().contains("forbidden benchmark path literal"));

    let err3 = check_python_worker_hygiene(
        "SPEC = 'SPEC-v5-ADDENDUM-01.md'\n",
        "tests/conformance/v5/fresh_card_only_v2/worker.py",
        "agent_view",
    )
    .unwrap_err();
    assert!(err3.to_string().contains("normative spec"));

    // Tamper 5: mutating bound file makes it writable and assert_current fails
    let _ = make_readonly(root);
    let pub_file = root.join(format!("{result_root}/public-inputs.jsonl"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&pub_file, std::fs::Permissions::from_mode(0o644)).unwrap();
    }
    assert!(
        token
            .assert_current()
            .unwrap_err()
            .to_string()
            .contains("not immutable/read-only")
    );
}
