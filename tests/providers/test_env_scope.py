"""The ``.env`` export scope (``envfile.env_names_for``): only a command that calls models gets
the file's keys and endpoints into the process environment; every other command gets the
harness's own ``IDENTITYBENCH_*`` settings and nothing else, so what the harness starts for it
(and what a later ``.env`` edit adds) can't inherit a secret. The decision is by the full
command path, so ``v3 score`` is not live just because ``v3 run`` is."""

from __future__ import annotations

from pathlib import Path

import pytest

from identitybench.envfile import (
    HARNESS_PREFIX,
    LEAF_GROUPS,
    LIVE_COMMANDS,
    env_names_for,
    is_live_command,
    load_env_file,
)

FILE = (
    "LOCAL_LLM_BASE_URL=https://llm.example.invalid/v1\n"
    "LOCAL_LLM_API_KEY=placeholder-secret-for-tests\n"
    "IDENTITYBENCH_PRICING=pricing.yaml\n"
)

LIVE = [
    "run",
    "judge",
    "audit",
    ("run",),
    ("audit", "panel"),
    ("audit", "adjudicate"),
    ("v3", "run"),
    ("v4", "run"),
]
NOT_LIVE = [
    "generate",
    "validate",
    "score",
    "report",
    "compare",
    None,
    (),
    (None,),
    "v3",
    "v4",
    ("v3",),
    ("v3", None),
    ("v3", "generate"),
    ("v3", "validate"),
    ("v3", "score"),
    ("v3", "report"),
    ("v4", "generate"),
    ("v4", "validate"),
    ("v4", "score"),
    ("v4", "report"),
]


def test_the_live_commands_are_exactly_the_ones_that_create_providers() -> None:
    assert (
        frozenset({("run",), ("judge",), ("audit",), ("v3", "run"), ("v4", "run")}) == LIVE_COMMANDS
    )
    assert frozenset({"v3", "v4"}) == LEAF_GROUPS
    assert HARNESS_PREFIX == "IDENTITYBENCH_"


@pytest.mark.parametrize("command", LIVE, ids=str)
def test_a_live_command_gets_every_name(command: object, tmp_path: Path) -> None:
    assert is_live_command(command)  # type: ignore[arg-type]
    env = tmp_path / ".env"
    env.write_text(FILE, encoding="utf-8")
    target: dict[str, str] = {}
    loaded, problems = load_env_file(
        env,
        environ=target,
        wanted=env_names_for(command),  # type: ignore[arg-type]
    )
    assert problems == ()
    assert loaded == ["IDENTITYBENCH_PRICING", "LOCAL_LLM_API_KEY", "LOCAL_LLM_BASE_URL"]
    assert set(target) == set(loaded)


@pytest.mark.parametrize("command", NOT_LIVE, ids=str)
def test_any_other_command_gets_only_harness_settings(command: object, tmp_path: Path) -> None:
    assert not is_live_command(command)  # type: ignore[arg-type]
    env = tmp_path / ".env"
    env.write_text(FILE, encoding="utf-8")
    target: dict[str, str] = {}
    loaded, problems = load_env_file(
        env,
        environ=target,
        wanted=env_names_for(command),  # type: ignore[arg-type]
    )
    assert problems == ()
    assert loaded == ["IDENTITYBENCH_PRICING"]
    assert target == {"IDENTITYBENCH_PRICING": "pricing.yaml"}


def test_a_group_is_decided_by_its_leaf_not_by_the_group() -> None:
    """``v3 run`` is live and ``v3 score`` is not, even though both are under ``v3``; the group
    on its own (what the root callback sees) is not live, so it loads nothing for it."""
    for group in sorted(LEAF_GROUPS):
        assert is_live_command((group, "run"))
        assert not is_live_command((group, "score"))
        assert not is_live_command((group,))
        assert not is_live_command(group)
        assert not any(path == (group,) for path in LIVE_COMMANDS)


def test_a_path_below_a_live_group_is_live() -> None:
    assert is_live_command(("audit", "export"))
    assert is_live_command(("audit", "panel", "anything"))
    assert is_live_command(("v3", "run", "extra"))  # still live below ``v3 run``


def test_without_a_filter_every_name_is_loaded_as_before(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(FILE, encoding="utf-8")
    target: dict[str, str] = {}
    loaded, _ = load_env_file(env, environ=target)
    assert len(loaded) == 3


def test_a_withheld_name_is_not_reported_as_loaded_and_never_overrides(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(FILE, encoding="utf-8")
    target = {"IDENTITYBENCH_PRICING": "from-shell"}
    loaded, _ = load_env_file(env, environ=target, wanted=env_names_for(("v3", "score")))
    assert loaded == []
    assert target == {"IDENTITYBENCH_PRICING": "from-shell"}
