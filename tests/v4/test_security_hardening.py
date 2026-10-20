"""Security-review hardening tests for the v4 runner (P1-A caps, P1-B leaks, P2 fixes)."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from identitybench.providers.base import (
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderCapabilities,
    ToolCall,
    Usage,
)
from identitybench.v4 import (
    OrchestrationTopologyId,
    SkillCondition,
    SplitRingV4,
    SteeringMode,
    V4AgentEnvironment,
    V4ExperimentConfig,
    V4ModelEntryConfig,
    generate_v4_suite_in_memory,
)
from identitybench.v4.baselines import run_v4_baseline_on_episode
from identitybench.v4.report import CHECKER_METRICS_NOTE, render_v4_markdown_report
from identitybench.v4.runner import (
    MAX_ACTIONS_PER_TURN_V4,
    MAX_CHECKER_COMPLETIONS_PER_EPISODE_V4,
    MAX_VALIDATOR_RUNS_PER_EPISODE_V4,
    MAX_WORKER_COMPLETIONS_PER_EPISODE_V4,
    V4RunArtifact,
    _make_baseline_trial_record,
    _write_trajectory_artifact,
    run_v4_agent_episode,
    run_v4_baselines_on_suite,
    run_v4_experiment,
)
from identitybench.v4.schemas import ProgramEpisodeSpecV4, SealedEpisodeOracleV4, WorldStateV4

REPO = Path(__file__).resolve().parents[2]
SEALED_DEV_DATASET = REPO / "datasets" / "v4_dev"

# Field names that exist only on SealedEpisodeOracleV4 / ObligationSpecV4 / conflict and
# hazard specs and must never appear in any text sent to a model.
ORACLE_ONLY_FIELD_NAMES: tuple[str, ...] = (
    "drifted_skill_ids",
    "drifted_skill_pairs",
    "gold_payload",
    "required_preconditions",
    "required_citations",
    "upstream_dependency_obligation_ids",
    "is_discretionary_hygiene",
    "paired_custodian_id",
    "drops_quorum_without_custodian",
    "cae_e_max_seconds",
    "sod_conflict_capability_id",
    "drifted_fragile_payload",
    "revealed_vault_evidence_ids",
    "hazard_slot",
    "seeded_hazard_slot_ids",
    "gold_post_wave_state_hashes",
    "optimal_ieau",
    "mandatory_gate_wave_ids",
    "scheduled_interrupts_by_wave",
    "wave_obligation_ids",
    "obligations_by_id",
)

REMOVED_TOOL_OUTPUT_KEYS: tuple[str, ...] = (
    "remediation_hint",
    "quorum_warning_apps",
    "required_custodian_app_ids",
)


@pytest.fixture(scope="module")
def dev_suite() -> tuple[
    Sequence[ProgramEpisodeSpecV4], Sequence[WorldStateV4], Sequence[SealedEpisodeOracleV4]
]:
    return generate_v4_suite_in_memory(SplitRingV4.v4_dev)


def _run_cfg(
    topology: OrchestrationTopologyId,
    *,
    skill: SkillCondition = SkillCondition.S1_CURATED_GOLD_SKILLS,
    steer: SteeringMode = SteeringMode.FULL_LIVE_STEERED,
):  # type: ignore[no-untyped-def]
    return V4ModelEntryConfig(
        provider="codex_cli",
        model="gpt-6-astra",
        topology_id=topology,
        skill_condition=skill,
        steering_mode=steer,
    ).to_run_config_v4()


class _ScriptedLeadProvider(Provider):
    """Lead provider replaying a fixed per-turn action script, native or JSON mode."""

    kind = "fake_lead"

    def __init__(
        self,
        script: Sequence[Sequence[tuple[str, dict[str, object]]]],
        *,
        native_tools: bool,
        usage: Usage | None = None,
    ) -> None:
        self.model = "fake-lead"
        self._script = [list(turn) for turn in script]
        self._native_tools = native_tools
        self._usage = usage or Usage(input_tokens=100, output_tokens=20)
        self.requests: list[ModelRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=True,
            native_tools=self._native_tools,
            supports_temperature=True,
        )

    def endpoint_identity(self) -> str:
        return "fake_lead:fake-lead"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        turn = sum(1 for m in request.messages if m.role.value == "assistant")
        usage = Usage(
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
            cache_read_tokens=self._usage.cache_read_tokens,
            reasoning_tokens=self._usage.reasoning_tokens,
        )
        if turn >= len(self._script):
            # Unparseable: terminates the loop with PARSE_FAILURE.
            return ModelResponse(text="done", usage=usage, latency_s=0.0)
        actions = self._script[turn]
        if self._native_tools:
            return ModelResponse(
                text=f"turn {turn}",
                tool_calls=[
                    ToolCall(id=f"tc_{turn}_{i}", name=n, arguments=dict(a))
                    for i, (n, a) in enumerate(actions)
                ],
                usage=usage,
                latency_s=0.0,
            )
        payload = {
            "thought": f"turn {turn}",
            "actions": [{"action": n, "arguments_json": json.dumps(a)} for n, a in actions],
        }
        return ModelResponse(text=json.dumps(payload), parsed=payload, usage=usage, latency_s=0.0)


class _FakeSubagentProvider(Provider):
    """Worker/checker provider that records prompts and returns a canned JSON reply."""

    kind = "fake_sub"

    def __init__(self, reply: dict[str, object], *, tokens: int = 10) -> None:
        self.model = "fake-sub"
        self._reply = reply
        self._tokens = tokens
        self.requests: list[ModelRequest] = []

    @property
    def completions(self) -> int:
        return len(self.requests)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=False, native_tools=False, supports_temperature=True
        )

    def endpoint_identity(self) -> str:
        return "fake_sub:fake-sub"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text=json.dumps(self._reply),
            usage=Usage(input_tokens=self._tokens, output_tokens=1),
            latency_s=0.0,
        )


def _request_text(req: ModelRequest) -> str:
    return (req.system or "") + "\n" + "\n".join(m.content for m in req.messages)


# ---------------------------------------------------------------------------
# P1-A: per-turn action cap, in-loop token budget, per-episode worker/checker/validator caps
# ---------------------------------------------------------------------------


def test_default_caps_are_bounded() -> None:
    assert MAX_ACTIONS_PER_TURN_V4 == 8
    assert MAX_WORKER_COMPLETIONS_PER_EPISODE_V4 == 24
    assert MAX_CHECKER_COMPLETIONS_PER_EPISODE_V4 == 24
    assert MAX_VALIDATOR_RUNS_PER_EPISODE_V4 == 32
    cfg = V4ExperimentConfig(
        models=(V4ModelEntryConfig(provider="baseline", model="noop_team"),),
        max_actions_per_turn=3,
        max_worker_completions_per_episode=5,
    )
    assert cfg.max_actions_per_turn == 3
    assert cfg.max_worker_completions_per_episode == 5
    assert cfg.max_checker_completions_per_episode == MAX_CHECKER_COMPLETIONS_PER_EPISODE_V4
    assert cfg.max_validator_runs_per_episode == MAX_VALIDATOR_RUNS_PER_EPISODE_V4


@pytest.mark.parametrize("native_tools", [True, False])
def test_actions_per_turn_cap_returns_error_results_for_surplus(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
    native_tools: bool,
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T0_SINGLE_MONOLITH)
    n_actions = 12
    cap = 8
    lead = _ScriptedLeadProvider(
        [[("inspect_wave_plan", {"wave_id": "W1"})] * n_actions],
        native_tools=native_tools,
    )
    rec = asyncio.run(
        run_v4_agent_episode(
            ep,
            w0,
            oracle,
            lead,
            run_config=run_cfg,
            max_turns=2,
            max_actions_per_turn=cap,
        )
    )
    executed = [t for t in rec.tool_trace if "cap_hit" not in t]
    capped = [t for t in rec.tool_trace if t.get("cap_hit") == "ACTION_CAP_EXCEEDED"]
    assert len(executed) == cap
    assert len(capped) == n_actions - cap
    assert rec.tool_calls_count == cap
    assert rec.resource_cap_hits == {"ACTION_CAP_EXCEEDED": n_actions - cap}
    for t in capped:
        assert t["result"]["error"] == "ACTION_CAP_EXCEEDED"
        assert f"At most {cap} actions" in t["result"]["message"]
        assert "NOT executed" in t["result"]["message"]

    # The model sees an explicit error result for every surplus action on the next turn.
    assert len(lead.requests) == 2
    second_req_text = "\n".join(m.content for m in lead.requests[1].messages)
    assert second_req_text.count("ACTION_CAP_EXCEEDED") == n_actions - cap
    if native_tools:
        tool_msgs = [m for m in lead.requests[1].messages if m.role.value == "tool"]
        assert len(tool_msgs) == n_actions


def test_token_budget_is_rechecked_inside_action_loop(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED)
    worker = _FakeSubagentProvider(
        {"worker_thought": "ok", "recommended_custodian_apps": []}, tokens=50_000
    )
    lead = _ScriptedLeadProvider(
        [[("read_wave_evidence", {"wave_id": "W1"})] * 6],
        native_tools=False,
    )
    rec = asyncio.run(
        run_v4_agent_episode(
            ep,
            w0,
            oracle,
            lead,
            run_config=run_cfg,
            max_turns=4,
            max_tokens=120_000,
            worker_provider=worker,
        )
    )
    # 3 worker completions x 50k tokens >= 120k budget: stop mid-turn, not after 6.
    assert worker.completions == 3
    assert rec.token_truncated is True
    assert rec.termination_reason == "TOKEN_BUDGET_EXCEEDED"
    assert rec.turns_used == 1
    assert rec.tool_calls_count == 3


def test_worker_completion_cap_per_episode(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED)
    worker = _FakeSubagentProvider({"worker_thought": "ok", "recommended_custodian_apps": []})
    env = V4AgentEnvironment(
        ep, w0, oracle, run_cfg, worker_provider=worker, max_worker_completions=2
    )

    async def _drive() -> list[dict[str, object]]:
        return [
            await env.execute_tool_async("read_wave_evidence", {"wave_id": "W1"}) for _ in range(4)
        ]

    outs = asyncio.run(_drive())
    assert worker.completions == 2
    assert all("error" not in o for o in outs[:2])
    assert outs[2]["error"] == "WORKER_COMPLETION_CAP_EXCEEDED"
    assert outs[3]["error"] == "WORKER_COMPLETION_CAP_EXCEEDED"
    assert "cap of 2 live worker completions" in str(outs[2]["message"])
    assert env.cap_hits == {"WORKER_COMPLETION_CAP_EXCEEDED": 2}
    assert sum(1 for t in env.tool_trace if t.get("cap_hit")) == 2
    # Capped calls are not dispatched to the environment at all.
    assert env.tool_calls_count == 2


def test_checker_completion_cap_per_episode(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T3_MAKER_CHECKER_GUARDRAIL)
    checker = _FakeSubagentProvider({"verdict": "PASS"})
    env = V4AgentEnvironment(
        ep, w0, oracle, run_cfg, checker_provider=checker, max_checker_completions=2
    )
    env.read_wave_evidence("W1")

    async def _drive() -> list[dict[str, object]]:
        return [
            await env.execute_tool_async("stage_wave_mutations", {"wave_id": "W1"})
            for _ in range(3)
        ]

    outs = asyncio.run(_drive())
    assert checker.completions == 2
    assert outs[0]["status"] in {"STAGED", "REJECTED_BY_CHECKER"}
    assert outs[2]["error"] == "CHECKER_COMPLETION_CAP_EXCEEDED"
    assert env.cap_hits == {"CHECKER_COMPLETION_CAP_EXCEEDED": 1}
    # Deterministic checker audits are still counted only for executed stages.
    assert env.checker_calls_count == 2


def test_validator_run_cap_per_episode(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T0_SINGLE_MONOLITH)
    env = V4AgentEnvironment(ep, w0, oracle, run_cfg, max_validator_runs=1)
    skill_id = env.list_available_skills()["skills"][0]["skill_id"]
    env.load_skill(skill_id)
    first = env.execute_tool("run_skill_validator", {"skill_id": skill_id})
    second = env.execute_tool("run_skill_validator", {"skill_id": skill_id})
    assert "error" not in first
    assert second["error"] == "VALIDATOR_RUN_CAP_EXCEEDED"
    assert env.validator_runs == 1
    assert env.cap_hits == {"VALIDATOR_RUN_CAP_EXCEEDED": 1}


# ---------------------------------------------------------------------------
# P1-B: no answer leakage to lead/worker, blind live checker + agreement tracking
# ---------------------------------------------------------------------------


def test_tool_outputs_do_not_leak_remediation_or_quorum_answer(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    for idx in range(len(episodes)):
        ep, w0, oracle = episodes[idx], worlds[idx], oracles[idx]
        for topo in (
            OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED,
            OrchestrationTopologyId.T3_MAKER_CHECKER_GUARDRAIL,
        ):
            env = V4AgentEnvironment(
                ep, w0, oracle, _run_cfg(topo, skill=SkillCondition.S3_STALE_DRIFTED_SKILLS)
            )
            outputs: list[dict[str, object]] = [env.inspect_wave_plan("W1")]
            # Stage before reading evidence -> deterministic checker rejects (T3).
            outputs.append(env.stage_wave_mutations("W1"))
            outputs.append(env.read_wave_evidence("W1"))
            outputs.append(env.stage_wave_mutations("W1"))
            outputs.append(env.request_team_steer("W1"))
            outputs.append(env.commit_and_advance_wave())
            for out in outputs:
                for key in REMOVED_TOOL_OUTPUT_KEYS:
                    assert key not in out, (ep.episode_id, topo, key)
                text = json.dumps(out, sort_keys=True)
                for name in ORACLE_ONLY_FIELD_NAMES:
                    assert name not in text, (ep.episode_id, topo, name)


def test_worker_prompt_has_real_evidence_and_no_oracle_fields(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED)
    worker = _FakeSubagentProvider({"worker_thought": "ok", "recommended_custodian_apps": []})
    env = V4AgentEnvironment(ep, w0, oracle, run_cfg, worker_provider=worker)
    res = asyncio.run(env.execute_tool_async("read_wave_evidence", {"wave_id": "W1"}))
    assert worker.completions == 1
    req = worker.requests[0]
    prompt_text = _request_text(req)
    for name in ORACLE_ONLY_FIELD_NAMES:
        assert name not in prompt_text, name
    for key in REMOVED_TOOL_OUTPUT_KEYS:
        assert key not in prompt_text, key
    body = json.loads(req.messages[0].content)
    # The worker is fed what read_wave_evidence actually returns, not phantom keys.
    assert "source_records" not in body
    assert body["read_entity_ids"]
    assert body["fetched_evidence_ids"] == res["fetched_evidence_ids"]
    assert body["citations"] == res["citations"]
    assert body["has_source_authority_conflict"] == res["has_source_authority_conflict"]
    assert set(body["read_entity_ids"]) == {c["target_entity_id"] for c in res["citations"]}
    assert all("drops_quorum_without_custodian" not in ob for ob in body["obligations"])
    assert res["worker_thought"] == "ok"


def test_checker_prompt_is_blind_and_agreement_is_recorded(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T3_MAKER_CHECKER_GUARDRAIL)
    checker = _FakeSubagentProvider({"verdict": "PASS", "rationale": "looks fine"})
    env = V4AgentEnvironment(ep, w0, oracle, run_cfg, checker_provider=checker)

    # Staging before any evidence read -> deterministic verdict is REJECT_VIOLATION,
    # while the fake live checker always says PASS -> disagreement.
    res_reject = asyncio.run(env.execute_tool_async("stage_wave_mutations", {"wave_id": "W1"}))
    assert res_reject["checker_verdict"] == "REJECT_VIOLATION"
    req = checker.requests[0]
    user_text = "\n".join(m.content for m in req.messages)
    assert "REJECT_VIOLATION" not in user_text
    body = json.loads(req.messages[0].content)
    for key in ("checker_verdict", "violated_rules", "missing_evidence"):
        assert key not in body
    for mev in res_reject["checker_missing_evidence_ids"]:
        assert mev not in user_text
    for name in ORACLE_ONLY_FIELD_NAMES:
        assert name not in _request_text(req), name
    assert env.checker_log[-1]["live_checker_verdict"] == "PASS"
    assert env.checker_log[-1]["checker_agreement"] is False
    assert env.checker_agreement is False

    # After reading evidence the deterministic verdict is PASS -> agreement.
    env.read_wave_evidence("W1")
    res_pass = asyncio.run(env.execute_tool_async("stage_wave_mutations", {"wave_id": "W1"}))
    assert res_pass["checker_verdict"] == "PASS"
    assert env.checker_log[-1]["checker_agreement"] is True
    assert env.checker_live_verdicts_count == 2
    assert env.checker_live_disagreements_count == 1
    assert env.checker_agreement is False


def test_episode_record_carries_checker_agreement_and_report_labels(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T3_MAKER_CHECKER_GUARDRAIL)
    checker = _FakeSubagentProvider({"verdict": "PASS"})
    lead = _ScriptedLeadProvider(
        [
            [
                ("read_wave_evidence", {"wave_id": "W1"}),
                ("stage_wave_mutations", {"wave_id": "W1"}),
            ]
        ],
        native_tools=True,
    )
    rec = asyncio.run(
        run_v4_agent_episode(
            ep, w0, oracle, lead, run_config=run_cfg, max_turns=2, checker_provider=checker
        )
    )
    assert rec.checker_live_verdicts_count == 1
    assert rec.checker_agreement is not None
    assert rec.checker_agreement == (rec.checker_live_disagreements_count == 0)

    # Records lacking the new fields (older artifacts) still load with defaults.
    legacy = rec.model_dump(mode="json")
    for key in (
        "checker_agreement",
        "checker_live_verdicts_count",
        "checker_live_disagreements_count",
        "resource_cap_hits",
    ):
        legacy.pop(key)
    from identitybench.v4.runner import EpisodeTrialRecordV4

    reloaded = EpisodeTrialRecordV4.model_validate(legacy)
    assert reloaded.checker_agreement is None
    assert reloaded.resource_cap_hits == {}

    from identitybench.v4.scoring import summarize_suite_v4

    summary = summarize_suite_v4(ep.split_ring, run_cfg, (rec.scorecard,))
    artifact = V4RunArtifact(
        suite=ep.split_ring,
        episode_count=1,
        wave_count=len(ep.waves),
        summaries={"fake": summary},
        per_episode={"fake": (rec.scorecard,)},
        records_by_model={"fake": (rec,)},
        execution_valid_rates={"fake": 1.0},
    )
    md = render_v4_markdown_report(artifact, n_boot=10)
    assert CHECKER_METRICS_NOTE in md
    assert "Checker Audits (deterministic harness)" in md
    assert "Checker Caught (`GAR_agent`, deterministic harness)" in md
    assert "Live Checker Agreement" in md
    expected_cell = "1.000 (1/1)" if rec.checker_live_disagreements_count == 0 else "0.000 (0/1)"
    assert expected_cell in md


# ---------------------------------------------------------------------------
# P2: trajectory filename sanitization and cumulative token accounting
# ---------------------------------------------------------------------------


def test_trajectory_filename_sanitizes_episode_id(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
    tmp_path: Path,
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = V4ModelEntryConfig(provider="baseline", model="noop_team").to_run_config_v4()
    sc = run_v4_baseline_on_episode("noop_team", ep, w0, oracle, run_config=run_cfg)
    rec = _make_baseline_trial_record(
        "noop_team",
        "noop_team",
        ep,
        sc,
        run_cfg,
        oracle=oracle,
        trial_index=0,
        config_fingerprint="fp",
        episode_sha256="sha",
    ).model_copy(update={"episode_id": "../../evil/ep id", "model_id": "m/../x"})
    traj_dir = tmp_path / "traj"
    _write_trajectory_artifact(traj_dir, rec)
    files = sorted(tmp_path.rglob("*.json"))
    assert len(files) == 1
    assert files[0].parent == traj_dir
    assert files[0].name == "m_.._x__.._.._evil_ep_id__t0.json"
    assert not (tmp_path / "evil").exists()


def test_token_budget_counts_cumulative_input_for_stateless_cli(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T0_SINGLE_MONOLITH)
    # Stateless CLI style: cache_read_tokens == 0, each turn re-sends the whole context.
    lead = _ScriptedLeadProvider(
        [[("inspect_wave_plan", {"wave_id": "W1"})]] * 10,
        native_tools=False,
        usage=Usage(input_tokens=1_000, output_tokens=10, cache_read_tokens=0),
    )
    rec = asyncio.run(
        run_v4_agent_episode(
            ep, w0, oracle, lead, run_config=run_cfg, max_turns=10, max_tokens=2_500
        )
    )
    # Cumulative: 1010 -> 2020 -> 3030 >= 2500 after the 3rd turn. The previous per-turn
    # averaging (1000 + 10*n) would never have truncated within 10 turns.
    assert rec.turns_used == 3
    assert rec.token_truncated is True
    assert rec.termination_reason == "TOKEN_BUDGET_EXCEEDED"
    assert rec.input_tokens == 3_000


# ---------------------------------------------------------------------------
# P2: INTERRUPT_FIRED must not leak the oracle-derived affected-obligation answer
# ---------------------------------------------------------------------------


def _drive_to_w2_interrupt(env: V4AgentEnvironment) -> dict[str, object]:
    env.read_wave_evidence("W1")
    env.stage_wave_mutations("W1", grant_emergency_custodian_for_apps=["all"])
    env.request_team_steer("W1")
    assert env.commit_and_advance_wave()["status"] == "ADVANCED_TO_NEXT_WAVE"
    env.read_wave_evidence("W2")
    env.stage_wave_mutations("W2", grant_emergency_custodian_for_apps=["all"])
    env.request_team_steer("W2")
    return env.commit_and_advance_wave()


def test_interrupt_fired_commit_has_no_oracle_derived_hint(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    for idx in range(len(episodes)):
        ep, w0, oracle = episodes[idx], worlds[idx], oracles[idx]
        env = V4AgentEnvironment(
            ep, w0, oracle, _run_cfg(OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED)
        )
        out = _drive_to_w2_interrupt(env)
        assert out["status"] == "INTERRUPT_FIRED", ep.episode_id
        # Agent-visible interrupt facts stay ...
        assert out["fired_interrupts"]
        assert "excluded_scope_ids" in out
        assert "frozen_application_ids" in out
        # ... but the answer derived from oracle-only `is_discretionary_hygiene` is gone.
        assert "affected_obligation_ids" not in out, ep.episode_id
        assert "next_step_hint" not in out, ep.episode_id
        text = json.dumps(out, sort_keys=True)
        for name in ORACLE_ONLY_FIELD_NAMES:
            assert name not in text, (ep.episode_id, name)
        for oid in oracle.wave_obligation_ids.get("W2", ()):
            assert oid not in text, (ep.episode_id, oid)


# ---------------------------------------------------------------------------
# P2: worker cap check is gated on the same condition that triggers a live completion
# ---------------------------------------------------------------------------


def test_worker_cap_check_only_fires_when_a_live_completion_would_run(
    dev_suite: tuple[
        Sequence[ProgramEpisodeSpecV4],
        Sequence[WorldStateV4],
        Sequence[SealedEpisodeOracleV4],
    ],
) -> None:
    episodes, worlds, oracles = dev_suite
    ep, w0, oracle = episodes[0], worlds[0], oracles[0]
    run_cfg = _run_cfg(OrchestrationTopologyId.T2_HETEROGENEOUS_TIERED)
    worker = _FakeSubagentProvider({"worker_thought": "ok", "recommended_custodian_apps": []})
    env = V4AgentEnvironment(
        ep, w0, oracle, run_cfg, worker_provider=worker, max_worker_completions=1
    )

    async def _drive() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        first = await env.execute_tool_async("read_wave_evidence", {"wave_id": "W1"})
        # No scoped entity -> no recon subagent spawned -> no live completion would run,
        # so the (already reached) cap must not reject this call.
        no_spawn = await env.execute_tool_async(
            "read_wave_evidence", {"wave_id": "W1", "entity_ids": ["ent_does_not_exist"]}
        )
        capped = await env.execute_tool_async("read_wave_evidence", {"wave_id": "W1"})
        return first, no_spawn, capped

    first, no_spawn, capped = asyncio.run(_drive())
    assert worker.completions == 1
    assert "error" not in first
    assert first["worker_thought"] == "ok"
    assert "error" not in no_spawn
    assert no_spawn["status"] == "OK"
    # The earlier (stale) subagent_log entry is not re-completed either.
    assert "worker_thought" not in no_spawn
    assert len(env.subagent_log) == 1
    assert capped["error"] == "WORKER_COMPLETION_CAP_EXCEEDED"
    assert env.cap_hits == {"WORKER_COMPLETION_CAP_EXCEEDED": 1}
    assert env.tool_calls_count == 2


# ---------------------------------------------------------------------------
# P2: the v4 runner requires SEAL.json and honours an optional dataset_sha256 pin
# ---------------------------------------------------------------------------


class _NeverCalledProvider(Provider):
    """Lead provider that must never be reached when seal/pin verification fails."""

    kind = "never"

    def __init__(self) -> None:
        self.model = "never"
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=True, native_tools=True, supports_temperature=True
        )

    def endpoint_identity(self) -> str:
        return "never:never"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise AssertionError("model called despite seal/pin verification failure")


def _unsealed_copy_of_dev_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "v4_dev"
    target.mkdir()
    for name in ("episodes.jsonl", "worlds.jsonl", "oracles.jsonl"):
        shutil.copyfile(SEALED_DEV_DATASET / name, target / name)
    assert not (target / "SEAL.json").exists()
    return target


def _baseline_only_config(**overrides: object) -> V4ExperimentConfig:
    return V4ExperimentConfig.model_validate(
        {
            "name": "seal-test",
            "suite": "v4_dev",
            "max_episodes_per_archetype": 1,
            "n_boot": 10,
            "models": [{"provider": "baseline", "model": "noop_team"}],
            **overrides,
        }
    )


def test_v4_experiment_config_dataset_sha256_pin_must_be_64_hex() -> None:
    for bad in ("abc", "0" * 63, "0" * 65, "g" * 64, "A" * 64):
        with pytest.raises(ValidationError):
            _baseline_only_config(dataset_sha256=bad)
    assert _baseline_only_config().dataset_sha256 is None
    assert _baseline_only_config(dataset_sha256="0" * 64).dataset_sha256 == "0" * 64


def test_run_refuses_unsealed_dataset_unless_explicitly_opted_out(tmp_path: Path) -> None:
    unsealed = _unsealed_copy_of_dev_dataset(tmp_path)
    cfg = _baseline_only_config(dataset_dir=str(unsealed))
    with pytest.raises(FileNotFoundError, match=r"SEAL\.json not found"):
        run_v4_experiment(cfg, tmp_path / "out", repo_root=REPO)
    assert not (tmp_path / "out").exists()
    with pytest.raises(FileNotFoundError, match=r"SEAL\.json not found"):
        run_v4_baselines_on_suite(unsealed, n_boot=10)

    # The explicit opt-out is the only unsealed path, and the manifest records it.
    run_v4_experiment(cfg, tmp_path / "out_unsealed", repo_root=REPO, require_seal=False)
    manifest = json.loads((tmp_path / "out_unsealed" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sealed"] is False
    assert manifest["dataset_sha256_pin"] is None


def test_wrong_dataset_pin_fails_before_any_provider_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_provider(spec: object) -> Provider:
        raise AssertionError(f"provider created despite pin mismatch: {spec}")

    monkeypatch.setattr("identitybench.v4.runner.make_provider", _no_provider)
    never = _NeverCalledProvider()
    cfg = _baseline_only_config(
        dataset_sha256="0" * 64,
        models=[
            {"provider": "baseline", "model": "noop_team"},
            {"provider": "codex_cli", "model": "gpt-6-astra", "label": "live-overridden"},
            {"provider": "codex_cli", "model": "gpt-6-astra", "label": "live-factory"},
        ],
    )
    with pytest.raises(ValueError, match="pin mismatch"):
        run_v4_experiment(
            cfg, tmp_path / "out", repo_root=REPO, provider_overrides={"live-overridden": never}
        )
    assert never.calls == 0
    assert not (tmp_path / "out").exists()

    # A pin can never be satisfied by an unsealed directory, even with require_seal=False.
    unsealed = _unsealed_copy_of_dev_dataset(tmp_path)
    cfg_unsealed = cfg.model_copy(update={"dataset_dir": str(unsealed)})
    with pytest.raises(FileNotFoundError, match="does not exist"):
        run_v4_experiment(
            cfg_unsealed,
            tmp_path / "out2",
            repo_root=REPO,
            require_seal=False,
            provider_overrides={"live-overridden": never},
        )
    assert never.calls == 0


def test_matching_dataset_pin_on_sealed_dev_dataset_is_verified_and_recorded(
    tmp_path: Path,
) -> None:
    seal = json.loads((SEALED_DEV_DATASET / "SEAL.json").read_text(encoding="utf-8"))
    pin = seal["episodes_sha256"]
    cfg = _baseline_only_config(dataset_sha256=pin)
    artifact = run_v4_experiment(cfg, tmp_path / "out", repo_root=REPO)
    assert artifact.episode_count == 6
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sealed"] is True
    assert manifest["dataset_sha256"] == pin
    assert manifest["dataset_sha256_pin"] == pin
    assert manifest["worlds_sha256"] == seal["worlds_sha256"]
    assert manifest["oracles_sha256"] == seal["oracles_sha256"]
