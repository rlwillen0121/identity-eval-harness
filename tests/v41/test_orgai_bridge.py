# ruff: noqa: E501
"""Free transport and actual-OrgAI-adapter tests. Never start a live model."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest

from identitybench.providers.base import Message, ModelRequest, Role
from identitybench.v41.orgai_bridge import (
    OrgAIProvider,
    disabled_integrations,
    validate_action_turn,
)

ROOT = Path(__file__).resolve().parents[2]
ACTION = {"actions": [{"action": "read_wave_evidence", "arguments_json": '{"wave_index":1}'}]}


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"actions": []},
        {"actions": [ACTION["actions"][0]] * 5},
        {"actions": [{"action": "read_wave_evidence", "arguments_json": "[]"}]},
        {"actions": [{"action": "shell", "arguments_json": "{}"}]},
    ],
)
def test_action_bounds(value: Any) -> None:
    with pytest.raises(ValueError):
        validate_action_turn(value, {"read_wave_evidence"})


def test_action_array_order() -> None:
    assert validate_action_turn(ACTION, {"read_wave_evidence"}) == ACTION


def test_inherited_profile_collision_and_secret_nonread(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text("not valid JSON and never read")
    (tmp_path / "config.toml").write_text('[mcp_servers.private]\nurl="secret-url"\n')
    assert disabled_integrations(tmp_path) == {"mcp": {"private": None}, "plugins": {}}
    (tmp_path / "config.toml").write_text('[permissions.identitybench]\nextends=":workspace"')
    with pytest.raises(ValueError, match="collision"):
        disabled_integrations(tmp_path)


class FakeTransport:
    runtime_root = Path("/runtime")
    codex_home = Path("/auth-path-only")
    disabled: ClassVar[dict[str, Any]] = {}
    identity: ClassVar[dict[str, Any]] = {"source_sha256": "mock"}

    def __init__(self, result: Any):
        self.result = result
        self.payloads: list[dict[str, Any]] = []

    async def exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return {"text": json.dumps(self.result), "platform_turns": 3, "usage": None}


@pytest.mark.asyncio
async def test_cli_request_empty_tool_specs_and_budget() -> None:
    transport = FakeTransport(ACTION)
    provider = OrgAIProvider(transport, "v41_lite_001")  # type: ignore[arg-type]
    request = ModelRequest(system="public policy", messages=(Message(Role.user, "public obs"),))
    for _ in range(12):
        result = await provider.complete(request)
        assert result.parsed == ACTION
        assert result.raw and result.raw["usage_known"] is False
    assert (await provider.complete(request)).error == "ORGAI_BENCHMARK_TURN_LIMIT"
    assert len(transport.payloads) == 12
    assert transport.payloads[0]["op"] == "start"
    assert transport.payloads[1]["op"] == "send"
    assert "oracle" not in json.dumps(transport.payloads)


@pytest.mark.asyncio
async def test_parse_failure_separate_from_transport_failure() -> None:
    request = ModelRequest(system="public", messages=())
    bad_parse = OrgAIProvider(FakeTransport({"bad": "format"}), "v41_lite_001")  # type: ignore[arg-type]
    response = await bad_parse.complete(request)
    assert response.error is None and response.text == ""
    assert response.raw and response.raw["parse_failure"]
    failed = OrgAIProvider(
        FakeTransport(RuntimeError("private URL must not escape")), "v41_lite_001"
    )  # type: ignore[arg-type]
    response = await failed.complete(request)
    assert response.error == "ORGAI_RuntimeError"
    assert "private" not in str(response.raw)


def test_committed_config_and_baselines() -> None:
    spec = importlib.util.spec_from_file_location(
        "orgai_operator", ROOT / "reports/v41-lite/run_orgai_comparison.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.matched_config(ROOT / "experiments/v41-orgai-12.yaml")
    assert config["historical_cells"]["team"].startswith("team_gpt-6.1-sol_boss")
    assert module.terminations(
        [{"termination_reason": "PARSE_FAILURE"}, {"termination_reason": "PROVIDER_ERROR"}]
    ) == {"PARSE_FAILURE": 1, "PROVIDER_ERROR": 1}


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "read_delegate",
        "plan_complete_followup",
        "read_mutate",
        "read_complete",
        "read_limit",
        "solo",
        "empty_child",
        "interrupted_child",
        "failed_child",
        "stale_root",
        "bad_config",
    ],
)
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
def test_actual_orgai_adapter_mock_transport(tmp_path: Path, mode: str, model: str) -> None:
    """Exercise actual source spawnChild ordering with fake app-server; zero model calls."""
    runtime = os.environ.get("ORGAI_TEST_RUNTIME_ROOT")
    if not runtime:
        pytest.skip("Set ORGAI_TEST_RUNTIME_ROOT to test installed OrgAI source without models")
    script = tmp_path / "test.mjs"
    script.write_text(
        """
import {start,send,cleanup} from BRIDGE;
const MODE=TEST_MODE;
class Fake {
 constructor(o){this.o=o;this.hs=[];this.n=0;this.t=0;this.exited=new Promise(r=>this.exit=r);}
 async start(){return {};}
 onNotification(h){this.hs.push(h);return ()=>{};}
 onServerRequest(h){this.call=h;}
 onExit(){return ()=>{};}
 notify(){}
 close(){this.exit();}
 push(m,p){for(const h of this.hs)h(m,p);}
 async request(m,p={}){
  if(m==='config/read'){
   const features=Object.fromEntries(this.o.config.filter(x=>x.startsWith('features.')).map(x=>[x.split('.')[1].split('=')[0],false]));
   const cwd=this.o.cwd;
   return {config:{features:{...features,...(MODE==='bad_config'?{shell_tool:true}:{})},web_search:'disabled',project_doc_max_bytes:0,mcp_servers:{},plugins:{},permissions:{identitybench:{filesystem:{':root':'deny',':minimal':'read',[cwd]:'read'},network:{enabled:false}}}}};
  }
  if(m==='thread/start'){
   const names=p.dynamicTools.map(t=>t.name);
   if(this.n===0){
    for(const name of ['set_plan','orgai_update_plan','complete_plan','mark_blocked','spawn_agent']) if(!names.includes(name)) throw Error('missing tool '+name);
    if(!p.developerInstructions.includes('set_plan')) throw Error('missing plan instructions');
   }
   return {thread:{id:'th-'+(++this.n)},cwd:p.cwd,model:p.model,approvalPolicy:'never',activePermissionProfile:{id:'identitybench'},reasoningEffort:'medium',instructionSources:[]};
  }
  if(m==='turn/start'){
   if(p.threadId==='th-1' && !p.outputSchema) throw Error('root schema removed');
   const id='tu-'+(++this.t);setTimeout(()=>this.step(p.threadId,id),5);return {turn:{id}};
  }
  return {};
 }
 async step(threadId,id){
  this.push('turn/started',{threadId,turn:{id}});
  if(threadId==='th-1' && id==='tu-1'){
   const plan=await this.call('item/tool/call',{threadId,turnId:id,callId:'plan',tool:'set_plan',arguments:{goal:'IGA evidence analysis',steps:[{title:'Read evidence and delegate'}]}});
   if(!plan.success)throw Error('actual plan handler rejected');
   const update=await this.call('item/tool/call',{threadId,turnId:id,callId:'update',tool:'orgai_update_plan',arguments:{steps:[{id:'p1',status:'in_progress'}]}});
   if(!update.success)throw Error('actual plan update rejected');
  }
  if(MODE==='plan_complete_followup' && threadId==='th-1' && id==='tu-3'){
   const r=await this.call('item/tool/call',{threadId,turnId:id,callId:'complete-after-report',tool:'complete_plan',arguments:{summary:'Specialist analysis received'}});
   if(!r.success)throw Error('actual complete_plan failed');
  }
  if(MODE==='read_complete' && id==='tu-2') {
   try {await this.call('item/tool/call',{threadId,turnId:id,callId:'complete',tool:'complete_plan',arguments:{summary:'Finished'}});}catch{}
   return;
  }
  if(threadId==='th-1' && ((id==='tu-1' && !MODE.startsWith('read_') && MODE !== 'solo') || (id==='tu-2' && MODE==='read_delegate'))){
   const r=await this.call('item/tool/call',{threadId,turnId:id,callId:'real-spawn',tool:'spawn_agent',arguments:{role:'iga-specialist',brief:'Analyze the public policy and evidence'}});
   if(!r.success)throw Error('actual OrgAI rejected spawn');
  } else {
   const text=threadId==='th-1'?JSON.stringify({actions:[{action:(MODE==='solo' || (MODE==='read_mutate' && id==='tu-2'))?'stage_wave_mutations':'read_wave_evidence',arguments_json:'{"wave_index":1}'}]}):'Public evidence supports a bounded IGA recommendation.';
   if(!(threadId==='th-2' && MODE==='empty_child')) this.push('item/completed',{threadId,turnId:(MODE==='stale_root' && id==='tu-4')?'tu-3':id,item:{id:'msg-'+id,type:'agentMessage',phase:'final_answer',text}});
  }
  this.push('thread/tokenUsage/updated',{threadId,tokenUsage:{total:{inputTokens:10,outputTokens:4}}});
  const status=threadId==='th-2' && MODE==='interrupted_child'?'interrupted':threadId==='th-2' && MODE==='failed_child'?'failed':'completed';
  this.push('turn/completed',{threadId,turn:{id,status}});
 }
}
try {
 const r=await start({runtime_root:RUNTIME,codex_home:'/no-auth-read',model:TEST_MODEL,episode_id:'mock-only',system:'public policy',prompt:JSON.stringify({action_schema:{type:'object',properties:{actions:{type:'array'}}}})},Fake);
 if(MODE==='stale_root' || MODE==='plan_complete_followup' || MODE.startsWith('read_')) await send('next public observations: evidence observed');
 if(MODE==='read_limit') for(let i=0;i<10;i++) await send('public observations');
 console.log(JSON.stringify({mock_result:r}));
} catch(e) {console.log(JSON.stringify({mock_error:e.message}));}
finally {await cleanup();}
""".replace("BRIDGE", json.dumps((ROOT / "src/identitybench/v41/orgai_runtime.mjs").as_uri()))
        .replace("RUNTIME", json.dumps(runtime))
        .replace("TEST_MODEL", json.dumps(model))
        .replace("TEST_MODE", json.dumps(mode))
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if mode not in {"success", "read_delegate", "plan_complete_followup"}:
        assert any("mock_error" in e for e in events), result.stdout
        assert not any("mock_result" in e for e in events)
        return
    bound = [e for e in events if e.get("type") == "platform.child.bound"]
    assert len(bound) == 1 and bound[0]["parent_id"] == "root", result.stdout
    assert bound[0]["agent_id"] != "root" and bound[0]["thread_id"] == "th-2"
    assert any(e.get("type") == "platform.delegation.tool" for e in events)
    plans = [e["plan"] for e in events if e.get("type") == "platform.plan.updated"]
    assert plans[0]["steps"][0]["status"] == "pending"
    assert plans[1]["steps"][0]["status"] == "in_progress"
    if mode == "read_delegate":
        requested = [e for e in events if e.get("type") == "platform.turn.requested"]
        assert requested[1]["benchmark_turn"] == 2
    if mode == "plan_complete_followup":
        assert plans[-1]["status"] == "done"
        requested = [e for e in events if e.get("type") == "platform.turn.requested"]
        assert requested[-1]["benchmark_turn"] == 2
    reply = next(e["mock_result"] for e in events if "mock_result" in e)
    assert json.loads(reply["text"]) == ACTION


@pytest.mark.asyncio
async def test_large_event_transport_and_owned_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    from identitybench.v41.orgai_bridge import OrgAITransport

    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        assert kwargs["limit"] == 16 * 1024 * 1024
        assert kwargs["start_new_session"] is True
        program = (
            "import json,sys; m=json.loads(sys.stdin.readline());"
            "print(json.dumps({'kind':'event','public':'x'*100000}),flush=True);"
            "print(json.dumps({'kind':'reply','id':m['id'],'status':'mock'}),flush=True)"
        )
        return await real_spawn(sys.executable, "-c", program, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    transport = OrgAITransport.__new__(OrgAITransport)
    transport.process = None
    transport.terminal_error = None
    transport.timeout_s = 5
    transport.request_id = 0
    transport.events = []
    transport.trace = tmp_path / "trace.jsonl"
    try:
        result = await transport.exchange({"op": "mock-only"})
        assert result["status"] == "mock"
        assert len(transport.events[0]["public"]) == 100000
    finally:
        await transport.close()


def test_partial_comparison_never_claims_matched_twelve() -> None:
    spec = importlib.util.spec_from_file_location(
        "orgai_stats", ROOT / "reports/v41-lite/run_orgai_comparison.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cell = module.summarize_cell(
        [
            {
                "episode_id": "v41_lite_001",
                "execution_valid": False,
                "termination_reason": "PROVIDER_ERROR",
                "scorecard": {"g_srr": 0},
            }
        ]
    )
    assert cell["planned_n"] == 12 and cell["attempted_n"] == 1
    assert cell["completed_n"] == 0 and len(cell["missing_episode_ids"]) == 11
    assert cell["g_srr"] is None and cell["execution_valid_rate"] is None


def test_astra_full_suite_and_matched_history() -> None:
    spec = importlib.util.spec_from_file_location(
        "orgai_astra", ROOT / "reports/v41-lite/run_orgai_comparison.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.matched_config(ROOT / "experiments/v41-orgai-astra-lite.yaml")
    assert config["model"] == "gpt-6-astra"
    assert len(config["episode_ids"]) == 60
    assert config["episode_ids"][-1] == "v41_lite_060"
    assert set(config["historical_cells"]) == {"solo", "team"}
    assert all("gpt-6-astra" in v for v in config["historical_cells"].values())
    summary = module.summarize_cell([], tuple(config["episode_ids"]))
    assert summary["planned_n"] == 60 and len(summary["missing_episode_ids"]) == 60
    assert summary["g_srr"] is None


@pytest.mark.asyncio
async def test_astra_model_passed_without_sol_substitution() -> None:
    transport = FakeTransport(ACTION)
    provider = OrgAIProvider(transport, "v41_lite_060", "gpt-6-astra")  # type: ignore[arg-type]
    result = await provider.complete(ModelRequest(system="public", messages=()))
    assert transport.payloads[0]["model"] == "gpt-6-astra"
    assert result.served_model == "gpt-6-astra"


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_event_only", [False, True])
async def test_fatal_transport_closes_and_preserves_first_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fatal_event_only: bool
) -> None:
    import sys

    from identitybench.v41.orgai_bridge import OrgAITransport

    real_spawn = asyncio.create_subprocess_exec
    spawns = 0

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawns
        spawns += 1
        program = (
            "import json,sys,time; json.loads(sys.stdin.readline());"
            "print(json.dumps({'kind':'reply','error':'SUCCESSFUL_DELEGATION_REQUIRED'}),flush=True);"
            "time.sleep(60)"
        )
        if fatal_event_only:
            program = (
                "import json,sys; json.loads(sys.stdin.readline());"
                "print(json.dumps({'kind':'event','type':'bridge.fatal','code':'SUCCESSFUL_DELEGATION_REQUIRED'}),flush=True)"
            )
        return await real_spawn(sys.executable, "-c", program, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    transport = OrgAITransport.__new__(OrgAITransport)
    transport.process = None
    transport.terminal_error = None
    transport.timeout_s = 2
    transport.request_id = 0
    transport.events = []
    transport.trace = tmp_path / "nested" / "trace.jsonl"
    for _ in range(2):
        with pytest.raises(RuntimeError, match=r"^SUCCESSFUL_DELEGATION_REQUIRED$"):
            await asyncio.wait_for(transport.exchange({"op": "mock"}), 3)
    assert spawns == 1
    assert transport.process and transport.process.returncode is not None
    assert transport.events[-1]["code"] == "SUCCESSFUL_DELEGATION_REQUIRED"
    assert json.loads(transport.trace.read_text().splitlines()[-1])["code"] == "SUCCESSFUL_DELEGATION_REQUIRED"


@pytest.mark.asyncio
async def test_fatal_provider_never_resends() -> None:
    transport = FakeTransport(RuntimeError("not safe to log"))
    provider = OrgAIProvider(transport, "mock-only")  # type: ignore[arg-type]
    request = ModelRequest(system="public", messages=())
    first = await provider.complete(request)
    second = await provider.complete(request)
    assert first is second
    assert first.raw and first.raw["nonretryable"] is True
    assert len(transport.payloads) == 1
    assert "not safe" not in str(first)


@pytest.mark.asyncio
async def test_node_fatal_protocol_exits_with_stdin_open() -> None:
    process = await asyncio.create_subprocess_exec(
        "node", str(ROOT / "src/identitybench/v41/orgai_runtime.mjs"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout
    try:
        process.stdin.write(b'{"op":"invalid","id":1}\n')
        await process.stdin.drain()
        first = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        assert first["type"] == "bridge.fatal"
        assert reply["error"] == first["code"] == "ORGAI_BRIDGE_RUNTIME_FAILURE"
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
