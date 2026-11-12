"""Offline replay verification of sealed v4.1 run artifacts.

Replay establishes consistency with the recorded actions, not authenticity of provider output.
No provider is constructed or contacted by this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from identitybench.v41.env import MAX_ARGUMENT_BYTES, MAX_ARGUMENT_DEPTH
from identitybench.v41.generator import load_v41_dataset
from identitybench.v41.report import verify_v41_report_artifact
from identitybench.v41.runner import (
    EpisodeTrialRecordV41,
    V41RunArtifact,
    _make_env,
    _score_env,
    compute_v41_episode_sha256,
    load_v41_run_artifact,
)
from identitybench.v41.schemas import (
    EpisodeScorecardV41,
    EpisodeSpecV41,
    RunConfigV41,
    SealedEpisodeOracleV41,
)


@dataclass(frozen=True)
class RunConfigCandidate:
    config: RunConfigV41
    sources: tuple[str, ...]
    authoritative: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources),
            "run_config": self.config.model_dump(mode="json"),
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True)
class RecordVerification:
    status: Literal["verified", "recovered", "mismatch", "unverifiable"]
    reason: str = ""
    matched_candidate: RunConfigCandidate | None = None
    scorecard: EpisodeScorecardV41 | None = None


def _replay_record(
    record: EpisodeTrialRecordV41,
    episode: EpisodeSpecV41,
    oracle: SealedEpisodeOracleV41,
    run_config: RunConfigV41 | None = None,
    *,
    oracles_sha256: str | None = None,
) -> RecordVerification:
    """Reconstruct state and scores without using the stored scorecard."""
    if record.episode_sha256 != compute_v41_episode_sha256(episode):
        return RecordVerification("mismatch", "episode_sha256 disagrees with sealed episode")
    if (record.episode_id, record.family_id) != (episode.episode_id, episode.family_id):
        return RecordVerification("mismatch", "record identity disagrees with sealed episode")
    if oracle.episode_id != episode.episode_id:
        return RecordVerification("mismatch", "oracle identity disagrees with sealed episode")
    if (
        record.oracles_sha256 is not None
        and oracles_sha256 is not None
        and record.oracles_sha256 != oracles_sha256
    ):
        return RecordVerification("mismatch", "oracles_sha256 disagrees with sealed oracle bytes")
    if record.termination_reason == "INFRASTRUCTURE_ERROR":
        return RecordVerification("unverifiable", "infrastructure failure has no replayable score")
    if run_config is None:
        return RecordVerification("unverifiable", "execution configuration missing from manifest")
    if not record.tool_trace and record.tool_calls_count:
        return RecordVerification(
            "unverifiable", "legacy record has tool count but no action trace"
        )
    env = _make_env(episode, oracle, run_config)
    for index, item in enumerate(record.tool_trace):
        if not {"turn", "name", "arguments", "result_status"} <= item.keys():
            return RecordVerification("unverifiable", f"trace[{index}] lacks replay fields")
        turn, name, args = item["turn"], item["name"], item["arguments"]
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            return RecordVerification("mismatch", f"trace[{index}] invalid turn index")
        if not isinstance(name, str) or not isinstance(args, dict):
            return RecordVerification("unverifiable", f"trace[{index}] unsupported action format")
        if item["result_status"] == "CALL_CAP_EXCEEDED":
            continue  # The original loop did not execute surplus calls.
        env.current_turn_index = turn
        if "_rejected_arguments" in args:
            if (
                args.get("_rejected_arguments") not in ("depth_limit", "size_limit", "invalid_json")
                or args.get("max_depth") != MAX_ARGUMENT_DEPTH
                or args.get("max_bytes") != MAX_ARGUMENT_BYTES
                or set(args) != {"_rejected_arguments", "max_depth", "max_bytes"}
                or item["result_status"] != "INVALID_ARGUMENTS"
            ):
                return RecordVerification(
                    "mismatch", f"trace[{index}] invalid rejection placeholder"
                )
            # Bounded arguments were deliberately discarded. This rejection changes only
            # the call counter, never world state, checker state or staged mutations.
            env.tool_calls_count += 1
            continue
        try:
            result = env.execute_tool(name, args)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            return RecordVerification(
                "mismatch", f"trace[{index}] replay failed: {type(exc).__name__}"
            )
        if result.get("status") != item["result_status"]:
            return RecordVerification(
                "mismatch", f"trace[{index}] result_status disagrees with replay"
            )
    if env.tool_calls_count != record.tool_calls_count:
        return RecordVerification("mismatch", "tool_calls_count disagrees with replay")
    computed = _score_env(episode, oracle, env, run_config)
    return RecordVerification("verified", scorecard=computed)


def verify_record(
    record: EpisodeTrialRecordV41,
    episode: EpisodeSpecV41,
    oracle: SealedEpisodeOracleV41,
    run_config: RunConfigV41 | None = None,
    *,
    oracles_sha256: str | None = None,
) -> RecordVerification:
    """Require exact scorecard equality under a known execution configuration."""
    result = _replay_record(record, episode, oracle, run_config, oracles_sha256=oracles_sha256)
    computed = result.scorecard
    if computed is None:
        return result
    if computed != record.scorecard:
        fields = [
            key
            for key in type(computed).model_fields
            if getattr(computed, key) != getattr(record.scorecard, key)
        ]
        return RecordVerification(
            "mismatch", "scorecard disagrees with replay: " + ", ".join(fields)
        )
    return result


class _ReplayTrialRecord(EpisodeTrialRecordV41):
    """Validate every record field except the legacy scorecard, which replay must prove."""

    scorecard: Any


def _current_fields_agree(stored: Any, computed: Any) -> bool:
    if isinstance(computed, dict):
        return isinstance(stored, dict) and all(
            key in stored and _current_fields_agree(stored[key], value)
            for key, value in computed.items()
        )
    if isinstance(computed, (list, tuple)):
        return (
            isinstance(stored, (list, tuple))
            and len(stored) == len(computed)
            and all(_current_fields_agree(a, b) for a, b in zip(stored, computed, strict=True))
        )
    if type(computed) in (int, float):
        return type(stored) in (int, float) and bool(stored == computed)
    return type(stored) is type(computed) and bool(stored == computed)


def verify_with_candidates(
    record: EpisodeTrialRecordV41 | dict[str, Any],
    episode: EpisodeSpecV41,
    oracle: SealedEpisodeOracleV41,
    candidates: list[RunConfigCandidate],
    *,
    oracles_sha256: str | None = None,
) -> tuple[RecordVerification, EpisodeTrialRecordV41 | None]:
    """Prove a candidate by replay; failed guesses are not authoritative mismatches.

    A legacy scorecard is replaced only after replay agrees with every current schema
    field. Extra historical fields are explicitly reported as recovered, never stripped
    during ordinary validation.
    """
    legacy = False
    if isinstance(record, dict):
        try:
            parsed = EpisodeTrialRecordV41.model_validate(record)
        except ValidationError:
            try:
                parsed = _ReplayTrialRecord.model_validate(record)
            except ValidationError:
                return RecordVerification("mismatch", "invalid non-scorecard record fields"), None
            legacy = True
        stored = record.get("scorecard") if legacy else parsed.scorecard.model_dump(mode="json")
    else:
        parsed = record
        stored = record.scorecard.model_dump(mode="json")
    if not isinstance(stored, dict):
        return RecordVerification("mismatch", "scorecard must be an object"), None
    preflight = _replay_record(parsed, episode, oracle, oracles_sha256=oracles_sha256)
    if preflight.status == "mismatch" or parsed.termination_reason == "INFRASTRUCTURE_ERROR":
        return preflight, None
    applicable = [
        candidate
        for candidate in candidates
        if candidate.config.wave_execution_mode.value == stored.get("wave_execution_mode")
    ]
    failures = []
    for candidate in applicable:
        result = _replay_record(
            parsed, episode, oracle, candidate.config, oracles_sha256=oracles_sha256
        )
        computed = result.scorecard
        if computed is None:
            failures.append(result.reason)
            continue
        if not _current_fields_agree(stored, computed.model_dump(mode="json")):
            failures.append("scorecard disagrees with replay")
            continue
        restored = EpisodeTrialRecordV41.model_validate(
            {
                **parsed.model_dump(mode="json"),
                "scorecard": computed.model_dump(mode="json"),
            }
        )
        return RecordVerification(
            "recovered" if legacy else "verified",
            "legacy record re-scored by replay" if legacy else "",
            matched_candidate=candidate,
            scorecard=computed,
        ), restored
    reason = (
        failures[0]
        if failures
        else "no execution-config candidate matches scorecard wave_execution_mode"
    )
    return RecordVerification(
        "mismatch" if any(c.authoritative for c in candidates) else "unverifiable",
        reason,
    ), None


def _entry_run_config(entry: dict[str, Any], experiment: dict[str, Any]) -> RunConfigV41:
    payload: dict[str, Any] = {
        key: entry[key]
        for key in ("topology_id", "steering_mode", "wave_execution_mode")
        if key in entry
    }
    payload.update(
        max_turns=entry.get("max_turns") or experiment.get("max_turns", 12),
        max_calls_per_turn=experiment.get("max_calls_per_turn", 4),
        max_total_tokens=entry.get("max_total_tokens") or experiment.get("max_total_tokens"),
    )
    return RunConfigV41.model_validate(payload)


def _display_id(entry: dict[str, Any]) -> str:
    return str(
        entry.get("label")
        or (
            entry["model"]
            if entry.get("provider") == "baseline"
            else f"{entry['provider']}:{entry['model']}"
        )
    )


def load_run_configs(run_dir: Path) -> dict[str, RunConfigV41]:
    """Read only execution settings, without loading providers or endpoint credentials."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    configs = {}
    for entry in manifest.get("models", []):
        if not isinstance(entry, dict):
            continue
        model_id = _display_id(entry)
        if model_id in configs:
            raise ValueError(f"duplicate manifest model id: {model_id}")
        configs[model_id] = _entry_run_config(entry, manifest)
    return configs


def load_run_config_candidates(
    run_dir: Path,
    *,
    experiments_dir: Path | None = None,
) -> dict[str, list[RunConfigCandidate]]:
    """Collect distinct execution settings with manifest authority and YAML provenance.

    Only run settings are read from historical YAML; providers, credentials, and binaries
    are neither validated nor constructed. These settings never alter config fingerprints.
    """
    grouped: dict[str, dict[str, RunConfigCandidate]] = {}

    def add(model_id: str, cfg: RunConfigV41, source: str, authoritative: bool = False) -> None:
        key = json.dumps(cfg.model_dump(mode="json"), sort_keys=True)
        group = grouped.setdefault(model_id, {})
        prior = group.get(key)
        group[key] = RunConfigCandidate(
            cfg,
            (*prior.sources, source) if prior else (source,),
            authoritative or (prior.authoritative if prior else False),
        )

    for model_id, cfg in load_run_configs(run_dir).items():
        add(model_id, cfg, f"{run_dir.name}/manifest.json", True)
    experiments = experiments_dir or Path(__file__).resolve().parents[3] / "experiments"
    paths = [
        (path, f"{run_dir.name}/configs/{path.name}")
        for path in sorted((run_dir / "configs").glob("*.yaml"))
    ]
    paths.extend(
        (path, f"experiments/{path.name}") for path in sorted(experiments.glob("v41-*.yaml"))
    )
    for path, source in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: experiment must be a mapping")
        for entry in raw.get("models", []):
            if isinstance(entry, dict):
                add(_display_id(entry), _entry_run_config(entry, raw), source)
    return {model_id: list(group.values()) for model_id, group in grouped.items()}


def load_run_records(
    run_dir: Path,
    artifact: V41RunArtifact,
    *,
    deduplicate: bool = True,
    legacy_records: list[tuple[str, dict[str, Any], str]] | None = None,
) -> list[tuple[str, EpisodeTrialRecordV41]]:
    """Include durable JSONL records omitted by later summary rewrites.

    Identical copies in the summary and journal count once; conflicting versions remain
    separate and must each verify. Rejection archives are not accepted trial records.
    """
    records: list[tuple[str, EpisodeTrialRecordV41]] = []
    seen: set[bytes] = set()

    def append(source: str, record: EpisodeTrialRecordV41) -> None:
        digest = hashlib.sha256(
            json.dumps(record.model_dump(mode="json"), sort_keys=True).encode()
        ).digest()
        if not deduplicate or digest not in seen:
            seen.add(digest)
            records.append((source, record))

    for index, record in enumerate(artifact.records):
        append(f"v41_summary.json:records[{index}]", record)
    for path in sorted((run_dir / "records").glob("*.jsonl")):
        if path.name.endswith(".rejected.jsonl"):
            continue
        for index, line in enumerate(path.read_bytes().splitlines(), start=1):
            if not line.strip():
                continue
            source = f"records/{path.name}:{index}"
            try:
                record = EpisodeTrialRecordV41.model_validate_json(line)
            except ValidationError as exc:
                errors = exc.errors(include_input=False)
                # Keep the whole legacy scorecard for a field-by-field replay check.
                if legacy_records is not None and all(
                    error["loc"] and error["loc"][0] == "scorecard" for error in errors
                ):
                    legacy_records.append(
                        (source, json.loads(line), "scorecard requires replay recovery")
                    )
                    continue
                raise ValueError(
                    f"{run_dir}/{source}: invalid trial record schema ({errors[0]['type']})"
                ) from None
            append(source, record)
    return records


def verify_run(
    run_dir: Path,
    *,
    dataset_root: Path | None = None,
) -> tuple[V41RunArtifact, dict[str, Any]]:
    """Validate the pinned dataset and aggregates, then replay every artifact record."""
    artifact = load_v41_run_artifact(run_dir)
    root = dataset_root or Path(__file__).resolve().parents[3] / "datasets"
    seal, episodes, oracles = load_v41_dataset(root / artifact.suite.value)
    if artifact.seal is None or (
        artifact.seal.suite,
        artifact.seal.episodes_sha256,
        artifact.seal.oracles_sha256,
    ) != (seal.suite, seal.episodes_sha256, seal.oracles_sha256):
        raise ValueError("artifact seal digests disagree with the pinned dataset")
    artifact = verify_v41_report_artifact(artifact, check_dataset=False)
    pairs = {ep.episode_id: (ep, oracle) for ep, oracle in zip(episodes, oracles, strict=True)}
    configs = load_run_config_candidates(run_dir, experiments_dir=root.parent / "experiments")
    models: dict[str, Any] = {}
    details = []
    matches = []
    legacy_records: list[tuple[str, dict[str, Any], str]] = []
    records: list[tuple[str, EpisodeTrialRecordV41 | dict[str, Any]]] = list(
        load_run_records(run_dir, artifact, legacy_records=legacy_records)
    )
    records.extend((source, raw) for source, raw, _ in legacy_records)
    for source, record in records:
        raw = record if isinstance(record, dict) else record.model_dump(mode="json")
        pair = pairs.get(raw["episode_id"])
        result, _ = (
            verify_with_candidates(
                record,
                *pair,
                configs.get(raw["model_id"], []),
                oracles_sha256=seal.oracles_sha256,
            )
            if pair
            else (
                RecordVerification("mismatch", "episode not present in sealed dataset"),
                None,
            )
        )
        counts = models.setdefault(
            raw["model_id"], {"verified": 0, "recovered": 0, "mismatch": 0, "unverifiable": 0}
        )
        counts[result.status] += 1
        detail = dict(
            model_id=raw["model_id"],
            episode_id=raw["episode_id"],
            source=source,
            trial_index=raw["trial_index"],
            status=result.status,
            reason=result.reason,
        )
        if result.matched_candidate is not None:
            detail["matched_candidate"] = result.matched_candidate.as_json()
            matches.append(detail)
        if result.status != "verified":
            details.append(detail)
    return artifact, {"models": models, "details": details, "matches": matches}
