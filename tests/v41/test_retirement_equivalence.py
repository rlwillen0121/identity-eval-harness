"""Pinned pre-retirement serialization, statistics and action-parser behavior.

All committed parser inputs are synthetic. No model calls or real transcripts are used.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
from pathlib import Path

import pytest

from identitybench.providers.base import ModelResponse
from identitybench.providers.factory import make_provider
from identitybench.v41.jcs import jcs_dumps, jcs_sha256
from identitybench.v41.runner import (
    NO_THOUGHT_TURN_PROTOCOL,
    V41ModelEntryConfig,
    _uses_no_thought_schema,
    compute_v41_config_fingerprint,
    load_v41_experiment_config,
)
from identitybench.v41.stats import family_cluster_bca_bootstrap_ci

# Keep the parser's name: existing study code uses it at the new module path.
from identitybench.v41.turns import _extract_v4_turn_tool_calls

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads(Path(__file__).with_name("retirement_vectors.json").read_text())


@pytest.mark.parametrize("vector", VECTORS["fingerprints"])
def test_pinned_synthetic_fingerprints(vector):
    entry = V41ModelEntryConfig(**vector["entry"])
    assert (
        compute_v41_config_fingerprint(
            entry,
            entry.to_run_config(),
            seed=41,
            dataset_sha256="0" * 64,
            endpoint_identity="offline-equivalence-endpoint",
            turn_protocol=vector["protocol"],
        )
        == vector["fingerprint"]
    )


def test_committed_experiment_fingerprints():
    """Use the runner's config loading and protocol selection, without calling a provider."""
    found = []
    for path in sorted((ROOT / "experiments").glob("v41-*.yaml")):
        config = load_v41_experiment_config(path)
        data_dir = Path(config.dataset_dir or f"datasets/{config.suite.value}")
        seal = json.loads((ROOT / data_dir / "SEAL.json").read_text())
        for entry in config.models:
            protocol = None
            if entry.provider != "baseline":
                provider = make_provider(entry.to_model_spec())
                if _uses_no_thought_schema(provider):
                    protocol = NO_THOUGHT_TURN_PROTOCOL
            run_config = entry.to_run_config(
                max_turns=config.max_turns,
                max_calls_per_turn=config.max_calls_per_turn,
                max_total_tokens=config.max_total_tokens,
            )
            found.append(
                {
                    "config": path.name,
                    "label": entry.display_id,
                    "fingerprint": compute_v41_config_fingerprint(
                        entry,
                        run_config,
                        seed=config.seed,
                        dataset_sha256=seal["episodes_sha256"],
                        endpoint_identity="offline-equivalence-endpoint",
                        turn_protocol=protocol,
                    ),
                }
            )
    assert found == VECTORS["experiments"]


@pytest.mark.parametrize("vector", VECTORS["parser"])
def test_synthetic_action_turns(vector):
    calls = _extract_v4_turn_tool_calls(ModelResponse(text=vector["text"]), 7)
    assert [dataclasses.asdict(call) for call in calls] == vector["calls"]


def test_varied_jcs_and_bootstrap_vectors():
    rng = random.Random(410041)
    payloads = [
        {
            "index": i,
            "unicode": ["é", "e\u0301", "漢字", "😀", "\u0000\n\t", "\U00010000", "\ue000"],
            "float": rng.uniform(-1e8, 1e8),
            "nested": [
                None,
                True,
                False,
                {"z": -0.0, "a": i / 1000, "small": 1e-20, "large": 1e21},
            ],
            "integer": rng.randrange(-(2**53) + 1, 2**53),
        }
        for i in range(400)
    ]
    results = [{"dump": jcs_dumps(p).decode("utf-8"), "sha256": jcs_sha256(p)} for p in payloads]
    serialized = json.dumps(results, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
    assert hashlib.sha256(serialized.encode()).hexdigest() == VECTORS["jcs_vectors_sha256"]
    inputs = [
        {},
        {"one": [0.2]},
        {"a": [0, 1], "b": [0.2, 0.7], "c": [0.3, 0.9], "d": [1, 1]},
        {"empty": []},
        {f"f{i}": [rng.random() for _ in range(i % 5 + 1)] for i in range(24)},
    ]
    assert [
        list(family_cluster_bca_bootstrap_ci(v, n_resamples=2000, seed=41)) for v in inputs
    ] == VECTORS["bootstrap"]


def test_jcs_unicode_keys_zero_and_invalid_values():
    assert jcs_dumps({"\ue000": 1, "\U00010000": -0.0}) == '{"𐀀":0,"":1}'.encode()
    for number in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            jcs_dumps({"bad": number})
    with pytest.raises(TypeError):
        jcs_dumps({1: "non-string key"})
