"""Model family inference and the judge panel rules (SPEC §13.2)."""

from __future__ import annotations

from datetime import datetime

import pytest

from identitybench.judge.families import (
    BASELINE_FAMILY,
    FamilyError,
    SelfFamilyError,
    check_judge_panel,
    check_self_family,
    is_baseline_prediction,
    model_family,
    output_families_from_manifest,
    resolve_output_families,
)
from identitybench.providers.baselines import BASELINE_NAMES
from identitybench.runner.manifest import ConfigManifest, DatasetInfo, ParamsRecord, RunManifest
from identitybench.schemas.enums import Split
from identitybench.schemas.predictions import PredictionStatus

from .helpers import (
    CONFIG_ANTHROPIC,
    CONFIG_OPENAI,
    CONFIG_QWEN,
    baseline_prediction,
    failed_prediction,
    request_case,
    request_prediction,
)


@pytest.mark.parametrize(
    ("kind", "model", "family"),
    [
        ("anthropic", "claude-opus-5-5", "anthropic"),
        ("claude_cli", "opus", "anthropic"),
        ("codex_cli", "gpt-5-codex", "openai"),
        ("grok_cli", "grok-4", "xai"),
        ("baseline", "always_approve", BASELINE_FAMILY),
        ("openai_compatible", "gpt-5", "openai"),
        ("openai_compatible", "o3-mini", "openai"),
        ("openai_compatible", "gpt4o", "openai"),
        ("openai_compatible", "Qwen2.5-72B-Instruct-GPTQ-Int4", "alibaba"),
        ("openai_compatible", "openai/gpt-oss-120b", "openai"),
        ("openai_compatible", "gemini-2.5-pro", "google"),
        ("openai_compatible", "Qwen/Qwen3-32B-Instruct", "alibaba"),
        ("openai_compatible", "meta-llama/Llama-3.1-70B", "meta"),
        ("openai_compatible", "mistral-large-latest", "mistral"),
        ("openai_compatible", "codestral-2501", "mistral"),
        ("openai_compatible", "deepseek-v3", "deepseek"),
        ("openai_compatible", "phi-4", "microsoft"),
        ("anthropic_compatible", "claude-sonnet-4-5", "anthropic"),
        ("anthropic_compatible", "sonnet-4", "anthropic"),
        ("replay", "grok-code-fast", "xai"),
        (None, "claude-haiku-5-5-20261001", "anthropic"),
    ],
)
def test_known_families(kind: str | None, model: str, family: str) -> None:
    assert model_family(kind, model) == family


@pytest.mark.parametrize(
    ("kind", "model"),
    [
        ("cursor_cli", "auto"),
        ("openai_compatible", "my-finetune-v2"),
        ("openai_compatible", "deepseek-r1-distill-qwen-32b"),  # two families: ambiguous
        ("replay", "replay"),
        # Only the provider kind or a baseline's own prediction marks a baseline: a model whose
        # name mentions one is not mistaken for it.
        ("openai_compatible", "baseline"),
        (None, "my-baseline-model"),
        ("openai_compatible", ""),
        (None, None),
    ],
)
def test_unknown_or_ambiguous_families(kind: str | None, model: str | None) -> None:
    assert model_family(kind, model) is None


def _manifest(*configs: tuple[str, str, str]) -> RunManifest:
    return RunManifest(
        run_id="r",
        created_at=datetime(2026, 10, 10),
        dataset=DatasetInfo(name="d", version="v1", sha256="0" * 64, path="datasets/d"),
        split=Split.test,
        configs=[
            ConfigManifest(
                config_id=config_id,
                label=config_id,
                provider=provider,
                model=model,
                endpoint_identity="e",
                params=ParamsRecord(max_output_tokens=10),
                prompt_variant="baseline",
                prompt_sha256="0" * 64,
                mode="context",
            )
            for config_id, provider, model in configs
        ],
    )


def test_output_families_from_manifest() -> None:
    manifest = _manifest(
        ("a", "anthropic", "claude-opus-5-5"),
        ("b", "baseline", "always_deny"),
        ("c", "openai_compatible", "served-model-x"),
    )
    with pytest.raises(FamilyError, match=r"c \(openai_compatible:served-model-x\)"):
        output_families_from_manifest(manifest)
    families = output_families_from_manifest(manifest, overrides={"c": "alibaba"})
    assert families == {"a": "anthropic", "b": BASELINE_FAMILY, "c": "alibaba"}


def test_overrides_cannot_cross_the_baseline_line() -> None:
    """``BASELINE_FAMILY`` decides what is left out of judging, so an override can't hand it to
    a model (a mistyped ``--family cfg=baseline`` would drop the model silently), nor a model
    family to a baseline."""
    manifest = _manifest(
        ("a", "anthropic", "claude-opus-5-5"),
        ("q", "openai_compatible", "served-model-x"),
        ("oracle", "baseline", "oracle"),
    )
    with pytest.raises(FamilyError) as model_as_baseline:
        output_families_from_manifest(manifest, overrides={"q": BASELINE_FAMILY})
    message = str(model_as_baseline.value)
    assert "q (openai_compatible:served-model-x) is a model configuration" in message
    assert "reserved for the SPEC §14 baselines" in message
    with pytest.raises(FamilyError, match=r"oracle \(baseline:oracle\) is a SPEC §14 baseline"):
        output_families_from_manifest(manifest, overrides={"q": "alibaba", "oracle": "anthropic"})
    # Restating what inference gives is fine on either side of the line.
    families = output_families_from_manifest(
        manifest, overrides={"q": "alibaba", "oracle": BASELINE_FAMILY}
    )
    assert families == {"a": "anthropic", "q": "alibaba", "oracle": BASELINE_FAMILY}


def test_resolve_refuses_a_given_family_that_crosses_the_baseline_line() -> None:
    case = request_case()
    model, oracle = request_prediction(case, CONFIG_QWEN), baseline_prediction(case, "oracle")
    with pytest.raises(FamilyError, match=f"{CONFIG_QWEN} .* is a model configuration"):
        resolve_output_families([model, oracle], {CONFIG_QWEN: BASELINE_FAMILY})
    with pytest.raises(FamilyError, match=r"oracle .* is a SPEC §14 baseline"):
        resolve_output_families([model, oracle], {"oracle": "alibaba"})
    # A failed call shows nothing to judge, so it can't contradict the stated family.
    failed = failed_prediction(case, CONFIG_QWEN)
    assert resolve_output_families([failed], {CONFIG_QWEN: BASELINE_FAMILY}) == {
        CONFIG_QWEN: BASELINE_FAMILY
    }


def test_resolve_prefers_the_mapping_then_the_served_model() -> None:
    case = request_case()
    predictions = [
        request_prediction(case, CONFIG_ANTHROPIC),
        request_prediction(case, CONFIG_OPENAI),
    ]
    assert resolve_output_families(predictions) == {
        CONFIG_ANTHROPIC: "anthropic",
        CONFIG_OPENAI: "openai",
    }
    assert (
        resolve_output_families(predictions, {CONFIG_OPENAI: "custom"})[CONFIG_OPENAI] == "custom"
    )


def test_resolve_rejects_unknown_and_mixed_served_models() -> None:
    case = request_case()
    unknown = request_prediction(case, "cfg-x").model_copy(update={"served_model": "mystery"})
    with pytest.raises(FamilyError, match="cfg-x"):
        resolve_output_families([unknown])
    first = request_prediction(case, "cfg-y").model_copy(update={"served_model": "gpt-5"})
    second = first.model_copy(update={"served_model": "claude-opus-5-5", "sample_idx": 1})
    with pytest.raises(FamilyError, match="different families"):
        resolve_output_families([first, second])


def test_invalid_predictions_are_not_evidence_of_a_family() -> None:
    """A failed call usually reports no served model and is never judged, so it neither blocks
    nor decides a configuration's family."""
    cases = [request_case(f"c_req_{i}") for i in range(3)]
    predictions = [
        request_prediction(cases[0], CONFIG_OPENAI),
        failed_prediction(cases[1], CONFIG_OPENAI),
        failed_prediction(cases[2], CONFIG_OPENAI, status=PredictionStatus.refused),
        # A failed call reporting another vendor's model is ignored too: nothing of it is judged.
        failed_prediction(cases[1], CONFIG_ANTHROPIC, served_model="claude-opus-5-5"),
        request_prediction(cases[0], CONFIG_ANTHROPIC),
        # A configuration with nothing but failures needs no family...
        failed_prediction(cases[0], CONFIG_QWEN, status=PredictionStatus.over_budget),
    ]
    assert resolve_output_families(predictions) == {
        CONFIG_OPENAI: "openai",
        CONFIG_ANTHROPIC: "anthropic",
    }
    # ...but keeps the one it is given.
    given = resolve_output_families(predictions, {CONFIG_QWEN: "alibaba"})
    assert given[CONFIG_QWEN] == "alibaba"


def test_a_valid_prediction_without_a_served_model_still_needs_a_family() -> None:
    case = request_case()
    silent = request_prediction(case, "cfg-cli").model_copy(update={"served_model": None})
    with pytest.raises(FamilyError, match=r"cfg-cli \(served model None\)"):
        resolve_output_families([silent, failed_prediction(case, "cfg-cli")])
    assert resolve_output_families([silent], {"cfg-cli": "anthropic"}) == {"cfg-cli": "anthropic"}


def test_panel_must_span_enough_families() -> None:
    check_judge_panel({"a": "anthropic", "b": "openai"}, min_families=2)
    check_judge_panel({"a": "anthropic"}, min_families=1)
    with pytest.raises(FamilyError, match="span 1 family"):
        check_judge_panel({"a": "anthropic", "b": "anthropic"}, min_families=2)
    with pytest.raises(FamilyError, match="at least one judge"):
        check_judge_panel({}, min_families=1)


def test_self_family_guard() -> None:
    judges = {"j1": "anthropic"}
    check_self_family({"cfg-gpt": "openai"}, judges)
    with pytest.raises(SelfFamilyError, match=r"cfg-claude \(anthropic\)"):
        check_self_family({"cfg-claude": "anthropic", "cfg-gpt": "openai"}, judges)
    # One cross-family judge on the panel is enough.
    check_self_family({"cfg-claude": "anthropic"}, {"j1": "anthropic", "j2": "openai"})


def test_baselines_are_recognised_from_their_predictions() -> None:
    """The runner records a baseline under its own name as configuration and served model."""
    case = request_case()
    for name in BASELINE_NAMES:
        assert is_baseline_prediction(baseline_prediction(case, name))
    model = request_prediction(case, CONFIG_OPENAI)
    assert not is_baseline_prediction(model)
    # A model endpoint that reports a baseline-like served name is still a model.
    impostor = model.model_copy(update={"served_model": "oracle"})
    assert not is_baseline_prediction(impostor)
    predictions = [model, baseline_prediction(case, "always_approve"), baseline_prediction(case)]
    assert resolve_output_families(predictions) == {
        CONFIG_OPENAI: "openai",
        "always_approve": BASELINE_FAMILY,
    }
    with pytest.raises(FamilyError, match="served model 'oracle'"):
        resolve_output_families([impostor])


def test_no_judge_may_have_the_baseline_family() -> None:
    with pytest.raises(FamilyError, match=r"reserved for the SPEC §14 baselines.*: b"):
        check_judge_panel({"a": "anthropic", "b": BASELINE_FAMILY}, min_families=1)
    # Baselines are therefore never judged only by their own family.
    check_self_family({"oracle": BASELINE_FAMILY}, {"a": "anthropic"})
