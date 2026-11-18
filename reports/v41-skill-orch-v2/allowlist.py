"""Subscription CLI and local self-hosted LLM allowlist + secret redaction for Study v2.

Enforces zero-metered-API-spend rules before Turn 1, validates `LOCAL_LLM_BASE_URL` strictly
against loopback / RFC1918 / Tailscale / private LAN targets, blocks HTTP redirects, and scrubs
URLs, hostnames, bearer/basic/JWT/OAuth tokens, API keys, and environment secret values from
logs and error messages.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from identitybench.providers._cli import CliProvider
from identitybench.providers._subprocess import redact as _core_redact
from identitybench.providers.base import Provider
from identitybench.providers.factory import ModelSpec
from identitybench.providers.openai_compat import OpenAICompatProvider

MAX_PROBE_RESPONSE_BYTES = 1_048_576
MAX_PROPS_RESPONSE_BYTES = 65_536

SUBSCRIPTION_CLI_PROVIDERS: frozenset[str] = frozenset(
    {
        "claude_cli",
        "codex_cli",
        "agy_cli",
        "muse_cli",
        "cursor_cli",
        "grok_cli",
    }
)

ALLOWED_LOCAL_BASE_URL_ENV = "LOCAL_LLM_BASE_URL"
ALLOWED_LOCAL_API_KEY_ENVS: frozenset[str | None] = frozenset({None, "LOCAL_LLM_API_KEY"})

FORBIDDEN_METERED_BASE_URL_ENVS: frozenset[str] = frozenset(
    {
        "OPENROUTER_BASE_URL",
        "CPA_OPENAI_BASE_URL",
        "OPENCODE_GO_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    }
)

FORBIDDEN_METERED_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "anthropic_api",
        "anthropic_compatible",
    }
)

SUBSCRIPTION_AND_LOCAL_ALLOWLIST: dict[str, Any] = {
    "subscription_cli_providers": sorted(SUBSCRIPTION_CLI_PROVIDERS),
    "local_openai_compatible": {
        "provider": "openai_compatible",
        "base_url_env": ALLOWED_LOCAL_BASE_URL_ENV,
        "api_key_env": [None, "LOCAL_LLM_API_KEY"],
    },
    "forbidden_base_url_envs": sorted(FORBIDDEN_METERED_BASE_URL_ENVS),
    "forbidden_providers": sorted(FORBIDDEN_METERED_PROVIDERS),
}

_SENSITIVE_ENV_NAMES: tuple[str, ...] = (
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_API_KEY",
    "CPA_OPENAI_BASE_URL",
    "CPA_OPENAI_API_KEY",
    "CPA_CLIENT_API_KEY",
    "OPENCODE_GO_BASE_URL",
    "OPENCODE_GO_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "MUSE_API_KEY",
)

_SENSITIVE_ENV_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_BASE_URL",
    "_URL",
    "_ENDPOINT",
    "_HOST",
    "_AUTH",
    "_PASSWORD",
    "_CREDENTIAL",
    "_CREDENTIALS",
)

_ALLOWED_PRIVATE_IPV4_NETS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
)

_ALLOWED_PRIVATE_IPV6_NETS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
)

_ALLOWED_LOCAL_DNS_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".ts.net",
    ".local",
    ".lan",
    ".internal",
    ".home.arpa",
)

_FORBIDDEN_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.internal",
        "instance-data.ec2.internal",
    }
)

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_ESCAPED_URL_RE = re.compile(r"https?:\\/\\/[^\s\"'<>)\]]+", re.IGNORECASE)
_ENCODED_URL_RE = re.compile(r"https?%3A%2F%2F[^\s\"'<>)\]]+", re.IGNORECASE)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic|Token|ApiKey)\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]{4,})?\b")
_OAUTH_RE = re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}\b")
_COOKIE_HEADER_RE = re.compile(
    r"(?i)((?:\\?[\"'])?\b(?:cookie|set-cookie)\b(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?)"
    r"(?!\[REDACTED)([^\"'\r\n}]+)"
)
_KV_SECRET_RE = re.compile(
    r"(?i)((?:\\?[\"'])?\b(?:"
    r"api[_-]?key|authorization|x-api-key|x-goog-api-key|x-auth-token|x-access-token|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|oauth[_-]?token|"
    r"client[_-]?secret|secret|password"
    r")\b(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?(?:(?:Bearer|Basic|Token|ApiKey)\s+)?)"
    r"(?!\[REDACTED)([^\s\"'\\,;}\]]+)"
)
_KNOWN_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|rk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9_-]{8,}|"
    r"xai-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,})\b"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses all HTTP 3xx redirects when probing `LOCAL_LLM_BASE_URL`."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise PermissionError(f"HTTP redirect ({code}) is prohibited on LOCAL_LLM_BASE_URL.")


def _collect_env_secret_variants(
    raw_val: str, target: set[str], host_targets: set[str] | None = None
) -> None:
    stripped = raw_val.strip()
    if len(stripped) < 4:
        return
    no_slash = stripped.rstrip("/")
    for base in (stripped, no_slash):
        if len(base) >= 4:
            target.add(base)
            target.add(base.replace("/", r"\/"))
            target.add(urllib.parse.quote(base, safe=""))
            target.add(urllib.parse.quote(base, safe=":/"))
    try:
        parsed = urllib.parse.urlsplit(stripped)
    except ValueError:
        return
    if parsed.netloc and len(parsed.netloc) >= 4 and parsed.netloc.lower() != "localhost":
        target.add(parsed.netloc)
        if host_targets is not None:
            host_targets.add(parsed.netloc)
    if parsed.hostname and len(parsed.hostname) >= 4 and parsed.hostname.lower() != "localhost":
        target.add(parsed.hostname)
        if host_targets is not None:
            host_targets.add(parsed.hostname)


def redact_sensitive_text(msg: str) -> str:
    """Scrub URLs, hostnames, bearer/basic/JWT/OAuth tokens, API keys, and env secrets."""
    if not msg:
        return ""
    out = str(_core_redact(str(msg), explicit_env_names=_SENSITIVE_ENV_NAMES))

    secret_values: set[str] = set()
    host_values: set[str] = set()
    for name in _SENSITIVE_ENV_NAMES:
        val = os.environ.get(name)
        if val:
            _collect_env_secret_variants(val, secret_values, host_values)
    for env_key, env_val in os.environ.items():
        upper = env_key.upper()
        if env_val and any(upper.endswith(suf) for suf in _SENSITIVE_ENV_SUFFIXES):
            _collect_env_secret_variants(env_val, secret_values, host_values)

    for secret in sorted(secret_values, key=len, reverse=True):
        if secret:
            out = out.replace(secret, "[REDACTED]")

    # Also harvest hostnames/netlocs from any inline, JSON-escaped, or URL-encoded URLs in `out`
    # before scrubbing URLs so bare/mixed-case hostname repeats in the same log line are redacted!
    for inline_url in _URL_RE.findall(out):
        _collect_env_secret_variants(inline_url, set(), host_values)
    for esc_url in _ESCAPED_URL_RE.findall(out):
        _collect_env_secret_variants(esc_url.replace(r"\/", "/"), set(), host_values)
    for enc_url in _ENCODED_URL_RE.findall(out):
        _collect_env_secret_variants(urllib.parse.unquote(enc_url), set(), host_values)

    out = _ESCAPED_URL_RE.sub("[REDACTED_URL]", out)
    out = _ENCODED_URL_RE.sub("[REDACTED_URL]", out)
    out = _URL_RE.sub("[REDACTED_URL]", out)
    for host_secret in sorted(host_values, key=len, reverse=True):
        if host_secret:
            out = re.sub(re.escape(host_secret), "[REDACTED_HOST]", out, flags=re.IGNORECASE)
    out = _COOKIE_HEADER_RE.sub(r"\1[REDACTED_COOKIE]", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED_SECRET]", out)
    out = _AUTH_SCHEME_RE.sub(r"\1 [REDACTED_TOKEN]", out)
    out = _JWT_RE.sub("[REDACTED_JWT]", out)
    out = _OAUTH_RE.sub("[REDACTED_TOKEN]", out)
    out = _KNOWN_TOKEN_PREFIX_RE.sub("[REDACTED_KEY]", out)
    return out


def validate_local_llm_base_url(raw_url: str) -> None:
    """Validate that `raw_url` uses `http`/`https`, contains no userinfo/query/fragment, and
    targets loopback, RFC1918 private LAN, IPv6 ULA, CGNAT/Tailscale, or approved local DNS.
    """
    cleaned = (raw_url or "").strip()
    if not cleaned:
        raise PermissionError("LOCAL_LLM_BASE_URL is empty or unset.")

    try:
        parsed = urllib.parse.urlsplit(cleaned)
        _ = parsed.port
    except ValueError as exc:
        raise PermissionError(
            redact_sensitive_text(f"Invalid LOCAL_LLM_BASE_URL syntax: {exc}")
        ) from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise PermissionError(
            redact_sensitive_text("LOCAL_LLM_BASE_URL must use http or https scheme.")
        )
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise PermissionError(
            redact_sensitive_text("LOCAL_LLM_BASE_URL must not contain embedded credentials.")
        )
    if parsed.query or parsed.fragment:
        raise PermissionError(
            redact_sensitive_text(
                "LOCAL_LLM_BASE_URL must not contain query or fragment parameters."
            )
        )
    if not parsed.hostname:
        raise PermissionError("LOCAL_LLM_BASE_URL is missing a hostname.")

    host = parsed.hostname.lower().rstrip(".")
    host_no_zone = host.split("%", 1)[0]

    try:
        ip_obj = ipaddress.ip_address(host_no_zone)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped
        if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
            raise PermissionError(
                redact_sensitive_text(
                    "LOCAL_LLM_BASE_URL rejected: link-local, multicast, and unspecified IPs "
                    "are prohibited."
                )
            )
        if isinstance(ip_obj, ipaddress.IPv4Address):
            if any(ip_obj in net for net in _ALLOWED_PRIVATE_IPV4_NETS):
                return
        elif any(ip_obj in net for net in _ALLOWED_PRIVATE_IPV6_NETS):
            return
        raise PermissionError(
            redact_sensitive_text(
                "LOCAL_LLM_BASE_URL rejected: public/non-private IP address is prohibited."
            )
        )

    if host in _FORBIDDEN_METADATA_HOSTS or host.startswith(("metadata.", "instance-data.")):
        raise PermissionError(
            redact_sensitive_text("LOCAL_LLM_BASE_URL rejected: cloud metadata host prohibited.")
        )
    if host in {"localhost", "host.docker.internal"}:
        return
    if any(host.endswith(suf) and len(host) > len(suf) for suf in _ALLOWED_LOCAL_DNS_SUFFIXES):
        return

    raise PermissionError(
        redact_sensitive_text(
            "LOCAL_LLM_BASE_URL rejected: hostname is not loopback, Tailscale, or private LAN."
        )
    )


def assert_model_spec_allowed(spec: ModelSpec | Mapping[str, Any]) -> None:
    """Enforce `SUBSCRIPTION_AND_LOCAL_ALLOWLIST` before Turn 1.

    Raises `PermissionError` if `spec` targets any metered API, OpenRouter, CPA,
    OpenCode Go, `anthropic_api`, custom gateway headers, or non-local `openai_compatible` URL.
    """
    if isinstance(spec, ModelSpec):
        provider = spec.provider
        base_url_env = spec.base_url_env
        api_key_env = spec.api_key_env
        extra: Mapping[str, Any] = spec.extra
        label = spec.display_label
    elif isinstance(spec, Mapping):
        provider = str(spec.get("provider") or "")
        raw_base = spec.get("base_url_env")
        base_url_env = str(raw_base) if raw_base is not None else None
        raw_key = spec.get("api_key_env")
        api_key_env = str(raw_key) if raw_key is not None else None
        raw_extra = spec.get("extra")
        extra = raw_extra if isinstance(raw_extra, Mapping) else {}
        label = str(spec.get("label") or spec.get("model") or provider)
    else:
        raise PermissionError("Unsupported model specification type for allowlist check.")

    if base_url_env in FORBIDDEN_METERED_BASE_URL_ENVS:
        raise PermissionError(
            redact_sensitive_text(
                f"Model {label!r} rejected by SUBSCRIPTION_AND_LOCAL_ALLOWLIST: "
                f"metered/gateway base_url_env {base_url_env!r} is strictly prohibited."
            )
        )

    if provider in FORBIDDEN_METERED_PROVIDERS:
        raise PermissionError(
            redact_sensitive_text(
                f"Model {label!r} rejected by SUBSCRIPTION_AND_LOCAL_ALLOWLIST: "
                f"metered provider {provider!r} is strictly prohibited."
            )
        )

    if provider in SUBSCRIPTION_CLI_PROVIDERS:
        if base_url_env is not None or api_key_env is not None:
            raise PermissionError(
                redact_sensitive_text(
                    f"CLI model {label!r} ({provider}) must not set base_url_env or api_key_env."
                )
            )
        return

    if provider == "openai_compatible":
        if base_url_env != ALLOWED_LOCAL_BASE_URL_ENV:
            raise PermissionError(
                redact_sensitive_text(
                    f"Model {label!r} rejected by SUBSCRIPTION_AND_LOCAL_ALLOWLIST: "
                    f"openai_compatible requires base_url_env=='LOCAL_LLM_BASE_URL' "
                    f"(got {base_url_env!r})."
                )
            )
        if api_key_env not in ALLOWED_LOCAL_API_KEY_ENVS:
            raise PermissionError(
                redact_sensitive_text(
                    f"Model {label!r} rejected by SUBSCRIPTION_AND_LOCAL_ALLOWLIST: "
                    f"openai_compatible api_key_env must be None or 'LOCAL_LLM_API_KEY' "
                    f"(got {api_key_env!r})."
                )
            )
        if extra.get("provider_routing") is not None:
            raise PermissionError(
                redact_sensitive_text(
                    f"Model {label!r} rejected: OpenRouter provider_routing is prohibited."
                )
            )
        if extra.get("headers") is not None:
            raise PermissionError(
                redact_sensitive_text(
                    f"Model {label!r} rejected: custom gateway headers are prohibited."
                )
            )
        env_url = (os.environ.get(ALLOWED_LOCAL_BASE_URL_ENV) or "").strip()
        if env_url:
            validate_local_llm_base_url(env_url)
        return

    raise PermissionError(
        redact_sensitive_text(
            f"Model {label!r} rejected by SUBSCRIPTION_AND_LOCAL_ALLOWLIST: "
            f"provider {provider!r} is not an approved subscription CLI or local LLM server."
        )
    )


def assert_provider_allowed(provider: Provider, *, allow_offline_mock: bool = False) -> None:
    """Verify a constructed `Provider` instance complies with `SUBSCRIPTION_AND_LOCAL_ALLOWLIST`."""
    cur: Any = provider
    while hasattr(cur, "_inner"):
        cur = cur._inner

    kind = str(getattr(cur, "kind", ""))
    if allow_offline_mock and kind in {"mock", "fake", "test_mock"}:
        return

    if isinstance(cur, CliProvider) and kind in SUBSCRIPTION_CLI_PROVIDERS:
        return

    if isinstance(cur, OpenAICompatProvider) and kind == "openai_compatible":
        base_env = getattr(cur, "_base_url_env", None)
        key_env = getattr(cur, "_api_key_env", None)
        if base_env != ALLOWED_LOCAL_BASE_URL_ENV or key_env not in ALLOWED_LOCAL_API_KEY_ENVS:
            raise PermissionError(
                redact_sensitive_text(
                    f"Provider {kind!r} rejected: non-local env ({base_env!r}, {key_env!r})."
                )
            )
        if getattr(cur, "_provider_routing", None) is not None:
            raise PermissionError("Provider rejected: OpenRouter provider_routing prohibited.")
        if getattr(cur, "_headers", None) is not None:
            raise PermissionError("Provider rejected: custom gateway headers prohibited.")
        resolved_url = str(
            getattr(cur, "_base_url", None) or os.environ.get(ALLOWED_LOCAL_BASE_URL_ENV) or ""
        )
        validate_local_llm_base_url(resolved_url)
        return

    raise PermissionError(
        redact_sensitive_text(
            f"Provider {type(cur).__name__} (kind={kind!r}) rejected by "
            "SUBSCRIPTION_AND_LOCAL_ALLOWLIST."
        )
    )


def _classify_served_models(served_models: list[str]) -> tuple[str, int, str]:
    """Select local model ID, recommended concurrency, and `t0_source` tag."""
    if "qwen38" in served_models:
        return ("qwen38", 16, "qwen3.8-27b-exl3-4bit-local")
    qwen_matches = [m for m in served_models if "qwen" in m.lower()]
    if qwen_matches:
        chosen = served_models[0] if "qwen" in served_models[0].lower() else qwen_matches[0]
        return (chosen, 2, "qwen3.8-flash-next-local")
    if served_models:
        return (served_models[0], 1, "custom-local")
    return ("", 0, "unavailable")


def _unreachable_probe() -> dict[str, Any]:
    return {
        "reachable": False,
        "served_models": [],
        "selected_model": "",
        "recommended_concurrency": 0,
        "t0_source": "unavailable",
    }


def probe_local_llm_server(timeout_s: float = 5.0) -> dict[str, Any]:
    """Query `LOCAL_LLM_BASE_URL/models` (and `{root}/props`) without leaking URLs or keys."""
    raw_base = (os.environ.get(ALLOWED_LOCAL_BASE_URL_ENV) or "").strip().rstrip("/")
    if not raw_base:
        return _unreachable_probe()
    validate_local_llm_base_url(raw_base)

    headers: dict[str, str] = {"Accept": "application/json"}
    raw_key = (os.environ.get("LOCAL_LLM_API_KEY") or "").strip()
    if raw_key:
        headers["Authorization"] = f"Bearer {raw_key}"

    req = urllib.request.Request(f"{raw_base}/models", headers=headers, method="GET")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            raw_bytes = resp.read(MAX_PROBE_RESPONSE_BYTES + 1)
        if len(raw_bytes) > MAX_PROBE_RESPONSE_BYTES:
            return _unreachable_probe()
        payload = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except PermissionError:
        raise
    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        return _unreachable_probe()

    served: list[str] = []
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                    clean_id = redact_sensitive_text(item["id"]).strip()
                    if clean_id:
                        served.append(clean_id)
    selected_model, rec_conc, t0_source = _classify_served_models(served)

    # Probe llama-server `{root}/props` for `total_slots` (vLLM returns 404 -> ignored silently).
    root_base = raw_base[:-3].rstrip("/") if raw_base.endswith("/v1") else raw_base
    detected_slots: int | None = None
    if root_base:
        props_req = urllib.request.Request(f"{root_base}/props", headers=headers, method="GET")
        try:
            with opener.open(props_req, timeout=timeout_s) as p_resp:
                props_bytes = p_resp.read(MAX_PROPS_RESPONSE_BYTES + 1)
            if len(props_bytes) <= MAX_PROPS_RESPONSE_BYTES:
                props_obj = json.loads(props_bytes.decode("utf-8", errors="replace"))
                if isinstance(props_obj, Mapping):
                    raw_slots = props_obj.get("total_slots")
                    if (
                        isinstance(raw_slots, int)
                        and not isinstance(raw_slots, bool)
                        and raw_slots >= 1
                    ):
                        detected_slots = raw_slots
        except PermissionError:
            raise
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            http.client.HTTPException,
        ):
            detected_slots = None

    if detected_slots is not None and rec_conc > 0:
        rec_conc = min(rec_conc, detected_slots)

    result: dict[str, Any] = {
        "reachable": True,
        "served_models": served,
        "selected_model": redact_sensitive_text(selected_model),
        "recommended_concurrency": rec_conc,
        "t0_source": t0_source,
    }
    if detected_slots is not None:
        result["server_total_slots"] = detected_slots
    return result
