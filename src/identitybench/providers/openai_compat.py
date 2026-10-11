"""OpenAI Chat Completions provider (``openai_compatible``): OpenAI itself or any server that
speaks the same API (vLLM, Ollama, LiteLLM and other gateways) via a base URL from the
environment.

- **Structured output** uses ``response_format`` with a strict JSON schema.
- **Native tools** are strict function tools; tool results go back as ``tool`` messages.
- **Reasoning effort** is sent as ``reasoning_effort`` when configured, unless the server
  doesn't understand it (``reasoning_effort_param=False``); a dropped effort is reported in
  ``raw["ignored_params"]``.
- **Temperature** is sent whenever configured, except to OpenAI reasoning models
  (``NO_TEMPERATURE_MODELS``) at OpenAI's own endpoint: they reject any non-default value with a
  400, so one experiment-wide ``temperature: 0`` would make every call fail. For those it is
  dropped, reported in ``raw["ignored_params"]``, and ``supports_temperature`` is False. Other
  endpoints get the temperature whatever the model is called: a self-hosted server can serve any
  name, and its own model decides what it accepts.
- Usage: ``prompt_tokens`` includes cached tokens, so uncached input is
  ``prompt_tokens - cached_tokens``; ``completion_tokens`` already includes reasoning tokens.
- ``message.refusal`` or a ``content_filter`` finish reason come back as ``refused=True``.
- **Reported cost.** A gateway that bills per call and says so in the response (OpenRouter puts
  the call's USD in ``usage.cost``) gets it recorded as ``reported_cost_usd``, so a
  ``cost_mode: reported`` entry is costed, and budgeted, from what the call actually cost. A
  missing or non-numeric cost stays None (unknown), never 0. The upstream host that served the
  call (OpenRouter's top-level ``provider``) goes to ``raw["upstream_provider"]``, since one
  model name can be served by hosts that answer differently.
- **Routing preferences** (``provider_routing``, OpenRouter's ``provider`` request object: host
  ``order``, ``allow_fallbacks``, ``require_parameters``...) go in the request body. Routing can
  change which host answers, so it is part of the endpoint identity and therefore of every
  cache key.

Base URL and key are always passed to the SDK explicitly, so it never falls back to
``OPENAI_BASE_URL``/``OPENAI_API_KEY`` behind the configuration's back. Without an explicit
``api_key_env``, ``OPENAI_API_KEY`` is used only for the vendor endpoint (no ``base_url_env``,
or ``OPENAI_BASE_URL``, the variable it conventionally pairs with). A custom ``base_url_env``
reads its paired variable instead (``LOCAL_LLM_BASE_URL`` -> ``LOCAL_LLM_API_KEY``, as in
``.env.example``), and sends a placeholder when that is unset, so the SPEC §16 self-hosted entry
works against a key-protected server and never hands a real OpenAI key to a LAN server.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from collections.abc import Mapping
from typing import Any, Literal, cast
from urllib.parse import urlsplit

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
OPENROUTER_BASE_URL_ENV = "OPENROUTER_BASE_URL"
"""The variable ``.env.example`` gives OpenRouter's endpoint. Config files name variables, never
URLs, so this name is how cost accounting recognises an entry as a per-call-billed gateway that
reports its cost (``runner.cost.default_cost_mode``)."""
UNPARSED_ARGUMENTS_KEY = "_unparsed_arguments"
"""``ToolCall.arguments`` key holding the raw string when a tool call's arguments aren't valid
JSON, so the runner can count the call as malformed instead of losing it."""

_PLACEHOLDER_API_KEY = "not-set"

VENDOR_HOST = "api.openai.com"

NO_TEMPERATURE_MODELS: tuple[str, ...] = ("gpt-5", "o1", "o3", "o4")
"""OpenAI reasoning-model families that accept only the default temperature (any other value is
a 400). A model matches when its ID is a family name or starts with one followed by ``-`` or
``.`` (``gpt-5``, ``gpt-5-mini``, ``gpt-5.1``, ``o3-pro``, ``o4-mini``), after an ``ft:``
fine-tune prefix; ``gpt-50`` or ``o1x`` would not."""

MaxTokensParam = Literal["max_completion_tokens", "max_tokens"]

_ROUTING_FLAGS = ("allow_fallbacks", "require_parameters")
"""Boolean keys of a routing preference. YAML ``allow_fallbacks: "false"`` is a truthy string
that would let a request fall back to an unintended host, so these are type-checked."""


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


def _reported_cost(completion: ChatCompletion) -> float | None:
    """USD the endpoint reports for this call (OpenRouter's ``usage.cost``), or None.

    Only a finite, non-negative number counts: an absent, null, string or boolean value is an
    unknown cost, which the budget treats as such rather than as a free call.
    """
    usage = completion.usage
    extra = (usage.model_extra if usage is not None else None) or {}
    cost = extra.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost) if math.isfinite(cost) and cost >= 0 else None


def _upstream_provider(completion: ChatCompletion) -> str | None:
    """The host a gateway routed the call to (OpenRouter's top-level ``provider``), if named."""
    upstream = (completion.model_extra or {}).get("provider")
    return upstream if isinstance(upstream, str) and upstream else None


def provider_routing_problem(value: object) -> str | None:
    """Why ``value`` is not a usable routing preference, or None if it is.

    It must be a non-empty mapping with string keys and JSON values (it is sent as the request
    body's ``provider`` object and hashed into the endpoint identity). The keys OpenRouter
    documents most often are type-checked; other keys pass through, so new routing options
    need no harness change.
    """
    if not isinstance(value, Mapping) or not value:
        return "must be a non-empty mapping"
    routing = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) and key for key in routing):
        return "keys must be non-empty strings"
    try:
        json.dumps(routing, allow_nan=False)
    except (TypeError, ValueError):
        return "values must be JSON (strings, numbers, booleans, lists, mappings)"
    order = routing.get("order")
    if order is not None and (
        not isinstance(order, list)
        or not all(isinstance(host, str) and host for host in cast(list[object], order))
    ):
        return "order must be a list of host names"
    for flag in _ROUTING_FLAGS:
        if flag in routing and not isinstance(routing[flag], bool):
            return f"{flag} must be true or false"
    return None


def _tool_call(call: Any) -> ToolCall | None:
    function = getattr(call, "function", None)
    if function is None:
        return None
    raw_arguments = function.arguments or ""
    arguments = load_object(raw_arguments) if raw_arguments.strip() else {}
    if arguments is None:
        arguments = {UNPARSED_ARGUMENTS_KEY: raw_arguments}
    return ToolCall(id=call.id, name=function.name, arguments=arguments)


def accepts_temperature(model: str) -> bool:
    """False for the OpenAI reasoning models in ``NO_TEMPERATURE_MODELS``."""
    name = model.removeprefix("ft:")
    return not any(
        name == family or name.startswith((f"{family}-", f"{family}."))
        for family in NO_TEMPERATURE_MODELS
    )


def is_vendor_endpoint(base_url: str | None) -> bool:
    """Whether ``base_url`` is OpenAI's own API, where model names mean OpenAI's models."""
    return base_url is not None and urlsplit(base_url).hostname == VENDOR_HOST


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
        provider_routing: Mapping[str, Any] | None = None,
        max_retries: int = 4,
        timeout_s: float = 600.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if max_tokens_param not in ("max_completion_tokens", "max_tokens"):
            raise ValueError(f"unsupported max_tokens_param {max_tokens_param!r}")
        if provider_routing is not None:
            problem = provider_routing_problem(provider_routing)
            if problem is not None:
                raise ValueError(f"provider_routing {problem}")
        self.model = model
        self._base_url_env = base_url_env
        self._api_key_env = (
            api_key_env if api_key_env is not None else default_api_key_env(base_url_env)
        )
        self._cache_namespace = cache_namespace
        self._reasoning_effort_param = reasoning_effort_param
        self._max_tokens_param = max_tokens_param
        # A JSON round trip gives a private copy (the caller's mapping can't change requests
        # after the identity was computed) and is the canonical text the identity hashes.
        self._routing_json = (
            json.dumps(provider_routing, sort_keys=True, separators=(",", ":"))
            if provider_routing is not None
            else None
        )
        self._provider_routing: dict[str, Any] | None = (
            json.loads(self._routing_json) if self._routing_json is not None else None
        )
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._client = client
        self._base_url = self._resolve_base_url()
        self._drops_temperature = is_vendor_endpoint(self._base_url) and not accepts_temperature(
            model
        )

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
            native_structured_output=True,
            native_tools=True,
            supports_temperature=not self._drops_temperature,
        )

    def endpoint_identity(self) -> str:
        """Hash of base URL, model and cache namespace, plus the routing preference when one is
        set (a different host order can produce a different answer). Without routing the parts
        are exactly the earlier ones, so existing cache entries stay valid."""
        base = self._base_url or f"unset:{self._base_url_env}"
        parts = [base, self.model, self._cache_namespace]
        if self._routing_json is not None:
            parts.append(self._routing_json)
        return hash_endpoint(*parts)

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
        if request.params.temperature is not None and not self._drops_temperature:
            params["temperature"] = request.params.temperature
        if request.params.effort is not None and self._reasoning_effort_param:
            params["reasoning_effort"] = request.params.effort
        if self._provider_routing is not None:
            # The SDK merges ``extra_body`` into the JSON body; ``provider`` isn't an SDK field.
            params["extra_body"] = {"provider": self._provider_routing}
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
        ignored = self._ignored_params(request)
        if ignored:
            response.raw = {**(response.raw or {}), "ignored_params": ignored}
        return response

    def _ignored_params(self, request: ModelRequest) -> list[str]:
        """Requested settings this endpoint doesn't receive, recorded so the manifest's
        parameters aren't misread."""
        ignored: list[str] = []
        if request.params.temperature is not None and self._drops_temperature:
            ignored.append("temperature")
        if request.params.effort is not None and not self._reasoning_effort_param:
            ignored.append("effort")
        return ignored

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
            # fields a real completion always has; that is a failed call, not a crash. The call
            # may still have been billed, so its usage and reported cost are kept when readable.
            failed = self._fail(f"malformed response ({type(exc).__name__}: {exc})")
            self._keep_accounting(failed, completion)
            return failed

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

    @staticmethod
    def _keep_accounting(response: ModelResponse, completion: ChatCompletion) -> None:
        """Copy usage, reported cost and upstream host onto a failed ``response`` as far as the
        malformed ``completion`` allows; whatever can't be read stays at its default."""
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            response.usage = _usage(completion)
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            response.reported_cost_usd = _reported_cost(completion)
            upstream = _upstream_provider(completion)
            if upstream is not None:
                response.raw = {**(response.raw or {}), "upstream_provider": upstream}

    def _to_response(self, completion: ChatCompletion, request: ModelRequest) -> ModelResponse:
        response = ModelResponse(
            usage=_usage(completion),
            served_model=completion.model or None,
            reported_cost_usd=_reported_cost(completion),
            raw={"id": completion.id},
        )
        fingerprint = getattr(completion, "system_fingerprint", None)
        if fingerprint and response.raw is not None:
            response.raw["system_fingerprint"] = fingerprint
        upstream = _upstream_provider(completion)
        if upstream is not None and response.raw is not None:
            response.raw["upstream_provider"] = upstream
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
