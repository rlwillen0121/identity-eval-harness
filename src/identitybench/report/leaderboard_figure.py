"""The README's thesis graphic: the leaderboard's finding as one picture (SPEC §15: README
charts are SVG exports).

It is built like a chart that would hold its own on a data-visualization forum's front page:
one idea, one chart form, the title stating the finding, the picture showing why.

- **A large, plain title that is the finding** ("Only 4 of 20 AI models cleared the safety
  bar"), then a line under it that finishes the sentence, the model it names set in the accent
  color. The title is at least 2.5 times the size of the row labels.
- **One metric, one bar per model**: a horizontal bar on 0-100 (the README gives grounded
  accuracy), its value printed at the bar's tip, inside the bar when it fits and just past it
  when it doesn't. The first bar's value carries the metric's name ("grounded accuracy 99.7%"),
  so no axis, tick, legend or header line is needed; when it can't, a small head over the bars
  names it instead.
- **Groups, each under a big label** ("Clear the bar", "Miss the bar"), drawn in the order
  given. The group marked ``accent`` (the models that clear the safety bar) has its bars and
  its label in the theme's one accent color; every other bar is charcoal (a mid gray in the dark
  theme). The color is never the only cue: the label says which group a row is in.
- **The accent group's range as a band** behind every group's rows, in a pale tint of the
  accent, with a bracket and a caption under the rows (``band``: what the band is, and what it
  shows, such as a model that misses the bar inside it). It is what makes the bars say
  something the title doesn't: a model can be as accurate as those that clear the bar and still
  miss it.
- **A short gray note at each row's end**, in a column of its own whose head sits on the group
  label's line: what the caller says about the row (the README gives the cost for the models
  that clear the bar and the criterion each other model misses). A note marked ``strong`` is
  set in the main ink (the cheapest model that clears the bar).
- **Minimal chrome**: no grid, axis, box, stripe or badge; generous space between groups;
  the source and method in a few lines of small print at the bottom.

Every value is the text the README's tables print, given by the caller; the figure only places
it. The ``<desc>`` states every row's value and note. Text is laid out with
``figure_kit.text_width``; the title, subtitle, caption and small print are kept to
:data:`_FIT` of the width, so a wide fallback face (DejaVu Sans, Verdana) still fits the canvas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from identitybench.report.figure_kit import Canvas, Theme, balanced, text_width, truncate

__all__ = ["WIDTH", "ThesisGroup", "ThesisRow", "headline", "thesis_svg"]

WIDTH = 820.0
_PAD = 24.0
_FIT = 0.88
"""The share of the inner width the title, subtitle, caption and small print may fill: room for
a fallback face up to about 25% wider than the Helvetica widths the estimate uses."""
_TITLE, _SUBTITLE, _GROUP, _NAME, _DISCLOSE, _VALUE, _NOTE, _HEAD, _CAPTION, _SOURCE = (
    46.0,
    20.0,
    26.0,
    18.0,
    13.0,
    15.0,
    15.0,
    13.5,
    14.5,
    13.0,
)
"""Type sizes (px). The title is 2.5 times the row labels (and still well over GitHub's 16 px
body text once GitHub scales the 820 px figure down to its README column, about 700-790 px);
nothing is set below 13 px for the same reason."""
_TITLE_LEAD, _SUBTITLE_LEAD, _CAPTION_LEAD, _SOURCE_LEAD = 52.0, 27.0, 20.0, 18.5
"""Baseline to baseline within the title, the subtitle, the caption and the small print."""
_SUBTITLE_GAP = 52.0
"""From the title's last baseline to the subtitle's first."""
_ROW = 34.0
"""The row pitch: a bar and the air above and below it."""
_TALL = 8.0
"""The extra pitch of a row whose label has a disclosure on a second line ("self-hosted"),
so two such rows in a row don't run together."""
_BAR = 23.0
_TIP = 4.0
"""The rounded radius of a bar's data end (its baseline end is square)."""
_NAME_GAP = 12.0
"""Between a row's label and its bar."""
_NOTE_GAP = 24.0
"""Between the 0-100 scale's end and the note column."""
_INSET = 8.0
"""A value printed inside its bar: this far in from the tip, and the room it needs on its other
side to go inside at all."""
_NAME_MAX = 250.0
_NOTE_MAX = 310.0
_BAR_MIN = 230.0
"""The widest a row's label and note may grow, and the shortest the 0-100 scale may shrink:
together with the gaps they fit the canvas whatever the names and notes (a note is shortened
before the scale is)."""


@dataclass(frozen=True, slots=True)
class ThesisRow:
    """One model. ``value`` is the bar's length in percentage points (``93.3``), as ``text``
    prints it, or None (no bar, ``text`` alone in muted ink); ``note`` the words at the row's
    end; ``strong`` sets the note in the main ink."""

    name: str
    value: float | None
    text: str
    note: str = ""
    strong: bool = False


@dataclass(frozen=True, slots=True)
class ThesisGroup:
    """Rows under one big ``label`` ("Clear the bar"), drawn in the order given, which
    ``order`` states for the description ("by grounded accuracy"). ``accent`` draws the label
    and the bars in the theme's accent (and gives the band its range); ``note_head`` heads the
    note column on the label's line ("cost per 1,000 decisions"); ``verdict`` is what the label
    says of each row, for the description."""

    label: str
    rows: tuple[ThesisRow, ...]
    accent: bool = False
    note_head: str = ""
    order: str = ""
    verdict: str = ""


def headline(title: str, subtitle: str) -> str:
    """The title and the line under it as one sentence (for the figure's accessible name and
    alt text): joined by a space when the line continues the title ("on access decisions..."),
    else as two sentences."""
    if not subtitle:
        return title
    return f"{title} {subtitle}" if subtitle[:1].islower() else f"{title}. {subtitle}"


def _split_name(name: str) -> tuple[str, str | None]:
    """A display name's model and its parenthetical disclosure: ("Qwen3.8-27B", "4-bit,
    self-hosted"); a trailing marker (``†``, ``‡``) stays on the model's name."""
    base, paren, rest = name.partition(" (")
    core = rest.rstrip(" †‡")
    if not paren or not core.endswith(")"):
        return name, None
    return base + rest[len(core) :], core[:-1]


def _label_width(row: ThesisRow) -> float:
    """The width a row's label needs: its name, and any disclosure on the line under it."""
    name, note = _split_name(row.name)
    return max(text_width(name, _NAME), text_width(note, _DISCLOSE) if note else 0.0)


def _pitch(row: ThesisRow) -> float:
    return _ROW + (_TALL if _split_name(row.name)[1] else 0.0)


def _words(text: str) -> list[str]:
    """``text`` split at its spaces; a no-break space (U+00A0) holds its words together."""
    return [w for w in text.split(" ") if w]


def _tokens(text: str, keep: str) -> list[str]:
    """``text`` split into words, ``keep`` (a phrase in it) as one token with any punctuation
    that touches it, so a line never breaks inside it."""
    if not keep or keep not in text:
        return _words(text)
    head, _, tail = text.partition(keep)
    before, after = _words(head), _words(tail)
    token = keep
    if before and not head.endswith(" "):
        token = before.pop() + token
    if after and not tail.startswith(" "):
        token += after.pop(0)
    return [*before, token, *after]


def _greedy(tokens: Sequence[str], width: float, size: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}" if current else token
        if current and text_width(candidate, size) > width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _lines(text: str, keep: str, width: float, size: float) -> list[str]:
    """``text`` wrapped at ``width`` without breaking ``keep``, into as few lines as fit, as
    even as they can be (so the last never holds an orphaned word or two)."""
    tokens = _tokens(text, keep)
    lines = _greedy(tokens, width, size)
    trial = width - 8.0
    while len(lines) > 1 and trial > width / 2:
        narrower = _greedy(tokens, trial, size)
        if len(narrower) > len(lines):
            break
        lines, trial = narrower, trial - 8.0
    return lines


def _small_print(text: str, width: float) -> list[str]:
    """A paragraph of small print in even lines; a source line (parts joined by `` · ``)
    breaks only between its parts."""
    if " · " not in text:
        return balanced(text, width, _SOURCE)
    lines: list[str] = []
    for part in text.split(" · "):
        candidate = f"{lines[-1]} · {part}" if lines else part
        if lines and text_width(candidate, _SOURCE) <= width:
            lines[-1] = candidate
        else:
            lines.append(part)
    return lines


def _bar_path(x: float, y: float, w: float, h: float) -> str:
    """A bar from ``x``, its data end rounded and its baseline end square."""
    r = min(_TIP, w / 2, h / 2)
    x1 = x + w
    return (
        f"M{x:.1f},{y:.1f}H{x1 - r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}"
        f"V{y + h - r:.1f}Q{x1:.1f},{y + h:.1f} {x1 - r:.1f},{y + h:.1f}H{x:.1f}Z"
    )


def thesis_svg(
    groups: Sequence[ThesisGroup],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    metric: str,
    source: Sequence[str],
    highlight: str = "",
    band: str = "",
    key: str = "identitybench-leaderboard",
) -> str:
    """The thesis graphic for ``groups`` (in their order; a group without rows is left out).
    ``title`` is the finding, ``subtitle`` the line under it, ``highlight`` a phrase of the
    subtitle set in the accent color, ``metric`` what the bars measure, in a few words
    ("grounded accuracy", printed with the first bar's value), ``band`` the caption of the
    accent group's range drawn behind the rows (no band without it), ``source`` the small print
    (a paragraph each)."""
    canvas = Canvas(WIDTH, theme)
    inner = WIDTH - 2 * _PAD
    fit = inner * _FIT
    shown = [g for g in groups if g.rows]
    rows = [r for g in shown for r in g.rows]

    # ---- Columns ------------------------------------------------------------------------
    label_w = min(max([_label_width(r) for r in rows] + [0.0]), _NAME_MAX)
    notes = [text_width(r.note, _NOTE, bold=r.strong) for r in rows if r.note]
    heads = [text_width(g.note_head, _HEAD) for g in shown if g.note_head]
    room = inner - label_w - _NAME_GAP - _BAR_MIN - _NOTE_GAP
    note_w = min(max([*notes, *heads, 0.0]), _NOTE_MAX, room)
    bar_x = _PAD + label_w + _NAME_GAP
    bar_w = inner - label_w - _NAME_GAP - (_NOTE_GAP + note_w if note_w > 0 else 0.0)
    note_x = bar_x + bar_w + _NOTE_GAP

    def length(value: float) -> float:
        return bar_w * min(max(value / 100.0, 0.0), 1.0)

    # The first bar's value names the metric, inside the bar or just past it; when neither
    # fits, a head over the bars does.
    first = next((r for r in rows if r.value is not None), None)
    named = f"{metric} " if metric else ""
    placed = ""
    if first is not None and first.value is not None and named:
        w = length(first.value)
        need = text_width(named, _VALUE) + text_width(f"{first.text}%", _VALUE, bold=True)
        if w >= need + 2 * _INSET:
            placed = "inside"
        elif w + 5.0 + need <= bar_w + _NOTE_GAP - 8.0:
            placed = "past"

    # ---- Title and subtitle -------------------------------------------------------------
    y = _PAD + _TITLE * 0.8
    for line in balanced(title, fit, _TITLE, bold=True):
        canvas.text(_PAD, y, line, size=_TITLE, weight=700, extra=' class="title"')
        y += _TITLE_LEAD
    y += _SUBTITLE_GAP - _TITLE_LEAD
    accent = canvas.color("safe")
    for line in _lines(subtitle, highlight, fit, _SUBTITLE) if subtitle else []:
        if highlight and highlight in line:
            head, _, tail = line.partition(highlight)
            canvas.parts.append(
                f'<text x="{_PAD:.1f}" y="{y:.1f}" font-size="{_SUBTITLE:g}" text-anchor="start" '
                f'fill="{theme.ink2}">{escape(head)}<tspan font-weight="700" fill="{accent}">'
                f"{escape(highlight)}</tspan>{escape(tail)}</text>"
            )
        else:
            canvas.text(_PAD, y, line, size=_SUBTITLE, fill="ink2")
        y += _SUBTITLE_LEAD
    y -= _SUBTITLE_LEAD
    if named and not placed:  # the metric as a head over the bars
        y += 34.0
        canvas.text(bar_x, y, f"{metric} (%)", size=_HEAD, fill="muted", extra=' class="metric"')

    # ---- Rows ---------------------------------------------------------------------------
    def value_text(row: ThesisRow, x: float, baseline: float, fill: str, anchor: str) -> None:
        if row is not first or not placed:
            canvas.text(x, baseline, row.text, size=_VALUE, fill=fill, anchor=anchor, weight=700)
            return
        canvas.parts.append(
            f'<text x="{x:.1f}" y="{baseline:.1f}" font-size="{_VALUE:g}" font-weight="700" '
            f'text-anchor="{anchor}" fill="{canvas.color(fill)}" class="named">'
            f'<tspan font-weight="400">{escape(named)}</tspan>{escape(row.text)}%</text>'
        )

    def draw_row(row: ThesisRow, top: float, accented: bool) -> None:
        cy = top + _pitch(row) / 2
        name, disclosure = _split_name(row.name)
        right = bar_x - _NAME_GAP
        if disclosure:  # the name and, on the line under it, its disclosure
            canvas.text(right, cy - 2.0, truncate(name, label_w, _NAME), size=_NAME, anchor="end")
            canvas.text(
                right,
                cy + 13.5,
                truncate(disclosure, label_w, _DISCLOSE),
                size=_DISCLOSE,
                fill="muted",
                anchor="end",
            )
        else:
            canvas.text(
                right, cy + _NAME * 0.34, truncate(name, label_w, _NAME), size=_NAME, anchor="end"
            )
        baseline = cy + _VALUE * 0.35
        if row.value is None:
            canvas.text(bar_x, baseline, row.text, size=_VALUE, fill="muted")
        else:
            w = length(row.value)
            fill = "safe" if accented else "bar"
            if w > 0:
                canvas.parts.append(
                    f'<path d="{_bar_path(bar_x, cy - _BAR / 2, w, _BAR)}" '
                    f'fill="{canvas.color(fill)}" class="bar"/>'
                )
            inside = (
                placed == "inside"
                if row is first and placed
                else w >= text_width(row.text, _VALUE, bold=True) + 2 * _INSET
            )
            if inside:
                value_text(row, bar_x + w - _INSET, baseline, f"{fill}_ink", "end")
            else:
                value_text(row, bar_x + w + 5.0, baseline, "ink", "start")
        if row.note:
            canvas.text(
                note_x,
                cy + _NOTE * 0.35,
                truncate(row.note, note_w, _NOTE, bold=row.strong),
                size=_NOTE,
                fill="ink" if row.strong else "muted",
                weight=600 if row.strong else 400,
                extra=' class="note"',
            )

    spans: list[tuple[float, float]] = []
    gap = 50.0
    for group in shown:
        y += gap
        gap = 58.0
        canvas.text(
            bar_x,
            y,
            group.label,
            size=_GROUP,
            fill="safe" if group.accent else "ink",
            weight=700,
            extra=' class="group"',
        )
        if group.note_head:
            canvas.text(
                note_x, y, truncate(group.note_head, note_w, _HEAD), size=_HEAD, fill="muted"
            )
        y += 10.0
        top = y
        for row in group.rows:
            draw_row(row, y, group.accent)
            y += _pitch(row)
        spans.append((top, y))

    # ---- The band: the accent group's range, behind every group's rows ------------------
    ranged = [r.value for g in shown if g.accent for r in g.rows if r.value is not None]
    bracket = None
    if band and ranged:
        x0, x1 = bar_x + length(min(ranged)), bar_x + length(max(ranged))
        if x1 - x0 < 3.0:  # one model, or one value: a narrow strip centered on it
            x0, x1 = (x0 + x1) / 2 - 1.5, (x0 + x1) / 2 + 1.5
        canvas.parts[:0] = [  # behind everything drawn so far
            f'<rect x="{x0:.1f}" y="{a:.1f}" width="{x1 - x0:.1f}" height="{b - a:.1f}" '
            f'fill="{theme.safe_band}" class="band"/>'
            for a, b in spans
        ]
        bracket = (x0, x1)

    if bracket is not None:
        x0, x1 = bracket
        y += 10.0
        canvas.path(
            f"M{x0:.1f},{y - 7.0:.1f}V{y:.1f}H{x1:.1f}V{y - 7.0:.1f}",
            stroke="muted",
            width=1.5,
            extra=' class="bracket"',
        )
        end = x1 - _PAD >= 0.45 * inner
        width = min(x1 - _PAD, fit) if end else min(WIDTH - _PAD - x0, fit)
        y += 6.0
        for line in balanced(band, width, _CAPTION):
            y += _CAPTION_LEAD
            canvas.text(
                x1 if end else x0,
                y,
                line,
                size=_CAPTION,
                fill="ink2",
                anchor="end" if end else "start",
                extra=' class="caption"',
            )

    # ---- Source -------------------------------------------------------------------------
    y += 36.0
    for text in source:
        for line in _small_print(text, fit):
            canvas.text(_PAD, y, line, size=_SOURCE, fill="muted")
            y += _SOURCE_LEAD
    height = y - _SOURCE_LEAD + _PAD

    def described(row: ThesisRow, group: ThesisGroup) -> str:
        parts = [group.verdict] if group.verdict else []
        parts.append(f"{metric} {row.text}" + ("%" if row.value is not None else ""))
        if row.note:
            parts.append(f"{group.note_head}: {row.note}" if group.note_head else row.note)
        return f"{row.name}: " + ", ".join(parts)

    noun = "model" if len(rows) == 1 else "models"
    joined = headline(title, subtitle)
    desc = (
        f"Bar chart of {metric} for {len(rows)} {noun}, in {len(shown)} "
        f"group{'s' if len(shown) != 1 else ''}. {joined.rstrip('.')}. "
        + (f"{band} " if bracket is not None else "")
        + " ".join(
            f"{group.label}"
            + (f" ({group.order})" if group.order else "")
            + ": "
            + "; ".join(described(r, group) for r in group.rows)
            + "."
            for group in shown
        )
        + f" {' '.join(source)}"
    )
    return canvas.render(height, title=joined, desc=desc, key=key)
