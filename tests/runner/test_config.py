from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from identitybench.providers.base import GenerationParams, Message, ModelRequest, Role
from identitybench.providers.factory import ModelSpec
from identitybench.providers.replay import CACHE_KEY_PARAM
from identitybench.runner.cache import key_for_request
from identitybench.runner.config import (
    _NO_EFFORT_KINDS,  # pyright: ignore[reportPrivateUsage]
    BASELINES,
    PROVIDER_KINDS,
    RUNNER_EXTRA_KEYS,
    TOOL_PROTOCOLS,
    ConfigError,
    ExperimentConfig,
    ModelEntry,
    Params,
    load_config,
    make_config_id,
)
from identitybench.schemas.enums import Split, Suite
from tests.runner._spec import spec_example


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "exp.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _base(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "t",
        "dataset": "datasets/x",
        "tasks": ["requests"],
        "models": [{"provider": "anthropic", "model": "claude-opus-5-5"}],
    }
    data.update(overrides)
    return data


def test_spec_example_parses(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, spec_example()))
    assert cfg.name == "v1-leaderboard"
    assert cfg.split is Split.test
    assert cfg.tasks == [Suite.requests]
    assert cfg.mode == "context"
    assert cfg.prompts == ["baseline", "checklist"]
    assert [m.label for m in cfg.models] == [
        "anthropic:claude-opus-5-5",
        "anthropic:claude-haiku-5-5",
        "openai_compatible:<openai-model-id>",
        "openai_compatible:<served-model-name>",
    ]
    assert cfg.models[3].base_url_env == "LOCAL_LLM_BASE_URL"
    assert cfg.models[3].cache_namespace == "deploy-1"
    assert cfg.params.temperature == 0
    assert cfg.params.max_output_tokens == 2048
    assert cfg.repeats.k == 3 and cfg.repeats.subset == 100
    assert cfg.concurrency == 8
    assert cfg.budget_usd == 50
    assert cfg.loss_matrix == "default"
    assert len(cfg.baselines) == 6
    assert cfg.sample is None and cfg.seed == 0
    assert cfg.repair is False


def test_resolved_dict_is_json_safe_and_complete(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, spec_example()))
    resolved = cfg.resolved_dict()
    assert json.loads(json.dumps(resolved)) == resolved
    assert resolved["dataset"] == "datasets/identitybench-v1"
    assert resolved["repair"] is False
    assert resolved["tasks"] == ["requests"]
    assert resolved["split"] == "test"
    opus, _, openai, local = resolved["models"]
    assert opus["label"] == "anthropic:claude-opus-5-5"
    assert opus["extra"] == {}
    # Each model shows what it ran with, not the unfilled None of its entry.
    assert (opus["temperature"], opus["max_output_tokens"], opus["effort"]) == (0.0, 2048, None)
    assert (opus["cost_mode"], openai["cost_mode"], local["cost_mode"]) == (
        "metered",
        "metered",
        "unmetered",
    )
    # Round trip: the resolved dict is a valid config with the same per-model behavior.
    again = ExperimentConfig.model_validate(resolved)
    assert again.config_ids() == cfg.config_ids()
    for a, b in zip(again.models, cfg.models, strict=True):
        assert a.to_spec(again.params) == b.to_spec(cfg.params)
        assert a.resolved_cost_mode == b.resolved_cost_mode
    assert again.resolved_dict() == resolved


def test_resolved_dict_round_trips_an_opt_out() -> None:
    cfg = ExperimentConfig.model_validate(
        _base(
            params={"temperature": 0, "effort": "high"},
            models=[{"provider": "openai_compatible", "model": "o5", "temperature": None}],
        )
    )
    resolved = cfg.resolved_dict()
    assert resolved["models"][0]["temperature"] is None
    assert resolved["models"][0]["effort"] == "high"
    again = ExperimentConfig.model_validate(resolved)
    assert again.models[0].generation_params(again.params).temperature is None


@pytest.mark.parametrize(
    ("alias", "suite"),
    [
        ("access_request", Suite.requests),
        ("requests", Suite.requests),
        ("access_review", Suite.reviews),
        ("reviews", Suite.reviews),
        ("jml", Suite.jml),
        ("twins", Suite.twins),
        ("adversarial", Suite.adversarial),
    ],
)
def test_task_aliases_normalize(alias: str, suite: Suite) -> None:
    assert ExperimentConfig.model_validate(_base(tasks=[alias])).tasks == [suite]


def test_tasks_dedupe_and_reject_unknown() -> None:
    cfg = ExperimentConfig.model_validate(_base(tasks=["access_request", "requests", "jml"]))
    assert cfg.tasks == [Suite.requests, Suite.jml]
    with pytest.raises(ValidationError, match="unknown suite"):
        ExperimentConfig.model_validate(_base(tasks=["onboarding"]))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(tasks=[]))


def test_all_model_entry_fields() -> None:
    entry = ModelEntry.model_validate(
        {
            "provider": "openai_compatible",
            "model": "qwen3-32b",
            "label": "qwen-local",
            "base_url_env": "AI_SERVER_BASE_URL",
            "api_key_env": "AI_SERVER_API_KEY",
            "cache_namespace": "deploy-7",
            "effort": "high",
            "temperature": 0.2,
            "max_output_tokens": 1024,
            "timeout_s": 120,
            "concurrency": 2,
            "cost_mode": "unmetered",
            "extra": {"max_retries": 5, "reasoning_effort_param": True},
        }
    )
    assert entry.label == "qwen-local"
    assert entry.resolved_cost_mode == "unmetered"
    params = entry.generation_params(Params(temperature=0, max_output_tokens=2048))
    assert params.temperature == 0.2
    assert params.max_output_tokens == 1024
    assert params.effort == "high"
    # Constructor options configure the provider; they never travel in request params.
    assert params.extra == {}
    spec = entry.to_spec(Params())
    assert isinstance(spec, ModelSpec)
    assert spec.extra == {"max_retries": 5, "reasoning_effort_param": True}
    assert (spec.timeout_s, spec.concurrency, spec.cache_namespace) == (120, 2, "deploy-7")
    assert spec.display_label == "qwen-local"


def test_constructor_options_do_not_change_cache_keys() -> None:
    def key(entry: Mapping[str, object]) -> str:
        params = ModelEntry.model_validate(entry).generation_params(Params(temperature=0))
        request = ModelRequest(
            system="s", messages=(Message(role=Role.user, content="hi"),), params=params
        )
        return key_for_request("claude_cli", "claude-opus-5-5", "e", request, 0)

    base = {"provider": "claude_cli", "model": "claude-opus-5-5"}
    assert key({**base, "extra": {"binary": "/usr/local/bin/claude"}}) == key(
        {**base, "extra": {"binary": "/opt/homebrew/bin/claude"}}
    )
    assert key(base) == key({**base, "extra": {"binary": "claude"}})


def test_replay_key_injection_does_not_change_key() -> None:
    request = ModelRequest(system="s", messages=(Message(role=Role.user, content="hi"),))
    before = key_for_request("replay", "m", "e", request, 0)
    injected = ModelRequest(
        system="s",
        messages=request.messages,
        params=GenerationParams(temperature=None, extra={CACHE_KEY_PARAM: before}),
    )
    assert key_for_request("replay", "m", "e", injected, 0) == before


@pytest.mark.parametrize(
    "entry",
    [
        {"provider": "claude_cli", "model": "m", "base_url_env": "X_BASE_URL"},
        {"provider": "claude_cli", "model": "m", "cache_namespace": "d1"},
        {"provider": "anthropic", "model": "m", "base_url_env": "X_BASE_URL"},
        {"provider": "anthropic_compatible", "model": "m"},
        {"provider": "replay", "model": "m"},
        {"provider": "openai_compatible", "model": "m", "extra": {"top_p": 0.9}},
        {"provider": "cursor_cli", "model": "m", "effort": "high"},
    ],
)
def test_entries_the_factory_rejects_fail_at_load(entry: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(entry)


def test_experiment_effort_default_skips_kinds_without_effort() -> None:
    cfg = ExperimentConfig.model_validate(
        _base(
            params={"effort": "high"},
            models=[
                {"provider": "cursor_cli", "model": "composer"},
                {"provider": "grok_cli", "model": "grok-4"},
            ],
        )
    )
    cursor, grok = (m.generation_params(cfg.params) for m in cfg.models)
    assert cursor.effort is None
    assert grok.effort == "high"


@pytest.mark.parametrize("kind", sorted(PROVIDER_KINDS))
def test_no_effort_kinds_match_factory(kind: str) -> None:
    """``_NO_EFFORT_KINDS`` mirrors the factory's rule; this fails if the two drift apart."""
    extra = {"path": "r.jsonl"} if kind == "replay" else {}
    base_url = {"base_url_env": "X_BASE_URL"} if kind == "anthropic_compatible" else {}
    try:
        ModelSpec.model_validate(
            {"provider": kind, "model": "m", "effort": "high", "extra": extra, **base_url}
        )
        rejects_effort = False
    except ValidationError:
        rejects_effort = True
    assert rejects_effort == (kind in _NO_EFFORT_KINDS)


def test_generation_params_fall_back_to_experiment_defaults() -> None:
    entry = ModelEntry.model_validate({"provider": "anthropic", "model": "claude-opus-5-5"})
    params = entry.generation_params(Params(temperature=0, max_output_tokens=2048, effort="low"))
    assert (params.temperature, params.max_output_tokens, params.effort) == (0, 2048, "low")


def test_explicit_null_opts_out_of_experiment_defaults() -> None:
    """A model that rejects temperature (OpenAI reasoning models) can opt out of a global 0."""
    defaults = Params(temperature=0, max_output_tokens=2048, effort="high")
    opted_out = ModelEntry.model_validate(
        {"provider": "openai_compatible", "model": "o5", "temperature": None, "effort": None}
    )
    params = opted_out.generation_params(defaults)
    assert (params.temperature, params.effort, params.max_output_tokens) == (None, None, 2048)
    inherited = ModelEntry.model_validate({"provider": "openai_compatible", "model": "o5"})
    params = inherited.generation_params(defaults)
    assert (params.temperature, params.effort) == (0.0, "high")


def test_spec_example_openai_model_can_drop_temperature(tmp_path: Path) -> None:
    text = spec_example().replace(
        '    model: "<openai-model-id>"\n',
        '    model: "<openai-model-id>"\n    temperature: null\n',
    )
    cfg = load_config(_write(tmp_path, text))
    temps = [m.generation_params(cfg.params).temperature for m in cfg.models]
    assert temps == [0.0, 0.0, None, 0.0]


def test_effort_not_recorded_when_server_ignores_it() -> None:
    qwen = {
        "provider": "openai_compatible",
        "model": "qwen3-32b",
        "cost_mode": "unmetered",
        "extra": {"reasoning_effort_param": False},
    }
    entry = ModelEntry.model_validate(qwen)
    assert entry.generation_params(Params(effort="high")).effort is None
    with pytest.raises(ValidationError, match="reasoning_effort_param"):
        ModelEntry.model_validate({**qwen, "effort": "high"})
    # Explicitly null is fine, and the param still goes out when the server accepts it.
    assert ModelEntry.model_validate({**qwen, "effort": None}).to_spec().effort is None
    sends = ModelEntry.model_validate({**qwen, "extra": {"reasoning_effort_param": True}})
    assert sends.generation_params(Params(effort="high")).effort == "high"


@pytest.mark.parametrize("kind", sorted(PROVIDER_KINDS))
def test_known_provider_kinds(kind: str) -> None:
    data: dict[str, object] = {"provider": kind, "model": "m"}
    if kind == "replay":
        data["extra"] = {"path": "runs/x/responses.jsonl"}
    if kind == "anthropic_compatible":
        data["base_url_env"] = "LOCAL_LLM_BASE_URL"
    entry = ModelEntry.model_validate(data)
    assert entry.label == f"{kind}:m"
    assert entry.to_spec().provider == kind


def test_unknown_provider_and_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"provider": "gemini", "model": "m"})
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"provider": "anthropic", "model": "m", "base_url": "x"})
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(modle="typo"))


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/v1",
        "http://localhost:8000",
        "localhost:8000",
        "api.example.com/v1",
        "sk-ant-api03-abcdefghijklmnop",
        "sk_live_abc123",
        "xai-AbCdEf1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "MY_TOKEN_A1B2C3D4E5F6G7H8I9",
        "KEY_AKIA2OGYBAH6Q3EXAMPL",
        "openai_base_url",
        "OPENAI-BASE-URL",
        "1OPENAI",
        "",
        "A" * 65,
    ],
)
@pytest.mark.parametrize("field", ["base_url_env", "api_key_env"])
def test_env_fields_reject_urls_and_keys(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"provider": "openai_compatible", "model": "m", field: value})


@pytest.mark.parametrize(
    "value",
    [
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "LOCAL_LLM_BASE_URL",
        "V2",
        "SK_BASE_URL",
        "PK_API_KEY",
        "RK_TOKEN",
        "LLAMA3170BINSTRUCT_BASE_URL",
        "QWEN25CODER32BINSTRUCT_BASE_URL",
        "QWEN3235BA22BINSTRUCT2507_BASE_URL",
        "ASIA_PACIFIC_BASE_URL",
    ],
)
def test_env_fields_accept_names(value: str) -> None:
    entry = ModelEntry.model_validate(
        {"provider": "openai_compatible", "model": "m", "base_url_env": value, "api_key_env": value}
    )
    assert entry.base_url_env == value


def test_urls_rejected_in_namespace_and_model() -> None:
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(
            {"provider": "openai_compatible", "model": "m", "cache_namespace": "http://x"}
        )
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"provider": "openai_compatible", "model": "https://x/m"})


def test_urls_rejected_in_extra() -> None:
    with pytest.raises(ValidationError, match="URL"):
        ModelEntry.model_validate(
            {"provider": "claude_cli", "model": "m", "extra": {"binary": "http://localhost/c"}}
        )
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(
            {"provider": "openai_compatible", "model": "m", "extra": {"api_key": "sk-live-ab12"}}
        )


def test_labels_that_slug_to_one_config_id_rejected() -> None:
    models = [
        {"provider": "openai_compatible", "model": "Qwen/Qwen3-32B"},
        {"provider": "openai_compatible", "model": "Qwen_Qwen3-32B"},
    ]
    assert make_config_id("openai_compatible:Qwen/Qwen3-32B", "baseline", "context") == (
        make_config_id("openai_compatible:Qwen_Qwen3-32B", "baseline", "context")
    )
    with pytest.raises(ValidationError, match="same config id"):
        ExperimentConfig.model_validate(_base(models=models))


def test_config_ids_cover_matrix() -> None:
    cfg = ExperimentConfig.model_validate(
        _base(
            prompts=["baseline", "fewshot-3"],
            mode="tools",
            models=[
                {"provider": "anthropic", "model": "claude-opus-5-5"},
                {"provider": "grok_cli", "model": "grok-4", "label": "grok"},
            ],
        )
    )
    assert cfg.config_ids() == [
        "anthropic_claude-opus-5-5__baseline__tools",
        "anthropic_claude-opus-5-5__fewshot-3__tools",
        "grok__baseline__tools",
        "grok__fewshot-3__tools",
    ]


def test_config_ids_cannot_collide_through_separator() -> None:
    with pytest.raises(ValidationError, match="must not contain"):
        ExperimentConfig.model_validate(
            _base(
                prompts=["c"],
                models=[{"provider": "anthropic", "model": "m", "label": "a__b"}],
            )
        )
    with pytest.raises(ValidationError, match="must not contain"):
        ExperimentConfig.model_validate(_base(prompts=["b__c"]))
    # 'a::b' slugs to 'a_b' (one underscore), which is fine.
    cfg = ExperimentConfig.model_validate(
        _base(models=[{"provider": "anthropic", "model": "m", "label": "a::b"}])
    )
    assert cfg.config_ids() == ["a_b__baseline__context"]


def test_baseline_names_checked() -> None:
    with pytest.raises(ValidationError, match="unknown baselines"):
        ExperimentConfig.model_validate(_base(baselines=["always_aprove"]))
    cfg = ExperimentConfig.model_validate(_base(baselines=sorted(BASELINES)))
    assert set(cfg.baselines) == BASELINES
    assert {"noop_plan", "birthright_only_plan", "oracle"} <= BASELINES
    with pytest.raises(ValidationError, match="baseline names"):
        ExperimentConfig.model_validate(
            _base(models=[{"provider": "anthropic", "model": "m", "label": "oracle"}])
        )


def test_warnings_for_probably_self_hosted_metered_model() -> None:
    cfg = ExperimentConfig.model_validate(
        _base(
            models=[
                {"provider": "openai_compatible", "model": "qwen", "base_url_env": "AI_BASE_URL"},
                {"provider": "openai_compatible", "model": "gpt-5"},
                {
                    "provider": "openai_compatible",
                    "model": "qwen2",
                    "base_url_env": "AI_BASE_URL",
                    "cost_mode": "unmetered",
                },
            ]
        )
    )
    warnings = cfg.warnings()
    assert len(warnings) == 1 and "openai_compatible:qwen'" in warnings[0]
    # Without a budget, a CLI that reports no cost isn't worth a warning.
    cli = ExperimentConfig.model_validate(_base(models=[{"provider": "cursor_cli", "model": "c"}]))
    assert cli.warnings() == []


def test_duplicate_labels_rejected() -> None:
    models = [
        {"provider": "anthropic", "model": "claude-opus-5-5"},
        {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
    ]
    with pytest.raises(ValidationError, match="duplicate model labels"):
        ExperimentConfig.model_validate(_base(models=models))
    models[1]["label"] = "opus-high"
    cfg = ExperimentConfig.model_validate(_base(models=models))
    assert [m.label for m in cfg.models] == ["anthropic:claude-opus-5-5", "opus-high"]


def test_needs_model_or_baseline() -> None:
    with pytest.raises(ValidationError, match="at least one model or baseline"):
        ExperimentConfig.model_validate(_base(models=[]))
    cfg = ExperimentConfig.model_validate(_base(models=[], baselines=["oracle"]))
    assert cfg.baselines == ["oracle"]


@pytest.mark.parametrize(("value", "expected"), [(0.1, 0.1), (1.0, 1.0), (25, 25), (None, None)])
def test_sample_accepts_fraction_or_count(value: float | int | None, expected: object) -> None:
    cfg = ExperimentConfig.model_validate(_base(sample=value))
    assert cfg.sample == expected
    assert type(cfg.sample) is type(expected)


@pytest.mark.parametrize("value", [0.0, 1.5, -0.1, 0, -3, True, "ten"])
def test_sample_rejects_bad_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(sample=value))


def test_inline_loss_matrix() -> None:
    inline = {"APPROVE": {"APPROVE": 0, "DENY": 2}, "DENY": {"APPROVE": 10}}
    cfg = ExperimentConfig.model_validate(_base(loss_matrix=inline))
    assert cfg.loss_matrix == inline
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(loss_matrix={"APPROVE": {"DENY": "two"}}))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(loss_matrix="custom"))


def test_other_field_validation() -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(mode="agentic"))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(name="bad name/with slash"))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(prompts=["baseline", "baseline"]))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(concurrency=0))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(budget_usd=-1))
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(repeats={"k": 0}))


@pytest.mark.parametrize("value", ["../../etc/passwd", "a/b", ".hidden", "", "base line"])
@pytest.mark.parametrize("field", ["prompts", "baselines"])
def test_prompt_and_baseline_names_are_safe(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(**{field: [value]}))


@pytest.mark.parametrize(
    "override",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": "3"},
        {"concurrency": True},
        {"repeats": {"k": True}},
        {"params": {"max_output_tokens": True}},
        {"params": {"effort": "turbo"}},
        {"models": [{"provider": "anthropic", "model": "m", "effort": "turbo"}]},
        {"models": [{"provider": "anthropic", "model": "m", "max_output_tokens": True}]},
        {"models": [{"provider": "anthropic", "model": "m", "cost_mode": "free"}]},
        {"params": {"temperature": True}},
        {"params": {"temperature": "0.5"}},
        {"models": [{"provider": "anthropic", "model": "m", "temperature": True}]},
        {"models": [{"provider": "anthropic", "model": "m", "timeout_s": True}]},
        {"models": [{"provider": "anthropic", "model": "m", "timeout_s": "30"}]},
        {"budget_usd": True},
        {"budget_usd": False},
        {"budget_usd": "50"},
        {"repair": 1},
        {"repair": "yes"},
    ],
)
def test_strict_scalar_fields(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(**override))


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        ("anthropic", "metered"),
        ("openai_compatible", "metered"),
        ("grok_cli", "reported"),
        ("anthropic_compatible", "unmetered"),
        ("replay", "unmetered"),
        ("claude_cli", "reported"),
        ("cursor_cli", "reported"),
    ],
)
def test_default_cost_modes(kind: str, mode: str) -> None:
    data: dict[str, object] = {"provider": kind, "model": "m"}
    if kind == "replay":
        data["extra"] = {"path": "r.jsonl"}
    if kind == "anthropic_compatible":
        data["base_url_env"] = "X_BASE_URL"
    assert ModelEntry.model_validate(data).resolved_cost_mode == mode


def test_float_fields_still_accept_ints_and_floats() -> None:
    cfg = ExperimentConfig.model_validate(
        _base(
            params={"temperature": 0},
            budget_usd=50,
            models=[
                {"provider": "anthropic", "model": "m", "temperature": 0.7, "timeout_s": 30},
            ],
        )
    )
    assert cfg.params.temperature == 0 and cfg.budget_usd == 50
    assert cfg.models[0].temperature == 0.7 and cfg.models[0].timeout_s == 30
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_base(params={"temperature": 2.5}))


def test_yaml_booleans_rejected_in_float_fields(tmp_path: Path) -> None:
    text = "name: t\ndataset: d\ntasks: [requests]\nbudget_usd: true\nbaselines: [oracle]\n"
    with pytest.raises(ValidationError, match="expected a number"):
        load_config(_write(tmp_path, text))


def test_repair_defaults_off_and_is_resolved() -> None:
    off = ExperimentConfig.model_validate(_base())
    assert off.repair is False
    assert off.resolved_dict()["repair"] is False
    on = ExperimentConfig.model_validate(_base(repair=True))
    resolved = on.resolved_dict()
    assert resolved["repair"] is True
    assert ExperimentConfig.model_validate(resolved).repair is True


def test_repair_from_yaml(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, spec_example() + "repair: true\n"))
    assert cfg.repair is True
    assert load_config(_write(tmp_path, spec_example())).repair is False


def _absolute_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node] if node.startswith(("/", "~")) or Path(node).is_absolute() else []
    if isinstance(node, dict):
        return [x for v in node.values() for x in _absolute_strings(v)]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType, reportUnknownMemberType]
    if isinstance(node, list):
        return [x for v in node for x in _absolute_strings(v)]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return []


def test_resolved_dict_never_records_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "someone"
    monkeypatch.setenv("HOME", str(home))
    cfg = ExperimentConfig.model_validate(
        _base(
            dataset=str(home / "datasets" / "identitybench-v1"),
            models=[
                {
                    "provider": "grok_cli",
                    "model": "grok-4",
                    "extra": {"binary": str(home / ".local" / "bin" / "grok")},
                },
                {"provider": "claude_cli", "model": "c", "extra": {"binary": "~/bin/claude"}},
                {"provider": "codex_cli", "model": "x", "extra": {"binary": "codex"}},
                {
                    "provider": "replay",
                    "model": "m",
                    "extra": {"path": str(home / "runs" / "x" / "responses.jsonl")},
                },
            ],
        )
    )
    monkeypatch.chdir(tmp_path)
    resolved = cfg.resolved_dict()
    assert _absolute_strings(resolved) == []
    assert "someone" not in json.dumps(resolved)
    assert resolved["dataset"] == "identitybench-v1"
    extras = [m["extra"] for m in resolved["models"]]
    assert extras == [
        {"binary": "grok"},
        {"binary": "claude"},
        {"binary": "codex"},
        {"path": "responses.jsonl"},
    ]
    # Relative to a base above them, paths keep their structure; the config is untouched.
    under = cfg.resolved_dict(base=home)
    assert under["dataset"] == "datasets/identitybench-v1"
    assert under["models"][0]["extra"] == {"binary": ".local/bin/grok"}
    assert under["models"][3]["extra"] == {"path": "runs/x/responses.jsonl"}
    assert cfg.models[0].extra["binary"] == str(home / ".local" / "bin" / "grok")


def test_integer_sample_of_one_warns() -> None:
    assert any(
        "sample: 1 is a count" in w
        for w in ExperimentConfig.model_validate(_base(sample=1)).warnings()
    )
    assert ExperimentConfig.model_validate(_base(sample=1.0)).warnings() == []
    assert ExperimentConfig.model_validate(_base(sample=2)).warnings() == []


def test_namespace_alone_does_not_make_openai_self_hosted() -> None:
    """SPEC §11.4: any model may set cache_namespace, so it is no self-hosting marker alone."""
    paid = {"provider": "openai_compatible", "model": "gpt-5", "cache_namespace": "v2"}
    assert ModelEntry.model_validate(paid).resolved_cost_mode == "metered"
    default_env = {**paid, "base_url_env": "OPENAI_BASE_URL"}
    assert ModelEntry.model_validate(default_env).resolved_cost_mode == "metered"
    local = {**paid, "base_url_env": "LOCAL_LLM_BASE_URL"}
    assert ModelEntry.model_validate(local).resolved_cost_mode == "unmetered"


@pytest.mark.parametrize(
    "entry",
    [
        {"provider": "anthropic", "model": "claude-opus-5-5", "cost_mode": "unmetered"},
        {"provider": "openai_compatible", "model": "gpt-5", "cost_mode": "unmetered"},
        {
            "provider": "openai_compatible",
            "model": "gpt-5",
            "base_url_env": "OPENAI_BASE_URL",
            "cost_mode": "unmetered",
        },
    ],
)
def test_unmetered_metered_vendor_warns(entry: dict[str, object]) -> None:
    without_budget = ExperimentConfig.model_validate(_base(models=[entry]))
    (warning,) = without_budget.warnings()
    assert "calls a metered API" in warning and "budget_usd" not in warning
    with_budget = ExperimentConfig.model_validate(_base(models=[entry], budget_usd=50))
    (warning,) = with_budget.warnings()
    assert "budget_usd (50) will not count its spend" in warning


def test_unmetered_self_hosted_does_not_warn() -> None:
    entries = [
        {
            "provider": "openai_compatible",
            "model": "q",
            "base_url_env": "AI_BASE_URL",
            "cache_namespace": "d1",
        },
        {
            "provider": "openai_compatible",
            "model": "r",
            "base_url_env": "AI_BASE_URL",
            "cost_mode": "unmetered",
        },
        {"provider": "anthropic_compatible", "model": "s", "base_url_env": "AI_BASE_URL"},
        {"provider": "replay", "model": "t", "extra": {"path": "r.jsonl"}},
    ]
    cfg = ExperimentConfig.model_validate(_base(models=entries, budget_usd=5))
    assert cfg.warnings() == []


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, "- just\n- a list\n"))
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(_write(tmp_path, "name: [unclosed\n"))


@pytest.mark.parametrize("protocol", ["native", "action"])
def test_a_tool_protocol_override_is_kept_from_the_provider(protocol: str) -> None:
    """``extra.tool_protocol`` is the runner's, not a constructor option: the entry exposes
    it, the manifest's resolved config records it, and the provider's spec never sees it."""
    entry = {
        "provider": "openai_compatible",
        "model": "served",
        "base_url_env": "LOCAL_LLM_BASE_URL",
        "cache_namespace": "deploy-1",
        "extra": {"tool_protocol": protocol, "max_retries": 2},
    }
    cfg = ExperimentConfig.model_validate(_base(mode="tools", models=[entry]))
    (model,) = cfg.models
    assert model.tool_protocol == protocol
    assert model.to_spec().extra == {"max_retries": 2}
    recorded = cfg.resolved_dict()["models"][0]["extra"]
    assert recorded == {"tool_protocol": protocol, "max_retries": 2}
    plain = ExperimentConfig.model_validate(_base(mode="tools"))
    assert plain.models[0].tool_protocol is None
    unset = {"provider": "claude_cli", "model": "sonnet", "extra": {"tool_protocol": None}}
    nulled = ExperimentConfig.model_validate(_base(models=[unset]))  # no override: any mode
    assert nulled.models[0].tool_protocol is None and nulled.models[0].to_spec().extra == {}


@pytest.mark.parametrize("value", ["json", "NATIVE", ["native"], 1])
def test_a_tool_protocol_override_must_name_a_protocol(value: object) -> None:
    entry = {"provider": "claude_cli", "model": "sonnet", "extra": {"tool_protocol": value}}
    with pytest.raises(ValidationError, match=r"extra\.tool_protocol must be one of"):
        ExperimentConfig.model_validate(_base(mode="tools", models=[entry]))


def test_a_tool_protocol_override_needs_tool_use_mode() -> None:
    entry = {"provider": "claude_cli", "model": "sonnet", "extra": {"tool_protocol": "action"}}
    with pytest.raises(ValidationError, match="applies to mode 'tools' only"):
        ExperimentConfig.model_validate(_base(models=[entry]))


def test_tool_protocols_match_the_renderers() -> None:
    from typing import get_args

    from identitybench.render.tools import ToolProtocol

    assert get_args(ToolProtocol) == TOOL_PROTOCOLS
    assert frozenset({"tool_protocol"}) == RUNNER_EXTRA_KEYS


OPENROUTER_ENTRY: dict[str, object] = {
    "provider": "openai_compatible",
    "model": "deepseek/deepseek-v4-pro-0813",
    "label": "deepseek-v4-pro",
    "base_url_env": "OPENROUTER_BASE_URL",
}


def test_an_openrouter_entry_is_costed_from_reported_cost_under_a_budget() -> None:
    cfg = ExperimentConfig.model_validate(_base(models=[OPENROUTER_ENTRY], budget_usd=2))
    (entry,) = cfg.models
    assert entry.resolved_cost_mode == "reported" and entry.reports_cost
    assert cfg.resolved_dict()["models"][0]["cost_mode"] == "reported"
    assert cfg.warnings() == []


def test_a_routing_preference_is_validated_passed_on_and_recorded(tmp_path: Path) -> None:
    text = """\
name: t
dataset: datasets/x
tasks: [requests]
models:
  - provider: openai_compatible
    model: deepseek/deepseek-v4-pro-0813
    base_url_env: OPENROUTER_BASE_URL
    extra:
      provider_routing: {order: [Baidu, Alibaba], allow_fallbacks: false, require_parameters: true}
"""
    cfg = load_config(_write(tmp_path, text))
    routing = {"order": ["Baidu", "Alibaba"], "allow_fallbacks": False, "require_parameters": True}
    assert cfg.models[0].to_spec(cfg.params).extra == {"provider_routing": routing}
    assert cfg.resolved_dict()["models"][0]["extra"] == {"provider_routing": routing}


@pytest.mark.parametrize(
    ("routing", "message"),
    [
        ("Baidu", "provider_routing must be a non-empty mapping"),
        ("{allow_fallbacks: 'false'}", "provider_routing allow_fallbacks must be true or false"),
        ("{order: Baidu}", "provider_routing order must be a list of host names"),
        ("{order: ['http://evil.example']}", "contains a URL"),
    ],
)
def test_a_bad_routing_preference_fails_at_load(tmp_path: Path, routing: str, message: str) -> None:
    text = (
        "name: t\ndataset: d\ntasks: [requests]\nmodels:\n"
        "  - {provider: openai_compatible, model: m, base_url_env: OPENROUTER_BASE_URL,\n"
        f"     extra: {{provider_routing: {routing}}}}}\n"
    )
    with pytest.raises(ValidationError, match=message):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("base_url_env", [None, "GATEWAY_BASE_URL"])
def test_reported_cost_off_openrouter_warns_under_a_budget(base_url_env: str | None) -> None:
    """Only OpenRouter states a per-call cost among OpenAI-compatible endpoints, so
    ``cost_mode: reported`` on OpenAI itself or another gateway can't be budgeted."""
    entry = {
        "provider": "openai_compatible",
        "model": "gpt-5",
        "cost_mode": "reported",
        "base_url_env": base_url_env,
    }
    cfg = ExperimentConfig.model_validate(_base(models=[entry], budget_usd=5))
    (warning,) = cfg.warnings()
    assert "reports no cost" in warning
    openrouter = {**entry, "base_url_env": "OPENROUTER_BASE_URL"}
    assert (
        ExperimentConfig.model_validate(_base(models=[openrouter], budget_usd=5)).warnings() == []
    )


def test_openrouter_marked_unmetered_warns_and_metered_is_not_called_self_hosted() -> None:
    unmetered = {**OPENROUTER_ENTRY, "cost_mode": "unmetered"}
    (warning,) = ExperimentConfig.model_validate(_base(models=[unmetered])).warnings()
    assert "calls a metered API" in warning
    metered = {**OPENROUTER_ENTRY, "cost_mode": "metered"}
    assert ExperimentConfig.model_validate(_base(models=[metered])).warnings() == []
