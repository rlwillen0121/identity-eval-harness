from __future__ import annotations

import json
from pathlib import Path

import pytest

from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.muse_cli import SETTINGS, MuseCliProvider
from tests.providers.fakes import DECISION_SCHEMA, FakeRunner, flag_value, make_request

MODEL = "muse-wonder-full-1.0"
SYSTEM = "You are an access reviewer."


def result(
    *,
    model=MODEL,
    subscription=True,
    tools=False,
    system=SYSTEM,
    status="completed",
    exit_code=0,
    duplicate=False,
    answer='{"decision":"DENY","rules":[]}',
):
    request = {"model": MODEL, "instructions": system}
    if tools:
        request["tools"] = [{"name": "shell"}]
    payload = {
        "model": model,
        "status": status,
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens": 40,
            "output_tokens_details": {"reasoning_tokens": 20},
        },
    }
    event = {"type": "response." + status, "response": payload}
    stderr = "REQUEST: " + json.dumps(request) + "\n"
    stderr += "SSE RESPONSE: event: response." + status + "\\ndata: " + json.dumps(event) + "\n"
    if duplicate:
        stderr += stderr
    if subscription:
        stderr += (
            "SSE RESPONSE: event: response.subscription_usage\\ndata: "
            + json.dumps(
                {
                    "type": "response.subscription_usage",
                    "subscription": {"window": {"used_percent": 2}},
                }
            )
            + "\n"
        )
    stdout = json.dumps(
        {
            "payload_type": "run.terminal.completed",
            "payload": {
                "terminal": "completed",
                "text": answer,
            },
        }
    )
    return ProcessResult(exit_code, stdout, stderr)


@pytest.fixture(autouse=True)
def private_login(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/muse"
    config.mkdir(parents=True)
    (config / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "meta": {
                        "mechanism": "oauth",
                        "subs_active": True,
                        "api_base_url": "https://api.meta.ai/v1",
                        "storage": "keychain",
                    }
                }
            }
        )
    )
    return config


async def test_isolated_call_and_accounting(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "must-not-pass")
    monkeypatch.setenv("MUSE_BASE_URL", "must-not-pass")
    monkeypatch.setenv("XDG_CONFIG_HOME", "must-not-pass")
    huge = "x" * 200_000

    def inspect(command):
        assert "META_API_KEY" not in command.env
        assert "MUSE_BASE_URL" not in command.env
        assert "XDG_CONFIG_HOME" not in command.env
        home = Path(command.env["HOME"])
        config = json.loads((home / SETTINGS).read_text())
        assert config["run"]["system_prompt"] == huge
        assert config["contributor_mode_enabled"] is False
        assert all(v == "off" for v in config["skills"]["activation"]["bundled"].values())
        assert Path(flag_value(command.argv, "--prompt-file")).read_text() == huge
        assert (
            json.loads(Path(flag_value(command.argv, "--output-schema")).read_text())
            == DECISION_SCHEMA
        )
        assert flag_value(command.argv, "--max-model-steps") == "1"
        assert flag_value(command.argv, "--reasoning-effort") == "medium"
        assert len(list(Path(command.cwd).iterdir())) == 0
        return result(system=huge)

    runner = FakeRunner(inspect)
    provider = MuseCliProvider(MODEL, runner=runner, version="fixture")
    response = await provider.complete(make_request(huge, system=huge, schema=DECISION_SCHEMA))
    assert response.error is None
    assert response.parsed == {"decision": "DENY", "rules": []}
    assert response.served_model == MODEL
    assert response.usage.input_tokens == 70
    assert response.usage.cache_read_tokens == 30
    assert response.usage.output_tokens == 40
    assert response.usage.reasoning_tokens == 20
    assert response.reported_cost_usd is None
    assert huge not in json.dumps(response.raw)
    assert not Path(runner.command.env["HOME"]).exists()


@pytest.mark.parametrize(
    "changes,fragment",
    [
        ({"model": "another-model"}, "served model"),
        ({"subscription": False}, "subscription accounting"),
        ({"tools": True}, "unexpected tools"),
        ({"system": "different"}, "instructions were changed"),
        ({"duplicate": True}, "exactly one"),
        ({"status": "incomplete"}, "incomplete"),
        ({"exit_code": 1}, "exit 1"),
    ],
)
async def test_fail_closed(changes, fragment):
    runner = FakeRunner(result(**changes))
    response = await MuseCliProvider(MODEL, runner=runner).complete(make_request())
    assert fragment in response.error
    assert len(runner.commands) == 1
    assert "REQUEST:" not in response.error


async def test_payg_login_is_refused_before_call(private_login):
    (private_login / "auth.json").write_text('{"providers":{"meta":{"mechanism":"api_key"}}}')
    runner = FakeRunner(result())
    response = await MuseCliProvider(MODEL, runner=runner).complete(make_request())
    assert "active OAuth subscription" in response.error
    assert runner.commands == []


async def test_missing_terminal_does_not_leak_trace():
    runner = FakeRunner(ProcessResult(1, "", "REQUEST: private prompt and credential material"))
    response = await MuseCliProvider(MODEL, runner=runner).complete(make_request())
    assert "private prompt" not in response.error
    assert "no completed terminal" in response.error


async def test_invalid_json_remains_model_output():
    response = await MuseCliProvider(MODEL, runner=FakeRunner(result(answer="not JSON"))).complete(
        make_request(schema=DECISION_SCHEMA)
    )
    assert response.error is None
    assert response.parsed is None
    assert response.text == "not JSON"


def test_setup_revision_is_stable_and_separates_effort():
    first = MuseCliProvider(MODEL, version="fixture")
    second = MuseCliProvider(MODEL, version="fixture")
    assert first.setup_revision() == second.setup_revision()
    assert (
        first.endpoint_identity()
        != MuseCliProvider(MODEL, effort="high", version="fixture").endpoint_identity()
    )


async def test_cli_only_trims_outer_system_whitespace():
    response = await MuseCliProvider(MODEL, runner=FakeRunner(result())).complete(
        make_request(system="  " + SYSTEM + " \n")
    )
    assert response.error is None
    assert response.raw["system_outer_whitespace_normalized"] is True
