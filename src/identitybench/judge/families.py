"""Model families, for the judge's self-family guard and the family-preference check.

SPEC §13.2: a model is never judged only by its own family, and SPEC §13.3 measures family
preference as judge-minus-human residuals compared across output families. Both need to know
which family produced an output and which family a judge belongs to.

A family is inferred from the provider kind when the kind pins the vendor (``anthropic`` and
``claude_cli`` only serve Claude, ``codex_cli`` OpenAI models, ``grok_cli`` Grok) and otherwise
from the model name. ``openai_compatible``, ``anthropic_compatible``, ``replay``, ``agy_cli``
(Gemini, Claude and gpt-oss models) and ``cursor_cli`` can serve anything, so their family comes
from the name alone. A name that
matches no family, or more than one (a distilled ``deepseek-r1-distill-qwen``), is unknown, and
the caller must state the family: guessing would let the guard pass on a wrong guess.

**Baselines** (SPEC §14) are configurations in every run: the runner records each under its own
name (``config_id`` and ``served_model`` both ``always_approve``, ``oracle``, ...) with provider
kind ``baseline``. Their rationales are fixed templates, not model explanations, so they get
``BASELINE_FAMILY``, which no judge may claim: a baseline is never "its own family" to any judge,
and the callers (``run.judge_predictions``, ``labels.export_label_sheets``) leave baselines out
unless asked to include them as calibration probes. Because that family decides what is left
out, a stated family can't move a configuration across the line: ``BASELINE_FAMILY`` is refused
for a model configuration (a mistyped ``--family cfg=baseline`` would silently drop the model
from the campaign), and a model family for a baseline (its templates would be judged as a model's
explanations).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from identitybench.providers.baselines import BASELINE_NAMES
from identitybench.runner.manifest import RunManifest
from identitybench.schemas.predictions import Prediction

__all__ = [
    "BASELINE_FAMILY",
    "KIND_FAMILIES",
    "FamilyError",
    "SelfFamilyError",
    "check_judge_panel",
    "check_self_family",
    "is_baseline_prediction",
    "model_family",
    "output_families_from_manifest",
    "resolve_output_families",
]

BASELINE_FAMILY = "baseline"
"""The SPEC §14 baselines: rule-based, so no judge may have this family (``check_judge_panel``)."""

KIND_FAMILIES: Mapping[str, str] = MappingProxyType(
    {
        "anthropic": "anthropic",
        "claude_cli": "anthropic",
        "codex_cli": "openai",
        "grok_cli": "xai",
        "baseline": BASELINE_FAMILY,
    }
)
"""Provider kinds that only ever serve one family."""

_SPLIT_RE = re.compile(r"[/:_.\s-]+")

_SUBSTRINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic", ("claude", "anthropic")),
    ("openai", ("chatgpt", "openai", "davinci")),
    ("xai", ("grok",)),
    ("google", ("gemini", "gemma", "palm")),
    ("meta", ("llama",)),
    ("alibaba", ("qwen", "qwq")),
    (
        "mistral",
        ("mistral", "mixtral", "codestral", "ministral", "magistral", "devstral", "pixtral"),
    ),
    ("deepseek", ("deepseek",)),
    ("cohere", ("cohere",)),
    ("moonshot", ("kimi", "moonshot")),
    ("zhipu", ("chatglm", "zhipu")),
    ("nvidia", ("nemotron",)),
)
"""Distinctive substrings of a lower-cased model name, per family."""

_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"^(opus|sonnet|haiku)\d*$")),
    ("openai", re.compile(r"^(gpt(\d+[a-z]*)?|o\d+|codex)$")),  # not "gptq" (quantization)
    ("xai", re.compile(r"^xai$")),
    ("meta", re.compile(r"^meta$")),
    ("microsoft", re.compile(r"^phi\d*$")),
    ("cohere", re.compile(r"^(command|aya)$")),
    ("zhipu", re.compile(r"^glm\d*$")),
)
"""Short names that would match inside unrelated words, so they must be a whole token of the
name (split on ``/ : _ . -`` and whitespace)."""


class FamilyError(ValueError):
    """A family can't be determined, or a judge panel breaks the family rules."""


class SelfFamilyError(FamilyError):
    """Some configuration would be judged only by judges of its own family (SPEC §13.2)."""


def model_family(kind: str | None, model: str | None) -> str | None:
    """The family of ``model`` served by provider ``kind``; None when unknown or ambiguous."""
    if kind is not None and kind in KIND_FAMILIES:
        return KIND_FAMILIES[kind]
    if not model:
        return None
    name = model.casefold()
    tokens = [t for t in _SPLIT_RE.split(name) if t]
    found = {family for family, needles in _SUBSTRINGS if any(n in name for n in needles)}
    found |= {family for family, pattern in _TOKENS if any(pattern.match(t) for t in tokens)}
    return found.pop() if len(found) == 1 else None


def is_baseline_prediction(prediction: Prediction) -> bool:
    """Whether ``prediction`` comes from a SPEC §14 baseline, judging by the prediction alone.

    The runner names a baseline's configuration after the baseline and the baseline reports
    that name as its served model, so both must equal one of ``BASELINE_NAMES``. Model
    configurations never qualify: their IDs are ``<label>__<prompt>__<mode>``, so a model
    whose endpoint happens to report a served name like ``oracle`` is not mistaken for one.
    """
    served = prediction.served_model
    return served is not None and served in BASELINE_NAMES and prediction.config_id == served


def output_families_from_manifest(
    manifest: RunManifest, overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``config_id -> family`` for every configuration of a run.

    ``overrides`` (by ``config_id``) wins over inference; it is how a self-hosted model with an
    uninformative served name gets a family. Raises ``FamilyError`` naming every configuration
    whose family is still unknown, and every override that crosses the baseline line (see the
    module docstring): ``BASELINE_FAMILY`` for a model configuration, or a model family for one
    whose provider kind is ``baseline``.
    """
    overrides = overrides or {}
    families: dict[str, str] = {}
    unknown: list[str] = []
    crossing: list[str] = []
    for config in manifest.configs:
        name = f"{config.config_id} ({config.provider}:{config.model})"
        override = overrides.get(config.config_id)
        is_baseline = KIND_FAMILIES.get(config.provider) == BASELINE_FAMILY
        if override and (override == BASELINE_FAMILY) != is_baseline:
            crossing.append(_crossing(name, override, is_baseline))
            continue
        family = override or model_family(config.provider, config.model)
        if family is None:
            unknown.append(name)
        else:
            families[config.config_id] = family
    if crossing:
        raise FamilyError("; ".join(crossing))
    if unknown:
        raise FamilyError(
            "cannot infer the model family of " + ", ".join(unknown) + "; pass it in overrides"
        )
    return families


def _crossing(name: str, family: str, is_baseline: bool) -> str:
    """Why ``name`` can't be given ``family`` (it crosses the baseline line)."""
    if is_baseline:
        return (
            f"{name} is a SPEC §14 baseline, so its family is {BASELINE_FAMILY!r}, not {family!r}: "
            "its template rationales must not be judged as a model's"
        )
    return (
        f"{name} is a model configuration; family {BASELINE_FAMILY!r} is reserved for the SPEC "
        "§14 baselines and would leave its rationales out of judging; give its model family"
    )


def resolve_output_families(
    predictions: Sequence[Prediction], output_families: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``config_id -> family`` for every configuration with an output a judge could see.

    ``output_families`` (usually ``output_families_from_manifest``) is used where it has the
    configuration; otherwise a baseline's predictions (``is_baseline_prediction``) get
    ``BASELINE_FAMILY``, and any other family is inferred from the ``served_model`` the endpoint
    reported on the configuration's valid (status ``ok``) predictions. INVALID predictions are
    skipped: an errored, refused or over-budget call usually reports no served model, and none
    of it is judged, so one transport error must not leave a configuration without a family.
    A configuration with only INVALID predictions (and no entry in ``output_families``) is left
    out, since nothing of it needs a family. A configuration whose valid predictions name
    different families, or one of which has no recognizable served model, raises
    ``FamilyError``: the self-family guard is about the outputs actually judged, so it can't
    rest on a guess for any of them. So does a given family that crosses the baseline line
    (see the module docstring) on any valid prediction: ``BASELINE_FAMILY`` for a model's
    output, or a model family for a baseline's.
    """
    given = output_families or {}
    resolved: dict[str, str] = {}
    problems: list[str] = []
    crossing: set[str] = set()
    for prediction in predictions:
        config_id = prediction.config_id
        if config_id in given:
            family_given = given[config_id]
            is_baseline = is_baseline_prediction(prediction)
            if not prediction.is_invalid and (family_given == BASELINE_FAMILY) != is_baseline:
                served = f"served model {prediction.served_model!r}"
                crossing.add(_crossing(f"{config_id} ({served})", family_given, is_baseline))
            resolved[config_id] = family_given
            continue
        if prediction.is_invalid:
            continue
        if is_baseline_prediction(prediction):
            family: str | None = BASELINE_FAMILY
        else:
            family = model_family(None, prediction.served_model)
        if family is None:
            problems.append(f"{config_id} (served model {prediction.served_model!r})")
        elif resolved.setdefault(config_id, family) != family:
            problems.append(f"{config_id} (served models from different families)")
    if crossing:
        raise FamilyError("; ".join(sorted(crossing)))
    if problems:
        detail = ", ".join(sorted(set(problems)))
        raise FamilyError(f"cannot determine the output family of {detail}")
    return resolved


def check_judge_panel(judge_families: Mapping[str, str], *, min_families: int) -> None:
    """Raise ``FamilyError`` unless the judges span at least ``min_families`` families, none of
    them ``BASELINE_FAMILY``.

    SPEC §13.2's default panel is two judges from different families, so their average never
    rests on one vendor's taste. ``BASELINE_FAMILY`` is reserved for the rule-based baselines,
    which is what lets every judge score them when they are included as calibration probes.
    """
    if not judge_families:
        raise FamilyError("at least one judge is required")
    reserved = sorted(name for name, family in judge_families.items() if family == BASELINE_FAMILY)
    if reserved:
        raise FamilyError(
            f"family {BASELINE_FAMILY!r} is reserved for the SPEC §14 baselines; "
            f"give these judges their model family: {', '.join(reserved)}"
        )
    distinct = sorted(set(judge_families.values()))
    if len(distinct) < min_families:
        raise FamilyError(
            f"judges span {len(distinct)} famil{'y' if len(distinct) == 1 else 'ies'} "
            f"({', '.join(distinct)}); at least {min_families} are required"
        )


def check_self_family(
    output_families: Mapping[str, str], judge_families: Mapping[str, str]
) -> None:
    """Raise ``SelfFamilyError`` if any configuration would be judged only by its own family.

    This is checked before any judge is called. ``aggregate.aggregate_scores`` applies the
    same rule again per rationale, because a cross-family judge can still fail on a single
    call and leave only same-family scores behind.
    """
    families = set(judge_families.values())
    offenders = sorted(
        f"{config_id} ({family})"
        for config_id, family in output_families.items()
        if families <= {family}
    )
    if offenders:
        raise SelfFamilyError(
            "these configurations would be judged only by their own family: " + ", ".join(offenders)
        )
