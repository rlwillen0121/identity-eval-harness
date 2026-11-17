//! Every sealed Draft 5 dataset line is JCS output from Python; parsing it and
//! re-encoding with `ib_core::jcs_dumps` must reproduce the sealed bytes and digests.

use std::path::PathBuf;

use ib_core::hashing::sha256_hex;
use serde_json::Value;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn draft5_datasets_roundtrip_byte_exact() {
    let base = repo_root().join("datasets/v5_draft5");
    for suite in ["v5_dev", "v5_lite", "v5_core", "v5_full"] {
        let dir = base.join(suite);
        let seal: Value =
            serde_json::from_slice(&std::fs::read(dir.join("SEAL.json")).expect("seal")).expect("seal json");
        for (file, key) in [("episodes.jsonl", "episodes_sha256"), ("oracles.jsonl", "oracles_sha256")] {
            let bytes = std::fs::read(dir.join(file)).expect("dataset file");
            assert_eq!(
                sha256_hex(&bytes),
                seal[key].as_str().expect("digest"),
                "{suite}/{file} sealed digest"
            );
            let mut rebuilt = Vec::with_capacity(bytes.len());
            for line in bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
                let v: Value = serde_json::from_slice(line).expect("jsonl line");
                rebuilt.extend(ib_core::jcs_dumps(&v).expect("jcs"));
                rebuilt.push(b'\n');
            }
            assert!(rebuilt == bytes, "{suite}/{file} JCS round-trip differs");
        }
    }
}
