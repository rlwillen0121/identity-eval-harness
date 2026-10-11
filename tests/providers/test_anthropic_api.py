from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message as ApiMessage

from identitybench.providers.anthropic_api import (
    ASSISTANT_CONTENT_KEY,
    DEFAULT_BASE_URL,
    NO_TEMPERATURE_MODELS,
    AnthropicProvider,
    accepts_temperature,
)
from identitybench.providers.base import (
    Message,
    ModelResponse,
    Provider,
    Role,
    ToolCall,
    hash_endpoint,
)
from identitybench.providers.replay import response_from_dict, response_to_dict
from tests.providers.fakes import (
    DECISION_SCHEMA,
    LOOKUP_TOOL,
    FakeAnthropicClient,
    make_request,
)


def api_message(
    content: list[dict[str, Any]] | None = None,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> ApiMessage:
    return ApiMessage.model_validate(
        {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content if content is not None else [{"type": "text", "text": "hi"}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage
            or {
                "input_tokens": 12,
                "output_tokens": 40,
                "cache_creation_input_tokens": 1500,
                "cache_read_input_tokens": 3000,
                "output_tokens_details": {"thinking_tokens": 25},
            },
        }
    )


def provider(*results: Any, model: str = "claude-opus-5-5", **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(model, client=FakeAnthropicClient(*results), **kwargs)  # type: ignore[arg-type]


def fake_client(p: AnthropicProvider) -> FakeAnthropicClient:
    client: object = p._client
    assert isinstance(client, FakeAnthropicClient)
    return client


def sent(p: AnthropicProvider) -> dict[str, Any]:
    client = fake_client(p)
    calls = client.messages.create_calls + client.messages.stream_calls
    assert len(calls) == 1
    return calls[0]


def status_error(cls: type[anthropic.APIStatusError], code: int, message: str):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(code, request=request, headers={"request-id": "req_123"})
    return cls(message, response=response, body=None)


# --------------------------------------------------------------------------- request construction


async def test_system_prompt_is_a_cached_text_block() -> None:
    p = provider(api_message())
    await p.complete(make_request("Decide.", system="POLICY PACK"))
    params = sent(p)
    assert params["system"] == [
        {"type": "text", "text": "POLICY PACK", "cache_control": {"type": "ephemeral"}}
    ]
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 4096
    assert params["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Decide."}]}
    ]


async def test_empty_system_is_omitted() -> None:
    p = provider(api_message())
    await p.complete(make_request(system=""))
    assert "system" not in sent(p)


async def test_structured_output_and_effort_go_in_output_config() -> None:
    p = provider(api_message([{"type": "text", "text": '{"decision": "DENY", "rules": []}'}]))
    await p.complete(make_request(schema=DECISION_SCHEMA, effort="high"))
    assert sent(p)["output_config"] == {
        "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
        "effort": "high",
    }


async def test_no_output_config_thinking_or_tool_choice_by_default() -> None:
    p = provider(api_message())
    await p.complete(make_request())
    params = sent(p)
    for key in ("output_config", "thinking", "tool_choice", "tools", "temperature", "extra_body"):
        assert key not in params, key


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-opus-4-7",
        "us.anthropic.claude-opus-4-8-v1",
    ],
)
async def test_temperature_is_omitted_for_models_that_reject_it(model: str) -> None:
    p = provider(api_message(), model=model)
    response = await p.complete(make_request(temperature=0.0))
    params = sent(p)
    assert "temperature" not in params
    assert "extra_body" not in params
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["temperature"]
    assert p.capabilities().supports_temperature is False


async def test_temperature_is_sent_to_models_that_accept_it() -> None:
    p = provider(api_message(), model="claude-haiku-4-5")
    response = await p.complete(make_request(temperature=0.0))
    assert sent(p)["extra_body"] == {"temperature": 0.0}
    assert response.raw is not None
    assert "ignored_params" not in response.raw
    assert p.capabilities().supports_temperature is True


def test_no_temperature_table_covers_the_current_models() -> None:
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"):
        assert not accepts_temperature(model)
    assert accepts_temperature("claude-haiku-4-5")
    assert accepts_temperature("local-model")
    assert len(NO_TEMPERATURE_MODELS) == len(set(NO_TEMPERATURE_MODELS))


async def test_tools_are_strict_and_tool_choice_is_never_forced() -> None:
    p = provider(api_message())
    await p.complete(make_request(tools=(LOOKUP_TOOL,)))
    params = sent(p)
    assert params["tools"] == [
        {
            "name": "get_identity",
            "description": "Look up an identity by id.",
            "input_schema": LOOKUP_TOOL.input_schema,
            "strict": True,
        }
    ]
    assert "tool_choice" not in params


async def test_tool_results_of_a_turn_are_grouped_into_one_user_message() -> None:
    messages = (
        Message(Role.user, "Decide REQ-1."),
        Message(
            Role.assistant,
            "Checking.",
            tool_calls=(
                ToolCall("toolu_1", "get_identity", {"id": "U1"}),
                ToolCall("toolu_2", "get_identity", {"id": "U2"}),
            ),
        ),
        Message(Role.tool, "U1 record", tool_call_id="toolu_1"),
        Message(Role.tool, "U2 record", tool_call_id="toolu_2"),
        Message(Role.user, "Submit now."),
    )
    p = provider(api_message())
    await p.complete(make_request(messages=messages, tools=(LOOKUP_TOOL,)))
    assert sent(p)["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Decide REQ-1."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_identity",
                    "input": {"id": "U1"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "get_identity",
                    "input": {"id": "U2"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "U1 record"},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "U2 record"},
                {"type": "text", "text": "Submit now."},
            ],
        },
    ]


async def test_tool_turns_are_replayed_with_their_original_thinking_blocks() -> None:
    first = api_message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "tool_use", "id": "toolu_9", "name": "get_identity", "input": {"id": "U9"}},
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient(first, api_message())
    p = AnthropicProvider("claude-opus-5-5", client=client)  # type: ignore[arg-type]
    request = make_request(tools=(LOOKUP_TOOL,))
    response = await p.complete(request)
    assert response.tool_calls == [ToolCall("toolu_9", "get_identity", {"id": "U9"})]

    follow_up = make_request(
        tools=(LOOKUP_TOOL,),
        messages=(
            *request.messages,
            Message(Role.assistant, "", tool_calls=tuple(response.tool_calls)),
            Message(Role.tool, "U9 record", tool_call_id="toolu_9"),
        ),
    )
    await p.complete(follow_up)
    assistant = client.messages.create_calls[1]["messages"][1]
    assert assistant == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "tool_use", "id": "toolu_9", "name": "get_identity", "input": {"id": "U9"}},
        ],
    }


TOOL_TURN_BLOCKS = [
    {"type": "thinking", "thinking": "", "signature": "sig-abc"},
    {"type": "tool_use", "id": "toolu_9", "name": "get_identity", "input": {"id": "U9"}},
]


def follow_up_after(response: ModelResponse) -> Any:
    first = make_request(tools=(LOOKUP_TOOL,))
    return make_request(
        tools=(LOOKUP_TOOL,),
        messages=(
            *first.messages,
            Message(Role.assistant, "", tool_calls=tuple(response.tool_calls)),
            Message(Role.tool, "U9 record", tool_call_id="toolu_9"),
        ),
    )


async def test_tool_turn_blocks_are_returned_in_raw_for_the_cache() -> None:
    p = provider(api_message(TOOL_TURN_BLOCKS, stop_reason="tool_use"))
    response = await p.complete(make_request(tools=(LOOKUP_TOOL,)))
    assert response.raw is not None
    assert response.raw[ASSISTANT_CONTENT_KEY] == TOOL_TURN_BLOCKS
    plain = await provider(api_message()).complete(make_request())
    assert ASSISTANT_CONTENT_KEY not in (plain.raw or {})


async def test_a_cached_tool_turn_is_replayed_exactly_by_a_new_instance() -> None:
    """A cache hit or resumed run continues a tool loop in a provider that never saw the turn."""
    original = await provider(api_message(TOOL_TURN_BLOCKS, stop_reason="tool_use")).complete(
        make_request(tools=(LOOKUP_TOOL,))
    )
    cached = response_from_dict(json.loads(json.dumps(response_to_dict(original))))

    fresh = provider(api_message())
    fresh.remember_response(cached)
    await fresh.complete(follow_up_after(cached))
    assert sent(fresh)["messages"][1] == {"role": "assistant", "content": TOOL_TURN_BLOCKS}


async def test_without_remembered_blocks_the_turn_is_rebuilt_from_the_transcript() -> None:
    original = await provider(api_message(TOOL_TURN_BLOCKS, stop_reason="tool_use")).complete(
        make_request(tools=(LOOKUP_TOOL,))
    )
    fresh = provider(api_message())
    fresh.remember_response(ModelResponse(tool_calls=original.tool_calls))  # no raw blocks
    await fresh.complete(follow_up_after(original))
    assert sent(fresh)["messages"][1] == {"role": "assistant", "content": TOOL_TURN_BLOCKS[1:]}


async def test_large_outputs_are_streamed() -> None:
    p = provider(api_message())
    response = await p.complete(make_request(max_output_tokens=32000))
    client = fake_client(p)
    assert client.messages.create_calls == []
    assert client.messages.stream_calls[0]["max_tokens"] == 32000
    assert response.text == "hi"


async def test_outputs_up_to_16k_are_not_streamed() -> None:
    p = provider(api_message())
    await p.complete(make_request(max_output_tokens=16000))
    client = fake_client(p)
    assert len(client.messages.create_calls) == 1
    assert client.messages.stream_calls == []


# --------------------------------------------------------------------------- response parsing


async def test_text_usage_and_served_model() -> None:
    message = api_message(
        [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Hello"}]
    )
    response = await provider(message).complete(make_request())
    assert response.ok
    assert response.text == "Hello"
    assert response.parsed is None
    assert response.stop_reason == "end_turn"
    assert response.served_model == "claude-opus-5-5"
    usage = response.usage
    assert (usage.input_tokens, usage.output_tokens) == (12, 40)
    assert (usage.cache_write_tokens, usage.cache_read_tokens) == (1500, 3000)
    assert usage.reasoning_tokens == 25
    assert response.raw is not None
    assert response.raw["id"] == "msg_01"
    assert response.latency_s >= 0


async def test_structured_output_is_parsed_from_the_first_text_block() -> None:
    message = api_message([{"type": "text", "text": '{"decision": "DENY", "rules": ["SOD-001"]}'}])
    response = await provider(message).complete(make_request(schema=DECISION_SCHEMA))
    assert response.parsed == {"decision": "DENY", "rules": ["SOD-001"]}
    assert json.loads(response.text or "") == response.parsed


async def test_truncated_structured_output_leaves_parsed_empty() -> None:
    message = api_message([{"type": "text", "text": '{"decision": "DE'}], stop_reason="max_tokens")
    response = await provider(message).complete(make_request(schema=DECISION_SCHEMA))
    assert response.parsed is None
    assert response.stop_reason == "max_tokens"
    assert response.error is None


async def test_missing_usage_details_default_to_zero() -> None:
    message = api_message(usage={"input_tokens": 1, "output_tokens": 2})
    response = await provider(message).complete(make_request())
    assert response.usage.cache_read_tokens == 0
    assert response.usage.cache_write_tokens == 0
    assert response.usage.reasoning_tokens == 0


async def test_message_without_usage_from_a_compatible_server() -> None:
    # model_construct skips validation, like the SDK's lenient parsing of a server's reply.
    message = ApiMessage.model_construct(
        id="msg_x",
        type="message",
        role="assistant",
        model="local-model",
        content=api_message().content,
        stop_reason="end_turn",
        stop_sequence=None,
        usage=None,
    )
    response = await provider(message).complete(make_request())
    assert response.ok
    assert response.text == "hi"
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0


async def test_malformed_message_is_an_error_not_a_crash() -> None:
    message = ApiMessage.model_construct(
        id="msg_x", model="local-model", content=[object()], stop_reason="end_turn", usage=None
    )
    response = await provider(message).complete(make_request())
    assert response.error is not None
    assert "malformed response" in response.error


async def test_refusal_is_checked_first() -> None:
    message = api_message(
        [{"type": "text", "text": '{"decision": "AP'}],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    )
    response = await provider(message).complete(make_request(schema=DECISION_SCHEMA))
    assert response.refused
    assert not response.ok
    assert response.parsed is None
    assert response.error is None
    assert response.raw is not None
    assert response.raw["stop_details"] == {
        "type": "refusal",
        "category": "cyber",
        "explanation": "declined",
    }


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (status_error(anthropic.BadRequestError, 400, "temperature is not supported"), "400"),
        (status_error(anthropic.RateLimitError, 429, "rate limited"), "RateLimitError 429"),
        (status_error(anthropic.InternalServerError, 529, "overloaded"), "529"),
    ],
)
async def test_api_errors_become_error_responses(error: Exception, fragment: str) -> None:
    response = await provider(error).complete(make_request())
    assert response.error is not None
    assert fragment in response.error
    assert response.error.startswith("anthropic: ")
    assert response.raw == {"request_id": "req_123"}
    assert response.latency_s >= 0


async def test_connection_errors_become_error_responses() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = await provider(anthropic.APITimeoutError(request)).complete(make_request())
    assert response.error is not None
    assert "APITimeoutError" in response.error


async def test_stream_errors_become_error_responses() -> None:
    error = status_error(anthropic.InternalServerError, 500, "boom")
    response = await provider(error).complete(make_request(max_output_tokens=20000))
    assert response.error is not None
    assert "500" in response.error


# --------------------------------------------------------------------------- identity and client


def test_kind_and_endpoint_identity(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_ANTHROPIC_URL", "http://localhost:8000/")
    native = AnthropicProvider("claude-opus-5-5")
    local = AnthropicProvider(
        "local-model", base_url_env="LOCAL_ANTHROPIC_URL", cache_namespace="d1"
    )
    assert isinstance(native, Provider)
    assert native.kind == "anthropic"
    assert local.kind == "anthropic_compatible"
    assert native.endpoint_identity() == hash_endpoint(DEFAULT_BASE_URL, "claude-opus-5-5", None)
    assert local.endpoint_identity() == hash_endpoint("http://localhost:8000", "local-model", "d1")
    assert "localhost" not in local.endpoint_identity()
    bumped = AnthropicProvider(
        "local-model", base_url_env="LOCAL_ANTHROPIC_URL", cache_namespace="d2"
    )
    assert bumped.endpoint_identity() != local.endpoint_identity()


def test_identity_follows_anthropic_base_url_like_the_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    p = AnthropicProvider("claude-opus-5-5")
    assert p.endpoint_identity() == hash_endpoint(
        "https://gateway.example", "claude-opus-5-5", None
    )


async def test_unset_base_url_env_is_an_error_not_a_silent_switch(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_URL", raising=False)
    p = AnthropicProvider("local-model", base_url_env="MISSING_URL")
    response = await p.complete(make_request())
    assert response.error == "anthropic_compatible: environment variable MISSING_URL is not set"


def test_lazy_client_uses_configured_key_url_retries_and_timeout(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_ANTHROPIC_URL", "http://localhost:8000")
    monkeypatch.setenv("LOCAL_ANTHROPIC_KEY", "local-key-123")
    p = AnthropicProvider(
        "local-model",
        base_url_env="LOCAL_ANTHROPIC_URL",
        api_key_env="LOCAL_ANTHROPIC_KEY",
        max_retries=7,
        timeout_s=42,
    )
    assert p._client is None
    client = p._get_client()
    assert client is p._get_client()
    assert client.api_key == "local-key-123"
    assert str(client.base_url).startswith("http://localhost:8000")
    assert client.max_retries == 7
    assert client.timeout == 42


def test_compatible_server_never_receives_the_real_anthropic_key(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_ANTHROPIC_URL", "http://localhost:8000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-oauth-token")
    monkeypatch.delenv("LOCAL_ANTHROPIC_KEY", raising=False)
    p = AnthropicProvider(
        "local-model", base_url_env="LOCAL_ANTHROPIC_URL", api_key_env="LOCAL_ANTHROPIC_KEY"
    )
    client = p._get_client()
    assert client.api_key == "not-set"
    assert client.auth_token is None


async def test_missing_custom_key_for_native_api_is_an_error(monkeypatch) -> None:
    monkeypatch.delenv("OTHER_ANTHROPIC_KEY", raising=False)
    p = AnthropicProvider("claude-opus-5-5", api_key_env="OTHER_ANTHROPIC_KEY")
    response = await p.complete(make_request())
    assert response.error is not None
    assert "OTHER_ANTHROPIC_KEY is not set" in response.error


def test_capabilities() -> None:
    caps = AnthropicProvider("claude-opus-5-5").capabilities()
    assert caps.native_structured_output and caps.native_tools


def test_compatible_server_without_a_key_variable_gets_a_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_ANTHROPIC_URL", "http://localhost:8000")
    monkeypatch.delenv("LOCAL_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-oauth-token")
    p = AnthropicProvider("local-model", base_url_env="LOCAL_ANTHROPIC_URL")
    client = p._get_client()
    assert client.api_key == "not-set"
    assert client.auth_token is None


def test_compatible_server_reads_the_paired_key_variable(monkeypatch) -> None:
    """``.env.example`` pairs LOCAL_LLM_BASE_URL with LOCAL_LLM_API_KEY."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    p = AnthropicProvider("local-model", base_url_env="LOCAL_LLM_BASE_URL")
    assert p._get_client().api_key == "local-key-123"


def test_compatible_server_at_anthropic_base_url_never_pairs_with_the_real_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    p = AnthropicProvider("local-model", base_url_env="ANTHROPIC_BASE_URL")
    assert p._get_client().api_key == "not-set"


def test_native_api_without_a_key_variable_uses_anthropic_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    assert AnthropicProvider("claude-opus-5-5")._get_client().api_key == "sk-ant-real-key"


async def test_degenerate_deeply_nested_reply_is_unparsed_not_a_crash() -> None:
    p = provider(api_message([{"type": "text", "text": "[" * 30000}]))
    response = await p.complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.parsed is None
    assert response.text == "[" * 30000
