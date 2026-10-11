"""Number formatting: n/a for undefined values, intervals, exact bounds, money and time."""

from __future__ import annotations

from identitybench.report import format as fmt
from identitybench.scoring.metrics import Bound, Metric


def _metric(
    point: float | None, low: float | None, high: float | None, bound: Bound | None = None
) -> Metric:
    return Metric(
        point=point,
        low=low,
        high=high,
        numerator=1.0,
        denominator=4.0 if point is not None else 0.0,
        n_units=4,
        n_clusters=4,
        units="base request case",
        cluster=None,
        method="bootstrap_iid",
        alpha=0.05,
        bound=bound,
    )


def _bound(k: int, n: int, upper: float) -> Bound:
    return Bound(
        estimand="WAR",
        units="base request case",
        failure_event="x",
        k=k,
        n=n,
        upper=upper,
        alpha=0.05,
    )


def test_undefined_values_are_na_never_zero() -> None:
    assert fmt.pct(None) == "n/a"
    assert fmt.number(None) == "n/a"
    assert fmt.usd(None) == "n/a"
    assert fmt.seconds(None) == "n/a"
    assert fmt.interval(None, 0.1, 0.2) == "n/a"
    assert fmt.metric(None) == "n/a"
    assert fmt.metric(_metric(None, None, None, _bound(0, 1, 0.95))) == "n/a"
    assert fmt.pp(None, None, None) == "n/a"


def test_rates_and_intervals() -> None:
    assert fmt.pct(0.1234) == "12.3%"
    assert fmt.pct(0.5, 0) == "50%"
    assert fmt.interval(0.25, 0.1, 0.4) == "25.0% (10.0–40.0)"
    assert fmt.interval(0.25, None, None) == "25.0%"
    assert fmt.interval(0.842, 0.71, 0.98, percent=False) == "0.842 (0.710–0.980)"
    assert fmt.interval(1.5, 1.0, 2.0, percent=False, digits=2) == "1.50 (1.00–2.00)"


def test_a_metric_states_its_exact_bound_with_counts() -> None:
    text = fmt.metric(_metric(0.0, 0.0, 0.0, _bound(0, 73, 0.0402)))
    assert text == "0.0% (0.0–0.0) (CP95 ≤ 4.0%, 0/73)"
    assert fmt.metric(_metric(0.0, 0.0, 0.0, _bound(0, 73, 0.0402)), show_bound=False) == (
        "0.0% (0.0–0.0)"
    )
    assert fmt.bound(_bound(2, 10, 0.5)) == "CP95 ≤ 50.0%, 2/10"


def test_differences_in_percentage_points() -> None:
    assert fmt.pp(0.032, -0.01, 0.074) == "+3.2 pp (-1.0 to +7.4)"
    assert fmt.pp(-0.05, None, None) == "-5.0 pp"


def test_money_keeps_small_costs_visible() -> None:
    assert fmt.usd(0.0) == "$0.00"
    assert fmt.usd(0.0042) == "$0.0042"
    assert fmt.usd(6.75) == "$6.75"
    assert fmt.usd(1234.5) == "$1,234.50"
    assert fmt.seconds(2.345) == "2.35 s"


def test_a_metric_without_an_interval_names_its_n() -> None:
    lone = _metric(0.0, None, None).model_copy(
        update={"denominator": 1.0, "units": "request twin pair"}
    )
    assert fmt.metric(lone) == "0.0% (no interval: 1 request twin pair)"
    pair = lone.model_copy(update={"denominator": 3.0, "units": "family"})
    assert fmt.metric(pair) == "0.0% (no interval: 3 families)"
    bounded = lone.model_copy(update={"bound": _bound(0, 1, 0.95)})
    assert fmt.metric(bounded) == "0.0% (no interval: 1 request twin pair) (CP95 ≤ 95.0%, 0/1)"


def test_counts_and_units() -> None:
    assert fmt.count(3.0) == "3"
    assert fmt.count(2.5) == "2.50"
    assert fmt.units(1, "campaign") == "1 campaign"
    assert fmt.units(8, "base request case") == "8 base request cases"
    assert fmt.units(2, "family") == "2 families"
    assert fmt.units(2, "day") == "2 days"
    assert fmt.units(2.5, "JML event") == "2.50 JML events"


def test_bound_notes_state_each_distinct_bound_once() -> None:
    """SPEC §12.8: what a table's bounds are on, verbatim from the scorer's records; n/a
    metrics carry no visible bound and add nothing."""
    war = Bound(
        estimand="WAR: share of gold non-APPROVE cases predicted APPROVE",
        units="base request case",
        failure_event="predicted APPROVE on a case whose gold isn't APPROVE",
        k=0,
        n=5,
        upper=0.45,
        alpha=0.05,
    )
    bounded = _metric(0.0, 0.0, 0.0, war)
    note = fmt.bound_notes([bounded, None, _metric(0.1, 0.0, 0.2), bounded])
    assert note == (
        "Exact bounds: CP95 bound on WAR: share of gold non-APPROVE cases predicted APPROVE; "
        "units: base request cases; failure event: predicted APPROVE on a case whose gold "
        "isn't APPROVE"
    )
    other = _metric(0.0, 0.0, 0.0, _bound(0, 9, 0.3))
    two = fmt.bound_notes([bounded, other])
    assert two is not None and two.count("CP95 bound on") == 2 and " · " in two
    assert fmt.bound_notes([_metric(None, None, None, war), _metric(0.1, 0.0, 0.2)]) is None


def test_a_percentage_against_a_reference_shows_which_side_it_is_on() -> None:
    """Enough decimals that the value doesn't read as equal to the reference: the operating
    point labels state a bound's side of the WAR target."""
    assert fmt.pct_against(0.451, 0.02) == "45.1%"
    assert fmt.pct_against(0.01977, 0.02) == "1.98%"
    assert fmt.pct_against(0.02004, 0.02) == "2.004%"
    assert fmt.pct_against(0.019996, 0.02) == "1.9996%"
    assert fmt.pct_against(0.02, 0.02) == "2.0%"
    assert fmt.pct_against(0.0200000001, 0.02, max_digits=3) == "2.000%"
    # A target with two decimals is compared at its own precision, never rounded to one.
    assert fmt.pct_against(0.0124, 0.0125) == "1.24%"
    assert fmt.pct_against(0.0126, 0.0125) == "1.26%"
    assert fmt.pct_against(0.01251, 0.0125) == "1.251%"
    assert fmt.pct_against(0.0125, 0.0125) == "1.25%"


def test_a_target_prints_exactly() -> None:
    """The fewest decimals (at least ``digits``) that read back as the value: half-even
    rounding would print 1.25% as 1.2% and 0.25% as 0.2%."""
    assert fmt.pct_exact(0.02) == "2.0%"
    assert fmt.pct_exact(0.02, digits=0) == "2%"
    assert fmt.pct_exact(0.0125) == "1.25%"
    assert fmt.pct_exact(0.0025, digits=0) == "0.25%"
    assert fmt.pct_exact(0.07) == "7.0%"  # 0.07 * 100 is 7.000000000000001
    assert fmt.pct_exact(1 / 3) == "33.333333%"  # capped at max_digits


def test_a_bound_note_lists_each_distinct_bound_once() -> None:
    entry = (95, "WAR", "base request case", "x")
    assert fmt.bound_note([]) is None
    assert fmt.bound_note([entry, entry]) == (
        "Exact bounds: CP95 bound on WAR; units: base request cases; failure event: x"
    )
