"""Vega-Lite specs for the HTML report's interactive charts (SPEC §15, M7).

The HTML report loads Vega, Vega-Lite and Vega-Embed from a CDN at exact versions that have
been public for a long time (:data:`SCRIPTS`), each pinned by its Subresource Integrity hash
(:data:`INTEGRITY`), and renders each spec in place of the inline SVG it ships as a fallback.
Opened offline, or with scripts blocked (or altered, which the hash refuses), the page keeps
the SVG, so every chart is readable either way.

Specs are plain JSON-ready dicts with their data inline (no fetches, so the page works from
disk) and a tooltip on every mark. Colors match ``report.svg``: blue then orange for series,
a single-hue blue scheme for magnitudes. Chrome colors are mid-grays that read on light and
dark pages alike; text color is left to the embed theme the page picks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from identitybench.report.svg import war_target_label

__all__ = [
    "INTEGRITY",
    "SCHEMA",
    "SCRIPTS",
    "bar_spec",
    "calibration_spec",
    "heatmap_spec",
    "selective_spec",
]

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

SCRIPTS: tuple[str, ...] = (
    "https://cdn.jsdelivr.net/npm/vega@5.30.0/build/vega.min.js",
    "https://cdn.jsdelivr.net/npm/vega-lite@5.21.0/build/vega-lite.min.js",
    "https://cdn.jsdelivr.net/npm/vega-embed@6.26.0/build/vega-embed.min.js",
)
"""Exact, long-released versions: a report must render the same next year as today."""

INTEGRITY: Mapping[str, str] = {
    SCRIPTS[0]: "sha384-em7CHpJd+SsMugVFf6TY7AKQcLWMcbPhD84hmNK8o6WFDkK+2uHSUQRVQV1/w827",
    SCRIPTS[1]: "sha384-GhkD6ks9/zgY1m5EFOUZWz/vMVMUFF/92DL61RZc+B42J8osL+jNufKv68bNHHZ2",
    SCRIPTS[2]: "sha384-TqXb8su49m5OnEpKGO8m+VrgHesrUxyP22HgpXi4hnh1Hm43dXroiSYemNf5D8lv",
}
"""Subresource Integrity hash of each pinned script, computed from the published file
(identical on jsDelivr and unpkg). The page loads each with ``integrity`` and
``crossorigin="anonymous"``, so a CDN that serves anything else is refused by the browser and
the inline SVG stays. Changing a version in :data:`SCRIPTS` means recomputing its hash:
``curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A``."""

_SERIES = ("#2a78d6", "#eb6834")
_BASELINE = "#898781"
_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

_CONFIG: Mapping[str, Any] = {
    "background": "transparent",
    "font": _FONT,
    "view": {"stroke": None},
    "axis": {
        "domainColor": "#c3c2b7",
        "gridColor": "#e1e0d9",
        "labelColor": "#898781",
        "tickColor": "#c3c2b7",
        "titleColor": "#52514e",
        "titleFontWeight": "normal",
    },
    "legend": {"labelColor": "#52514e", "titleColor": "#52514e"},
    "title": {"anchor": "start", "fontSize": 14, "fontWeight": 600},
}


def _spec(
    title: str, body: Mapping[str, Any], *, width: int = 640, subtitle: str | None = None
) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "title": title if subtitle is None else {"text": title, "subtitle": subtitle},
        "width": width,
        "config": dict(_CONFIG),
        **body,
    }


def heatmap_spec(
    cells: Sequence[Mapping[str, Any]], *, title: str, subtitle: str, n_title: str
) -> dict[str, Any]:
    """Accuracy by policy family × tier. ``cells`` have ``family``, ``tier``, ``accuracy``
    (None for n/a), ``n`` (titled ``n_title`` in the tooltip), ``items`` (review items, or
    None), ``small_n`` and ``n_label``, the cell's second line (``n=2†``), so the rendered
    chart shows every cell's n and small-n flag as the SVG fallback does; ``subtitle`` says
    what n counts and what † means. Family and tier order is the order given. An n/a cell gets
    no fill (Vega-Lite drops null values from a color scale) and reads "n/a"."""
    families = list(dict.fromkeys(c["family"] for c in cells))
    tiers = list(dict.fromkeys(c["tier"] for c in cells))
    encoding = {
        "x": {"field": "tier", "type": "ordinal", "sort": tiers, "title": "Tier"},
        "y": {"field": "family", "type": "ordinal", "sort": families, "title": "Policy family"},
    }
    tooltip = [
        {"field": "family", "title": "Family"},
        {"field": "tier", "title": "Tier"},
        {"field": "accuracy", "title": "Accuracy", "format": ".1%"},
        {"field": "n", "title": n_title},
        *(
            [{"field": "items", "title": "Review items"}]
            if any(c.get("items") is not None for c in cells)
            else []
        ),
        {"field": "small_n", "title": "Small n (†)"},
    ]
    ink = {
        "condition": {
            "test": "datum.accuracy !== null && datum.accuracy >= 0.45",
            "value": "#ffffff",
        },
        "value": "#0b0b0b",
    }
    return _spec(
        title,
        {
            "height": max(40 * len(families), 80),
            "data": {"values": [dict(c) for c in cells]},
            "encoding": encoding,
            "layer": [
                {
                    "mark": {"type": "rect", "stroke": "#fcfcfb", "strokeWidth": 2},
                    "encoding": {
                        "color": {
                            "field": "accuracy",
                            "type": "quantitative",
                            "scale": {"scheme": "blues", "domain": [0, 1]},
                            "title": "Accuracy",
                            "legend": {"format": ".0%"},
                        },
                        "tooltip": tooltip,
                    },
                },
                {
                    "mark": {"type": "text", "baseline": "middle", "dy": -7},
                    "encoding": {
                        "text": {
                            "condition": {"test": "datum.accuracy === null", "value": "n/a"},
                            "field": "accuracy",
                            "format": ".0%",
                        },
                        "color": ink,
                        "tooltip": tooltip,
                    },
                },
                {
                    "transform": [{"filter": "datum.accuracy !== null"}],
                    "mark": {"type": "text", "baseline": "middle", "dy": 8, "fontSize": 10},
                    "encoding": {
                        "text": {"field": "n_label"},
                        "color": ink,
                        "tooltip": tooltip,
                    },
                },
            ],
        },
        width=max(96 * len(tiers), 240),
        subtitle=subtitle,
    )


def selective_spec(
    points: Sequence[Mapping[str, Any]],
    *,
    title: str,
    target: float | None,
    operating: Mapping[str, Any] | None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    """WAR against coverage across thresholds, with ``subtitle`` as a caveat under the title;
    ``points`` have ``threshold``, ``coverage``, ``war`` and ``correct_automation``. The WAR
    target is a rule labeled at its right end with the SVG fallback's text
    (``svg.war_target_label``), so the line reads without its tooltip, and the operating point
    a larger orange mark; ``operating`` also carries ``label`` (drawn
    beside it: certified or empirical), ``status`` (in its tooltip), ``bound`` (the exact
    WAR upper bound, drawn as a dashed rule up from the point, or None) and ``bound_text`` (that
    bound as the status prints it against the target, for the rule's tooltip: a d3 format
    can't, since the precision that keeps a 2.004% bound from reading as a 2.0% target depends
    on both numbers)."""
    tooltip = [
        {"field": "threshold", "title": "Threshold", "format": ".3f"},
        {"field": "coverage", "title": "Coverage", "format": ".1%"},
        {"field": "war", "title": "WAR", "format": ".1%"},
        {"field": "correct_automation", "title": "Correct automation", "format": ".1%"},
    ]
    x = {
        "field": "coverage",
        "type": "quantitative",
        "title": "Coverage",
        "axis": {"format": "%"},
        "scale": {"domain": [0, 1]},
    }
    y = {"field": "war", "type": "quantitative", "title": "WAR", "axis": {"format": "%"}}
    layers: list[dict[str, Any]] = [
        {
            "data": {"values": [dict(p) for p in points]},
            "mark": {"type": "line", "point": True, "color": _SERIES[0], "strokeWidth": 2},
            "encoding": {
                "x": x,
                "y": y,
                "order": {"field": "coverage", "type": "quantitative"},
                "tooltip": tooltip,
            },
        }
    ]
    if target is not None:
        label = {"coverage": 1.0, "war": target, "label": war_target_label(target)}
        layers += [
            {
                "data": {"values": [{"target": target}]},
                "mark": {"type": "rule", "color": _BASELINE},
                "encoding": {
                    "y": {"field": "target", "type": "quantitative"},
                    # Exact, like the label: ".1%" would show a 1.25% target as 1.3%.
                    "tooltip": [{"field": "target", "title": "WAR target", "format": ".6~%"}],
                },
            },
            {
                # Placed with the curve's own x and y fields, so the layers share axis titles.
                "data": {"values": [label]},
                "mark": {
                    "type": "text",
                    "align": "right",
                    "baseline": "bottom",
                    "dy": -4,
                    "fontSize": 11,
                    "color": _BASELINE,
                },
                "encoding": {"x": x, "y": y, "text": {"field": "label"}},
            },
        ]
    if operating is not None:
        datum = {"values": [dict(operating)]}
        status = {"field": "status", "title": "Status"}
        if operating.get("bound") is not None:
            layers.append(
                {
                    "data": datum,
                    "mark": {"type": "rule", "color": _SERIES[1], "strokeDash": [4, 3]},
                    "encoding": {
                        "x": x,
                        "y": y,
                        "y2": {"field": "bound"},
                        "tooltip": [
                            {"field": "bound_text", "title": "Exact WAR upper bound"},
                            status,
                        ],
                    },
                }
            )
        layers += [
            {
                "data": datum,
                "mark": {"type": "point", "filled": True, "size": 140, "color": _SERIES[1]},
                "encoding": {"x": x, "y": y, "tooltip": [*tooltip, status]},
            },
            {
                "data": datum,
                "mark": {"type": "text", "align": "left", "dx": 10, "dy": -10, "fontSize": 11},
                "encoding": {"x": x, "y": y, "text": {"field": "label"}},
            },
        ]
    return _spec(title, {"height": 280, "layer": layers}, subtitle=subtitle)


def calibration_spec(
    bins: Sequence[Mapping[str, Any]], *, title: str, subtitle: str | None = None
) -> dict[str, Any]:
    """Reliability diagram; ``bins`` have ``confidence``, ``accuracy`` and ``n``; ``subtitle``
    is a caveat under the title. Both layers name their axes identically, since Vega-Lite
    joins differing layer titles."""

    def axis(field: str, title: str) -> dict[str, Any]:
        return {
            "field": field,
            "type": "quantitative",
            "title": title,
            "axis": {"format": "%"},
            "scale": {"domain": [0, 1]},
        }

    return _spec(
        title,
        {
            "height": 280,
            "layer": [
                {
                    "data": {"values": [{"v": 0.0}, {"v": 1.0}]},
                    "mark": {"type": "line", "color": _BASELINE, "strokeWidth": 1},
                    "encoding": {
                        "x": axis("v", "Mean confidence"),
                        "y": axis("v", "Accuracy"),
                    },
                },
                {
                    "data": {"values": [dict(b) for b in bins]},
                    "mark": {"type": "line", "point": True, "color": _SERIES[0], "strokeWidth": 2},
                    "encoding": {
                        "x": axis("confidence", "Mean confidence"),
                        "y": axis("accuracy", "Accuracy"),
                        "tooltip": [
                            {"field": "confidence", "title": "Mean confidence", "format": ".1%"},
                            {"field": "accuracy", "title": "Accuracy", "format": ".1%"},
                            {"field": "n", "title": "n"},
                        ],
                    },
                },
            ],
        },
        subtitle=subtitle,
    )


def bar_spec(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str,
    axis_title: str,
    value_format: str,
    series: Sequence[str] = (),
) -> dict[str, Any]:
    """Horizontal bars in the row order given. Rows have ``name`` and ``value``; with
    ``series``, rows are long-format with a ``series`` field and bars are grouped and colored
    by it; without, ``baseline`` colors baselines gray.

    A row whose value is None (n/a: a subscription-CLI model has no API cost) keeps its place:
    Vega-Lite drops null values from a quantitative encoding, so the bars filter them out, the
    y domain is pinned to every name, and a text layer writes "n/a" where the bar would be, as
    the SVG fallback does."""
    names = list(dict.fromkeys(r["name"] for r in rows))
    y: dict[str, Any] = {
        "field": "name",
        "type": "nominal",
        "sort": names,
        "title": None,
        "scale": {"domain": names},
    }
    x = {
        "field": "value",
        "type": "quantitative",
        "title": axis_title,
        "axis": {"format": value_format},
    }
    bar: dict[str, Any] = {
        "x": x,
        "tooltip": [
            {"field": "name", "title": "Configuration"},
            *([{"field": "series", "title": "Series"}] if series else []),
            {"field": "value", "title": axis_title, "format": value_format},
        ],
    }
    shared: dict[str, Any] = {"y": y}
    if series:
        shared["yOffset"] = {"field": "series", "sort": list(series)}
        bar["color"] = {
            "field": "series",
            "type": "nominal",
            "sort": list(series),
            "scale": {"domain": list(series), "range": list(_SERIES[: len(series)])},
            "title": None,
        }
    else:
        bar["color"] = {
            "condition": {"test": "datum.baseline", "value": _BASELINE},
            "value": _SERIES[0],
        }
    per_row = 22 * max(len(series), 1) + 10
    return _spec(
        title,
        {
            "height": max(per_row * len(names), 60),
            "data": {"values": [dict(r) for r in rows]},
            "encoding": shared,
            "layer": [
                {
                    "transform": [{"filter": "datum.value !== null"}],
                    "mark": {
                        "type": "bar",
                        "cornerRadiusEnd": 4,
                        **({} if series else {"height": 16}),
                    },
                    "encoding": bar,
                },
                {
                    "transform": [{"filter": "datum.value === null"}],
                    "mark": {"type": "text", "align": "left", "dx": 4, "color": _BASELINE},
                    "encoding": {
                        "x": {"datum": 0, "type": "quantitative"},
                        "text": {"value": "n/a"},
                        "tooltip": [
                            {"field": "name", "title": "Configuration"},
                            *([{"field": "series", "title": "Series"}] if series else []),
                        ],
                    },
                },
            ],
        },
    )
