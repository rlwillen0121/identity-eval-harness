"""The committed experiment files load, say what they are meant to, and name no private
endpoints or served models."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from identitybench.cli import load_judges
from identitybench.judge.consensus import check_consensus_panel
from identitybench.providers.baselines import BASELINE_NAMES
from identitybench.render.prompt import PROMPT_VARIANTS
from identitybench.runner.config import BASELINES, ExperimentConfig, load_config
from identitybench.schemas.enums import Split, Suite
from identitybench.v3.runner import load_v3_experiment_config
from identitybench.v4.runner import load_v4_experiment_config
from identitybench.v41.runner import load_v41_experiment_config

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "experiments"
CONFIGS = (
    "smoke",
    "v1-leaderboard",
    "v1-leaderboard-openai-compatible.example",
    "v1-ablation",
    "local-model.example",
)
V3_CONFIGS = (
    "v3-dev-baselines",
    "v3-dev-pilot",
    "v3-lite",
    "v3-full",
    "v3-full-muse",
    "v3-full-openrouter",
    "v3-full-qwen",
)
V4_CONFIGS = (
    "v4-dev-baselines",
    "v4-dev-pilot",
    "v4-dev-subset6",
    "v4-lite",
)
V41_CONFIGS = (
    "v41-dev-baselines",
    "v41-dev-pilot",
    "v41-full-claude",
    "v41-full-codex",
    "v41-full-muse",
    "v41-lite-muse-wonder",
    "v41-lite-or-resume",
    "v41-lite-sub",
    "v41-lite-wave2",
    "v41-lite-wave3",
)
ALL_SUITES = list(Suite)


def config(name: str) -> ExperimentConfig:
    return load_config(EXPERIMENTS / f"{name}.yaml")


@pytest.mark.parametrize("name", CONFIGS)
def test_every_experiment_loads_without_warnings(name: str) -> None:
    loaded = config(name)
    assert loaded.name == name.removesuffix(".example")
    assert loaded.dataset == Path("datasets/identitybench-v1")
    assert loaded.warnings() == []


@pytest.mark.parametrize("name", V3_CONFIGS)
def test_every_v3_experiment_loads_cleanly(name: str) -> None:
    loaded = load_v3_experiment_config(EXPERIMENTS / f"{name}.yaml")
    assert loaded.name == name
    assert (REPO / "datasets" / loaded.suite.value / "SEAL.json").is_file()


@pytest.mark.parametrize("name", V4_CONFIGS)
def test_every_v4_experiment_loads_cleanly(name: str) -> None:
    loaded = load_v4_experiment_config(EXPERIMENTS / f"{name}.yaml")
    assert loaded.name == name
    assert (REPO / "datasets" / loaded.suite.value / "SEAL.json").is_file()


@pytest.mark.parametrize("name", V41_CONFIGS)
def test_every_v41_experiment_loads_cleanly(name: str) -> None:
    loaded = load_v41_experiment_config(EXPERIMENTS / f"{name}.yaml")
    assert loaded.name in (name, "v41-lite-sub")
    live = [m for m in loaded.models if m.provider != "baseline"]
    assert all(m.effort in ("medium", None) for m in live)
    assert all(m.timeout_s in (900, 1800) for m in live if m.provider == "agy_cli")


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
    expected = [
        *(f"{n}.yaml" for n in CONFIGS),
        *(f"{n}.yaml" for n in V3_CONFIGS),
        *(f"{n}.yaml" for n in V4_CONFIGS),
        *(f"{n}.yaml" for n in V41_CONFIGS),
        "judges.yaml",
        "judges-consensus.example.yaml",
        "panel-adjudication.example.yaml",
    ]
    assert names == sorted(expected)


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
        ("claude_cli", "claude-fable-5-1"),
        ("codex_cli", "gpt-6-astra"),
        ("codex_cli", "gpt-6.1-sol"),
        ("codex_cli", "gpt-6-luna"),
        ("agy_cli", "gemini-4-argon"),
    ]
    codex = [m for m in board.models if m.provider == "codex_cli"]
    assert all(m.to_spec(board.params).effort == "medium" for m in codex)
    assert all(m.concurrency == 3 for m in codex)
    agy = [m for m in board.models if m.provider == "agy_cli"]
    assert [(m.to_spec(board.params).effort, m.concurrency) for m in agy] == [("medium", 10)]
    others = [m for m in board.models if m.provider != "agy_cli"]
    assert all(m.concurrency is not None and 2 <= m.concurrency <= 4 for m in others)
    assert set(board.baselines) == BASELINES
    assert (board.repeats.k, board.repeats.subset) == (3, 100)
    assert board.budget_usd is None  # codex_cli reports no cost, so a budget can't be enforced


def test_leaderboard_header_says_baselines_are_repeated_too() -> None:
    """The runner repeats every config on the repeat subset, baselines included."""
    header = (EXPERIMENTS / "v1-leaderboard.yaml").read_text(encoding="utf-8").split("\nname:")[0]
    assert "baselines included" in " ".join(header.replace("#", " ").split())
    assert "stability" in header


def test_openai_compatible_example_matches_the_leaderboard() -> None:
    """Same split, suites, prompt and parameters as the main leaderboard, so its rows merge
    into one report; only the main run carries baselines."""
    board = config("v1-leaderboard")
    extra = config("v1-leaderboard-openai-compatible.example")
    assert (extra.split, extra.tasks, extra.prompts, extra.mode) == (
        board.split,
        board.tasks,
        board.prompts,
        board.mode,
    )
    assert extra.params == board.params
    assert extra.baselines == []


OPENROUTER_ROWS = {
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
    "mistral-large-4": "mistralai/mistral-large-4-0",
    "mimo-v2.6-flash": "xiaomi/mimo-v2.6-flash",
    "gemini-3.5-flash-lite": "google/gemini-3.5-flash-lite",
    "deepseek-v4.1-flash": "deepseek/deepseek-v4.1-flash",
    "hunyuan-3": "tencent/hy3",
    "gemini-3.8-flash": "google/gemini-3.8-flash",
    "mercury-2.5": "inception/mercury-2.5",
    "solar-pro-4": "upstage/solar-pro4",
    "mimo-v2.6-pro": "xiaomi/mimo-v2.6-pro",
    "qwen3.8-flash-api": "qwen/qwen3.8-flash",
    "muse-glimmer-30b": "meta/muse-glimmer-30b",
    "minimax-m3": "minimax/minimax-m3",
    "granite-4.2-8b": "ibm-granite/granite-4.2-8b",
    "longcat-2.0": "meituan/longcat-2.0",
    "inkling-small": "thinkingmachines/inkling-small",
    "glm-5.3": "z-ai/glm-5.3",
}
"""The published v1 leaderboard's OpenRouter rows: label -> OpenRouter model ID."""
REQUIRE_PARAMETERS = {"deepseek-v4.1-flash", "hunyuan-3", "gemini-3.8-flash", "mercury-2.5"}
REQUIRE_PARAMETERS |= {"solar-pro-4", "mimo-v2.6-pro", "qwen3.8-flash-api", "muse-glimmer-30b"}
REQUIRE_PARAMETERS |= {"minimax-m3", "granite-4.2-8b", "glm-5.3"}


def test_openai_compatible_example_holds_the_published_rows_and_a_placeholder() -> None:
    """Every published OpenRouter row as it ran (model ID, routing, output cap), so the committed
    files reproduce every leaderboard row but the three that ran outside OpenRouter, named in the
    header; then a self-hosted placeholder."""
    extra = config("v1-leaderboard-openai-compatible.example")
    *routed, local = extra.models
    assert {m.label: m.model for m in routed} == OPENROUTER_ROWS
    assert [m.provider for m in extra.models] == ["openai_compatible"] * len(extra.models)
    for entry in routed:
        assert (entry.base_url_env, entry.api_key_env) == (
            "OPENROUTER_BASE_URL",
            "OPENROUTER_API_KEY",
        )
        assert entry.resolved_cost_mode == "reported" and entry.reports_cost
        assert entry.max_output_tokens == 65536
        routing = entry.extra.get("provider_routing")
        if entry.label == "deepseek-v4-pro":
            assert routing == {
                "order": ["Baidu", "Alibaba"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
        elif entry.label in REQUIRE_PARAMETERS:
            assert routing == {"require_parameters": True}
        else:
            assert routing is None
    assert (local.base_url_env, local.api_key_env) == ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY")
    assert re.fullmatch(r"<[a-z-]+>", local.model)
    assert local.cache_namespace
    assert local.resolved_cost_mode == "unmetered"
    # OpenRouter reports every call's cost, so the budget is enforceable for those rows.
    assert extra.budget_usd is not None and extra.budget_usd > 0
    text = (EXPERIMENTS / "v1-leaderboard-openai-compatible.example.yaml").read_text("utf-8")
    assert "experiments/local/" in text
    header = " ".join(text.replace("#", " ").split())
    assert "GLM-5.3 Flash through a private" in header
    assert "qwen3.8-27b-exl3-4bit" in header and "qwen3.8-flash-next" in header


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


def test_consensus_judges_example_is_three_vendors() -> None:
    judges = load_judges(EXPERIMENTS / "judges-consensus.example.yaml")
    assert [(j.provider.kind, j.provider.model) for j in judges] == [
        ("claude_cli", "claude-opus-5-5"),
        ("codex_cli", "gpt-6-astra"),
        ("openai_compatible", "grok-4.7"),
    ]
    check_consensus_panel({j.name: j.family for j in judges})


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
