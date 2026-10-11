"""Experiment configuration: YAML -> validated pydantic model (SPEC §16, §11.1, §11.4).

The config is committed to the repo, so it must never contain secrets or endpoint URLs.
Models name the *environment variables* that hold their base URL and key (``base_url_env``,
``api_key_env``); the validators here reject anything that looks like a URL or a key pasted in
place of a variable name.

Each ``models:`` entry is checked against the providers' ``ModelSpec`` at load time, so a
config that validates here is one ``make_provider`` will accept: mistakes surface when the
file is loaded, not halfway through a run.

``ModelEntry`` carries one field beyond the brief's ``ModelSpec`` list: ``cost_mode``, because
the manifest must tell a self-hosted model's real $0 from an unpriced API's unknown cost
(SPEC §11.4) and the provider kind alone can't (``openai_compatible`` covers both).
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    ValidationError,
    field_validator,
    model_validator,
)

from identitybench.providers.base import GenerationParams
from identitybench.providers.factory import PROVIDER_KINDS, ModelSpec, ProviderKind
from identitybench.providers.openai_compat import (
    DEFAULT_BASE_URL_ENV as OPENAI_DEFAULT_BASE_URL_ENV,
)
from identitybench.runner.cost import (
    CostMode,
    default_cost_mode,
    points_at_metered_vendor,
    reports_cost,
)
from identitybench.runner.dataset import SampleSpec, normalize_suite, portable_path
from identitybench.schemas.enums import Split, Suite

__all__ = [
    "BASELINES",
    "PROVIDER_KINDS",
    "ConfigError",
    "Effort",
    "ExperimentConfig",
    "Mode",
    "ModelEntry",
    "Params",
    "ProviderKind",
    "Repeats",
    "StrictFloat",
    "load_config",
    "make_config_id",
    "slug",
]

Mode = Literal["context", "tools"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""The reasoning-effort values ``GenerationParams.effort`` documents."""

StrictInt = Annotated[int, Strict()]
"""YAML ``true`` would otherwise coerce to 1 for a seed, count or limit."""


def _check_number(value: Any) -> Any:
    """Accept a real int or float only: YAML ``true`` would otherwise become 1.0 (a $1 budget,
    temperature 1), and a quoted ``"0.5"`` would be coerced silently."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    return value


StrictFloat = Annotated[float, BeforeValidator(_check_number)]
"""A float field that still accepts an int (``temperature: 0``) but never a bool or string."""

_PATH_EXTRA_KEYS = ("binary", "path")
"""``ModelSpec.extra`` options that hold a local path (CLI binary, replay file)."""

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_AWS_KEY_ID = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")

_NO_EFFORT_KINDS = frozenset({"cursor_cli"})
"""Kinds whose ``ModelSpec`` rejects ``effort`` (the factory's rule; a test pins the match).
The experiment-wide ``params.effort`` is a default, so it is simply not applied to these."""

BASELINES = frozenset(
    {
        "always_approve",
        "always_deny",
        "always_escalate",
        "peer_recommender",
        "surface_probe",
        "noop_plan",
        "birthright_only_plan",
        "oracle",
    }
)
"""The SPEC §14 baselines, the only names ``baselines:`` accepts."""

_CONFIG_ID_SEP = "__"


class ConfigError(ValueError):
    """The experiment file is not a valid experiment config."""


def slug(text: str) -> str:
    """Filesystem-safe form of ``text``: runs of other characters become one ``_``."""
    return _SAFE.sub("_", text).strip("_") or "x"


def make_config_id(label: str, prompt_variant: str, mode: str) -> str:
    """Filesystem-safe ID for one matrix cell (keys manifest usage/cost and output paths).

    Example: ``anthropic_claude-opus-5-5__baseline__context``. ``ExperimentConfig`` keeps
    ``__`` out of labels and prompt names so the parts stay unambiguous.
    """
    return _CONFIG_ID_SEP.join(slug(part) for part in (label, prompt_variant, mode))


def _looks_like_key(segment: str) -> bool:
    """Upper-case credential shapes the UPPER_SNAKE rule alone lets through.

    Model names legitimately mix letters and digits (``LLAMA3170BINSTRUCT``,
    ``QWEN25CODER32BINSTRUCT``: up to about five letter/digit switches) but in a few long
    runs; random keys switch constantly (about 0.4 switches per character for random upper
    alphanumerics). AWS access key IDs are the one common all-upper-case format with a fixed
    shape, so they are matched exactly.
    """
    if _AWS_KEY_ID.fullmatch(segment):
        return True
    switches = sum(1 for a, b in itertools.pairwise(segment) if a.isdigit() != b.isdigit())
    return len(segment) >= 16 and switches >= 6


def _check_env_name(value: str | None) -> str | None:
    """Accept only an upper-snake environment variable *name*.

    Why so strict: the failure we guard against is someone pasting a URL or an API key into a
    committed file. Upper-snake rules out URLs (``/``, ``:``, ``.``) and almost every key
    format (keys are mixed or lower case); ``_looks_like_key`` catches the upper-case rest.
    """
    if value is None:
        return None
    if "/" in value or ":" in value:
        raise ValueError(f"{value!r} looks like a URL; give the environment variable NAME")
    if not _ENV_NAME.fullmatch(value):
        raise ValueError(
            f"{value!r} is not an environment variable name (expected UPPER_SNAKE_CASE, "
            "at most 64 characters); never put keys or URLs in the config"
        )
    if any(_looks_like_key(segment) for segment in value.split("_")):
        raise ValueError(f"{value!r} looks like a credential; give the env var NAME instead")
    return value


EnvName = Annotated[str | None, BeforeValidator(_check_env_name)]


def _no_url(value: str | None) -> str | None:
    if value is not None and "://" in value:
        raise ValueError(f"{value!r} contains a URL; endpoint URLs never go in config")
    return value


NoUrlStr = Annotated[str, BeforeValidator(_no_url)]
OptNoUrlStr = Annotated[str | None, BeforeValidator(_no_url)]


def _check_no_urls(node: Any, path: str) -> None:
    """Reject a URL anywhere in a nested ``extra`` value (it lands verbatim in the manifest)."""
    if isinstance(node, str):
        if "://" in node:
            raise ValueError(f"{path} contains a URL; endpoint URLs never go in config")
    elif isinstance(node, dict):
        for key, child in node.items():
            _check_no_urls(child, f"{path}.{key}")
    elif isinstance(node, list):
        for i, child in enumerate(node):
            _check_no_urls(child, f"{path}[{i}]")


def _check_sample(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("sample must be a fraction in (0, 1] or a positive count")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("sample count must be >= 1")
        return value
    if isinstance(value, float):
        if not 0.0 < value <= 1.0:
            raise ValueError("sample fraction must be in (0, 1]")
        return value
    raise ValueError("sample must be a fraction in (0, 1] or a positive count")


Sample = Annotated[SampleSpec | None, BeforeValidator(_check_sample)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Params(_Strict):
    """Experiment-wide generation defaults (SPEC §16 ``params``)."""

    temperature: StrictFloat | None = Field(default=None, ge=0.0, le=2.0)
    """None means "don't send" (several current models reject non-default temperatures)."""
    max_output_tokens: StrictInt = Field(default=4096, ge=1)
    effort: Effort | None = None
    """Default effort for every model whose provider has an effort setting."""


class ModelEntry(_Strict):
    """One model under test: the providers' ``ModelSpec`` fields plus config-only settings.

    ``temperature`` and ``effort`` left *out* of an entry fall back to the experiment-level
    ``params``; an explicit ``null`` means "don't send" (e.g. OpenAI reasoning models reject
    a temperature, so they need to opt out of a global ``temperature: 0``).
    ``max_output_tokens`` left out or null falls back too. ``extra`` holds provider
    *constructor* options (``binary``, ``max_retries``, replay ``path``, ...) exactly as
    ``ModelSpec`` defines them; they never reach the request params, so they don't change
    cache keys.
    """

    provider: ProviderKind
    model: NoUrlStr = Field(min_length=1)
    label: NoUrlStr = Field(min_length=1)
    """Unique display/config name. Defaults to ``provider:model``."""
    base_url_env: EnvName = None
    api_key_env: EnvName = None
    cache_namespace: OptNoUrlStr = None
    """Part of the endpoint identity; bump it when a self-hosted deployment changes behind the
    same URL (SPEC §11.4). Any model may set one."""
    effort: Effort | None = None
    temperature: StrictFloat | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: StrictInt | None = Field(default=None, ge=1)
    timeout_s: StrictFloat | None = Field(default=None, gt=0)
    concurrency: StrictInt | None = Field(default=None, ge=1)
    cost_mode: CostMode | None = None
    """How this model's spend is known (see ``cost.CostMode``). None picks the provider kind's
    default (``cost.default_cost_mode``): an ``openai_compatible`` entry counts as self-hosted,
    so ``unmetered``, only when it sets a ``cache_namespace`` and names its own endpoint with
    ``base_url_env``; OpenAI itself stays ``metered`` with or without a namespace."""
    extra: dict[str, Any] = Field(default_factory=dict)
    """Provider constructor options, as ``ModelSpec.extra`` (recorded in the manifest)."""

    @model_validator(mode="before")
    @classmethod
    def _default_label(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("label"):
            provider = data.get("provider")
            model = data.get("model")
            if provider is not None and model is not None:
                return {**data, "label": f"{provider}:{model}"}
        return data

    @field_validator("extra")
    @classmethod
    def _extra_has_no_urls(cls, value: dict[str, Any]) -> dict[str, Any]:
        _check_no_urls(value, "extra")
        return value

    @model_validator(mode="after")
    def _factory_accepts(self) -> ModelEntry:
        self.to_spec()
        return self

    @property
    def resolved_cost_mode(self) -> CostMode:
        return self.cost_mode or default_cost_mode(
            self.provider, cache_namespace=self.cache_namespace, base_url_env=self.base_url_env
        )

    @property
    def reports_cost(self) -> bool:
        """For ``reported`` mode: whether this kind's provider reports a per-call cost."""
        return reports_cost(self.provider)

    def _effort_param_off(self) -> bool:
        """An ``openai_compatible`` server configured not to send ``reasoning_effort``: the
        provider drops any effort, so recording one would misstate what ran."""
        return (
            self.provider == "openai_compatible"
            and self.extra.get("reasoning_effort_param") is False
        )

    def to_spec(self, defaults: Params | None = None) -> ModelSpec:
        """The providers' ``ModelSpec`` for this entry, with experiment defaults filled in.

        Raises ``ValueError`` with the factory's message when ``make_provider`` would reject
        the entry.
        """
        d = defaults or Params()
        if "effort" in self.model_fields_set:
            effort = self.effort
            if effort is not None and self._effort_param_off():
                raise ValueError(
                    f"model {self.label!r}: effort is never sent when "
                    "extra.reasoning_effort_param is false"
                )
        elif self.provider in _NO_EFFORT_KINDS or self._effort_param_off():
            effort = None
        else:
            effort = d.effort
        temperature = self.temperature if "temperature" in self.model_fields_set else d.temperature
        try:
            return ModelSpec(
                provider=self.provider,
                model=self.model,
                label=self.label,
                base_url_env=self.base_url_env,
                api_key_env=self.api_key_env,
                cache_namespace=self.cache_namespace,
                effort=effort,
                temperature=temperature,
                max_output_tokens=self.max_output_tokens or d.max_output_tokens,
                timeout_s=self.timeout_s,
                concurrency=self.concurrency,
                extra=dict(self.extra),
            )
        except ValidationError as exc:
            reasons = "; ".join(str(err["msg"]) for err in exc.errors())
            raise ValueError(f"model {self.label!r}: {reasons}") from None

    def generation_params(self, defaults: Params) -> GenerationParams:
        """Per-request generation settings: this entry's overrides over the experiment's."""
        return self.to_spec(defaults).generation_params()


class Repeats(_Strict):
    """``k`` repeats on a stratified ``subset`` of cases (all cases when ``subset`` is None)."""

    k: StrictInt = Field(default=1, ge=1)
    subset: StrictInt | None = Field(default=None, ge=1)


LossMatrix = Literal["default"] | dict[str, Any]


class ExperimentConfig(_Strict):
    """A run matrix: models x prompt variants x one mode, over chosen suites of one split."""

    name: str
    dataset: Path
    split: Split = Split.test
    tasks: list[Suite] = Field(min_length=1)
    mode: Mode = "context"
    prompts: list[str] = Field(default_factory=lambda: ["baseline"], min_length=1)
    """Prompt variant names: file stems under ``prompts/``."""
    models: list[ModelEntry] = Field(default_factory=list[ModelEntry])
    baselines: list[str] = Field(default_factory=list[str])
    params: Params = Field(default_factory=Params)
    repeats: Repeats = Field(default_factory=Repeats)
    concurrency: StrictInt = Field(default=4, ge=1)
    budget_usd: StrictFloat | None = Field(default=None, ge=0.0)
    """Experiment-wide hard stop on summed spend across all configs (``cost.BudgetGuard``)."""
    loss_matrix: LossMatrix = "default"
    """``default`` (SPEC §12.3) or an inline mapping; the scorer validates the inline shape."""
    sample: Sample = None
    """A fraction in (0, 1] (a float) or a count (an int); None runs every case.

    Both are in **families** (a base case plus every twin and adversarial copy of it, see
    ``dataset.stratified_sample``), so ``sample: 30`` can return more than 30 cases, and the
    int ``1`` means one family while the float ``1.0`` means everything. The manifest records
    the unit next to the number (``RunManifest.sample_unit``)."""
    seed: StrictInt = Field(default=0, ge=0)
    """Seed for sampling and repeat-subset selection."""
    repair: bool = Field(default=False, strict=True)
    """SPEC §11.3: off by default. When on, an output that fails validation gets one re-ask with
    the validation error; repaired predictions are flagged and the flag is in the manifest."""

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("name must be 1-128 chars of letters, digits, '.', '_' or '-'")
        return value

    @field_validator("tasks", mode="before")
    @classmethod
    def _normalize_tasks(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            suites = [normalize_suite(v) if isinstance(v, str) else v for v in value]
            return list(dict.fromkeys(suites))
        return value

    @field_validator("prompts", "baselines")
    @classmethod
    def _check_names(cls, value: list[str]) -> list[str]:
        """Names become file stems and path parts, so they get the same safe charset."""
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        bad = [v for v in value if not _NAME.fullmatch(v)]
        if bad:
            raise ValueError(f"names must be letters, digits, '.', '_' or '-': {bad}")
        return value

    @field_validator("loss_matrix")
    @classmethod
    def _check_loss_matrix(cls, value: LossMatrix) -> LossMatrix:
        if isinstance(value, dict):
            _check_numeric_tree(value, "loss_matrix")
        return value

    @field_validator("prompts")
    @classmethod
    def _no_separator_in_prompts(cls, value: list[str]) -> list[str]:
        bad = [v for v in value if _CONFIG_ID_SEP in v]
        if bad:
            raise ValueError(f"prompt names must not contain {_CONFIG_ID_SEP!r}: {bad}")
        return value

    @field_validator("baselines")
    @classmethod
    def _known_baselines(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in BASELINES]
        if unknown:
            raise ValueError(f"unknown baselines {unknown}; expected some of {sorted(BASELINES)}")
        return value

    @model_validator(mode="after")
    def _check_models(self) -> ExperimentConfig:
        if not self.models and not self.baselines:
            raise ValueError("config needs at least one model or baseline")
        labels = [m.label for m in self.models]
        dupes = sorted({x for x in labels if labels.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate model labels: {', '.join(dupes)}")
        # Distinct labels can still slug to the same config_id ('a/b' vs 'a_b'), and config_ids
        # key the manifest's usage/cost and the output paths, so one would overwrite the other.
        by_slug: dict[str, list[str]] = {}
        for label in labels:
            by_slug.setdefault(slug(label), []).append(label)
        clashes = [group for group in by_slug.values() if len(group) > 1]
        if clashes:
            raise ValueError(f"model labels map to the same config id: {clashes}")
        separated = [label for label in labels if _CONFIG_ID_SEP in slug(label)]
        if separated:
            raise ValueError(
                f"model labels must not contain {_CONFIG_ID_SEP!r} (the config id separator): "
                f"{separated}"
            )
        # Baselines are providers with their own config ids (SPEC §14); a model named like one
        # would share its rows in reports.
        shadowing = [label for label in labels if slug(label) in BASELINES]
        if shadowing:
            raise ValueError(f"model labels must not be baseline names: {shadowing}")
        ids = self.config_ids()
        if len(set(ids)) != len(ids):
            raise ValueError(f"config ids are not unique: {sorted(ids)}")
        for entry in self.models:
            entry.to_spec(self.params)
        return self

    def config_ids(self) -> list[str]:
        """Every (model, prompt) cell's config_id, in matrix order."""
        return [make_config_id(m.label, p, self.mode) for m in self.models for p in self.prompts]

    def resolved_dict(self, *, base: Path | str | None = None) -> dict[str, Any]:
        """JSON-safe dict with every default filled in, for the run manifest.

        Each model entry shows what it actually ran with: the generation settings after the
        experiment ``params`` were applied (``temperature``/``effort`` None means not sent)
        and its resolved ``cost_mode``. Every field is explicit (``repair`` included), so
        loading the dict back gives a config with the same per-model behavior.

        Manifests get published (SPEC §15), so local paths go through
        ``dataset.portable_path``: an absolute ``dataset`` or ``extra.binary``/``extra.path``
        is recorded relative to ``base`` (default: the working directory), else by its last
        component, never with a home directory in it. A config with relative paths (the
        committed kind) round-trips unchanged.
        """
        data = self.model_dump(mode="json")
        data["dataset"] = portable_path(self.dataset, base)
        models: list[dict[str, Any]] = []
        for entry, dumped in zip(self.models, data["models"], strict=True):
            spec = entry.to_spec(self.params)
            extra = dict(dumped["extra"])
            for key in _PATH_EXTRA_KEYS:
                if isinstance(extra.get(key), str):
                    extra[key] = portable_path(extra[key], base)
            models.append(
                dumped
                | {
                    "temperature": spec.temperature,
                    "effort": spec.effort,
                    "max_output_tokens": spec.max_output_tokens,
                    "cost_mode": entry.resolved_cost_mode,
                    "extra": extra,
                }
            )
        data["models"] = models
        return data

    def warnings(self) -> list[str]:
        """Valid but probably unintended settings, for the CLI to print before a run."""
        out: list[str] = []
        for entry in self.models:
            if entry.resolved_cost_mode == "metered" and entry.base_url_env not in (
                None,
                OPENAI_DEFAULT_BASE_URL_ENV,
            ):
                out.append(
                    f"model {entry.label!r} uses a custom base URL ({entry.base_url_env}) and "
                    "is costed as a metered API; if it is self-hosted, set "
                    "cost_mode: unmetered (or a cache_namespace) so it shows $0, not unknown"
                )
            if entry.resolved_cost_mode == "unmetered" and points_at_metered_vendor(
                entry.provider, entry.base_url_env
            ):
                budget = (
                    f" and budget_usd ({self.budget_usd:g}) will not count its spend"
                    if self.budget_usd is not None
                    else ""
                )
                out.append(
                    f"model {entry.label!r} ({entry.provider}) calls a metered API but has "
                    f"cost_mode: unmetered, so its paid calls are recorded as $0{budget}; "
                    "remove cost_mode unless it really is a self-hosted endpoint"
                )
            if (
                self.budget_usd is not None
                and entry.resolved_cost_mode == "reported"
                and not entry.reports_cost
            ):
                out.append(
                    f"model {entry.label!r} ({entry.provider}) reports no cost, so "
                    "budget_usd can't limit its spend"
                )
        if isinstance(self.sample, int) and self.sample == 1:
            out.append(
                "sample: 1 is a count (one family), not 100%; write sample: 1.0 for every case"
            )
        return out


def _check_numeric_tree(node: Any, path: str) -> None:
    """Inline loss matrices are nested mappings whose leaves are numbers."""
    if isinstance(node, dict):
        if not node:
            raise ValueError(f"{path} must not be empty")
        for key, child in node.items():
            _check_numeric_tree(child, f"{path}.{key}")
    elif isinstance(node, bool) or not isinstance(node, int | float):
        raise ValueError(f"{path} must be a number or a mapping of numbers")


def load_config(path: Path | str) -> ExperimentConfig:
    """Load and validate an experiment YAML file.

    Raises ``ConfigError`` for unreadable YAML or a non-mapping document, and pydantic's
    ``ValidationError`` (also a ``ValueError``) for invalid fields.
    """
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: experiment config must be a YAML mapping")
    return ExperimentConfig.model_validate(data)
