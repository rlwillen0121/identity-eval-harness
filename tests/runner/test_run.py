"""runner.run: matrix expansion, caching, budget stop, resume, repeats, replay and baselines.

Every provider here is a fake (no network, no subprocess); the replay test goes through the real
``ReplayProvider`` via ``make_provider``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import identitybench
import identitybench.runner.run as run_module
from identitybench.providers.base import (
    GenerationParams,
    Message,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from identitybench.providers.factory import ModelSpec
from identitybench.providers.replay import load_responses_jsonl
from identitybench.runner.config import ExperimentConfig, load_config
from identitybench.runner.cost import ModelPrice, PricingTable
from identitybench.runner.dataset import subset_for_repeats, suite_file, write_jsonl
from identitybench.runner.manifest import (
    CLI_EXPECTED_TURNS,
    CLI_EXPECTED_TURNS_WITHOUT_SCHEMA,
    ConfigDiagnostics,
    RunManifest,
)
from identitybench.runner.parse import UNKNOWN_ITEM
from identitybench.runner.run import (
    EXPERIMENT_FILE,
    MANIFEST_FILE,
    PART_OVERHEAD_TOKENS,
    PREDICTIONS_FILE,
    PROMPT_OVERHEAD_TOKENS,
    RESPONSES_FILE,
    SKIPPED_FILE,
    ProgressEvent,
    RunError,
    RunResult,
    load_predictions,
    prompt_token_bound,
    replay_key,
    run_experiment,
)
from identitybench.schemas.case import Case
from identitybench.schemas.enums import RequestDecision, Split, Suite, Task, Tier
from identitybench.schemas.json_schemas import OUTPUT_SCHEMAS, SCHEMA_NAMES
from identitybench.schemas.outputs import RequestOutput, ReviewOutput
from identitybench.schemas.predictions import Prediction, PredictionStatus
from identitybench.scoring.units import decision_units
from identitybench.tasks.generate import PROBE_JSON
from tests.runner._cases import make_case
from tests.runner.test_parse import REQUEST_ANSWER, review_case, review_item

DATASET = "datasets/bench-v1"
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PAID_MODEL = "claude-opus-5-5"
PRICES = PricingTable(
    models={
        PAID_MODEL: ModelPrice(
            input=0.0, output=10_000.0, as_of=date(2026, 10, 1), source="unit-test prices"
        )
    }
)
"""$0.01 per output token: a 10-token answer costs exactly $0.10."""


# --------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------


class Clock:
    """Deterministic clock: each call is one minute after the previous one."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        moment = self.now
        self.now += timedelta(minutes=1)
        return moment


@dataclass(frozen=True)
class Variant:
    fewshot: int


@dataclass
class FakeRenderer:
    """Renders ``case=<id> examples=[<ids>]`` so fakes can tell cases apart."""

    fewshot_k: dict[str, int] = field(default_factory=dict[str, int])
    fewshot_calls: list[tuple[Task, int, int, tuple[str, ...]]] = field(
        default_factory=list[tuple[Task, int, int, tuple[str, ...]]]
    )

    def build_request(
        self,
        case: Case,
        variant: str,
        /,
        *,
        fewshot: Sequence[Case] = (),
        params: GenerationParams | None = None,
    ) -> ModelRequest:
        examples = " ".join(c.case_id for c in fewshot)
        return ModelRequest(
            system=f"policy [{variant}]",
            messages=(Message(Role.user, f"case={case.case_id} examples=[{examples}]"),),
            output_schema=OUTPUT_SCHEMAS[case.task],
            schema_name=SCHEMA_NAMES[case.task],
            params=params or GenerationParams(),
        )

    def prompt_sha256(self, variant: str, /) -> str:
        return hashlib.sha256(variant.encode()).hexdigest()

    def load_variant(self, variant: str, /) -> Variant:
        return Variant(self.fewshot_k.get(variant, 0))

    def select_fewshot(
        self, dev_cases: Iterable[Case], task: Task, /, k: int = 3, seed: int = 0
    ) -> list[Case]:
        pool = sorted((c for c in dev_cases if c.task is task), key=lambda c: c.case_id)
        self.fewshot_calls.append((task, k, seed, tuple(c.case_id for c in pool)))
        return pool[:k]


_CASE = re.compile(r"case=(\S+)")


def stable_hash(text: str) -> int:
    """A per-text number that, unlike ``hash()``, doesn't change with ``PYTHONHASHSEED``."""
    return hashlib.sha256(text.encode()).digest()[0]


def case_of(request: ModelRequest) -> str:
    match = _CASE.search(request.messages[0].content)
    assert match is not None
    return match.group(1)


def answer(text: str | None = None, **kw: Any) -> ModelResponse:
    """A valid request answer unless ``text`` says otherwise."""
    defaults: dict[str, Any] = {
        "text": json.dumps(REQUEST_ANSWER) if text is None else text,
        "usage": Usage(input_tokens=100, output_tokens=10),
        "served_model": "m1-2026-10",
        "latency_s": 0.5,
        "stop_reason": "end_turn",
    }
    return ModelResponse(**(defaults | kw))


class Interrupted(Exception):
    """Stands in for a crash (or Ctrl-C) in the middle of a run."""


@dataclass
class FakeProvider:
    """Answers from ``script(request)``; records every request and the peak concurrency."""

    model: str = "m1"
    script: Callable[[ModelRequest], ModelResponse] = lambda _request: answer()
    identity: str = "fake-endpoint"
    delay: Callable[[ModelRequest], float] = lambda _request: 0.0
    fail_on_call: int | None = None
    supports_temperature: bool = True
    kind: str = "fake"
    calls: list[ModelRequest] = field(default_factory=list[ModelRequest])
    others_in_flight: list[int] = field(default_factory=list[int])
    """For each call, how many other calls were in flight when it started."""
    in_flight: int = 0
    peak: int = 0

    def endpoint_identity(self) -> str:
        return self.identity

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_structured_output=True,
            native_tools=False,
            supports_temperature=self.supports_temperature,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise Interrupted
        self.others_in_flight.append(self.in_flight)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(self.delay(request))
        self.in_flight -= 1
        return self.script(request)

    def called_cases(self) -> list[str]:
        return [case_of(r) for r in self.calls]


@dataclass
class FakeBaseline:
    """A ``CaseAwareProvider`` that answers every task with ESCALATE."""

    model: str
    kind: str = "baseline"
    decided: list[str] = field(default_factory=list[str])

    def endpoint_identity(self) -> str:
        return f"baseline:{self.model}"

    async def decide(self, case: Case) -> ModelResponse:
        self.decided.append(case.case_id)
        if case.task is Task.access_review:
            assert case.view.campaign is not None
            items = [review_item(i.item_id, "ESCALATE") for i in case.view.campaign.items]
            return ModelResponse(parsed={"items": items}, served_model=self.model)
        return ModelResponse(parsed={"decision": "ESCALATE"}, served_model=self.model)


@dataclass
class FailingBaseline(FakeBaseline):
    """A baseline whose every answer is a provider error (``error``)."""

    error: str = "failed"

    async def decide(self, case: Case) -> ModelResponse:
        self.decided.append(case.case_id)
        return ModelResponse(error=self.error)


@dataclass
class ScopedBaseline(FakeBaseline):
    """A task-scoped baseline (``runner.run.TaskScoped``), like ``noop_plan``."""

    tasks: frozenset[Task] = frozenset()

    def supports(self, task: Task) -> bool:
        return task in self.tasks


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def write_dataset(repo: Path, cases: Sequence[Case]) -> None:
    root = repo / DATASET
    groups: dict[Path, list[Case]] = {}
    for case in cases:
        groups.setdefault(suite_file(root, case.suite, case.split), []).append(case)
    for path, group in groups.items():
        write_jsonl(path, group)


def request_cases(n: int, split: Split = Split.test, prefix: str = "c") -> list[Case]:
    tiers = (Tier.L1, Tier.L2, Tier.L3)
    return [make_case(f"{prefix}{i}", split=split, tier=tiers[i % 3]) for i in range(1, n + 1)]


FAKE_MODEL: dict[str, Any] = {
    "provider": "openai_compatible",
    "model": "m1",
    "label": "fake-a",
    "base_url_env": "LOCAL_LLM_BASE_URL",
    "cost_mode": "unmetered",
}
PAID_ENTRY: dict[str, Any] = {"provider": "anthropic", "model": PAID_MODEL, "label": "paid"}


def experiment(**overrides: Any) -> ExperimentConfig:
    data: dict[str, Any] = {
        "name": "unit",
        "dataset": DATASET,
        "tasks": ["requests"],
        "prompts": ["baseline"],
        "models": [FAKE_MODEL],
        "concurrency": 2,
    }
    return ExperimentConfig.model_validate(data | overrides)


async def run(
    repo: Path,
    config: ExperimentConfig,
    providers: FakeProvider | dict[str, FakeProvider],
    *,
    render: FakeRenderer | None = None,
    **kw: Any,
) -> RunResult:
    """``run_experiment`` with a fake renderer, a fixed clock and fake providers (one for
    every model, or a map from model label to fake)."""
    if isinstance(providers, FakeProvider):
        providers = {entry.label: providers for entry in config.models}
    table = providers

    def factory(spec: ModelSpec) -> FakeProvider:
        return table[spec.display_label]

    kw.setdefault("now", Clock())
    return await run_experiment(
        config, repo_root=repo, render=render or FakeRenderer(), provider_factory=factory, **kw
    )


def predictions(result: RunResult) -> list[Prediction]:
    return load_predictions(result.run_dir / PREDICTIONS_FILE)


def comparable(prediction: Prediction, *drop: str) -> dict[str, Any]:
    return prediction.model_dump(mode="json", exclude={"run_id", *drop})


# --------------------------------------------------------------------------------------------
# Layout, manifest, matrix
# --------------------------------------------------------------------------------------------


async def test_run_writes_the_layout_manifest_and_predictions(tmp_path: Path) -> None:
    cases = request_cases(4)
    write_dataset(tmp_path, cases)
    config = experiment(prompts=["baseline", "checklist"], baselines=["always_escalate"])
    provider = FakeProvider()
    events: list[ProgressEvent] = []
    result = await run(
        tmp_path,
        config,
        provider,
        baseline_factory=lambda name: FakeBaseline(name),
        progress=events.append,
    )

    assert result.run_id == "20261010T120000Z-unit"
    assert result.run_dir == tmp_path / "runs" / result.run_id
    for name in (MANIFEST_FILE, EXPERIMENT_FILE, PREDICTIONS_FILE, RESPONSES_FILE):
        assert (result.run_dir / name).is_file()
    assert not (result.run_dir / SKIPPED_FILE).exists()
    assert result.complete and result.written == 12 and result.resumed == 0

    ids = ["fake-a__baseline__context", "fake-a__checklist__context", "always_escalate"]
    rows = predictions(result)
    assert [(p.config_id, p.case_id) for p in rows] == [
        (cid, c.case_id) for cid in ids for c in cases
    ]
    assert all(p.status is PredictionStatus.ok and p.run_id == result.run_id for p in rows)
    assert len(provider.calls) == 8
    assert {r.system for r in provider.calls} == {"policy [baseline]", "policy [checklist]"}

    manifest = RunManifest.load(result.run_dir / MANIFEST_FILE)
    assert manifest == result.manifest
    assert manifest.created_at == T0 and manifest.finished_at == T0 + timedelta(minutes=1)
    assert (manifest.dataset.name, manifest.dataset.version) == ("bench", "v1")
    assert manifest.dataset.path == DATASET
    assert manifest.split is Split.test and manifest.sample is None
    assert not manifest.repair and not manifest.refresh
    assert [c.config_id for c in manifest.configs] == ids
    model_cfg, _, baseline_cfg = manifest.configs
    assert model_cfg.provider == "openai_compatible" and model_cfg.model == "m1"
    assert model_cfg.endpoint_identity == "fake-endpoint"
    assert model_cfg.prompt_sha256 == hashlib.sha256(b"baseline").hexdigest()
    assert model_cfg.params.max_output_tokens == 4096
    assert (baseline_cfg.provider, baseline_cfg.prompt_variant) == ("baseline", "none")
    assert baseline_cfg.endpoint_identity == "baseline:always_escalate"
    assert manifest.seeds == {"sample": 0, "repeats": 0, "fewshot": 0}
    assert manifest.usage[ids[0]].input_tokens == 400
    assert manifest.usage[ids[0]].output_tokens == 40
    assert manifest.usage["always_escalate"].input_tokens == 0
    assert manifest.cost[ids[0]].cost_mode == "unmetered"
    assert manifest.cost[ids[0]].metered_api_usd == 0.0
    assert manifest.pricing is None
    counts = manifest.counts
    assert (counts.cases, counts.calls, counts.cache_hits, counts.errors) == (4, 8, 0, 0)
    assert manifest.experiment == config.resolved_dict(base=tmp_path)

    reloaded = load_config(result.run_dir / EXPERIMENT_FILE)
    assert reloaded.resolved_dict(base=tmp_path) == manifest.experiment

    assert [e.done for e in events] == list(range(1, 13))
    assert {e.total for e in events} == {12}
    assert all(e.status is PredictionStatus.ok for e in events)

    recordings = (result.run_dir / RESPONSES_FILE).read_text().splitlines()
    assert len(recordings) == 8  # model responses only; baselines aren't recorded


async def test_the_manifest_is_complete_before_the_first_call(tmp_path: Path) -> None:
    """A hard kill (SIGKILL, power loss) skips the end-of-run rewrite, so the manifest written
    at the start must already be resumable: configs, pricing and harness version are there,
    and finished_at is unset."""
    write_dataset(tmp_path, request_cases(2))
    seen: list[RunManifest] = []

    def look(request: ModelRequest) -> ModelResponse:
        if not seen:
            (run_dir,) = (tmp_path / "runs").iterdir()
            seen.append(RunManifest.load(run_dir / MANIFEST_FILE))
        return answer()

    provider = FakeProvider(model=PAID_MODEL, script=look)
    result = await run(tmp_path, paid(), {"paid": provider}, pricing=PRICES)
    (at_start,) = seen
    assert at_start.run_id == result.run_id and at_start.finished_at is None
    assert [c.config_id for c in at_start.configs] == ["paid__baseline__context"]
    assert at_start.pricing is not None and at_start.pricing.sha256 == PRICES.sha256
    assert at_start.harness_version == identitybench.__version__
    assert at_start.experiment == result.manifest.experiment


async def test_the_manifest_records_the_temperature_that_is_sent(tmp_path: Path) -> None:
    """SPEC §12.8 flags models that take no temperature from a recorded None; the request
    keeps the configured value (the provider drops it), so cache keys don't change."""
    write_dataset(tmp_path, request_cases(1))
    config = experiment(
        models=[FAKE_MODEL, FAKE_MODEL | {"label": "fake-b"}], params={"temperature": 0}
    )
    takes = FakeProvider()
    drops = FakeProvider(supports_temperature=False, identity="other-endpoint")
    result = await run(tmp_path, config, {"fake-a": takes, "fake-b": drops})
    assert {c.label: c.params.temperature for c in result.manifest.configs} == {
        "fake-a": 0.0,
        "fake-b": None,
    }
    assert takes.calls[0].params.temperature == drops.calls[0].params.temperature == 0.0
    assert result.manifest.experiment["models"][1]["temperature"] == 0.0  # what was configured


async def test_tool_use_mode_needs_worlds_it_can_rebuild(tmp_path: Path) -> None:
    """Tool-use mode serves facts from each case's world, rebuilt from its ID, so a case whose
    world ID isn't one stops the run before any call (``tests/runner/test_tooluse.py`` runs the
    mode end to end)."""
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider()
    with pytest.raises(RunError, match="'w_1' is not a world ID"):
        await run(tmp_path, experiment(mode="tools"), provider)
    assert provider.calls == [] and not (tmp_path / "runs").exists()


async def test_an_empty_selection_is_an_error(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(2))
    with pytest.raises(RunError, match="no test cases selected"):
        await run(tmp_path, experiment(), FakeProvider(), sample_override=0)
    assert not (tmp_path / "runs").exists()


async def test_sample_override_is_applied_and_recorded(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(6))
    result = await run(tmp_path, experiment(sample=0.5), FakeProvider(), sample_override=2)
    assert len({p.case_id for p in predictions(result)}) == 2
    assert result.manifest.sample == 2
    assert result.manifest.counts.cases == 2


async def test_the_experiment_file_records_the_sample_used(tmp_path: Path) -> None:
    """After ``--sample``, runs/<id>/experiment.yaml says the sample that ran, so a new run
    from that file selects the same cases; and a resume whose file now names that same sample
    is the same measurement."""
    write_dataset(tmp_path, request_cases(6))
    first = await run(tmp_path, experiment(), FakeProvider(), sample_override=2)
    saved = load_config(first.run_dir / EXPERIMENT_FILE)
    assert saved.sample == 2 and first.manifest.experiment["sample"] == 2
    again = await run(tmp_path, saved, FakeProvider())
    assert again.manifest.sample == 2
    assert {p.case_id for p in predictions(again)} == {p.case_id for p in predictions(first)}

    resumed = await run(tmp_path, experiment(sample=2), FakeProvider(), resume=first.run_id)
    assert resumed.written == 0 and resumed.resumed == 2


async def test_run_ids_never_collide(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    first = await run(tmp_path, experiment(), FakeProvider())
    second = await run(tmp_path, experiment(), FakeProvider())
    assert second.run_id == f"{first.run_id}-2"


async def test_concurrency_limit_is_shared_by_a_models_prompts(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(6))
    provider = FakeProvider(delay=lambda _r: 0.01)
    config = experiment(
        prompts=["baseline", "checklist"], models=[FAKE_MODEL | {"concurrency": 2}], concurrency=5
    )
    await run(tmp_path, config, provider)
    assert provider.peak == 2

    wide = FakeProvider(delay=lambda _r: 0.01)
    await run(tmp_path, experiment(concurrency=3), wide, refresh=True)
    assert wide.peak == 3


async def test_predictions_are_rewritten_in_canonical_order(tmp_path: Path) -> None:
    """Later cases finish first here; the file still comes out in config/case order."""
    cases = request_cases(5)
    write_dataset(tmp_path, cases)
    provider = FakeProvider(delay=lambda r: 0.05 - 0.01 * int(case_of(r)[1:]))
    result = await run(tmp_path, experiment(concurrency=5), provider)
    assert provider.called_cases() == [c.case_id for c in cases]
    assert [p.case_id for p in predictions(result)] == [c.case_id for c in cases]


# --------------------------------------------------------------------------------------------
# Statuses and caching
# --------------------------------------------------------------------------------------------


def scripted(behaviours: dict[str, ModelResponse]) -> Callable[[ModelRequest], ModelResponse]:
    return lambda request: behaviours.get(case_of(request), answer())


FAILURES = {
    "c1": ModelResponse(error="HTTP 503 after 4 attempts"),
    "c2": answer(None, refused=True, stop_reason="refusal"),
    "c3": answer("Deny. Policy says so."),
}


async def test_errors_refusals_and_invalid_output_are_recorded(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(4))
    result = await run(tmp_path, experiment(), FakeProvider(script=scripted(FAILURES)))
    by_case = {p.case_id: p for p in predictions(result)}
    assert by_case["c1"].status is PredictionStatus.error
    assert by_case["c1"].invalid_reason == "HTTP 503 after 4 attempts"
    assert by_case["c2"].status is PredictionStatus.refused
    assert by_case["c3"].status is PredictionStatus.invalid
    assert by_case["c3"].raw_text == "Deny. Policy says so."
    assert by_case["c3"].invalid_reason == "no JSON object in the response (stop_reason: end_turn)"
    ok = by_case["c4"]
    assert ok.status is PredictionStatus.ok and isinstance(ok.output, RequestOutput)
    assert ok.output.decision is RequestDecision.DENY
    assert ok.served_model == "m1-2026-10" and ok.latency_s == 0.5
    assert ok.usage.input_tokens == 100 and ok.usage.cost_source == "unmetered"
    counts = result.manifest.counts
    assert (counts.errors, counts.refusals, counts.invalid) == (1, 1, 1)


async def test_a_native_answer_that_fails_validation_keeps_its_text(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    bad = {"decision": "MAYBE", "rationale": "Ünsure."}
    provider = FakeProvider(script=lambda _r: ModelResponse(parsed=bad))
    (prediction,) = predictions(await run(tmp_path, experiment(), provider))
    assert prediction.status is PredictionStatus.invalid
    assert prediction.raw_text == json.dumps(bad, sort_keys=True, ensure_ascii=False)


async def test_home_directories_never_reach_run_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Providers quote their own failures verbatim; a home directory would name the user."""
    home = "/Users/alice"
    monkeypatch.setenv("HOME", home)
    write_dataset(tmp_path, request_cases(1))
    message = f"claude_cli: could not start '{home}/.local/bin/claude' (see {home}2/log, {home})"
    provider = FakeProvider(script=lambda _r: ModelResponse(error=message))
    result = await run(tmp_path, experiment(), provider)
    (prediction,) = predictions(result)
    # Another user's home that merely starts with the same characters is left alone.
    expected = "claude_cli: could not start '~/.local/bin/claude' (see /Users/alice2/log, ~)"
    assert prediction.invalid_reason == expected
    (recording,) = load_responses_jsonl(result.run_dir / RESPONSES_FILE).values()
    assert recording.error == expected


async def test_home_directories_never_reach_baseline_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = "/Users/alice"
    monkeypatch.setenv("HOME", home)
    write_dataset(tmp_path, request_cases(1))
    baseline = FailingBaseline("oracle", error=f"oracle: cannot read {home}/bench/cases.jsonl")
    result = await run_experiment(
        experiment(models=[], baselines=["oracle"]),
        repo_root=tmp_path,
        baseline_factory=lambda _n: baseline,
        now=Clock(),
    )
    (prediction,) = predictions(result)
    assert prediction.status is PredictionStatus.error
    assert prediction.invalid_reason == "oracle: cannot read ~/bench/cases.jsonl"


async def test_a_second_run_is_served_from_the_cache_except_errors(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(4))
    provider = FakeProvider(script=scripted(FAILURES))
    first = await run(tmp_path, experiment(), provider)
    assert len(provider.calls) == 4

    second = await run(tmp_path, experiment(), provider)
    # Errors aren't cached (a rerun retries them); refusals and invalid answers are.
    assert provider.called_cases()[4:] == ["c1"]
    rows = {p.case_id: p for p in predictions(second)}
    assert [rows[c].cache_hit for c in ("c1", "c2", "c3", "c4")] == [False, True, True, True]
    for old in predictions(first):
        assert comparable(old, "cache_hit") == comparable(rows[old.case_id], "cache_hit")
    assert second.manifest.counts.calls == 1 and second.manifest.counts.cache_hits == 3
    assert second.manifest.usage == first.manifest.usage


async def test_refresh_bypasses_the_cache(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(3))
    provider = FakeProvider()
    await run(tmp_path, experiment(), provider)
    result = await run(tmp_path, experiment(), provider, refresh=True)
    assert len(provider.calls) == 6
    assert not any(p.cache_hit for p in predictions(result))
    assert result.manifest.refresh


async def test_cached_reruns_write_byte_identical_predictions(tmp_path: Path) -> None:
    write_dataset(tmp_path, [*request_cases(5), review_case("r1")])
    config = experiment(tasks=["requests", "reviews"], concurrency=4)
    provider = FakeProvider(delay=lambda r: 0.001 * (stable_hash(case_of(r)) % 5))
    await run(tmp_path, config, provider)
    second = await run(tmp_path, config, provider)
    third = await run(tmp_path, config, provider)
    text = [
        (r.run_dir / PREDICTIONS_FILE).read_text().replace(r.run_id, "RUN") for r in (second, third)
    ]
    assert text[0] == text[1]
    assert second.manifest.usage == third.manifest.usage
    assert second.manifest.counts == third.manifest.counts


async def test_review_answers_keep_duplicates_and_item_errors(tmp_path: Path) -> None:
    write_dataset(tmp_path, [review_case("r1")])
    items = [review_item("rv_1"), review_item("rv_1"), review_item("rv_2", "MAYBE")]
    provider = FakeProvider(script=lambda _r: answer(json.dumps({"items": items})))
    result = await run(tmp_path, experiment(tasks=["reviews"]), provider)
    (prediction,) = predictions(result)
    assert prediction.task is Task.access_review
    assert prediction.status is PredictionStatus.ok
    assert isinstance(prediction.output, ReviewOutput)
    assert [i.item_id for i in prediction.output.items] == ["rv_1", "rv_1"]
    assert list(prediction.item_errors) == ["rv_2"]


async def test_an_invented_review_item_is_noted_counted_and_never_scored(tmp_path: Path) -> None:
    """The pilot's model added ``rv_11909_placeholder`` to a campaign answer. The prediction
    keeps it as written and notes it in ``item_errors``, the manifest counts it, and the
    scorer's units are those of the same answer without it."""
    case = review_case("r1")
    write_dataset(tmp_path, [case])
    items = [review_item(i) for i in ("rv_1", "rv_2", "rv_11909_placeholder", "rv_3")]
    provider = FakeProvider(script=lambda _r: answer(json.dumps({"items": items})))
    result = await run(tmp_path, experiment(tasks=["reviews"]), provider)
    (prediction,) = predictions(result)
    assert isinstance(prediction.output, ReviewOutput)
    assert len(prediction.output.items) == 4
    assert prediction.item_errors == {"rv_11909_placeholder": UNKNOWN_ITEM}
    (config_id,) = result.manifest.diagnostics
    assert result.manifest.diagnostics[config_id].unknown_review_items == 1
    honest = prediction.model_copy(
        update={
            "output": ReviewOutput.model_validate({"items": [items[0], items[1], items[3]]}),
            "item_errors": {},
        }
    )
    assert decision_units(case, prediction) == decision_units(case, honest)


def turns(n: int | None) -> ModelResponse:
    """A valid answer from a CLI that reports ``n`` internal agent turns (None: no report)."""
    return answer(raw=None if n is None else {"num_turns": n, "session_id": "s"})


async def test_cli_internal_turns_are_counted_per_config_across_resumes(tmp_path: Path) -> None:
    """A CLI that ran more than two agent turns inside one call (in tool-use mode, a model
    trying to call the lookups natively) is counted in the manifest's diagnostics, once per
    recorded request: a resume that retries a failed call counts its new answer instead."""
    write_dataset(tmp_path, request_cases(4))
    first_turns = {"c1": ModelResponse(error="HTTP 503"), "c2": turns(2), "c3": turns(5)}
    config = experiment(concurrency=1, baselines=["always_deny"])
    first = await run(tmp_path, config, FakeProvider(script=scripted(first_turns)))
    model_id = first.manifest.configs[0].config_id
    assert first.manifest.diagnostics == {
        model_id: ConfigDiagnostics(responses=4, cli_extra_turns=1),
        "always_deny": ConfigDiagnostics(),
    }
    retried = FakeProvider(script=scripted({"c1": turns(3), "c4": turns(9)}))
    later = Clock(T0 + timedelta(hours=1))
    resumed = await run(tmp_path, config, retried, resume=first.run_id, now=later)
    assert retried.called_cases() == ["c1"]
    lines = (resumed.run_dir / RESPONSES_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5  # the failed call's recording stays; its retry is the one counted
    assert resumed.manifest.diagnostics[model_id] == ConfigDiagnostics(
        responses=4, cli_extra_turns=2
    )
    assert (CLI_EXPECTED_TURNS, CLI_EXPECTED_TURNS_WITHOUT_SCHEMA) == (2, 1)


# --------------------------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------------------------


def invalid_then(second: ModelResponse) -> Callable[[ModelRequest], ModelResponse]:
    return lambda request: answer("I deny it.") if len(request.messages) == 1 else second


async def test_repair_reasks_once_and_marks_the_prediction(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider(script=invalid_then(answer(latency_s=0.25)))
    result = await run(tmp_path, experiment(repair=True), provider)
    (prediction,) = predictions(result)
    assert prediction.status is PredictionStatus.ok and prediction.repaired
    assert prediction.raw_text == json.dumps(REQUEST_ANSWER)
    assert prediction.usage.input_tokens == 200 and prediction.usage.output_tokens == 20
    assert prediction.latency_s == 0.75
    assert len(provider.calls) == 2
    reask = provider.calls[1].messages
    assert reask[1] == Message(Role.assistant, "I deny it.")
    assert "could not be used" in reask[2].content
    assert result.manifest.repair
    assert len((result.run_dir / RESPONSES_FILE).read_text().splitlines()) == 2

    cached = await run(tmp_path, experiment(repair=True), provider)
    (again,) = predictions(cached)
    assert again.cache_hit and len(provider.calls) == 2
    assert comparable(again, "cache_hit") == comparable(prediction, "cache_hit")


async def test_a_failed_repair_stays_invalid(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider(script=invalid_then(answer("Still no JSON.")))
    result = await run(tmp_path, experiment(repair=True), provider)
    (prediction,) = predictions(result)
    assert prediction.status is PredictionStatus.invalid and not prediction.repaired
    assert prediction.raw_text == "I deny it."
    assert prediction.invalid_reason is not None
    assert "; repair failed: no JSON object" in prediction.invalid_reason
    assert prediction.usage.output_tokens == 20


async def test_no_repair_by_default_and_never_for_refusals(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(2))
    behaviours = {"c1": answer("I deny it."), "c2": answer(None, refused=True)}
    provider = FakeProvider(script=scripted(behaviours))
    result = await run(tmp_path, experiment(), provider)
    assert [p.status for p in predictions(result)] == [
        PredictionStatus.invalid,
        PredictionStatus.refused,
    ]
    assert len(provider.calls) == 2

    repairing = FakeProvider(script=scripted({"c2": answer(None, refused=True)}))
    await run(tmp_path, experiment(repair=True), repairing, refresh=True)
    assert len(repairing.calls) == 2  # a refusal is never re-asked


def review_answer(*items: dict[str, Any]) -> ModelResponse:
    return answer(json.dumps({"items": list(items)}))


LOST_ITEM = review_answer(review_item("rv_1", "APPROVE"), review_item("rv_2"), review_item("rv_3"))
"""A review of ``review_case``'s three items whose first item fails validation (dropped)."""


async def test_repair_reasks_a_review_that_lost_items(tmp_path: Path) -> None:
    """A dropped item would be scored INVALID: that is the failed validation repair is for,
    even though the answer as a whole parsed."""
    write_dataset(tmp_path, [review_case("r1")])
    fixed = review_answer(*(review_item(i) for i in ("rv_1", "rv_2", "rv_3")))
    provider = FakeProvider(script=lambda r: LOST_ITEM if len(r.messages) == 1 else fixed)
    result = await run(tmp_path, experiment(tasks=["reviews"], repair=True), provider)
    (prediction,) = predictions(result)
    assert prediction.status is PredictionStatus.ok and prediction.repaired
    assert isinstance(prediction.output, ReviewOutput)
    assert [i.item_id for i in prediction.output.items] == ["rv_1", "rv_2", "rv_3"]
    assert prediction.item_errors == {}
    assert len(provider.calls) == 2
    assert "dropped: rv_1 (decision: Input should be" in provider.calls[1].messages[-1].content


async def test_a_review_repair_that_settles_no_more_items_keeps_the_first_answer(
    tmp_path: Path,
) -> None:
    write_dataset(tmp_path, [review_case("r1")])
    narrower = review_answer(review_item("rv_1"))  # fixes rv_1 but leaves out rv_2 and rv_3
    provider = FakeProvider(script=lambda r: LOST_ITEM if len(r.messages) == 1 else narrower)
    result = await run(tmp_path, experiment(tasks=["reviews"], repair=True), provider)
    (prediction,) = predictions(result)
    assert prediction.status is PredictionStatus.ok and not prediction.repaired
    assert isinstance(prediction.output, ReviewOutput)
    assert [i.item_id for i in prediction.output.items] == ["rv_2", "rv_3"]
    assert list(prediction.item_errors) == ["rv_1"] and prediction.invalid_reason is None
    assert prediction.usage.output_tokens == 20  # both calls were made, and paid for


# --------------------------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------------------------


def paid(**overrides: Any) -> ExperimentConfig:
    """One priced model whose 10-token replies reserve (and by default cost) $0.10 a call."""
    defaults: dict[str, Any] = {
        "models": [PAID_ENTRY],
        "params": {"max_output_tokens": 10},
        "concurrency": 1,
    }
    return experiment(**(defaults | overrides))


async def test_budget_stop_skips_cells_and_resume_finishes_them(tmp_path: Path) -> None:
    cases = request_cases(5)
    write_dataset(tmp_path, cases)
    provider = FakeProvider(model=PAID_MODEL)
    providers = {"paid": provider}
    result = await run(tmp_path, paid(budget_usd=0.25), providers, pricing=PRICES)

    # Each call reserves and costs $0.10: two fit under $0.25, the third would not.
    rows = predictions(result)
    assert [p.case_id for p in rows] == ["c1", "c2"]
    assert all(p.status is PredictionStatus.ok for p in rows)
    assert rows[0].usage.cost_usd == pytest.approx(0.10)
    assert rows[0].usage.cost_source == "metered"
    assert not result.complete
    assert [s.case_id for s in result.skipped] == ["c3", "c4", "c5"]
    assert all("budget_usd 0.25 reached" in s.reason for s in result.skipped)
    skipped_lines = (result.run_dir / SKIPPED_FILE).read_text().splitlines()
    assert [json.loads(line)["case_id"] for line in skipped_lines] == ["c3", "c4", "c5"]
    assert result.manifest.finished_at is None  # incomplete, so never marked finished
    assert result.manifest.cost["paid__baseline__context"].metered_api_usd == pytest.approx(0.2)
    assert result.spent_usd == pytest.approx(0.2)
    pricing = result.manifest.pricing
    assert pricing is not None and pricing.path is None and pricing.sha256 == PRICES.sha256
    assert pricing.rows[PAID_MODEL].source == "unit-test prices"

    # The budget covers the whole run: $0.20 is already spent, so $0.45 buys two more calls.
    more = await run(
        tmp_path, paid(budget_usd=0.45), providers, pricing=PRICES, resume=result.run_id
    )
    assert more.resumed == 2 and more.written == 2
    assert [s.case_id for s in more.skipped] == ["c5"]
    assert more.spent_usd == pytest.approx(0.4)  # every session's fresh calls

    done = await run(
        tmp_path, paid(budget_usd=1.0), providers, pricing=PRICES, resume=result.run_id
    )
    assert done.complete and not (done.run_dir / SKIPPED_FILE).exists()
    assert done.manifest.finished_at is not None
    assert [p.case_id for p in predictions(done)] == [c.case_id for c in cases]
    assert provider.called_cases() == [c.case_id for c in cases]
    assert done.manifest.cost["paid__baseline__context"].metered_api_usd == pytest.approx(0.5)
    assert done.manifest.experiment["budget_usd"] == 1.0


async def test_a_budget_stop_before_the_repair_skips_the_whole_cell(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider(model=PAID_MODEL, script=invalid_then(answer()))
    providers = {"paid": provider}
    config = paid(budget_usd=0.15, repair=True)
    result = await run(tmp_path, config, providers, pricing=PRICES)
    assert predictions(result) == []  # skipped, never INVALID
    assert [s.case_id for s in result.skipped] == ["c1"]

    resumed = await run(
        tmp_path,
        paid(budget_usd=1.0, repair=True),
        providers,
        pricing=PRICES,
        resume=result.run_id,
    )
    (prediction,) = predictions(resumed)
    assert prediction.repaired and not prediction.cache_hit
    assert len(provider.calls) == 2  # the invalid first answer came from the cache


async def test_cache_hits_never_spend_budget(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(3))
    providers = {"paid": FakeProvider(model=PAID_MODEL)}
    await run(tmp_path, paid(), providers, pricing=PRICES)
    result = await run(tmp_path, paid(budget_usd=0.0), providers, pricing=PRICES)
    assert result.complete and result.spent_usd == 0.0
    assert all(p.cache_hit for p in predictions(result))
    # The answers' cost is the same whether or not they were served from the cache.
    assert result.manifest.cost["paid__baseline__context"].metered_api_usd == pytest.approx(0.3)


async def test_an_unenforceable_budget_is_refused_before_any_call(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider(model="claude-unpriced")
    config = experiment(models=[PAID_ENTRY | {"model": "claude-unpriced"}], budget_usd=5.0)
    with pytest.raises(RunError, match="spend can't be known: paid__baseline__context"):
        await run(tmp_path, config, {"paid": provider}, pricing=PRICES)
    assert provider.calls == [] and not (tmp_path / "runs").exists()


async def test_a_cli_that_stops_reporting_cost_stops_the_budgeted_run(tmp_path: Path) -> None:
    """A failed call without a cost is taken as free; a successful one makes spend unknown."""
    write_dataset(tmp_path, request_cases(4))
    behaviours = {
        "c1": ModelResponse(error="timeout"),
        "c2": answer(reported_cost_usd=0.01),
        "c3": answer(),
    }
    provider = FakeProvider(script=scripted(behaviours))
    config = experiment(
        models=[{"provider": "claude_cli", "model": "sonnet", "label": "cli"}],
        budget_usd=1.0,
        concurrency=1,
    )
    result = await run(tmp_path, config, {"cli": provider})
    assert [p.case_id for p in predictions(result)] == ["c1", "c2", "c3"]
    by_case = {p.case_id: p for p in predictions(result)}
    assert by_case["c2"].usage.cost_usd == 0.01 and by_case["c2"].usage.cost_source == "reported"
    assert by_case["c3"].usage.cost_usd is None and by_case["c3"].usage.cost_source == "unknown"
    (skip,) = result.skipped
    assert skip.case_id == "c4" and "can't be enforced" in skip.reason
    assert result.manifest.cost["cli__baseline__context"].reported_usd == 0.01

    # A resume re-reads the earlier calls: c3's unknown cost still blocks paid calls, the
    # retry of c1's failure among them.
    resumed = await run(tmp_path, config, {"cli": provider}, resume=result.run_id)
    assert [s.case_id for s in resumed.skipped] == ["c1", "c4"] and len(provider.calls) == 3


async def test_a_budget_stop_leaves_the_run_unfinished_even_with_baselines(
    tmp_path: Path,
) -> None:
    """Free baselines answer every case; the model cells the budget stopped still make the run
    incomplete, so it can never pass for a finished one."""
    cases = request_cases(4)
    write_dataset(tmp_path, cases)
    baseline = FakeBaseline("always_escalate")
    result = await run(
        tmp_path,
        paid(budget_usd=0.15, baselines=["always_escalate"]),
        {"paid": FakeProvider(model=PAID_MODEL)},
        pricing=PRICES,
        baseline_factory=lambda _n: baseline,
    )
    assert baseline.decided == [c.case_id for c in cases]
    assert [(s.config_id, s.case_id) for s in result.skipped] == [
        ("paid__baseline__context", c) for c in ("c2", "c3", "c4")
    ]
    assert not result.complete and result.manifest.finished_at is None
    assert RunManifest.load(result.run_dir / MANIFEST_FILE).finished_at is None
    assert len((result.run_dir / SKIPPED_FILE).read_text().splitlines()) == 3


async def test_a_concurrent_budget_waits_for_reservations_instead_of_stopping(
    tmp_path: Path,
) -> None:
    """Every call reserves a full 10-token reply ($0.10) but uses one token ($0.01). Calls in
    flight give most of their reservation back, so a refused call waits for them; the run stops
    only when spend alone leaves no room for one more reservation ($0.35 - $0.10)."""
    write_dataset(tmp_path, request_cases(40))
    provider = FakeProvider(
        model=PAID_MODEL,
        script=lambda _r: answer(usage=Usage(input_tokens=0, output_tokens=1)),
        delay=lambda r: 0.001 * (stable_hash(case_of(r)) % 4),
    )
    config = paid(budget_usd=0.35, concurrency=4)
    result = await run(tmp_path, config, {"paid": provider}, pricing=PRICES)
    rows = predictions(result)
    spent = sum(p.usage.cost_usd or 0.0 for p in rows)
    assert provider.peak >= 2
    assert 0.25 - 1e-9 <= spent <= 0.35 and result.spent_usd == pytest.approx(spent)
    assert len(rows) == len(provider.calls) and 25 <= len(rows) <= 28
    assert len(result.skipped) == 40 - len(rows)
    assert all("budget_usd 0.35 reached" in s.reason for s in result.skipped)


def test_the_prompt_token_bound_counts_every_byte_sent() -> None:
    """One token per UTF-8 byte of every text a request sends (tool turns' calls and tool
    definitions included), plus the providers' framing: a bound, never an average."""
    plain = ModelRequest(system="sys", messages=(Message(Role.user, "héllo"),))
    assert prompt_token_bound(plain) == 3 + 6 + PROMPT_OVERHEAD_TOKENS + PART_OVERHEAD_TOKENS
    call = ToolCall("c1", "get_identity", {"identity_id": "u_1"})
    turn = ModelRequest(
        system="sys",
        messages=(
            Message(Role.user, "u"),
            Message(Role.assistant, "", tool_calls=(call,)),
            Message(Role.tool, "{}", tool_call_id="c1"),
        ),
        tools=(ToolSpec("get_identity", "Look one up.", {"type": "object"}),),
        output_schema={"type": "object"},
    )
    schema = json.dumps({"type": "object"})
    messages = ("u", "", "c1", "get_identity", json.dumps(call.arguments), "{}", "c1")
    texts = ("sys", *messages, "get_identity", "Look one up.", schema, schema)
    framing = PROMPT_OVERHEAD_TOKENS + PART_OVERHEAD_TOKENS * 5  # 3 messages, 1 call, 1 tool
    assert prompt_token_bound(turn) == sum(len(s.encode()) for s in texts) + framing


def price_row(input_usd: float, output_usd: float, cache_write_usd: float | None) -> PricingTable:
    """``PAID_MODEL`` at these USD-per-1M-token rates (no cache-write rate: 1.25x input)."""
    row = ModelPrice(
        input=input_usd,
        output=output_usd,
        cache_write_5m=cache_write_usd,
        as_of=date(2026, 10, 1),
        source="unit-test prices",
    )
    return PricingTable(models={PAID_MODEL: row})


@pytest.mark.parametrize(
    ("cache_write_usd", "per_token"),
    [
        (None, 1.25),  # cache writes default to 1.25x input
        (3_000_000.0, 3.0),
        (500_000.0, 1.0),  # input is the dearer rate
    ],
)
async def test_the_estimate_prices_the_prompt_at_the_dearer_rate(
    tmp_path: Path, cache_write_usd: float | None, per_token: float
) -> None:
    """The Anthropic provider caches the system prompt, so a cache-writing call bills it at
    the cache-write rate: a call reserves its prompt's token bound at the dearer of that and
    the input rate, and the budget decides on that worst case. Output is free here, so the
    prompt alone decides whether the call fits."""
    case = make_case("c1")
    write_dataset(tmp_path, [case])
    worst = prompt_token_bound(FakeRenderer().build_request(case, "baseline")) * per_token
    for budget, calls in ((worst - 0.5, 0), (worst, 1)):
        provider = FakeProvider(model=PAID_MODEL)
        prices = price_row(1_000_000.0, 0.0, cache_write_usd)
        await run(tmp_path, paid(budget_usd=budget), {"paid": provider}, pricing=prices)
        assert len(provider.calls) == calls, budget


async def test_concurrent_cache_writing_calls_never_overshoot_the_budget(tmp_path: Path) -> None:
    """Every call bills its whole prompt as a cache write (the dearest way it can be billed)
    and a full reply: reservations are true bounds, so a concurrent run stops at or under
    budget_usd, and only when spend alone leaves no room for one more reservation."""
    cases = request_cases(30)
    write_dataset(tmp_path, cases)
    prompt = prompt_token_bound(FakeRenderer().build_request(cases[0], "baseline"))
    assert {prompt_token_bound(FakeRenderer().build_request(c, "baseline")) for c in cases[:9]} == {
        prompt
    }
    prices = price_row(1.0, 1_000.0, 2.0)

    def dearest(_request: ModelRequest) -> ModelResponse:
        return answer(usage=Usage(cache_write_tokens=prompt, output_tokens=10))

    provider = FakeProvider(
        model=PAID_MODEL, script=dearest, delay=lambda r: 0.001 * (stable_hash(case_of(r)) % 4)
    )
    per_call = (2.0 * prompt + 1_000.0 * 10) / 1_000_000
    budget = per_call * 7.5
    config = paid(budget_usd=budget, concurrency=4)
    result = await run(tmp_path, config, {"paid": provider}, pricing=prices)
    assert provider.peak >= 2
    assert result.spent_usd <= budget
    assert len(provider.calls) == 7 and len(result.skipped) == 23


async def test_a_budget_stop_leaves_every_config_at_the_same_cases(tmp_path: Path) -> None:
    """Cells run case by case across configs, so a budget stop cuts every config at about the
    same case instead of finishing one config and starving the next."""
    write_dataset(tmp_path, request_cases(4))
    provider = FakeProvider(model=PAID_MODEL)
    config = paid(budget_usd=0.45, prompts=["baseline", "checklist"])
    result = await run(tmp_path, config, {"paid": provider}, pricing=PRICES)
    assert provider.called_cases() == ["c1", "c1", "c2", "c2"]
    plain, checklist = "paid__baseline__context", "paid__checklist__context"
    assert [(s.case_id, s.config_id) for s in result.skipped] == [
        ("c3", plain),
        ("c3", checklist),
        ("c4", plain),
        ("c4", checklist),
    ]


async def test_free_models_finish_after_the_budget_stops_the_paid_ones(tmp_path: Path) -> None:
    """An unmetered model can't spend, so the budget never holds it back, and a resume with
    the same budget has nothing left to do for it."""
    cases = request_cases(4)
    write_dataset(tmp_path, cases)
    free, dear = FakeProvider(), FakeProvider(model=PAID_MODEL)
    config = paid(budget_usd=0.15, models=[PAID_ENTRY, FAKE_MODEL])
    result = await run(tmp_path, config, {"paid": dear, "fake-a": free}, pricing=PRICES)
    assert dear.called_cases() == ["c1"]
    assert free.called_cases() == [c.case_id for c in cases]
    assert [(s.config_id, s.case_id) for s in result.skipped] == [
        ("paid__baseline__context", c) for c in ("c2", "c3", "c4")
    ]


CLI_ENTRY: dict[str, Any] = {"provider": "claude_cli", "model": "sonnet", "label": "cli"}


def cli(**overrides: Any) -> ExperimentConfig:
    return experiment(models=[CLI_ENTRY], **overrides)


def reporting(cost: float, failures: dict[str, ModelResponse] | None = None) -> FakeProvider:
    """A CLI-like fake: every answer reports ``cost`` (except scripted ``failures``)."""
    table = failures or {}
    return FakeProvider(
        script=lambda r: table.get(case_of(r), answer(reported_cost_usd=cost)),
        delay=lambda _r: 0.01,
    )


@pytest.mark.parametrize(
    ("budget", "others_in_flight"),
    [(0.0, []), (0.015, [0]), (0.035, [0, 0, 1])],
)
async def test_a_reported_cost_cli_cant_overshoot_the_budget_concurrently(
    tmp_path: Path, budget: float, others_in_flight: list[int]
) -> None:
    """Before its first reported cost a CLI has no estimate, so its first call runs alone (and
    only while some budget is left); the calls queued behind it reserve what it reported."""
    write_dataset(tmp_path, request_cases(6))
    provider = reporting(0.01)
    result = await run(tmp_path, cli(budget_usd=budget, concurrency=4), {"cli": provider})
    assert provider.others_in_flight == others_in_flight
    spent = sum(p.usage.cost_usd or 0.0 for p in predictions(result))
    assert spent == pytest.approx(0.01 * len(others_in_flight)) and spent <= budget
    assert len(result.skipped) == 6 - len(others_in_flight)
    assert all("reached" in s.reason for s in result.skipped)


async def test_a_reported_cost_cli_spends_its_budget_concurrently(tmp_path: Path) -> None:
    """A CLI reserves its dearest call so far ($0.05, the first) for every call, though the rest
    cost $0.01: refused calls wait for those reservations to come back, and the run stops only
    once spend alone leaves no room for one more ($0.20 - $0.05)."""
    write_dataset(tmp_path, request_cases(20))
    provider = reporting(0.01, {"c1": answer(reported_cost_usd=0.05)})
    result = await run(tmp_path, cli(budget_usd=0.2, concurrency=4), {"cli": provider})
    spent = sum(p.usage.cost_usd or 0.0 for p in predictions(result))
    assert 0.15 - 1e-9 <= spent <= 0.2 and len(provider.calls) >= 11
    assert max(provider.others_in_flight) >= 1
    assert all("budget_usd 0.2 reached" in s.reason for s in result.skipped)


async def test_a_failed_first_call_leaves_the_next_one_to_calibrate(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(5))
    provider = reporting(0.01, {"c1": ModelResponse(error="timeout")})
    result = await run(tmp_path, cli(budget_usd=1.0, concurrency=4), {"cli": provider})
    assert result.complete
    assert provider.called_cases() == ["c1", "c2", "c3", "c4", "c5"]
    # c1 (no cost reported) and c2 run alone; the rest overlap once c2 has reported a cost.
    assert provider.others_in_flight == [0, 0, 0, 1, 2]


async def test_without_a_budget_a_reported_cost_cli_runs_at_full_concurrency(
    tmp_path: Path,
) -> None:
    write_dataset(tmp_path, request_cases(4))
    provider = reporting(0.01)
    result = await run(tmp_path, cli(concurrency=4), {"cli": provider})
    assert result.complete and provider.peak == 4


OPENROUTER_ENTRY: dict[str, Any] = {
    "provider": "openai_compatible",
    "model": "mistralai/mistral-large-4-0",
    "label": "gateway",
    "base_url_env": "OPENROUTER_BASE_URL",
    "extra": {"provider_routing": {"order": ["Mistral"], "allow_fallbacks": False}},
}
"""An OpenRouter entry: ``reported`` by default, with no ``pricing.yaml`` row needed."""


GATEWAY_MAX_OUTPUT = 200


def gateway_config(**overrides: Any) -> ExperimentConfig:
    """The OpenRouter entry with replies capped at ``GATEWAY_MAX_OUTPUT`` tokens."""
    defaults: dict[str, Any] = {
        "models": [OPENROUTER_ENTRY],
        "params": {"max_output_tokens": GATEWAY_MAX_OUTPUT},
    }
    return experiment(**(defaults | overrides))


def gateway(
    rates: Mapping[str, tuple[float, float]],
    *,
    host_of: Callable[[str], str] = lambda _case: "Mistral",
    output_of: Callable[[str], int] = lambda _case: GATEWAY_MAX_OUTPUT,
    failures: dict[str, ModelResponse] | None = None,
) -> FakeProvider:
    """A per-call-billed gateway: the case's host charges its (input, output) USD per token,
    and reports that cost and its name as OpenRouter does. Prompts use a quarter of
    ``prompt_token_bound`` (about what prose tokenizes to), replies ``output_of(case)``
    tokens."""
    table = failures or {}

    def script(request: ModelRequest) -> ModelResponse:
        case = case_of(request)
        if case in table:
            return table[case]
        host = host_of(case)
        input_rate, output_rate = rates[host]
        usage = Usage(input_tokens=prompt_token_bound(request) // 4, output_tokens=output_of(case))
        cost = input_rate * usage.input_tokens + output_rate * usage.output_tokens
        return answer(usage=usage, reported_cost_usd=cost, raw={"upstream_provider": host})

    return FakeProvider(script=script, delay=lambda _r: 0.01)


async def test_a_budget_stops_an_openrouter_model_before_the_cap(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(10))
    provider = gateway({"Mistral": (2e-6, 6e-6)})
    config = gateway_config(budget_usd=0.03, concurrency=4)
    result = await run(tmp_path, config, {"gateway": provider})
    spent = sum(p.usage.cost_usd or 0.0 for p in predictions(result))
    assert spent == pytest.approx(result.spent_usd) and 0 < result.spent_usd <= 0.03
    assert all(p.usage.cost_source == "reported" for p in predictions(result))
    assert 2 <= len(provider.calls) < 10 and len(result.skipped) == 10 - len(provider.calls)
    assert all("budget_usd 0.03 reached" in s.reason for s in result.skipped)
    cost = result.manifest.cost["gateway__baseline__context"]
    assert cost.cost_mode == "reported" and cost.reported_usd == pytest.approx(spent)
    # The routing preference is part of what the manifest says ran.
    (model,) = result.manifest.experiment["models"]
    assert model["extra"]["provider_routing"] == {"order": ["Mistral"], "allow_fallbacks": False}


@pytest.mark.parametrize("concurrency", [1, 4])
@pytest.mark.parametrize("budget", [0.01, 0.03, 0.1, 0.25])
async def test_an_openrouter_call_dearer_than_any_before_cant_pass_the_cap(
    tmp_path: Path, budget: float, concurrency: int
) -> None:
    """A cheap first call (a 5-token reply) mustn't set the reservation for the full-length
    replies after it, which cost ten times as much: each call reserves the most its prompt
    and ``max_output_tokens`` can cost at the per-token rates the host's costs imply."""
    write_dataset(tmp_path, request_cases(12))
    provider = gateway(
        {"Mistral": (1e-5, 2e-4)}, output_of=lambda case: 5 if case == "c1" else GATEWAY_MAX_OUTPUT
    )
    config = gateway_config(budget_usd=budget, concurrency=concurrency)
    result = await run(tmp_path, config, {"gateway": provider})
    assert 0 < result.spent_usd <= budget
    assert len(provider.calls) + len(result.skipped) == 12
    assert all("reached" in s.reason for s in result.skipped)


@pytest.mark.parametrize("budget", [0.06, 0.1, 0.2, 0.4])
async def test_an_openrouter_budget_reserves_for_the_dearest_host_seen(
    tmp_path: Path, budget: float
) -> None:
    """Rates are learned per upstream host, and a call reserves the dearest host's bound: a
    gateway alternating with a host twenty times dearer must not be reserved for at the
    cheap host's rates."""
    write_dataset(tmp_path, request_cases(12))

    def host_of(case: str) -> str:
        return "Dear" if int(case.removeprefix("c")) % 2 == 0 else "Cheap"

    provider = gateway({"Cheap": (1e-6, 1e-5), "Dear": (2e-5, 2e-4)}, host_of=host_of)
    config = gateway_config(budget_usd=budget, concurrency=1)
    result = await run(tmp_path, config, {"gateway": provider})
    assert 0 < result.spent_usd <= budget
    assert {host_of(case) for case in provider.called_cases()} == {"Cheap", "Dear"}


async def test_an_openrouter_call_without_a_cost_is_unknown_and_stops_the_budget(
    tmp_path: Path,
) -> None:
    write_dataset(tmp_path, request_cases(4))
    provider = gateway({"Mistral": (2e-6, 6e-6)}, failures={"c2": answer()})
    result = await run(
        tmp_path, gateway_config(budget_usd=1.0, concurrency=1), {"gateway": provider}
    )
    by_case = {p.case_id: p for p in predictions(result)}
    assert by_case["c2"].usage.cost_usd is None and by_case["c2"].usage.cost_source == "unknown"
    assert [s.case_id for s in result.skipped] == ["c3", "c4"]
    assert all("can't be enforced" in s.reason for s in result.skipped)


async def test_a_failed_openrouter_call_that_used_tokens_without_a_cost_stops_the_budget(
    tmp_path: Path,
) -> None:
    """A failed call that used tokens may have been billed, so it isn't counted as free."""
    write_dataset(tmp_path, request_cases(4))
    failed = ModelResponse(error="response has no choices", usage=Usage(input_tokens=1000))
    provider = gateway({"Mistral": (2e-6, 6e-6)}, failures={"c2": failed})
    result = await run(
        tmp_path, gateway_config(budget_usd=1.0, concurrency=1), {"gateway": provider}
    )
    assert [s.case_id for s in result.skipped] == ["c3", "c4"]
    assert all("can't be enforced" in s.reason for s in result.skipped)


async def test_a_failed_openrouter_call_teaches_no_rates(tmp_path: Path) -> None:
    """A failed call's cost needn't match its usage (here it was billed $0 for 1,000 prompt
    tokens), so it gives no estimate: the next call still runs alone, and the calls after
    it overlap at the rates that one implies."""
    write_dataset(tmp_path, request_cases(5))
    failed = ModelResponse(
        error="upstream error",
        usage=Usage(input_tokens=1000, output_tokens=1),
        reported_cost_usd=0.0,
    )
    provider = gateway({"Mistral": (2e-6, 6e-6)}, failures={"c1": failed})
    result = await run(
        tmp_path, gateway_config(budget_usd=1.0, concurrency=4), {"gateway": provider}
    )
    assert provider.called_cases() == ["c1", "c2", "c3", "c4", "c5"]
    assert provider.others_in_flight == [0, 0, 0, 1, 2]
    assert result.spent_usd > 0


async def test_a_resume_charges_calls_whose_cell_was_skipped(tmp_path: Path) -> None:
    """A cell whose repair the budget stopped has no prediction, but its first answer was paid
    for: the resume charges it from responses.jsonl, so the same budget stays exhausted."""
    write_dataset(tmp_path, request_cases(1))
    provider = FakeProvider(model=PAID_MODEL, script=invalid_then(answer()))
    providers = {"paid": provider}
    config = paid(budget_usd=0.15, repair=True)
    first = await run(tmp_path, config, providers, pricing=PRICES)
    assert predictions(first) == [] and len(provider.calls) == 1

    again = await run(tmp_path, config, providers, pricing=PRICES, resume=first.run_id)
    assert [s.case_id for s in again.skipped] == ["c1"] and len(provider.calls) == 1
    # Spent, yet in no answer: the manifest's cost is the answers' cost, not the spend.
    assert again.spent_usd == pytest.approx(0.10)
    assert again.manifest.cost["paid__baseline__context"].metered_api_usd == 0.0


# --------------------------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------------------------


async def test_resume_after_an_interruption(tmp_path: Path) -> None:
    cases = request_cases(5)
    write_dataset(tmp_path, cases)
    crashing = FakeProvider(fail_on_call=3)
    with pytest.raises(Interrupted):
        await run(tmp_path, experiment(concurrency=1), crashing)
    (run_dir,) = (tmp_path / "runs").iterdir()
    manifest = RunManifest.load(run_dir / MANIFEST_FILE)
    assert manifest.finished_at is None
    assert manifest.counts.calls == 2
    with (run_dir / PREDICTIONS_FILE).open("a", encoding="utf-8") as fh:
        fh.write('{"run_id": "torn-wri')  # a write cut short by the crash
    with (run_dir / RESPONSES_FILE).open("a", encoding="utf-8") as fh:
        fh.write('{"key": "torn')

    provider = FakeProvider()
    result = await run(
        tmp_path,
        experiment(concurrency=1),
        provider,
        resume=run_dir.name,
        now=Clock(T0 + timedelta(hours=1)),
    )
    assert result.run_id == run_dir.name
    assert result.resumed == 2 and result.written == 3
    assert provider.called_cases() == ["c3", "c4", "c5"]
    assert [p.case_id for p in predictions(result)] == [c.case_id for c in cases]
    assert result.manifest.created_at == T0
    assert result.manifest.finished_at == T0 + timedelta(hours=1)
    assert result.manifest.counts.calls == 5
    # The recording is still a valid replay file: the torn line was cut, not merged into.
    assert len(load_responses_jsonl(run_dir / RESPONSES_FILE)) == 5


async def test_resume_retries_error_predictions_and_nothing_else(tmp_path: Path) -> None:
    """A transport failure is no answer, so a resume drops the error record and asks again;
    the new attempt takes its place. A refusal and an invalid answer are the model's answer:
    SPEC §11.3 allows no content retries, so they stay."""
    write_dataset(tmp_path, request_cases(4))
    first = await run(tmp_path, experiment(concurrency=1), FakeProvider(script=scripted(FAILURES)))
    assert first.complete and first.manifest.counts.errors == 1
    provider = FakeProvider()
    later = Clock(T0 + timedelta(hours=1))
    resumed = await run(
        tmp_path, experiment(concurrency=1), provider, resume=first.run_id, now=later
    )
    assert provider.called_cases() == ["c1"]
    assert (resumed.resumed, resumed.written) == (3, 1) and resumed.complete
    rows = predictions(resumed)
    assert [(p.case_id, p.status) for p in rows] == [
        ("c1", PredictionStatus.ok),
        ("c2", PredictionStatus.refused),
        ("c3", PredictionStatus.invalid),
        ("c4", PredictionStatus.ok),
    ]
    assert len((resumed.run_dir / PREDICTIONS_FILE).read_text().splitlines()) == 4
    counts = resumed.manifest.counts
    assert (counts.errors, counts.refusals, counts.invalid, counts.calls) == (0, 1, 1, 4)
    assert resumed.manifest.finished_at == T0 + timedelta(hours=1)  # finished again, now

    again = FakeProvider()
    await run(tmp_path, experiment(), again, resume=first.run_id)
    assert again.calls == []  # nothing left to retry


async def test_a_retry_the_budget_stops_leaves_the_cell_unanswered(tmp_path: Path) -> None:
    """The resume dropped the error record before the budget stopped it, so the cell has no
    prediction and is listed as skipped: the run stays unfinished until a resume with budget
    left gets an answer."""
    write_dataset(tmp_path, request_cases(2))
    failing = FakeProvider(model=PAID_MODEL, script=scripted({"c1": ModelResponse(error="503")}))
    first = await run(tmp_path, paid(), {"paid": failing}, pricing=PRICES)
    assert first.spent_usd == pytest.approx(0.10)  # c2's answer; the failure cost nothing
    assert first.complete and first.manifest.finished_at is not None

    later = FakeProvider(model=PAID_MODEL)
    stopped = await run(
        tmp_path, paid(budget_usd=0.15), {"paid": later}, pricing=PRICES, resume=first.run_id
    )
    assert later.calls == [] and not stopped.complete
    assert stopped.resumed == 1
    assert [(s.case_id, s.sample_idx) for s in stopped.skipped] == [("c1", 0)]
    assert [(p.case_id, p.status) for p in predictions(stopped)] == [("c2", PredictionStatus.ok)]
    assert stopped.manifest.finished_at is None and stopped.manifest.counts.errors == 0

    done = await run(
        tmp_path, paid(budget_usd=1.0), {"paid": later}, pricing=PRICES, resume=first.run_id
    )
    assert later.called_cases() == ["c1"] and done.complete
    assert [p.status for p in predictions(done)] == [PredictionStatus.ok, PredictionStatus.ok]


class Killed(BaseException):
    """Stands in for SIGKILL: the process dies, and no ``finally`` gets to run."""


@pytest.mark.parametrize("dies_in", ["the retried call", "the end-of-run rewrite"])
async def test_a_resume_killed_midway_leaves_one_record_per_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dies_in: str
) -> None:
    """The error record is gone before the retry is appended, so whenever the resume dies the
    file holds at most one record per cell: the scorer reads it (a missing cell is INVALID
    under ``allow_incomplete``), and the next resume answers whatever is left."""
    from identitybench.scoring.summary import score_run

    write_dataset(tmp_path, request_cases(4))
    first = await run(tmp_path, experiment(concurrency=1), FakeProvider(script=scripted(FAILURES)))
    assert first.manifest.counts.errors == 1 and first.manifest.finished_at is not None

    def killed(*_args: Any, **_kw: Any) -> RunManifest:
        raise Killed

    monkeypatch.setattr(run_module, "_finalize", killed)
    retry = FakeProvider(fail_on_call=1 if dies_in == "the retried call" else None)
    with pytest.raises((Killed, Interrupted)):
        await run(tmp_path, experiment(concurrency=1), retry, resume=first.run_id)
    monkeypatch.undo()

    path = first.run_dir / PREDICTIONS_FILE
    cells = [(p.case_id, p.status) for p in load_predictions(path, first.run_id)]
    assert len(path.read_text(encoding="utf-8").splitlines()) == len(cells)
    if dies_in == "the retried call":
        assert ("c1", PredictionStatus.error) not in cells and len(cells) == 3
    else:
        assert ("c1", PredictionStatus.ok) in cells and len(cells) == 4
    stored = RunManifest.load(first.run_dir / MANIFEST_FILE)
    assert stored.finished_at is None  # cleared before the error record was dropped
    scores = score_run(first.run_dir, tmp_path / DATASET, n_boot=20, allow_incomplete=True)
    assert list(scores.configs) == ["fake-a__baseline__context"]
    assert not scores.completeness.complete

    after = FakeProvider()
    resumed = await run(tmp_path, experiment(concurrency=1), after, resume=first.run_id)
    assert after.called_cases() == (["c1"] if dies_in == "the retried call" else [])
    assert resumed.complete and resumed.manifest.finished_at is not None
    assert [p.status for p in predictions(resumed)].count(PredictionStatus.error) == 0


async def test_a_refused_resume_changes_no_run_file(tmp_path: Path) -> None:
    """A resume that finds a corrupt recording refuses before it drops the error records it
    would retry or touches the manifest."""
    write_dataset(tmp_path, request_cases(4))
    first = await run(tmp_path, experiment(concurrency=1), FakeProvider(script=scripted(FAILURES)))
    with (first.run_dir / RESPONSES_FILE).open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    before = {f.name: f.read_bytes() for f in first.run_dir.iterdir()}
    with pytest.raises(RunError, match="not a response record"):
        await run(tmp_path, experiment(concurrency=1), FakeProvider(), resume=first.run_id)
    assert {f.name: f.read_bytes() for f in first.run_dir.iterdir()} == before


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_runs_own_files_never_make_the_tree_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean checkout that doesn't git-ignore a custom runs_dir or the response cache: the
    files the run writes there are untracked, yet they aren't code edits, so the run records a
    clean tree and resumes; a real edit still refuses the resume."""
    for name, value in (
        ("GIT_CONFIG_GLOBAL", os.devnull),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_TEMPLATE_DIR", ""),
        ("HOME", str(tmp_path)),
        ("GIT_CEILING_DIRECTORIES", str(tmp_path)),
    ):
        monkeypatch.setenv(name, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    write_dataset(repo, request_cases(3))
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "dataset")

    with pytest.raises(Interrupted):
        await run(repo, experiment(concurrency=1), FakeProvider(fail_on_call=2), runs_dir="results")
    (run_dir,) = (repo / "results").iterdir()
    assert RunManifest.load(run_dir / MANIFEST_FILE).git_dirty is False
    assert (repo / ".cache" / "responses").is_dir()
    result = await run(repo, experiment(), FakeProvider(), runs_dir="results", resume=run_dir.name)
    assert result.complete and result.manifest.git_dirty is False

    with pytest.raises(Interrupted):
        await run(
            repo, experiment(), FakeProvider(fail_on_call=1), runs_dir="results", refresh=True
        )
    (second,) = {path.name for path in (repo / "results").iterdir()} - {run_dir.name}
    (repo / "notes.txt").write_text("an edit", encoding="utf-8")
    with pytest.raises(RunError, match="the code changed"):
        await run(repo, experiment(), FakeProvider(), runs_dir="results", resume=second)


async def test_resume_refuses_a_different_experiment(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(2))
    first = await run(tmp_path, experiment(), FakeProvider())
    with pytest.raises(RunError, match="experiment settings changed: prompts"):
        await run(tmp_path, experiment(prompts=["checklist"]), FakeProvider(), resume=first.run_id)
    with pytest.raises(RunError, match="sample 1 differs"):
        await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id, sample_override=1)
    with pytest.raises(RunError, match="endpoint identity"):
        await run(tmp_path, experiment(), FakeProvider(identity="moved"), resume=first.run_id)
    with pytest.raises(RunError, match=r"no manifest\.json"):
        await run(tmp_path, experiment(), FakeProvider(), resume="nope")

    write_dataset(tmp_path, request_cases(2, prefix="d"))
    with pytest.raises(RunError, match="dataset content changed"):
        await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id)


async def test_resume_accepts_operational_changes_and_keeps_the_sample(tmp_path: Path) -> None:
    """Concurrency, timeouts and retries can't change an answer, and a resume without
    ``--sample`` continues the sample the run started with."""
    write_dataset(tmp_path, request_cases(6))
    with pytest.raises(Interrupted):
        await run(
            tmp_path, experiment(concurrency=1), FakeProvider(fail_on_call=2), sample_override=3
        )
    (run_dir,) = (tmp_path / "runs").iterdir()
    gentler = FAKE_MODEL | {"concurrency": 1, "timeout_s": 30.0, "extra": {"max_retries": 1}}
    config = experiment(concurrency=3, models=[gentler], budget_usd=2.0)
    result = await run(tmp_path, config, FakeProvider(), resume=run_dir.name)
    assert result.complete and result.manifest.sample == 3
    assert (result.resumed, result.written) == (1, 2)
    assert len({p.case_id for p in predictions(result)}) == 3
    assert result.manifest.experiment == config.resolved_dict(base=tmp_path) | {"sample": 3}


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("[]", "expected a JSON object"),
        ('{"key": "k", "response": {}}', "expected string 'key' and 'config_id'"),
        (
            '{"key": "k", "config_id": "c", "fresh": true, '
            '"response": {"tool_calls": [{"name": "x"}]}}',
            "malformed response",
        ),
    ],
)
async def test_resume_refuses_a_corrupt_recording(tmp_path: Path, line: str, reason: str) -> None:
    write_dataset(tmp_path, request_cases(1))
    first = await run(tmp_path, experiment(), FakeProvider())
    path = first.run_dir / RESPONSES_FILE
    path.write_text(line + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(RunError, match=rf":1: not a response record: {re.escape(reason)}"):
        await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id)


async def test_resume_of_a_finished_run_calls_nothing(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(2))
    first = await run(tmp_path, experiment(budget_usd=3.0), FakeProvider())
    provider = FakeProvider()
    later = Clock(T0 + timedelta(days=3))
    again = await run(
        tmp_path, experiment(budget_usd=9.0), provider, resume=first.run_id, now=later
    )
    assert provider.calls == [] and again.written == 0 and again.resumed == 2
    assert again.manifest.counts == first.manifest.counts
    # Nothing was answered, so the run finished when it finished, not when it was reopened.
    assert again.manifest.finished_at == first.manifest.finished_at == T0 + timedelta(minutes=1)
    assert RunManifest.load(again.run_dir / MANIFEST_FILE).finished_at == T0 + timedelta(minutes=1)


async def test_resume_refuses_different_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest records one git SHA, dirty flag and harness version for the whole run, and
    the prompt hash relies on the SHA to pin the policy text."""
    write_dataset(tmp_path, request_cases(2))
    sha = "a" * 40
    source: dict[str, tuple[str | None, bool | None]] = {"now": (sha, False)}
    monkeypatch.setattr(run_module, "git_info", lambda _root, **_kw: source["now"])
    first = await run(tmp_path, experiment(), FakeProvider())
    assert (first.manifest.git_sha, first.manifest.git_dirty) == (sha, False)
    assert first.manifest.harness_version == identitybench.__version__

    for now, says in (
        (("b" * 40, False), "git aaaaaaaaaaaa, now bbbbbbbbbbbb"),
        ((sha, True), "git aaaaaaaaaaaa, now aaaaaaaaaaaa with local edits"),
        ((None, None), "git aaaaaaaaaaaa, now no git checkout"),
    ):
        source["now"] = now
        with pytest.raises(RunError, match=re.escape(f"the code changed ({says})")):
            await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id)

    source["now"] = (sha, False)
    monkeypatch.setattr(run_module, "__version__", "9.9.9")
    run_version = identitybench.__version__
    with pytest.raises(
        RunError, match=f"harness version 9.9.9 differs from the run's {run_version}"
    ):
        await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id)


async def test_a_run_started_with_local_edits_resumes_on_the_same_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its manifest already says the code isn't pinned, so a resume can't make it say anything
    false."""
    write_dataset(tmp_path, request_cases(3))
    monkeypatch.setattr(run_module, "git_info", lambda _root, **_kw: ("a" * 40, True))
    with pytest.raises(Interrupted):
        await run(tmp_path, experiment(concurrency=1), FakeProvider(fail_on_call=2))
    (run_dir,) = (tmp_path / "runs").iterdir()
    result = await run(tmp_path, experiment(), FakeProvider(), resume=run_dir.name)
    assert result.complete and result.manifest.git_dirty is True


async def test_resume_refuses_a_different_price_table(tmp_path: Path) -> None:
    """Costs priced from two tables can't share the manifest's one ``pricing`` record."""
    write_dataset(tmp_path, request_cases(3))
    providers = {"paid": FakeProvider(model=PAID_MODEL)}
    first = await run(tmp_path, paid(budget_usd=0.15), providers, pricing=PRICES)
    dearer = PricingTable(
        models={PAID_MODEL: PRICES.models[PAID_MODEL].model_copy(update={"output": 20_000.0})}
    )
    with pytest.raises(RunError, match="the pricing table changed"):
        await run(tmp_path, paid(budget_usd=1.0), providers, pricing=dearer, resume=first.run_id)
    done = await run(tmp_path, paid(budget_usd=1.0), providers, pricing=PRICES, resume=first.run_id)
    assert done.complete and done.manifest.pricing == first.manifest.pricing


async def test_resume_tells_a_count_of_one_from_every_case(tmp_path: Path) -> None:
    """``1`` is one family and ``1.0`` every case, though Python calls them equal."""
    write_dataset(tmp_path, request_cases(3))
    first = await run(tmp_path, experiment(), FakeProvider(), sample_override=1)
    assert first.manifest.counts.cases == 1
    with pytest.raises(RunError, match=r"sample 1\.0 differs from the run's 1(?!\.)"):
        await run(tmp_path, experiment(), FakeProvider(), resume=first.run_id, sample_override=1.0)

    every = await run(tmp_path, experiment(sample=1.0), FakeProvider())
    with pytest.raises(RunError, match=r"sample 1 differs from the run's 1\.0"):
        await run(tmp_path, experiment(sample=1), FakeProvider(), resume=every.run_id)


GOOD = Prediction(
    run_id="r",
    config_id="c",
    case_id="k",
    task=Task.access_request,
    status=PredictionStatus.ok,
    output=RequestOutput(decision=RequestDecision.DENY, rationale="line separator"),
)


def test_load_predictions_forgives_only_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / PREDICTIONS_FILE
    assert load_predictions(path) == []
    line = GOOD.model_dump_json() + "\n"
    path.write_text(line + "\n" + '{"run_id"', encoding="utf-8")
    assert load_predictions(path, "r") == [GOOD]

    path.write_text(line.rstrip("\n"), encoding="utf-8")  # complete record, no newline yet
    assert load_predictions(path) == [GOOD]

    with pytest.raises(RunError, match="record of run r, not other"):
        load_predictions(path, "other")
    path.write_text("not json\n" + line, encoding="utf-8")
    with pytest.raises(RunError, match=":1: not a prediction record"):
        load_predictions(path)


def test_load_predictions_never_rewrites_the_file(tmp_path: Path) -> None:
    """A torn tail and an unterminated last record are left byte for byte: only the session
    that owns the run cuts them, before it appends."""
    path = tmp_path / PREDICTIONS_FILE
    line = GOOD.model_dump_json() + "\n"
    for text in (line + '{"run_id": "torn', line.rstrip("\n")):
        path.write_bytes(text.encode("utf-8"))
        inode = path.stat().st_ino
        assert load_predictions(path) == [GOOD]
        assert path.read_bytes() == text.encode("utf-8")
        assert path.stat().st_ino == inode


def test_a_reader_never_strands_a_live_writer(tmp_path: Path) -> None:
    """A progress view reads predictions.jsonl while the run is mid-line: the writer's handle
    still names the visible file afterwards, so nothing it appends is lost."""
    path = tmp_path / PREDICTIONS_FILE
    records = [GOOD.model_copy(update={"case_id": f"k{i}"}) for i in range(3)]
    with path.open("a", encoding="utf-8") as writer:
        writer.write(records[0].model_dump_json() + "\n")
        half = records[1].model_dump_json()
        writer.write(half[:20])  # a write cut in the middle
        writer.flush()
        assert load_predictions(path) == records[:1]
        writer.write(half[20:] + "\n" + records[2].model_dump_json() + "\n")
    assert load_predictions(path) == records


def test_load_predictions_refuses_a_second_record_of_a_cell(tmp_path: Path) -> None:
    """The runner writes one record per cell (a resume drops an error record before it retries
    the cell), so a second one means two sessions wrote the run: refused, never guessed at."""
    path = tmp_path / PREDICTIONS_FILE
    failed = GOOD.model_copy(update={"status": PredictionStatus.error, "output": None})
    other = GOOD.model_copy(update={"case_id": "k2"})
    repeat = GOOD.model_copy(update={"sample_idx": 1})
    lines = [failed, other, repeat]
    path.write_text("".join(p.model_dump_json() + "\n" for p in lines), encoding="utf-8")
    assert load_predictions(path) == lines
    with path.open("a", encoding="utf-8") as fh:
        fh.write(GOOD.model_dump_json() + "\n")
    with pytest.raises(RunError, match=r":4: second record of .* \(first on line 1\)"):
        load_predictions(path)


# --------------------------------------------------------------------------------------------
# Repeats
# --------------------------------------------------------------------------------------------


async def test_repeats_run_a_stratified_subset_with_fresh_inferences(tmp_path: Path) -> None:
    cases = request_cases(5)
    write_dataset(tmp_path, cases)
    baseline = FakeBaseline("always_escalate")
    provider = FakeProvider()
    config = experiment(repeats={"k": 3, "subset": 2}, baselines=["always_escalate"])
    result = await run(tmp_path, config, provider, baseline_factory=lambda _n: baseline)

    subset = [c.case_id for c in subset_for_repeats(cases, 2, seed=0)]
    rows = [p for p in predictions(result) if p.config_id == "fake-a__baseline__context"]
    assert [(p.case_id, p.sample_idx) for p in rows] == [
        (c.case_id, idx) for c in cases for idx in ((0, 1, 2) if c.case_id in subset else (0,))
    ]
    assert len(provider.calls) == 9  # sample_idx is in the cache key: each repeat is fresh
    assert len(baseline.decided) == 9
    assert result.manifest.repeats.k == 3 and result.manifest.repeats.subset == 2

    cached = await run(tmp_path, config, provider, baseline_factory=lambda _n: baseline)
    assert len(provider.calls) == 9 and all(
        p.cache_hit for p in predictions(cached) if p.config_id != "always_escalate"
    )


async def test_repeats_come_after_every_cases_first_answer(tmp_path: Path) -> None:
    """Repeat-major scheduling: every case's sample 0 is asked before any repeat, so a budget
    stop leaves the repeats unanswered, never a case's first answer (the scorer's completeness
    check looks for missing sample-0 predictions)."""
    cases = request_cases(5)
    write_dataset(tmp_path, cases)
    subset = [c.case_id for c in subset_for_repeats(cases, 2, seed=0)]
    order = FakeProvider(model=PAID_MODEL)
    await run(tmp_path, paid(repeats={"k": 3, "subset": 2}), {"paid": order}, pricing=PRICES)
    assert order.called_cases() == [c.case_id for c in cases] + subset + subset

    # $0.10 a call: the budget buys three first answers, and every repeat is left over.
    stopped = FakeProvider(model=PAID_MODEL)
    config = paid(repeats={"k": 3, "subset": 2}, budget_usd=0.35)
    result = await run(tmp_path, config, {"paid": stopped}, pricing=PRICES, refresh=True)
    assert [(p.case_id, p.sample_idx) for p in predictions(result)] == [
        (c.case_id, 0) for c in cases[:3]
    ]
    assert [(s.case_id, s.sample_idx) for s in result.skipped] == [
        ("c4", 0),
        ("c5", 0),
        *((case_id, 1) for case_id in subset),
        *((case_id, 2) for case_id in subset),
    ]


async def test_repeats_without_a_subset_cover_every_case(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(3))
    result = await run(tmp_path, experiment(repeats={"k": 2}), FakeProvider())
    assert sorted((p.case_id, p.sample_idx) for p in predictions(result)) == [
        (f"c{i}", idx) for i in range(1, 4) for idx in (0, 1)
    ]


# --------------------------------------------------------------------------------------------
# Record, then replay
# --------------------------------------------------------------------------------------------


async def test_record_then_replay_gives_identical_predictions(tmp_path: Path) -> None:
    write_dataset(tmp_path, [*request_cases(4), review_case("r1")])
    behaviours = FAILURES | {"r1": answer(json.dumps({"items": [review_item("rv_1")]}))}
    recorded = await run(
        tmp_path,
        experiment(tasks=["requests", "reviews"], repeats={"k": 2, "subset": 2}),
        FakeProvider(script=scripted(behaviours)),
    )
    recording = recorded.run_dir / RESPONSES_FILE

    replay_entry = {"provider": "replay", "model": "fake-a", "extra": {"path": str(recording)}}
    config = experiment(
        tasks=["requests", "reviews"], repeats={"k": 2, "subset": 2}, models=[replay_entry]
    )
    replayed = await run_experiment(config, repo_root=tmp_path, render=FakeRenderer(), now=Clock())
    original = {(p.case_id, p.sample_idx): p for p in predictions(recorded)}
    copies = predictions(replayed)
    assert len(copies) == len(original) == 7
    for copy in copies:
        assert copy.config_id == "replay_fake-a__baseline__context"
        source = original[(copy.case_id, copy.sample_idx)]
        assert comparable(copy, "config_id") == comparable(source, "config_id")
    assert replayed.manifest.configs[0].provider == "replay"

    # Replays never go through the response cache, so an edited recording is always served.
    again = await run_experiment(config, repo_root=tmp_path, render=FakeRenderer(), now=Clock())
    assert not any(p.cache_hit for p in predictions(again))

    # A prompt the recording never saw has no recorded answer: an error, never a fabrication.
    other = experiment(tasks=["requests"], prompts=["checklist"], models=[replay_entry])
    missing = await run_experiment(other, repo_root=tmp_path, render=FakeRenderer(), now=Clock())
    rows = predictions(missing)
    assert rows and all(p.status is PredictionStatus.error for p in rows)
    assert all("no recorded response" in (p.invalid_reason or "") for p in rows)


async def test_replaying_a_metered_recording_keeps_its_cost_provenance(tmp_path: Path) -> None:
    """A replay spends nothing, but the answers it serves cost what the recording says: the
    replayed predictions match the original's, costs and sources included, and the manifest's
    cost is the answers' (like a cache hit's), while the replay's spend is $0. A recording
    without costs (an older runner's) replays at the replay's own $0, unmetered."""
    write_dataset(tmp_path, request_cases(3))
    behaviours = {"c1": ModelResponse(error="HTTP 503 after 4 attempts")}
    provider = FakeProvider(model=PAID_MODEL, script=scripted(behaviours))
    recorded = await run(tmp_path, paid(), {"paid": provider}, pricing=PRICES)
    recording = recorded.run_dir / RESPONSES_FILE
    entry = {"provider": "replay", "model": "paid", "extra": {"path": str(recording)}}
    replayed = await run_experiment(
        experiment(models=[entry]), repo_root=tmp_path, render=FakeRenderer(), now=Clock()
    )
    original = {p.case_id: p for p in predictions(recorded)}
    copies = predictions(replayed)
    assert len(copies) == 3
    for copy in copies:
        assert comparable(copy, "config_id") == comparable(original[copy.case_id], "config_id")
    assert [(p.usage.cost_usd, p.usage.cost_source) for p in copies] == [
        (0.0, "metered"),
        (pytest.approx(0.10), "metered"),
        (pytest.approx(0.10), "metered"),
    ]
    assert replayed.spent_usd == 0.0
    summary = replayed.manifest.cost["replay_paid__baseline__context"]
    assert summary.cost_mode == "unmetered"
    assert summary.metered_api_usd == pytest.approx(0.2)

    # Recordings carry each answer's cost, the replay's own recording included, so a replay
    # of a replay keeps the original costs too.
    def costs(path: Path) -> list[tuple[str, float]]:
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        return sorted((line["cost_source"], line["cost_usd"]) for line in lines)

    assert costs(recording) == costs(replayed.run_dir / RESPONSES_FILE)
    assert costs(recording) == [
        ("metered", 0.0),
        ("metered", pytest.approx(0.10)),
        ("metered", pytest.approx(0.10)),
    ]
    lines = [json.loads(line) for line in recording.read_text().splitlines()]
    for line in lines:
        del line["cost_usd"], line["cost_source"]
    recording.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    older = await run_experiment(
        experiment(models=[entry]), repo_root=tmp_path, render=FakeRenderer(), now=Clock()
    )
    assert {(p.usage.cost_usd, p.usage.cost_source) for p in predictions(older)} == {
        (0.0, "unmetered")
    }
    assert older.manifest.cost["replay_paid__baseline__context"].metered_api_usd == 0.0


async def test_a_replay_recording_is_found_relative_to_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like every other path of a run, and like the manifest records it, wherever the process
    happens to run from."""
    write_dataset(tmp_path, request_cases(2))
    recorded = await run(tmp_path, experiment(), FakeProvider())
    relative = (recorded.run_dir / RESPONSES_FILE).relative_to(tmp_path).as_posix()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    entry = {"provider": "replay", "model": "fake-a", "extra": {"path": relative}}
    replayed = await run_experiment(
        experiment(models=[entry]), repo_root=tmp_path, render=FakeRenderer(), now=Clock()
    )
    rows = predictions(replayed)
    assert len(rows) == 2 and all(p.status is PredictionStatus.ok for p in rows)
    assert replayed.manifest.experiment["models"][0]["extra"]["path"] == relative

    gone = {**entry, "extra": {"path": "runs/nope/responses.jsonl"}}
    with pytest.raises(RunError, match="model 'replay:fake-a': cannot create its provider"):
        await run_experiment(
            experiment(models=[gone]), repo_root=tmp_path, render=FakeRenderer(), now=Clock()
        )


async def test_cli_binaries_with_a_directory_part_are_found_relative_to_the_repo(
    tmp_path: Path,
) -> None:
    """A bare name is a ``PATH`` lookup and is passed on as written."""
    write_dataset(tmp_path, request_cases(1))
    seen: dict[str, Any] = {}

    def factory(spec: ModelSpec) -> FakeProvider:
        seen[spec.display_label] = spec.extra.get("binary")
        return FakeProvider()

    models = [
        CLI_ENTRY | {"label": "local", "extra": {"binary": "bin/claude"}},
        CLI_ENTRY | {"label": "on-path", "extra": {"binary": "claude"}},
    ]
    result = await run_experiment(
        experiment(models=models),
        repo_root=tmp_path,
        render=FakeRenderer(),
        provider_factory=factory,
        now=Clock(),
    )
    assert seen == {"local": str(tmp_path / "bin" / "claude"), "on-path": "claude"}
    recorded = [m["extra"]["binary"] for m in result.manifest.experiment["models"]]
    assert recorded == ["bin/claude", "claude"]


def test_replay_key_names_the_recorded_config_and_ignores_params() -> None:
    request = ModelRequest(system="s", messages=(Message(Role.user, "u"),))
    hot = ModelRequest(
        system="s",
        messages=(Message(Role.user, "u"),),
        params=GenerationParams(temperature=1.0, extra={"cache_key": "x"}),
    )
    key = replay_key("fake-a", request, 0)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert replay_key("fake-a", hot, 0) == key
    assert replay_key("fake-b", request, 0) != key
    assert replay_key("fake-a", request, 1) != key
    other_prompt = ModelRequest(system="s2", messages=request.messages)
    assert replay_key("fake-a", other_prompt, 0) != key


# --------------------------------------------------------------------------------------------
# Baselines and few-shot
# --------------------------------------------------------------------------------------------


async def test_baselines_answer_through_decide_and_skip_unsupported_tasks(
    tmp_path: Path,
) -> None:
    cases = [*request_cases(2), review_case("r1"), review_case("r2")]
    write_dataset(tmp_path, cases)
    plans = ScopedBaseline("noop_plan", tasks=frozenset({Task.jml}))
    reviews_only = ScopedBaseline("peer_recommender", tasks=frozenset({Task.access_review}))
    escalate = FakeBaseline("always_escalate")
    table = {b.model: b for b in (plans, reviews_only, escalate)}
    config = experiment(
        tasks=["requests", "reviews"],
        models=[],
        baselines=["noop_plan", "peer_recommender", "always_escalate"],
    )
    # No model configs: the prompt renderer is never needed.
    result = await run_experiment(
        config, repo_root=tmp_path, baseline_factory=table.__getitem__, now=Clock()
    )
    assert plans.decided == []
    assert reviews_only.decided == ["r1", "r2"]
    assert escalate.decided == ["c1", "c2", "r1", "r2"]
    assert result.unsupported == 4 + 2
    rows = predictions(result)
    assert [(p.config_id, p.case_id) for p in rows] == [
        ("peer_recommender", "r1"),
        ("peer_recommender", "r2"),
        *(("always_escalate", c.case_id) for c in cases),
    ]
    assert all(p.status is PredictionStatus.ok for p in rows)
    assert all(p.usage.cost_usd == 0.0 and p.usage.cost_source == "unmetered" for p in rows)
    assert result.manifest.counts.calls == 0
    assert result.manifest.cost["always_escalate"].metered_api_usd == 0.0
    assert not (result.run_dir / RESPONSES_FILE).read_text()


async def test_real_baselines_through_the_default_factory(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(2))
    config = experiment(models=[], baselines=["always_escalate", "noop_plan"])
    result = await run_experiment(config, repo_root=tmp_path, now=Clock())
    rows = predictions(result)
    assert [p.config_id for p in rows] == ["always_escalate", "always_escalate"]
    assert all(
        isinstance(p.output, RequestOutput) and p.output.decision is RequestDecision.ESCALATE
        for p in rows
    )
    assert result.unsupported == 2  # noop_plan only plans JML events


@pytest.mark.parametrize(
    "dev",
    [
        pytest.param([], id="no dev split (a fresh release)"),
        pytest.param([review_case("rd1", split=Split.dev)], id="no probe suite in both splits"),
    ],
)
async def test_surface_probe_on_a_dataset_the_probe_never_applies_to_says_to_drop_it(
    tmp_path: Path, dev: list[Case]
) -> None:
    """The probe trains on dev and is scored on test, per suite, so a dataset without some
    probe suite in both splits never has a PROBE.json to publish (validate skips the probe,
    and ``write_probe_file`` refuses): the error says to drop the baseline, not to publish."""
    write_dataset(tmp_path, request_cases(1) + dev)
    config = experiment(models=[], baselines=["surface_probe"])
    with pytest.raises(RunError, match="surface_probe can't run on this dataset") as raised:
        await run_experiment(config, repo_root=tmp_path, now=Clock())
    assert "drop surface_probe from baselines" in str(raised.value)
    assert "--write-probe" not in str(raised.value)
    assert not (tmp_path / "runs").exists()


async def test_fewshot_examples_come_from_dev(tmp_path: Path) -> None:
    dev = request_cases(4, split=Split.dev, prefix="d")
    write_dataset(tmp_path, request_cases(2) + dev)
    render = FakeRenderer(fewshot_k={"fewshot-3": 2})
    provider = FakeProvider()
    config = experiment(prompts=["baseline", "fewshot-3"], seed=7)
    await run(tmp_path, config, provider, render=render)
    assert render.fewshot_calls == [(Task.access_request, 2, 7, ("d1", "d2", "d3", "d4"))]
    by_prompt = {(r.system, case_of(r)): r.messages[0].content for r in provider.calls}
    assert by_prompt[("policy [fewshot-3]", "c1")] == "case=c1 examples=[d1 d2]"
    assert by_prompt[("policy [baseline]", "c1")] == "case=c1 examples=[]"


async def test_fewshot_never_shows_a_dev_case_its_own_family(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(3, split=Split.dev, prefix="d"))
    render = FakeRenderer(fewshot_k={"fewshot-3": 2})
    provider = FakeProvider()
    await run(tmp_path, experiment(split="dev", prompts=["fewshot-3"]), provider, render=render)
    content = {case_of(r): r.messages[0].content for r in provider.calls}
    assert content == {
        "d1": "case=d1 examples=[d2 d3]",
        "d2": "case=d2 examples=[d1 d3]",
        "d3": "case=d3 examples=[d1 d2]",
    }


# --------------------------------------------------------------------------------------------
# Real collaborators and setup errors
# --------------------------------------------------------------------------------------------


async def test_the_real_renderer_end_to_end(tmp_path: Path) -> None:
    """No injected renderer or clock: the run uses ``render.prompt`` and the wall clock."""
    from identitybench.render import prompt as real_prompt

    write_dataset(tmp_path, [make_case("c1"), review_case("r1")])
    review_answer = answer(json.dumps({"items": [review_item("rv_1")]}))

    def script(request: ModelRequest) -> ModelResponse:
        is_review = request.output_schema == OUTPUT_SCHEMAS[Task.access_review]
        return review_answer if is_review else answer()

    provider = FakeProvider(script=script)
    config = experiment(tasks=["requests", "reviews"], prompts=["baseline", "checklist"])
    result = await run_experiment(config, repo_root=tmp_path, provider_factory=lambda _s: provider)

    assert result.manifest.created_at.tzinfo is not None
    assert [c.prompt_sha256 for c in result.manifest.configs] == [
        real_prompt.prompt_sha256("baseline"),
        real_prompt.prompt_sha256("checklist"),
    ]
    for variant in ("baseline", "checklist"):
        assert real_prompt.build_request(make_case("c1"), variant) in provider.calls
    assert len(provider.calls) == 4
    assert all(p.status is PredictionStatus.ok for p in predictions(result))


async def test_unavailable_modules_fail_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_dataset(tmp_path, request_cases(1))
    monkeypatch.setattr(run_module, "_RENDER_MODULE", "identitybench.render.not_there")
    with pytest.raises(RunError, match="prompt rendering"):
        await run_experiment(
            experiment(), repo_root=tmp_path, provider_factory=lambda _s: FakeProvider()
        )
    monkeypatch.setattr(run_module, "_BASELINES_MODULE", "identitybench.providers.not_there")
    with pytest.raises(RunError, match="baselines"):
        await run_experiment(experiment(models=[], baselines=["oracle"]), repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()


def write_probe_record(repo: Path, record: object) -> None:
    """``record`` as the dataset's ``PROBE.json`` (raw: the runner tests don't validate)."""
    (repo / DATASET / PROBE_JSON).write_text(json.dumps(record), encoding="utf-8")


async def test_surface_probe_is_refitted_from_the_frozen_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen probe is the ``probe`` object of the dataset's published ``PROBE.json``,
    beside the results that ``validate --write-probe`` records there."""
    import identitybench.providers.baselines as baselines

    dev = request_cases(2, split=Split.dev, prefix="d")
    write_dataset(tmp_path, request_cases(1) + dev)
    write_probe_record(tmp_path, {"results": {"access_request": {}}, "probe": {"frozen": True}})
    seen: dict[str, Any] = {}

    def fake_make_baseline(name: str, **kw: Any) -> FakeBaseline:
        seen[name] = kw
        return FakeBaseline(name)

    monkeypatch.setattr(baselines, "make_baseline", fake_make_baseline)
    config = experiment(models=[], baselines=["surface_probe", "always_deny"])
    await run_experiment(config, repo_root=tmp_path, now=Clock())
    assert seen["always_deny"] == {}
    probe_kw = seen["surface_probe"]
    assert probe_kw["params"] == {"frozen": True}
    assert [c.case_id for c in probe_kw["dev_cases"]] == ["d1", "d2"]


@pytest.mark.parametrize(
    ("record", "problem"),
    [
        (None, "no PROBE.json in "),
        ({"format": 1, "results": {}}, "holds no frozen probe"),
        ({"probe": "not an object"}, "holds no frozen probe"),
        ("not json", "can't be read"),
    ],
)
async def test_surface_probe_without_a_published_frozen_probe_fails_before_any_run(
    tmp_path: Path, record: object, problem: str
) -> None:
    """A missing PROBE.json, a results-only (format 1) record or a broken file stops the run
    before anything is created, naming the command that publishes the frozen probe. Nothing
    reads a lowercase ``probe.json`` any more, which a case-insensitive file system would
    resolve to the results file."""
    write_dataset(tmp_path, request_cases(1) + request_cases(2, split=Split.dev, prefix="d"))
    if record == "not json":
        (tmp_path / DATASET / PROBE_JSON).write_text("{", encoding="utf-8")
    elif record is not None:
        write_probe_record(tmp_path, record)
    config = experiment(models=[], baselines=["surface_probe"])
    with pytest.raises(RunError, match="surface_probe needs the dataset's frozen") as raised:
        await run_experiment(config, repo_root=tmp_path, now=Clock())
    assert problem in str(raised.value)
    if record != "not json":
        assert "identitybench validate" in str(raised.value)
        assert "--write-probe" in str(raised.value)
    assert not (tmp_path / "runs").exists()


async def test_a_frozen_probe_the_dev_split_cannot_refit_fails_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Probe.from_json`` refuses a frozen probe whose training data the dev split doesn't
    reproduce (or whose trees another scikit-learn refits differently) with a ``ValueError``;
    the run reports it as a run error before anything is created."""
    import identitybench.providers.baselines as baselines

    write_dataset(tmp_path, request_cases(1) + request_cases(2, split=Split.dev, prefix="d"))
    write_probe_record(tmp_path, {"probe": {"frozen": True}})

    def refuse(name: str, **kw: Any) -> FakeBaseline:
        raise ValueError("these dev cases don't reproduce the probe's training data")

    monkeypatch.setattr(baselines, "make_baseline", refuse)
    config = experiment(models=[], baselines=["surface_probe"])
    with pytest.raises(RunError, match="can't refit the dataset's frozen probe: these dev"):
        await run_experiment(config, repo_root=tmp_path, now=Clock())
    assert not (tmp_path / "runs").exists()


async def test_prices_come_from_the_checkout_being_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_dataset(tmp_path, request_cases(1))
    rows = (
        "models:\n  {model}:\n    input: 0.0\n    output: {output}\n"
        "    as_of: 2026-10-01\n    source: {source}\n"
    )
    (tmp_path / "pricing.yaml").write_text(
        rows.format(model=PAID_MODEL, output=10_000.0, source="repo file"), encoding="utf-8"
    )
    providers = {"paid": FakeProvider(model=PAID_MODEL)}
    result = await run(tmp_path, paid(), providers)
    pricing = result.manifest.pricing
    assert pricing is not None and pricing.path == "pricing.yaml"
    assert pricing.rows[PAID_MODEL].source == "repo file"
    assert predictions(result)[0].usage.cost_usd == pytest.approx(0.10)

    override = tmp_path / "elsewhere.yaml"
    override.write_text(
        rows.format(model=PAID_MODEL, output=20_000.0, source="env"), encoding="utf-8"
    )
    monkeypatch.setenv("IDENTITYBENCH_PRICING", str(override))
    again = await run(tmp_path, paid(), providers)
    assert again.manifest.pricing is not None
    assert again.manifest.pricing.rows[PAID_MODEL].source == "env"
    assert predictions(again)[0].usage.cost_usd == pytest.approx(0.20)


async def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    base = make_case("c1")
    twin = make_case("c1", suite=Suite.twins)
    write_dataset(tmp_path, [base, twin])
    with pytest.raises(RunError, match="duplicate case ids"):
        await run(tmp_path, experiment(tasks=["requests", "twins"]), FakeProvider())


async def test_a_resumed_budget_counts_fresh_calls_only(tmp_path: Path) -> None:
    write_dataset(tmp_path, request_cases(3))
    providers = {"paid": FakeProvider(model=PAID_MODEL)}
    warm = await run(tmp_path, paid(budget_usd=0.15), providers, pricing=PRICES)
    assert [p.case_id for p in predictions(warm)] == ["c1"]  # c1 is now cached

    second = await run(tmp_path, paid(budget_usd=0.15), providers, pricing=PRICES)
    rows = predictions(second)
    assert [(p.case_id, p.cache_hit) for p in rows] == [("c1", True), ("c2", False)]
    assert [s.case_id for s in second.skipped] == ["c3"]

    # Spent so far: $0.10 (c2). Counting the cached c1 too would leave no room for c3.
    done = await run(
        tmp_path, paid(budget_usd=0.25), providers, pricing=PRICES, resume=second.run_id
    )
    assert done.complete and [p.case_id for p in predictions(done)] == ["c1", "c2", "c3"]
