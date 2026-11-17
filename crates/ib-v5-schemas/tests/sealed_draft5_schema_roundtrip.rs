use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

use ib_v5_schemas::{
    EpisodeV5, SealedEpisodeOracleV5, StrictV5Model, decode_compact_table_v5,
    encode_compact_table_v5,
};

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("workspace root")
        .to_path_buf()
}

#[test]
fn sealed_draft5_all_episodes_and_oracles_roundtrip_byte_for_byte() {
    let root = workspace_root();
    let suites = ["v5_dev", "v5_lite", "v5_core", "v5_full"];
    let mut total_episodes = 0usize;
    let mut total_oracles = 0usize;
    let mut total_compact_tables = 0usize;

    for suite in suites {
        let ep_path = root
            .join("datasets")
            .join("v5_draft5")
            .join(suite)
            .join("episodes.jsonl");
        let ep_file = File::open(&ep_path).unwrap_or_else(|e| panic!("open {ep_path:?}: {e}"));
        let ep_reader = BufReader::new(ep_file);
        for (line_idx, line_res) in ep_reader.lines().enumerate() {
            let line = line_res.expect("read line");
            let ep = EpisodeV5::from_strict_json_str(&line).unwrap_or_else(|e| {
                panic!("{suite}/episodes.jsonl line {}: strict parse/validate failed: {e}", line_idx + 1)
            });
            let val = ep.to_strict_json_value().expect("to_strict_json_value");
            let jcs = ib_core::jcs_dumps(&val).expect("jcs_dumps");
            assert_eq!(
                jcs,
                line.as_bytes(),
                "{suite}/episodes.jsonl line {} JCS mismatch",
                line_idx + 1
            );

            // Round-trip every delivered CompactTableV5 via decode_compact_table_v5 / encode_compact_table_v5
            for slice in ep.phase1_slices.values().chain(ep.phase2_slices.values()) {
                for table in &slice.tables {
                    let rows = decode_compact_table_v5(table).unwrap_or_else(|e| {
                        panic!("{suite} {} decode_compact_table_v5({}) failed: {e}", ep.episode_id, table.table_name)
                    });
                    let reencoded = encode_compact_table_v5(&table.table_name, &rows).unwrap_or_else(|e| {
                        panic!("{suite} {} encode_compact_table_v5({}) failed: {e}", ep.episode_id, table.table_name)
                    });
                    assert_eq!(
                        reencoded.to_jcs_bytes().expect("reencoded jcs"),
                        table.to_jcs_bytes().expect("orig jcs"),
                        "{suite} {} compact table {} roundtrip mismatch",
                        ep.episode_id,
                        table.table_name
                    );
                    total_compact_tables += 1;
                }
            }
            total_episodes += 1;
        }

        let or_path = root
            .join("datasets")
            .join("v5_draft5")
            .join(suite)
            .join("oracles.jsonl");
        let or_file = File::open(&or_path).unwrap_or_else(|e| panic!("open {or_path:?}: {e}"));
        let or_reader = BufReader::new(or_file);
        for (line_idx, line_res) in or_reader.lines().enumerate() {
            let line = line_res.expect("read line");
            let oracle = SealedEpisodeOracleV5::from_strict_json_str(&line).unwrap_or_else(|e| {
                panic!("{suite}/oracles.jsonl line {}: strict parse/validate failed: {e}", line_idx + 1)
            });
            let val = oracle
                .to_strict_json_value()
                .expect("to_strict_json_value");
            let jcs = ib_core::jcs_dumps(&val).expect("jcs_dumps");
            assert_eq!(
                jcs,
                line.as_bytes(),
                "{suite}/oracles.jsonl line {} JCS mismatch",
                line_idx + 1
            );
            total_oracles += 1;
        }
    }

    assert_eq!(total_episodes, 180);
    assert_eq!(total_oracles, 180);
    assert!(total_compact_tables > 0);
}
