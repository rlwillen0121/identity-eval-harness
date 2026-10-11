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
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["EnvFile", "load_env_file", "parse_env"]

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
) -> tuple[list[str], tuple[str, ...]]:
    """Set the variables of the ``.env`` file at ``path`` that ``environ`` doesn't have yet.

    ``environ`` defaults to ``os.environ``. Returns the names that were set (sorted) and the
    file's problems; a missing file sets nothing and is not a problem, since ``.env`` is
    optional (tests, generation, baselines and replay runs need no endpoint). A file that
    can't be read as UTF-8 sets nothing and is one problem, which names the error class only
    (a decode error's message would quote the offending bytes). Values are never returned, so
    nothing downstream can print one.
    """
    target = os.environ if environ is None else environ
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
        if key not in target:
            target[key] = value
            loaded.append(key)
    return sorted(loaded), parsed.problems
