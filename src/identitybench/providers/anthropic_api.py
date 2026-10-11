"""Anthropic Messages API provider (``anthropic``) and Messages-compatible servers
(``anthropic_compatible``, e.g. a self-hosted vLLM speaking ``/v1/messages``).

Request construction follows the current API surface:

- **Structured output** uses ``output_config.format`` (a JSON schema); the first text block of
  the reply is the JSON document.
- **Native tools** are sent with ``strict: true`` and the default ``tool_choice`` (auto). Forcing
  a tool (``any``/``tool``) returns a 400 on Opus 5.5, Sonnet 5.5 and Fable 5.1, so it is never
  used; the prompt has to ask for the tool call.
- **Effort** goes in ``output_config.effort``. ``thinking`` is never sent: current models think
  adaptively by default, and a thinking budget is a 400 on them.
- **Temperature** is sent only when configured *and* the model is not in
  ``NO_TEMPERATURE_MODELS``; for those it is dropped (and reported in ``raw["ignored_params"]``)
  because they reject it, or accept only the default value.
- **Prompt caching**: the system prompt is a text block marked ``cache_control: ephemeral``, so
  the policy pack (a large shared prefix) is billed at cache-read rates after the first call.
- Requests with ``max_output_tokens`` above 16k are streamed (``messages.stream`` +
  ``get_final_message``) so long generations don't trip idle-connection timeouts.

Tool loops: current models think on every turn, and an assistant turn that called tools must be
sent back with its original content blocks (thinking blocks and their signatures) or the API can
reject the continuation. The provider-neutral ``Message`` can't carry those blocks, so the
provider keeps them in memory by ``tool_use`` id and also returns them in
``raw["assistant_content"]``, where the runner's cache stores them. A runner that serves a
tool-calling response from the cache or a recording, or resumes a run in a new process, passes it
to ``remember_response`` before continuing the loop, so the next request replays the exact
blocks.

Refusals (``stop_reason == "refusal"``) come back as ``refused=True``. Server-side refusal
fallbacks are deliberately **not** enabled: a fallback answers the refused request with a
different model inside the same call, and an eval must never attribute that answer to the model
under test.

Transport errors are retried by the SDK (``max_retries``, exponential backoff on 408/409/429/5xx
and connection errors); anything still failing comes back as ``ModelResponse.error``.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import Message as ApiMessage

from identitybench.providers._env import paired_api_key_env
from identitybench.providers._json import load_object
from identitybench.providers._subprocess import redact
from identitybench.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Role,
    ToolCall,
    Usage,
    hash_endpoint,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"
STREAMING_THRESHOLD_TOKENS = 16_000

NO_TEMPERATURE_MODELS: tuple[str, ...] = (
    # Sampling parameters removed: any temperature is a 400.
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    # Only the default temperature is accepted, so a configured value can never take effect.
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
)
"""Model families that must not be sent ``temperature`` (matched as substrings, so dated or
platform-prefixed IDs such as ``us.anthropic.claude-opus-4-7-v1`` match too)."""

_PLACEHOLDER_API_KEY = "not-set"
"""Sent to a compatible server whose key variable is unset, or that has none.
Passing *some* key stops the SDK from falling back to ``ANTHROPIC_API_KEY``,
``ANTHROPIC_AUTH_TOKEN`` or an ``ant auth login`` profile, which would hand a real Anthropic
credential to a self-hosted endpoint; servers without auth ignore it."""

_TOOL_TURN_CACHE_SIZE = 4096

ASSISTANT_CONTENT_KEY = "assistant_content"
"""``ModelResponse.raw`` key holding a tool-calling turn's original content blocks."""


def accepts_temperature(model: str) -> bool:
    return not any(family in model for family in NO_TEMPERATURE_MODELS)


def _usage(usage: Any) -> Usage:
    """Token usage from an API ``usage`` object; a missing object or field counts as 0."""
    if usage is None:
        return Usage()
    details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "input_tokens", None) or 0,
        output_tokens=getattr(usage, "output_tokens", None) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        reasoning_tokens=(getattr(details, "thinking_tokens", None) or 0) if details else 0,
    )


class AnthropicProvider:
    """Claude models via the Messages API, or any server speaking it (``base_url_env``)."""

    def __init__(
        self,
        model: str,
        *,
        api_key_env: str | None = None,
        base_url_env: str | None = None,
        cache_namespace: str | None = None,
        max_retries: int = 4,
        timeout_s: float = 600.0,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self.kind: str = "anthropic_compatible" if base_url_env else "anthropic"
        # Without an explicit variable only the vendor endpoint reads ANTHROPIC_API_KEY; a
        # Messages-compatible server reads its base URL variable's paired key variable
        # (LOCAL_LLM_BASE_URL -> LOCAL_LLM_API_KEY), never the real Anthropic key.
        if api_key_env is None:
            if self.kind == "anthropic":
                api_key_env = DEFAULT_API_KEY_ENV
            elif base_url_env:
                api_key_env = paired_api_key_env(base_url_env)
        self._api_key_env = api_key_env
        self._base_url_env = base_url_env
        self._cache_namespace = cache_namespace
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._client = client
        self._base_url = self._resolve_base_url()
        # Original content blocks of assistant turns that called tools, keyed by tool_use id.
        # Thinking is always on for current models and the API requires a tool-calling turn's
        # thinking blocks to be sent back unchanged, but the provider-neutral ``Message`` can't
        # carry them; replaying the exact blocks from here keeps tool loops valid.
        self._tool_turns: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def _resolve_base_url(self) -> str | None:
        if self._base_url_env:
            value = os.environ.get(self._base_url_env)
            return value.rstrip("/") if value else None
        # The SDK itself honours ANTHROPIC_BASE_URL; resolving it here keeps the endpoint
        # identity truthful about where requests actually go.
        return (os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=True,
            native_tools=True,
            supports_temperature=accepts_temperature(self.model),
        )

    def endpoint_identity(self) -> str:
        base = self._base_url or f"unset:{self._base_url_env}"
        return hash_endpoint(base, self.model, self._cache_namespace)

    # ------------------------------------------------------------------ request construction

    def request_params(self, request: ModelRequest) -> dict[str, Any]:
        """Keyword arguments for ``messages.create`` / ``messages.stream``."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.params.max_output_tokens,
            "messages": self._api_messages(request.messages),
        }
        if request.system:
            params["system"] = [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ]
        output_config: dict[str, Any] = {}
        if request.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
        if request.params.effort is not None:
            output_config["effort"] = request.params.effort
        if output_config:
            params["output_config"] = output_config
        if request.tools:
            params["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "strict": True,
                }
                for tool in request.tools
            ]
        if request.params.temperature is not None and accepts_temperature(self.model):
            # The 1.x SDK has no ``temperature`` keyword (current models reject it), so models
            # that still accept it get it through the request body.
            params["extra_body"] = {"temperature": request.params.temperature}
        return params

    def _api_messages(self, messages: tuple[Message, ...]) -> list[dict[str, Any]]:
        """Map the neutral transcript to API messages.

        Consecutive user-side entries (all tool results of a turn, plus any user text after them)
        are merged into one user message, as the API requires every ``tool_result`` answering a
        turn to arrive together.
        """
        api: list[dict[str, Any]] = []
        for message in messages:
            if message.role is Role.assistant:
                api.append({"role": "assistant", "content": self._assistant_content(message)})
                continue
            if message.role is Role.tool:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            else:
                block = {"type": "text", "text": message.content}
            if api and api[-1]["role"] == "user":
                api[-1]["content"].append(block)
            else:
                api.append({"role": "user", "content": [block]})
        return api

    def _assistant_content(self, message: Message) -> list[dict[str, Any]]:
        if message.tool_calls:
            original = self._tool_turns.get(message.tool_calls[0].id)
            if original is not None:
                return [dict(block) for block in original]
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in message.tool_calls
        )
        return blocks

    # ------------------------------------------------------------------ calling

    async def complete(self, request: ModelRequest) -> ModelResponse:
        start = time.perf_counter()
        response = await self._complete(request)
        response.latency_s = time.perf_counter() - start
        if request.params.temperature is not None and not accepts_temperature(self.model):
            response.raw = {**(response.raw or {}), "ignored_params": ["temperature"]}
        return response

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        if self._base_url is None:
            return self._fail(f"environment variable {self._base_url_env} is not set")
        params = self.request_params(request)
        try:
            client = self._get_client()
            if params["max_tokens"] > STREAMING_THRESHOLD_TOKENS:
                async with client.messages.stream(**params) as stream:
                    message = await stream.get_final_message()
            else:
                message = cast(ApiMessage, await client.messages.create(**params))
        except anthropic.APIStatusError as exc:
            return self._fail(
                f"{type(exc).__name__} {exc.status_code}: {exc.message}",
                raw={"request_id": exc.request_id} if exc.request_id else None,
            )
        except anthropic.APIConnectionError as exc:
            return self._fail(f"{type(exc).__name__}: {exc.message}")
        except anthropic.AnthropicError as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        try:
            return self._to_response(message, request)
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            # The SDK builds responses leniently, so a Messages-compatible server can hand back
            # a shape missing fields a real Message always has; that is a failed call, not a
            # crash.
            return self._fail(f"malformed response ({type(exc).__name__}: {exc})")

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "max_retries": self._max_retries,
                "timeout": self._timeout_s,
            }
            key = os.environ.get(self._api_key_env) if self._api_key_env else None
            if key:
                kwargs["api_key"] = key
            elif self.kind == "anthropic_compatible":
                kwargs["api_key"] = _PLACEHOLDER_API_KEY
            elif self._api_key_env != DEFAULT_API_KEY_ENV:
                raise anthropic.AnthropicError(
                    f"environment variable {self._api_key_env} is not set"
                )
            # With the default variable unset, the SDK resolves ANTHROPIC_AUTH_TOKEN or an
            # `ant auth login` profile itself.
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    # ------------------------------------------------------------------ response parsing

    def _to_response(self, message: ApiMessage, request: ModelRequest) -> ModelResponse:
        # Optional-in-practice fields are read defensively: compatible servers may omit them.
        content = list(getattr(message, "content", None) or [])
        raw: dict[str, Any] = {"id": getattr(message, "id", None)}
        response = ModelResponse(
            stop_reason=getattr(message, "stop_reason", None),
            usage=_usage(getattr(message, "usage", None)),
            served_model=getattr(message, "model", None) or None,
            raw=raw,
        )
        request_id = getattr(message, "_request_id", None)
        if isinstance(request_id, str):
            raw["request_id"] = request_id

        texts = [block.text for block in content if block.type == "text"]
        if response.stop_reason == "refusal":
            # Checked before reading content: a refusal can cut a reply off mid-output.
            response.refused = True
            response.text = "".join(texts) or None
            stop_details = getattr(message, "stop_details", None)
            if stop_details is not None:
                raw["stop_details"] = stop_details.model_dump(mode="json", exclude_none=True)
            return response

        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in content
            if block.type == "tool_use"
        ]
        if calls:
            blocks = [block.model_dump(mode="json", exclude_none=True) for block in content]
            self._remember_tool_turn(blocks, calls)
            raw[ASSISTANT_CONTENT_KEY] = blocks
        response.tool_calls = calls

        if request.output_schema is not None and texts:
            response.text = texts[0]
            response.parsed = load_object(texts[0])
        else:
            response.text = "".join(texts) or None
        return response

    def remember_response(self, response: ModelResponse) -> None:
        """Make a tool-calling response that didn't come from this instance's own ``complete``
        (a cache hit, a recording, a resumed run) replayable with its original content blocks.

        Responses without ``raw["assistant_content"]`` (no tool calls, or recorded by another
        provider) are ignored.
        """
        blocks = (response.raw or {}).get(ASSISTANT_CONTENT_KEY)
        if not response.tool_calls or not isinstance(blocks, list):
            return
        valid = [dict(block) for block in cast(list[Any], blocks) if isinstance(block, dict)]
        if valid:
            self._remember_tool_turn(valid, response.tool_calls)

    def _remember_tool_turn(self, blocks: list[dict[str, Any]], calls: list[ToolCall]) -> None:
        for call in calls:
            self._tool_turns[call.id] = blocks
            self._tool_turns.move_to_end(call.id)
        while len(self._tool_turns) > _TOOL_TURN_CACHE_SIZE:
            self._tool_turns.popitem(last=False)

    def _fail(self, message: str, raw: dict[str, Any] | None = None) -> ModelResponse:
        return ModelResponse(error=redact(f"{self.kind}: {message}"), raw=raw)
