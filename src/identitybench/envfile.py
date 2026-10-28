"""Minimal ``.env`` loading for the command line (SPEC §11.1, §11.4, §17.3).

Base URLs and keys live only in ``.env`` (gitignored); committed configs name the *variables*.
The CLI loads the repo's ``.env`` at startup so ``identitybench run`` finds ``LOCAL_LLM_BASE_URL``
and friends without the user exporting them by hand.

Two rules matter more than parser features:

- **Never override.** A variable already set in the environment wins over the file, so a
  one-off ``LOCAL_LLM_BASE_URL=... identitybench run ...`` or a CI secret is never silently
  replaced by a stale local value.
- **Never print values.** Problems are reported by line number and variable name only, and the
  loader returns names, not values, so no caller can echo a secret by accident.
- **Never block a command.** ``.env`` is optional, so a line that can't be parsed is skipped and
  a file that can't be read (not UTF-8, no permission) is skipped whole; each is reported as a
  problem for the caller to warn about, and every command still runs.

The format is the common subset every dotenv tool accepts: ``KEY=VALUE`` lines, blank lines and
``#`` comments, an optional ``export`` prefix, single quotes (literal) and double quotes (with
``\\n``, ``\\t``, ``\\r``, ``\\"``, ``\\$`` and ``\\\\`` escapes), and inline comments: a ``#``
that starts an unquoted value or follows whitespace in it, or a ``#`` after a closing quote.
Variables are not expanded (``$OTHER`` stays literal). Multi-line values are not supported, so
an unterminated quote is reported, as is any other line that can't be parsed, rather than
guessed at.

An empty value (``KEY=``) means "not filled in", as in ``.env.example`` ("fill in what you
have"), so it is skipped rather than set: an empty ``ANTHROPIC_API_KEY`` or
``IDENTITYBENCH_PRICING`` in the environment would read as configured and fail later, far from
the cause. Write ``KEY=""`` to set an empty string on purpose.

**Only commands that call models get the secrets.** The file's values are exported into the
process environment, because that is where the API providers read their key and base-URL
variables (``openai_compat``, ``anthropic_api``) and the simplest place for them to keep doing
so. But a process environment is inherited by every child process, so the export is limited to
what a command can need: ``env_names_for`` lets a command that makes model calls (``run``,
``judge``, the ``audit`` commands, ``v3 run`` and ``v4 run``) load every name, and every other
command (``generate``, ``validate``, ``score``, ``report``, ``compare``, and the same under
``v3`` and ``v4``) only the harness's own ``IDENTITYBENCH_*`` settings, never a key or an
endpoint. The decision is by the full command path (``LIVE_COMMANDS``): the root callback
decides for a top-level command, and for the ``v3`` and ``v4`` groups (``LEAF_GROUPS``) leaves
it to the group's own callback, which sees the leaf. The CLI providers don't rely on this (their
child environment is an allow-list, ``providers._cli.COMMON_ENV_KEEP``); it keeps the harness's
own environment clean for everything else it starts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LEAF_GROUPS",
    "LIVE_COMMANDS",
    "EnvFile",
    "env_names_for",
    "is_live_command",
    "load_env_file",
    "parse_env",
]

LIVE_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {("run",), ("judge",), ("audit",), ("v3", "run"), ("v4", "run"), ("v41", "run")}
)
"""Command paths that call model endpoints and so get the whole ``.env``: a top-level command,
a group every subcommand of which may (``audit``: its panel commands do), or the one live
subcommand of a group whose others don't (``v3 run``, ``v4 run``, ``v41 run``). A path below one
of these (``audit panel``) is live too; a group on its own (``v3``) is not: its leaf decides."""
LEAF_GROUPS = frozenset({"v3", "v4", "v41"})
"""Top-level groups whose subcommands differ in whether they call models: the root callback
loads nothing for them and the group's own callback loads for its leaf."""
HARNESS_PREFIX = "IDENTITYBENCH_"
"""The harness's own settings (``IDENTITYBENCH_PRICING``...): loaded for every command."""

CommandPath = str | Sequence[str | None] | None
"""A command as the root callback or a group callback sees it: the top-level name, or the path
from the top (``("v3", "score")``); ``None`` elements (no subcommand) make nothing live."""


def _as_path(command: CommandPath) -> tuple[str | None, ...]:
    if isinstance(command, str):
        return (command,)
    return tuple(command) if command is not None else ()


def is_live_command(command: CommandPath) -> bool:
    """Whether ``command`` (or a command it is under) is in ``LIVE_COMMANDS``."""
    path = _as_path(command)
    return any(path[:n] in LIVE_COMMANDS for n in range(1, len(path) + 1))


def env_names_for(command: CommandPath) -> Callable[[str], bool]:
    """Which ``.env`` names ``command`` may export: every name for a live command
    (``is_live_command``), only ``HARNESS_PREFIX`` names otherwise."""
    if is_live_command(command):
        return lambda name: True
    return lambda name: name.startswith(HARNESS_PREFIX)


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPORT = re.compile(r"export\s+")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}


@dataclass(frozen=True)
class EnvFile:
    """A parsed ``.env`` file: values by name, and the lines that couldn't be parsed.

    ``problems`` hold line numbers and names only (never values), so they are safe to print,
    and each says what was skipped.
    """

    values: dict[str, str]
    problems: tuple[str, ...]


def _double_quoted(body: str) -> tuple[str, str] | None:
    """(value, rest of the line after the closing quote), or None when the quote never closes.

    ``body`` starts just after the opening quote."""
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if char == '"':
            return "".join(out), body[i + 1 :]
        out.append(char)
        i += 1
    return None


def _value(raw: str) -> tuple[str | None, str | None]:
    """(value, problem) for the text after ``=``.

    The value is None when the line leaves it unfilled (nothing but whitespace or a comment
    after ``=``), and when there is a problem."""
    text = raw.strip()
    if text[:1] in ("'", '"'):
        if text[0] == "'":
            end = text.find("'", 1)
            parsed = None if end < 0 else (text[1:end], text[end + 1 :])
        else:
            parsed = _double_quoted(text[1:])
        if parsed is None:
            return None, "unterminated quote (multi-line values are not supported)"
        value, rest = parsed
        rest = rest.strip()
        if rest and not rest.startswith("#"):
            return None, "text after the closing quote"
        return value, None
    comment = re.search(r"(?:^|\s)#", text)
    if comment is not None:
        text = text[: comment.start()]
    return text.strip() or None, None


def parse_env(text: str) -> EnvFile:
    """Parse ``.env`` text. Later assignments of a name win, as in a shell (an empty one
    included: it unsets the name again)."""
    values: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        export = _EXPORT.match(stripped)
        if export is not None:
            stripped = stripped[export.end() :]
        key, sep, raw = stripped.partition("=")
        key = key.strip()
        if not sep:
            problems.append(f"line {number}: expected KEY=VALUE; line skipped")
            continue
        if not _KEY.fullmatch(key):
            problems.append(f"line {number}: invalid variable name; line skipped")
            continue
        value, problem = _value(raw)
        if problem is not None:
            problems.append(f"line {number} ({key}): {problem}; line skipped")
        elif value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return EnvFile(values=values, problems=tuple(problems))


def load_env_file(
    path: Path | str,
    *,
    environ: MutableMapping[str, str] | None = None,
    wanted: Callable[[str], bool] | None = None,
) -> tuple[list[str], tuple[str, ...]]:
    """Set the variables of the ``.env`` file at ``path`` that ``environ`` doesn't have yet.

    ``environ`` defaults to ``os.environ``. ``wanted`` (default: every name) says which names
    to export at all (``env_names_for``); the others are left out as if the file didn't hold
    them. Returns the names that were set (sorted) and the file's problems; a missing file sets
    nothing and is not a problem, since ``.env`` is optional (tests, generation, baselines and
    replay runs need no endpoint). A file that can't be read as UTF-8 sets nothing and is one
    problem, which names the error class only (a decode error's message would quote the
    offending bytes). Values are never returned, so nothing downstream can print one.
    """
    target = os.environ if environ is None else environ
    keep = wanted if wanted is not None else (lambda name: True)
    file = Path(path)
    if not file.is_file():
        return [], ()
    try:
        text = file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return [], ("not valid UTF-8 text; file skipped",)
    except OSError as exc:
        return [], (f"unreadable ({type(exc).__name__}); file skipped",)
    parsed = parse_env(text)
    loaded: list[str] = []
    for key, value in parsed.values.items():
        if key not in target and keep(key):
            target[key] = value
            loaded.append(key)
    return sorted(loaded), parsed.problems
