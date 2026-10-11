"""Run manifests: everything needed to say exactly what a run measured (SPEC §11.4).

Timestamps are always passed in by the caller, never read from the clock here, so building a
manifest is deterministic and testable.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from identitybench import __version__
from identitybench.providers.base import GenerationParams, Usage
from identitybench.runner.config import Mode, Repeats, slug
from identitybench.runner.cost import CostMode, CostTracker
from identitybench.runner.dataset import SHA256SUMS, SampleSpec, verify_sha256sums
from identitybench.schemas.enums import Split

_VERSION_SUFFIX = re.compile(r"^(?P<name>.+?)-(?P<version>v\d+(?:\.\d+)*)$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetInfo(_Model):
    name: str
    version: str
    sha256: str
    path: str
    """As configured (relative), never an absolute path from the machine that ran it."""


class ParamsRecord(_Model):
    """The generation settings a config ran with (``GenerationParams``, as JSON)."""

    max_output_tokens: int
    temperature: float | None = None
    effort: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_params(cls, params: GenerationParams) -> ParamsRecord:
        return cls(
            max_output_tokens=params.max_output_tokens,
            temperature=params.temperature,
            effort=params.effort,
            extra=dict(params.extra),
        )


class ConfigManifest(_Model):
    """One (model, prompt variant, mode) cell of the run matrix."""

    config_id: str
    label: str
    provider: str
    """A provider kind, or ``baseline`` for the SPEC §14 baselines."""
    model: str
    endpoint_identity: str
    params: ParamsRecord
    """Per-config settings; per-request values such as the replay cache key don't belong here."""
    prompt_variant: str
    prompt_sha256: str
    mode: Mode


class UsageTotals(_Model):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: Usage) -> UsageTotals:
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )


class CostSummary(_Model):
    cost_mode: CostMode
    metered_api_usd: float | None = None
    """Usage x pricing.yaml; 0.0 when nothing is metered (self-hosted, replay); None when
    unknown (unpriced API model) or not applicable (subscription CLI)."""
    reported_usd: float | None = None
    """Sum of costs the access path itself reported (CLI ``total_cost_usd``)."""

    @classmethod
    def from_tracker(cls, tracker: CostTracker) -> CostSummary:
        return cls(
            cost_mode=tracker.mode,
            metered_api_usd=tracker.metered_api_usd,
            reported_usd=tracker.reported_usd,
        )


class Counts(_Model):
    cases: int = 0
    calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    refusals: int = 0
    invalid: int = 0


class RunManifest(_Model):
    run_id: str
    created_at: datetime
    finished_at: datetime | None = None
    harness_version: str = __version__
    git_sha: str | None = None
    git_dirty: bool | None = None
    dataset: DatasetInfo
    split: Split
    sample: SampleSpec | None = None
    repeats: Repeats = Field(default_factory=Repeats)
    repair: bool = False
    """JSON repair is off by default and recorded when enabled (SPEC §11.3)."""
    refresh: bool = False
    """``--refresh``: every call bypassed the response cache (SPEC §11.4)."""
    configs: list[ConfigManifest] = Field(default_factory=list[ConfigManifest])
    seeds: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, UsageTotals] = Field(default_factory=dict)
    """Keyed by ``config_id``."""
    cost: dict[str, CostSummary] = Field(default_factory=dict)
    """Keyed by ``config_id``."""
    counts: Counts = Field(default_factory=Counts)
    experiment: dict[str, Any] = Field(default_factory=dict)
    """The resolved experiment config (``ExperimentConfig.resolved_dict()``)."""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"

    def save(self, path: Path | str) -> Path:
        """Write atomically, so a crash mid-write never leaves a truncated manifest."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.to_json())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return p

    @classmethod
    def load(cls, path: Path | str) -> RunManifest:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def new_run_id(name: str, timestamp_str: str) -> str:
    """``<timestamp>-<name>``, sanitized for use as a directory name.

    The timestamp comes first so run directories sort chronologically.
    """
    return f"{slug(timestamp_str)}-{slug(name)}"


def sha256_text(text: str) -> str:
    """Hex sha256 of UTF-8 text (prompt hashes)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_info(repo_root: Path | str) -> tuple[str | None, bool | None]:
    """(HEAD sha, dirty flag) via read-only git; (None, None) outside a repo or without git.

    ``--no-optional-locks`` stops ``git status`` from refreshing the index, which would
    otherwise take ``index.lock`` and write to the repo of someone running a benchmark.
    """
    root = str(repo_root)

    def run(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "--no-optional-locks", "-C", root, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    sha = run("rev-parse", "HEAD")
    if sha is None:
        return None, None
    status = run("status", "--porcelain", "--untracked-files=normal")
    dirty = None if status is None else bool(status.strip())
    return sha.strip(), dirty


class DatasetMismatchError(ValueError):
    """The dataset's files don't match its ``SHA256SUMS``."""


def dataset_fingerprint(dataset_dir: Path | str) -> str:
    """sha256 identifying a dataset's content.

    Prefers the hash of ``SHA256SUMS`` (which commits to every file), after checking that the
    files still match it: a stale ``SHA256SUMS`` would otherwise make the manifest claim the
    published dataset for edited data. Raises ``DatasetMismatchError`` listing the problems.
    Without ``SHA256SUMS`` it hashes the sorted (relative path, file sha256) list of every
    non-hidden file.
    """
    root = Path(dataset_dir)
    sums = root / SHA256SUMS
    if sums.is_file():
        problems = verify_sha256sums(root)
        if problems:
            raise DatasetMismatchError(f"{root}: {'; '.join(problems)}")
        return hashlib.sha256(sums.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def _portable_path(root: Path, base: Path | None) -> str:
    """``root`` as a path that is safe to publish: relative paths as given, absolute ones made
    relative to ``base`` (default: the working directory) or else just the directory name, so
    a published manifest never carries a home directory or user name."""
    if not root.is_absolute():
        return root.as_posix()
    anchor = (base or Path.cwd()).resolve()
    try:
        return root.resolve().relative_to(anchor).as_posix()
    except ValueError:
        return root.name


def dataset_info(dataset_dir: Path | str, *, base: Path | str | None = None) -> DatasetInfo:
    """Name, version, fingerprint and portable path of a dataset directory.

    Name and version come from the directory name (``identitybench-v1`` -> ``identitybench``,
    ``v1``); a name without a ``-vN`` suffix gets version ``unversioned``. ``base`` is what an
    absolute ``dataset_dir`` is recorded relative to (see ``_portable_path``).
    """
    root = Path(dataset_dir)
    match = _VERSION_SUFFIX.match(root.name)
    name, version = (match["name"], match["version"]) if match else (root.name, "unversioned")
    return DatasetInfo(
        name=name,
        version=version,
        sha256=dataset_fingerprint(root),
        path=_portable_path(root, None if base is None else Path(base)),
    )
