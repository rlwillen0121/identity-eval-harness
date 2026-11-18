use std::path::Path;

use ib_v5_baselines::run_shortcut_proofs_v5;
use ib_v5_generator::load_v5_dataset;

#[test]
fn sealed_v5_dev_shortcut_witnesses_match_all_counterfactuals_and_permutations() {
    let dataset_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../datasets/v5_draft5/v5_dev");
    let dataset = load_v5_dataset(&dataset_dir).expect("load sealed v5_dev dataset read-only");
    let seed = dataset
        .seal
        .get("seed")
        .and_then(serde_json::Value::as_u64)
        .expect("v5_dev seal seed");

    let report = run_shortcut_proofs_v5(&dataset.episodes, &dataset.oracles, "v5_dev", seed);
    let report_mapping = report.to_mapping();
    let python_evidence: serde_json::Value = serde_json::from_str(include_str!(
        "fixtures/shortcut_proofs_python_evidence.json"
    ))
    .expect("Python Draft5 G-BASE evidence fixture");

    assert_eq!(report.counterfactual_flip_checks, 12);
    assert_eq!(
        report.counterfactual_flip_matches, 12,
        "{:?}",
        report.errors
    );
    assert_eq!(report.id_row_order_checks, 24);
    assert_eq!(report.id_row_order_matches, 24, "{:?}", report.errors);
    assert!(report.errors.is_empty(), "{:?}", report.errors);
    assert_eq!(
        report_mapping.get("counterfactual_flip_pairs"),
        python_evidence.get("counterfactual_flip_pairs"),
        "counterfactual proof evidence matches Python Draft5 surface"
    );
    assert_eq!(
        report_mapping.get("id_row_order_permutations"),
        python_evidence.get("id_row_order_permutations"),
        "permutation proof evidence matches Python Draft5 surface"
    );
}
