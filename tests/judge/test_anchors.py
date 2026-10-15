"""Anchor pairs (SPEC §13.3): oracle rationales against deliberately defective copies."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from identitybench.judge.anchors import (
    ANCHOR_DIMENSIONS,
    GOOD_CONFIG,
    AnchorKind,
    build_anchor_pairs,
    corrupt,
    oracle_targets,
)
from identitybench.judge.families import BASELINE_FAMILY
from identitybench.judge.targets import output_for
from identitybench.runner.dataset import read_jsonl
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Task

DATASET = Path(__file__).resolve().parents[2] / "datasets" / "identitybench-v1"


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return [
        case
        for suite in ("requests", "reviews", "jml")
        for case in read_jsonl(DATASET / f"{suite}.dev.jsonl")
    ]


def test_good_rationales_are_the_oracles(cases: list[Case]) -> None:
    goods = oracle_targets(cases)
    assert goods and all(t.correct for t in goods)
    assert all(t.output_family == BASELINE_FAMILY for t in goods)
    assert all(t.prediction.config_id == GOOD_CONFIG for t in goods)
    assert {t.case.task for t in goods} == set(Task)


def test_pairs_are_deterministic_balanced_and_differ_only_in_prose(cases: list[Case]) -> None:
    pairs = build_anchor_pairs(cases, per_kind=9, seed=1)
    again = build_anchor_pairs(cases, per_kind=9, seed=1)
    assert [(p.anchor_id, p.bad.rationale) for p in pairs] == [
        (p.anchor_id, p.bad.rationale) for p in again
    ]
    counts = Counter(p.kind for p in pairs)
    assert set(counts) == set(AnchorKind) and all(n <= 9 for n in counts.values())
    tasks = {p.good.case.task for p in pairs if p.kind is AnchorKind.wrong_fact}
    assert tasks == set(Task)  # tasks are taken in turn
    assert {p.good.case.task for p in pairs if p.kind is AnchorKind.missing_duration} == {
        Task.access_request
    }
    for pair in pairs:
        assert pair.dimensions == ANCHOR_DIMENSIONS[pair.kind]
        assert pair.good.gold.certificates  # never a floor-scored default outcome
        assert pair.good.rationale != pair.bad.rationale
        assert pair.bad_target_id != pair.good_target_id
        good_out = output_for(pair.good.prediction, pair.good.item_id).model_dump()
        bad_out = output_for(pair.bad.prediction, pair.bad.item_id).model_dump()
        del good_out["rationale"], bad_out["rationale"]
        assert good_out == bad_out


def test_each_defect_does_what_it_says(cases: list[Case]) -> None:
    goods = oracle_targets(cases)
    for good in goods:
        decisive = {c.rule for c in good.gold.certificates}
        wrong_rule = corrupt(good, AnchorKind.wrong_rule)
        if wrong_rule is not None:
            named = set(re.findall(r"[A-Z]+-\d{3}", wrong_rule))
            assert not decisive & named
        wrong_fact = corrupt(good, AnchorKind.wrong_fact)
        if wrong_fact is not None:
            assert wrong_fact.split(" Facts: ")[0] == good.rationale.split(" Facts: ")[0]
        missing = corrupt(good, AnchorKind.missing_duration)
        if missing is not None:
            assert missing.startswith("APPROVE under ") and " days" not in missing.split(".")[0]
    assert all(corrupt(g, AnchorKind.wrong_rule) is None for g in goods if not g.gold.certificates)
