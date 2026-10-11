"""The run orchestrator: one experiment config in, one run directory out (SPEC §11, §12.8, §16).

``run_experiment`` expands the matrix (models x prompt variants x mode, plus each baseline
once), loads and samples the cases, and answers every (config, case, sample_idx) cell:

- **Model configs** render a prompt (``render.prompt.build_request``), look the request up in
  the response cache (SPEC §11.4) and otherwise call the provider under the model's
  concurrency limit and the experiment-wide budget. **Baselines** answer through
  ``CaseAwareProvider.decide(case)`` (SPEC §14) and skip tasks they don't do.
- **Tool-use mode** (``mode: tools``, SPEC §8.6) runs each model cell as an agent loop
  (``runner.tooluse``) over a ``render.tools.ToolEnvironment`` for the case's world, which a
  ``render.tools.WorldStore`` rebuilds once per run and checks against the dataset's
  ``worlds.json`` when there is one. Every turn goes through the same cache, budget, recording
  and cost path as a context-mode call, so resume and replay work turn by turn, and the
  prediction carries the case's ``ToolTrace``. A model's ``extra.tool_protocol`` picks the
  protocol when set (``native`` or ``action``: a self-hosted server without tool-call parsing
  still claims native tools); otherwise providers with native tools get them and the others
  (the subscription CLIs) use the JSON action protocol. A replay without that override
  answers whatever was recorded, so it uses the protocol of the recording: the one whose first
  turn of the case the recording holds (``ReplayProvider.has``), an answer or a provider error
  alike. The manifest records each config's protocol (``ConfigManifest.tool_protocol``). An
  ID-renaming twin's environment serves the world under the twin's IDs by the renaming its
  provenance records (``render.tools``), so it needs nothing but the twin.
  Baselines answer from the case as in context mode. Few-shot variants and ``repair`` are
  refused before any call: worked examples are context-mode transcripts, and a tool loop has no
  re-ask defined.
- Every answer goes through ``runner.parse`` exactly once and is appended to
  ``predictions.jsonl`` as soon as it is known (one line, flushed), so an interrupted run loses
  at most the calls in flight and ``resume`` picks up the rest.
- Every model response is also appended to ``responses.jsonl``, keyed by ``replay_key``, so any
  run can be replayed through the ``replay`` provider (CI and re-scoring, SPEC §11.1, §17.3).
  Each record also names its config and whether it was a fresh call, which makes the file the
  run's per-call spend log (a resume charges the budget from it), and what the answer cost
  (``cost_usd``, ``cost_source``). A replay spends nothing, but each prediction it writes
  carries the cost and source its recording gives, as a cache hit carries the cost of the call
  that first produced it, so a replayed metered run's predictions match the original's; a
  recording without costs (older, or made by hand) replays at the replay config's own $0.
- ``manifest.json`` is written before the first call and finalized at the end with usage,
  cost, pricing provenance and counts recomputed from ``predictions.jsonl``, and per-config
  diagnostics (``runner.manifest.ConfigDiagnostics``: CLI calls that ran more internal agent
  turns than the call needed, review items no campaign holds) from ``responses.jsonl`` and the
  predictions;
  they change no score. Budget-skipped cells are listed in ``skipped.jsonl`` and keep
  ``finished_at`` unset: a run the budget stopped is incomplete, and must never pass for a
  finished one.
- The manifest's usage and cost are those of the *answers*: a cache hit carries the cost of
  the call that first produced it, and a call whose cell the budget then skipped (a paid first
  answer before a skipped repair) belongs to no answer. What the run actually *spent* (fresh
  calls only, over every session) is what the budget is checked against; it is
  ``RunResult.spent_usd``, and each call's share is in ``responses.jsonl``.
- A resume continues one measurement or refuses: the dataset, sample, measured settings,
  config identities, code (git SHA, dirty flag, harness version) and price table must all be
  the ones the run started with. The dirty flag ignores what runs create (run directories, the
  response cache), so a tree that doesn't git-ignore them stays resumable. Starting a new run
  instead costs nothing for the cells the cache already holds.
- A resume answers every cell without a prediction and retries every cell whose prediction is
  ``error`` (a transport, auth or provider failure after the provider's own retries, which the
  cache never holds). It drops those error records from ``predictions.jsonl`` before it appends
  anything, so a retried cell is an unanswered one until its new attempt lands, and the file
  holds at most one prediction per (config, case, sample) at every moment: a resume killed
  midway, or a scorer reading a live run, never sees two. ``invalid``, ``refused`` and
  ``over_budget`` are the model's answer and final: SPEC §11.3 allows no content retries.

Run directory (``runs/<run_id>/``)::

    manifest.json       runner.manifest.RunManifest
    experiment.yaml     the resolved experiment config, with the sample actually used
    predictions.jsonl   one schemas.predictions.Prediction per line, at most one per cell
    responses.jsonl     {"key", "response", "config_id", "fresh", "cost_usd", "cost_source"}
                        per line (a replay recording)
    skipped.jsonl       cells the budget stop left unanswered (only while any remain)

Only the session that owns a run directory writes to it: a reader (``load_predictions``) never
rewrites a file, so it can't strand a live writer's append handle on a replaced file. The next
session that reopens the run cuts the torn tail an interrupted write leaves, and drops the error
records it is about to retry, in one atomic rewrite before it appends.

Determinism: cells are scheduled in a fixed order (repeat, then case, then config), every
semaphore wakes its waiters first-in first-out, and ``predictions.jsonl`` is rewritten in
canonical order (config, case, repeat) when the run ends, so a fully cached rerun produces the
same predictions byte for byte apart from the run ID.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol, cast, runtime_checkable

import yaml

from identitybench import __version__
from identitybench.providers.base import (
    CaseAwareProvider,
    GenerationParams,
    ModelRequest,
    ModelResponse,
    Provider,
    Usage,
)
from identitybench.providers.factory import ModelSpec, make_provider
from identitybench.providers.replay import (
    CACHE_KEY_PARAM,
    RecordedCost,
    ReplayProvider,
    recorded_cost_from,
    response_from_dict,
    response_to_dict,
)
from identitybench.render.tools import (
    ID_RENAME_VARIANT,
    ToolProtocol,
    WorldStore,
    build_tool_request,
    tool_prompt_sha256,
    tool_protocol,
)
from identitybench.render.variants import load_variant
from identitybench.runner.cache import DEFAULT_CACHE_ROOT, ResponseCache, cache_key, key_for_request
from identitybench.runner.config import ExperimentConfig, ModelEntry, make_config_id
from identitybench.runner.cost import (
    PRICING_ENV,
    BudgetGuard,
    CacheTtl,
    CostMode,
    CostTracker,
    ModelPrice,
    PricingTable,
    load_pricing,
)
from identitybench.runner.dataset import (
    SampleSpec,
    read_cases,
    stratified_sample,
    subset_for_repeats,
)
from identitybench.runner.manifest import (
    CLI_EXPECTED_TURNS,
    CLI_EXPECTED_TURNS_WITHOUT_SCHEMA,
    ConfigDiagnostics,
    ConfigManifest,
    CostSummary,
    Counts,
    DatasetInfo,
    ParamsRecord,
    PricingRecord,
    RunManifest,
    UsageTotals,
    dataset_info,
    git_info,
    new_run_id,
)
from identitybench.runner.parse import (
    ParseResult,
    answer_text,
    needs_repair,
    parse_response,
    repair_improves,
    repair_request,
    unknown_items,
)
from identitybench.runner.tooluse import RemembersResponses, run_tool_case
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Split, Suite, Task
from identitybench.schemas.outputs import ReviewOutput
from identitybench.schemas.predictions import (
    CostSource,
    Prediction,
    PredictionStatus,
    PredictionUsage,
    ToolTrace,
)

__all__ = [
    "BASELINE_PROMPT",
    "EXPERIMENT_FILE",
    "MANIFEST_FILE",
    "PART_OVERHEAD_TOKENS",
    "PREDICTIONS_FILE",
    "PROMPT_OVERHEAD_TOKENS",
    "RESPONSES_FILE",
    "SKIPPED_FILE",
    "ProgressEvent",
    "PromptRenderer",
    "RunError",
    "RunResult",
    "SkippedWork",
    "TaskScoped",
    "VariantInfo",
    "load_predictions",
    "prompt_token_bound",
    "replay_key",
    "run_experiment",
]

MANIFEST_FILE = "manifest.json"
EXPERIMENT_FILE = "experiment.yaml"
PREDICTIONS_FILE = "predictions.jsonl"
RESPONSES_FILE = "responses.jsonl"
SKIPPED_FILE = "skipped.jsonl"

BASELINE_PROMPT = "none"
"""``ConfigManifest.prompt_variant`` of a baseline: it reads the case, not a prompt."""

PROMPT_OVERHEAD_TOKENS = 1_000
"""Prompt tokens a request may be billed beyond its own text (``prompt_token_bound``): the chat
template, and the preamble a provider adds when tools are offered (a few hundred tokens of
system prompt for Anthropic tool use)."""

PART_OVERHEAD_TOKENS = 16
"""Framing tokens (role markers, separators) each message, tool definition and tool call may
add to a prompt (``prompt_token_bound``)."""

_TASK_SUITE: dict[Task, Suite] = {
    Task.access_request: Suite.requests,
    Task.access_review: Suite.reviews,
    Task.jml: Suite.jml,
}
"""Where few-shot examples for a task come from: base cases, never twins or attacks."""

_RENDER_MODULE = "identitybench.render.prompt"
_BASELINES_MODULE = "identitybench.providers.baselines"

_OPERATIONAL_KEYS = frozenset({"budget_usd", "concurrency"})
_OPERATIONAL_MODEL_KEYS = frozenset({"concurrency", "timeout_s"})
_OPERATIONAL_EXTRA_KEYS = frozenset({"max_retries", "binary"})
"""Settings a resume may change because they can't change an answer: how much to spend, how
hard to push an endpoint, how long and how often to retry it, and where the CLI is installed
(the CLI's version is part of its endpoint identity, which a resume does compare)."""


class RunError(ValueError):
    """The run can't start or continue as configured (nothing was called when this is raised
    before the first call)."""


class VariantInfo(Protocol):
    """The part of a loaded prompt variant the runner reads."""

    @property
    def fewshot(self) -> int:
        """Worked examples ``build_request`` must be given (0 for most variants)."""
        ...


class PromptRenderer(Protocol):
    """What the runner needs from prompt rendering; the ``identitybench.render.prompt`` module
    is one (parameters are positional-only so its own parameter names don't matter)."""

    def build_request(
        self,
        case: Case,
        variant: str,
        /,
        *,
        fewshot: Sequence[Case] = ...,
        params: GenerationParams | None = ...,
    ) -> ModelRequest: ...

    def prompt_sha256(self, variant: str, /) -> str: ...

    def load_variant(self, variant: str, /) -> VariantInfo: ...

    def select_fewshot(
        self, dev_cases: Iterable[Case], task: Task, /, k: int = ..., seed: int = ...
    ) -> Sequence[Case]: ...


@runtime_checkable
class TaskScoped(Protocol):
    """A baseline that only answers some tasks (``noop_plan`` plans JML events and has no
    answer for an access request). Baselines without ``supports`` answer every task."""

    def supports(self, task: Task) -> bool: ...


@dataclass(frozen=True)
class ProgressEvent:
    """One cell finished: answered (``status`` set) or skipped by the budget stop (None)."""

    done: int
    total: int
    config_id: str
    case_id: str
    sample_idx: int
    status: PredictionStatus | None
    cache_hit: bool = False


@dataclass(frozen=True)
class SkippedWork:
    """A cell the budget stop left unanswered. It has no prediction (never INVALID), so a
    ``resume`` with a larger budget answers it. That includes a cell whose ``error``
    prediction the resume dropped to retry it (module docstring)."""

    config_id: str
    case_id: str
    sample_idx: int
    reason: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    manifest: RunManifest
    written: int
    """Predictions written by this call (a resume doesn't count earlier sessions)."""
    resumed: int
    """Earlier sessions' predictions a resume kept (0 for a new run): all but the ``error``
    ones, which it dropped to retry."""
    skipped: tuple[SkippedWork, ...]
    """Cells left unanswered by the budget stop, in schedule order."""
    unsupported: int
    """Baseline cells not scheduled because the baseline doesn't do that case's task."""
    spent_usd: float
    """Fresh spend the budget was checked against, over every session of the run: metered or
    reported cost of calls actually made (cache hits are free). The manifest's cost is the
    answers' cost instead (see the module docstring)."""

    @property
    def complete(self) -> bool:
        """True when every scheduled cell has a prediction (and only then is the manifest's
        ``finished_at`` set)."""
        return not self.skipped


# --------------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------------


def replay_key(label: str, request: ModelRequest, sample_idx: int) -> str:
    """The key a response is recorded under in ``responses.jsonl`` and looked up by through
    the ``replay`` provider (``params.extra["cache_key"]``).

    It can't be the response-cache key: that hashes the provider's endpoint identity, and the
    replay provider's identity hashes the set of keys it holds, so a recording could never
    contain its own lookup keys. Instead the key names the *recorded config* by its label (a
    replay entry's ``model`` is the label of the config it replays) and hashes the rendered
    request and ``sample_idx`` with ``runner.cache``'s canonical hashing.

    Generation params are left out on purpose: a recording answers one rendered prompt, so the
    replay entry needn't restate the recorded model's settings (a pinned codex effort, a
    per-model temperature) to find it.
    """
    return cache_key(
        kind=ReplayProvider.kind,
        model=label,
        endpoint_identity="",
        params={"schema_name": request.schema_name},
        system=request.system,
        messages=[dataclasses.asdict(m) for m in request.messages],
        output_schema=request.output_schema,
        tools=[dataclasses.asdict(t) for t in request.tools],
        sample_idx=sample_idx,
    )


def _with_replay_key(request: ModelRequest, key: str) -> ModelRequest:
    extra = {**request.params.extra, CACHE_KEY_PARAM: key}
    return dataclasses.replace(request, params=dataclasses.replace(request.params, extra=extra))


# --------------------------------------------------------------------------------------------
# Configs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ModelConfig:
    config_id: str
    entry: ModelEntry
    prompt: str
    provider: Provider
    params: GenerationParams
    render: PromptRenderer | None
    """The context-mode renderer; None in tool-use mode, which renders with ``render.tools``."""
    protocol: ToolProtocol | None
    """Tool-use mode: the protocol every case speaks; None in context mode and for a replay
    without an override, whose cases each speak their recording's (``_tool_protocol``)."""
    fewshot: _FewShot | None
    """Worked examples for a few-shot prompt variant; None for the others."""
    semaphore: asyncio.Semaphore
    tracker: CostTracker
    """Fresh spend for the budget (cache hits are free); registered with the ``BudgetGuard``."""
    manifest: ConfigManifest

    def build_request(self, case: Case) -> ModelRequest:
        assert self.render is not None, "only context-mode configs render through a renderer"
        examples = self.fewshot.examples(case) if self.fewshot is not None else ()
        return self.render.build_request(case, self.prompt, fewshot=examples, params=self.params)

    @property
    def tools(self) -> bool:
        """Whether this config runs in tool-use mode."""
        return self.manifest.mode == "tools"

    @property
    def replay(self) -> ReplayProvider | None:
        """The provider when this config replays a recording, else None."""
        return self.provider if isinstance(self.provider, ReplayProvider) else None

    @property
    def cost_mode(self) -> CostMode:
        return self.tracker.mode

    @property
    def price(self) -> ModelPrice | None:
        return self.tracker.price


@dataclass(frozen=True)
class _BaselineConfig:
    config_id: str
    provider: CaseAwareProvider
    semaphore: asyncio.Semaphore
    manifest: ConfigManifest

    def supports(self, task: Task) -> bool:
        return self.provider.supports(task) if isinstance(self.provider, TaskScoped) else True


_Config = _ModelConfig | _BaselineConfig
_Cell = tuple[str, str, int]
"""(config_id, case_id, sample_idx): one prediction's identity within a run."""


def _default_renderer() -> PromptRenderer:
    try:
        return cast(PromptRenderer, importlib.import_module(_RENDER_MODULE))
    except ModuleNotFoundError as exc:
        raise RunError(f"prompt rendering ({_RENDER_MODULE}) is not available: {exc}") from exc


def _default_baseline_factory(dataset_dir: Path) -> Callable[[str], CaseAwareProvider]:
    """``providers.baselines.make_baseline``, with ``surface_probe`` refitted from the frozen
    probe the dataset publishes in ``PROBE.json`` and from its dev split.

    The probe comes from ``tasks.validate.read_probe_spec``, which raises when the file or its
    frozen probe is missing, naming ``identitybench validate --write-probe``; that, and a
    refit that doesn't reproduce the frozen model, is a ``RunError`` before any call. A dataset
    the probe never applies to (no probe suite in both dev and test, such as a fresh release)
    has no frozen probe to publish, so its ``RunError`` says to drop the baseline instead. A
    probe is never trained here: SPEC §9.4 scores the probe frozen and checked when the dataset
    was validated, and one trained at run time would be another, unvalidated predictor under
    the same name.
    """
    try:
        module = importlib.import_module(_BASELINES_MODULE)
    except ModuleNotFoundError as exc:
        raise RunError(f"baselines ({_BASELINES_MODULE}) are not available: {exc}") from exc
    make_baseline = cast(Callable[..., CaseAwareProvider], module.make_baseline)

    def build(name: str) -> CaseAwareProvider:
        if name != "surface_probe":
            return make_baseline(name)
        # Imported here: tasks.validate pulls in the oracle and the dataset generator, which
        # only this baseline needs.
        from identitybench.tasks.generate import PROBE_JSON
        from identitybench.tasks.validate import ProbeFileError, read_probe_spec

        if not (dataset_dir / PROBE_JSON).is_file() and not _probe_applies(dataset_dir):
            raise RunError(
                "surface_probe can't run on this dataset: the shortcut probe is trained on dev "
                "and scored on test, so it applies only where requests or review campaigns are "
                "in both splits (SPEC §9.4), and this dataset has none in both; drop "
                "surface_probe from baselines"
            )
        try:
            frozen = read_probe_spec(dataset_dir)
        except ProbeFileError as exc:
            raise RunError(
                f"surface_probe needs the dataset's frozen shortcut probe: {exc}"
            ) from exc
        dev = read_cases(dataset_dir, split=Split.dev)
        try:
            return make_baseline(name, params=frozen, dev_cases=dev)
        except ValueError as exc:  # the dev split or scikit-learn doesn't refit the frozen probe
            raise RunError(f"surface_probe can't refit the dataset's frozen probe: {exc}") from exc

    return build


def _probe_applies(dataset_dir: Path) -> bool:
    """Whether the shortcut probe applies to the dataset's files: some probe suite has cases
    in both dev, to train on, and test, to be scored on (SPEC §9.4), as ``tasks.validate``
    decides before publishing ``PROBE.json``. Read from the suite files, not the spec in
    ``dataset.json``, which a dataset the runner reads needn't have."""
    from identitybench.tasks.generate import PROBE_SUITES

    present = {
        (case.suite, case.split)
        for case in read_cases(dataset_dir, suites=PROBE_SUITES)
        if case.split in (Split.dev, Split.test)
    }
    return any(
        (suite, Split.dev) in present and (suite, Split.test) in present for suite in PROBE_SUITES
    )


def _repo_pricing(repo_root: Path) -> PricingTable:
    """``$IDENTITYBENCH_PRICING``, else the repo's own ``pricing.yaml``, else the default
    lookup: prices follow the checkout being run, not the shell's working directory."""
    candidate = repo_root / "pricing.yaml"
    if not os.environ.get(PRICING_ENV) and candidate.is_file():
        return load_pricing(candidate)
    return load_pricing()


def _has_directory(value: str) -> bool:
    return os.sep in value or (os.altsep is not None and os.altsep in value)


def _local_spec(spec: ModelSpec, repo_root: Path) -> ModelSpec:
    """``spec`` with its local paths resolved against ``repo_root``, like every other path of
    the run, instead of against the process's working directory.

    A replay ``path`` always names a file. A CLI ``binary`` is resolved only when it has a
    directory part (``bin/claude``): a bare name is a ``PATH`` lookup. Only the provider gets
    the resolved paths; the manifest keeps the configured, portable ones.
    """
    extra = dict(spec.extra)
    path = extra.get("path")
    if isinstance(path, str):
        extra["path"] = str(_under(repo_root, Path(path).expanduser()))
    binary = extra.get("binary")
    if isinstance(binary, str) and _has_directory(binary):
        extra["binary"] = str(_under(repo_root, Path(binary).expanduser()))
    return spec if extra == spec.extra else spec.model_copy(update={"extra": extra})


def _model_configs(
    config: ExperimentConfig,
    *,
    repo_root: Path,
    factory: Callable[[ModelSpec], Provider],
    render: PromptRenderer | None,
    fewshot: Mapping[str, _FewShot],
    guard: BudgetGuard,
) -> list[_ModelConfig]:
    """One config per (model, prompt) in ``config.config_ids()`` order. A model's prompt
    variants share its provider and its concurrency limit, which protects one endpoint.

    The manifest records the parameters that are *sent*: a provider whose capabilities say it
    doesn't take a temperature (models that reject one, every CLI) drops it, and SPEC §12.8
    flags those models from a temperature of None. Requests still carry the configured value,
    so cache keys don't depend on a provider's capability table.

    In tool-use mode ``render`` is None and the prompt hash is
    ``render.tools.tool_prompt_sha256``, which covers the tool-use texts and schemas too. The
    protocol is fixed per model here (``_fixed_protocol``), so a ``native`` override for a
    provider without native tools fails before any call.
    """
    out: list[_ModelConfig] = []
    if config.mode == "tools":
        hashes = {prompt: tool_prompt_sha256(prompt) for prompt in config.prompts}
    else:
        assert render is not None, "context mode renders with a PromptRenderer"
        hashes = {prompt: render.prompt_sha256(prompt) for prompt in config.prompts}
    for entry in config.models:
        spec = entry.to_spec(config.params)
        try:
            provider = factory(_local_spec(spec, repo_root))
        except (OSError, ValueError) as exc:  # e.g. a replay recording that isn't there
            raise RunError(f"model {entry.label!r}: cannot create its provider: {exc}") from exc
        params = spec.generation_params()
        sent = (
            params
            if provider.capabilities().supports_temperature
            else dataclasses.replace(params, temperature=None)
        )
        semaphore = asyncio.Semaphore(entry.concurrency or config.concurrency)
        protocol = _fixed_protocol(entry, provider) if config.mode == "tools" else None
        for prompt in config.prompts:
            config_id = make_config_id(entry.label, prompt, config.mode)
            tracker = guard.track(
                config_id, entry.model, entry.resolved_cost_mode, reports_cost=entry.reports_cost
            )
            manifest = ConfigManifest(
                config_id=config_id,
                label=entry.label,
                provider=entry.provider,
                model=entry.model,
                endpoint_identity=provider.endpoint_identity(),
                params=ParamsRecord.from_params(sent),
                prompt_variant=prompt,
                prompt_sha256=hashes[prompt],
                mode=config.mode,
                tool_protocol=protocol,
            )
            out.append(
                _ModelConfig(
                    config_id=config_id,
                    entry=entry,
                    prompt=prompt,
                    provider=provider,
                    params=params,
                    render=render,
                    protocol=protocol,
                    fewshot=fewshot.get(prompt),
                    semaphore=semaphore,
                    tracker=tracker,
                    manifest=manifest,
                )
            )
    return out


def _fixed_protocol(entry: ModelEntry, provider: Provider) -> ToolProtocol | None:
    """The tool-use protocol of every case of ``entry``: its ``extra.tool_protocol`` override,
    else what the provider supports (``render.tools.tool_protocol``); None for a replay without
    an override, which follows its recording case by case.

    Raises ``RunError`` for a ``native`` override on a provider without native tools: every
    case would fail on its first turn.
    """
    override = entry.tool_protocol
    if override == "native" and not provider.capabilities().native_tools:
        raise RunError(
            f"model {entry.label!r}: extra.tool_protocol is 'native', but {provider.kind} has "
            "no native tools (use 'action')"
        )
    if override is not None:
        return override
    if isinstance(provider, ReplayProvider):
        return None
    return tool_protocol(provider.capabilities())


def _baseline_configs(
    config: ExperimentConfig, factory: Callable[[str], CaseAwareProvider]
) -> list[_BaselineConfig]:
    """Each baseline once, under its own name: it reads the case, so prompts and the mode
    don't change its answer."""
    out: list[_BaselineConfig] = []
    for name in config.baselines:
        provider = factory(name)
        manifest = ConfigManifest(
            config_id=name,
            label=name,
            provider="baseline",
            model=provider.model,
            endpoint_identity=provider.endpoint_identity(),
            # Baselines generate no tokens; 0 records "no limit applies", not a real setting.
            params=ParamsRecord(max_output_tokens=0),
            prompt_variant=BASELINE_PROMPT,
            prompt_sha256="",
            mode=config.mode,
        )
        semaphore = asyncio.Semaphore(config.concurrency)
        out.append(_BaselineConfig(name, provider, semaphore, manifest))
    return out


# --------------------------------------------------------------------------------------------
# Cases and the schedule
# --------------------------------------------------------------------------------------------


def _load_cases(
    config: ExperimentConfig, dataset_dir: Path, sample: SampleSpec | None
) -> list[Case]:
    """The configured split and suites, sampled by family (``dataset.DEFAULT_SAMPLE_UNIT``)."""
    cases = read_cases(dataset_dir, split=config.split, suites=config.tasks)
    if sample is not None:
        cases = stratified_sample(cases, sample, seed=config.seed)
    if not cases:
        raise RunError(f"no {config.split} cases selected from {dataset_dir}")
    dupes = sorted(cid for cid, n in Counter(c.case_id for c in cases).items() if n > 1)
    if dupes:
        raise RunError(f"duplicate case ids in {dataset_dir}: {', '.join(dupes[:5])}")
    return cases


def _repeat_pool(config: ExperimentConfig, cases: list[Case]) -> list[Case]:
    """Cases that get repeats 1..k-1: a stratified ``repeats.subset`` (SPEC §12.8), or all."""
    if config.repeats.k == 1:
        return []
    subset = config.repeats.subset
    if subset is None or subset >= len(cases):
        return cases
    return subset_for_repeats(cases, subset, seed=config.seed)


class _FewShot:
    """Worked examples for one few-shot prompt variant, chosen from ``dev`` (SPEC §11.2,
    §10.5).

    Every case of a task gets the same examples, picked once with the experiment seed, except
    a case from the family of one of them (only possible when the run is on ``dev``): it gets
    a set chosen without its own family, because an example must never show the model the
    evaluated case or its twin with the answer.
    """

    def __init__(
        self, render: PromptRenderer, pool: Sequence[Case], k: int, seed: int, tasks: Iterable[Task]
    ) -> None:
        self._render = render
        self._pool = pool
        self._k = k
        self._seed = seed
        self._chosen: dict[tuple[Task, str | None], tuple[Case, ...]] = {}
        for task in tasks:  # choose up front, so a short dev pool fails before any call
            self._select(task, None)

    def examples(self, case: Case) -> tuple[Case, ...]:
        shared = self._select(case.task, None)
        if all(example.family_id != case.family_id for example in shared):
            return shared
        return self._select(case.task, case.family_id)

    def _select(self, task: Task, excluded_family: str | None) -> tuple[Case, ...]:
        key = (task, excluded_family)
        if key not in self._chosen:
            pool = [c for c in self._pool if c.family_id != excluded_family]
            chosen = self._render.select_fewshot(pool, task, k=self._k, seed=self._seed)
            self._chosen[key] = tuple(chosen)
        return self._chosen[key]


def _fewshot_sources(
    config: ExperimentConfig,
    dataset_dir: Path,
    cases: Sequence[Case],
    render: PromptRenderer,
) -> dict[str, _FewShot]:
    """A ``_FewShot`` per prompt variant that takes worked examples; dev is read only when
    one does. Examples come from the base suites, never from twins or attacks."""
    wanted = {prompt: render.load_variant(prompt).fewshot for prompt in config.prompts}
    wanted = {prompt: k for prompt, k in wanted.items() if k > 0}
    if not wanted:
        return {}
    tasks = sorted({c.task for c in cases})
    dev = read_cases(dataset_dir, split=Split.dev, suites=[_TASK_SUITE[t] for t in tasks])
    return {prompt: _FewShot(render, dev, k, config.seed, tasks) for prompt, k in wanted.items()}


def _schedule(
    configs: Sequence[_Config],
    cases: Sequence[Case],
    repeat_pool: Sequence[Case],
    k: int,
    done: set[_Cell],
) -> tuple[list[tuple[_Config, Case, int]], int]:
    """Cells to run, in order, and how many baseline cells were left out as unsupported.

    Repeat-major then case then config: every case's first answer comes before any repeat,
    and when the budget stops a run every config has covered about the same cases.
    """
    cells: list[tuple[_Config, Case, int]] = []
    unsupported = 0
    for sample_idx in range(k):
        for case in cases if sample_idx == 0 else repeat_pool:
            for cfg in configs:
                if isinstance(cfg, _BaselineConfig) and not cfg.supports(case.task):
                    unsupported += 1
                elif (cfg.config_id, case.case_id, sample_idx) not in done:
                    cells.append((cfg, case, sample_idx))
    return cells, unsupported


# --------------------------------------------------------------------------------------------
# Predictions file
# --------------------------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class _Line[T]:
    """One record of an append-only JSONL file."""

    number: int
    text: str
    """The line as written, newline-terminated."""
    record: T


@dataclass(frozen=True)
class _Appended[T]:
    """What an append-only JSONL file holds: its records, and whether the file is exactly their
    lines (False when its last line is torn, or a complete record missing its newline)."""

    lines: list[_Line[T]]
    clean: bool


def _read_appended[T](path: Path, parse: Callable[[str], T], what: str) -> _Appended[T]:
    """The records of an append-only JSONL file, minus a torn tail. Never writes.

    Lines are split on ``\\n`` only (a rationale may hold U+2028, which ``splitlines`` would
    treat as a break). A line ``parse`` rejects (with ``ValueError``) that ended with a newline
    is real corruption and raises ``RunError``; only the unterminated tail an interrupted
    write leaves is forgiven. An unterminated last line that parses is a complete record whose
    newline didn't make it, and is kept.
    """
    if not path.is_file():
        return _Appended([], True)
    text = path.read_text(encoding="utf-8")
    parts = text.split("\n")
    lines: list[_Line[T]] = []
    for number, line in enumerate(parts, start=1):
        if not line.strip():
            continue
        try:
            record = parse(line)
        except ValueError as exc:
            if number < len(parts):
                raise RunError(f"{path}:{number}: not a {what}: {exc}") from exc
            break
        lines.append(_Line(number, line + "\n", record))
    return _Appended(lines, "".join(line.text for line in lines) == text)


def _rewrite_appended[T](path: Path, appended: _Appended[T], kept: Sequence[_Line[T]]) -> None:
    """Make a run file this session is about to append to hold exactly ``kept`` of the lines it
    held (``appended``), in one atomic replace; nothing is written when that changes nothing.

    That cuts the torn tail an interrupted write left (the next append would otherwise merge
    into the torn line and corrupt it), and lets a resume drop the error predictions it retries.
    Only the session that owns the run directory may do this, before it opens the file for
    appending; readers use ``_read_appended``."""
    if not appended.clean or len(kept) != len(appended.lines):
        _atomic_write_text(path, "".join(line.text for line in kept))


def _predictions_of(
    path: Path, lines: Iterable[_Line[Prediction]], run_id: str | None
) -> list[Prediction]:
    """``lines``' predictions, each checked to be of ``run_id`` (unless it is None) and the only
    record of its (config, case, sample). The runner never writes a second one (a resume drops
    an error record before it retries the cell), so a second one means two sessions wrote the
    run, and which record counts is anyone's guess."""
    out: list[Prediction] = []
    seen: dict[_Cell, int] = {}
    for line in lines:
        prediction = line.record
        if run_id is not None and prediction.run_id != run_id:
            raise RunError(f"{path}:{line.number}: record of run {prediction.run_id}, not {run_id}")
        cell = (prediction.config_id, prediction.case_id, prediction.sample_idx)
        if cell in seen:
            raise RunError(
                f"{path}:{line.number}: second record of {cell[0]} case {cell[1]} sample "
                f"{cell[2]} (first on line {seen[cell]}): did two sessions write this run?"
            )
        seen[cell] = line.number
        out.append(prediction)
    return out


def load_predictions(path: Path | str, run_id: str | None = None) -> list[Prediction]:
    """Read ``predictions.jsonl``, skipping a torn last line (a run interrupted mid-append, or
    one still writing). Read-only: it never rewrites the file, so it is safe while a run is
    appending to it.

    Raises ``RunError`` for a corrupt line, a record of a run other than ``run_id`` (when
    given), or a second record of one (config, case, sample), which the runner never writes."""
    p = Path(path)
    lines = _read_appended(p, Prediction.model_validate_json, "prediction record").lines
    return _predictions_of(p, lines, run_id)


@dataclass(frozen=True)
class _Recording:
    """One ``responses.jsonl`` line. ``key`` and ``response`` are what the replay provider
    reads; ``config_id`` and ``fresh`` (False for a cache hit) make the file a spend log;
    ``cost`` is what the answer cost (``ReplayProvider.recorded_cost`` reads it back), None
    only in a line an older runner wrote."""

    key: str
    config_id: str
    fresh: bool
    response: ModelResponse
    cost: RecordedCost | None

    def to_json(self) -> str:
        record: dict[str, Any] = {
            "key": self.key,
            "config_id": self.config_id,
            "fresh": self.fresh,
            "response": response_to_dict(self.response),
        }
        if self.cost is not None:
            record |= {"cost_usd": self.cost.cost_usd, "cost_source": self.cost.cost_source}
        return json.dumps(record, sort_keys=True, ensure_ascii=False)


def _parse_recording(line: str) -> _Recording:
    data: Any = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    record = cast(dict[str, Any], data)
    key, config_id, fresh, response = (
        record.get(name) for name in ("key", "config_id", "fresh", "response")
    )
    if not (
        isinstance(key, str)
        and isinstance(config_id, str)
        and isinstance(fresh, bool)
        and isinstance(response, dict)
    ):
        raise ValueError("expected string 'key' and 'config_id', bool 'fresh', object 'response'")
    try:
        parsed = response_from_dict(cast(dict[str, Any], response))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed response: {exc!r}") from exc
    return _Recording(key, config_id, fresh, parsed, recorded_cost_from(record))


# --------------------------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------------------------


def prompt_token_bound(request: ModelRequest) -> int:
    """An upper bound on the prompt tokens the texts of ``request`` can be billed for, for the
    budget's pre-call reservation.

    One token per UTF-8 byte of every text the request sends: the system prompt, every
    message (a tool turn's calls and their IDs included), every tool definition and the output
    schema, with JSON in Python's default (ASCII-escaped, spaced) form, the longer of the usual
    serializations. The byte-level tokenizers current models use never put less than one byte
    in a token, so that part can't undercount, where a characters-per-token average can (IDs
    and non-ASCII text run far below the average). ``PROMPT_OVERHEAD_TOKENS`` plus
    ``PART_OVERHEAD_TOKENS`` per message, tool definition and tool call cover what a provider
    wraps around them. Prose is overestimated about fourfold: a reservation mostly comes back,
    and at worst the budget stop leaves that much of the last call's reservation unspent.

    What a provider sends beyond the request's texts isn't here: the Anthropic provider re-sends
    each earlier tool-calling turn of a loop with its original content blocks, thinking
    included, which ``ModelRequest`` doesn't carry. ``_metered_estimate`` adds those
    (``_resent_tokens``, for providers that re-send turn state only).
    """
    texts = [request.system]
    parts = 0
    for message in request.messages:
        parts += 1
        texts += [message.content, message.tool_call_id or ""]
        for call in message.tool_calls:
            parts += 1
            texts += [call.id, call.name, json.dumps(call.arguments)]
    for tool in request.tools:
        parts += 1
        texts += [tool.name, tool.description, json.dumps(tool.input_schema)]
    if request.output_schema is not None:
        texts.append(json.dumps(request.output_schema))
    size = sum(len(text.encode("utf-8")) for text in texts)
    return size + PROMPT_OVERHEAD_TOKENS + PART_OVERHEAD_TOKENS * parts


def _metered_estimate(
    price: ModelPrice, request: ModelRequest, *, ttl: CacheTtl, resent_tokens: int = 0
) -> float:
    """The most one call can cost at the config's price: ``prompt_token_bound`` prompt tokens
    plus ``resent_tokens``, and a full ``max_output_tokens`` reply, with the prompt billed at
    the dearer of the input and cache-write rates.

    ``resent_tokens`` covers what the provider sends that the request's texts don't show: in a
    tool loop with a provider that re-sends turn state, the output of every earlier turn, a
    bound on the content blocks (thinking, text and calls) it re-sends for each
    (``_resent_tokens``); 0 for every other call. A provider that caches the system prompt
    (the Anthropic provider marks it ``cache_control: ephemeral``) bills a cache-writing call's
    cached tokens at the cache-write rate of the config's cache lifetime (``ttl``, the
    tracker's: 1.25x input for five minutes by default); pricing every prompt token at the
    dearer rate bounds any split between the two."""
    prompt = prompt_token_bound(request) + resent_tokens
    reply = request.params.max_output_tokens
    return max(
        price.call_cost(Usage(input_tokens=prompt, output_tokens=reply), ttl),
        price.call_cost(Usage(cache_write_tokens=prompt, output_tokens=reply), ttl),
    )


def _call_cost(
    mode: CostMode, price: ModelPrice | None, response: ModelResponse, ttl: CacheTtl
) -> tuple[float | None, CostSource]:
    """One response's cost as a prediction records it (cache hits included: the cost of an
    answer doesn't depend on where it was served from, which keeps cached reruns identical).

    Cache writes are billed at ``ttl``, the lifetime the config's tracker bills its spend at
    (``CostTracker.cache_ttl``), so a prediction's cost, the manifest's answer cost, the budget's
    spend and the pre-call reservation (``_metered_estimate``) all price them alike."""
    if mode == "unmetered":
        return 0.0, "unmetered"
    if mode == "metered":
        if price is None:
            return None, "unknown"
        return price.call_cost(response.usage, ttl), "metered"
    if response.reported_cost_usd is None:
        return None, "unknown"
    return response.reported_cost_usd, "reported"


def _resent_tokens(provider: Provider, answers: Sequence[_Answer]) -> int:
    """The prompt tokens a tool loop's next turn may cost beyond its request's texts: every
    earlier turn's output for a provider that re-sends turn state (``RemembersResponses``, the
    Anthropic provider: each earlier tool-calling turn goes back with its original content
    blocks, thinking included, which ``ModelRequest`` doesn't carry), nothing for any other.
    Another provider sends only the request's messages, whose assistant text and tool calls
    ``prompt_token_bound`` already counts, so reserving its earlier output (reasoning included)
    would only grow each late turn's reservation and stop a run early near its budget. The
    test is the one ``runner.tooluse.run_tool_case`` applies to the same provider object before
    handing it each response."""
    if not isinstance(provider, RemembersResponses):
        return 0
    return sum(answer.response.usage.output_tokens for answer in answers)


def _summed_cost(answers: Sequence[_Answer]) -> tuple[float | None, CostSource]:
    """The cost of a prediction's calls (a repair's re-ask, a tool loop's turns): their sum
    under the first call's source, or unknown when any one call's cost is."""
    costs = [answer.cost_usd for answer in answers]
    if any(cost is None for cost in costs):
        return None, "unknown"
    return sum(cost or 0.0 for cost in costs), answers[0].cost_source


def _usage_record(usage: Usage, cost: float | None, source: CostSource) -> PredictionUsage:
    return PredictionUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=cost,
        cost_source=source,
    )


def _as_usage(record: PredictionUsage) -> Usage:
    return Usage(
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_tokens=record.cache_read_tokens,
        cache_write_tokens=record.cache_write_tokens,
        reasoning_tokens=record.reasoning_tokens,
    )


def _cost_summary(
    mode: CostMode, priced: bool, predictions: Sequence[Prediction], *, replayed: bool = False
) -> CostSummary:
    """A config's ``CostSummary`` from its predictions' per-call costs (each priced on its
    own prompt, so long-context tiers apply per call as ``cost.cost_usd`` requires).

    Like every manifest cost, it is the cost of the answers: for a replay (``replayed``), what
    its recording says each answer cost (``_recorded_metered``), though the replay itself
    spent nothing (``RunResult.spent_usd``)."""
    metered: float | None
    if replayed:
        metered = _recorded_metered(predictions)
    elif mode == "unmetered":
        metered = 0.0
    elif mode == "metered" and priced:
        metered = sum(p.usage.cost_usd or 0.0 for p in predictions)
    else:
        metered = None
    reported = [
        p.usage.cost_usd
        for p in predictions
        if p.usage.cost_source == "reported" and p.usage.cost_usd is not None
    ]
    return CostSummary(
        cost_mode=mode, metered_api_usd=metered, reported_usd=sum(reported) if reported else None
    )


def _recorded_metered(predictions: Sequence[Prediction]) -> float | None:
    """A replay config's metered API cost: the metered (and real $0 unmetered) costs its
    recording gives its answers. Unknown (None) when any answer's cost is; not applicable
    (None) when every answer's cost was reported by its access path (a CLI's recording)."""
    sources = [p.usage.cost_source for p in predictions]
    if "unknown" in sources or (sources and all(s == "reported" for s in sources)):
        return None
    return sum(p.usage.cost_usd or 0.0 for p in predictions if p.usage.cost_source != "reported")


def _home_patterns() -> list[re.Pattern[str]]:
    """The user's home directory as written and resolved, longest first (one may contain the
    other), each matched only as a whole path component, never as a prefix of a longer name.
    A home of ``/`` is skipped: scrubbing it would mangle every path."""
    home = Path.home()
    spellings = {str(p) for p in (home, home.resolve()) if p.parent != p}
    return [
        re.compile(re.escape(s) + r"(?![\w.-])") for s in sorted(spellings, key=len, reverse=True)
    ]


def _without_home(response: ModelResponse) -> ModelResponse:
    """``response`` with the home directory in its error written as ``~``.

    Providers redact secrets but quote their own failures verbatim (CLI stderr tails, "could
    not start '/Users/<name>/.local/bin/claude'"), and the error lands in run files that get
    published (SPEC §15), where a home directory would name the user.
    """
    if response.error is None:
        return response
    error = response.error
    for pattern in _home_patterns():
        error = pattern.sub("~", error)
    return response if error == response.error else dataclasses.replace(response, error=error)


# --------------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    response: ModelResponse
    cache_hit: bool
    cost_usd: float | None
    cost_source: CostSource


class _Execution:
    """Runs the scheduled cells and appends their predictions and recordings.

    File writes happen between ``await``s, so concurrent cells never interleave within a line.

    The budget (``budget_usd``) is charged per fresh call, never for a cache hit, and covers the
    whole run: ``earlier`` (a resumed run's ``responses.jsonl``) is charged up front. Calls of
    an ``unmetered`` config (self-hosted, replay) can't spend, so the budget never holds them
    back, not even after it has stopped the paid ones. A tool-use turn is one such call.
    """

    def __init__(
        self,
        *,
        run_id: str,
        repair: bool,
        cache: ResponseCache,
        guard: BudgetGuard,
        predictions: IO[str],
        responses: IO[str],
        total: int,
        progress: Callable[[ProgressEvent], None] | None,
        earlier: Iterable[_Recording] = (),
        worlds: WorldStore | None = None,
    ) -> None:
        self._run_id = run_id
        self._repair = repair
        self._cache = cache
        self._guard = guard
        self._worlds = worlds
        """The run's worlds, every one already rebuilt; set exactly when a config uses tools."""
        self._predictions = predictions
        self._responses = responses
        self._total = total
        self._progress = progress
        self._done = 0
        self.written = 0
        self._skipped: list[tuple[int, SkippedWork]] = []
        self._stop_reason: str | None = None
        self._max_reported: dict[str, float] = {}
        """Highest cost each reported-cost config has reported: its per-call estimate."""
        self._calibration = asyncio.Lock()
        """Held by a budgeted call that has no cost estimate yet (see ``_call``)."""
        self._reserving = 0
        """Calls in flight holding a reservation: what a refused call may wait for. Counted,
        not read off ``reserved_usd``, which float rounding can leave a hair above zero."""
        self._released = asyncio.Event()
        """Set, then replaced, each time one of those calls gives its reservation back."""
        for recording in earlier:
            if recording.fresh and recording.config_id in guard.trackers:
                self._charge(recording.config_id, recording.response)

    @property
    def skipped(self) -> tuple[SkippedWork, ...]:
        """Budget-skipped cells in schedule order (they finish in completion order)."""
        return tuple(work for _, work in sorted(self._skipped, key=lambda pair: pair[0]))

    async def run(self, cells: Iterable[tuple[_Config, Case, int]]) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                for position, (cfg, case, sample_idx) in enumerate(cells):
                    group.create_task(self._cell(position, cfg, case, sample_idx))
        except BaseExceptionGroup as failure:
            # One failing cell cancels the rest; surface its own exception, not the group.
            if len(failure.exceptions) == 1:
                raise failure.exceptions[0] from None
            raise

    async def _cell(self, position: int, cfg: _Config, case: Case, sample_idx: int) -> None:
        async with cfg.semaphore:
            if isinstance(cfg, _BaselineConfig):
                prediction = await self._baseline(cfg, case, sample_idx)
            else:
                prediction = await self._model(cfg, case, sample_idx)
        self._done += 1
        if prediction is None:
            assert self._stop_reason is not None, "only the budget stop leaves a cell unanswered"
            work = SkippedWork(cfg.config_id, case.case_id, sample_idx, self._stop_reason)
            self._skipped.append((position, work))
            status, hit = None, False
        else:
            self._predictions.write(prediction.model_dump_json() + "\n")
            self._predictions.flush()
            self.written += 1
            status, hit = prediction.status, prediction.cache_hit
        if self._progress is not None:
            self._progress(
                ProgressEvent(
                    self._done, self._total, cfg.config_id, case.case_id, sample_idx, status, hit
                )
            )

    async def _baseline(self, cfg: _BaselineConfig, case: Case, sample_idx: int) -> Prediction:
        response = _without_home(await cfg.provider.decide(case))
        result = parse_response(case, response)
        return self._prediction(
            cfg.config_id,
            case,
            sample_idx,
            result,
            response,
            usage=_usage_record(response.usage, 0.0, "unmetered"),
            latency_s=response.latency_s,
            cache_hit=False,
            repaired=False,
            raw_text=answer_text(response),
        )

    async def _model(self, cfg: _ModelConfig, case: Case, sample_idx: int) -> Prediction | None:
        """Render, answer (cache or call), parse, and repair once when enabled. None when the
        budget stop prevented a call this cell needed, so the cell is skipped, not INVALID."""
        if cfg.tools:
            return await self._tool_model(cfg, case, sample_idx)
        request = cfg.build_request(case)
        first = await self._answer(cfg, request, sample_idx)
        if first is None:
            return None
        answers = [first]
        final = first.response
        result = parse_response(case, first.response)
        repaired = False
        if self._repair and needs_repair(result):
            second = await self._answer(
                cfg, repair_request(request, first.response, result), sample_idx
            )
            if second is None:
                return None
            answers.append(second)
            retry = parse_response(case, second.response)
            if repair_improves(case, result, retry):
                result, final, repaired = retry, second.response, True
            elif not result.ok:
                reason = f"{result.invalid_reason}; repair failed: {retry.invalid_reason}"
                result = dataclasses.replace(result, invalid_reason=reason)
            # A review whose re-ask settled no more items keeps its first answer as it was.
        usage = Usage()
        for answer in answers:
            usage.add(answer.response.usage)
        cost, source = _summed_cost(answers)
        return self._prediction(
            cfg.config_id,
            case,
            sample_idx,
            result,
            final,
            usage=_usage_record(usage, cost, source),
            latency_s=sum(a.response.latency_s for a in answers),
            cache_hit=all(a.cache_hit for a in answers),
            repaired=repaired,
            raw_text=answer_text(final),
        )

    async def _tool_model(
        self, cfg: _ModelConfig, case: Case, sample_idx: int
    ) -> Prediction | None:
        """One tool-use case (``runner.tooluse``), every turn through ``_answer``: cached,
        budgeted, recorded and costed exactly like a context-mode call. None when the budget
        stop refused a turn; the cell is then skipped, and a resume replays the finished turns
        from the cache."""
        assert self._worlds is not None, "tool-use configs need the run's worlds"
        protocol = self._tool_protocol(cfg, case, sample_idx)
        request = build_tool_request(case, cfg.prompt, params=cfg.params, protocol=protocol)
        answers: list[_Answer] = []

        async def send(turn: ModelRequest) -> ModelResponse | None:
            answer = await self._answer(
                cfg, turn, sample_idx, resent_tokens=_resent_tokens(cfg.provider, answers)
            )
            if answer is None:
                return None
            answers.append(answer)
            return answer.response

        env = self._worlds.environment(case)
        outcome = await run_tool_case(cfg.provider, case, env, request, send=send)
        if outcome is None:
            return None
        cost, source = _summed_cost(answers)
        return self._prediction(
            cfg.config_id,
            case,
            sample_idx,
            outcome.result,
            outcome.final,
            usage=_usage_record(outcome.usage, cost, source),
            latency_s=outcome.latency_s,
            cache_hit=all(a.cache_hit for a in answers),
            repaired=False,
            raw_text=outcome.raw_text,
            tool_trace=outcome.trace,
        )

    def _tool_protocol(self, cfg: _ModelConfig, case: Case, sample_idx: int) -> ToolProtocol:
        """The config's protocol (``_fixed_protocol``), except for a replay without an
        override.

        Such a replay answers whatever was recorded and has no tool capabilities of its own, so
        it must ask the requests the recorded run asked: the first protocol whose first turn
        the recording holds (``ReplayProvider.has``), whether it recorded an answer or a
        provider error (so the error replays as itself, reason and all), native when it holds
        neither (every turn then replays as a miss).
        """
        if cfg.protocol is not None:
            return cfg.protocol
        replay = cfg.replay
        assert replay is not None, "only a replay leaves the protocol to its recording"
        protocols: tuple[ToolProtocol, ...] = ("native", "action")
        for protocol in protocols:
            first = build_tool_request(case, cfg.prompt, params=cfg.params, protocol=protocol)
            if replay.has(replay_key(cfg.entry.model, first, sample_idx)):
                return protocol
        return "native"

    def _prediction(
        self,
        config_id: str,
        case: Case,
        sample_idx: int,
        result: ParseResult,
        response: ModelResponse,
        *,
        usage: PredictionUsage,
        latency_s: float,
        cache_hit: bool,
        repaired: bool,
        raw_text: str | None,
        tool_trace: ToolTrace | None = None,
    ) -> Prediction:
        return Prediction(
            run_id=self._run_id,
            config_id=config_id,
            case_id=case.case_id,
            task=case.task,
            sample_idx=sample_idx,
            status=result.status,
            output=result.output,
            invalid_reason=result.invalid_reason,
            item_errors=result.prediction_item_errors,
            raw_text=raw_text,
            served_model=response.served_model,
            usage=usage,
            latency_s=latency_s,
            cache_hit=cache_hit,
            repaired=repaired,
            tool_trace=tool_trace,
        )

    async def _answer(
        self, cfg: _ModelConfig, request: ModelRequest, sample_idx: int, *, resent_tokens: int = 0
    ) -> _Answer | None:
        """The response to ``request``: from the cache, else from a budgeted call (whose
        reservation counts ``resent_tokens`` more prompt tokens, ``_metered_estimate``).

        Replay providers bypass the cache (a recording already is one, and caching it would
        keep serving an entry after the recording file was edited); they get the recorded
        config's ``replay_key`` instead, and the answer keeps the cost its recording gives
        (``ReplayProvider.recorded_cost``). Provider errors aren't cached, so a resume (which
        retries every cell whose prediction is an error) or a new run calls again;
        refusals and invalid answers are cached, because they are the model's answer, and are
        never retried (SPEC §11.3).
        """
        provider = cfg.provider
        replay = cfg.replay
        key: str | None = None
        response: ModelResponse | None = None
        if replay is None:
            key = key_for_request(
                provider.kind, provider.model, provider.endpoint_identity(), request, sample_idx
            )
            response = self._cache.get_response(key)
        cache_hit = response is not None
        recorded: RecordedCost | None = None
        if response is None:
            to_send = request
            if replay is not None:
                lookup = replay_key(cfg.entry.model, request, sample_idx)
                recorded = replay.recorded_cost(lookup)
                to_send = _with_replay_key(request, lookup)
            response = await self._call(cfg, to_send, resent_tokens)
            if response is None:
                return None
            response = _without_home(response)
            if key is not None and response.error is None:
                self._cache.put_response(key, response)
        if recorded is None:
            cost, source = _call_cost(cfg.cost_mode, cfg.price, response, cfg.tracker.cache_ttl)
            recorded = RecordedCost(cost, source)
        recording = _Recording(
            replay_key(cfg.entry.label, request, sample_idx),
            cfg.config_id,
            not cache_hit,
            response,
            recorded,
        )
        self._responses.write(recording.to_json() + "\n")
        self._responses.flush()
        return _Answer(response, cache_hit, recorded.cost_usd, recorded.cost_source)

    def _estimate(
        self, cfg: _ModelConfig, request: ModelRequest, resent_tokens: int
    ) -> float | None:
        """What a call reserves from the budget before it starts.

        A priced model reserves the most the call can cost (``_metered_estimate``, with the
        ``resent_tokens`` its provider adds to the request), so concurrent calls can't jointly
        overshoot ``budget_usd``. A reported-cost CLI reserves the most it has reported for one
        call so far, and has no estimate (None) before its first report while a budget is set.
        Anything else reserves nothing: there is no budget to enforce (``_build_configs``
        refuses a budget it can't).
        """
        if cfg.price is not None:
            return _metered_estimate(
                cfg.price, request, ttl=cfg.tracker.cache_ttl, resent_tokens=resent_tokens
            )
        if cfg.cost_mode != "reported" or self._guard.budget_usd is None:
            return 0.0
        return self._max_reported.get(cfg.config_id)

    async def _call(
        self, cfg: _ModelConfig, request: ModelRequest, resent_tokens: int
    ) -> ModelResponse | None:
        """Call the provider unless the budget stop says no; None when it does.

        The estimate is reserved before the call and released after the actual cost is
        recorded, so concurrent calls can't jointly overshoot ``budget_usd``. A call with no
        estimate (a reported-cost CLI that hasn't reported a cost yet) would reserve nothing,
        so it starts only while some budget is left, and only while no other such call is in
        flight: the run never has more than one call of unknown cost outstanding, and the
        calls queued behind it reserve the cost it reports. Once the budget stops the run, no
        new paid call starts (cache hits and unmetered calls continue: they cost nothing).
        """
        if cfg.cost_mode == "unmetered":
            response = await cfg.provider.complete(request)
            self._charge(cfg.config_id, response)
            return response
        estimate = self._estimate(cfg, request, resent_tokens)
        if estimate is not None:
            return await self._budgeted(cfg, request, estimate)
        async with self._calibration:
            # The call this one queued behind may have given the config an estimate.
            estimate = self._estimate(cfg, request, resent_tokens)
            if estimate is None:
                return await self._budgeted(cfg, request, None)
        return await self._budgeted(cfg, request, estimate)

    async def _budgeted(
        self, cfg: _ModelConfig, request: ModelRequest, estimate: float | None
    ) -> ModelResponse | None:
        if not await self._may_start(estimate):
            return None
        reserved = estimate or 0.0
        if reserved:
            self._reserving += 1
        try:
            response = await cfg.provider.complete(request)
            self._charge(cfg.config_id, response)
        finally:
            self._guard.release(reserved)
            if reserved:
                self._reserving -= 1
                self._released.set()
                self._released = asyncio.Event()
        return response

    async def _may_start(self, estimate: float | None) -> bool:
        """Reserve ``estimate`` (None: require headroom instead), or set the stop reason.

        A refusal is final only when the call wouldn't fit even if every call in flight gave
        its reservation back unspent, or when none is in flight. Otherwise this call waits for
        them: a reservation is pessimistic (for a priced model ``_metered_estimate``'s bound:
        the request's prompt token bound plus, in a tool loop with a provider that re-sends turn
        state, the earlier turns' output, all at the dearer of the input and cache-write rates,
        plus a full ``max_output_tokens`` reply;
        for a CLI its dearest call so far) and mostly comes back, and stopping on it would end
        a concurrent run with most of its budget unspent.
        """
        guard = self._guard
        while True:
            if self._stop_reason is None:
                unknown = guard.unenforceable()
                if unknown:
                    self._stop_reason = (
                        f"budget_usd can't be enforced: no cost known for {', '.join(unknown)}"
                    )
            if self._stop_reason is not None:
                return False
            if estimate is not None and guard.reserve(estimate):
                return True
            budget = guard.budget_usd
            assert budget is not None, "only a budget refuses a call or leaves it unestimated"
            if estimate is None:
                if guard.spend_usd + guard.reserved_usd < budget:
                    return True
                fits_on_spend = guard.spend_usd < budget
            else:
                fits_on_spend = guard.spend_usd + estimate <= budget
            if not (fits_on_spend and self._reserving):
                needs = "" if estimate is None else f", next call estimated at ${estimate:.4f}"
                self._stop_reason = (
                    f"budget_usd {budget:g} reached (spent ${guard.spend_usd:.4f}{needs})"
                )
                return False
            await self._released.wait()

    def _charge(self, config_id: str, response: ModelResponse) -> None:
        """Add one fresh call to its config's spend, and learn the config's per-call cost.

        A failed call that reports no cost is taken as free rather than as an unknown cost:
        otherwise one CLI timeout would make the budget unenforceable and stop the run.
        """
        reported = response.reported_cost_usd
        if reported is not None:
            self._max_reported[config_id] = max(
                reported, self._max_reported.get(config_id, reported)
            )
        elif response.error is not None:
            reported = 0.0
        self._guard.trackers[config_id].add(response.usage, reported)


# --------------------------------------------------------------------------------------------
# Run directory
# --------------------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _under(root: Path, path: Path | str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else root / p


def _new_run_dir(runs_root: Path, name: str, created_at: datetime) -> tuple[str, Path]:
    """Create ``<timestamp>-<name>``, adding ``-2``, ``-3``... when that run already exists
    (two runs started in the same second must never share a directory)."""
    base = new_run_id(name, created_at.strftime("%Y%m%dT%H%M%SZ"))
    runs_root.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while True:
        run_id = base if attempt == 1 else f"{base}-{attempt}"
        try:
            (runs_root / run_id).mkdir()
        except FileExistsError:
            attempt += 1
        else:
            return run_id, runs_root / run_id


def _measured_settings(experiment: Mapping[str, Any]) -> dict[str, Any]:
    """A resolved experiment dict without the settings that can't change an answer, and
    without ``sample``, which ``_check_resume`` compares as ``RunManifest.sample`` (the sample
    actually used, which ``--sample`` may have set instead of the file)."""
    out = {k: v for k, v in experiment.items() if k not in {*_OPERATIONAL_KEYS, "sample"}}
    models: list[dict[str, Any]] = []
    for entry in cast(list[dict[str, Any]], experiment.get("models", [])):
        kept = {k: v for k, v in entry.items() if k not in _OPERATIONAL_MODEL_KEYS}
        extra = cast(dict[str, Any], entry.get("extra", {}))
        kept["extra"] = {k: v for k, v in extra.items() if k not in _OPERATIONAL_EXTRA_KEYS}
        models.append(kept)
    out["models"] = models
    return out


def _canonical(value: Any) -> str:
    """JSON text that tells ``1`` from ``1.0``, which ``==`` doesn't: as a sample, one is a
    single family and the other every case."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _describe_source(sha: str | None, dirty: bool | None) -> str:
    name = "no git checkout" if sha is None else sha[:12]
    return f"{name} with local edits" if dirty else name


def _check_resume(
    manifest: RunManifest,
    *,
    experiment: dict[str, Any],
    dataset: DatasetInfo,
    sample: SampleSpec | None,
    configs: Sequence[ConfigManifest],
    source: tuple[str | None, bool | None],
    pricing: PricingRecord | None,
) -> None:
    """Refuse to resume into a run that would end up measuring two different things.

    Everything must match except the operational settings (``_OPERATIONAL_KEYS`` and
    friends): raising the budget after a budget stop, or lowering concurrency after rate-limit
    trouble, and resuming is the intended way to finish such a run.

    That includes the code and the prices, which the manifest records once for the whole run.
    The prompt hash leaves the policy text out and relies on the git SHA to pin it, so a new
    SHA, or edits appearing in (or vanishing from) a clean checkout, could change prompts and
    parse rules under the old record. A run that started with local edits recorded that its
    code isn't pinned, so more edits on the same SHA don't make it say anything false. The
    price table must be the same file: costs from two tables can't share one ``pricing``.

    The sample is compared as the one actually used (``sample``: ``--sample``, else the run's
    own, else the file's), never as the file's ``sample`` field: a resume after ``--sample``
    whose file names the same sample, or none, continues the same selection.
    """
    problems: list[str] = []
    if (manifest.git_sha, manifest.git_dirty) != source:
        before = _describe_source(manifest.git_sha, manifest.git_dirty)
        problems.append(f"the code changed (git {before}, now {_describe_source(*source)})")
    if manifest.harness_version != __version__:
        problems.append(
            f"harness version {__version__} differs from the run's {manifest.harness_version}"
        )
    if (
        manifest.pricing is not None
        and pricing is not None
        and manifest.pricing.sha256 != pricing.sha256
    ):
        problems.append("the pricing table changed")
    if manifest.dataset.sha256 != dataset.sha256:
        problems.append("the dataset content changed")
    if _canonical(manifest.sample) != _canonical(sample):
        problems.append(f"sample {sample!r} differs from the run's {manifest.sample!r}")
    old = _measured_settings(manifest.experiment)
    new = _measured_settings(experiment)
    changed = sorted(
        k for k in old.keys() | new.keys() if _canonical(old.get(k)) != _canonical(new.get(k))
    )
    if changed:
        problems.append(f"experiment settings changed: {', '.join(changed)}")
    elif [c.model_dump_json() for c in manifest.configs] != [c.model_dump_json() for c in configs]:
        problems.append(
            "a config's endpoint identity, parameters, prompt hash or tool protocol changed"
        )
    if problems:
        raise RunError(
            f"cannot resume run {manifest.run_id}: {'; '.join(problems)} (start a new run "
            "instead: answers the response cache holds cost nothing)"
        )


_SCHEMA_WITHHELD = "withheld"
"""How a CLI provider's ``raw["json_schema"]`` note starts when it sent the call without the
structured-output flag (``CLI_EXPECTED_TURNS_WITHOUT_SCHEMA``)."""


def _spent_extra_cli_turns(response: ModelResponse) -> bool:
    """Whether a CLI provider reported more internal agent turns for ``response``
    (``raw["num_turns"]``) than the call needed, which depends on whether it sent the
    structured-output flag; False when it reported none (every other provider)."""
    raw = response.raw or {}
    turns = raw.get("num_turns")
    if not isinstance(turns, int) or isinstance(turns, bool):
        return False
    withheld = str(raw.get("json_schema", "")).startswith(_SCHEMA_WITHHELD)
    return turns > (CLI_EXPECTED_TURNS_WITHOUT_SCHEMA if withheld else CLI_EXPECTED_TURNS)


def _diagnostics(
    configs: Sequence[_Config],
    by_config: Mapping[str, Sequence[Prediction]],
    cases: Sequence[Case],
    recordings: Sequence[_Recording],
) -> dict[str, ConfigDiagnostics]:
    """Each config's ``ConfigDiagnostics``, from the run's recorded responses and predictions.

    A resume that asks a request again (a retried error, a tool loop's turns replayed from the
    cache) records it again, so responses are counted once per (config, replay key), the
    latest recording standing for the request."""
    latest: dict[tuple[str, str], ModelResponse] = {}
    for recording in recordings:
        latest[(recording.config_id, recording.key)] = recording.response
    responses: Counter[str] = Counter()
    extra_turns: Counter[str] = Counter()
    for (config_id, _), response in latest.items():
        responses[config_id] += 1
        if _spent_extra_cli_turns(response):
            extra_turns[config_id] += 1
    by_id = {case.case_id: case for case in cases}
    diagnostics: dict[str, ConfigDiagnostics] = {}
    for cfg in configs:
        unknown = sum(
            len(unknown_items(by_id[p.case_id], p.output))
            for p in by_config[cfg.config_id]
            if isinstance(p.output, ReviewOutput) and p.case_id in by_id
        )
        diagnostics[cfg.config_id] = ConfigDiagnostics(
            responses=responses[cfg.config_id],
            cli_extra_turns=extra_turns[cfg.config_id],
            unknown_review_items=unknown,
        )
    return diagnostics


def _finalize(
    run_dir: Path,
    manifest: RunManifest,
    *,
    configs: Sequence[_Config],
    cases: Sequence[Case],
    skipped: Sequence[SkippedWork],
    guard: BudgetGuard,
    repo_root: Path,
    finished_at: datetime | None,
) -> RunManifest:
    """Rewrite ``predictions.jsonl`` in canonical order and recompute the manifest's totals
    and diagnostics (``_diagnostics``).

    Totals come from the predictions file, not from in-memory counters, so they stay right
    across interrupted sessions and resumes. ``calls`` counts model predictions answered by a
    fresh provider call (a repair re-ask or a tool loop's turns belong to their prediction);
    baselines answer in-process and aren't calls. ``invalid`` counts tool-use cases that went
    over budget too: SPEC §8.6 makes them INVALID, and ``Counts`` has no field of its own for
    them, so leaving them out would report a run full of them as having no invalid answers.
    """
    path = run_dir / PREDICTIONS_FILE
    config_rank = {cfg.config_id: i for i, cfg in enumerate(configs)}
    case_rank = {case.case_id: i for i, case in enumerate(cases)}
    predictions = sorted(
        load_predictions(path, manifest.run_id),
        key=lambda p: (
            config_rank.get(p.config_id, len(config_rank)),
            case_rank.get(p.case_id, len(case_rank)),
            p.sample_idx,
            p.config_id,
            p.case_id,
        ),
    )
    _atomic_write_text(path, "".join(p.model_dump_json() + "\n" for p in predictions))

    by_config: dict[str, list[Prediction]] = {cfg.config_id: [] for cfg in configs}
    for prediction in predictions:
        by_config.setdefault(prediction.config_id, []).append(prediction)
    usage: dict[str, UsageTotals] = {}
    cost: dict[str, CostSummary] = {}
    for cfg in configs:
        total = Usage()
        for prediction in by_config[cfg.config_id]:
            total.add(_as_usage(prediction.usage))
        usage[cfg.config_id] = UsageTotals.from_usage(total)
        if isinstance(cfg, _ModelConfig):
            summary = _cost_summary(
                cfg.cost_mode,
                cfg.price is not None,
                by_config[cfg.config_id],
                replayed=cfg.replay is not None,
            )
        else:
            summary = _cost_summary("unmetered", False, by_config[cfg.config_id])
        cost[cfg.config_id] = summary
    model_ids = {cfg.config_id for cfg in configs if isinstance(cfg, _ModelConfig)}
    statuses = [p.status for p in predictions]
    counts = Counts(
        cases=len(cases),
        calls=sum(1 for p in predictions if p.config_id in model_ids and not p.cache_hit),
        cache_hits=sum(1 for p in predictions if p.cache_hit),
        errors=statuses.count(PredictionStatus.error),
        refusals=statuses.count(PredictionStatus.refused),
        invalid=statuses.count(PredictionStatus.invalid)
        + statuses.count(PredictionStatus.over_budget),
    )
    recordings = _read_appended(run_dir / RESPONSES_FILE, _parse_recording, "response record")
    final = manifest.model_copy(
        update={
            "usage": usage,
            "cost": cost,
            "pricing": PricingRecord.from_trackers(guard.trackers.values(), base=repo_root),
            "counts": counts,
            "diagnostics": _diagnostics(
                configs, by_config, cases, [line.record for line in recordings.lines]
            ),
            "finished_at": finished_at,
        }
    )
    final.save(run_dir / MANIFEST_FILE)

    skipped_path = run_dir / SKIPPED_FILE
    if skipped:
        lines = (json.dumps(dataclasses.asdict(s), sort_keys=True) + "\n" for s in skipped)
        _atomic_write_text(skipped_path, "".join(lines))
    else:
        skipped_path.unlink(missing_ok=True)
    return final


def _build_configs(
    config: ExperimentConfig,
    *,
    repo_root: Path,
    dataset_dir: Path,
    cases: Sequence[Case],
    render: PromptRenderer | None,
    provider_factory: Callable[[ModelSpec], Provider] | None,
    baseline_factory: Callable[[str], CaseAwareProvider] | None,
    pricing: PricingTable | None,
) -> tuple[list[_Config], BudgetGuard]:
    """Model configs in matrix order, then baselines, plus the budget guard over them.

    Everything that can fail on a bad setup (an unknown prompt variant, a short dev pool for
    few-shot, a missing frozen probe, a budget that can't be enforced) fails here, before a run
    directory exists or any call is made. The renderer and the pricing file are only loaded
    when a model config needs them.
    """
    if pricing is None and any(m.resolved_cost_mode == "metered" for m in config.models):
        pricing = _repo_pricing(repo_root)
    guard = BudgetGuard(config.budget_usd, table=pricing)
    configs: list[_Config] = []
    if config.models:
        if config.mode == "tools":
            _check_tool_mode(config)
            renderer: PromptRenderer | None = None
            fewshot: dict[str, _FewShot] = {}
        else:
            renderer = render or _default_renderer()
            fewshot = _fewshot_sources(config, dataset_dir, cases, renderer)
        configs.extend(
            _model_configs(
                config,
                repo_root=repo_root,
                factory=provider_factory or make_provider,
                render=renderer,
                fewshot=fewshot,
                guard=guard,
            )
        )
    if config.baselines:
        factory = baseline_factory or _default_baseline_factory(dataset_dir)
        configs.extend(_baseline_configs(config, factory))
    unknown = guard.unenforceable()
    if unknown:
        raise RunError(
            f"budget_usd is set but these configs' spend can't be known: {', '.join(unknown)} "
            "(add a pricing.yaml row, use a provider that reports cost, or drop budget_usd)"
        )
    return configs, guard


def _check_tool_mode(config: ExperimentConfig) -> None:
    """Refuse what tool-use mode can't run, before anything is created or called: worked
    examples (few-shot variants show context-mode transcripts) and ``repair`` (SPEC §11.3's
    re-ask quotes a failed answer back, and a tool loop has no such turn defined)."""
    if config.repair:
        raise RunError("repair is not supported in mode 'tools'")
    with_examples = [prompt for prompt in config.prompts if load_variant(prompt).fewshot]
    if with_examples:
        raise RunError(
            f"prompt variants {with_examples} take worked examples, which mode 'tools' doesn't "
            "support"
        )


def _tool_worlds(store: WorldStore, cases: Sequence[Case]) -> WorldStore:
    """``store`` with every world the cases need rebuilt (and checked against the dataset's
    fingerprints), and every ID-renaming twin checked by building its environment once: a
    world that can't be served, or a twin whose recorded renaming is missing, isn't its own or
    clashes with its world's IDs (``render.tools``), stops the run before any call."""
    for world_id in sorted({case.world_id for case in cases}):
        try:
            store.world(world_id)
        except ValueError as exc:  # render.tools.WorldUnavailableError
            raise RunError(f"mode 'tools' serves facts from each case's world: {exc}") from exc
    for twin in (case for case in cases if case.variant == ID_RENAME_VARIANT):
        try:
            store.environment(twin)
        except ValueError as exc:
            raise RunError(f"mode 'tools' can't serve twin {twin.case_id}: {exc}") from exc
    return store


def _dataset_worlds(dataset_dir: Path) -> WorldStore:
    """A store checked against the dataset's ``worlds.json``, when it has one."""
    try:
        return WorldStore.for_dataset(dataset_dir)
    except ValueError as exc:  # a worlds.json that isn't one
        raise RunError(f"cannot read the dataset's worlds: {exc}") from exc


@dataclass(frozen=True)
class _Reopened:
    """A run directory named by ``resume`` and the manifest its last session left."""

    run_dir: Path
    manifest: RunManifest


def _reopen(runs_root: Path, resume: str) -> _Reopened:
    run_dir = runs_root / resume
    if not (run_dir / MANIFEST_FILE).is_file():
        raise RunError(f"cannot resume {resume}: no {MANIFEST_FILE} in {run_dir}")
    return _Reopened(run_dir, RunManifest.load(run_dir / MANIFEST_FILE))


@dataclass(frozen=True)
class _OpenRun:
    run_dir: Path
    manifest: RunManifest
    previous: list[Prediction]
    """Earlier sessions' predictions this session keeps: all but the ``error`` ones, which it
    dropped from the file to retry them (empty for a new run)."""
    recordings: list[_Recording]
    """Earlier sessions' ``responses.jsonl``: the calls the budget has already paid for."""


def _open_run(
    config: ExperimentConfig,
    *,
    repo_root: Path,
    runs_root: Path,
    dataset: DatasetInfo,
    sample: SampleSpec | None,
    configs: list[ConfigManifest],
    pricing: PricingRecord | None,
    reopened: _Reopened | None,
    refresh: bool,
    created_at: datetime | None,
) -> _OpenRun:
    """Create a run directory (or check and reopen ``reopened``), write its manifest and
    experiment file, and read back what earlier sessions wrote (this session owns the
    directory now, and appends next).

    The experiment file records the sample actually used (``sample``: ``--sample``, or the
    run's own on a resume), so re-running from it selects the same cases. The code's dirty
    flag ignores the run's own outputs (``git_info``'s ``exclude``).

    A reopened run's files lose their torn tails, and ``predictions.jsonl`` its ``error``
    records, which this session retries (module docstring). The manifest is saved first, with
    ``finished_at`` cleared when there are any, so a run is never marked finished while a cell
    lacks its prediction. Otherwise a reopened run keeps its ``finished_at`` here;
    ``run_experiment`` clears it only once it knows there is work left, so resuming a finished
    run doesn't restamp it."""
    experiment = config.resolved_dict(base=repo_root) | {"sample": sample}
    source = git_info(repo_root, exclude=(runs_root, repo_root / DEFAULT_CACHE_ROOT))
    if reopened is None:
        assert created_at is not None
        run_id, run_dir = _new_run_dir(runs_root, config.name, created_at)
        sha, dirty = source
        manifest = RunManifest(
            run_id=run_id,
            created_at=created_at,
            harness_version=__version__,
            git_sha=sha,
            git_dirty=dirty,
            pricing=pricing,
            dataset=dataset,
            split=config.split,
            sample=sample,
            repeats=config.repeats,
            repair=config.repair,
            refresh=refresh,
            configs=configs,
            seeds={"sample": config.seed, "repeats": config.seed, "fewshot": config.seed},
            experiment=experiment,
        )
        _save_run_files(run_dir, manifest, experiment)
        return _OpenRun(run_dir, manifest, [], [])
    run_dir, stored = reopened.run_dir, reopened.manifest
    _check_resume(
        stored,
        experiment=experiment,
        dataset=dataset,
        sample=sample,
        configs=configs,
        source=source,
        pricing=pricing,
    )
    # Read and check both files before writing anything, so a refused resume changes nothing.
    predictions_path = run_dir / PREDICTIONS_FILE
    stored_predictions = _read_appended(
        predictions_path, Prediction.model_validate_json, "prediction record"
    )
    _predictions_of(predictions_path, stored_predictions.lines, stored.run_id)
    responses_path = run_dir / RESPONSES_FILE
    recorded = _read_appended(responses_path, _parse_recording, "response record")
    kept = [
        line
        for line in stored_predictions.lines
        if line.record.status is not PredictionStatus.error
    ]
    update: dict[str, Any] = {"refresh": stored.refresh or refresh, "experiment": experiment}
    if len(kept) < len(stored_predictions.lines):
        update["finished_at"] = None
    manifest = stored.model_copy(update=update)
    _save_run_files(run_dir, manifest, experiment)
    _rewrite_appended(predictions_path, stored_predictions, kept)
    _rewrite_appended(responses_path, recorded, recorded.lines)
    return _OpenRun(
        run_dir,
        manifest,
        [line.record for line in kept],
        [line.record for line in recorded.lines],
    )


def _save_run_files(run_dir: Path, manifest: RunManifest, experiment: dict[str, Any]) -> None:
    """Write the session's manifest and the resolved experiment it runs."""
    manifest.save(run_dir / MANIFEST_FILE)
    (run_dir / EXPERIMENT_FILE).write_text(
        yaml.safe_dump(experiment, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


async def run_experiment(
    config: ExperimentConfig,
    *,
    repo_root: Path | str,
    runs_dir: Path | str = "runs",
    resume: str | None = None,
    refresh: bool = False,
    sample_override: SampleSpec | None = None,
    now: Callable[[], datetime] | None = None,
    render: PromptRenderer | None = None,
    provider_factory: Callable[[ModelSpec], Provider] | None = None,
    baseline_factory: Callable[[str], CaseAwareProvider] | None = None,
    pricing: PricingTable | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
    worlds: WorldStore | None = None,
) -> RunResult:
    """Run (or ``resume``) one experiment and return what was written.

    Paths (``config.dataset``, ``runs_dir``, the ``.cache/responses`` cache) are relative to
    ``repo_root``. ``refresh`` bypasses the response cache (fresh results replace stale
    entries). ``sample_override`` replaces ``config.sample`` (the CLI's ``--sample``); a resume
    without one keeps the sample the run started with (a ``config.sample`` it names must be
    that one), and ``experiment.yaml`` records the sample used. A resume answers the cells left
    unanswered and retries those whose prediction is ``error``; nothing else is asked
    again. ``now`` is the clock for the run ID and timestamps; ``finished_at`` is only set once
    every scheduled cell has a prediction, so a run the budget stopped (or that crashed) stays
    visibly unfinished until a resume completes it. ``render`` (context mode's renderer;
    tool-use mode renders with ``render.tools``), ``provider_factory`` (default
    ``providers.factory.make_provider``), ``baseline_factory``
    (default ``providers.baselines.make_baseline``), ``pricing`` (default: the repo's
    ``pricing.yaml``, loaded only when a config is metered) and ``worlds`` (tool-use mode's
    worlds; default: rebuilt from their IDs and checked against the dataset's ``worlds.json``
    when it has one) are injectable for tests. ``progress`` is called after every cell. Local
    paths in a model's ``extra`` (a replay recording, a CLI binary with a directory part) are
    relative to ``repo_root`` too.

    Raises ``RunError`` before any call for an empty case selection, a provider that can't be
    created (a missing replay recording), a budget that can't be enforced, a resume whose
    settings, code or prices no longer match, or, in tool-use mode, a few-shot variant,
    ``repair``, a ``native`` protocol override for a provider without native tools, a case
    whose world can't be rebuilt or doesn't match the dataset's record, or an ID-renaming twin
    whose recorded renaming is missing or isn't its own.
    """
    root = Path(repo_root)
    clock = now or _utc_now
    runs_root = _under(root, runs_dir)
    reopened = _reopen(runs_root, resume) if resume is not None else None
    if sample_override is not None:
        sample = sample_override
    elif reopened is not None and config.sample is None:
        sample = reopened.manifest.sample
    else:
        # A resume whose file names a sample must name the run's own; _check_resume says so.
        sample = config.sample
    dataset_dir = _under(root, config.dataset)
    cases = _load_cases(config, dataset_dir, sample)
    configs, guard = _build_configs(
        config,
        repo_root=root,
        dataset_dir=dataset_dir,
        cases=cases,
        render=render,
        provider_factory=provider_factory,
        baseline_factory=baseline_factory,
        pricing=pricing,
    )
    tool_worlds: WorldStore | None = None
    if config.mode == "tools" and config.models:
        tool_worlds = _tool_worlds(worlds or _dataset_worlds(dataset_dir), cases)
    opened = _open_run(
        config,
        repo_root=root,
        runs_root=runs_root,
        dataset=dataset_info(dataset_dir, base=root),
        sample=sample,
        configs=[cfg.manifest for cfg in configs],
        pricing=PricingRecord.from_trackers(guard.trackers.values(), base=root),
        reopened=reopened,
        refresh=refresh,
        created_at=_as_utc(clock()) if reopened is None else None,
    )
    run_dir, manifest = opened.run_dir, opened.manifest

    done = {(p.config_id, p.case_id, p.sample_idx) for p in opened.previous}
    cells, unsupported = _schedule(
        configs, cases, _repeat_pool(config, cases), config.repeats.k, done
    )
    if cells and manifest.finished_at is not None:
        # A finished run with work left (lines removed from its predictions file): unfinished
        # until this session completes it.
        manifest = manifest.model_copy(update={"finished_at": None})
        manifest.save(run_dir / MANIFEST_FILE)
    execution: _Execution | None = None
    finished_at = manifest.finished_at  # set only when a resume found nothing left to do
    try:
        with (
            (run_dir / PREDICTIONS_FILE).open("a", encoding="utf-8") as predictions,
            (run_dir / RESPONSES_FILE).open("a", encoding="utf-8") as responses,
        ):
            execution = _Execution(
                run_id=manifest.run_id,
                repair=config.repair,
                cache=ResponseCache(root / DEFAULT_CACHE_ROOT, refresh=refresh),
                guard=guard,
                predictions=predictions,
                responses=responses,
                total=len(cells),
                progress=progress,
                earlier=opened.recordings,
                worlds=tool_worlds,
            )
            await execution.run(cells)
        if not execution.skipped and finished_at is None:
            finished_at = _as_utc(clock())
    finally:
        # Also on failure or cancellation: what was written gets counted, finished_at stays
        # unset, and the manifest is ready for a resume.
        final = _finalize(
            run_dir,
            manifest,
            configs=configs,
            cases=cases,
            skipped=execution.skipped if execution is not None else (),
            guard=guard,
            repo_root=root,
            finished_at=finished_at,
        )
    assert execution is not None
    return RunResult(
        run_id=manifest.run_id,
        run_dir=run_dir,
        manifest=final,
        written=execution.written,
        resumed=len(opened.previous),
        skipped=execution.skipped,
        unsupported=unsupported,
        spent_usd=guard.spend_usd,
    )
