"""Minimal .env loading with command-specific export scope.

Existing environment values always win. Diagnostics contain names and line numbers,
never values. Only ``v41 run`` receives model keys and endpoints; every offline command
receives only the harness's own ``IDENTITYBENCH_*`` settings. Parsing accepts optional
``export``, quoted values, common double-quote escapes and inline comments, without
variable expansion. Unquoted empty values are skipped; quoted empty strings are kept.
"""

from __future__ import annotations

import contextlib
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

LIVE_COMMANDS: frozenset[tuple[str, ...]] = frozenset({("v41", "run")})
"""Only live v4.1 execution receives model endpoint settings."""
LEAF_GROUPS = frozenset({"v41"})
"""The group callback decides whether its leaf needs model settings."""
HARNESS_PREFIX = "IDENTITYBENCH_"
"""The harness's own settings (``IDENTITYBENCH_PRICING``...): loaded for every command."""

CommandPath = str | Sequence[str | None] | None
"""A command as the root callback or a group callback sees it: the top-level name, or the path
from the top (``("v41", "score")``); ``None`` elements (no subcommand) make nothing live."""


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


def _load_dotenv_if_present(repo_root: Path) -> None:
    """Load `KEY=VALUE` pairs from `.env` into `os.environ` via `setdefault` without logging."""
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    with contextlib.suppress(OSError):
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key:
                os.environ.setdefault(key, val)
