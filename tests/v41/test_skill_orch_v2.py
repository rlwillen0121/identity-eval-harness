# ruff: noqa: E501
"""Offline deterministic unit & integration tests for IdentityBench v4.1 Study v2 (`reports/v41-skill-orch-v2/`).

Covers all Phase 1 requirements and all Phase 2 (5-Lane Blind Review) P0/P1/P2/P3 remediations:
1. `allowlist.py`:
   - Rejects metered APIs (`OPENROUTER_BASE_URL`, `CPA_OPENAI_BASE_URL`, `OPENCODE_GO_BASE_URL`,
     `OPENAI_BASE_URL`, `anthropic_api`, custom `headers`, `provider_routing`, non-instance fake
     CLI classes when `allow_offline_mock=False`) with `PermissionError` before Turn 1.
   - `validate_local_llm_base_url`: enforces `http`/`https`, zero userinfo, loopback/RFC1918/ULA/
     Tailscale/local DNS only; blocks link-local (`169.254.0.0/16`, `fe80::/10`), cloud metadata
     hosts, HTTP 3xx redirects (`_NoRedirectHandler`), oversized `/models` bodies, and public URLs.
   - `redact_sensitive_text`: chains core `_subprocess.redact`, scrubs URLs, JSON-escaped/encoded
     URLs, hostnames, quoted secret keys, Bearer/Basic/3-part JWT/OAuth/GitHub PAT tokens, and env
     secrets without leaking endpoints or keys.
2. `skills_v2.py` (Zero Oracle Leakage — zero imports of `oracle.py`, `generator.py`, `optimization.py`):
   - `S2(1..3)` compactness (`< 60%` of `S1`), wave isolation, and `SPEC-v4.1.md` clause alignment
     (`P_bind(a)`, `TERMINATED -> DISABLE_ACCOUNT` binding-only citation, `MOVER-03` `from_dept_id`,
     `K_grp(p)` `granted_role_ids`, rotator-decisive preconditions, `APPROVE_JIT` TTL, half-open
     cooldown `[t, t + approved_ttl_s + cooldown)`).
   - `lint_stage_payload` (`MUTATION_ADAPTER`, zero leakage of expected OCC tuples or computed
     `OPT-01` integers, unconditional `read_evidence_ids` check even when empty, tuple acceptance,
     empty-citations & target self-precondition warnings).
   - `verify_opt_candidate_feasibility` across all 5 optimization kinds on `datasets/v41_dev` plus
     `tier_weights` partial-override merging and `selected_ids` string/non-sequence guards.
3. `run_study_v2.py`:
   - Pure multi-turn `V41AgentEnvironment` loop across all controls (`C_*`) and all teams
     (`T01`..`T14`) with zero Python tool/target/graph/citation synthesis (`<= 4` calls/env turn),
     seamless `native_tools=True` (`qwen-local`) <-> `native_tools=False` CLI JSON seat handoffs,
     and no import-time monkey-patching of `render.render_cli_system_prompt`.
   - Exact completion counting (`1` completion = `+1`, never collapsing parallel batches) under
     `G_TURN_12_STRICT` (`<= 12`) and `FANOUT_CAPPED_24` (`<= 24` completions, `<= 12` env turns).
   - Collision-proof JCS-SHA256 `StudyCacheStore` (`temp=0.0` only), `UnrecoveredProviderError`
     sibling/partial token retention, `INCOMPLETE_WAVE_COMMIT` vs `MAX_TURNS_EXCEEDED` vs
     `PARSE_FAILURE`, `S0` baseline leak guards (`grok-4.7` newcomer, zero `$ > 0` / provider
     mismatch rejection), and suite-locked `manifest.json`.
4. `build_report_v2.py`:
   - `SEAL.json` + `manifest.json` + `episode_sha256` verification, dynamic `actual_billed_usd`
     fail-closed check, zero survivorship bias on invalid/incomplete cells, official `scorecard.u_exec`
     `committed_unsafe_rate` vs `stage34_provenance_miss_rate`, ratio-of-means family-cluster BCa CIs
     (`point == g_srr` under unequal cluster sizes), paired family-cluster BCa delta CIs,
     `pricing.yaml` `cache_write_1h` & `long_context` + fail-closed unknown models, attributed vs
     executed study totals, XML-valid light/dark SVGs, and strict HTML/Markdown/control-char escaping.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import http.client
import io
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY_V2_DIR = REPO_ROOT / "reports" / "v41-skill-orch-v2"
if str(STUDY_V2_DIR) not in sys.path:
    sys.path.insert(0, str(STUDY_V2_DIR))

import allowlist  # noqa: E402
import build_report_v2  # noqa: E402
import run_study_v2  # noqa: E402
import skills_v2  # noqa: E402

from identitybench.providers.base import (  # noqa: E402
    GenerationParams,
    Message,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderCapabilities,
    Role,
    ToolCall,
    Usage,
)
from identitybench.v41 import render  # noqa: E402
from identitybench.v41.checker import make_semantic_checker  # noqa: E402
from identitybench.v41.env import V41AgentEnvironment  # noqa: E402
from identitybench.v41.mechanics import narrowed_targets  # noqa: E402
from identitybench.v41.schemas import (  # noqa: E402
    EpisodeSpecV41,
    OrchestrationTopologyId,
    RunConfigV41,
    SealedEpisodeOracleV41,
    SplitRingV41,
    SteeringMode,
    WaveExecutionMode,
    WaveTelemetrySnapshotV4_1,
)

DEV_DATASET_DIR = REPO_ROOT / "datasets" / "v41_dev"


# ---------------------------------------------------------------------------
# Helpers: Sealed Dataset Loader (Zero `oracle.py` Import!) & Mock Providers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dev_pairs() -> list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]]:
    episodes = [
        EpisodeSpecV41.model_validate_json(line)
        for line in (DEV_DATASET_DIR / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    oracles = {
        ora.episode_id: ora
        for ora in (
            SealedEpisodeOracleV41.model_validate_json(line)
            for line in (DEV_DATASET_DIR / "oracles.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    return [(ep, oracles[ep.episode_id]) for ep in episodes]


def _gold_stage_dict(oracle: SealedEpisodeOracleV41, wave_index: int) -> dict[str, Any]:
    ow = oracle.waves[wave_index - 1]
    return {
        "wave_index": wave_index,
        "mutations": [m.model_dump(mode="json") for m in ow.mutations],
        "citations": [c.model_dump(mode="json") for c in ow.citations],
        "resource_preconditions": [p.model_dump(mode="json") for p in ow.preconditions],
    }


def _step_env_to_live_snapshots(
    ep: EpisodeSpecV41, ora: SealedEpisodeOracleV41
) -> dict[int, tuple[WaveTelemetrySnapshotV4_1, set[str], tuple[tuple[str, str], ...]]]:
    cfg = RunConfigV41(
        topology_id=OrchestrationTopologyId.T3_MAKER_CHECKER_GUARDRAIL,
        steering_mode=SteeringMode.UNSTEERED,
        wave_execution_mode=WaveExecutionMode.COUPLED,
        max_turns=12,
        max_calls_per_turn=4,
    )
    env = V41AgentEnvironment(ep, cfg, semantic_checker=make_semantic_checker())
    out: dict[int, tuple[WaveTelemetrySnapshotV4_1, set[str], tuple[tuple[str, str], ...]]] = {}
    for k in (1, 2, 3):
        env.execute_tool("read_wave_evidence", {"wave_index": k})
        env.execute_tool("poll_interrupts", {"wave_index": k})
        snap = env.snapshots[k]
        read_eids = set(env.read_ledger[k][env.actor])
        nar = narrowed_targets(k, ep.scheduled_interrupts)
        out[k] = (snap, read_eids, nar)
        g_args = _gold_stage_dict(ora, k)
        s_res = env.execute_tool("stage_wave_mutations", g_args)
        assert s_res["status"] == "STAGED"
        digest = s_res["stage_digest"]
        cosigns: list[str] = []
        if k == 2:
            for m in ora.waves[1].mutations:
                if (
                    m.mutation_kind == "UAR_CAMPAIGN_MUTATION"
                    and m.emergency_custodian_account_id is not None
                ):
                    cr = env.execute_tool(
                        "request_cosign",
                        {
                            "wave_index": 2,
                            "stage_digest": digest,
                            "campaign_id": m.target_entity_id,
                            "custodian_account_id": m.emergency_custodian_account_id,
                        },
                    )
                    cosigns.append(cr["cosign_token"])
        c_res = env.execute_tool(
            "commit_wave_mutations",
            {
                "wave_index": k,
                "stage_digest": digest,
                "cosign_tokens": cosigns,
                "steer_approval_token": None,
            },
        )
        assert c_res["status"] in ("COMMITTED_AND_ADVANCED", "EPISODE_COMPLETED")
    return out


def _corrupt_stage_semantic(stage_args: dict[str, Any], wave_index: int) -> dict[str, Any]:
    """Create a structurally valid stage payload that fails Stage 3/4/5 in `run_wave_checker`."""
    bad = copy.deepcopy(stage_args)
    if len(bad["citations"]) >= 2:
        # Drop one citation while keeping non-empty citations -> passes S3 lint, fails Stage 4!
        bad["citations"] = bad["citations"][:-1]
    elif len(bad["resource_preconditions"]) >= 2:
        bad["resource_preconditions"] = bad["resource_preconditions"][:-1]
    elif bad["mutations"]:
        # Flip an action/decision semantically while preserving schema validity
        m0 = bad["mutations"][0]
        if m0["mutation_kind"] == "ACCOUNT_RECON_MUTATION" and m0["bound_person_id"] is not None:
            m0["account_action"] = (
                "DISABLE_ACCOUNT" if m0["account_action"] == "KEEP_ACTIVE" else "KEEP_ACTIVE"
            )
            if m0["account_action"] == "DISABLE_ACCOUNT":
                m0["retained_grant_ids"] = []
    bad["wave_index"] = wave_index
    return bad


def _parse_tool_batches(messages: Sequence[Message]) -> list[list[dict[str, Any]]]:
    """Extract chronological tool-result batches across both `Role.tool` and CLI JSON transcripts."""
    batches: list[list[dict[str, Any]]] = []
    cur_native: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.tool:
            cur_native.append(json.loads(m.content))
            continue
        if cur_native:
            batches.append(cur_native)
            cur_native = []
        content = m.content or ""
        if m.role == Role.user and "## Tool Execution Results\n" in content:
            parts = content.split("## Tool Execution Results\n")
            for seg in parts[1:]:
                first_line = seg.splitlines()[0].strip()
                if first_line.startswith("["):
                    obs_list = json.loads(first_line)
                    batches.append([dict(obs["result"]) for obs in obs_list])
    if cur_native:
        batches.append(cur_native)
    return batches


HookCallable = Callable[
    [str, float | None, int, str, dict[str, Any]],
    ModelResponse | dict[str, Any] | None,
]


class ScriptedStudyMockProvider(Provider):
    """Deterministic offline `Provider` (`kind='mock'`) driving the pure multi-turn `V41AgentEnvironment`
    loop across both `native_tools=True` (`qwen-local`) and `native_tools=False` (CLI JSON) seats,
    plus `advisory_text_only` specialist/swarm memo completions.
    """

    def __init__(
        self,
        model_key: str,
        temperature: float | None,
        oracles_by_ep: dict[str, SealedEpisodeOracleV41],
        call_counts: dict[str, int],
        hook: HookCallable | None = None,
        *,
        native_tools: bool | None = None,
        emitted_call_ids: set[str] | None = None,
    ) -> None:
        self._model_key = model_key
        self._temp = temperature
        self._oracles = oracles_by_ep
        self._counts = call_counts
        self._hook = hook
        # Default mirrors production: local OpenAI-compat uses native tools; subscription CLIs use JSON!
        self._native_tools = (model_key == "qwen-local") if native_tools is None else native_tools
        self._emitted_ids = emitted_call_ids

    @property
    def kind(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return self._model_key

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_tools=self._native_tools,
            native_structured_output=False,
            supports_temperature=True,
        )

    def endpoint_identity(self) -> str:
        mode = "native" if self._native_tools else "cli"
        return f"mock://{self._model_key}:{mode}"

    def _detect_episode_id(self, full_text: str) -> str:
        for eid in self._oracles:
            if eid in full_text:
                return eid
        return next(iter(self._oracles))

    def _wrap_calls(
        self,
        calls: Sequence[ToolCall],
        usage: Usage,
        lat: float,
    ) -> ModelResponse:
        if self._emitted_ids is not None:
            for c in calls:
                self._emitted_ids.add(c.id)
        if self._native_tools:
            return ModelResponse(text="", tool_calls=tuple(calls), usage=usage, latency_s=lat)
        payload: dict[str, Any] = {
            "actions": [
                {"action": c.name, "arguments_json": json.dumps(c.arguments, sort_keys=True)}
                for c in calls
            ]
        }
        if "fable" not in self._model_key:
            payload["thought"] = "next_step"
        return ModelResponse(
            text=json.dumps(payload, sort_keys=True),
            tool_calls=(),
            usage=usage,
            latency_s=lat,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._counts[self._model_key] += 1
        call_num = self._counts[self._model_key]
        joined = "\n".join(m.content or "" for m in request.messages)
        last_msg = request.messages[-1].content or ""
        eid = self._detect_episode_id(joined)
        ora = self._oracles[eid]
        usage = Usage(input_tokens=400, output_tokens=120, reasoning_tokens=30)
        lat = 0.50 if "qwen" in self._model_key else 0.25

        # 1. Advisory text-only mode (T07 Fable OPT-01 specialist & T02/T06 parallel subagents)
        if not request.tools and request.output_schema is None:
            memo_dict: dict[str, Any] = {
                "mode": "advisory",
                "model": self._model_key,
                "episode_id": eid,
                "status": "verified",
            }
            if self._hook is not None:
                adv_override = self._hook(
                    self._model_key,
                    self._temp,
                    call_num,
                    f"ADVISORY::{joined}",
                    memo_dict,
                )
                if isinstance(adv_override, ModelResponse):
                    return adv_override
                if isinstance(adv_override, dict):
                    memo_dict = adv_override
            return ModelResponse(
                text=json.dumps(memo_dict, sort_keys=True),
                tool_calls=(),
                usage=usage,
                latency_s=lat,
            )

        # 2. Parse multi-turn tool batches across native & CLI transcript encodings
        is_t3 = "T3_MAKER_CHECKER_GUARDRAIL" in joined or "## Maker-Checker (`T3`)" in joined
        batches = _parse_tool_batches(request.messages)
        committed_waves = {
            int(r["wave_index"])
            for b in batches
            for r in b
            if r.get("status") in ("COMMITTED_AND_ADVANCED", "EPISODE_COMPLETED")
        }
        cur_w = 1 if 1 not in committed_waves else (2 if 2 not in committed_waves else 3)
        read_waves = {
            int(r["wave_index"])
            for b in batches
            for r in b
            if "snapshot" in r and isinstance(r.get("wave_index"), int)
        }
        checker_called_waves = {
            int(r["wave_index"])
            for b in batches
            for r in b
            if r.get("status") in ("PASS", "REJECT_VIOLATION", "DIAGNOSTIC_LIMIT_REACHED")
        }
        cosign_tokens_by_digest: dict[tuple[int, str], list[str]] = defaultdict(list)
        for b in batches:
            for r in b:
                if r.get("status") == "CO_SIGNED" and r.get("cosign_token"):
                    key = (int(r["wave_index"]), str(r["stage_digest"]))
                    tok = str(r["cosign_token"])
                    if tok not in cosign_tokens_by_digest[key]:
                        cosign_tokens_by_digest[key].append(tok)

        # Check active staged buffer state on cur_w
        active_digest: str | None = None
        digest_failed = False
        for b in batches:
            for r in b:
                if r.get("wave_index") != cur_w:
                    continue
                st = str(r.get("status") or "")
                if st == "STAGED" and "stage_digest" in r:
                    active_digest = str(r["stage_digest"])
                    digest_failed = False
                elif st in ("REJECT_VIOLATION", "REJECTED_STRUCTURAL") or st.startswith(
                    "COMMIT_BLOCKED_"
                ):
                    digest_failed = True

        is_lint_retry = "S3 Pre-Stage Linter Rejected" in last_msg
        is_explicit_stage = (
            "Independent Jury Member — Wave" in last_msg
            or "Compute-Scaled Parallel Repair — Wave" in last_msg
            or "Boss-Less Jury Synthesis — Wave" in last_msg
        )

        # Decide whether this turn is a staging turn or a read/check/cosign/commit turn
        will_stage = (
            is_lint_retry
            or is_explicit_stage
            or (cur_w in read_waves and (active_digest is None or digest_failed))
        )

        stage_draft = _gold_stage_dict(ora, cur_w) if will_stage else {}
        phase_tag = (
            f"LINT_FIX_W{cur_w}"
            if is_lint_retry
            else (
                f"REPAIR_STAGE_W{cur_w}"
                if (will_stage and (digest_failed or cur_w in checker_called_waves))
                else (f"STAGE_W{cur_w}" if will_stage else f"STEP_W{cur_w}")
            )
        )
        if self._hook is not None:
            override = self._hook(
                self._model_key,
                self._temp,
                call_num,
                f"{phase_tag}::{joined}",
                stage_draft,
            )
            if isinstance(override, ModelResponse):
                return override
            if will_stage and isinstance(override, dict):
                stage_draft = override

        # Turn Type A: Read + Poll Wave `cur_w`
        if cur_w not in read_waves and not is_lint_retry and not is_explicit_stage:
            return self._wrap_calls(
                (
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_r{cur_w}",
                        name="read_wave_evidence",
                        arguments={"wave_index": cur_w},
                    ),
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_p{cur_w}",
                        name="poll_interrupts",
                        arguments={"wave_index": cur_w},
                    ),
                ),
                usage,
                lat,
            )

        # Turn Type B: Stage Wave `cur_w`
        if will_stage:
            return self._wrap_calls(
                (
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_s{cur_w}",
                        name="stage_wave_mutations",
                        arguments=stage_draft,
                    ),
                ),
                usage,
                lat,
            )

        # Turn Type C: Checker / CoSign / Commit on `active_digest`
        assert active_digest is not None
        needed_custodians_w2 = [
            (gm.target_entity_id, gm.emergency_custodian_account_id)
            for gm in ora.waves[1].mutations
            if gm.mutation_kind == "UAR_CAMPAIGN_MUTATION" and gm.emergency_custodian_account_id
        ]
        have_cosigns = cosign_tokens_by_digest.get((cur_w, active_digest), [])
        skip_checker_directive = f"do NOT call `run_wave_checker` on Wave {cur_w}" in joined
        need_checker = is_t3 and not skip_checker_directive and (cur_w not in checker_called_waves)
        need_cosign = cur_w == 2 and len(have_cosigns) < len(needed_custodians_w2)

        if need_checker or need_cosign:
            chk_calls: list[ToolCall] = []
            if need_checker:
                chk_calls.append(
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_k{cur_w}",
                        name="run_wave_checker",
                        arguments={"wave_index": cur_w, "stage_digest": active_digest},
                    )
                )
            if need_cosign:
                for camp_id, cust_id in needed_custodians_w2:
                    assert cust_id is not None
                    chk_calls.append(
                        ToolCall(
                            id=f"tc_{self._model_key}_{call_num}_cs_{camp_id}",
                            name="request_cosign",
                            arguments={
                                "wave_index": 2,
                                "stage_digest": active_digest,
                                "campaign_id": camp_id,
                                "custodian_account_id": cust_id,
                            },
                        )
                    )
            return self._wrap_calls(chk_calls, usage, lat)

        return self._wrap_calls(
            self._commit_and_advance_calls(call_num, cur_w, active_digest, have_cosigns),
            usage,
            lat,
        )

    def _commit_and_advance_calls(
        self,
        call_num: int,
        w: int,
        digest: str,
        cosigns: list[str],
    ) -> list[ToolCall]:
        calls = [
            ToolCall(
                id=f"tc_{self._model_key}_{call_num}_cm{w}",
                name="commit_wave_mutations",
                arguments={
                    "wave_index": w,
                    "stage_digest": digest,
                    "cosign_tokens": cosigns,
                    "steer_approval_token": None,
                },
            )
        ]
        if w < 3:
            calls.extend(
                [
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_r{w + 1}",
                        name="read_wave_evidence",
                        arguments={"wave_index": w + 1},
                    ),
                    ToolCall(
                        id=f"tc_{self._model_key}_{call_num}_p{w + 1}",
                        name="poll_interrupts",
                        arguments={"wave_index": w + 1},
                    ),
                ]
            )
        return calls


# ===========================================================================
# 1. `allowlist.py` Unit Tests (`[P1-02]`, `[P1-03]`)
# ===========================================================================


def test_allowlist_accepts_subscription_clis_and_local_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    for cli_kind in (
        "claude_cli",
        "codex_cli",
        "agy_cli",
        "muse_cli",
        "cursor_cli",
        "grok_cli",
    ):
        allowlist.assert_model_spec_allowed({"provider": cli_kind, "model": "test-model"})

    allowlist.assert_model_spec_allowed(
        {
            "provider": "openai_compatible",
            "model": "qwen38",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "api_key_env": None,
        }
    )
    allowlist.assert_model_spec_allowed(
        {
            "provider": "openai_compatible",
            "model": "qwen38",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "api_key_env": "LOCAL_LLM_API_KEY",
        }
    )

    # [R2-P2-04] build_default_provider attaches a redirect-disabled, trust_env=False HTTP client
    prov, _, _ = run_study_v2.build_default_provider("qwen-local")
    assert prov._client is not None  # type: ignore[attr-defined]
    assert prov._client._client.follow_redirects is False  # type: ignore[attr-defined]
    assert prov._client._client._trust_env is False  # type: ignore[attr-defined]
    asyncio.run(run_study_v2.aclose_managed_http_clients())


@pytest.mark.parametrize(
    "good_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8000/v1",
        "http://10.1.2.3:8080/v1",
        "http://172.16.5.4:8080/v1",
        "http://192.168.1.50:8080/v1",
        "http://100.90.12.34:8080/v1",
        "https://gpu-box.tailnet123.ts.net/v1",
        "http://host.docker.internal:8000/v1",
        "http://workstation.local:8000/v1",
        "http://llm.home.arpa:8000/v1",
    ],
)
def test_validate_local_llm_base_url_accepts_private_and_loopback_targets(
    good_url: str,
) -> None:
    allowlist.validate_local_llm_base_url(good_url)


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "ftp://127.0.0.1:8000/v1",
        "http://user:pass@127.0.0.1:8000/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]:8000/v1",
        "http://0.0.0.0:8000/v1",
        "http://8.8.8.8:8000/v1",
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://instance-data.ec2.internal/latest",
        "http://127.0.0.1:8000/v1?redirect=https://openrouter.ai",
        "http://127.0.0.1:8000/v1#frag",
    ],
)
def test_validate_local_llm_base_url_rejects_public_metadata_link_local_and_userinfo(
    bad_url: str,
) -> None:
    with pytest.raises(PermissionError):
        allowlist.validate_local_llm_base_url(bad_url)


@pytest.mark.parametrize(
    "bad_spec",
    [
        {
            "provider": "openai_compatible",
            "model": "kimi-k3",
            "base_url_env": "OPENROUTER_BASE_URL",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        {
            "provider": "openai_compatible",
            "model": "gpt-6.1-sol",
            "base_url_env": "CPA_OPENAI_BASE_URL",
            "api_key_env": "CPA_OPENAI_API_KEY",
        },
        {
            "provider": "openai_compatible",
            "model": "glm-5.3",
            "base_url_env": "OPENCODE_GO_BASE_URL",
            "api_key_env": "OPENCODE_GO_API_KEY",
        },
        {
            "provider": "openai_compatible",
            "model": "gpt-6",
            "base_url_env": "OPENAI_BASE_URL",
            "api_key_env": "OPENAI_API_KEY",
        },
        {"provider": "anthropic_api", "model": "claude-opus-5-5"},
        {"provider": "anthropic", "model": "claude-opus-5-5"},
        {"provider": "anthropic_compatible", "model": "claude-sonnet-5-5"},
        {
            "provider": "openai_compatible",
            "model": "qwen38",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        {
            "provider": "openai_compatible",
            "model": "qwen38",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "extra": {"provider_routing": {"order": ["Together"]}},
        },
        {
            "provider": "openai_compatible",
            "model": "qwen38",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "extra": {"headers": {"HTTP-Referer": "https://openrouter.ai"}},
        },
        {
            "provider": "claude_cli",
            "model": "claude-fable-5-2",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    ],
)
def test_allowlist_rejects_metered_configs_before_turn_1(
    bad_spec: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    with pytest.raises(PermissionError):
        allowlist.assert_model_spec_allowed(bad_spec)


def test_assert_provider_allowed_rejects_spoofed_non_cli_instance() -> None:
    class _SpoofedProvider:
        kind = "claude_cli"
        model = "claude-fable-5-2"

    with pytest.raises(PermissionError):
        allowlist.assert_provider_allowed(_SpoofedProvider(), allow_offline_mock=False)  # type: ignore[arg-type]


def test_redact_sensitive_text_scrubs_urls_escaped_urls_quoted_keys_jwt_oauth_and_env_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://192.168.77.42:8088/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-super-secret-xyz987")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-supersecret987654")
    monkeypatch.setenv("CPA_CLIENT_API_KEY", "cpa-priv-key-998877")
    raw = (
        "Failed at http://192.168.77.42:8088/v1/chat and host 192.168.77.42 and "
        "https://MixedCase-GW.Internal.Lan:8443/v1 on MIXEDCASE-GW.INTERNAL.LAN and "
        r'escaped https:\/\/private-gw.example.internal:8443\/models '
        "encoded https%3A%2F%2Fsecret-gw.corp.lan%2Fv1 "
        'JSON {"api_key": "my_custom_json_secret_value_123", "x-api-key": "hdr_tok_999", '
        '"refresh_token": "rt_secret_abc"} '
        r'escaped_json {\"api_key\": \"escaped_json_secret_value_888\"} '
        "Authorization: Basic dXNlcjpzdXBlcnNlY3JldA== "
        "Authorization: Token tok_secret_header_value_555 "
        "Authorization: ApiKey ak_secret_header_value_777 "
        "Cookie: session_id=sess_multi_111; auth_token=auth_multi_222\r\n"
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "oauth=ya29.a0AfH6SMBx9876543210secret "
        "pat=github_pat_11ABCDEFG0123456789_secret "
        "raw=sk-proj-1234567890abcdef and local-super-secret-xyz987 "
        "and sk-ant-oat01-supersecret987654 and cpa-priv-key-998877"
    )
    scrubbed = allowlist.redact_sensitive_text(raw)
    for forbidden in (
        "192.168.77.42",
        "mixedcase-gw.internal.lan",
        "private-gw.example.internal",
        "secret-gw.corp.lan",
        "my_custom_json_secret_value_123",
        "escaped_json_secret_value_888",
        "hdr_tok_999",
        "rt_secret_abc",
        "dXNlcjpzdXBlcnNlY3JldA==",
        "tok_secret_header_value_555",
        "ak_secret_header_value_777",
        "sess_multi_111",
        "auth_multi_222",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "ya29.a0AfH6SMBx9876543210secret",
        "github_pat_11ABCDEFG0123456789_secret",
        "sk-proj-1234567890abcdef",
        "local-super-secret-xyz987",
        "sk-ant-oat01-supersecret987654",
        "cpa-priv-key-998877",
    ):
        assert forbidden not in scrubbed.lower(), f"Leaked {forbidden!r} in {scrubbed!r}"
    assert "[REDACTED" in scrubbed


def test_probe_local_llm_server_blocks_redirects_caps_size_and_classifies_qwen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://10.20.30.40:9999/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "topsecret-local-token")

    class _FakeResp(io.BytesIO):
        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=_FakeResp(json.dumps({"data": [{"id": "qwen38"}]}).encode("utf-8")),
    ):
        res_27b = allowlist.probe_local_llm_server(timeout_s=1.0)

    assert res_27b == {
        "reachable": True,
        "served_models": ["qwen38"],
        "selected_model": "qwen38",
        "recommended_concurrency": 16,
        "t0_source": "qwen3.8-27b-exl3-4bit-local",
    }
    assert "10.20.30.40" not in json.dumps(res_27b)
    assert "topsecret-local-token" not in json.dumps(res_27b)

    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=_FakeResp(
            json.dumps({"data": [{"id": "qwen3.8-flash-next-iq3_xxs"}]}).encode("utf-8")
        ),
    ):
        res_flash = allowlist.probe_local_llm_server(timeout_s=1.0)

    assert res_flash["selected_model"] == "qwen3.8-flash-next-iq3_xxs"
    assert res_flash["recommended_concurrency"] == 2
    assert res_flash["t0_source"] == "qwen3.8-flash-next-local"

    # Oversized body (> 1 MiB) -> unreachable
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        return_value=_FakeResp(b"x" * (allowlist.MAX_PROBE_RESPONSE_BYTES + 50)),
    ):
        res_big = allowlist.probe_local_llm_server(timeout_s=1.0)
    assert res_big["reachable"] is False

    # HTTP 302 redirect handler raises PermissionError (fail-closed against SSRF redirect)
    handler = allowlist._NoRedirectHandler()
    req = urllib.request.Request("http://10.20.30.40:9999/v1/models")
    with pytest.raises(PermissionError, match="redirect"):
        handler.redirect_request(
            req,
            None,
            302,
            "Found",
            http.client.HTTPMessage(),
            "https://openrouter.ai/api/v1/models",
        )

    # Public URL in LOCAL_LLM_BASE_URL fails closed with PermissionError in probe_local_llm_server
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    with pytest.raises(PermissionError):
        allowlist.probe_local_llm_server(timeout_s=1.0)


# ===========================================================================
# 2. `skills_v2.py` Unit Tests (`[P1-06]..[P1-09]`, `[P2-01]`, `[P2-02]`, `[P3-05]`)
# ===========================================================================


def _collect_imported_modules(py_path: Path) -> set[str]:
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_s2_progressive_playbooks_compact_wave_isolated_and_spec_aligned() -> None:
    # AST anti-oracle-leakage check across Study v2 & test files
    for p in (
        STUDY_V2_DIR / "allowlist.py",
        STUDY_V2_DIR / "skills_v2.py",
        STUDY_V2_DIR / "build_report_v2.py",
        Path(__file__).resolve(),
    ):
        imp = _collect_imported_modules(p)
        assert not any("oracle" in m or "generator" in m or "optimization" in m for m in imp)
    runner_imp = _collect_imported_modules(STUDY_V2_DIR / "run_study_v2.py")
    assert not any("oracle" in m or "optimization" in m for m in runner_imp)

    s1 = skills_v2.S1_MONOLITH_PLAYBOOK
    s1_len = len(s1)
    for k in (1, 2, 3):
        for inc_mc in (False, True):
            s2_k = skills_v2.get_s2_progressive_wave_playbook(k, include_maker_checker=inc_mc)
            assert len(s2_k) < 0.60 * s1_len, f"S2({k}, mc={inc_mc}) exceeded 60% of S1"

    w1 = skills_v2.get_s2_progressive_wave_playbook(1)
    assert "BIND-01a" in w1 and "CONV-02" in w1 and "MOVER-03" in w1 and "SOD-SELF-04" in w1
    assert "P_bind(a)" in w1 and "P_bind(a)" in s1
    assert "NEVER `CONV-02`" in w1 and "NEVER cite `CONV-02`" in s1
    assert "from_dept_id == g.dept_scope_id == p.prior_dept_id" in w1
    assert "granted_role_ids" in w1
    assert "NHI-GOV-01" not in w1 and "CONN-SYNC-01" not in w1 and "OPT-01" not in w1

    w2 = skills_v2.get_s2_progressive_wave_playbook(2)
    assert "NHI-GOV-01" in w2 and "UAR-QUORUM-01" in w2 and "request_cosign" in w2
    assert "m_eligible satisfied" not in w2  # [P1-08] Step 3 floor retention uses m_standing only!
    assert "decisive rank" in w2
    assert ">= 2" in w2
    assert "cosign_tokens" in w2
    assert '(if `nhi.environment == "prod"`)' in s1 and '(if `nhi.environment == "prod"`)' in w2
    assert "BIND-01a" not in w2 and "CONN-SYNC-01" not in w2 and "OPT-01" not in w2

    w3 = skills_v2.get_s2_progressive_wave_playbook(3)
    assert "CONN-SYNC-01" in w3 and "JIT-ZSP-01" in w3 and "CAE-SLI-01" in w3 and "OPT-01" in w3
    assert "min(requested_ttl_s, max_policy_ttl_s, ticket.expires_at_s - clock_epoch_s)" in s1
    assert "[t, t + approved_ttl_s + cooldown)" in w3
    assert "**only** on `APPROVE_JIT`" in w3
    assert "uar_rightsizing" in w3
    assert "BIND-01a" not in w3 and "NHI-GOV-01" not in w3

    with pytest.raises(ValueError):
        skills_v2.get_s2_progressive_wave_playbook(0)

    hashes = skills_v2.skill_manifest_hashes()
    assert len(hashes["S1_monolith_sha256"]) == 64
    assert hashes["validator_version"] == skills_v2.VALIDATOR_VERSION


def test_lint_stage_payload_zero_leakage_discriminated_adapter_and_unread_gate(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
) -> None:
    ep, ora = dev_pairs[0]
    live = _step_env_to_live_snapshots(ep, ora)

    for k in (1, 2, 3):
        snap, read_eids, nar = live[k]
        gold_args = _gold_stage_dict(ora, k)
        assert skills_v2.lint_stage_payload(k, gold_args, snap, read_eids, nar) == []
        # [P2-02] Also accepts tuples cleanly
        tuple_args = {
            "wave_index": k,
            "mutations": tuple(gold_args["mutations"]),
            "citations": tuple(gold_args["citations"]),
            "resource_preconditions": tuple(gold_args["resource_preconditions"]),
        }
        assert skills_v2.lint_stage_payload(k, tuple_args, snap, read_eids, nar) == []

    # [P3-05] & [R2-P2-02] Rejects bool/missing/None wave_index and unknown top-level keys!
    snap1, read1, nar1 = live[1]
    bool_wave = _gold_stage_dict(ora, 1)
    bool_wave["wave_index"] = True
    assert any(
        "wave_index mismatch" in e
        for e in skills_v2.lint_stage_payload(1, bool_wave, snap1, read1, nar1)
    )
    missing_wave = _gold_stage_dict(ora, 1)
    missing_wave.pop("wave_index")
    assert any(
        "wave_index is required" in e
        for e in skills_v2.lint_stage_payload(1, missing_wave, snap1, read1, nar1)
    )
    none_wave = _gold_stage_dict(ora, 1)
    none_wave["wave_index"] = None
    assert any(
        "wave_index is required" in e
        for e in skills_v2.lint_stage_payload(1, none_wave, snap1, read1, nar1)
    )
    unk_top = _gold_stage_dict(ora, 1)
    unk_top["unexpected_extra_key"] = 123
    assert any(
        "unknown top-level" in e and "unexpected_extra_key" in e
        for e in skills_v2.lint_stage_payload(1, unk_top, snap1, read1, nar1)
    )

    # [R2-P1-01] Empty `mutations=[]` / `resource_preconditions=[]` NEVER leaks expected entity IDs!
    empty_w1 = {
        "wave_index": 1,
        "mutations": [],
        "citations": [{"target_entity_id": "bogus_unattached_tid_99", "rule_id": "BIND-01a", "evidence_id": next(iter(read1))}],
        "resource_preconditions": [],
    }
    errs_empty_w1 = "\n".join(skills_v2.lint_stage_payload(1, empty_w1, snap1, read1, nar1))
    assert "COMPLETENESS_VIOLATION for Wave 1" in errs_empty_w1
    assert "missing self resource_preconditions for" in errs_empty_w1
    assert "citations reference unattached/ungoverned target_entity_ids" in errs_empty_w1
    assert "bogus_unattached_tid_99" in errs_empty_w1
    for gold_m in ora.waves[0].mutations:
        assert gold_m.target_entity_id not in errs_empty_w1, (
            f"Leaked expected target_entity_id {gold_m.target_entity_id!r} in {errs_empty_w1!r}"
        )

    # [P1-06] Unconditional unread check even when `read_evidence_ids == set()`!
    unread_errs = skills_v2.lint_stage_payload(1, _gold_stage_dict(ora, 1), snap1, set(), nar1)
    assert any("unread/non-existent evidence_ids" in e for e in unread_errs)

    # [P1-07] Discriminated MUTATION_ADAPTER reports loc+msg WITHOUT double-firing COMPLETENESS_VIOLATION!
    snap2, read2, nar2 = live[2]
    bad_field_w2 = _gold_stage_dict(ora, 2)
    nhi_tid = str(bad_field_w2["mutations"][0]["target_entity_id"])
    bad_field_w2["mutations"][0] = {
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": nhi_tid,
        "nhi_action": "ESCALATE",
        "resolved_owner_person_id": None,
        "owner_resolution_source": "cmdb_service_owner",
    }
    errs_field = skills_v2.lint_stage_payload(2, bad_field_w2, snap2, read2, nar2)
    assert any("schema error" in e and nhi_tid in e for e in errs_field)
    assert not any("COMPLETENESS_VIOLATION" in e for e in errs_field)

    # [R2-P1-01] UAR item drop reports count mismatch ONLY (never leaks missing item_id!)
    uar_drop_w2 = _gold_stage_dict(ora, 2)
    dropped_item_id: str | None = None
    for m_dict in uar_drop_w2["mutations"]:
        if m_dict.get("mutation_kind") == "UAR_CAMPAIGN_MUTATION" and m_dict.get("uar_decisions"):
            dropped_item_id = str(m_dict["uar_decisions"][0][0])
            m_dict["uar_decisions"] = m_dict["uar_decisions"][1:]
            break
    assert dropped_item_id is not None
    errs_uar_drop = "\n".join(skills_v2.lint_stage_payload(2, uar_drop_w2, snap2, read2, nar2))
    assert "uar_decisions item count mismatch" in errs_uar_drop
    assert dropped_item_id not in errs_uar_drop

    # Dropping a mutation reports COMPLETENESS_VIOLATION + duplicate/empty/OCC checks (ZERO ID/etag leakage!)
    bad_w2 = _gold_stage_dict(ora, 2)
    dropped_w2_tid = str(bad_w2["mutations"][0]["target_entity_id"])
    bad_w2["mutations"] = bad_w2["mutations"][1:]
    bad_w2["citations"] = [
        c for c in bad_w2["citations"] if c.get("target_entity_id") != dropped_w2_tid
    ]
    bad_w2["citations"].append(bad_w2["citations"][0])
    surviving_tid = str(bad_w2["mutations"][0]["target_entity_id"])
    bad_w2["citations"].append(
        {
            "target_entity_id": surviving_tid,
            "rule_id": "NHI-GOV-01",
            "evidence_id": "ev_unread_bogus_999",
        }
    )
    # Keep preconditions on surviving targets so dropped_w2_tid isn't referenced in submitted preconditions
    bad_w2["resource_preconditions"] = [
        p for p in bad_w2["resource_preconditions"] if p.get("entity_id") != dropped_w2_tid
    ]
    real_etag = str(bad_w2["resource_preconditions"][0]["if_match_etag"])
    bad_w2["resource_preconditions"][0] = {
        "entity_id": bad_w2["resource_preconditions"][0]["entity_id"],
        "if_match_etag": "deadbeef",
        "account_gen": 999,
    }
    bad_w2["resource_preconditions"].append(bad_w2["resource_preconditions"][-1])

    errs_w2 = "\n".join(skills_v2.lint_stage_payload(2, bad_w2, snap2, read2, nar2))
    assert "COMPLETENESS_VIOLATION for Wave 2" in errs_w2
    assert "duplicate citations" in errs_w2
    assert "ev_unread_bogus_999" in errs_w2
    assert "duplicate resource_preconditions" in errs_w2
    assert "PRECONDITION_TUPLE_MISMATCH" in errs_w2
    # [P1-06] & [R2-P1-01] ZERO LEAKAGE: expected live etag & dropped target ID NEVER appear in lint output!
    assert real_etag not in errs_w2
    assert dropped_w2_tid not in errs_w2


def test_verify_opt_candidate_feasibility_all_5_kinds_and_defensive_guards(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
) -> None:
    seen_kinds: set[str] = set()
    sample_uar_task = None
    sample_snap3 = None
    sample_appr_task = None
    sample_appr_snap3 = None

    for ep, ora in dev_pairs[:6]:
        live = _step_env_to_live_snapshots(ep, ora)
        snap3, read3, nar3 = live[3]
        opt_map = {o.task_id: o for o in snap3.optimization_tasks}

        for gm in ora.waves[2].mutations:
            if gm.mutation_kind != "OPTIMIZATION_MUTATION":
                continue
            task = opt_map[gm.target_entity_id]
            seen_kinds.add(task.kind)
            if task.kind == "uar_rightsizing" and sample_uar_task is None:
                sample_uar_task = (task, gm.selected_ids)
                sample_snap3 = snap3
            if task.kind == "approver_routing" and sample_appr_task is None:
                sample_appr_task = (task, gm.selected_ids, gm.objective_value)
                sample_appr_snap3 = snap3

            # Gold candidate must be feasible with exact objective match
            ok, comp, reasons = skills_v2.verify_opt_candidate_feasibility(
                task, gm.selected_ids, gm.objective_value, snap3
            )
            assert ok is True and comp == gm.objective_value and reasons == []

            # [P1-06] Wrong objective_value -> feasible subset, comp_mis is None (ZERO integer leak!)
            ok_mis, comp_mis, reasons_mis = skills_v2.verify_opt_candidate_feasibility(
                task, gm.selected_ids, gm.objective_value + 11, snap3
            )
            assert ok_mis is True and comp_mis is None
            joined_mis = "\n".join(reasons_mis)
            assert "OPT_OBJECTIVE_MISMATCH" in joined_mis
            assert str(gm.objective_value) not in joined_mis

            # Linter surfaces OPT_OBJECTIVE_MISMATCH without leaking gold objective_value
            w3_bad = _gold_stage_dict(ora, 3)
            for m_dict in w3_bad["mutations"]:
                if m_dict.get("target_entity_id") == task.task_id:
                    m_dict["objective_value"] = gm.objective_value + 11
            lint_errs = "\n".join(skills_v2.lint_stage_payload(3, w3_bad, snap3, read3, nar3))
            assert "OPT_OBJECTIVE_MISMATCH" in lint_errs

            # [P1-09] String / bytes / non-sequence selected_ids rejected cleanly without raising
            for bad_sel in ("opt_str_not_seq", b"bytes_not_seq", 12345, [123]):
                ok_bad, comp_bad, r_bad = skills_v2.verify_opt_candidate_feasibility(
                    task,
                    bad_sel,  # type: ignore[arg-type]
                    0,
                    snap3,
                )
                assert ok_bad is False and comp_bad is None and len(r_bad) == 1

            # Unknown ID -> infeasible
            ok_unk, comp_unk, reasons_unk = skills_v2.verify_opt_candidate_feasibility(
                task, ["zz_nonexistent_id_99"], 0, snap3
            )
            assert ok_unk is False and comp_unk is None and len(reasons_unk) >= 1

            # Kind-specific constraint violation check
            if task.kind in {
                "zsp_right_sizing",
                "approver_routing",
                "sod_remediation_shared_groups",
            } or (task.kind == "role_mining" and any(u.capabilities for u in task.users)):
                ok_emp, _, _ = skills_v2.verify_opt_candidate_feasibility(task, [], 0, snap3)
                assert ok_emp is False

    # [P1-09] Partial tier_weights override merges onto {"admin": 5, "write": 3, "read": 1}
    assert sample_uar_task is not None and sample_snap3 is not None
    u_task, u_sel = sample_uar_task
    partial_task = u_task.model_copy(update={"tier_weights": (("admin", 9),)})
    ok_pw, _, _ = skills_v2.verify_opt_candidate_feasibility(
        partial_task, u_sel, 999999, sample_snap3
    )
    assert ok_pw is True  # No KeyError on "write" or "read"!

    # [P3] approver_routing falls back to snapshot.roles via setdefault & never leaks unassigned IDs
    assert sample_appr_task is not None and sample_appr_snap3 is not None
    a_task, a_sel, a_obj = sample_appr_task
    no_rcap_task = a_task.model_copy(update={"role_capabilities": ()})
    ok_ar, comp_ar, _ = skills_v2.verify_opt_candidate_feasibility(
        no_rcap_task, a_sel, a_obj, sample_appr_snap3
    )
    assert ok_ar is True and comp_ar == a_obj
    ok_unass, _, r_unass = skills_v2.verify_opt_candidate_feasibility(
        a_task, a_sel[:-1], 0, sample_appr_snap3
    )
    assert ok_unass is False
    unass_joined = "\n".join(r_unass)
    assert "missing_count=1" in unass_joined
    for req_rec in a_task.routing_requests:
        assert req_rec.request_id not in unass_joined

    assert seen_kinds == {
        "zsp_right_sizing",
        "role_mining",
        "sod_remediation_shared_groups",
        "approver_routing",
        "uar_rightsizing",
    }


# ===========================================================================
# 3. `run_study_v2.py` Multi-Turn Loop, Exact Budgets, Caching & Error Tests
# ===========================================================================


def _make_builder(
    oracles_by_ep: dict[str, SealedEpisodeOracleV41],
    counts: dict[str, int],
    hook: HookCallable | None = None,
    *,
    emitted_call_ids: set[str] | None = None,
) -> run_study_v2.ProviderBuilder:
    def _builder(
        model_key: str, temperature: float | None = None
    ) -> tuple[Provider, GenerationParams, int]:
        prov = ScriptedStudyMockProvider(
            model_key,
            temperature,
            oracles_by_ep,
            counts,
            hook,
            emitted_call_ids=emitted_call_ids,
        )
        return prov, GenerationParams(temperature=temperature), 4

    return _builder


def test_no_monkeypatch_and_zero_python_tool_synthesis_in_runner() -> None:
    # [P1-05] Core render_cli_system_prompt must remain untouched at import time!
    assert '"thought"' in render.render_cli_system_prompt()

    # [P0-02] & [R2-P0-02] Deleted Python synthesis helpers and removed 24-turn fanout cap!
    for forbidden_helper in (
        "_ensure_wave_telemetry",
        "_stage_check_and_maybe_commit",
        "_build_10_shards_for_wave",
        "_merge_shard_outputs",
        "HARD_MAX_FANOUT_COMPLETIONS",
        "BUDGET_REGIME_FANOUT",
    ):
        assert not hasattr(run_study_v2, forbidden_helper)

    # [R2-P0-02] Every cell in the matrix strictly obeys G-TURN-01 (`max_turns == 12`)
    for cid in run_study_v2.all_cell_ids(SplitRingV41.v41_lite):
        assert run_study_v2.budget_for_cell(cid) == ("G_TURN_12_STRICT", 12, 12)

    # [P1-01] grok-4.7 is in NEWCOMER_S0_MODELS (never imported from OpenRouter JSONL!)
    assert "grok-4.7" in run_study_v2.NEWCOMER_S0_MODELS
    assert "grok-4.7" not in run_study_v2.EXISTING_S0_MODELS


def test_single_agent_s2_and_s3_prestage_linter_gating_and_retry(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
) -> None:
    ep, ora = dev_pairs[0]
    oracles_map = {ep.episode_id: ora}
    counts: dict[str, int] = defaultdict(int)
    emitted_ids: set[str] = set()

    # Inject unread bogus citation on first Wave 1 stage AFTER Wave 1 is read -> S3 lint triggers 1 retry
    injected = {"done": False}

    def _s3_hook(
        _mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tag.startswith("STAGE_W1::") and not injected["done"]:
            injected["done"] = True
            bad = copy.deepcopy(stage_args)
            bad["citations"].append(
                {
                    "target_entity_id": bad["mutations"][0]["target_entity_id"],
                    "rule_id": "BIND-01a",
                    "evidence_id": "ev_fake_unread_lint_trigger",
                }
            )
            return bad
        return None

    rec_s3, meta_s3 = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="qwen-local",
            arm_id="C_S3_prog_val_t3",
            cell_id="qwen-local__C_S3_prog_val_t3",
            provider_builder=_make_builder(oracles_map, counts, _s3_hook, emitted_call_ids=emitted_ids),
            allow_offline_mock=True,
        )
    )
    assert rec_s3.execution_valid is True and rec_s3.provider_error is False
    assert rec_s3.scorecard.g_srr == 1.0
    assert rec_s3.turns_used == 11  # 10 standard T3 turns + 1 S3 lint retry!
    assert rec_s3.max_calls_in_turn <= 4
    assert meta_s3["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_s3["waves"][0]["s3_lint_retries"] == 1
    assert rec_s3.tool_calls_count == len(rec_s3.tool_trace)

    # [P1-06] If LLM tries to stage BEFORE calling read_wave_evidence(1), S3 linter does NOT intercept!
    unread_counts: dict[str, int] = defaultdict(int)

    def _premature_stage_hook(
        _mk: str,
        _temp: float | None,
        call_num: int,
        _tag: str,
        _stage_args: dict[str, Any],
    ) -> ModelResponse | None:
        if call_num == 1:
            return ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="premature_s1",
                        name="stage_wave_mutations",
                        arguments=_gold_stage_dict(ora, 1),
                    ),
                ),
                usage=Usage(input_tokens=100, output_tokens=20),
                latency_s=0.1,
            )
        return None

    rec_pre, meta_pre = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="qwen-local",
            arm_id="C_S3_prog_val_t3",
            cell_id="qwen-local__C_S3_prog_val_t3",
            provider_builder=_make_builder(oracles_map, unread_counts, _premature_stage_hook),
            allow_offline_mock=True,
        )
    )
    # Premature stage went straight to env (0 S3 lint retries before read_wave_evidence!)
    assert meta_pre["waves"][0]["s3_lint_retries"] == 0
    assert rec_pre.tool_trace[0]["name"] == "stage_wave_mutations"

    # Also verify CLI-mode S2 T0 control (`gemini-3.8-flash`, `native_tools=False`) completes in <= 8 turns
    rec_s2, _ = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="gemini-3.8-flash",
            arm_id="C_S2_prog_t0",
            cell_id="gemini-3.8-flash__C_S2_prog_t0",
            provider_builder=_make_builder(oracles_map, counts),
            allow_offline_mock=True,
        )
    )
    assert rec_s2.scorecard.g_srr == 1.0
    assert rec_s2.turns_used <= 8


def test_o3_teams_t01_rollouts_and_t03_t04_cross_cell_cache_reuse(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
) -> None:
    ep, ora = dev_pairs[1]
    oracles_map = {ep.episode_id: ora}

    # 1. T01: temp=0 worker drops a citation on Wave 1 -> REJECT_VIOLATION -> K=2 parallel temp=0.6 rollouts!
    t01_counts: dict[str, int] = defaultdict(int)
    rollout_seen = {"n": 0}

    def _t01_hook(
        _mk: str,
        temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if temp == 0.0 and tag.startswith("STAGE_W1::"):
            return _corrupt_stage_semantic(stage_args, 1)
        if temp == 0.6 and "Compute-Scaled Parallel Repair — Wave 1" in tag:
            rollout_seen["n"] += 1
            if rollout_seen["n"] == 1:
                # First rollout has a missing mutation (lint defect), second rollout is clean gold!
                bad_r = copy.deepcopy(stage_args)
                bad_r["mutations"] = bad_r["mutations"][1:]
                return bad_r
        return None

    rec_t01, meta_t01 = asyncio.run(
        run_study_v2.run_team_episode(
            "T01_Local_ComputeTiered_27B",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, t01_counts, _t01_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_t01.scorecard.g_srr == 1.0
    assert meta_t01["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_t01["waves"][0]["committed_by"] == "local_rollout"
    assert rollout_seen["n"] == 2
    # [P0-01] Exact completion count: 10 standard turns + 2 parallel rollouts = 12 completions, 11 env turns!
    assert rec_t01.turns_used == 12
    assert meta_t01["llm_completions_used"] == 12
    assert meta_t01["env_turns_used"] == 11
    assert meta_t01["compute_latency_s"] > meta_t01["wall_latency_s"]
    # [R2-P2-06] Parallel batch wall-time (`max` = 0.50s) < summed request GPU time (`2 * 0.50s` = 1.00s)!
    assert meta_t01["local_gpu_request_seconds"] > meta_t01["local_gpu_seconds"] > 0.0

    # 1b. [R2-P1-02] Premature stage BEFORE `read_wave_evidence(1)` NEVER triggers T01 rollout or touches `snapshots[1]`!
    premature_t01_counts: dict[str, int] = defaultdict(int)

    def _premature_t01_hook(
        _mk: str,
        _temp: float | None,
        call_num: int,
        _tag: str,
        _stage_args: dict[str, Any],
    ) -> ModelResponse | None:
        if call_num == 1:
            return ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="prem_t01_s1",
                        name="stage_wave_mutations",
                        arguments={"wave_index": 1, "mutations": []},
                    ),
                ),
                usage=Usage(input_tokens=100, output_tokens=20),
                latency_s=0.1,
            )
        return None

    rec_t01_pre, meta_t01_pre = asyncio.run(
        run_study_v2.run_team_episode(
            "T01_Local_ComputeTiered_27B",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, premature_t01_counts, _premature_t01_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_t01_pre.scorecard.g_srr == 1.0
    assert meta_t01_pre["local_rollouts_count"] == 0
    assert rec_t01_pre.turns_used <= 12

    # 1c. [R2-P2-05] S3 worker gets 1 local pre-stage lint retry before escalating to Boss!
    s3_self_counts: dict[str, int] = defaultdict(int)
    s3_once = {"fired": False}

    def _s3_worker_self_fix(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "qwen-local" and tag.startswith("STAGE_W1::") and not s3_once["fired"]:
            s3_once["fired"] = True
            bad = copy.deepcopy(stage_args)
            bad["citations"].append(
                {
                    "target_entity_id": bad["mutations"][0]["target_entity_id"],
                    "rule_id": "BIND-01a",
                    "evidence_id": "ev_s3_worker_local_retry_1",
                }
            )
            return bad
        return None

    rec_s3_self, meta_s3_self = asyncio.run(
        run_study_v2.run_team_episode(
            "T03_Hybrid_Sol_Qwen27B",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, s3_self_counts, _s3_worker_self_fix),
            allow_offline_mock=True,
        )
    )
    assert rec_s3_self.scorecard.g_srr == 1.0
    assert meta_s3_self["s3_lint_retries_count"] == 1
    assert meta_s3_self["boss_escalations_count"] == 0
    assert s3_self_counts["gpt-6.1-sol"] == 0  # Worker fixed its own lint defect in 1 retry!

    # If the S3 worker STILL fails lint after its 1 local retry, it escalates to Boss!
    s3_esc_counts: dict[str, int] = defaultdict(int)

    def _s3_worker_double_lint_fail(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "qwen-local" and (tag.startswith("STAGE_W1::") or tag.startswith("LINT_FIX_W1::")):
            bad = copy.deepcopy(stage_args)
            bad["citations"].append(
                {
                    "target_entity_id": bad["mutations"][0]["target_entity_id"],
                    "rule_id": "BIND-01a",
                    "evidence_id": "ev_s3_worker_double_fail",
                }
            )
            return bad
        return None

    rec_s3_esc, meta_s3_esc = asyncio.run(
        run_study_v2.run_team_episode(
            "T03_Hybrid_Sol_Qwen27B",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, s3_esc_counts, _s3_worker_double_lint_fail),
            allow_offline_mock=True,
        )
    )
    assert rec_s3_esc.scorecard.g_srr == 1.0
    assert meta_s3_esc["s3_lint_retries_count"] == 1
    assert meta_s3_esc["boss_escalations_count"] == 1
    assert rec_s3_esc.turns_used <= 12

    # 2. T03 + T04: qwen-local (`native_tools=True`, temp=0.0) passes W1-W2, fails W3 -> Boss (`native_tools=False` CLI)
    # repairs W3; T04 reuses 100% of T03's 9 deterministic qwen-local completions from StudyCacheStore!
    cache = run_study_v2.StudyCacheStore(tmp_path)
    shared_counts: dict[str, int] = defaultdict(int)

    def _w3_worker_fail(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "qwen-local" and tag.startswith("STAGE_W3::"):
            return _corrupt_stage_semantic(stage_args, 3)
        return None

    rec_t03, meta_t03 = asyncio.run(
        run_study_v2.run_team_episode(
            "T03_Hybrid_Sol_Qwen27B",
            ep,
            ora,
            cache_store=cache,
            provider_builder=_make_builder(oracles_map, shared_counts, _w3_worker_fail),
            allow_offline_mock=True,
        )
    )
    assert rec_t03.scorecard.g_srr == 1.0
    assert meta_t03["worker_direct_commits_count"] == 2
    assert meta_t03["boss_escalations_count"] == 1
    assert meta_t03["waves"][2]["committed_by"] == "boss"
    assert rec_t03.turns_used == 11  # 9 qwen-local + 2 gpt-6.1-sol repair/commit turns
    assert shared_counts["qwen-local"] == 9
    assert shared_counts["gpt-6.1-sol"] == 2

    rec_t04, meta_t04 = asyncio.run(
        run_study_v2.run_team_episode(
            "T04_Hybrid_Carbon_Qwen27B",
            ep,
            ora,
            cache_store=cache,
            provider_builder=_make_builder(oracles_map, shared_counts, _w3_worker_fail),
            allow_offline_mock=True,
        )
    )
    assert rec_t04.scorecard.g_srr == 1.0
    assert meta_t04["boss_escalations_count"] == 1
    assert rec_t04.turns_used == 11
    assert meta_t04["cache_hit_completions"] == 9
    assert shared_counts["qwen-local"] == 9  # ZERO new qwen-local calls in T04!
    assert shared_counts["gemini-4-carbon"] == 2
    assert meta_t04["executed_local_gpu_seconds"] == 0.0
    assert meta_t04["local_gpu_seconds"] > 0.0


def test_o4_o5_o6_o7_and_subscription_tiered_teams_pure_multiturn(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
) -> None:
    ep, ora = dev_pairs[2]
    oracles_map = {ep.episode_id: ora}

    # 1. O4 Wave Split (`T05`): Qwen W1-W2 (`native_tools=True`) + Fable 5.2 W3 (`native_tools=False`, no-thought)
    c_t05: dict[str, int] = defaultdict(int)
    rec_t05, meta_t05 = asyncio.run(
        run_study_v2.run_team_episode(
            "T05_Hybrid_WaveSplit_Fable_Qwen",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_t05),
            allow_offline_mock=True,
        )
    )
    assert rec_t05.scorecard.g_srr == 1.0
    assert meta_t05["budget_regime"] == "G_TURN_12_STRICT"
    assert rec_t05.turns_used == 10  # 7 Qwen (W1-W2) + 3 Fable (W3)
    assert c_t05["qwen-local"] == 7 and c_t05["claude-fable-5-2"] == 3

    # 2. O4 Specialist Consultation (`T07`): Sol multi-turn + 1 Fable OPT-01 advisory memo on Wave 3
    c_t07: dict[str, int] = defaultdict(int)
    rec_t07, meta_t07 = asyncio.run(
        run_study_v2.run_team_episode(
            "T07_Specialist_FableMath_SolSync",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_t07),
            allow_offline_mock=True,
        )
    )
    assert rec_t07.scorecard.g_srr == 1.0
    assert meta_t07["waves"][2]["committed_by"] == "specialist_merge"
    assert c_t07["gpt-6.1-sol"] == 10 and c_t07["claude-fable-5-2"] == 1
    assert rec_t07.turns_used == 11  # 10 Sol env turns + 1 Fable advisory completion!
    assert meta_t07["env_turns_used"] == 10

    # 3. O5 Swarm (`T02` & `T06`): `[R2-P0-02]` strictly `<= 12` completions under `G_TURN_12_STRICT`!
    swarm_cache = run_study_v2.StudyCacheStore(tmp_path)
    c_o5: dict[str, int] = defaultdict(int)
    rec_t02, meta_t02 = asyncio.run(
        run_study_v2.run_team_episode(
            "T02_Local_vLLM_10Shard_Swarm",
            ep,
            ora,
            cache_store=swarm_cache,
            provider_builder=_make_builder(oracles_map, c_o5),
            allow_offline_mock=True,
        )
    )
    assert rec_t02.scorecard.g_srr == 1.0
    assert meta_t02["budget_regime"] == "G_TURN_12_STRICT"
    assert rec_t02.turns_used == 10 <= 12  # 8 env turns + 2 parallel W3 specialist shards!
    assert meta_t02["env_turns_used"] == 8
    assert meta_t02["waves"][2]["committed_by"] == "coalescer"
    assert rec_t02.max_calls_in_turn <= 4

    # T06 reuses temp=0.0 W3_ops_and_opt_A subagent cache entry (1 hit; temp=0.6 W3_opt_rollout_B is not cached!)
    # and escalates Wave 3 to `gpt-6-astra` when Terra's W3 stage fails checker — all within <= 12 completions!
    def _terra_w3_fail(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "gpt-6.1-terra" and tag.startswith("STAGE_W3::"):
            return _corrupt_stage_semantic(stage_args, 3)
        return None

    rec_t06, meta_t06 = asyncio.run(
        run_study_v2.run_team_episode(
            "T06_Hybrid_3Layer_UltraSwarm",
            ep,
            ora,
            cache_store=swarm_cache,
            provider_builder=_make_builder(oracles_map, c_o5, _terra_w3_fail),
            allow_offline_mock=True,
        )
    )
    assert rec_t06.scorecard.g_srr == 1.0
    assert meta_t06["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_t06["boss_escalations_count"] == 1
    assert meta_t06["waves"][2]["committed_by"] == "boss"
    assert meta_t06["cache_hit_completions"] == 2  # W1 read + temp=0.0 W3_ops_and_opt_A cached from T02!
    assert rec_t06.turns_used == 11 <= 12
    assert meta_t06["env_turns_used"] == 9 <= 12

    # 4. O6 Rival Peer Cross-Audit (`T08`): Sonnet fails W2 -> Argon audits & repairs W2!
    c_o6: dict[str, int] = defaultdict(int)

    def _t08_hook(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "claude-sonnet-5-5" and tag.startswith("STAGE_W2::"):
            return _corrupt_stage_semantic(stage_args, 2)
        return None

    rec_t08, meta_t08 = asyncio.run(
        run_study_v2.run_team_episode(
            "T08_PeerDebate_Argon_x_Sonnet",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_o6, _t08_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_t08.scorecard.g_srr == 1.0
    assert meta_t08["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_t08["boss_escalations_count"] == 0
    assert meta_t08["peer_audits_count"] == 1
    assert meta_t08["waves"][1]["committed_by"] == "peer_auditor"
    assert rec_t08.turns_used <= 12

    # 5. O7 Boss-Less 3-Pipe Jury (`T14`): all 3 pipes rotate across W1-W2 and converge on W3 within <= 12 completions!
    c_o7_vote: dict[str, int] = defaultdict(int)

    def _t14_vote_hook(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if mk == "qwen-local" and "Independent Jury Member — Wave 3" in tag:
            bad = copy.deepcopy(stage_args)
            bad["mutations"] = bad["mutations"][1:]  # lint defect -> outvoted by composer-2.5!
            return bad
        return None

    rec_t14_v, meta_t14_v = asyncio.run(
        run_study_v2.run_team_episode(
            "T14_Bossless_3Pipe_Jury",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_o7_vote, _t14_vote_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_t14_v.scorecard.g_srr == 1.0
    assert meta_t14_v["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_t14_v["boss_escalations_count"] == 0
    assert meta_t14_v["waves"][2]["committed_by"] == "jury_vote"
    assert rec_t14_v.turns_used <= 12
    assert c_o7_vote["qwen-local"] >= 1 and c_o7_vote["composer-2.5"] >= 1 and c_o7_vote["grok-4.7"] >= 1

    # Also verify T14 Wave 3 synthesis repair when both parallel W3 proposals fail semantic checker!
    c_o7_rep: dict[str, int] = defaultdict(int)

    def _t14_repair_hook(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        stage_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if "Independent Jury Member — Wave 3" in tag and mk in {"qwen-local", "composer-2.5"}:
            return _corrupt_stage_semantic(stage_args, 3)
        return None

    rec_t14_r, meta_t14_r = asyncio.run(
        run_study_v2.run_team_episode(
            "T14_Bossless_3Pipe_Jury",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_o7_rep, _t14_repair_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_t14_r.scorecard.g_srr == 1.0
    assert meta_t14_r["budget_regime"] == "G_TURN_12_STRICT"
    assert meta_t14_r["waves"][2]["committed_by"] == "jury_synthesis"
    assert meta_t14_r["jury_repairs_count"] == 1
    assert rec_t14_r.turns_used <= 12
    assert meta_t14_r["env_turns_used"] <= 12

    # 6. [R2-P0-02] Verify ALL 14 teams (`T01`..`T14`) complete cleanly with `g_srr == 1.0` and `turns_used <= 12`!
    assert len(run_study_v2.TEAM_DEFINITIONS) == 14
    for team_id in run_study_v2.TEAM_DEFINITIONS:
        c_all: dict[str, int] = defaultdict(int)
        r_all, m_all = asyncio.run(
            run_study_v2.run_team_episode(
                team_id,
                ep,
                ora,
                cache_store=None,
                provider_builder=_make_builder(oracles_map, c_all),
                allow_offline_mock=True,
            )
        )
        assert r_all.scorecard.g_srr == 1.0, f"{team_id} failed g_srr"
        assert m_all["budget_regime"] == "G_TURN_12_STRICT"
        assert r_all.turns_used <= 12, f"{team_id} exceeded 12 completions: {r_all.turns_used}"


def test_parallel_provider_error_retains_sibling_tokens_and_resume_deduplicates_team_logs(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
) -> None:
    sub_pairs = dev_pairs[:2]
    fail_eid = sub_pairs[1][0].episode_id
    oracles_map = {ep.episode_id: ora for ep, ora in sub_pairs}

    # [P1-12] & [R2-P1-05] In T14's parallel jury on Wave 3, composer-2.5 fails while qwen-local succeeds:
    # ALL prior wave tokens + both parallel siblings' tokens MUST be retained AND logged in executed_calls.jsonl!
    c_par: dict[str, int] = defaultdict(int)

    def _jury_sibling_fail(
        mk: str,
        _temp: float | None,
        _call_num: int,
        tag: str,
        _stage: dict[str, Any],
    ) -> ModelResponse | None:
        if fail_eid in tag and mk == "composer-2.5" and "Independent Jury Member — Wave 3" in tag:
            return ModelResponse(
                text="",
                error="HTTP 503 upstream timeout at https://10.99.88.77:8000/v1 with Bearer sk-secret999999999",
                raw={"nonretryable": True},
                usage=Usage(input_tokens=111, output_tokens=7),
                latency_s=1.2,
            )
        return None

    cache = run_study_v2.StudyCacheStore(tmp_path)
    recs_1 = asyncio.run(
        run_study_v2.run_cell_in_stratified_blocks(
            "T14_Bossless_3Pipe_Jury",
            sub_pairs,
            suite=SplitRingV41.v41_dev,
            out_dir=tmp_path,
            cache_store=cache,
            provider_builder=_make_builder(oracles_map, c_par, _jury_sibling_fail),
            allow_offline_mock=True,
        )
    )
    by_ep_1 = {r.episode_id: r for r in recs_1}
    failed_rec = by_ep_1[fail_eid]
    assert failed_rec.execution_valid is False
    assert failed_rec.provider_error is True
    assert failed_rec.parse_failure is False
    assert failed_rec.termination_reason == "PROVIDER_ERROR"
    w2_has_custodian = any(
        m.mutation_kind == "UAR_CAMPAIGN_MUTATION" and m.emergency_custodian_account_id
        for m in sub_pairs[1][1].waves[1].mutations
    )
    prior_turns = 6 if w2_has_custodian else 5
    # Prior wave turns (prior_turns * 400 in) + 1 succeeded W3 juror (400 in) + 1 failed W3 juror (111 in)!
    assert failed_rec.input_tokens == (prior_turns * 400) + 400 + 111
    assert failed_rec.turns_used == prior_turns + 2
    assert failed_rec.error_message is not None
    assert "10.99.88.77" not in failed_rec.error_message
    assert "sk-secret999999999" not in failed_rec.error_message

    exec_lines_before_resume = [
        json.loads(line)
        for line in (tmp_path / "executed_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed_ep_calls_before = [r for r in exec_lines_before_resume if r["episode_id"] == fail_eid]
    assert len(failed_ep_calls_before) >= 2  # Non-cached calls on failed attempt logged durably!

    # Resume run: episode 0 stays cached, fail_eid reruns cleanly, team_logs has NO duplicate rows,
    # AND executed_calls.jsonl + team_logs executed_* retain prior provider_error executed compute!
    c_par2: dict[str, int] = defaultdict(int)
    recs_2 = asyncio.run(
        run_study_v2.run_cell_in_stratified_blocks(
            "T14_Bossless_3Pipe_Jury",
            sub_pairs,
            suite=SplitRingV41.v41_dev,
            out_dir=tmp_path,
            cache_store=cache,
            provider_builder=_make_builder(oracles_map, c_par2),
            allow_offline_mock=True,
        )
    )
    assert all(r.execution_valid and not r.provider_error and r.scorecard.g_srr == 1.0 for r in recs_2)
    team_log_lines = [
        json.loads(line)
        for line in (tmp_path / "team_logs" / "T14_Bossless_3Pipe_Jury.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    # [P2-08] Deduplicated by episode_id on resume, while carrying forward prior failed attempt's executed tokens!
    assert len(team_log_lines) == 2
    assert {row["episode_id"] for row in team_log_lines} == {ep.episode_id for ep, _ in sub_pairs}
    resumed_row = next(row for row in team_log_lines if row["episode_id"] == fail_eid)
    assert resumed_row["executed_input_tokens"] > resumed_row["input_tokens"]
    exec_lines_after_resume = [
        json.loads(line)
        for line in (tmp_path / "executed_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(exec_lines_after_resume) > len(exec_lines_before_resume)


def test_failure_taxonomy_max_turns_parse_failure_and_manifest_suite_lock(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    ep, ora = dev_pairs[0]
    oracles_map = {ep.episode_id: ora}

    # 1. MAX_TURNS_EXCEEDED (`turns_used == 12`, `turn_truncated == True`)
    c_loop: dict[str, int] = defaultdict(int)

    def _loop_forever(
        _mk: str,
        _temp: float | None,
        call_num: int,
        _tag: str,
        _stage: dict[str, Any],
    ) -> ModelResponse:
        return ModelResponse(
            text="",
            tool_calls=(
                ToolCall(id=f"poll_{call_num}", name="poll_interrupts", arguments={"wave_index": 1}),
            ),
            usage=Usage(input_tokens=50, output_tokens=10),
            latency_s=0.1,
        )

    rec_loop, _ = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="qwen-local",
            arm_id="C_S2_prog_t0",
            cell_id="qwen-local__C_S2_prog_t0",
            provider_builder=_make_builder(oracles_map, c_loop, _loop_forever),
            allow_offline_mock=True,
        )
    )
    assert rec_loop.turns_used == 12
    assert rec_loop.turn_truncated is True
    assert rec_loop.parse_failure is False
    assert rec_loop.termination_reason == "MAX_TURNS_EXCEEDED"

    # 1b. [P3] Turn-12 first-time unparsed output (`consecutive_parse_failures == 1` at cap) -> MAX_TURNS_EXCEEDED!
    c_t12: dict[str, int] = defaultdict(int)

    def _turn12_unparsed(
        _mk: str,
        _temp: float | None,
        call_num: int,
        _tag: str,
        _stage: dict[str, Any],
    ) -> ModelResponse:
        if call_num < 12:
            return ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(id=f"p_{call_num}", name="poll_interrupts", arguments={"wave_index": 1}),
                ),
                usage=Usage(input_tokens=50, output_tokens=10),
                latency_s=0.1,
            )
        return ModelResponse(text="not-json", tool_calls=(), usage=Usage(input_tokens=50, output_tokens=5), latency_s=0.1)

    rec_t12, _ = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="qwen-local",
            arm_id="C_S2_prog_t0",
            cell_id="qwen-local__C_S2_prog_t0",
            provider_builder=_make_builder(oracles_map, c_t12, _turn12_unparsed),
            allow_offline_mock=True,
        )
    )
    assert rec_t12.turns_used == 12
    assert rec_t12.parse_failure is False
    assert rec_t12.turn_truncated is True
    assert rec_t12.termination_reason == "MAX_TURNS_EXCEEDED"

    # 1c. [P3] `_git_repo_state()` fails closed (`git_dirty == True`) when git fails!
    with patch("run_study_v2.subprocess.run", side_effect=OSError("git missing")):
        g_sha, g_dirty = run_study_v2._git_repo_state()
    assert g_sha == "unknown" and g_dirty is True

    # 2. [P1-12] Team PARSE_FAILURE (`refused=True` stops < 12 turns with `turn_truncated=False`!)
    c_ref: dict[str, int] = defaultdict(int)

    def _refuse_hook(
        _mk: str,
        _temp: float | None,
        _call_num: int,
        _tag: str,
        _stage: dict[str, Any],
    ) -> ModelResponse:
        return ModelResponse(
            text="I cannot perform this action.",
            refused=True,
            usage=Usage(input_tokens=80, output_tokens=10),
            latency_s=0.1,
        )

    rec_ref, meta_ref = asyncio.run(
        run_study_v2.run_team_episode(
            "T03_Hybrid_Sol_Qwen27B",
            ep,
            ora,
            cache_store=None,
            provider_builder=_make_builder(oracles_map, c_ref, _refuse_hook),
            allow_offline_mock=True,
        )
    )
    assert rec_ref.execution_valid is True
    assert rec_ref.parse_failure is True
    assert rec_ref.turn_truncated is False
    assert rec_ref.termination_reason == "PARSE_FAILURE"
    assert meta_ref["parse_failure"] is True

    # 3. [P1-04] write_or_verify_run_manifest locks suite and rejects cross-suite reuse!
    run_study_v2.write_or_verify_run_manifest(tmp_path, SplitRingV41.v41_dev, selected_episodes=2)
    with pytest.raises(ValueError, match="locked to suite"):
        run_study_v2.write_or_verify_run_manifest(tmp_path, SplitRingV41.v41_lite)

    # 4. CLI refuses without --live, redacts fatal main() errors, and --preflight spends 0 tokens
    with pytest.raises(PermissionError):
        asyncio.run(run_study_v2.async_main(["--suite", "v41_dev", "--out-dir", str(tmp_path)]))

    with patch(
        "run_study_v2.async_main",
        side_effect=PermissionError("Blocked https://secret-endpoint.internal:8443/v1 sk-1234567890abcdef"),
    ):
        rc_err = run_study_v2.main(["--suite", "v41_dev"])
    err_out = capsys.readouterr().err
    assert rc_err == 1
    assert "secret-endpoint.internal" not in err_out
    assert "sk-1234567890abcdef" not in err_out

    with (
        patch("run_study_v2._load_dotenv_if_present"),
        patch(
            "run_study_v2.probe_local_llm_server",
            return_value={
                "reachable": False,
                "served_models": [],
                "selected_model": "",
                "recommended_concurrency": 0,
                "t0_source": "unavailable",
            },
        ),
    ):
        rc = asyncio.run(
            run_study_v2.async_main(
                ["--suite", "v41_dev", "--preflight", "--out-dir", str(tmp_path)]
            )
        )
    assert rc == 0
    pf = json.loads((tmp_path / "preflight.json").read_text(encoding="utf-8"))
    assert pf["model_tokens_spent"] == 0
    assert pf["suite"] == "v41_dev"
    # [P2-06] Default cells on v41_dev resolves to dev_calibration (21 cells, not full 41-cell matrix!)
    assert pf["requested_cells"] == run_study_v2.dev_calibration_cell_ids()
    assert len(pf["requested_cells"]) == 21 < len(run_study_v2.all_cell_ids(SplitRingV41.v41_lite))


def test_import_existing_s0_baseline_blocks_metered_cost_wrong_provider_and_flash_fallback(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub_pairs = dev_pairs[:2]
    src_dir = tmp_path / "fake_lite_sub_records"
    out_dir = tmp_path / "out"
    src_dir.mkdir(parents=True)

    oracles_map = {ep.episode_id: ora for ep, ora in sub_pairs}
    counts: dict[str, int] = defaultdict(int)
    valid_recs: list[dict[str, Any]] = []
    for ep, ora in sub_pairs:
        rec, _ = asyncio.run(
            run_study_v2.run_single_agent_control_episode(
                ep,
                ora,
                model_key="gpt-6.1-sol",
                arm_id="C_S0_no_skill_t0",
                cell_id="gpt-6.1-sol-t0-medium",
                provider_builder=_make_builder(oracles_map, counts),
                allow_offline_mock=True,
            )
        )
        d = rec.model_dump(mode="json")
        d["provider_kind"] = "codex_cli"
        valid_recs.append(d)

    src_file = src_dir / "gpt-6.1-sol-t0-medium.jsonl"
    src_file.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in valid_recs) + "\n",
        encoding="utf-8",
    )

    imported = run_study_v2.import_existing_s0_baseline_cell(
        "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
    )
    assert len(imported) == 2
    assert all(r.model_id == "gpt-6.1-sol__C_S0_no_skill_t0" for r in imported)
    # [P2-03] Imported baseline writes team_logs with imported_baseline=True and 0 executed tokens!
    imp_logs = [
        json.loads(line)
        for line in (out_dir / "team_logs" / "gpt-6.1-sol__C_S0_no_skill_t0.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(imp_logs) == 2 and all(row["imported_baseline"] is True for row in imp_logs)

    # [R2-P0-01] Subscription CLI (`claude_cli`) notional `reported_cost_usd > 0` SUCCEEDS, normalizes
    # `reported_cost_usd -> None`, and records `cli_notional_usd` in `team_logs`!
    claude_file = src_dir / "claude-opus-5-5-t0-medium.jsonl"
    claude_recs: list[dict[str, Any]] = []
    for r_dict in valid_recs:
        cr = copy.deepcopy(r_dict)
        cr["model_id"] = "claude-opus-5-5-t0-medium"
        cr["provider_kind"] = "claude_cli"
        cr["reported_cost_usd"] = 0.42
        cr["error_message"] = "Note https://internal-cli-host.local:8443/v1"
        claude_recs.append(cr)
    claude_file.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in claude_recs) + "\n",
        encoding="utf-8",
    )
    imp_claude = run_study_v2.import_existing_s0_baseline_cell(
        "claude-opus-5-5", sub_pairs, out_dir, source_records_dir=src_dir
    )
    assert len(imp_claude) == 2
    assert all(r.reported_cost_usd is None for r in imp_claude)
    assert all(
        r.error_message is not None and "internal-cli-host.local" not in r.error_message
        for r in imp_claude
    )
    claude_logs = [
        json.loads(line)
        for line in (out_dir / "team_logs" / "claude-opus-5-5__C_S0_no_skill_t0.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(claude_logs) == 2 and all(row["cli_notional_usd"] == 0.42 for row in claude_logs)

    # [R2-P0-01] Conversely, `openai_compatible` (`qwen-local`) with `reported_cost_usd > 0` raises PermissionError!
    monkeypatch.setitem(
        run_study_v2.MODEL_SPECS["qwen-local"], "t0_source", "qwen3.8-27b-exl3-4bit-local"
    )
    qwen_27b_file = src_dir / "qwen3.8-27b-exl3-4bit-local.jsonl"
    billed_non_cli = copy.deepcopy(valid_recs[0])
    billed_non_cli["provider_kind"] = "openai_compatible"
    billed_non_cli["reported_cost_usd"] = 0.42
    qwen_27b_file.write_text(json.dumps(billed_non_cli) + "\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="reported_cost_usd"):
        run_study_v2.import_existing_s0_baseline_cell(
            "qwen-local", sub_pairs, out_dir, source_records_dir=src_dir
        )

    # [P1-01] Wrong provider_kind (e.g. openai_compatible for a CLI model) must raise PermissionError!
    wrong_prov = copy.deepcopy(valid_recs[0])
    wrong_prov["provider_kind"] = "openai_compatible"
    src_file.write_text(json.dumps(wrong_prov) + "\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="provider_kind"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )

    # [R2-P2-05] Protocol assertions on imported S0 baseline (`turns_used <= 12`, `T0`, `UNSTEERED`, `COUPLED`)
    over_turns = copy.deepcopy(valid_recs[0])
    over_turns["turns_used"] = 13
    src_file.write_text(json.dumps(over_turns) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="G-TURN-01"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )

    t3_leak = copy.deepcopy(valid_recs[0])
    t3_leak["scorecard"]["checker_calls"] = 1
    src_file.write_text(json.dumps(t3_leak) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="T0_SINGLE_MONOLITH"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )

    steer_leak = copy.deepcopy(valid_recs[0])
    steer_leak["scorecard"]["steer_calls"] = 1
    src_file.write_text(json.dumps(steer_leak) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="UNSTEERED"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )

    reset_leak = copy.deepcopy(valid_recs[0])
    reset_leak["scorecard"]["wave_execution_mode"] = "DIAGNOSTIC_RESET"
    src_file.write_text(json.dumps(reset_leak) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="wave_execution_mode"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )

    # [P1-01] qwen-local NEVER falls back to qwen3.8-flash-next-local.jsonl!
    (src_dir / "qwen3.8-flash-next-local.jsonl").write_text(
        json.dumps(valid_recs[0]) + "\n", encoding="utf-8"
    )
    monkeypatch.setitem(
        run_study_v2.MODEL_SPECS["qwen-local"], "t0_source", "qwen3.8-flash-next-local"
    )
    assert (
        run_study_v2.import_existing_s0_baseline_cell(
            "qwen-local", sub_pairs, out_dir, source_records_dir=src_dir
        )
        == []
    )

    # Tampered episode_sha256 must raise ValueError!
    tampered = copy.deepcopy(valid_recs[0])
    tampered["episode_sha256"] = "0" * 64
    src_file.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="episode_sha256 mismatch"):
        run_study_v2.import_existing_s0_baseline_cell(
            "gpt-6.1-sol", sub_pairs, out_dir, source_records_dir=src_dir
        )


def test_live_cli_notional_vs_metered_guard_singleflight_cache_and_malformed_retry(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
) -> None:
    from identitybench.providers.claude_cli import ClaudeCliProvider

    ep, ora = dev_pairs[0]

    # 1. [R2-P0-01] Real `ClaudeCliProvider` (`isinstance(..., CliProvider)`) returning CLI notional
    #    `reported_cost_usd > 0` records `cli_notional_usd` and leaves metered spend at `0.0`!
    cli_prov = ClaudeCliProvider(model="claude-opus-5-5")
    valid_cli_json = json.dumps(
        {
            "thought": "read",
            "actions": [{"action": "poll_interrupts", "arguments_json": '{"wave_index": 1}'}],
        }
    )
    fake_cli_resp = ModelResponse(
        text=valid_cli_json,
        usage=Usage(input_tokens=300, output_tokens=50, cache_write_tokens=100),
        latency_s=0.2,
        reported_cost_usd=0.37,
    )
    with (
        patch.object(ClaudeCliProvider, "endpoint_identity", return_value="claude_cli:mock"),
        patch.object(ClaudeCliProvider, "complete", return_value=fake_cli_resp),
    ):
        acct_cli = run_study_v2.EpisodeAccounting(
            budget_regime="G_TURN_12_STRICT", max_llm_completions=12, max_env_turns=12
        )
        cache_cli = run_study_v2.StudyCacheStore(tmp_path / "cli_cache")
        resp_c, calls_c = asyncio.run(
            run_study_v2._call_tracked_single(
                model_key="claude-opus-5-5",
                seat_role="worker",
                provider=cli_prov,
                messages=[Message(role=Role.user, content="hello")],
                params=GenerationParams(temperature=0.0),
                turn_idx=0,
                acct=acct_cli,
                suite_value="v41_dev",
                cell_id="claude-opus-5-5__C_S0_no_skill_t0",
                episode_id=ep.episode_id,
                cache_store=cache_cli,
                allow_offline_mock=False,
            )
        )
    assert len(calls_c) == 1 and resp_c.ok
    assert acct_cli.reported_cost_usd == 0.0
    assert acct_cli.cli_notional_usd == pytest.approx(0.37)

    # Conversely, non-CLI mock (`kind='mock'`, not `CliProvider`) returning `reported_cost_usd > 0` raises PermissionError!
    class _BilledNonCliMock(ScriptedStudyMockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            base = await super().complete(request)
            return ModelResponse(
                text=base.text,
                tool_calls=base.tool_calls,
                usage=base.usage,
                latency_s=base.latency_s,
                reported_cost_usd=0.01,
            )

    billed_prov = _BilledNonCliMock("qwen-local", 0.0, {ep.episode_id: ora}, defaultdict(int))
    acct_bad = run_study_v2.EpisodeAccounting(
        budget_regime="G_TURN_12_STRICT", max_llm_completions=12, max_env_turns=12
    )
    with pytest.raises(PermissionError, match="Metered non-CLI spend"):
        asyncio.run(
            run_study_v2._call_tracked_single(
                model_key="qwen-local",
                seat_role="worker",
                provider=billed_prov,
                messages=[Message(role=Role.user, content=ep.episode_id)],
                params=GenerationParams(temperature=0.0),
                turn_idx=0,
                acct=acct_bad,
                suite_value="v41_dev",
                cell_id="qwen-local__C_S0_no_skill_t0",
                episode_id=ep.episode_id,
                allow_offline_mock=True,
            )
        )

    # 2. [R2-P1-05] Singleflight `StudyCacheStore`: 2 concurrent `temp=0.0` misses execute ONLY 1 live call!
    sf_calls = {"n": 0}

    class _SlowDeterministicProvider(ScriptedStudyMockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            sf_calls["n"] += 1
            await asyncio.sleep(0.03)
            return await super().complete(request)

    sf_cache = run_study_v2.StudyCacheStore(tmp_path / "sf_cache")
    sf_prov = _SlowDeterministicProvider("qwen-local", 0.0, {ep.episode_id: ora}, defaultdict(int))

    async def _run_concurrent_pair() -> tuple[bool, bool]:
        t1 = run_study_v2._invoke_model_once(
            sf_prov,
            [Message(role=Role.user, content=f"same prompt {ep.episode_id}")],
            GenerationParams(temperature=0.0),
            0,
            model_key="qwen-local",
            cache_store=sf_cache,
            allow_offline_mock=True,
        )
        t2 = run_study_v2._invoke_model_once(
            sf_prov,
            [Message(role=Role.user, content=f"same prompt {ep.episode_id}")],
            GenerationParams(temperature=0.0),
            0,
            model_key="qwen-local",
            cache_store=sf_cache,
            allow_offline_mock=True,
        )
        r1, r2 = await asyncio.gather(t1, t2)
        return r1[5], r2[5]

    hit1, hit2 = asyncio.run(_run_concurrent_pair())
    assert sf_calls["n"] == 1
    assert {hit1, hit2} == {False, True}

    # [R2-P1-05] `cell_config_fingerprint` changes when `endpoint_identity()` changes, and
    # `load_valid_cached_records` rejects forged `imported_s0:wrong-source` fingerprints!
    fp_native = run_study_v2.cell_config_fingerprint(
        "qwen-local__C_S2_prog_t0",
        SplitRingV41.v41_dev,
        provider_builder=_make_builder({ep.episode_id: ora}, defaultdict(int)),
    )

    def _alt_builder(mk: str, temp: float | None = None) -> tuple[Provider, GenerationParams, int]:
        p = ScriptedStudyMockProvider(mk, temp, {ep.episode_id: ora}, defaultdict(int), native_tools=False)
        return p, GenerationParams(temperature=temp), 4

    fp_alt = run_study_v2.cell_config_fingerprint(
        "qwen-local__C_S2_prog_t0",
        SplitRingV41.v41_dev,
        provider_builder=_alt_builder,
    )
    assert fp_native != fp_alt

    rec_sample, _ = asyncio.run(
        run_study_v2.run_single_agent_control_episode(
            ep,
            ora,
            model_key="gpt-6.1-sol",
            arm_id="C_S0_no_skill_t0",
            cell_id="gpt-6.1-sol__C_S0_no_skill_t0",
            provider_builder=_make_builder({ep.episode_id: ora}, defaultdict(int)),
            allow_offline_mock=True,
        )
    )
    forged_rec_path = tmp_path / "gpt-6.1-sol__C_S0_no_skill_t0.jsonl"
    forged = rec_sample.model_copy(update={"config_fingerprint": "imported_s0:forged-other-model"})
    forged_rec_path.write_text(json.dumps(forged.model_dump(mode="json")) + "\n", encoding="utf-8")
    ep_sha_map = {ep.episode_id: rec_sample.episode_sha256}
    assert (
        run_study_v2.load_valid_cached_records(
            forged_rec_path,
            ep_sha_map,
            expected_fingerprint="study_v2:gpt-6.1-sol__C_S0_no_skill_t0:deadbeef",
        )
        == {}
    )
    legit_imp = rec_sample.model_copy(
        update={"config_fingerprint": "imported_s0:gpt-6.1-sol-t0-medium"}
    )
    forged_rec_path.write_text(json.dumps(legit_imp.model_dump(mode="json")) + "\n", encoding="utf-8")
    assert ep.episode_id in run_study_v2.load_valid_cached_records(
        forged_rec_path,
        ep_sha_map,
        expected_fingerprint="study_v2:gpt-6.1-sol__C_S0_no_skill_t0:deadbeef",
    )

    # 3. [R2-P2-05] Malformed-CLI retry counts against `acct` (`+2` completions) while `StudyCacheStore`
    #    caches ONLY the final single-call usage (`input_tokens == 400`, NOT cumulative `800`)!
    malf_attempt = {"n": 0}

    class _MalformedOnceProvider(ScriptedStudyMockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            malf_attempt["n"] += 1
            if malf_attempt["n"] == 1:
                return ModelResponse(
                    text="",
                    error="malformed function call: bad json tool call",
                    usage=Usage(input_tokens=400, output_tokens=30),
                    latency_s=0.25,
                )
            return await super().complete(request)

    malf_cache = run_study_v2.StudyCacheStore(tmp_path / "malf_cache")
    malf_prov = _MalformedOnceProvider(
        "gemini-3.8-flash", 0.0, {ep.episode_id: ora}, defaultdict(int), native_tools=False
    )
    acct_malf = run_study_v2.EpisodeAccounting(
        budget_regime="G_TURN_12_STRICT", max_llm_completions=12, max_env_turns=12
    )
    asyncio.run(
        run_study_v2._call_tracked_single(
            model_key="gemini-3.8-flash",
            seat_role="worker",
            provider=malf_prov,
            messages=[Message(role=Role.user, content=f"wave 1 {ep.episode_id}")],
            params=GenerationParams(temperature=0.0),
            turn_idx=0,
            acct=acct_malf,
            suite_value="v41_dev",
            cell_id="gemini-3.8-flash__C_S0_no_skill_t0",
            episode_id=ep.episode_id,
            cache_store=malf_cache,
            allow_offline_mock=True,
        )
    )
    assert acct_malf.llm_completions_used == 2  # 1 malformed + 1 retry completion counted!
    assert acct_malf.total_usage.input_tokens == 800  # Both calls' tokens accounted in live run!
    assert len(malf_cache.response_cache) == 1
    cached_row = next(iter(malf_cache.response_cache.values()))
    assert cached_row["input_tokens"] == 400  # Cached entry stores ONLY final single-call usage!


# ===========================================================================
# 4. `build_report_v2.py` Tests (`[P1-04]`, `[P1-10]..[P1-13]`, `[P2-03]..[P2-07]`, `[P3-03]`)
# ===========================================================================


def test_ratio_cluster_bootstrap_unequal_clusters_and_pricing_rules() -> None:
    # [P2-05] Ratio-of-means cluster BCa CI must equal exact episode mean under unequal cluster sizes!
    unequal_clusters = {
        "F01": [1.0],
        "F02": [0.0, 0.0, 0.0, 0.0],
        "F03": [1.0],
        "F04": [0.0, 0.0],
    }
    expected_point = 2.0 / 8.0  # 0.25 (whereas unweighted mean-of-cluster-means would be 0.50!)
    pt, lo, hi = build_report_v2.family_cluster_ratio_bca_ci(
        unequal_clusters, n_resamples=2000, seed=20261111
    )
    assert pt == round(expected_point, 6)
    assert 0.0 <= lo <= pt <= hi <= 1.0

    # [P1-10] Paired family-cluster BCa delta CI
    raw_pairs = (
        ("ep1", "F01", 1.0, 0.0),
        ("ep2", "F01", 1.0, 0.0),
        ("ep3", "F02", 1.0, 0.0),
        ("ep4", "F02", 1.0, 1.0),
        ("ep5", "F03", 1.0, 0.0),
        ("ep6", "F03", 0.0, 0.0),
        ("ep7", "F04", 1.0, 0.0),
        ("ep8", "F04", 1.0, 0.0),
    )
    recs_a = [
        {
            "episode_id": eid,
            "family_id": fid,
            "execution_valid": True,
            "provider_error": False,
            "scorecard": {"g_srr": va},
        }
        for eid, fid, va, _ in raw_pairs
    ]
    recs_b = [
        {
            "episode_id": eid,
            "family_id": fid,
            "execution_valid": True,
            "provider_error": False,
            "scorecard": {"g_srr": vb},
        }
        for eid, fid, _, vb in raw_pairs
    ]
    d_ci = build_report_v2.paired_family_cluster_bca_delta_ci(
        recs_a,
        recs_b,
        target_episode_count=8,
        n_resamples=2000,
        seed=20261111,
    )
    assert d_ci["point"] == round(6.0 / 8.0, 6)
    assert d_ci["n_paired_episodes"] == 8
    assert d_ci["low"] > 0.0
    assert d_ci["headline_valid"] is True
    assert d_ci["statistically_nonzero_95"] is True

    # [R2-P1-04] Incomplete coverage or invalid cell gates `headline_valid == False` & `statistically_nonzero_95 == False`!
    recs_b_with_err = [
        dict(recs_b[0], execution_valid=False, provider_error=True),
        *recs_b[1:],
    ]
    d_ci_invalid = build_report_v2.paired_family_cluster_bca_delta_ci(
        recs_a,
        recs_b_with_err,
        target_episode_count=8,
        n_resamples=2000,
        seed=20261111,
    )
    assert d_ci_invalid["headline_valid"] is False
    assert d_ci_invalid["statistically_nonzero_95"] is False

    d_ci_partial = build_report_v2.paired_family_cluster_bca_delta_ci(
        recs_a,
        recs_b,
        target_episode_count=12,
        n_resamples=2000,
        seed=20261111,
    )
    assert d_ci_partial["headline_valid"] is False
    assert d_ci_partial["statistically_nonzero_95"] is False

    # [R2-P1-03] Pricing strictly from `pricing.yaml` (zero `FALLBACK_RETAIL_PRICING_PER_1M`!):
    # - `claude_cli` uses `1h` cache-write TTL (`2.0x` input) vs `5m` (`1.25x` input)
    # - `grok-4.7` applies `long_context` per-call tier above 200k prompt tokens
    # - `qwen-local` and subscription models absent from `pricing.yaml` (`claude-fable-5-2`) return `None`!
    assert not hasattr(build_report_v2, "FALLBACK_RETAIL_PRICING_PER_1M")
    assert not hasattr(build_report_v2, "FANOUT_CAPPED_24")
    pricing = build_report_v2.load_retail_pricing_table()
    std_cost = build_report_v2.compute_retail_token_equivalent_usd(
        "grok-4.7",
        input_tokens=100_000,
        output_tokens=10_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        calls=1,
        pricing_table=pricing,
    )
    long_cost = build_report_v2.compute_retail_token_equivalent_usd(
        "grok-4.7",
        input_tokens=250_000,  # exceeds grok-4.7 200k long_context threshold!
        output_tokens=10_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        calls=1,
        pricing_table=pricing,
    )
    assert std_cost is not None and long_cost is not None and long_cost > 2.5 * std_cost

    cw1h_cost = build_report_v2.compute_retail_token_equivalent_usd(
        "claude-opus-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=100_000,
        calls=1,
        provider_kind="claude_cli",  # 1h TTL ($8.00/1M = 0.80)
        pricing_table=pricing,
    )
    cw5m_cost = build_report_v2.compute_retail_token_equivalent_usd(
        "claude-opus-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=100_000,
        calls=1,
        provider_kind="other_5m",  # 5m TTL ($5.00/1M = 0.50)
        pricing_table=pricing,
    )
    assert cw1h_cost == pytest.approx(0.80)
    assert cw5m_cost == pytest.approx(0.50)
    assert cw1h_cost > cw5m_cost > 0.0

    # Self-hosted `qwen-local` -> None (priced separately via `local_gpu_tco_usd`)
    assert (
        build_report_v2.compute_retail_token_equivalent_usd(
            "qwen-local",
            input_tokens=500_000,
            output_tokens=50_000,
            pricing_table=pricing,
        )
        is None
    )
    # Subscription-only model absent from `pricing.yaml` (`claude-fable-5-2`) -> None + unpriced_tokens!
    assert (
        build_report_v2.compute_retail_token_equivalent_usd(
            "claude-fable-5-2",
            input_tokens=100_000,
            output_tokens=10_000,
            pricing_table=pricing,
        )
        is None
    )
    fable_usd, fable_priced_tok, fable_unpriced_tok, fable_local_tok = (
        build_report_v2._price_model_bucket_dict(
            "claude-fable-5-2",
            {"input_tokens": 100_000, "output_tokens": 10_000, "provider_kind": "claude_cli"},
            pricing,
        )
    )
    assert fable_usd is None and fable_priced_tok == 0 and fable_unpriced_tok == 110_000 and fable_local_tok == 0

    # [P1-04] Default --out on v41_dev never points at reports/v41-skill-orch-v2!
    assert (
        build_report_v2.default_report_out_dir("v41_dev")
        == REPO_ROOT / "reports" / "v41-skill-orch-v2-dev"
    )
    assert (
        build_report_v2.default_report_out_dir("v41_lite")
        == REPO_ROOT / "reports" / "v41-skill-orch-v2"
    )


def test_build_report_v2_end_to_end_deltas_u_exec_survivorship_seal_svg_and_xss(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    out_dir = tmp_path / "report_out"
    sub_pairs = dev_pairs[:4]  # 4 distinct family_id clusters
    oracles_map = {ep.episode_id: ora for ep, ora in sub_pairs}
    counts: dict[str, int] = defaultdict(int)
    cache = run_study_v2.StudyCacheStore(run_dir)

    # 1. Run `T03_Hybrid_Sol_Qwen27B` across 4 family clusters (writes suite-locked manifest.json!)
    asyncio.run(
        run_study_v2.run_cell_in_stratified_blocks(
            "T03_Hybrid_Sol_Qwen27B",
            sub_pairs,
            suite=SplitRingV41.v41_dev,
            out_dir=run_dir,
            cache_store=cache,
            provider_builder=_make_builder(oracles_map, counts),
            allow_offline_mock=True,
        )
    )
    gold_lines = [
        json.loads(line)
        for line in (run_dir / "records" / "T03_Hybrid_Sol_Qwen27B.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    # 2. Populate `qwen-local__C_S0_no_skill_t0` (lower G-SRR, plus pure STAGE34 miss with u_exec==0!)
    #    so paired_deltas (`T03 - qwen-local S0`) AND `[P1-13]` (`u_exec == 0` -> `committed_unsafe_rate == 0`) are tested!
    s0_rows: list[str] = []
    for idx, base_row in enumerate(gold_lines):
        row = copy.deepcopy(base_row)
        row["model_id"] = "qwen-local__C_S0_no_skill_t0"
        row["provider_kind"] = "openai_compatible"
        if idx >= 1:
            # Pure Stage 3/4 citation miss: g_srr=0, stage34=0, but u_exec=0 (zero unsafe action!)
            row["scorecard"]["g_srr"] = 0
            row["scorecard"]["g_min"] = 0.0
            row["scorecard"]["u_exec"] = 0
            row["scorecard"]["u_overgrant"] = 0
            row["scorecard"]["u_availability"] = 0
            row["scorecard"]["waves_commit"][0]["stage34"] = 0
            row["scorecard"]["waves_commit"][0]["g"] = 0.0
        s0_rows.append(json.dumps(row, sort_keys=True))
    (run_dir / "records" / "qwen-local__C_S0_no_skill_t0.jsonl").write_text(
        "\n".join(s0_rows) + "\n", encoding="utf-8"
    )

    # 2b. Populate `qwen-local__C_S2_prog_t0` and `qwen-local__C_S3_prog_val_t3` so skill-matched
    #     paired contrasts (`T03 - S2(T0)`, `T03 - S3(T3)`, `S3(T3) - S2(T0)`) are verified (`[R2-P1-04]`)!
    s2_rows: list[str] = []
    s3_rows: list[str] = []
    for idx, base_row in enumerate(gold_lines):
        r2 = copy.deepcopy(base_row)
        r2["model_id"] = "qwen-local__C_S2_prog_t0"
        r2["provider_kind"] = "openai_compatible"
        if idx >= 2:
            r2["scorecard"]["g_srr"] = 0
            r2["scorecard"]["g_min"] = 0.5
        s2_rows.append(json.dumps(r2, sort_keys=True))

        r3 = copy.deepcopy(base_row)
        r3["model_id"] = "qwen-local__C_S3_prog_val_t3"
        r3["provider_kind"] = "openai_compatible"
        if idx >= 3:
            r3["scorecard"]["g_srr"] = 0
            r3["scorecard"]["g_min"] = 0.5
        s3_rows.append(json.dumps(r3, sort_keys=True))
    (run_dir / "records" / "qwen-local__C_S2_prog_t0.jsonl").write_text(
        "\n".join(s2_rows) + "\n", encoding="utf-8"
    )
    (run_dir / "records" / "qwen-local__C_S3_prog_val_t3.jsonl").write_text(
        "\n".join(s3_rows) + "\n", encoding="utf-8"
    )

    # 3. Populate a 100% PROVIDER_ERROR cell (`[P1-11]` zero survivorship bias!)
    all_err_rows: list[str] = []
    for base_row in gold_lines:
        er = copy.deepcopy(base_row)
        er["model_id"] = "gpt-6.1-sol__C_S0_no_skill_t0"
        er["provider_kind"] = "codex_cli"
        er["execution_valid"] = False
        er["provider_error"] = True
        er["termination_reason"] = "PROVIDER_ERROR"
        er["error_message"] = "Timeout at https://private-box.local:9000/v1"
        all_err_rows.append(json.dumps(er, sort_keys=True))
    (run_dir / "records" / "gpt-6.1-sol__C_S0_no_skill_t0.jsonl").write_text(
        "\n".join(all_err_rows) + "\n", encoding="utf-8"
    )

    # 4. Populate an adversarial XSS + Markdown + C1 control-char cell (`[P2-07]`, `[P3-03]`)
    xss_cell_id = 'claude-fable-5-2__XSS_<script>alert("xss")</script>&"\'`[md]|'
    xss_rows: list[str] = []
    for idx, base_row in enumerate(gold_lines):
        row = copy.deepcopy(base_row)
        row["model_id"] = xss_cell_id
        row["provider_kind"] = "claude_cli"
        row["reported_cost_usd"] = 0.55  # [R2-P0-01] Subscription CLI notional cost -> allowed (0 metered spend)!
        if idx == 1:
            row["scorecard"]["g_srr"] = 0
            row["scorecard"]["g_min"] = 0.25
            row["scorecard"]["u_exec"] = 2
            row["scorecard"]["u_overgrant"] = 2
            row["turns_used"] = 13  # [R2-P0-02] > 12 turns -> headline_g_turn_01_compliant == False!
            row["turn_truncated"] = True
            row["termination_reason"] = "MAX_TURNS_EXCEEDED"
        elif idx == 2:
            row["execution_valid"] = False
            row["provider_error"] = True
            row["termination_reason"] = "PROVIDER_ERROR"
            row["error_message"] = (
                'Err\x85\ufffe <script>alert("err")</script> `[x]` at https://secret.host.local/v1'
            )
        elif idx == 3:
            row["scorecard"]["g_srr"] = 0
            row["parse_failure"] = True
            row["termination_reason"] = "PARSE_FAILURE"
        xss_rows.append(json.dumps(row, sort_keys=True))
    (run_dir / "records" / "xss_cell.jsonl").write_text(
        "\n".join(xss_rows) + "\n", encoding="utf-8"
    )

    summary = build_report_v2.build_study_v2_report(
        run_dir=run_dir,
        dataset_dir=DEV_DATASET_DIR,
        out_dir=out_dir,
        suite="v41_dev",
        allow_offline_mock=True,
    )

    assert summary["actual_billed_usd"] == 0.0
    assert summary["bootstrap"]["n_draws"] == 10_000
    assert summary["bootstrap"]["seed"] == 20261111
    assert "git_sha" in summary and isinstance(summary["git_dirty"], bool)
    assert "attributed_study_totals" in summary and "executed_study_totals" in summary

    by_cell = {c["cell_id"]: c for c in summary["cells"]}
    t03 = by_cell["T03_Hybrid_Sol_Qwen27B"]
    assert t03["actual_billed_usd"] == 0.0
    assert t03["g_srr"] == 1.0
    assert t03["headline_eligible"] is True
    assert t03["headline_g_turn_01_compliant"] is True
    assert t03["safety_screen"]["passed_all"] is True
    assert t03["local_gpu_seconds"] > 0.0

    # [P1-13] & [R2-P1-03] Pure STAGE34 provenance miss has committed_unsafe_rate == 0.0,
    # and pure self-hosted `qwen-local` has `retail_token_equivalent_usd is None` (`self_hosted_local`)!
    qwen_s0 = by_cell["qwen-local__C_S0_no_skill_t0"]
    assert qwen_s0["g_srr"] == 0.25
    assert qwen_s0["committed_unsafe_rate"] == 0.0
    assert qwen_s0["stage34_provenance_miss_rate"] > 0.0
    assert qwen_s0["retail_token_equivalent_usd"] is None
    assert qwen_s0["priced_coverage"] == "self_hosted_local"

    # [P1-11] 100% provider_error cell has g_srr=0, committed_unsafe_rate=1.0, headline_eligible=False, sorts last!
    sol_err = by_cell["gpt-6.1-sol__C_S0_no_skill_t0"]
    assert sol_err["execution_valid_count"] == 0
    assert sol_err["g_srr"] == 0.0
    assert sol_err["committed_unsafe_rate"] == 1.0
    assert sol_err["headline_eligible"] is False
    assert sol_err["safety_screen"]["passed_all"] is False
    assert summary["cells"][0]["headline_eligible"] is True
    assert summary["cells"][-1]["headline_eligible"] is False

    # [P1-10] & [R2-P1-04] Paired family-cluster BCa delta CIs present for S0, S2(T0), S3(T3), and single-agent S3-S2,
    # AND contrast against provider_error cell (`sol_err`) has `headline_valid == False` & `statistically_nonzero_95 == False`!
    assert any(
        d["cell_a"] == "T03_Hybrid_Sol_Qwen27B"
        and d["cell_b"] == "qwen-local__C_S0_no_skill_t0"
        and d["point"] == 0.75
        and d["headline_valid"] is True
        for d in summary["paired_deltas"]
    )
    assert any(
        d["cell_a"] == "T03_Hybrid_Sol_Qwen27B"
        and d["cell_b"] == "qwen-local__C_S2_prog_t0"
        and d["point"] == 0.50
        and d["headline_valid"] is True
        for d in summary["paired_deltas"]
    )
    assert any(
        d["cell_a"] == "T03_Hybrid_Sol_Qwen27B"
        and d["cell_b"] == "qwen-local__C_S3_prog_val_t3"
        and d["point"] == 0.25
        and d["headline_valid"] is True
        for d in summary["paired_deltas"]
    )
    assert any(
        d["cell_a"] == "qwen-local__C_S3_prog_val_t3"
        and d["cell_b"] == "qwen-local__C_S2_prog_t0"
        and d["point"] == 0.25
        and d["headline_valid"] is True
        for d in summary["paired_deltas"]
    )
    err_delta = next(
        d
        for d in summary["paired_deltas"]
        if d["cell_a"] == "T03_Hybrid_Sol_Qwen27B"
        and d["cell_b"] == "gpt-6.1-sol__C_S0_no_skill_t0"
    )
    assert err_delta["headline_valid"] is False
    assert err_delta["statistically_nonzero_95"] is False

    xss_c = by_cell[xss_cell_id]
    assert xss_c["execution_valid_count"] == 3
    assert xss_c["provider_error_count"] == 1
    assert xss_c["parse_failure_count"] == 1
    assert xss_c["turn_truncated_count"] == 1
    assert xss_c["headline_g_turn_01_compliant"] is False
    assert xss_c["cli_notional_usd"] == pytest.approx(4 * 0.55)
    assert xss_c["retail_token_equivalent_usd"] is None  # Fable 5.2 is unpriced in pricing.yaml!
    assert xss_c["unpriced_tokens"] > 0
    assert xss_c["priced_coverage"] == "unpriced_subscription"
    assert xss_c["committed_unsafe_rate"] > 0.0
    assert xss_c["headline_eligible"] is False
    assert all("secret.host.local" not in s for s in xss_c["redacted_error_samples"])
    assert all("\x85" not in s and "\ufffe" not in s for s in xss_c["redacted_error_samples"])

    # Every generated SVG must parse cleanly as strict XML
    svg_files = sorted((out_dir / "charts").glob("*.svg"))
    assert len(svg_files) == 8
    for svg_path in svg_files:
        root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
        assert root.tag.endswith("svg")

    # HTML and Markdown must escape all `<script>`, backticks, brackets, and URLs
    html_body = (out_dir / "index.html").read_text(encoding="utf-8")
    md_body = (out_dir / "report.md").read_text(encoding="utf-8")
    assert '<script>alert("xss")</script>' not in html_body
    assert '<script>alert("err")</script>' not in html_body
    assert "secret.host.local" not in html_body
    assert "Paired Family-Cluster BCa" in html_body
    assert '<script>alert("xss")</script>' not in md_body
    assert "secret.host.local" not in md_body
    assert "Paired Family-Cluster BCa" in md_body

    # [R2-P0-01] Non-zero reported_cost_usd on `openai_compatible` MUST raise ValueError in build_study_v2_report!
    billed_row = copy.deepcopy(gold_lines[0])
    billed_row["model_id"] = "qwen-local__C_S1_monolith_t0"
    billed_row["provider_kind"] = "openai_compatible"
    billed_row["reported_cost_usd"] = 0.05
    bad_cost_file = run_dir / "records" / "billed_leak.jsonl"
    bad_cost_file.write_text(json.dumps(billed_row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Non-zero metered API spend"):
        build_report_v2.build_study_v2_report(
            run_dir=run_dir,
            dataset_dir=DEV_DATASET_DIR,
            out_dir=out_dir,
            suite="v41_dev",
            allow_offline_mock=True,
        )
    bad_cost_file.unlink()

    # [R2-P0-01] Disallowed `provider_kind` (`anthropic`) MUST raise ValueError in build_study_v2_report!
    bad_prov_row = copy.deepcopy(gold_lines[0])
    bad_prov_row["model_id"] = "claude-opus-5-5__C_S1_monolith_t0"
    bad_prov_row["provider_kind"] = "anthropic"
    bad_prov_file = run_dir / "records" / "bad_prov.jsonl"
    bad_prov_file.write_text(json.dumps(bad_prov_row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Disallowed provider_kind"):
        build_report_v2.build_study_v2_report(
            run_dir=run_dir,
            dataset_dir=DEV_DATASET_DIR,
            out_dir=out_dir,
            suite="v41_dev",
            allow_offline_mock=True,
        )
    bad_prov_file.unlink()

    # Default `allow_offline_mock=False` also fails closed on `"mock"` records!
    with pytest.raises(ValueError, match="Disallowed provider_kind"):
        build_report_v2.build_study_v2_report(
            run_dir=run_dir,
            dataset_dir=DEV_DATASET_DIR,
            out_dir=out_dir,
            suite="v41_dev",
        )

    # [R2-P2-01] Tampered `manifest["oracles_sha256"]` MUST raise ValueError!
    manifest_path = run_dir / "manifest.json"
    orig_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad_manifest = dict(orig_manifest, oracles_sha256="f" * 64)
    manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="oracles_sha256 mismatch"):
        build_report_v2.build_study_v2_report(
            run_dir=run_dir,
            dataset_dir=DEV_DATASET_DIR,
            out_dir=out_dir,
            suite="v41_dev",
            allow_offline_mock=True,
        )
    manifest_path.write_text(json.dumps(orig_manifest), encoding="utf-8")

    # [R2-P2-01] Partial `v41_lite` run (`2 < 60` episodes) sets `headline_eligible == False`!
    lite_pairs_all = [
        (
            EpisodeSpecV41.model_validate_json(el),
            SealedEpisodeOracleV41.model_validate_json(ol),
        )
        for el, ol in zip(
            (REPO_ROOT / "datasets" / "v41_lite" / "episodes.jsonl").read_text(encoding="utf-8").splitlines()[:2],
            (REPO_ROOT / "datasets" / "v41_lite" / "oracles.jsonl").read_text(encoding="utf-8").splitlines()[:2],
            strict=True,
        )
    ]
    lite_run_dir = tmp_path / "partial_lite_run"
    lite_out_dir = tmp_path / "partial_lite_report"
    lite_oras = {ep.episode_id: ora for ep, ora in lite_pairs_all}
    asyncio.run(
        run_study_v2.run_cell_in_stratified_blocks(
            "T01_Local_ComputeTiered_27B",
            lite_pairs_all,
            suite=SplitRingV41.v41_lite,
            out_dir=lite_run_dir,
            cache_store=run_study_v2.StudyCacheStore(lite_run_dir),
            provider_builder=_make_builder(lite_oras, defaultdict(int)),
            allow_offline_mock=True,
        )
    )
    lite_partial_summary = build_report_v2.build_study_v2_report(
        run_dir=lite_run_dir,
        dataset_dir=REPO_ROOT / "datasets" / "v41_lite",
        out_dir=lite_out_dir,
        suite="v41_lite",
        allow_offline_mock=True,
    )
    assert lite_partial_summary["dataset_episode_count"] == 60
    assert lite_partial_summary["target_episode_count"] == 60
    assert lite_partial_summary["cells"][0]["episodes_total"] == 2
    assert lite_partial_summary["cells"][0]["headline_eligible"] is False

    # [P1-04] Suite mismatch against manifest.json MUST raise ValueError!
    with pytest.raises(ValueError, match="suite mismatch"):
        build_report_v2.build_study_v2_report(
            run_dir=run_dir,
            dataset_dir=REPO_ROOT / "datasets" / "v41_lite",
            out_dir=out_dir,
            suite="v41_lite",
            allow_offline_mock=True,
        )


def test_two_wave_parallel_cells_pipe_limiter_non_deadlock_and_llama_server_props_slots(
    dev_pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # 1. `probe_local_llm_server` caps `recommended_concurrency` via `{root}/props` `total_slots`
    #    and ignores HTTP 404 on `/props` (vLLM), using `ProxyHandler({})` + `_NoRedirectHandler`.
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("HTTP_PROXY", "http://evil-proxy.example.com:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil-proxy.example.com:8080")

    class _FakeResp(io.BytesIO):
        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    seen_urls: list[str] = []

    def _fake_open_slots_1(
        _self: urllib.request.OpenerDirector,
        req: urllib.request.Request,
        timeout: float = 5.0,
    ) -> _FakeResp:
        assert timeout > 0
        seen_urls.append(req.full_url)
        if req.full_url.endswith("/v1/models"):
            return _FakeResp(
                json.dumps({"data": [{"id": "qwen3.8-flash-next-iq3_xxs"}]}).encode("utf-8")
            )
        if req.full_url.endswith("/props"):
            return _FakeResp(json.dumps({"total_slots": 1}).encode("utf-8"))
        raise AssertionError(f"Unexpected probe URL: {req.full_url}")

    with patch.object(urllib.request.OpenerDirector, "open", _fake_open_slots_1):
        probe_1slot = allowlist.probe_local_llm_server(timeout_s=1.0)

    assert seen_urls == ["http://127.0.0.1:8080/v1/models", "http://127.0.0.1:8080/props"]
    assert probe_1slot["selected_model"] == "qwen3.8-flash-next-iq3_xxs"
    assert probe_1slot["recommended_concurrency"] == 1
    assert probe_1slot["server_total_slots"] == 1

    def _fake_open_vllm_404(
        _self: urllib.request.OpenerDirector,
        req: urllib.request.Request,
        timeout: float = 5.0,
    ) -> _FakeResp:
        assert timeout > 0
        if req.full_url.endswith("/v1/models"):
            return _FakeResp(
                json.dumps({"data": [{"id": "qwen3.8-flash-next-iq3_xxs"}]}).encode("utf-8")
            )
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", http.client.HTTPMessage(), None)

    with patch.object(urllib.request.OpenerDirector, "open", _fake_open_vllm_404):
        probe_vllm = allowlist.probe_local_llm_server(timeout_s=1.0)
    assert probe_vllm["recommended_concurrency"] == 2
    assert "server_total_slots" not in probe_vllm

    # 2. Wave A (Controls) -> Wave B (Teams) concurrent execution + `ProviderPipeLimiter(mock=1)`
    #    non-deadlock stress test (even with strict limit=1 on nested O5/O7 teams!).
    sub_pairs = dev_pairs[:2]
    oracles_map = {ep.episode_id: ora for ep, ora in sub_pairs}
    call_order: list[str] = []
    inflight = {"cur": 0, "peak": 0}

    class _TrackedPipeProvider(ScriptedStudyMockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            inflight["cur"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["cur"])
            try:
                await asyncio.sleep(0.002)
                return await super().complete(request)
            finally:
                inflight["cur"] -= 1

    counts: dict[str, int] = defaultdict(int)

    def _tracked_builder(
        mk: str, temp: float | None = None
    ) -> tuple[Provider, GenerationParams, int]:
        eff_t = temp if temp is not None else (0.0 if mk == "qwen-local" else None)
        prov = _TrackedPipeProvider(mk, eff_t, oracles_map, counts)
        return prov, GenerationParams(temperature=eff_t), 4

    orig_run_cell = run_study_v2.run_cell_in_stratified_blocks

    async def _instrumented_run_cell(
        cell_id: str,
        pairs: list[tuple[EpisodeSpecV41, SealedEpisodeOracleV41]],
        **kwargs: Any,
    ) -> list[Any]:
        call_order.append(f"start:{cell_id}")
        res = await orig_run_cell(cell_id, pairs, **kwargs)
        call_order.append(f"done:{cell_id}")
        return res

    async def _drive_two_wave_with_limit_1() -> dict[str, list[Any]]:
        # Even with pipe limit = 1 across ALL leaf calls, nested O5 (`T02`) & O7 (`T14`) NEVER deadlock!
        run_study_v2.set_pipe_limiter_for_current_loop(
            run_study_v2.ProviderPipeLimiter(overrides={"mock": 1})
        )
        cache = run_study_v2.StudyCacheStore(tmp_path / "two_wave_run")
        # Interleave team IDs before control IDs in input list to verify Wave A always runs before Wave B!
        mixed_cells = [
            "T02_Local_vLLM_10Shard_Swarm",
            "qwen-local__C_S2_prog_t0",
            "T14_Bossless_3Pipe_Jury",
            "gpt-6.1-sol__C_S0_no_skill_t0",
        ]
        with patch.object(
            run_study_v2, "run_cell_in_stratified_blocks", side_effect=_instrumented_run_cell
        ):
            return await run_study_v2.run_cells_two_wave(
                mixed_cells,
                sub_pairs,
                suite=SplitRingV41.v41_dev,
                out_dir=tmp_path / "two_wave_run",
                cache_store=cache,
                provider_builder=_tracked_builder,
                allow_offline_mock=True,
            )

    res_by_cell = asyncio.run(_drive_two_wave_with_limit_1())
    assert set(res_by_cell) == {
        "qwen-local__C_S2_prog_t0",
        "gpt-6.1-sol__C_S0_no_skill_t0",
        "T02_Local_vLLM_10Shard_Swarm",
        "T14_Bossless_3Pipe_Jury",
    }
    for cid, recs in res_by_cell.items():
        assert len(recs) == 2 and all(r.scorecard.g_srr == 1.0 for r in recs), cid

    # `ProviderPipeLimiter(overrides={"mock": 1})` strictly enforced peak leaf concurrency == 1
    assert inflight["peak"] == 1

    # Both Wave A controls finished BEFORE any Wave B team started!
    latest_control_done = max(
        call_order.index("done:qwen-local__C_S2_prog_t0"),
        call_order.index("done:gpt-6.1-sol__C_S0_no_skill_t0"),
    )
    earliest_team_start = min(
        call_order.index("start:T02_Local_vLLM_10Shard_Swarm"),
        call_order.index("start:T14_Bossless_3Pipe_Jury"),
    )
    assert latest_control_done < earliest_team_start

    out_stdout = capsys.readouterr().out
    assert "[Wave Start] Wave A (Controls)" in out_stdout
    assert "[Wave Start] Wave B (Teams)" in out_stdout
    assert "[Progress] qwen-local__C_S2_prog_t0" in out_stdout
    assert "term=EPISODE_COMPLETED" in out_stdout
