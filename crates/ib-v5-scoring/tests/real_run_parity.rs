use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

use ib_v5_schemas::{EpisodeV5, SealedEpisodeOracleV5, StrictV5Model};
use ib_v5_scoring::{
    BuildEpisodeAttemptOptionsV5, EpisodeExecutionV5, ScoreExecutionOptionsV5,
    build_episode_attempt_v5, episode_score_to_mapping, episode_scoring_input_to_mapping,
    score_execution_raw_v5, score_execution_v5,
};
use serde_json::Value;

const PILOT_RUN_DIRS: [&str; 2] = [
    "/Users/ryanwillen/orca/projects/identity-eval-harness/runs/v5_draft5_pilot_v3_gemini_hydrogen_t0",
    "/Users/ryanwillen/orca/projects/identity-eval-harness/runs/v5_draft5_pilot_v3_gemini_hydrogen_t5",
];

fn load_dataset_maps(
    dataset_dir: &Path,
) -> (
    BTreeMap<String, EpisodeV5>,
    BTreeMap<String, SealedEpisodeOracleV5>,
) {
    let ep_file = File::open(dataset_dir.join("episodes.jsonl")).expect("open dataset episodes");
    let mut episodes = BTreeMap::new();
    for line in BufReader::new(ep_file).lines() {
        let line = line.expect("read episode line");
        if line.trim().is_empty() {
            continue;
        }
        let ep = EpisodeV5::from_strict_json_str(&line).expect("parse EpisodeV5");
        episodes.insert(ep.episode_id.clone(), ep);
    }

    let or_file = File::open(dataset_dir.join("oracles.jsonl")).expect("open dataset oracles");
    let mut oracles = BTreeMap::new();
    for line in BufReader::new(or_file).lines() {
        let line = line.expect("read oracle line");
        if line.trim().is_empty() {
            continue;
        }
        let orc = SealedEpisodeOracleV5::from_strict_json_str(&line)
            .expect("parse SealedEpisodeOracleV5");
        oracles.insert(orc.episode_id.clone(), orc);
    }

    (episodes, oracles)
}

#[test]
fn real_pilot_run_artifacts_parity() {
    let mut checked_runs = 0usize;
    let mut checked_episodes = 0usize;

    for run_dir_str in PILOT_RUN_DIRS {
        let run_dir = Path::new(run_dir_str);
        if !run_dir.exists() {
            continue;
        }
        checked_runs += 1;
        let manifest: Value = serde_json::from_str(
            &std::fs::read_to_string(run_dir.join("manifest.json")).expect("read manifest.json"),
        )
        .expect("parse manifest.json");
        let run_config_sha256 = manifest["run_config_sha256"]
            .as_str()
            .expect("run_config_sha256 in manifest.json");

        let (episodes, oracles) = load_dataset_maps(&run_dir.join("dataset"));

        let exec_file =
            File::open(run_dir.join("episodes.jsonl")).expect("open run episodes.jsonl");
        for (idx, line) in BufReader::new(exec_file).lines().enumerate() {
            let line = line.expect("read episodes.jsonl line");
            if line.trim().is_empty() {
                continue;
            }
            let raw: Value = serde_json::from_str(&line).expect("parse raw json row");
            let execution = EpisodeExecutionV5::from_json_str(&line).unwrap_or_else(|e| {
                panic!("{run_dir_str} row {idx} EpisodeExecutionV5 decode failed: {e}")
            });

            let episode = &episodes[&execution.episode_id];
            let oracle = &oracles[&execution.episode_id];

            let (raw_input, raw_score) =
                score_execution_raw_v5(&raw, episode, oracle, ScoreExecutionOptionsV5::default())
                    .unwrap_or_else(|e| {
                        panic!(
                            "{run_dir_str} {} score_execution_raw_v5 failed: {e}",
                            execution.episode_id
                        )
                    });

            let (typed_input, typed_score) = score_execution_v5(
                &execution,
                episode,
                oracle,
                ScoreExecutionOptionsV5::default(),
            )
            .unwrap_or_else(|e| {
                panic!(
                    "{run_dir_str} {} score_execution_v5 failed: {e}",
                    execution.episode_id
                )
            });

            assert_eq!(
                typed_input, raw_input,
                "{run_dir_str} {} typed vs raw scoring_input mismatch",
                execution.episode_id
            );
            assert_eq!(
                typed_score, raw_score,
                "{run_dir_str} {} typed vs raw score mismatch",
                execution.episode_id
            );

            let actual_score_json = episode_score_to_mapping(&raw_score);
            assert_eq!(
                actual_score_json, raw["score"],
                "{run_dir_str} {} score mismatch against recorded JSON",
                execution.episode_id
            );

            let actual_input_json = episode_scoring_input_to_mapping(&raw_input);
            assert_eq!(
                actual_input_json, raw["scoring_input"],
                "{run_dir_str} {} scoring_input mismatch against recorded JSON",
                execution.episode_id
            );

            let built_attempt = build_episode_attempt_v5(
                &raw_score,
                run_config_sha256,
                BuildEpisodeAttemptOptionsV5 {
                    attempt_id: execution.attempt.as_ref().map(|a| a.attempt_id.as_str()),
                    completed_assistant_generations: execution.completed_assistant_generations,
                    tool_call_count: execution.tool_calls_executed,
                    retry_request_count: execution.retry_request_count,
                    elapsed_wall_time_ms: execution.elapsed_wall_time_ms,
                    terminal_outcome_elapsed_ms: execution.terminal_outcome_elapsed_ms,
                    active_elapsed_ms: execution.active_elapsed_ms,
                    terminal_reservation_ordinal: execution.terminal_reservation_ordinal.clone(),
                    terminal_cause: execution.terminal_cause.clone(),
                    context_limit_cause: execution.context_limit_cause,
                    provider_requests: execution.provider_requests.clone(),
                },
            )
            .expect("build_episode_attempt_v5");
            assert_eq!(
                Some(&built_attempt),
                execution.attempt.as_ref(),
                "{run_dir_str} {} attempt mismatch against recorded JSON",
                execution.episode_id
            );

            checked_episodes += 1;
        }
    }

    if checked_runs > 0 {
        assert_eq!(checked_episodes, 24, "expected 12 + 12 pilot episodes");
    }
}
