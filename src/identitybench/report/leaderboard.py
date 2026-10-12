"""The leaderboard: rows per task, one task's full table and the README update.

The README's results block itself (a lead and the thesis graphic, then the full leaderboard
table, the other figures, the method and statistics and the rest of the detail collapsed) is
built by ``report.readme`` and written with :func:`update_readme`; :func:`leaderboard_table`
is one task's full table with every interval, for callers that want it as Markdown.

**Rows.** One per scored configuration of every run, baselines included (SPEC §14: baselines
appear on every leaderboard), ordered by NRC, the headline ranking metric (SPEC §12.3), with
n/a last. The configuration the scorer marks as the ceiling (``ConfigScores.ceiling``: the
``oracle`` baseline, which returns gold) comes after them all, labeled as such: it is a
pipeline check that would otherwise top every table, and the scorer leaves it out of its
rankings too. A row's numbers are read straight from the scores; nothing is recomputed.

**The per-task table.** Its columns: Model · Prompt · Mode · NRC · Accuracy · WAR ·
High-risk WAR · Correct automation · Grounded accuracy · Harmful flip rate · API $/1k
decisions, each with its interval, under a one-line caption that says which task, dataset,
split and n the numbers are for and what the markers mean. The SPEC §12.8 primary metrics
(NRC, WAR, correct automation, grounded accuracy) carry ★ in their headers; the other columns
are exploratory. The row n is the base cases scored;
the caption names each rate's own denominator, which a cell shows where it has no interval or
an exact bound. Harmful flip rate is n/a for a run without adversarial pairs, and carries its
own † when it rests on fewer twin families than the small-n threshold (pairs are resampled by
family, so the row's base-case count says nothing about it). API cost is per configuration
over all of its decisions, every task and repeat included: metered, or billed by a gateway
(marked), and n/a with its reason for a CLI's estimate, or a cost that is partly or wholly
unknown (``common.api_cost_cell``).

**Rows from several runs.** SPEC §12.8 compares configurations only within one run, so when
the table's rows come from more than one run its caption says that configurations from
different runs were not compared and their relative order is untested. A single run's table
says nothing about it.

**Comparable runs only.** A table ranks its rows against each other, so every row must be
measured on the same cases under the same settings. Runs whose dataset, split, sample or base
cases of the task differ (a ``--sample 0.1`` smoke run next to a full run, say), or whose loss
matrix, interval level or small-n threshold differ (``comparable.settings_differences``), are
refused with :class:`IncomparableRunsError` unless ``allow_mixed=True``, which ranks them under
a warning that names the difference (and gives each row its own n when the sizes differ).

**README update.** :func:`update_readme` replaces exactly the text between the
``<!-- leaderboard:start -->`` and ``<!-- leaderboard:end -->`` markers and nothing else, so it
can run on every release: applied twice it changes nothing the second time.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from identitybench.report import format as fmt
from identitybench.report.common import (
    API_COST_TAG_NOTES,
    CEILING,
    DECISION_NAMES,
    DECISION_TASKS,
    FLAG_BASELINE,
    FLAG_INCOMPLETE,
    FLAG_SMALL_N,
    TASK_TITLES,
    ConfigRef,
    api_cost_cell,
    api_cost_tag,
    config_refs,
    decision_n,
    decision_scores,
    denominator_clusters,
    interval_level,
    small_n_limit,
)
from identitybench.report.comparable import settings_differences, task_differences
from identitybench.report.inputs import ReportRun, RunInput, load_report_runs
from identitybench.report.markdown_text import escape_cell
from identitybench.schemas.enums import Label, Task
from identitybench.scoring.decisions import DecisionScores
from identitybench.scoring.metrics import Metric

__all__ = [
    "PRIMARY",
    "README_COLUMNS",
    "README_END",
    "README_START",
    "IncomparableRunsError",
    "LeaderboardRow",
    "MarkerError",
    "leaderboard_rows",
    "leaderboard_table",
    "update_readme",
]

README_START = "<!-- leaderboard:start -->"
README_END = "<!-- leaderboard:end -->"

PRIMARY = "★"
"""Marks a SPEC §12.8 predeclared primary metric in a column header."""

README_COLUMNS: tuple[str, ...] = (
    "Model",
    "Prompt",
    "Mode",
    f"NRC {PRIMARY} ↓",
    "Accuracy ↑",
    f"WAR {PRIMARY} ↓",
    "High-risk WAR ↓",
    f"Correct automation {PRIMARY} ↑",
    f"Grounded accuracy {PRIMARY} ↑",
    "Harmful flip rate ↓",
    "API $/1k decisions",
)
"""SPEC §15, in order; ★ marks the primary metrics and arrows say which direction is
better."""


class MarkerError(ValueError):
    """The README's leaderboard markers are missing, repeated or out of order."""


class IncomparableRunsError(ValueError):
    """The runs given to :func:`leaderboard_table` were not measured on the same cases."""


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    """One configuration's leaderboard numbers on one decision task."""

    ref: ConfigRef
    task: Task
    scores: DecisionScores

    @property
    def nrc(self) -> Metric:
        return self.scores.cost.nrc

    @property
    def harmful_flip_rate(self) -> Metric | None:
        robustness = self.ref.scores.robustness
        if robustness is None:
            return None
        pairs = robustness.requests if self.task is Task.access_request else robustness.reviews
        return pairs.harmful_flip_rate if pairs is not None else None

    @property
    def api_usd_per_1k(self) -> float | None:
        return self.ref.scores.usage.api_usd_per_1k_decisions

    @property
    def small_n(self) -> bool:
        """Fewer resampled units (cases, or campaigns for reviews) than SPEC §10.2's flag."""
        return self.scores.n_clusters < self.ref.run.scores.settings.small_n

    @property
    def harmful_flip_small_n(self) -> bool:
        """The harmful flip rate is defined but rests on fewer twin families than SPEC §10.2's
        flag. Pairs are resampled by family, so this is independent of :attr:`small_n`: a run
        with hundreds of base cases and twenty adversarial pairs flags here alone. Only the
        families in the rate's denominator count (:func:`~identitybench.report.common.
        denominator_clusters`): gold-APPROVE pairs are resampled but carry nothing of it."""
        flips = self.harmful_flip_rate
        return (
            flips is not None
            and flips.point is not None
            and denominator_clusters(flips) < self.ref.run.scores.settings.small_n
        )

    @property
    def n(self) -> str:
        """The row's n in its resampled units, flagged when small."""
        return decision_n(self.scores) + (f" {FLAG_SMALL_N}" if self.small_n else "")

    @property
    def display_label(self) -> str:
        """The README's Model cell: the manifest label with its markers, and the ceiling marked
        as such rather than as a rank."""
        return self.ref.label + (f" · {CEILING}" if self.ref.ceiling else "")

    @property
    def display_name(self) -> str:
        """The report's Configuration cell: the config ID with its markers, as every other
        report table names the configuration, and the ceiling marked as such."""
        return self.ref.name + (f" · {CEILING}" if self.ref.ceiling else "")

    def metrics(self) -> tuple[Metric | None, ...]:
        """The README columns' metrics, NRC through harmful flip rate (API cost is a price,
        not a metric)."""
        m = self.scores.metrics
        return (
            self.nrc,
            m.accuracy,
            m.war,
            m.high_risk_war,
            m.correct_automation,
            self.scores.grounding.grounded_accuracy,
            self.harmful_flip_rate,
        )

    def cells(self) -> tuple[str, ...]:
        """The README columns' metric cells (everything after Model, Prompt and Mode). WAR
        shows its exact bound when it has one: with few wrongful approvals the bootstrap
        interval collapses toward zero, and the bound is what says how small WAR can be shown
        to be (SPEC §12.8). The harmful flip rate carries † when its families are few
        (:attr:`harmful_flip_small_n`)."""
        nrc, *rates, flips = self.metrics()
        flagged = f" {FLAG_SMALL_N}" if self.harmful_flip_small_n else ""
        return (
            fmt.metric(nrc, percent=False, digits=3),
            *(fmt.metric(rate) for rate in rates),
            fmt.metric(flips) + flagged,
            api_cost_cell(self.ref.scores.usage),
        )


def _nrc_key(row: LeaderboardRow) -> tuple[bool, bool, float, str, str]:
    point = row.nrc.point
    return (
        row.ref.ceiling,
        point is None,
        point if point is not None else 0.0,
        row.ref.label,
        row.ref.name,
    )


def leaderboard_rows(runs: Sequence[ReportRun], task: Task) -> list[LeaderboardRow]:
    """Every configuration scored on ``task``, ordered by NRC (n/a last), then by name, with
    the ceiling after every ranked row."""
    if task not in DECISION_TASKS:
        raise ValueError(f"{task} has no leaderboard; JML plans are reported separately")
    rows = [
        LeaderboardRow(ref, task, scores)
        for ref in config_refs(runs)
        if (scores := decision_scores(ref.scores, task)) is not None
    ]
    return sorted(rows, key=_nrc_key)


def _denominators(task: Task) -> str:
    """Each rate's own denominator, in the task's words. A README reader may not know the
    word "gold", so the caption says "the oracle's decision"."""
    approve = DECISION_NAMES[task][Label.A]
    unit = "cases" if task is Task.access_request else "items"
    return (
        f"WAR counts {unit} where the oracle's decision isn't {approve}, high-risk WAR the "
        f"high- and critical-tier ones among them, grounded accuracy {unit} whose oracle "
        "decision has a certificate, and harmful flip rate adversarial pairs where the oracle's "
        f"decision isn't {approve} (a cell names its n where it has no interval or carries an "
        "exact bound)"
    )


def _caption(
    runs: Sequence[ReportRun],
    task: Task,
    rows: Sequence[LeaderboardRow],
    differences: Sequence[str],
    settings: Sequence[str],
    *,
    per_row_n: bool,
) -> str:
    """Task, dataset and split, the rows' n and every rate's denominator, and what every
    marker in the table means. Most cells carry no n of their own, so the caption is where a
    reader finds it."""
    datasets = sorted(
        {
            f"{r.scores.dataset.name} {r.scores.dataset.version} "
            f"(sha256 {r.scores.dataset.sha256[:12]}), {r.scores.split} split"
            for r in runs
        }
    )
    scored = "base cases" if task is Task.access_request else "base campaigns and their items"
    if per_row_n:
        size = f"n (the {scored} scored) differs between rows and follows each Model name"
    else:
        size = f"n = {decision_n(rows[0].scores)} per row, the {scored} scored"
    parts = [
        f"{TASK_TITLES[task]} · {'; '.join(datasets)} · {size}; {_denominators(task)}",
        f"{interval_level(runs)} intervals in parentheses",
        f"{PRIMARY} = SPEC §12.8 primary metric; other columns are exploratory",
        "n/a = undefined (empty denominator)",
        f"{FLAG_BASELINE} = SPEC §14 baseline",
        "API $/1k decisions = API cost per 1,000 of the configuration's decisions over every "
        "task and repeat, not this task alone: metered (usage × the dated pricing table) or "
        "billed by a gateway, $0.00 when unmetered (an endpoint that charged nothing per call, "
        "which is not a price); never a CLI's estimate",
    ]
    tags = {tag for row in rows if (tag := api_cost_tag(row.ref.scores.usage)) is not None}
    parts.extend(note for tag, note in API_COST_TAG_NOTES.items() if tag in tags)
    cells = [row.cells() for row in rows]
    if any("(no interval:" in cell for row in cells for cell in row):
        parts.append("no interval = fewer than two resampled units carry the denominator")
    if any(fmt.collapsed(m) for row in rows for m in row.metrics()):
        parts.append(
            "an interval of zero width such as (0.0–0.0) = a collapsed bootstrap interval (no "
            "resample saw an event, or every one did), not evidence of certainty"
        )
    bounds = [
        b for r in rows for b in (r.scores.metrics.war.bound, r.scores.metrics.high_risk_war.bound)
    ]
    levels = sorted({round((1 - b.alpha) * 100) for b in bounds if b is not None})
    if levels:
        cp = " / ".join(str(level) for level in levels)
        parts.append(f"CP{cp} ≤ x, k/n = exact one-sided {cp}% bound with k events in n cases")
    if any(not r.scores.completeness.complete for r in runs):
        parts.append(f"{FLAG_INCOMPLETE} = incomplete run (missing cases scored INVALID)")
    if any(row.small_n or row.harmful_flip_small_n for row in rows):
        # In the task's decision words and without "gold", like :func:`_denominators`.
        approve = DECISION_NAMES[task][Label.A]
        parts.append(
            f"{FLAG_SMALL_N} = fewer than {small_n_limit(runs)} resampled units (the base cases "
            "or campaigns after a Model name; after a harmful flip rate, the twin families with "
            f"an adversarial pair where the oracle's decision isn't {approve})"
        )
    if any(row.ref.ceiling for row in rows):
        parts.append(
            f"{CEILING} = the oracle baseline, which answers with the oracle's decisions: a "
            "pipeline check, listed last"
        )
    if not any(row.ref.baseline for row in rows):
        parts.append("no SPEC §14 baseline was scored in these runs")
    if len({row.ref.run.run_id for row in rows}) > 1:
        parts.append(
            "rows come from more than one run: configurations from different runs were not "
            "compared (SPEC §12.8), so their relative order is untested"
        )
    if differences:
        parts.append(
            "Warning: rows from different runs were not measured on the same cases ("
            + "; ".join(differences)
            + ") and are not directly comparable"
        )
    if settings:
        parts.append(
            "Warning: rows from different runs were scored under different settings ("
            + "; ".join(settings)
            + "), so their NRCs, intervals or flags are not on one scale"
        )
    return " · ".join(parts) + "."


def leaderboard_table(
    runs: Iterable[RunInput], *, task: Task = Task.access_request, allow_mixed: bool = False
) -> str:
    """The README leaderboard for ``task`` as a Markdown table under a caption line.

    ``runs`` are scored run directories or loaded ``ReportRun``s. Raises ``ValueError`` when no
    configuration was scored on ``task``, and :class:`IncomparableRunsError` when the runs that
    scored it were not measured on the same cases of it or under the same loss matrix,
    interval level and small-n threshold, unless ``allow_mixed`` (then the caption says how
    they differ).
    """
    loaded = load_report_runs(runs, context=False)
    rows = leaderboard_rows(loaded, task)
    if not rows:
        raise ValueError(f"no configuration in these runs was scored on {task}")
    differences = task_differences(loaded, task)
    settings = settings_differences(loaded, task=task)
    if (differences or settings) and not allow_mixed:
        raise IncomparableRunsError(
            f"one {TASK_TITLES[task].lower()} table can't rank these runs: "
            f"{'; '.join([*differences, *settings])}; pass allow_mixed=True to rank them under "
            "a warning"
        )
    per_row_n = len({decision_n(row.scores) for row in rows}) > 1
    lines = [
        "| " + " | ".join(README_COLUMNS) + " |",
        "|" + "|".join(["---"] * 3 + ["---:"] * (len(README_COLUMNS) - 3)) + "|",
    ]
    for row in rows:
        name = row.display_label
        if per_row_n:
            name += f" (n = {decision_n(row.scores)})"
        if row.small_n:
            name += f" {FLAG_SMALL_N}"
        cells = (name, row.ref.prompt, row.ref.mode, *row.cells())
        lines.append("| " + " | ".join(escape_cell(c) for c in cells) + " |")
    caption = _caption(loaded, task, rows, differences, settings, per_row_n=per_row_n)
    return "\n".join([escape_cell(caption), "", *lines]) + "\n"


def _split_markers(text: str) -> tuple[str, str, str]:
    """``(before, block, after)`` around the markers, each marker kept in its outer part."""
    starts, ends = text.count(README_START), text.count(README_END)
    if starts != 1 or ends != 1:
        raise MarkerError(
            f"expected exactly one {README_START} and one {README_END}, found {starts} and {ends}"
        )
    start = text.index(README_START) + len(README_START)
    end = text.index(README_END)
    if end < start:
        raise MarkerError(f"{README_END} comes before {README_START}")
    return text[:start], text[start:end], text[end:]


def update_readme(readme_path: Path | str, table: str) -> bool:
    """Replace the README's leaderboard block with ``table``; return whether the file changed.

    Only the text between the markers changes, and the file is rewritten atomically (and only
    when its content changes, keeping its permissions and line endings), so a failed write
    never leaves a truncated README. Raises
    ``MarkerError`` when the markers are missing, repeated or out of order.
    """
    path = Path(readme_path)
    original = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    before, _, after = _split_markers(original)
    lines = table.replace("\r\n", "\n").strip("\n")
    block = newline + lines.replace("\n", newline) + newline
    updated = before + block + after
    if updated == original:
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
