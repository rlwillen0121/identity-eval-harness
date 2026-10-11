"""The committed experiment files load, say what they are meant to, and name no private
endpoints or served models."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from identitybench.cli import load_judges
from identitybench.providers.baselines import BASELINE_NAMES
from identitybench.render.prompt import PROMPT_VARIANTS
from identitybench.runner.config import BASELINES, ExperimentConfig, load_config
from identitybench.schemas.enums import Split, Suite

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "experiments"
CONFIGS = ("smoke", "v1-leaderboard", "v1-ablation", "local-model.example")
ALL_SUITES = list(Suite)


def config(name: str) -> ExperimentConfig:
    return load_config(EXPERIMENTS / f"{name}.yaml")


@pytest.mark.parametrize("name", CONFIGS)
def test_every_experiment_loads_without_warnings(name: str) -> None:
    loaded = config(name)
    assert loaded.name == name.removesuffix(".example")
    assert loaded.dataset == Path("datasets/identitybench-v1")
    assert loaded.warnings() == []


@pytest.mark.parametrize("name", CONFIGS)
def test_every_prompt_and_baseline_named_exists(name: str) -> None:
    """``load_config`` checks the shape of names, not that a prompt or baseline exists, so a
    typo would only surface when the run reaches it."""
    loaded = config(name)
    assert set(loaded.prompts) <= set(PROMPT_VARIANTS)
    assert all((REPO / "prompts" / f"{prompt}.md").is_file() for prompt in loaded.prompts)
    assert set(loaded.baselines) <= set(BASELINE_NAMES)


@pytest.mark.parametrize("name", CONFIGS)
def test_config_ids_in_usage_comments_exist(name: str) -> None:
    text = (EXPERIMENTS / f"{name}.yaml").read_text(encoding="utf-8")
    named = re.findall(r"runs/<run_id>:(\S+)", text)
    assert set(named) <= set(config(name).config_ids())


def test_the_committed_files_are_exactly_these() -> None:
    names = sorted(p.name for p in EXPERIMENTS.glob("*.yaml"))
    assert names == sorted([*(f"{n}.yaml" for n in CONFIGS), "judges.yaml"])


def test_smoke_runs_every_dev_suite_with_baselines_only() -> None:
    smoke = config("smoke")
    assert smoke.split is Split.dev
    assert smoke.tasks == ALL_SUITES
    assert smoke.models == []
    assert set(smoke.baselines) == BASELINES
    assert smoke.sample == 0.2 and isinstance(smoke.sample, float)


def test_leaderboard_matrix() -> None:
    board = config("v1-leaderboard")
    assert board.split is Split.test
    assert board.tasks == ALL_SUITES
    assert board.prompts == ["baseline"]
    assert [(m.provider, m.model) for m in board.models] == [
        ("claude_cli", "claude-opus-5-5"),
        ("claude_cli", "claude-sonnet-5-5"),
        ("claude_cli", "claude-haiku-5-5"),
        ("codex_cli", "gpt-6-astra"),
        ("grok_cli", "grok-4.7"),
    ]
    codex = next(m for m in board.models if m.provider == "codex_cli")
    assert codex.to_spec(board.params).effort == "medium"
    assert all(m.concurrency is not None and 2 <= m.concurrency <= 4 for m in board.models)
    assert set(board.baselines) == BASELINES
    assert (board.repeats.k, board.repeats.subset) == (3, 100)
    assert board.budget_usd is None  # codex_cli reports no cost, so a budget can't be enforced


def test_ablation_crosses_every_prompt_with_two_models() -> None:
    ablation = config("v1-ablation")
    assert ablation.prompts == ["baseline", "checklist", "conservative", "fewshot-3"]
    assert [m.model for m in ablation.models] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert len(ablation.config_ids()) == 8


def test_local_model_example_uses_env_vars_and_placeholders() -> None:
    local = config("local-model.example")
    (entry,) = local.models
    assert entry.provider == "anthropic_compatible"
    assert (entry.base_url_env, entry.api_key_env) == ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY")
    assert re.fullmatch(r"<[a-z-]+>", entry.model)
    assert entry.cache_namespace
    assert entry.resolved_cost_mode == "unmetered"
    text = (EXPERIMENTS / "local-model.example.yaml").read_text(encoding="utf-8")
    assert "experiments/local/" in text


def test_judges_file_is_two_families() -> None:
    judges = load_judges(EXPERIMENTS / "judges.yaml")
    assert [(j.provider.kind, j.provider.model) for j in judges] == [
        ("claude_cli", "claude-sonnet-5-5"),
        ("codex_cli", "gpt-6-astra"),
    ]
    assert len({j.family for j in judges}) == 2


@pytest.mark.parametrize("path", sorted(EXPERIMENTS.glob("*.yaml")), ids=lambda p: p.name)
def test_no_endpoints_or_secrets_in_committed_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "://" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "localhost" not in text.lower()
    assert not re.search(r"\b(sk|key)-[A-Za-z0-9]{8,}", text)


def test_local_configs_are_gitignored() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "experiments/local/" in ignored
