"""OpenAI provider (``openai_compatible``): OpenAI itself or any server that speaks the same API
(vLLM, Ollama, LiteLLM and other gateways) via a base URL from the environment.

- **API.** Chat Completions by default; ``api="responses"`` uses the Responses API instead, for
  models a server offers only there. The same request goes out either way: the system prompt as
  ``instructions``, the conversation as ``input`` items (tool calls and results as
  ``function_call``/``function_call_output`` items), structured output as ``text.format``,
  effort as ``reasoning.effort``, and ``store: false`` so the endpoint keeps nothing. Replies
  are read into the same response fields, with Chat Completions' stop reasons (``stop``,
  ``length`` for an answer cut at ``max_output_tokens``, ``tool_calls``, ``content_filter``),
  and a ``failed`` response is an error. The API is part of the endpoint identity.

- **Structured output** uses ``response_format`` (``text.format`` on the Responses API) with a
  strict JSON schema.
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
  ``prompt_tokens - cached_tokens``; ``completion_tokens`` already includes reasoning tokens
  (likewise ``input_tokens`` and ``output_tokens`` on the Responses API).
- ``message.refusal`` (a ``refusal`` content part) or a ``content_filter`` finish reason come
  back as ``refused=True``.
- **Reported cost.** A gateway that bills per call and says so in the response (OpenRouter puts
  the call's USD in ``usage.cost``) gets it recorded as ``reported_cost_usd``, so a
  ``cost_mode: reported`` entry is costed, and budgeted, from what the call actually cost. A
  missing or non-numeric cost stays None (unknown), never 0. With BYOK (``usage.is_byok``) the
  cost is OpenRouter's fee plus ``cost_details.upstream_inference_cost``, and ``raw["is_byok"]``
  is set. The upstream host that served the call (OpenRouter's top-level ``provider``) goes to
  ``raw["upstream_provider"]``, since one model name can be served by hosts that answer
  differently and charge differently.
- **Routing preferences** (``provider_routing``, OpenRouter's ``provider`` request object: host
  ``order``, ``allow_fallbacks``, ``require_parameters``...) go in the request body. Routing can
  change which host answers, so it is part of the endpoint identity and therefore of every
  cache key.
- **Extra headers** (``headers``) go on every request, for gateways that route on a header
  (OpenCode Go rejects requests without ``x-opencode-session``). They steer delivery, not the
  answer, so they are not part of the endpoint identity; credentials never go here
  (``Authorization`` and ``api-key`` are rejected), only through ``api_key_env``.

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
from openai.types.responses import Response

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
Api = Literal["chat_completions", "responses"]
APIS: tuple[str, ...] = ("chat_completions", "responses")
"""The OpenAI APIs a model can be called through (``extra.api``)."""

_ROUTING_FLAGS = ("allow_fallbacks", "require_parameters")
"""Boolean keys of a routing preference. YAML ``allow_fallbacks: "false"`` is a truthy string
that would let a request fall back to an unintended host, so these are type-checked."""


Reply = ChatCompletion | Response
"""A parsed reply from either API."""


def _usage(reply: Reply) -> Usage:
    if isinstance(reply, Response):
        return _responses_usage(reply)
    usage = reply.usage
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


def _responses_usage(response: Response) -> Usage:
    """Usage from a Responses API reply. The SDK types every detail as required but builds
    replies without validating them, and compatible servers leave details out (no
    ``cache_write_tokens``, say), so each one is read as optional."""
    usage = response.usage
    if usage is None:
        return Usage()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    cached = getattr(input_details, "cached_tokens", None) or 0
    cache_write = getattr(input_details, "cache_write_tokens", None) or 0
    reasoning = getattr(output_details, "reasoning_tokens", None) or 0
    return Usage(
        input_tokens=max((getattr(usage, "input_tokens", None) or 0) - cached - cache_write, 0),
        output_tokens=getattr(usage, "output_tokens", None) or 0,
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _usd(value: object) -> float | None:
    """``value`` as a USD amount if it is a finite, non-negative number, else None.

    An absent, null, string or boolean value is an unknown cost, which the budget treats as
    such rather than as a free call. So is an integer too large for a float: the server is
    untrusted, and a strange value must make a failed figure, not a crashed run.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        usd = float(value)
    except OverflowError:
        return None
    return usd if math.isfinite(usd) and usd >= 0 else None


def _usage_extra(completion: Reply) -> dict[str, Any]:
    """The non-OpenAI fields of ``usage`` (where OpenRouter puts cost and BYOK details)."""
    usage = completion.usage
    return (usage.model_extra if usage is not None else None) or {}


def _is_byok(completion: Reply) -> bool:
    """Whether the call ran on the user's own upstream key (OpenRouter's ``usage.is_byok``)."""
    return _usage_extra(completion).get("is_byok") is True


def _reported_cost(completion: Reply) -> float | None:
    """USD the endpoint reports for this call (OpenRouter's ``usage.cost``), or None.

    With BYOK, ``cost`` is only OpenRouter's fee; the upstream host bills the user's own key
    ``cost_details.upstream_inference_cost``, so the call's cost is the sum of the two, and
    unknown when the upstream part is missing (counting the fee alone would let a budget
    under-count real spend many times over).
    """
    extra = _usage_extra(completion)
    cost = _usd(extra.get("cost"))
    if cost is None or not _is_byok(completion):
        return cost
    details = extra.get("cost_details")
    upstream = (
        _usd(cast(dict[str, object], details).get("upstream_inference_cost"))
        if isinstance(details, dict)
        else None
    )
    return cost + upstream if upstream is not None else None


def _gateway_raw(completion: Reply) -> dict[str, Any]:
    """What a gateway says about how it served the call, for ``ModelResponse.raw``: the host it
    routed to (OpenRouter's top-level ``provider``; one model name can be served by hosts that
    answer differently) and whether the call ran on the user's own key (``is_byok``)."""
    raw: dict[str, Any] = {}
    upstream = (completion.model_extra or {}).get("provider")
    if isinstance(upstream, str) and upstream:
        raw["upstream_provider"] = upstream
    if _is_byok(completion):
        raw["is_byok"] = True
    return raw


_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "api-key",
        "x-api-key",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-auth-token",
        "x-access-token",
    }
)


def headers_problem(value: object) -> str | None:
    """Why ``value`` is not a usable ``headers`` mapping, or None if it is: a non-empty mapping
    of non-empty string names to string values, with no credential header (keys come from
    ``api_key_env``, never from a config file)."""
    if not isinstance(value, Mapping) or not value:
        return "must be a non-empty mapping"
    headers = cast(Mapping[object, object], value)
    for name, header in headers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(header, str):
            return "must map non-empty header names to string values"
        if name.lower() in _CREDENTIAL_HEADERS:
            return f"must not set {name!r}: credentials come from api_key_env"
    return None


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
        # Exactly the serialisation ``OpenAICompatProvider`` hashes, so a value that passes
        # here (say a nested mapping mixing int and str keys, which can't be sorted) can't
        # fail later when the provider is built.
        json.dumps(routing, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    """OpenAI or an OpenAI-compatible server via Chat Completions or the Responses API."""

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
        headers: Mapping[str, str] | None = None,
        api: Api = "chat_completions",
        max_retries: int = 4,
        timeout_s: float = 600.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if max_tokens_param not in ("max_completion_tokens", "max_tokens"):
            raise ValueError(f"unsupported max_tokens_param {max_tokens_param!r}")
        if api not in APIS:
            raise ValueError(f"unsupported api {api!r} (one of {list(APIS)})")
        if api == "responses" and max_tokens_param != "max_completion_tokens":
            raise ValueError("max_tokens_param does not apply to api 'responses'")
        if provider_routing is not None:
            problem = provider_routing_problem(provider_routing)
            if problem is not None:
                raise ValueError(f"provider_routing {problem}")
        if headers is not None:
            problem = headers_problem(headers)
            if problem is not None:
                raise ValueError(f"headers {problem}")
        self.model = model
        self._base_url_env = base_url_env
        self._api_key_env = (
            api_key_env if api_key_env is not None else default_api_key_env(base_url_env)
        )
        self._cache_namespace = cache_namespace
        self._reasoning_effort_param = reasoning_effort_param
        self._max_tokens_param = max_tokens_param
        self.api: Api = api
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
        self._headers: dict[str, str] | None = dict(headers) if headers is not None else None
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
        set (a different host order can produce a different answer) and the API when it is the
        Responses API (the same model can answer differently there). Without either the parts
        are exactly the earlier ones, so existing cache entries stay valid."""
        base = self._base_url or f"unset:{self._base_url_env}"
        parts = [base, self.model, self._cache_namespace]
        if self._routing_json is not None:
            parts.append(self._routing_json)
        if self.api != "chat_completions":
            parts.append(f"api={self.api}")
        return hash_endpoint(*parts)

    # ------------------------------------------------------------------ request construction

    def request_params(self, request: ModelRequest) -> dict[str, Any]:
        """Keyword arguments for ``chat.completions.create`` (``responses.create`` when the
        provider uses the Responses API)."""
        if self.api == "responses":
            return self._responses_params(request)
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

    def _responses_params(self, request: ModelRequest) -> dict[str, Any]:
        """Keyword arguments for ``responses.create``: what ``request_params`` sends to Chat
        Completions, in the Responses API's shape."""
        params: dict[str, Any] = {"model": self.model}
        if request.system:
            params["instructions"] = request.system
        params["input"] = [
            item for message in request.messages for item in self._input_items(message)
        ]
        params["max_output_tokens"] = request.params.max_output_tokens
        # Responses are stored server-side by default; Chat Completions' are not.
        params["store"] = False
        if request.output_schema is not None:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                }
            }
        if request.tools:
            params["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": True,
                }
                for tool in request.tools
            ]
        if request.params.temperature is not None and not self._drops_temperature:
            params["temperature"] = request.params.temperature
        if request.params.effort is not None and self._reasoning_effort_param:
            params["reasoning"] = {"effort": request.params.effort}
        if self._provider_routing is not None:
            params["extra_body"] = {"provider": self._provider_routing}
        return params

    @staticmethod
    def _input_items(message: Message) -> list[dict[str, Any]]:
        """A conversation message as Responses API input items."""
        if message.role is Role.tool:
            return [
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            ]
        if message.role is Role.assistant:
            items: list[dict[str, Any]] = []
            if message.content:
                items.append({"role": "assistant", "content": message.content})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
            return items
        return [{"role": "user", "content": message.content}]

    def _api_message(self, message: Message) -> dict[str, Any]:
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
            if "reflection/" in self.model or "beam-1.0" in self.model:
                api["reasoning_content"] = message.content or " "
                api["reasoning"] = message.content or " "
            if "cohere/" in self.model and not message.tool_calls and not message.content:
                api["content"] = " "
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
            completion: Reply
            if self.api == "responses":
                completion = cast(Response, await client.responses.create(**params))
            else:
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
        except ValueError as exc:
            # The SDK parses the body itself; a gateway that cuts a response short (seen on
            # OpenRouter) raises json.JSONDecodeError there. That is a failed call to retry,
            # not a crash that ends the run.
            return self._fail(f"unparseable response body ({type(exc).__name__}: {exc})")
        try:
            if isinstance(completion, Response):
                return self._from_responses(completion, request)
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
                default_headers=self._headers,
            )
        return self._client

    # ------------------------------------------------------------------ response parsing

    @staticmethod
    def _keep_accounting(response: ModelResponse, completion: Reply) -> None:
        """Copy usage, reported cost and upstream host onto a failed ``response`` as far as the
        malformed ``completion`` allows; whatever can't be read stays at its default."""
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            response.usage = _usage(completion)
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            response.reported_cost_usd = _reported_cost(completion)
            gateway = _gateway_raw(completion)
            if gateway:
                response.raw = {**(response.raw or {}), **gateway}

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
        if response.raw is not None:
            response.raw.update(_gateway_raw(completion))
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

    def _from_responses(self, reply: Response, request: ModelRequest) -> ModelResponse:
        """A Responses API reply as a ``ModelResponse``, read like a chat completion: output text
        from the ``message`` items (reasoning items are skipped), ``function_call`` items as tool
        calls, and the status as a Chat Completions stop reason."""
        response = ModelResponse(
            usage=_usage(reply),
            served_model=reply.model or None,
            reported_cost_usd=_reported_cost(reply),
            raw={"id": reply.id, **_gateway_raw(reply)},
        )
        if reply.status == "failed" or reply.error is not None:
            error = reply.error
            detail = f"{error.code}: {error.message}" if error is not None else "no error given"
            response.error = redact(f"{self.kind}: response failed ({detail})")
            return response

        texts: list[str] = []
        refusals: list[str] = []
        calls: list[ToolCall] = []
        for item in reply.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        texts.append(part.text)
                    elif part.type == "refusal":
                        refusals.append(part.refusal)
            elif item.type == "function_call":
                raw_arguments = item.arguments or ""
                arguments = load_object(raw_arguments) if raw_arguments.strip() else {}
                if arguments is None:
                    arguments = {UNPARSED_ARGUMENTS_KEY: raw_arguments}
                calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
        response.text = "".join(texts) or None

        reason = reply.incomplete_details.reason if reply.incomplete_details else None
        if reply.status == "incomplete":
            response.stop_reason = (
                "length" if reason == "max_output_tokens" else reason or "incomplete"
            )
        else:
            response.stop_reason = "tool_calls" if calls else "stop"
        if refusals or reason == "content_filter":
            response.refused = True
            if reason == "content_filter":
                response.stop_reason = "content_filter"
            if refusals and response.raw is not None:
                response.raw["refusal"] = "".join(refusals)
            return response

        response.tool_calls = calls
        if request.output_schema is not None and response.text:
            response.parsed = load_object(response.text)
        return response

    def _fail(self, message: str, raw: dict[str, Any] | None = None) -> ModelResponse:
        return ModelResponse(error=redact(f"{self.kind}: {message}"), raw=raw)
