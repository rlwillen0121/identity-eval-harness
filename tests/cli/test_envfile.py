"""The ``.env`` parser and loader: format subset, no override, and no values in problems."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from identitybench.envfile import load_env_file, parse_env


def test_parses_comments_blank_lines_export_and_quotes() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "PLAIN=value",
            "  SPACED  =  padded value  ",
            "export EXPORTED=yes",
            "SINGLE='literal $HOME \\n # not a comment'",
            'DOUBLE="line\\nbreak \\"quoted\\" back\\\\slash \\$x"',
            "INLINE=abc # trailing comment",
            "HASH_IN_VALUE=abc#def",
            "QUOTED_THEN_COMMENT='kept' # comment",
            "EQUALS=a=b=c",
        ]
    )
    parsed = parse_env(text)
    assert parsed.problems == ()
    assert parsed.values == {
        "PLAIN": "value",
        "SPACED": "padded value",
        "EXPORTED": "yes",
        "SINGLE": "literal $HOME \\n # not a comment",
        "DOUBLE": 'line\nbreak "quoted" back\\slash $x',
        "INLINE": "abc",
        "HASH_IN_VALUE": "abc#def",
        "QUOTED_THEN_COMMENT": "kept",
        "EQUALS": "a=b=c",
    }


def test_empty_values_are_unfilled_but_quoted_empty_is_kept() -> None:
    parsed = parse_env('UNSET=\nCOMMENT_ONLY= # nothing yet\nHASH=#nothing\nEMPTY=""\n')
    assert parsed.values == {"EMPTY": ""}
    assert parsed.problems == ()


def test_later_assignments_win_and_an_empty_one_unsets() -> None:
    assert parse_env("A=1\nA=2\n").values == {"A": "2"}
    assert parse_env("A=1\nA=\n").values == {}


def test_problems_name_lines_and_keys_never_values() -> None:
    secret = "placeholder-secret-for-tests"
    text = "\n".join(
        [
            "GOOD=fine",
            f"not a pair {secret}",
            f"1BAD={secret}",
            f"OPEN='{secret}",
            f'DQ="{secret}',
            f"TRAIL='{secret}' extra",
        ]
    )
    parsed = parse_env(text)
    assert parsed.values == {"GOOD": "fine"}
    assert len(parsed.problems) == 5
    assert parsed.problems[0].startswith("line 2:")
    assert "OPEN" in parsed.problems[2] and "unterminated" in parsed.problems[2]
    assert "text after the closing quote" in parsed.problems[4]
    assert all(problem.endswith("; line skipped") for problem in parsed.problems)
    assert all(secret not in problem for problem in parsed.problems)


def test_load_sets_only_missing_variables(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NEW_VAR=from-file\nEXISTING=from-file\n", encoding="utf-8")
    environ = {"EXISTING": "from-environment"}
    loaded, problems = load_env_file(env, environ=environ)
    assert loaded == ["NEW_VAR"]
    assert problems == ()
    assert environ == {"EXISTING": "from-environment", "NEW_VAR": "from-file"}


def test_load_returns_names_not_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("B_KEY=secret-b\nA_KEY=secret-a\nbroken line\n", encoding="utf-8")
    loaded, problems = load_env_file(env, environ={})
    assert loaded == ["A_KEY", "B_KEY"]
    assert problems == ("line 3: expected KEY=VALUE; line skipped",)


def test_missing_file_is_not_a_problem(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    assert load_env_file(tmp_path / ".env", environ=environ) == ([], ())
    assert environ == {}


def test_load_defaults_to_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_environ = {"ALREADY": "set"}
    monkeypatch.setattr(os, "environ", fake_environ)
    env = tmp_path / ".env"
    env.write_text("ALREADY=other\nFRESH=1\n", encoding="utf-8")
    assert load_env_file(env)[0] == ["FRESH"]
    assert fake_environ == {"ALREADY": "set", "FRESH": "1"}


def test_utf8_bom_is_ignored(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfFIRST=1\n")
    assert load_env_file(env, environ={})[0] == ["FIRST"]


def test_a_file_that_is_not_utf8_is_skipped_whole(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"GOOD=1\nSECRET=caf\xe9-placeholder\n")
    environ: dict[str, str] = {}
    loaded, problems = load_env_file(env, environ=environ)
    assert (loaded, environ) == ([], {})
    assert problems == ("not valid UTF-8 text; file skipped",)


def test_an_unreadable_file_is_skipped_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")

    def denied(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", denied)
    assert load_env_file(env, environ={}) == ([], ("unreadable (PermissionError); file skipped",))
