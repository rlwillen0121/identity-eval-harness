"""The provenance section: exactly what each run measured (SPEC §11.4).

A published number must be traceable to its dataset (name, version, sha256), the code that
produced it (git sha, and whether the tree was dirty), the endpoints that answered (endpoint
identities, never URLs or keys), the prompts (the full sha256 of each variant, which a reader
can check against the published prompt files), the prices behind its
cost figures (the pricing table's hash and each row's date and source), how the cases were
sampled, and the scoring settings, including the loss matrix. Everything here comes from the
run manifest and the scores, verbatim; a run loaded without its manifest says so. Where the run
directory sat on disk is left out: the run ID and the dataset sha256 identify the run, and a
local path would leak the machine's layout into a published report and make its bytes depend on
where the run was read from.
"""

from __future__ import annotations

from collections.abc import Sequence

from identitybench.report import format as fmt
from identitybench.report.common import ConfigRef
from identitybench.report.comparable import sample_text
from identitybench.report.document import Block, Facts, Group, Table, Text
from identitybench.report.inputs import JudgeSummary, ReportRun, ValidationSummary
from identitybench.runner.manifest import RunManifest

__all__ = ["provenance_blocks"]


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _run_facts(run: ReportRun) -> list[tuple[str, str]]:
    scores = run.scores
    dataset = scores.dataset
    settings = scores.settings
    loss = ", ".join(f"{name} {value:g}" for name, value in settings.loss.model_dump().items())
    c = scores.completeness
    facts = [
        ("Run", scores.run_id),
        ("Dataset", f"{dataset.name} {dataset.version} at {dataset.path}"),
        ("Dataset sha256", dataset.sha256),
        ("Split", scores.split.value),
        ("Scorer version", scores.scorer_version),
        (
            "Completeness",
            f"{'complete' if c.complete else 'INCOMPLETE'}: {c.n_expected_cases} expected cases, "
            f"{c.n_skipped_cells} skipped cells, finished {_yes_no(c.finished)}",
        ),
        ("Loss matrix", loss),
        (
            "Statistics",
            f"B = {settings.n_boot} bootstrap resamples, alpha {settings.alpha:g}, seed "
            f"{settings.seed}, WAR target {fmt.pct_exact(settings.war_target)}, small n < "
            f"{settings.small_n}",
        ),
        ("Operating points chosen on", scores.dev_run_id or "no dev run"),
    ]
    return facts


def _manifest_facts(manifest: RunManifest) -> list[tuple[str, str]]:
    seeds = ", ".join(f"{k} {v}" for k, v in sorted(manifest.seeds.items())) or "none"
    return [
        ("Created", manifest.created_at.isoformat()),
        ("Finished", manifest.finished_at.isoformat() if manifest.finished_at else "not finished"),
        ("Harness version", manifest.harness_version),
        ("Git sha", manifest.git_sha or "unknown"),
        ("Git tree dirty", _yes_no(manifest.git_dirty)),
        ("Sample", sample_text(manifest)),
        ("Repeats", manifest.repeats.model_dump_json()),
        ("Seeds", seeds),
        ("JSON repair", "on" if manifest.repair else "off"),
        ("Response cache bypassed (--refresh)", "yes" if manifest.refresh else "no"),
        (
            "Counts",
            f"{manifest.counts.cases} cases, {manifest.counts.calls} calls, "
            f"{manifest.counts.cache_hits} cache hits, {manifest.counts.errors} errors, "
            f"{manifest.counts.refusals} refusals, {manifest.counts.invalid} invalid",
        ),
    ]


def _configs(manifest: RunManifest) -> Table:
    columns = (
        "Config",
        "Provider",
        "Model",
        "Endpoint identity",
        "Prompt",
        "Prompt sha256",
        "Mode",
        "Temperature",
        "Max output tokens",
        "Effort",
    )
    rows = tuple(
        (
            c.config_id,
            c.provider,
            c.model,
            c.endpoint_identity,
            c.prompt_variant,
            c.prompt_sha256,
            c.mode,
            "not sent" if c.params.temperature is None else f"{c.params.temperature:g}",
            str(c.params.max_output_tokens),
            c.params.effort or "default",
        )
        for c in sorted(manifest.configs, key=lambda c: c.config_id)
    )
    return Table(columns, rows, caption="Configurations as run.")


def _pricing(manifest: RunManifest) -> list[Block]:
    pricing = manifest.pricing
    if pricing is None:
        return [Text("No configuration was metered, so no pricing table was used.")]
    blocks: list[Block] = [
        Facts(
            (
                ("Pricing table", pricing.path or "built in memory"),
                ("Pricing sha256", pricing.sha256),
            )
        )
    ]
    if pricing.rows:
        blocks.append(
            Table(
                ("Model", "Price as of", "Source"),
                tuple(
                    (model, row.as_of.isoformat(), row.source)
                    for model, row in sorted(pricing.rows.items())
                ),
                caption="Price rows the run was billed from.",
            )
        )
    return blocks


def _judge(judge: JudgeSummary, validation: ValidationSummary | None) -> list[Block]:
    blocks: list[Block] = [Facts((("Judge rubric sha256", judge.rubric_sha256),))]
    if judge.judge_info:
        blocks.append(
            Table(
                ("Judge", "Family", "Kind", "Model", "Endpoint identity", "Temperature"),
                tuple(
                    (
                        info.name,
                        info.family,
                        info.kind,
                        info.model,
                        info.endpoint_identity,
                        "not sent"
                        if info.params.get("temperature") is None
                        else str(info.params["temperature"]),
                    )
                    for info in judge.judge_info
                ),
                caption="Judges.",
            )
        )
    if judge.temperature_flagged:
        blocks.append(
            Text(
                f"Judges not run at temperature 0 on every call: "
                f"{', '.join(judge.temperature_flagged)}.",
                "warning",
            )
        )
    if validation is not None:
        blocks.append(
            Facts(
                (
                    ("Judge validation", validation.framing),
                    ("Validation labels", f"{validation.n_labels} {validation.split} labels"),
                    ("Validation rubric sha256", validation.rubric_sha256),
                    ("Gated rater", validation.gated_rater),
                )
            )
        )
    return blocks


def provenance_blocks(runs: Sequence[ReportRun], refs: Sequence[ConfigRef]) -> list[Block]:
    """One group per run: run facts, manifest facts and configurations, pricing, judges."""
    blocks: list[Block] = []
    for run in runs:
        group: list[Block] = [Facts(tuple(_run_facts(run)))]
        manifest = run.manifest
        if manifest is None:
            group.append(Text("No manifest.json was read for this run.", "warning"))
        else:
            group += [Facts(tuple(_manifest_facts(manifest))), _configs(manifest)]
            group += _pricing(manifest)
        if run.judge is not None:
            group += _judge(run.judge, run.validation)
        names = [r.name for r in refs if r.run is run]
        group.append(Text(f"Scored configurations: {', '.join(names) or 'none'}."))
        blocks.append(Group(f"Run {run.run_id}", tuple(group)))
    return blocks
