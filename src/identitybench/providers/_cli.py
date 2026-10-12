"""Shared machinery for the subscription-CLI providers (``claude``, ``codex``, ``grok``, Cursor).

Each CLI call runs in a fresh, empty temporary working directory so the agent has nothing to
read, no project instructions to pick up and nowhere useful to write. Prompt, schema and output
files live in a sibling directory that is never the agent's cwd. Credentials the CLI shouldn't
see (API keys that would switch it off its subscription login, parent-session variables) are
removed from the child environment.

A CLI that loads the user's own setup from its home directory whatever the cwd (global rules,
skills, MCP servers, hooks, memory) also gets a private, empty home for the call
(``CliProvider._isolate``): ``HOME`` and the CLI's config-dir variable point into the call's
temporary directory, which holds only links to the real login files. Without that, every row
would score a prompt that silently differs between machines.

The CLIs can't take a multi-turn transcript or native tools, so transcripts are rendered into a
single user message and requests with tools are rejected (``capabilities().native_tools`` is
False for every CLI provider).

A CLI's endpoint identity (SPEC §11.4) covers more than its version and the model: the harness's
own setup (fixed flags, disabled features, the private home's contents, environment rules) and
the provider's default effort change the prompt or the model's behaviour too, so they are hashed
in as well (``CliProvider.setup_revision``). Changing that setup invalidates cached responses
instead of serving them as if the new setup had produced them.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, TypedDict

from identitybench.providers._json import load_object
from identitybench.providers._subprocess import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_S,
    Command,
    ProcessResult,
    ProcessRunner,
    is_transient_error,
    redact,
    retry_transient,
    run_command,
    tail,
)
from identitybench.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Role,
    ToolCall,
    hash_endpoint,
)

logger = logging.getLogger("identitybench.providers")

SYSTEM_BEGIN = "===== SYSTEM INSTRUCTIONS ====="
SYSTEM_END = "===== END SYSTEM INSTRUCTIONS ====="

MAX_ARG_BYTES = 100_000
"""Largest single argv element we pass. Linux caps one argument at 128 KiB (MAX_ARG_STRLEN) and
macOS caps argv+env at 1 MiB in total, so anything bigger moves into stdin or a file."""

SYSTEM_POINTER = (
    "Your instructions are at the start of the user message, between the lines "
    f"{SYSTEM_BEGIN!r} and {SYSTEM_END!r}. Treat them as your system prompt."
)
"""Short system prompt used when the real one is too large for argv and moves into the prompt."""

NEUTRAL_SYSTEM = "You are a helpful assistant."
"""System prompt sent in place of an empty one by CLIs that replace their own system prompt.

An empty value doesn't mean "no system prompt" to these CLIs: grok treats
``--system-prompt-override ""`` as unset and falls back to its built-in coding-agent prompt
(measured on grok 1.0.46: ~2.8K input tokens with an empty override against ~1.8K with a short
one), and claude isn't trusted to do better with ``--system-prompt ""``. The API providers send
no system prompt at all, so the CLI rows get the closest equivalent instead: the default that
chat templates conventionally apply to a conversation without a system message, which carries no
task instructions. Responses record the substitution in ``raw["substituted_system"]``.
"""

ACTION_SCHEMA_NAME = "tool_action"
"""The schema name of a turn of the JSON action protocol (``render.tools.ACTION_SCHEMA_NAME``;
a test pins the two together, since providers don't import the renderer). A CLI may send such
a turn differently from a plain structured answer (``claude_cli`` withholds ``--json-schema``),
so ``setup_revision`` builds one as a template too."""

_EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def prepend_system(system: str, body: str) -> str:
    """Put the system text at the top of the prompt between unambiguous delimiter lines."""
    if not system:
        return body
    return f"{SYSTEM_BEGIN}\n{system}\n{SYSTEM_END}\n\n{body}"


def system_override(system: str, body: str, *, prefix: str = "") -> tuple[str, str, dict[str, Any]]:
    """``(system argument, prompt body, raw notes)`` for a CLI that replaces its system prompt.

    - An empty or whitespace-only ``system`` becomes ``NEUTRAL_SYSTEM`` (noted in the raw notes),
      so the CLI never falls back to its own agent prompt.
    - A ``system`` too large for one argv element (``prefix`` is whatever shares the element with
      it, such as an attached ``--flag=``) moves to the top of the body between delimiter lines,
      and the argument becomes ``SYSTEM_POINTER``.
    """
    if not system.strip():
        return NEUTRAL_SYSTEM, body, {"substituted_system": NEUTRAL_SYSTEM}
    if exceeds_arg_limit(prefix + system):
        return SYSTEM_POINTER, prepend_system(system, body), {}
    return system, body, {}


def render_transcript(messages: Sequence[Message]) -> str:
    """Render a provider-neutral transcript as one user message.

    A single user message (context mode, the common case) is passed through verbatim so CLI and
    API providers see byte-identical prompts.
    """
    if len(messages) == 1 and messages[0].role is Role.user:
        return messages[0].content
    parts: list[str] = []
    for message in messages:
        if message.role is Role.user:
            parts.append(f"[User]\n{message.content}")
        elif message.role is Role.assistant:
            lines = [message.content] if message.content else []
            lines.extend(
                f"(tool call {call.name} id={call.id}: "
                f"{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)})"
                for call in message.tool_calls
            )
            parts.append("[Assistant]\n" + "\n".join(lines))
        else:
            parts.append(f"[Tool result {message.tool_call_id}]\n{message.content}")
    return "\n\n".join(parts)


def exceeds_arg_limit(text: str) -> bool:
    return len(text.encode()) > MAX_ARG_BYTES


def valid_effort(effort: str) -> bool:
    """Effort values are interpolated into argv (and, for codex, a TOML string), so they must be
    plain identifiers like ``low`` or ``xhigh``."""
    return _EFFORT_RE.match(effort) is not None


def parse_json_payload(stdout: str) -> dict[str, Any] | None:
    """The CLI's JSON result object: the whole stdout, or else its last line that is one.

    Some CLIs print warnings or progress lines before the result; the result comes last.
    """
    text = stdout.strip()
    if not text:
        return None
    whole = load_object(text)
    if whole is not None:
        return whole
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and (value := load_object(line)) is not None:
            return value
    return None


def parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Every JSON-object line of a JSONL event stream; other lines are ignored."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and (value := load_object(line)) is not None:
            events.append(value)
    return events


def as_int(value: object) -> int:
    """Token counts from CLI JSON: missing, null or malformed counts are 0.

    ``json.loads`` accepts ``NaN`` and ``Infinity``, so non-finite floats count as malformed.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    return 0


def as_float(value: object) -> float | None:
    """A reported cost from CLI JSON; missing, malformed or non-finite values are None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def served_models(model_usage: object) -> str | None:
    """Models the CLI reports it used (``modelUsage`` keys), comma-joined when several did."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    return ",".join(sorted(str(key) for key in model_usage))


def structured_text(
    text: object, structured: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """``(response text, CLI text to keep in raw)`` for a CLI result.

    ``ModelResponse.text`` must agree with ``parsed``: anything that reads the text later (cache
    inspection, the judge, re-extraction) has to see the answer that was scored. So the CLI's own
    text is used only when it *is* the structured output; otherwise the text is the structured
    output serialized and the CLI's text is returned separately for ``raw["cli_text"]``.
    """
    cli_text = text if isinstance(text, str) and text.strip() else None
    if structured is None:
        return (text if isinstance(text, str) else None), None
    if cli_text is not None and load_object(cli_text) == structured:
        return cli_text, None
    return json.dumps(structured, ensure_ascii=False), cli_text


@functools.cache
def probe_version(binary: str) -> str:
    """``<binary> --version`` (first line), cached per binary for the life of the process.

    Part of a CLI provider's endpoint identity (SPEC §11.4): upgrading the CLI changes the system
    prompt and agent scaffolding around the model, so cached responses from an older CLI must not
    be reused.
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return "not-installed"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()
    return "unknown"


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Facts about how the request was sent (e.g. a substituted system prompt), merged into the
    response's ``raw``."""


@dataclass(frozen=True)
class CredentialLink:
    """A symlink in a private CLI home pointing at one of the user's real login files."""

    real: Path
    link: Path
    sync_back: bool
    """Copy the file back to ``real`` if the CLI replaced the link (see ``sync_back``)."""


@dataclass(frozen=True)
class Isolation:
    """How one CLI call is cut off from the user's own CLI setup."""

    env: Mapping[str, str] = field(default_factory=dict[str, str])
    """Environment overrides (``HOME``, the CLI's config-dir variable, feature switches)."""
    links: tuple[CredentialLink, ...] = ()


HOME_RELATIVE_VARS = frozenset({"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"})
"""Dropped whenever a call gets a private ``HOME`` (``Isolation.env`` sets it): they would point
config lookups back at the user's real directories. A CLI that keeps the real ``HOME`` keeps
them too, since they may be how it finds its own login."""


def link_credentials(
    real_dir: Path,
    private_dir: Path,
    files: Sequence[str],
    *,
    shared: Sequence[str] = (),
) -> tuple[CredentialLink, ...]:
    """Symlink the login ``files`` (and lock ``shared`` files) of ``real_dir`` into ``private_dir``.

    Only files that exist are linked; a missing login makes the CLI fail with its own "not logged
    in" error, which is the right outcome. Lock files are shared so a token refresh in the call
    still coordinates with the user's other sessions of the same CLI.
    """
    private_dir.mkdir(parents=True, exist_ok=True)
    links: list[CredentialLink] = []
    for name, sync in [*((name, True) for name in files), *((name, False) for name in shared)]:
        real = real_dir / name
        if not real.is_file():
            continue
        link = private_dir / name
        link.symlink_to(real)
        links.append(CredentialLink(real=real, link=link, sync_back=sync))
    return tuple(links)


def sync_back(links: Sequence[CredentialLink]) -> None:
    """Propagate a refreshed login from a private CLI home to the user's real one.

    A CLI that refreshes its OAuth token either writes through the symlink (the real file is
    already current) or writes a new file and renames it over the link. In the second case the
    refreshed token, and possibly a rotated refresh token, exists only in the temporary home, and
    losing it could log the user out of the CLI. So a replaced link whose content is a JSON object
    different from the real file is copied back atomically with owner-only permissions. Failures
    are logged, never raised: the model call itself already finished.
    """
    for item in links:
        if not item.sync_back or item.link.is_symlink() or not item.link.is_file():
            continue
        try:
            content = item.link.read_bytes()
            if load_object(content) is None:
                continue
            if item.real.is_file() and item.real.read_bytes() == content:
                continue
            staging = item.real.with_name(f".{item.real.name}.identitybench-{os.getpid()}")
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(staging, item.real)
        except OSError as exc:
            logger.warning("could not sync a refreshed CLI login back to %s: %s", item.real, exc)


class CliOptions(TypedDict, total=False):
    """Keyword options every CLI provider accepts on top of its own."""

    runner: ProcessRunner | None
    """Subprocess runner; tests inject a fake. Defaults to ``run_command``."""
    version: str | None
    """CLI version to use in the endpoint identity instead of probing ``--version``."""
    max_retries: int
    retry_base_delay_s: float


class CliProvider(ABC):
    """Base for subscription-CLI providers. Subclasses build the argv and parse the output."""

    KIND: ClassVar[str]
    CAPABILITIES: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        native_structured_output=True, native_tools=False, supports_temperature=False
    )
    SUPPORTS_EFFORT: ClassVar[bool] = True
    ENV_DROP: ClassVar[frozenset[str]] = frozenset()
    ENV_DROP_PREFIXES: ClassVar[tuple[str, ...]] = ()
    ENV_KEEP: ClassVar[frozenset[str]] = frozenset()
    ENV_SET: ClassVar[Mapping[str, str]] = MappingProxyType({})
    """Variables set for every call whatever the parent environment holds (e.g. switching the
    CLI's self-updater off, so the version in the endpoint identity stays the one that runs)."""

    def __init__(
        self,
        model: str,
        *,
        effort: str | None,
        timeout_s: float,
        binary: str,
        runner: ProcessRunner | None = None,
        version: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
    ) -> None:
        self.kind: str = self.KIND
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.binary = binary
        self._runner: ProcessRunner = runner or run_command
        self._version = version
        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._setup_revision: str | None = None

    def capabilities(self) -> ProviderCapabilities:
        return self.CAPABILITIES

    def endpoint_identity(self) -> str:
        """Hash of the kind, CLI version, model, ``setup_revision()`` and the default effort.

        The default effort is the constructor's, which applies whenever a request leaves effort
        unset; a request's own effort is part of the cache key already.
        """
        if self._version is None:
            self._version = probe_version(self.binary)
        return hash_endpoint(
            self.kind, self._version, self.model, self.setup_revision(), self.effort
        )

    def setup_revision(self) -> str:
        """Short hash of the call setup the harness controls, computed once per provider.

        It is derived from the setup itself rather than a hand-bumped constant, so no change can
        be forgotten: the argv, stdin and input files ``_build`` produces for two template
        requests (an empty system prompt, no schema and no effort; a system prompt, a multi-turn
        transcript, a schema and an effort; and the same as a JSON action protocol turn), the
        environment and files ``_isolate`` sets up in a private home (login links excluded:
        whether the user is logged in is not setup), the environment rules, and the
        large-system-prompt fallback. Temporary paths, the binary's location and the model name
        are replaced by placeholders, so the revision is the same on every machine and for every
        model of the provider.

        An ``OSError`` while setting up the scratch home propagates: a machine that can't do that
        can't run a call either.
        """
        if self._setup_revision is None:
            self._setup_revision = self._compute_setup_revision()
        return self._setup_revision

    def _compute_setup_revision(self) -> str:
        transcript = (
            Message(Role.user, "<prompt>"),
            Message(
                Role.assistant,
                "<reply>",
                tool_calls=(ToolCall("<call-id>", "<tool>", {"<arg>": "<value>"}),),
            ),
            Message(Role.tool, "<result>", tool_call_id="<call-id>"),
            Message(Role.user, "<follow-up>"),
        )
        templates = (
            (ModelRequest(system="", messages=(Message(Role.user, "<prompt>"),)), None),
            (
                ModelRequest(
                    system="<system>",
                    messages=transcript,
                    output_schema={"type": "object", "properties": {}},
                ),
                "<effort>" if self.SUPPORTS_EFFORT else None,
            ),
            (
                ModelRequest(
                    system="<system>",
                    messages=transcript,
                    output_schema={"type": "object", "properties": {}},
                    schema_name=ACTION_SCHEMA_NAME,
                ),
                None,
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix=f"identitybench-{self.kind}-setup-", ignore_cleanup_errors=True
        ) as tmp:
            root = Path(tmp).resolve()
            invocations: list[dict[str, Any]] = []
            for number, (request, effort) in enumerate(templates):
                io_dir = root / f"t{number}" / "io"
                work_dir = root / f"t{number}" / "work"
                io_dir.mkdir(parents=True)
                work_dir.mkdir()
                built = self._build(request, effort, io_dir, work_dir)
                argv = [
                    "<binary>",
                    *("<model>" if arg == self.model else arg for arg in built.argv[1:]),
                ]
                invocations.append(
                    {
                        "argv": argv,
                        "stdin": built.stdin,
                        "raw": dict(built.raw),
                        "files": _files(io_dir),
                    }
                )
            home = root / "home"
            home.mkdir()
            isolation = self._isolate(home)
            setup = {
                "invocations": invocations,
                "isolation": {"env": dict(isolation.env), "files": _files(home)},
                "env": {
                    "drop": sorted(self.ENV_DROP),
                    "drop_prefixes": list(self.ENV_DROP_PREFIXES),
                    "keep": sorted(self.ENV_KEEP),
                    "set": dict(self.ENV_SET),
                    "home_relative": sorted(HOME_RELATIVE_VARS),
                },
                "large_system": [MAX_ARG_BYTES, SYSTEM_POINTER, SYSTEM_BEGIN, SYSTEM_END],
            }
            text = json.dumps(setup, sort_keys=True, ensure_ascii=False).replace(str(root), "<tmp>")
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment for the CLI process: ``base`` (default ``os.environ``) minus the
        variables this CLI must not inherit, plus ``ENV_SET``."""
        source = os.environ if base is None else base
        env = {name: value for name, value in source.items() if not self._drops(name)}
        env.update(self.ENV_SET)
        return env

    def _drops(self, name: str) -> bool:
        if name in self.ENV_KEEP:
            return False
        return name in self.ENV_DROP or name.startswith(self.ENV_DROP_PREFIXES)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._complete(request)
        ignored = self._ignored_params(request)
        if ignored:
            response.raw = {**(response.raw or {}), "ignored_params": ignored}
        return response

    def _ignored_params(self, request: ModelRequest) -> list[str]:
        """Requested settings the CLI can't apply, recorded so the manifest isn't misread.

        ``max_output_tokens`` is always listed: every request carries a value (the dataclass
        default included) and no CLI can apply one, so leaving it out for the default would
        make the record depend on whether the value happened to be spelled out.
        """
        ignored: list[str] = []
        if request.params.temperature is not None:
            ignored.append("temperature")
        ignored.append("max_output_tokens")
        if request.params.effort is not None and not self.SUPPORTS_EFFORT:
            ignored.append("effort")
        return ignored

    def _isolate(self, home: Path) -> Isolation:
        """Private-home setup for one call under ``home`` (an empty directory). Default: none,
        for CLIs whose flags already keep the user's own setup out."""
        return Isolation()

    def _after_call(self, work_dir: Path, env: Mapping[str, str]) -> None:
        """Remove per-call state the CLI left outside the call's temporary directory (``env`` is
        the child's environment). Default: nothing to remove. Must log, never raise."""
        return None

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        if request.tools:
            return self._fail("native tools are not supported by CLI providers; use context mode")
        if not request.messages:
            return self._fail("request has no messages")
        effort = (request.params.effort or self.effort) if self.SUPPORTS_EFFORT else None
        if effort is not None and not valid_effort(effort):
            return self._fail(f"invalid effort value {effort!r}")

        # Cleanup errors are ignored: a CLI can leave read-only files or a straggling writer in
        # the directory, and that must not turn a finished call into an exception.
        with tempfile.TemporaryDirectory(
            prefix=f"identitybench-{self.kind}-", ignore_cleanup_errors=True
        ) as tmp:
            # Resolved, so paths handed to the CLI match what it sees (macOS /var -> /private/var).
            root = Path(tmp).resolve()
            io_dir = root / "io"
            work_dir = root / "work"
            home_dir = root / "home"
            for directory in (io_dir, work_dir, home_dir):
                directory.mkdir()
            built = self._build(request, effort, io_dir, work_dir)
            too_long = next((arg for arg in built.argv if exceeds_arg_limit(arg)), None)
            if too_long is not None:
                return self._fail(
                    f"an argument is {len(too_long.encode())} bytes, over the "
                    f"{MAX_ARG_BYTES}-byte argv limit"
                )
            try:
                isolation = self._isolate(home_dir)
            except OSError as exc:
                return self._fail(f"could not set up a private CLI home: {exc}")
            env = self.child_env()
            if "HOME" in isolation.env:
                for name in HOME_RELATIVE_VARS:
                    env.pop(name, None)
            env.update(isolation.env)
            command = Command(
                argv=built.argv,
                cwd=str(work_dir),
                env=env,
                timeout_s=self.timeout_s,
                stdin=built.stdin,
            )
            attempts: list[ModelResponse] = []

            async def attempt() -> ModelResponse:
                for stale in self._stale_outputs(io_dir):
                    stale.unlink(missing_ok=True)
                started = time.perf_counter()
                result = await self._runner(command)
                if result.launch_error is not None:
                    response = self._fail(result.launch_error)
                elif result.timed_out:
                    response = self._fail(f"no result within the {self.timeout_s:g}s timeout")
                else:
                    response = self._parse(result, request, io_dir)
                response.latency_s = time.perf_counter() - started
                attempts.append(response)
                return response

            started = time.perf_counter()
            try:
                response = await retry_transient(
                    attempt,
                    lambda response: (
                        response.error is not None and is_transient_error(response.error)
                    ),
                    label=f"{self.kind}:{self.model}",
                    max_retries=self._max_retries,
                    base_delay_s=self._retry_base_delay_s,
                    describe=lambda response: response.error or "",
                )
            finally:
                sync_back(isolation.links)
                self._after_call(work_dir, env)
            if len(attempts) > 1:
                _merge_attempts(response, attempts, wall_s=time.perf_counter() - started)
            if built.raw:
                response.raw = {**(response.raw or {}), **built.raw}
            if request.params.effort is None and effort is not None:
                # The provider's own default applied; record it, since the request (and so the
                # cache key and manifest parameters) says nothing about effort.
                response.raw = {**(response.raw or {}), "effort": effort}
            return response

    def _stale_outputs(self, io_dir: Path) -> tuple[Path, ...]:
        """Files an earlier attempt may have written; deleted before each attempt so a retry
        never reads a previous attempt's output."""
        return ()

    @abstractmethod
    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        """The command line (and stdin) for ``request``; may write input files to ``io_dir``."""

    @abstractmethod
    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        """Turn a finished process into a ``ModelResponse`` (never raises)."""

    def _error_text(self, message: str, result: ProcessResult | None = None) -> str:
        text = f"{self.kind}: {message}"
        if result is not None:
            details = [f"exit {result.returncode}"]
            if result.stderr.strip():
                details.append(f"stderr: {tail(result.stderr, 1000)}")
            text += f" ({'; '.join(details)})"
        return redact(text)

    def _fail(self, message: str, result: ProcessResult | None = None) -> ModelResponse:
        return ModelResponse(error=self._error_text(message, result))


def _files(directory: Path) -> dict[str, str]:
    """Regular files under ``directory`` by relative path, with their text; symlinks (login
    links) are skipped."""
    return {
        str(path.relative_to(directory)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _merge_attempts(final: ModelResponse, attempts: Sequence[ModelResponse], wall_s: float) -> None:
    """Fold the failed attempts of a retried call into the final response.

    Usage and reported cost cover every attempt, because every attempt was billed and the
    runner's hard ``budget_usd`` stop must see that spend. ``latency_s`` stays the final
    attempt's own duration; the wall time including backoff sleeps goes in ``raw``.
    """
    for earlier in attempts[:-1]:
        final.usage.add(earlier.usage)
    costs = [item.reported_cost_usd for item in attempts if item.reported_cost_usd is not None]
    final.reported_cost_usd = sum(costs) if costs else None
    final.raw = {
        **(final.raw or {}),
        "attempts": len(attempts),
        "wall_s": round(wall_s, 3),
    }
