"""OpenAI Chat Completions provider (``openai_compatible``): OpenAI itself or any server that
speaks the same API (vLLM, Ollama, LiteLLM and other gateways) via a base URL from the
environment.

- **Structured output** uses ``response_format`` with a strict JSON schema.
- **Native tools** are strict function tools; tool results go back as ``tool`` messages.
- **Reasoning effort** is sent as ``reasoning_effort`` when configured, unless the server
  doesn't understand it (``reasoning_effort_param=False``); a dropped effort is reported in
  ``raw["ignored_params"]``.
- **Temperature** is sent whenever configured. Some OpenAI reasoning models accept only the
  default; configure ``temperature`` per model rather than globally for those.
- Usage: ``prompt_tokens`` includes cached tokens, so uncached input is
  ``prompt_tokens - cached_tokens``; ``completion_tokens`` already includes reasoning tokens.
- ``message.refusal`` or a ``content_filter`` finish reason come back as ``refused=True``.

Base URL and key are always passed to the SDK explicitly, so it never falls back to
``OPENAI_BASE_URL``/``OPENAI_API_KEY`` behind the configuration's back. Without an explicit
``api_key_env``, ``OPENAI_API_KEY`` is used only for the vendor endpoint (no ``base_url_env``,
or ``OPENAI_BASE_URL``, the variable it conventionally pairs with). A custom ``base_url_env``
reads its paired variable instead (``LOCAL_LLM_BASE_URL`` -> ``LOCAL_LLM_API_KEY``, as in
``.env.example``), and sends a placeholder when that is unset, so the SPEC §16 self-hosted entry
works against a key-protected server and never hands a real OpenAI key to a LAN server.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal, cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_BASE_URL_ENV = "OPENAI_BASE_URL"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
UNPARSED_ARGUMENTS_KEY = "_unparsed_arguments"
"""``ToolCall.arguments`` key holding the raw string when a tool call's arguments aren't valid
JSON, so the runner can count the call as malformed instead of losing it."""

_PLACEHOLDER_API_KEY = "not-set"

MaxTokensParam = Literal["max_completion_tokens", "max_tokens"]


def _usage(completion: ChatCompletion) -> Usage:
    usage = completion.usage
    if usage is None:
        return Usage()
    prompt_details = usage.prompt_tokens_details
    completion_details = usage.completion_tokens_details
    cached = (prompt_details.cached_tokens or 0) if prompt_details is not None else 0
    cache_write = getattr(prompt_details, "cache_write_tokens", None) or 0
    reasoning = (completion_details.reasoning_tokens or 0) if completion_details is not None else 0
    prompt = usage.prompt_tokens or 0
    return Usage(
        input_tokens=max(prompt - cached - cache_write, 0),
        output_tokens=usage.completion_tokens or 0,
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _tool_call(call: Any) -> ToolCall | None:
    function = getattr(call, "function", None)
    if function is None:
        return None
    raw_arguments = function.arguments or ""
    arguments = load_object(raw_arguments) if raw_arguments.strip() else {}
    if arguments is None:
        arguments = {UNPARSED_ARGUMENTS_KEY: raw_arguments}
    return ToolCall(id=call.id, name=function.name, arguments=arguments)


def default_api_key_env(base_url_env: str | None) -> str | None:
    """The key variable used when none is configured: ``OPENAI_API_KEY`` for the vendor endpoint,
    else the custom base URL variable's paired key variable (``paired_api_key_env``)."""
    if base_url_env is None or base_url_env == DEFAULT_BASE_URL_ENV:
        return DEFAULT_API_KEY_ENV
    return paired_api_key_env(base_url_env)


class OpenAICompatProvider:
    """OpenAI or an OpenAI-compatible server via Chat Completions."""

    kind = "openai_compatible"

    def __init__(
        self,
        model: str,
        *,
        api_key_env: str | None = None,
        base_url_env: str | None = DEFAULT_BASE_URL_ENV,
        cache_namespace: str | None = None,
        reasoning_effort_param: bool = True,
        max_tokens_param: MaxTokensParam = "max_completion_tokens",
        max_retries: int = 4,
        timeout_s: float = 600.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if max_tokens_param not in ("max_completion_tokens", "max_tokens"):
            raise ValueError(f"unsupported max_tokens_param {max_tokens_param!r}")
        self.model = model
        self._base_url_env = base_url_env
        self._api_key_env = (
            api_key_env if api_key_env is not None else default_api_key_env(base_url_env)
        )
        self._cache_namespace = cache_namespace
        self._reasoning_effort_param = reasoning_effort_param
        self._max_tokens_param = max_tokens_param
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._client = client
        self._base_url = self._resolve_base_url()

    def _resolve_base_url(self) -> str | None:
        if self._base_url_env is None:
            return DEFAULT_BASE_URL
        value = os.environ.get(self._base_url_env)
        if value:
            return value.rstrip("/")
        # Only the default variable falls back to OpenAI; an unset custom variable (say
        # LOCAL_LLM_BASE_URL) is a configuration error, never a silent switch to OpenAI.
        return DEFAULT_BASE_URL if self._base_url_env == DEFAULT_BASE_URL_ENV else None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=True, native_tools=True, supports_temperature=True
        )

    def endpoint_identity(self) -> str:
        base = self._base_url or f"unset:{self._base_url_env}"
        return hash_endpoint(base, self.model, self._cache_namespace)

    # ------------------------------------------------------------------ request construction

    def request_params(self, request: ModelRequest) -> dict[str, Any]:
        """Keyword arguments for ``chat.completions.create``."""
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(self._api_message(message) for message in request.messages)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            self._max_tokens_param: request.params.max_output_tokens,
        }
        if request.output_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                },
            }
        if request.tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                        "strict": True,
                    },
                }
                for tool in request.tools
            ]
        if request.params.temperature is not None:
            params["temperature"] = request.params.temperature
        if request.params.effort is not None and self._reasoning_effort_param:
            params["reasoning_effort"] = request.params.effort
        return params

    @staticmethod
    def _api_message(message: Message) -> dict[str, Any]:
        if message.role is Role.tool:
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        if message.role is Role.assistant:
            api: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                api["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            return api
        return {"role": "user", "content": message.content}

    # ------------------------------------------------------------------ calling

    async def complete(self, request: ModelRequest) -> ModelResponse:
        start = time.perf_counter()
        response = await self._complete(request)
        response.latency_s = time.perf_counter() - start
        if request.params.effort is not None and not self._reasoning_effort_param:
            response.raw = {**(response.raw or {}), "ignored_params": ["effort"]}
        return response

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        if self._base_url is None:
            return self._fail(f"environment variable {self._base_url_env} is not set")
        params = self.request_params(request)
        try:
            client = self._get_client()
            completion = cast(ChatCompletion, await client.chat.completions.create(**params))
        except openai.APIStatusError as exc:
            return self._fail(
                f"{type(exc).__name__} {exc.status_code}: {exc.message}",
                raw={"request_id": exc.request_id} if exc.request_id else None,
            )
        except openai.APIConnectionError as exc:
            return self._fail(f"{type(exc).__name__}: {exc.message}")
        except openai.OpenAIError as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        try:
            return self._to_response(completion, request)
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            # A compatible server can return a shape the SDK accepted leniently but that lacks
            # fields a real completion always has; that is a failed call, not a crash.
            return self._fail(f"malformed response ({type(exc).__name__}: {exc})")

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            key = os.environ.get(self._api_key_env) if self._api_key_env else None
            if not key:
                if self._base_url == DEFAULT_BASE_URL:
                    name = self._api_key_env or DEFAULT_API_KEY_ENV
                    raise openai.OpenAIError(f"environment variable {name} is not set")
                # Self-hosted servers often run without auth; a placeholder is harmless there.
                key = _PLACEHOLDER_API_KEY
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=self._base_url,
                max_retries=self._max_retries,
                timeout=self._timeout_s,
            )
        return self._client

    # ------------------------------------------------------------------ response parsing

    def _to_response(self, completion: ChatCompletion, request: ModelRequest) -> ModelResponse:
        response = ModelResponse(
            usage=_usage(completion),
            served_model=completion.model or None,
            raw={"id": completion.id},
        )
        fingerprint = getattr(completion, "system_fingerprint", None)
        if fingerprint and response.raw is not None:
            response.raw["system_fingerprint"] = fingerprint
        if not completion.choices:
            response.error = redact(f"{self.kind}: response has no choices")
            return response

        choice = completion.choices[0]
        message = choice.message
        response.stop_reason = choice.finish_reason
        response.text = message.content or None
        if message.refusal or choice.finish_reason == "content_filter":
            response.refused = True
            if message.refusal and response.raw is not None:
                response.raw["refusal"] = message.refusal
            return response

        response.tool_calls = [
            call for call in (_tool_call(item) for item in message.tool_calls or []) if call
        ]
        if request.output_schema is not None and response.text:
            response.parsed = load_object(response.text)
        return response

    def _fail(self, message: str, raw: dict[str, Any] | None = None) -> ModelResponse:
        return ModelResponse(error=redact(f"{self.kind}: {message}"), raw=raw)
