from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from identitybench import CANARY
from identitybench.runner.dataset import (
    _key_str,  # pyright: ignore[reportPrivateUsage]
    canonical_line,
    normalize_suite,
    portable_path,
    read_cases,
    read_jsonl,
    stratified_sample,
    subset_for_repeats,
    suite_file,
    verify_sha256sums,
    write_jsonl,
    write_sha256sums,
)
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Split, Suite, Task, Tier
from tests.runner._cases import make_case


def test_jsonl_roundtrip_canonical(tmp_path: Path) -> None:
    cases = [make_case("c_b"), make_case("c_a", tier=Tier.L3)]
    path = tmp_path / "requests.test.jsonl"
    assert write_jsonl(path, cases) == 2
    assert read_jsonl(path) == cases
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert raw.endswith("\n") and len(lines) == 2
    for line, case in zip(lines, cases, strict=True):
        obj = json.loads(line)
        assert obj["canary"] == CANARY
        assert line == json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert obj["case_id"] == case.case_id
    assert '"for":"SOD-002"' in lines[0]  # alias preserved on the wire
    assert "café" in raw  # UTF-8 kept, not \u-escaped
    # Byte-identical rewrite.
    write_jsonl(tmp_path / "again.jsonl", read_jsonl(path))
    assert (tmp_path / "again.jsonl").read_bytes() == path.read_bytes()


def test_write_jsonl_refuses_missing_canary(tmp_path: Path) -> None:
    bad = make_case("c_1").model_copy(update={"canary": "nope"})
    with pytest.raises(ValueError, match="canary"):
        write_jsonl(tmp_path / "x.jsonl", [bad])
    assert not (tmp_path / "x.jsonl").exists()


def test_write_jsonl_refuses_empty_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no cases"):
        write_jsonl(tmp_path / "twins.dev.jsonl", [])
    assert not (tmp_path / "twins.dev.jsonl").exists()


def test_read_jsonl_requires_canary_on_every_line(tmp_path: Path) -> None:
    good = canonical_line(make_case("c_1"))
    for bad in (
        json.loads(good) | {"canary": "nope"},
        {k: v for k, v in json.loads(good).items() if k != "canary"},
    ):
        path = tmp_path / "requests.test.jsonl"
        path.write_text(good + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"jsonl:2: case is missing the provenance canary"):
            read_jsonl(path)


def test_read_jsonl_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "requests.test.jsonl"
    write_jsonl(path, [make_case("c_1")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('\n{"case_id": "broken"}\n')
    with pytest.raises(ValueError, match=r"requests\.test\.jsonl:3"):
        read_jsonl(path)


def _build(root: Path) -> None:
    write_jsonl(
        suite_file(root, Suite.requests, Split.test),
        [make_case("r1"), make_case("r2", tier=Tier.L2)],
    )
    write_jsonl(suite_file(root, Suite.requests, Split.dev), [make_case("r3", split=Split.dev)])
    write_jsonl(
        suite_file(root, Suite.reviews, Split.test),
        [make_case("v1", task=Task.access_review, suite=Suite.reviews)],
    )
    write_jsonl(
        suite_file(root, Suite.jml, Split.test), [make_case("j1", task=Task.jml, suite=Suite.jml)]
    )
    write_jsonl(
        suite_file(root, "adversarial", "test"),
        [make_case("a1", suite=Suite.adversarial, tier=Tier.L4)],
    )
    (root / "worlds").mkdir()
    (root / "worlds" / "w_1.json").write_text("{}", encoding="utf-8")
    (root / "DATASET_CARD.md").write_text(f"# Card\n\n{CANARY}\n", encoding="utf-8")


def test_read_cases_filters(tmp_path: Path) -> None:
    _build(tmp_path)

    def ids(cases: list[Case]) -> list[str]:
        return [c.case_id for c in cases]

    assert ids(read_cases(tmp_path, split="test")) == ["r1", "r2", "v1", "j1", "a1"]
    assert ids(read_cases(tmp_path, split=Split.dev)) == ["r3"]
    assert ids(read_cases(tmp_path, split="test", suites=["access_request", "jml"])) == [
        "r1",
        "r2",
        "j1",
    ]
    assert ids(read_cases(tmp_path, split="test", tasks=[Task.access_request])) == [
        "r1",
        "r2",
        "a1",
    ]
    assert ids(read_cases(tmp_path, suites=["reviews"])) == ["v1"]


def test_read_cases_order_is_suite_then_split(tmp_path: Path) -> None:
    _build(tmp_path)
    assert [c.case_id for c in read_cases(tmp_path)] == ["r3", "r1", "r2", "v1", "j1", "a1"]


def test_read_cases_missing_and_mismatch(tmp_path: Path) -> None:
    _build(tmp_path)
    with pytest.raises(FileNotFoundError, match=r"twins\.test\.jsonl"):
        read_cases(tmp_path, split="test", suites=["twins"])
    assert read_cases(tmp_path, suites=["twins"]) == []
    with pytest.raises(FileNotFoundError):
        read_cases(tmp_path / "nope")
    write_jsonl(suite_file(tmp_path, Suite.twins, Split.test), [make_case("t1")])
    with pytest.raises(ValueError, match="expected twins/test"):
        read_cases(tmp_path, split="test", suites=["twins"])


def test_normalize_suite() -> None:
    assert normalize_suite("Access_Request") is Suite.requests
    assert normalize_suite(Suite.jml) is Suite.jml
    with pytest.raises(ValueError, match="unknown suite"):
        normalize_suite("onboarding")


def test_sha256sums_roundtrip_and_problems(tmp_path: Path) -> None:
    _build(tmp_path)
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")
    sums = write_sha256sums(tmp_path)
    lines = sums.read_text(encoding="utf-8").splitlines()
    rels = [line.split("  ", 1)[1] for line in lines]
    assert rels == sorted(rels)
    assert "worlds/w_1.json" in rels and "DATASET_CARD.md" in rels
    assert ".DS_Store" not in rels and "SHA256SUMS" not in rels
    assert verify_sha256sums(tmp_path) == []

    (tmp_path / "jml.test.jsonl").write_text("tampered\n", encoding="utf-8")
    (tmp_path / "worlds" / "w_1.json").unlink()
    (tmp_path / "extra.jsonl").write_text("{}\n", encoding="utf-8")
    with sums.open("a", encoding="utf-8") as fh:
        fh.write("not a checksum line\n")
        fh.write(f"{'0' * 64}  ../outside.txt\n")
    problems = verify_sha256sums(tmp_path)
    assert "checksum mismatch: jml.test.jsonl" in problems
    assert "missing file: worlds/w_1.json" in problems
    assert "unlisted file: extra.jsonl" in problems
    assert any("malformed line" in p for p in problems)
    assert any("unsafe path" in p for p in problems)
    assert len(problems) == 5


def test_verify_without_sums(tmp_path: Path) -> None:
    assert verify_sha256sums(tmp_path) == [f"missing SHA256SUMS in {tmp_path}"]


# --------------------------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------------------------


def _population() -> list[Case]:
    """200 cases: requests L1 100 / L2 50 / L3 30, reviews L1 20."""
    cases: list[Case] = []
    spec = [
        (Task.access_request, Tier.L1, 100),
        (Task.access_request, Tier.L2, 50),
        (Task.access_request, Tier.L3, 30),
        (Task.access_review, Tier.L1, 20),
    ]
    n = 0
    for task, tier, count in spec:
        suite = Suite.requests if task is Task.access_request else Suite.reviews
        for _ in range(count):
            cases.append(make_case(f"c_{n:04d}", task=task, suite=suite, tier=tier))
            n += 1
    return cases


def _strata_counts(cases: list[Case]) -> Counter[tuple[Task, Tier]]:
    return Counter((c.task, c.tier) for c in cases)


def test_stratified_sample_deterministic() -> None:
    cases = _population()
    a = stratified_sample(cases, 0.1, seed=7)
    b = stratified_sample(cases, 0.1, seed=7)
    assert [c.case_id for c in a] == [c.case_id for c in b]
    c = stratified_sample(cases, 0.1, seed=8)
    assert {x.case_id for x in a} != {x.case_id for x in c}
    # Selection does not depend on input order; output keeps input order.
    rev = stratified_sample(list(reversed(cases)), 0.1, seed=7)
    assert {x.case_id for x in rev} == {x.case_id for x in a}
    assert [x.case_id for x in rev] == [x.case_id for x in reversed(a)]


def test_stratified_sample_proportional() -> None:
    cases = _population()
    sample = stratified_sample(cases, 0.1, seed=1)
    assert len(sample) == 20
    assert _strata_counts(sample) == {
        (Task.access_request, Tier.L1): 10,
        (Task.access_request, Tier.L2): 5,
        (Task.access_request, Tier.L3): 3,
        (Task.access_review, Tier.L1): 2,
    }


@pytest.mark.parametrize("n", [1, 7, 33, 101, 199])
def test_stratified_count_within_one_of_quota(n: int) -> None:
    cases = _population()
    population = _strata_counts(cases)
    sample = stratified_sample(cases, n, seed=3)
    assert len(sample) == n
    assert len({c.case_id for c in sample}) == n
    got = _strata_counts(sample)
    for key, size in population.items():
        assert abs(got.get(key, 0) - n * size / len(cases)) < 1


def test_stratified_sample_edges() -> None:
    cases = _population()
    assert len(stratified_sample(cases, 1.0)) == 200
    assert len(stratified_sample(cases, 500)) == 200
    assert stratified_sample(cases, 0) == []
    assert stratified_sample([], 0.5) == []
    assert len(stratified_sample(cases, 0.001)) == 1
    with pytest.raises(ValueError):
        stratified_sample(cases, 1.5)
    with pytest.raises(ValueError):
        stratified_sample(cases, -1)
    with pytest.raises(TypeError):
        stratified_sample(cases, True)


def test_custom_strata() -> None:
    cases = _population()
    sample = stratified_sample(cases, 0.5, strata=lambda c: c.task, seed=0)
    assert Counter(c.task for c in sample) == {Task.access_request: 90, Task.access_review: 10}


_REPO_ROOT = Path(__file__).resolve().parents[2]

_HASH_SEED_SCRIPT = """
import json
from identitybench.runner.dataset import stratified_sample
from tests.runner.test_dataset import _population

cases = _population()
by_set = stratified_sample(
    cases, 0.2, strata=lambda c: frozenset({c.tier.value, "x", "requests"}), seed=1, unit="case"
)
by_dict = stratified_sample(
    cases, 0.2, strata=lambda c: {"tier": c.tier.value, "task": c.task.value, "z": 1}, seed=1
)
print(json.dumps([[c.case_id for c in by_set], [c.case_id for c in by_dict]]))
"""


def test_unordered_strata_same_under_every_hash_seed() -> None:
    """Set- and dict-valued stratum keys must not pick up PYTHONHASHSEED's iteration order."""
    outputs: set[str] = set()
    for hash_seed in ("1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        proc = subprocess.run(
            [sys.executable, "-c", _HASH_SEED_SCRIPT],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        outputs.add(proc.stdout)
    assert len(outputs) == 1
    by_set, by_dict = json.loads(outputs.pop())
    assert len(by_set) == 40 and len(by_dict) == 40
    here = stratified_sample(
        _population(),
        0.2,
        strata=lambda c: frozenset({c.tier.value, "x", "requests"}),
        seed=1,
        unit="case",
    )
    assert [c.case_id for c in here] == by_set


def test_stratum_keys_are_canonical() -> None:
    assert _key_str(frozenset({"b", "a", "c"})) == _key_str({"c", "a", "b"}) == '["a", "b", "c"]'
    assert _key_str({"y": 2, "x": 1}) == _key_str({"x": 1, "y": 2}) == '[["x", "1"], ["y", "2"]]'
    assert _key_str(["a", Tier.L1]) == _key_str(("a", Tier.L1)) == '["a", "L1"]'
    # Scalars keep the string form the pinned samples were drawn with.
    assert _key_str((Suite.requests, Task.access_request, Tier.L2)) == json.dumps(
        [str(Suite.requests), str(Task.access_request), str(Tier.L2)]
    )
    assert _key_str(date(2026, 1, 2)) == '"2026-01-02"'
    assert _key_str(None) == '"None"'
    with pytest.raises(TypeError, match="unsupported stratum key"):
        _key_str(object())
    with pytest.raises(TypeError):
        stratified_sample(_population(), 0.5, strata=lambda c: object())


def test_portable_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / "someone"
    monkeypatch.setenv("HOME", str(home))
    assert portable_path("datasets/identitybench-v1") == "datasets/identitybench-v1"
    assert portable_path(home / "data" / "x", base=home) == "data/x"
    assert portable_path(home / "data" / "x", base=tmp_path / "other") == "x"
    assert portable_path("~/data/x", base=home) == "data/x"
    assert portable_path("~/data/x", base=tmp_path / "other") == "x"
    # A base above the home directory would put the user name in the path.
    assert portable_path(home / "data" / "x", base=tmp_path) == "x"
    assert portable_path(tmp_path / "shared" / "x", base=tmp_path) == "shared/x"
    monkeypatch.chdir(tmp_path)
    assert portable_path(tmp_path / "shared" / "x") == "shared/x"
    # A symlinked file keeps its configured name rather than its target's.
    target = tmp_path / "real-binary-1.2.3"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "other" / "grok"
    link.parent.mkdir()
    link.symlink_to(target)
    assert portable_path(link, base=tmp_path / "elsewhere") == "grok"


def _v1_shaped() -> list[Case]:
    """300 base requests (L1/L2/L3), 100 twins and 80 adversarial copies pointing at bases."""
    bases = [
        make_case(f"r_{i:03d}", tier=Tier.L1 if i < 150 else Tier.L2 if i < 240 else Tier.L3)
        for i in range(300)
    ]
    twins = [
        make_case(f"t_{i:03d}", suite=Suite.twins, tier=b.tier, twin_of=b)
        for i, b in enumerate(bases)
        if i % 3 == 0
    ]
    adversarial = [
        make_case(f"a_{i:03d}", suite=Suite.adversarial, tier=Tier.L4, twin_of=b)
        for i, b in enumerate(bases)
        if i % 15 in (1, 2, 4, 7)
    ]
    return bases + twins + adversarial


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_family_sample_keeps_variants_with_their_base(seed: int) -> None:
    cases = _v1_shaped()
    sample = stratified_sample(cases, 0.1, seed=seed)
    ids = {c.case_id for c in sample}
    variants = [c for c in sample if c.twin_of is not None]
    assert variants
    assert all(v.twin_of in ids for v in variants)
    # A drawn family comes whole: every variant of a sampled base is in the sample.
    assert all(c.case_id in ids for c in cases if c.twin_of in ids)
    # 30 of 300 families; family composition is a stratum, so variant counts are exact too.
    assert Counter(c.suite for c in sample) == {
        Suite.requests: 30,
        Suite.twins: 10,
        Suite.adversarial: 8,
    }


def test_family_sample_without_bases_in_population() -> None:
    twins_only = [c for c in _v1_shaped() if c.suite is Suite.twins]
    sample = stratified_sample(twins_only, 0.2, seed=0)
    assert len(sample) == 20
    assert all(c.suite is Suite.twins for c in sample)


def test_sample_pinned_output() -> None:
    """Pins the draw: a change here silently changes which cases every smoke run uses."""
    small = [make_case(f"c_{i:02d}", tier=(Tier.L1, Tier.L2)[i % 2]) for i in range(12)]
    twins = [
        make_case(f"tw_{i:02d}", suite=Suite.twins, tier=small[i].tier, twin_of=small[i])
        for i in (0, 3, 5)
    ]
    cases = small + twins
    assert [c.case_id for c in stratified_sample(cases, 0.5, seed=42)] == [
        "c_01",
        "c_02",
        "c_04",
        "c_05",
        "c_07",
        "c_10",
        "tw_05",
    ]
    assert [c.case_id for c in stratified_sample(cases, 4, seed=42, unit="case")] == [
        "c_00",
        "c_01",
        "c_02",
        "c_03",
    ]
    assert [c.case_id for c in subset_for_repeats(cases, 5, seed=42)] == [
        "c_01",
        "c_08",
        "c_09",
        "c_10",
        "tw_03",
    ]


def test_default_strata_include_suite() -> None:
    cases = _v1_shaped()
    # Case-level: requests and twins share (task, tier) but are apportioned separately.
    sample = stratified_sample(cases, 0.1, seed=5, unit="case")
    counts = Counter(c.suite for c in sample)
    assert counts == {Suite.requests: 30, Suite.twins: 10, Suite.adversarial: 8}


def test_subset_for_repeats() -> None:
    cases = _population()
    subset = subset_for_repeats(cases, 40, seed=0)
    assert len(subset) == 40
    assert [c.case_id for c in subset] == [c.case_id for c in subset_for_repeats(cases, 40, 0)]
    assert _strata_counts(subset)[(Task.access_request, Tier.L1)] == 20
    # Different salt from a --sample draw of the same size and seed.
    assert {c.case_id for c in subset} != {c.case_id for c in stratified_sample(cases, 40, seed=0)}


def test_subset_for_repeats_counts_cases_not_families() -> None:
    subset = subset_for_repeats(_v1_shaped(), 100, seed=0)
    assert len(subset) == 100
