from __future__ import annotations

from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from identitybench.providers.base import Message, Provider, Role, ToolCall, hash_endpoint
from identitybench.providers.openai_compat import (
    DEFAULT_BASE_URL,
    NO_TEMPERATURE_MODELS,
    UNPARSED_ARGUMENTS_KEY,
    OpenAICompatProvider,
    accepts_temperature,
    default_api_key_env,
    is_vendor_endpoint,
    provider_routing_problem,
)
from tests.providers.fakes import DECISION_SCHEMA, LOOKUP_TOOL, FakeOpenAIClient, make_request


def completion(
    content: str | None = "hi",
    *,
    refusal: str | None = None,
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    model: str = "gpt-test-2026",
    **top: Any,
) -> ChatCompletion:
    """A completion as the SDK parses it; ``top`` adds non-OpenAI top-level fields (a gateway's
    ``provider``), which the SDK keeps in ``model_extra``."""
    message: dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ChatCompletion.model_validate(
        {
            **top,
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "system_fingerprint": "fp_1",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": usage
            or {
                "prompt_tokens": 1000,
                "completion_tokens": 300,
                "total_tokens": 1300,
                "prompt_tokens_details": {"cached_tokens": 600},
                "completion_tokens_details": {"reasoning_tokens": 200},
            },
        }
    )


def provider(*results: Any, **kwargs: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider("gpt-test", client=FakeOpenAIClient(*results), **kwargs)  # type: ignore[arg-type]


def fake_client(p: OpenAICompatProvider) -> FakeOpenAIClient:
    client: object = p._client
    assert isinstance(client, FakeOpenAIClient)
    return client


def sent(p: OpenAICompatProvider) -> dict[str, Any]:
    calls = fake_client(p).completions.calls
    assert len(calls) == 1
    return calls[0]


# --------------------------------------------------------------------------- request construction


async def test_basic_request() -> None:
    p = provider(completion())
    await p.complete(make_request("Decide.", system="POLICY"))
    params = sent(p)
    assert params == {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "POLICY"},
            {"role": "user", "content": "Decide."},
        ],
        "max_completion_tokens": 4096,
    }


async def test_structured_output_temperature_and_reasoning_effort() -> None:
    p = provider(completion('{"decision": "DENY", "rules": []}'))
    await p.complete(make_request(schema=DECISION_SCHEMA, temperature=0.0, effort="low"))
    params = sent(p)
    assert params["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "decision", "schema": DECISION_SCHEMA, "strict": True},
    }
    assert params["temperature"] == 0.0
    assert params["reasoning_effort"] == "low"


async def test_reasoning_effort_can_be_disabled_and_max_tokens_param_switched() -> None:
    p = provider(completion(), reasoning_effort_param=False, max_tokens_param="max_tokens")
    response = await p.complete(make_request(effort="high", max_output_tokens=512))
    params = sent(p)
    assert "reasoning_effort" not in params
    assert params["max_tokens"] == 512
    assert "max_completion_tokens" not in params
    # The spec's parameters still say effort=high, so the drop is recorded.
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["effort"]


async def test_nothing_is_recorded_as_ignored_when_everything_applies() -> None:
    response = await provider(completion()).complete(make_request(effort="high"))
    assert "ignored_params" not in (response.raw or {})
    no_effort = provider(completion(), reasoning_effort_param=False)
    response = await no_effort.complete(make_request())
    assert "ignored_params" not in (response.raw or {})


# --------------------------------------------------------------------------- temperature

REASONING_MODELS = [
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano-2025-08-07",
    "gpt-5.1",
    "gpt-5.2-codex",
    "o1",
    "o1-pro",
    "o3",
    "o3-mini",
    "o4-mini",
    "ft:o4-mini-2025-04-16:acme::abc123",
]


@pytest.mark.parametrize("model", REASONING_MODELS)
async def test_temperature_is_dropped_for_openai_reasoning_models(model: str, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = OpenAICompatProvider(model, client=FakeOpenAIClient(completion()))  # type: ignore[arg-type]
    response = await p.complete(make_request(temperature=0.0))
    assert "temperature" not in sent(p)
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["temperature"]
    assert p.capabilities().supports_temperature is False


async def test_temperature_is_sent_to_openai_models_that_accept_it(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = OpenAICompatProvider("gpt-4.1", client=FakeOpenAIClient(completion()))  # type: ignore[arg-type]
    response = await p.complete(make_request(temperature=0.0))
    assert sent(p)["temperature"] == 0.0
    assert "ignored_params" not in (response.raw or {})
    assert p.capabilities().supports_temperature is True


@pytest.mark.parametrize(
    ("base_url_env", "url"),
    [
        ("OPENAI_BASE_URL", "https://api.openai.com/v1/"),
        ("OPENAI_BASE_URL", "https://api.openai.com"),
    ],
)
async def test_the_vendor_endpoint_is_recognized_however_it_is_spelled(
    base_url_env: str, url: str, monkeypatch
) -> None:
    monkeypatch.setenv(base_url_env, url)
    p = OpenAICompatProvider("o3", client=FakeOpenAIClient(completion()))  # type: ignore[arg-type]
    await p.complete(make_request(temperature=0.0))
    assert "temperature" not in sent(p)


async def test_a_self_hosted_server_gets_the_temperature_whatever_the_model_name(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    p = OpenAICompatProvider(
        "gpt-5-mini",
        base_url_env="LOCAL_LLM_BASE_URL",
        client=FakeOpenAIClient(completion()),  # type: ignore[arg-type]
    )
    response = await p.complete(make_request(temperature=0.0))
    assert sent(p)["temperature"] == 0.0
    assert "ignored_params" not in (response.raw or {})
    assert p.capabilities().supports_temperature is True


async def test_dropped_temperature_and_effort_are_both_recorded(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = OpenAICompatProvider(
        "gpt-5",
        reasoning_effort_param=False,
        client=FakeOpenAIClient(completion()),  # type: ignore[arg-type]
    )
    response = await p.complete(make_request(temperature=0.0, effort="high"))
    assert "temperature" not in sent(p)
    assert "reasoning_effort" not in sent(p)
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["temperature", "effort"]


async def test_an_unset_temperature_is_not_recorded_as_ignored(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = OpenAICompatProvider("o3", client=FakeOpenAIClient(completion()))  # type: ignore[arg-type]
    response = await p.complete(make_request())
    assert "ignored_params" not in (response.raw or {})


def test_no_temperature_table() -> None:
    for model in REASONING_MODELS:
        assert not accepts_temperature(model), model
    for model in ("gpt-4.1", "gpt-4o-mini", "gpt-50", "o1x", "local-model", "ft:gpt-4.1:a::b"):
        assert accepts_temperature(model), model
    assert len(NO_TEMPERATURE_MODELS) == len(set(NO_TEMPERATURE_MODELS))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (DEFAULT_BASE_URL, True),
        ("https://api.openai.com", True),
        ("https://API.OpenAI.com/v1", True),
        ("http://localhost:8000/v1", False),
        ("https://api.openai.com.example.net/v1", False),
        ("https://gateway.example/openai/v1", False),
        (None, False),
    ],
)
def test_is_vendor_endpoint(url: str | None, expected: bool) -> None:
    assert is_vendor_endpoint(url) is expected


def test_rejects_unknown_max_tokens_param() -> None:
    with pytest.raises(ValueError, match="max_tokens_param"):
        OpenAICompatProvider("m", max_tokens_param="tokens")  # type: ignore[arg-type]


async def test_tools_and_tool_messages() -> None:
    messages = (
        Message(Role.user, "Decide."),
        Message(Role.assistant, "", tool_calls=(ToolCall("call_1", "get_identity", {"id": "U1"}),)),
        Message(Role.tool, "U1 record", tool_call_id="call_1"),
    )
    p = provider(completion())
    await p.complete(make_request(messages=messages, tools=(LOOKUP_TOOL,), system=""))
    params = sent(p)
    assert params["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_identity",
                "description": "Look up an identity by id.",
                "parameters": LOOKUP_TOOL.input_schema,
                "strict": True,
            },
        }
    ]
    assert params["messages"] == [
        {"role": "user", "content": "Decide."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_identity", "arguments": '{"id": "U1"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "U1 record"},
    ]


# --------------------------------------------------------------------------- response parsing


async def test_text_parsed_usage_and_served_model() -> None:
    p = provider(completion('{"decision": "DENY", "rules": ["LIFE-001"]}'))
    response = await p.complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.parsed == {"decision": "DENY", "rules": ["LIFE-001"]}
    assert response.stop_reason == "stop"
    assert response.served_model == "gpt-test-2026"
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens) == (400, 600)
    assert (usage.output_tokens, usage.reasoning_tokens) == (300, 200)
    assert response.raw == {"id": "chatcmpl-1", "system_fingerprint": "fp_1"}


async def test_usage_without_details_or_at_all() -> None:
    bare = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    response = await provider(completion(usage=bare)).complete(make_request())
    assert (response.usage.input_tokens, response.usage.output_tokens) == (10, 5)
    assert response.usage.cache_read_tokens == 0
    no_usage = completion().model_copy(update={"usage": None})
    response = await provider(no_usage).complete(make_request())
    assert response.usage.input_tokens == 0


async def test_parsed_is_none_without_schema_or_for_invalid_json() -> None:
    response = await provider(completion('{"a": 1}')).complete(make_request())
    assert response.parsed is None
    response = await provider(completion("not json")).complete(make_request(schema=DECISION_SCHEMA))
    assert response.parsed is None
    assert response.text == "not json"


async def test_tool_calls_are_parsed_and_bad_arguments_kept_raw() -> None:
    calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "get_identity", "arguments": '{"id": "U1"}'},
        },
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "get_identity", "arguments": "{oops"},
        },
        {"id": "c3", "type": "function", "function": {"name": "get_identity", "arguments": ""}},
    ]
    p = provider(completion(None, tool_calls=calls, finish_reason="tool_calls"))
    response = await p.complete(make_request(tools=(LOOKUP_TOOL,)))
    assert response.tool_calls == [
        ToolCall("c1", "get_identity", {"id": "U1"}),
        ToolCall("c2", "get_identity", {UNPARSED_ARGUMENTS_KEY: "{oops"}),
        ToolCall("c3", "get_identity", {}),
    ]
    assert response.text is None
    assert response.stop_reason == "tool_calls"


async def test_refusal() -> None:
    p = provider(completion(None, refusal="I can't help with that."))
    response = await p.complete(make_request(schema=DECISION_SCHEMA))
    assert response.refused
    assert response.error is None
    assert response.raw is not None
    assert response.raw["refusal"] == "I can't help with that."


async def test_content_filter_counts_as_refused() -> None:
    response = await provider(completion("", finish_reason="content_filter")).complete(
        make_request()
    )
    assert response.refused


async def test_no_choices_is_an_error() -> None:
    empty = completion().model_copy(update={"choices": []})
    response = await provider(empty).complete(make_request())
    assert response.error == "openai_compatible: response has no choices"


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (openai.BadRequestError, 400),
        (openai.RateLimitError, 429),
        (openai.InternalServerError, 503),
    ],
)
async def test_api_errors_become_error_responses(
    cls: type[openai.APIStatusError], code: int
) -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response_obj = httpx2.Response(code, request=request, headers={"x-request-id": "req_9"})
    error = cls("nope", response=response_obj, body=None)
    response = await provider(error).complete(make_request())
    assert response.error is not None
    assert str(code) in response.error
    assert response.raw == {"request_id": "req_9"}


async def test_connection_errors_become_error_responses() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = await provider(openai.APIConnectionError(request=request)).complete(make_request())
    assert response.error is not None
    assert "APIConnectionError" in response.error


# --------------------------------------------------------------------------- identity and client


def test_endpoint_identity_and_base_url_resolution(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1/")
    default = OpenAICompatProvider("gpt-test")
    local = OpenAICompatProvider(
        "local-model", base_url_env="LOCAL_LLM_BASE_URL", cache_namespace="d1"
    )
    assert isinstance(default, Provider)
    assert default.kind == "openai_compatible"
    assert default.endpoint_identity() == hash_endpoint(DEFAULT_BASE_URL, "gpt-test", None)
    assert local.endpoint_identity() == hash_endpoint(
        "http://localhost:8000/v1", "local-model", "d1"
    )
    assert OpenAICompatProvider("gpt-test", base_url_env=None).endpoint_identity() == (
        default.endpoint_identity()
    )


async def test_unset_custom_base_url_env_is_an_error(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    p = OpenAICompatProvider("local-model", base_url_env="LOCAL_LLM_BASE_URL")
    response = await p.complete(make_request())
    assert response.error == "openai_compatible: environment variable LOCAL_LLM_BASE_URL is not set"


def test_self_hosted_server_never_receives_the_openai_key(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key")
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    p = OpenAICompatProvider(
        "local-model",
        base_url_env="LOCAL_LLM_BASE_URL",
        api_key_env="LOCAL_LLM_API_KEY",
        timeout_s=9,
    )
    client = p._get_client()
    assert client.api_key == "not-set"
    assert str(client.base_url).startswith("http://localhost:8000/v1")
    assert client.timeout == 9
    assert client.max_retries == 4


async def test_official_endpoint_without_key_is_an_error(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = await OpenAICompatProvider("gpt-test").complete(make_request())
    assert response.error is not None
    assert "OPENAI_API_KEY is not set" in response.error


def test_self_hosted_entry_reads_the_paired_key_variable(monkeypatch) -> None:
    """SPEC §16's self-hosted entry sets only base_url_env; .env.example pairs LOCAL_LLM_BASE_URL
    with LOCAL_LLM_API_KEY, and the real OpenAI key must stay home."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-key-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    p = OpenAICompatProvider("local-model", base_url_env="LOCAL_LLM_BASE_URL")
    assert p._get_client().api_key == "local-key-123"


def test_self_hosted_entry_without_its_key_set_gets_a_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    p = OpenAICompatProvider("local-model", base_url_env="LOCAL_LLM_BASE_URL")
    assert p._get_client().api_key == "not-set"


@pytest.mark.parametrize("base_url_env", ["OPENAI_URL", "CUSTOM_ENDPOINT"])
def test_custom_url_variable_never_pairs_with_the_openai_key(
    monkeypatch, base_url_env: str
) -> None:
    monkeypatch.setenv(base_url_env, "http://localhost:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    p = OpenAICompatProvider("local-model", base_url_env=base_url_env)
    assert p._get_client().api_key == "not-set"


@pytest.mark.parametrize(
    ("base_url_env", "expected"),
    [
        (None, "OPENAI_API_KEY"),
        ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
        ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY"),
        ("GATEWAY_URL", "GATEWAY_API_KEY"),
        ("OPENROUTER_BASE_URL", "OPENROUTER_API_KEY"),
        ("OPENAI_URL", None),
        ("ANTHROPIC_BASE_URL", None),
        ("CUSTOM_ENDPOINT", None),
        ("_URL", None),
    ],
)
def test_default_api_key_env(base_url_env: str | None, expected: str | None) -> None:
    assert default_api_key_env(base_url_env) == expected


@pytest.mark.parametrize("base_url_env", [None, "OPENAI_BASE_URL"])
def test_vendor_endpoint_without_a_key_variable_uses_openai_api_key(
    monkeypatch, base_url_env: str | None
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    p = OpenAICompatProvider("gpt-test", base_url_env=base_url_env)
    assert p._get_client().api_key == "sk-real-openai-key-123"


async def test_malformed_completion_is_an_error_not_a_crash() -> None:
    broken = ChatCompletion.model_construct(
        id="x", object="chat.completion", created=0, model="m", choices=[object()], usage=None
    )
    response = await provider(broken).complete(make_request())
    assert response.error is not None
    assert "malformed response" in response.error


async def test_degenerate_deeply_nested_reply_is_unparsed_not_a_crash() -> None:
    """A repetition loop from a small model on a server that ignores response_format."""
    response = await provider(completion("[" * 30000)).complete(
        make_request(schema=DECISION_SCHEMA)
    )
    assert response.ok
    assert response.parsed is None
    assert response.text == "[" * 30000


async def test_deeply_nested_tool_arguments_are_kept_unparsed() -> None:
    raw_arguments = "[" * 30000
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_identity", "arguments": raw_arguments},
    }
    response = await provider(completion(None, tool_calls=[call])).complete(
        make_request(tools=(LOOKUP_TOOL,))
    )
    assert response.tool_calls[0].arguments == {UNPARSED_ARGUMENTS_KEY: raw_arguments}


# --------------------------------------------------------------------------- reported cost

OPENROUTER_USAGE: dict[str, Any] = {
    "prompt_tokens": 120,
    "completion_tokens": 40,
    "total_tokens": 160,
    "cost": 0.0001172292,
    "is_byok": False,
    "cost_details": {"upstream_inference_cost": 0.0001172292},
}
"""Usage as OpenRouter returns it: the call's USD in ``cost``, beside the OpenAI fields."""


async def test_reported_cost_and_upstream_provider_are_recorded() -> None:
    reply = completion(
        '{"decision": "DENY", "rules": []}', usage=OPENROUTER_USAGE, provider="Baidu"
    )
    response = await provider(reply).complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.reported_cost_usd == 0.0001172292
    assert response.raw is not None
    assert response.raw["upstream_provider"] == "Baidu"
    assert response.parsed == {"decision": "DENY", "rules": []}
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 40)


async def test_a_missing_cost_is_unknown_not_zero() -> None:
    """OpenAI and self-hosted servers send no cost; the budget must see None, never $0."""
    response = await provider(completion()).complete(make_request())
    assert response.reported_cost_usd is None
    assert "upstream_provider" not in (response.raw or {})
    no_usage = completion().model_copy(update={"usage": None})
    response = await provider(no_usage).complete(make_request())
    assert response.reported_cost_usd is None


@pytest.mark.parametrize("cost", [None, "0.01", True, -0.5, float("nan"), float("inf"), [0.01]])
async def test_a_cost_that_is_not_a_usable_number_is_unknown(cost: object) -> None:
    usage = {**OPENROUTER_USAGE, "cost": cost}
    response = await provider(completion(usage=usage)).complete(make_request())
    assert response.reported_cost_usd is None


async def test_a_zero_cost_is_a_real_zero() -> None:
    """Free model variants report ``cost: 0``, an integer, which is a known $0."""
    usage = {**OPENROUTER_USAGE, "cost": 0}
    response = await provider(completion(usage=usage)).complete(make_request())
    assert response.reported_cost_usd == 0.0


@pytest.mark.parametrize("upstream", ["", 7, None])
async def test_an_unnamed_upstream_provider_is_not_recorded(upstream: object) -> None:
    response = await provider(completion(provider=upstream)).complete(make_request())
    assert "upstream_provider" not in (response.raw or {})


async def test_an_error_after_usage_came_back_keeps_the_cost() -> None:
    """A billed call that fails to parse still spent money; the budget must count it."""
    empty = completion(usage=OPENROUTER_USAGE, provider="Mistral").model_copy(
        update={"choices": []}
    )
    response = await provider(empty).complete(make_request())
    assert response.error == "openai_compatible: response has no choices"
    assert response.reported_cost_usd == 0.0001172292
    assert response.raw is not None and response.raw["upstream_provider"] == "Mistral"

    shaped = completion(usage=OPENROUTER_USAGE, provider="Mistral")
    broken = ChatCompletion.model_construct(**{**dict(shaped), "choices": [object()]})
    response = await provider(broken).complete(make_request())
    assert response.error is not None and "malformed response" in response.error
    assert response.reported_cost_usd == 0.0001172292
    assert response.usage.output_tokens == 40
    assert response.raw == {"upstream_provider": "Mistral"}


# --------------------------------------------------------------------------- routing preferences

ROUTING: dict[str, Any] = {
    "order": ["Baidu", "Alibaba"],
    "allow_fallbacks": False,
    "require_parameters": True,
}


async def test_routing_preference_goes_in_the_request_body() -> None:
    p = provider(completion(), provider_routing=ROUTING)
    await p.complete(make_request(schema=DECISION_SCHEMA))
    assert sent(p)["extra_body"] == {"provider": ROUTING}


async def test_no_routing_sends_no_extra_body() -> None:
    p = provider(completion())
    await p.complete(make_request())
    assert "extra_body" not in sent(p)


def test_routing_is_part_of_the_endpoint_identity(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = "deepseek/deepseek-v4-pro-0813"
    plain = OpenAICompatProvider(model, base_url_env="OPENROUTER_BASE_URL")
    routed = OpenAICompatProvider(
        model, base_url_env="OPENROUTER_BASE_URL", provider_routing=ROUTING
    )
    reordered = OpenAICompatProvider(
        model,
        base_url_env="OPENROUTER_BASE_URL",
        provider_routing={**ROUTING, "order": ["Alibaba", "Baidu"]},
    )
    same = OpenAICompatProvider(
        model, base_url_env="OPENROUTER_BASE_URL", provider_routing=dict(reversed(ROUTING.items()))
    )
    # Without routing the identity is what it always was, so existing cache entries hold.
    assert plain.endpoint_identity() == hash_endpoint("https://openrouter.ai/api/v1", model, None)
    assert routed.endpoint_identity() != plain.endpoint_identity()
    assert reordered.endpoint_identity() != routed.endpoint_identity()  # host order matters
    assert same.endpoint_identity() == routed.endpoint_identity()  # key order doesn't


async def test_routing_is_copied_so_later_edits_change_nothing() -> None:
    routing: dict[str, Any] = {"order": ["Baidu"], "allow_fallbacks": False}
    p = provider(completion(), provider_routing=routing)
    identity = p.endpoint_identity()
    routing["order"].append("Alibaba")
    await p.complete(make_request())
    assert sent(p)["extra_body"] == {"provider": {"order": ["Baidu"], "allow_fallbacks": False}}
    assert p.endpoint_identity() == identity


@pytest.mark.parametrize(
    ("routing", "problem"),
    [
        ({}, "non-empty mapping"),
        (["Baidu"], "non-empty mapping"),
        ("Baidu", "non-empty mapping"),
        ({1: "x"}, "keys must be non-empty strings"),
        ({"": "x"}, "keys must be non-empty strings"),
        ({"order": {"Baidu"}}, "values must be JSON"),
        ({"max_price": {"prompt": float("nan")}}, "values must be JSON"),
        ({"order": "Baidu"}, "order must be a list of host names"),
        ({"order": ["Baidu", ""]}, "order must be a list of host names"),
        ({"allow_fallbacks": "false"}, "allow_fallbacks must be true or false"),
        ({"require_parameters": 1}, "require_parameters must be true or false"),
    ],
)
def test_bad_routing_preferences_are_rejected(routing: object, problem: str) -> None:
    found = provider_routing_problem(routing)
    assert found is not None and problem in found
    if isinstance(routing, dict):
        with pytest.raises(ValueError, match=f"^provider_routing .*{problem}"):
            OpenAICompatProvider("m", provider_routing=routing)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "routing",
    [
        ROUTING,
        {"only": ["Mistral"]},
        {"sort": "price", "max_price": {"prompt": 1, "completion": 2}},
    ],
)
def test_good_routing_preferences_pass(routing: dict[str, Any]) -> None:
    assert provider_routing_problem(routing) is None


def test_openrouter_entry_reads_its_paired_key(monkeypatch) -> None:
    """.env.example pairs OPENROUTER_BASE_URL with OPENROUTER_API_KEY; OpenAI's key stays home."""
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    p = OpenAICompatProvider("mistralai/mistral-large-4-0", base_url_env="OPENROUTER_BASE_URL")
    assert p._get_client().api_key == "or-test-key-123"
