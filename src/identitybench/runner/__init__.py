"""Runner infrastructure: experiment config, response cache, manifests, cost and dataset I/O."""

from identitybench.runner.cache import ResponseCache, cache_key, key_for_request
from identitybench.runner.config import (
    BASELINES,
    ExperimentConfig,
    ModelEntry,
    load_config,
    make_config_id,
)
from identitybench.runner.cost import (
    BudgetGuard,
    CostMode,
    CostTracker,
    cost_usd,
    default_cost_mode,
    load_pricing,
    reports_cost,
)
from identitybench.runner.dataset import (
    read_cases,
    stratified_sample,
    subset_for_repeats,
    verify_sha256sums,
    write_jsonl,
    write_sha256sums,
)
from identitybench.runner.manifest import (
    ConfigManifest,
    CostSummary,
    DatasetMismatchError,
    ParamsRecord,
    RunManifest,
    dataset_fingerprint,
    dataset_info,
    git_info,
    new_run_id,
)

__all__ = [
    "BASELINES",
    "BudgetGuard",
    "ConfigManifest",
    "CostMode",
    "CostSummary",
    "CostTracker",
    "DatasetMismatchError",
    "ExperimentConfig",
    "ModelEntry",
    "ParamsRecord",
    "ResponseCache",
    "RunManifest",
    "cache_key",
    "cost_usd",
    "dataset_fingerprint",
    "dataset_info",
    "default_cost_mode",
    "git_info",
    "key_for_request",
    "load_config",
    "load_pricing",
    "make_config_id",
    "new_run_id",
    "read_cases",
    "reports_cost",
    "stratified_sample",
    "subset_for_repeats",
    "verify_sha256sums",
    "write_jsonl",
    "write_sha256sums",
]
