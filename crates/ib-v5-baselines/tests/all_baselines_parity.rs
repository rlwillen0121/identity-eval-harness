//! All seven policies, raw trajectory fields, and independently recomputed scores.
//! Expected hashes come from the Python dev-only reference generator beside the fixture.
use std::path::Path;

use ib_core::{hashing::sha256_hex, jcs_dumps};
use ib_v5_baselines::{default_run_config_for_baseline_v5, run_v5_baseline_with_oracle};
use ib_v5_generator::load_v5_dataset;
use ib_v5_scoring::{
    BuildEpisodeAttemptOptionsV5, build_episode_attempt_v5, episode_score_to_mapping,
    episode_scoring_input_to_mapping, score_execution_v5,
};
use serde_json::{Map, Value};

fn stable(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .filter(|(key, _)| {
                    !matches!(
                        key.as_str(),
                        "active_elapsed_ms"
                            | "elapsed_wall_time_ms"
                            | "terminal_outcome_elapsed_ms"
                    )
                })
                .map(|(key, child)| (key.clone(), stable(child)))
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(stable).collect()),
        other => other.clone(),
    }
}

#[tokio::test]
async fn all_dev_baseline_trajectories_and_scores_match_python() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let dataset = load_v5_dataset(&root.join("datasets/v5_draft5/v5_dev")).unwrap();
    let expected: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/baseline_projection_python.json")).unwrap();
    assert_eq!(expected.len(), 84);
    let mut differences = Vec::new();
    for case in expected {
        let baseline = case["baseline"].as_str().unwrap();
        let index = dataset
            .episodes
            .iter()
            .position(|ep| ep.episode_id == case["episode_id"])
            .unwrap();
        let episode = &dataset.episodes[index];
        let oracle = &dataset.oracles[index];
        let mut config = default_run_config_for_baseline_v5(baseline).unwrap();
        config.repeat_index = 1;
        config.run_config_sha256 = Some(case["config_sha"].as_str().unwrap().to_owned());
        let execution = run_v5_baseline_with_oracle(baseline, episode, oracle, config, None)
            .await
            .unwrap();
        let (input, score) =
            score_execution_v5(&execution, episode, oracle, Default::default()).unwrap();
        let mut actual = execution.to_json_value().unwrap();
        actual["scoring_input"] = episode_scoring_input_to_mapping(&input);
        actual["score"] = episode_score_to_mapping(&score);
        actual["attempt"] = serde_json::to_value(
            build_episode_attempt_v5(
                &score,
                case["config_sha"].as_str().unwrap(),
                BuildEpisodeAttemptOptionsV5 {
                    completed_assistant_generations: execution.completed_assistant_generations,
                    tool_call_count: execution.tool_calls_executed,
                    retry_request_count: execution.retry_request_count,
                    elapsed_wall_time_ms: execution.elapsed_wall_time_ms,
                    terminal_outcome_elapsed_ms: execution.terminal_outcome_elapsed_ms,
                    active_elapsed_ms: execution.active_elapsed_ms,
                    terminal_reservation_ordinal: execution.terminal_reservation_ordinal.clone(),
                    terminal_cause: execution.terminal_cause.clone(),
                    context_limit_cause: execution.context_limit_cause.clone(),
                    provider_requests: execution.provider_requests.clone(),
                    ..Default::default()
                },
            )
            .unwrap(),
        )
        .unwrap();
        let fields: Map<String, Value> = actual
            .as_object()
            .unwrap()
            .iter()
            .filter(|(key, _)| {
                !matches!(
                    key.as_str(),
                    "active_elapsed_ms" | "elapsed_wall_time_ms" | "terminal_outcome_elapsed_ms"
                )
            })
            .map(|(key, value)| {
                (
                    key.clone(),
                    Value::String(sha256_hex(jcs_dumps(&stable(value)).unwrap())),
                )
            })
            .collect();
        if Value::Object(fields.clone()) != case["fields"] {
            for key in case["fields"]
                .as_object()
                .unwrap()
                .keys()
                .chain(fields.keys())
            {
                if fields.get(key) != case["fields"].get(key) {
                    let difference = format!("{baseline}/{}: {key}", episode.episode_id);
                    if !differences.contains(&difference) {
                        differences.push(difference);
                    }
                }
            }
            if let Some(path) = std::env::var_os("IB_PARITY_DEBUG_DIR") {
                let dir = Path::new(&path).join(baseline);
                std::fs::create_dir_all(&dir).unwrap();
                std::fs::write(
                    dir.join(format!("{}.json", episode.episode_id)),
                    serde_json::to_vec_pretty(&actual).unwrap(),
                )
                .unwrap();
            }
        }
    }
    assert!(
        differences.is_empty(),
        "Python field mismatches:\n{}",
        differences.join("\n")
    );
}
