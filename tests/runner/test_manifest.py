from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from identitybench import __version__
from identitybench.providers.base import GenerationParams, Usage
from identitybench.runner.config import Repeats, make_config_id
from identitybench.runner.dataset import write_sha256sums
from identitybench.runner.manifest import (
    ConfigManifest,
    CostSummary,
    Counts,
    DatasetInfo,
    DatasetMismatchError,
    ParamsRecord,
    RunManifest,
    UsageTotals,
    dataset_fingerprint,
    dataset_info,
    git_info,
    new_run_id,
    sha256_text,
)
from identitybench.schemas.enums import Split

CREATED = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


def _manifest() -> RunManifest:
    cid = make_config_id("anthropic:claude-opus-5-5", "baseline", "context")
    return RunManifest(
        run_id=new_run_id("v1-leaderboard", "20261010T120000Z"),
        created_at=CREATED,
        finished_at=datetime(2026, 10, 10, 13, 30, tzinfo=UTC),
        git_sha="a" * 40,
        git_dirty=False,
        dataset=DatasetInfo(
            name="identitybench", version="v1", sha256="b" * 64, path="datasets/identitybench-v1"
        ),
        split=Split.test,
        sample=0.1,
        repeats=Repeats(k=3, subset=100),
        configs=[
            ConfigManifest(
                config_id=cid,
                label="anthropic:claude-opus-5-5",
                provider="anthropic",
                model="claude-opus-5-5",
                endpoint_identity="0123456789abcdef",
                params=ParamsRecord.from_params(
                    GenerationParams(max_output_tokens=2048, effort="high")
                ),
                prompt_variant="baseline",
                prompt_sha256=sha256_text("prompt"),
                mode="context",
            )
        ],
        seeds={"sample": 0, "repeats": 0},
        usage={
            cid: UsageTotals.from_usage(
                Usage(input_tokens=10, output_tokens=5, cache_read_tokens=2, reasoning_tokens=1)
            )
        },
        cost={cid: CostSummary(cost_mode="metered", metered_api_usd=0.12, reported_usd=None)},
        counts=Counts(cases=30, calls=30, cache_hits=4, errors=1, refusals=0, invalid=2),
        experiment={"name": "v1-leaderboard"},
    )


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = _manifest()
    path = manifest.save(tmp_path / "runs" / manifest.run_id / "manifest.json")
    loaded = RunManifest.load(path)
    assert loaded == manifest
    assert loaded.harness_version == __version__
    assert loaded.usage[loaded.configs[0].config_id].cache_read_tokens == 2
    assert loaded.created_at == CREATED
    assert not list(path.parent.glob("*.tmp"))


def test_manifest_fields_are_typed() -> None:
    data = _manifest().model_dump(mode="json")
    assert data["split"] == "test"
    assert data["repeats"] == {"k": 3, "subset": 100}
    assert data["configs"][0]["params"] == {
        "max_output_tokens": 2048,
        "temperature": None,
        "effort": "high",
        "extra": {},
    }
    for path, value in [
        (("split",), "holdout"),
        (("configs", 0, "mode"), "agentic"),
        (("repeats",), {"k": 0}),
        (("configs", 0, "params"), {"temperature": 0}),
        (("cost", make_config_id("anthropic:claude-opus-5-5", "baseline", "context")), {}),
    ]:
        bad = _manifest().model_dump(mode="json")
        node = bad
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
        with pytest.raises(ValueError):
            RunManifest.model_validate(bad)


def test_manifest_sample_types_survive_roundtrip(tmp_path: Path) -> None:
    for sample in (25, 0.5, None):
        manifest = _manifest().model_copy(update={"sample": sample})
        loaded = RunManifest.load(manifest.save(tmp_path / "m.json"))
        assert loaded.sample == sample and type(loaded.sample) is type(sample)


def test_run_and_config_ids() -> None:
    assert new_run_id("v1-leaderboard", "20261010T120000Z") == "20261010T120000Z-v1-leaderboard"
    assert new_run_id("a b/c", "2026-10-10T12:00:00Z") == "2026-10-10T12_00_00Z-a_b_c"
    assert make_config_id("anthropic:claude-opus-5-5", "fewshot-3", "tools") == (
        "anthropic_claude-opus-5-5__fewshot-3__tools"
    )


def test_dataset_fingerprint_prefers_sha256sums(tmp_path: Path) -> None:
    (tmp_path / "requests.test.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "worlds").mkdir()
    (tmp_path / "worlds" / "w.json").write_text("{}", encoding="utf-8")
    without = dataset_fingerprint(tmp_path)
    assert without == dataset_fingerprint(tmp_path)
    (tmp_path / "worlds" / "w.json").write_text('{"x":1}', encoding="utf-8")
    assert dataset_fingerprint(tmp_path) != without
    sums = write_sha256sums(tmp_path)
    assert dataset_fingerprint(tmp_path) == hashlib.sha256(sums.read_bytes()).hexdigest()


def test_dataset_fingerprint_rejects_stale_sha256sums(tmp_path: Path) -> None:
    (tmp_path / "requests.test.jsonl").write_text("{}\n", encoding="utf-8")
    write_sha256sums(tmp_path)
    (tmp_path / "requests.test.jsonl").write_text('{"edited":1}\n', encoding="utf-8")
    with pytest.raises(DatasetMismatchError, match="checksum mismatch"):
        dataset_fingerprint(tmp_path)
    (tmp_path / "extra.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(DatasetMismatchError, match=r"unlisted file: extra\.jsonl"):
        dataset_fingerprint(tmp_path)


def test_dataset_info_never_records_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "datasets" / "identitybench-v1"
    root.mkdir(parents=True)
    (root / "a.jsonl").write_text("x", encoding="utf-8")
    assert dataset_info(root, base=tmp_path).path == "datasets/identitybench-v1"
    assert dataset_info(root, base=tmp_path / "elsewhere").path == "identitybench-v1"
    monkeypatch.chdir(tmp_path)
    assert dataset_info(root).path == "datasets/identitybench-v1"
    assert dataset_info(Path("datasets/identitybench-v1")).path == "datasets/identitybench-v1"


def test_manifest_records_refresh(tmp_path: Path) -> None:
    assert _manifest().refresh is False
    manifest = _manifest().model_copy(update={"refresh": True})
    assert RunManifest.load(manifest.save(tmp_path / "m.json")).refresh is True


def test_dataset_info(tmp_path: Path) -> None:
    root = tmp_path / "identitybench-v1"
    root.mkdir()
    (root / "a.jsonl").write_text("x", encoding="utf-8")
    info = dataset_info(root)
    assert (info.name, info.version) == ("identitybench", "v1")
    assert info.sha256 == dataset_fingerprint(root)
    other = tmp_path / "scratch"
    other.mkdir()
    assert dataset_info(other).version == "unversioned"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the host's git config (signing, hooks, templates, default branch).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git_info(repo) == (None, None)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("1", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-q", "-m", "init")
    sha, dirty = git_info(repo)
    assert sha is not None and len(sha) == 40
    assert dirty is False
    (repo / "f.txt").write_text("2", encoding="utf-8")
    assert git_info(repo) == (sha, True)
