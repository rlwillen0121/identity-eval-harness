use std::path::PathBuf;

use ib_core::hashing::sha256_hex;
use ib_v5_generator::{
    DEFAULT_V5_DRAFT5_SEED, V5Suite, build_v5_dataset, build_v5_seal, load_v5_dataset,
};

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("workspace root")
        .to_path_buf()
}

fn first_diff_context(got: &[u8], want: &[u8]) -> String {
    let min_len = got.len().min(want.len());
    let first_byte = (0..min_len).find(|&i| got[i] != want[i]);
    match first_byte {
        Some(idx) => {
            let start = idx.saturating_sub(80);
            let end_g = (idx + 120).min(got.len());
            let end_w = (idx + 120).min(want.len());
            let line = got[..idx].iter().filter(|&&b| b == b'\n').count() + 1;
            format!(
                "diff at byte {idx} (line {line}); got len={}, want len={}\n  got : {:?}\n  want: {:?}",
                got.len(),
                want.len(),
                String::from_utf8_lossy(&got[start..end_g]),
                String::from_utf8_lossy(&want[start..end_w]),
            )
        }
        None => format!("length differs: got {}, want {}", got.len(), want.len()),
    }
}

fn assert_suite_byte_exact(
    suite: V5Suite,
    seed: u64,
    expected_ep_sha: &str,
    expected_or_sha: &str,
    expected_seal_sha: &str,
) {
    let dir = workspace_root()
        .join("datasets")
        .join("v5_draft5")
        .join(suite.as_str());
    let loaded = load_v5_dataset(&dir).unwrap_or_else(|e| panic!("load {suite}: {e}"));
    assert_eq!(sha256_hex(&loaded.episodes_bytes), expected_ep_sha);
    assert_eq!(sha256_hex(&loaded.oracles_bytes), expected_or_sha);
    assert_eq!(sha256_hex(&loaded.seal_bytes), expected_seal_sha);

    let built = build_v5_dataset(suite, seed).unwrap_or_else(|e| panic!("build {suite}: {e}"));
    let (_seal, seal_bytes) = build_v5_seal(&built).unwrap_or_else(|e| panic!("seal {suite}: {e}"));

    if built.episodes_bytes != loaded.episodes_bytes {
        panic!(
            "{suite} episodes.jsonl byte mismatch:\n{}",
            first_diff_context(&built.episodes_bytes, &loaded.episodes_bytes)
        );
    }
    if built.oracles_bytes != loaded.oracles_bytes {
        panic!(
            "{suite} oracles.jsonl byte mismatch:\n{}",
            first_diff_context(&built.oracles_bytes, &loaded.oracles_bytes)
        );
    }
    if seal_bytes != loaded.seal_bytes {
        panic!(
            "{suite} SEAL.json byte mismatch:\n{}",
            first_diff_context(&seal_bytes, &loaded.seal_bytes)
        );
    }
    assert_eq!(sha256_hex(&built.episodes_bytes), expected_ep_sha);
    assert_eq!(sha256_hex(&built.oracles_bytes), expected_or_sha);
    assert_eq!(sha256_hex(&seal_bytes), expected_seal_sha);
}

#[test]
fn sealed_draft5_v5_dev_byte_exact_parity() {
    assert_suite_byte_exact(
        V5Suite::V5Dev,
        DEFAULT_V5_DRAFT5_SEED,
        "43948e7ff609ac5d535c18a7e6491343bbb0755db70b27d6c2977ef786567ef6",
        "3af9847db452bdc46964e5a87c1d41c5f5715a15e7c43ccbe942cd758b8c6047",
        "677c34659c8edf7f4dcacf928a748ead3abc018d1eb5bbbed8d94aaf2c84ca20",
    );
}

#[test]
fn sealed_draft5_v5_lite_byte_exact_parity() {
    assert_suite_byte_exact(
        V5Suite::V5Lite,
        20_261_115,
        "ba875a1d0f025641c791267572af9734dc873ddc1f2ab164c09557187223c39e",
        "d6871df9b842ecc4e601df32a2eae90259f1ed3d014e19f731fba7d1cf71e564",
        "52368030dd0bf3d0e2df91ba15185e5133b9d51047d005a1b5272c5f3b45ef25",
    );
}

#[test]
fn sealed_draft5_v5_core_byte_exact_parity() {
    assert_suite_byte_exact(
        V5Suite::V5Core,
        20_261_115,
        "13cb63c7f01762f44ea27415c8744a2cabdf52e62e79ec9016f9ae6cd0d1450a",
        "7a13573768df161981a1b7152a4a4897ee7a4d725dac3150eccccc56bf2c6060",
        "85cf18f1924b3813c5f6681045cf7cbb24d83bcef8f67bbcefbb07eb49843e52",
    );
}

#[test]
fn sealed_draft5_v5_full_byte_exact_parity() {
    assert_suite_byte_exact(
        V5Suite::V5Full,
        20_261_115,
        "7141f952cce80e8c6378406b36352cb96b2cb2bba25349e7690ca55b4814edd2",
        "1c8186f9ea2edda6e2ac1cad637afdf5bbc5f199f8758c50516d458ea4290b8f",
        "896b3a3116afa1c322504f1efaef526864918aa14baad6e1c952c077f3578e51",
    );
}
