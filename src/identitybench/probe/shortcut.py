"""The frozen shortcut probe (SPEC §9.4).

**The probe owns its population.** It trains and is scored on base request and review cases
only (``PROBE_SUITES``: the ``requests`` and ``reviews`` suites), whatever else the caller
passes: ``task_samples`` drops twins, adversarial copies and JML cases. SPEC §9.4 names no
suites, so this is a fixed reading of it, for three reasons:

- the gate asks whether the clean generator leaks the label, and a twin or an adversarial copy
  shares its base's ``family_id``, so it adds no independent family to train on or to split
  into validation, only near-copies of a base case that would weight some families twice;
- dev holds too few variants to learn from (25 adversarial cases and 25 twins in v1 dev);
- ``Probe.from_json`` must rebuild the exact training matrix, so a caller that reads a whole
  dev split (every suite) and one that reads only the base suites have to get the same probe.

The cost is that the gate never measures SPEC §9.4's "attacks on both positive and negative
cases" defense: whether an attack's surface (its injected text, its length) gives the label
away is outside this gate. The frozen probe still predicts on any request or review case
(``predict_request``, ``predict_items``), which is how the ``surface_probe`` baseline answers
twins and attacks, and so how a run can see that signal.

**Training** (``train_probe``). For each task, dev samples (one per request, or one per review
item) are split by case ``family_id`` into a training part and an internal validation slice,
so validation measures transfer to unseen cases. A small ``GradientBoostingClassifier`` grid
is fitted on the training part and scored on validation; the winner, fitted on the training
part only, is the probe.

**Validation sees what test sees.** Text template families (``probe.features``) are drawn per
split, not per case, so training and validation share them, while every test (and fresh)
family is one dev never saw: on test each family one-hot is 0. Scored with its family columns
intact, validation would reward a model that leans on dev's family IDs, and the probe frozen
from it would meet test with that crutch gone and other features underused, which can hide a
real leak in those other features. So validation rows are scored with every family column set
to 0, as test rows are, and the grid offers each setting both without and with the family
one-hots: a model that relies on families loses to one that transfers. Ties go to the
simpler candidate, without families, then earlier in ``PARAM_GRID``.

**Freezing without pickles.** A pickled estimator is opaque, tied to one library version, and
executes code when loaded. The probe is frozen as a JSON ``ProbeSpec`` instead: per task the
feature list, the chosen hyperparameters, the training families, a digest of the training
matrix and a fingerprint of the fitted trees, plus the scikit-learn version it was fitted
under. Gradient boosting with a fixed ``random_state`` is deterministic, so
``Probe.from_json`` refits the identical model from the same dev cases, and retraining from the
same dev cases and seed reproduces the spec byte for byte. Two checks keep "identical" honest:
the digest makes a different dev set fail loudly, and the fingerprint does the same for a
library that splits or breaks ties differently (another scikit-learn release may pick a
different one of two features that split the training data the same way, which predicts
differently on test). The version is recorded for that error message only, so a newer release
that refits the same trees is accepted.

**Evaluation** (``evaluate_probe``). The frozen probe is scored once on the release test set
(never on dev or on its own training families), per task: requests by case label, reviews by
item label (SPEC §12.1 labels A/N/E). The majority-class baseline predicts the test set's own
most common label, the strongest constant predictor. The probe passes when it beats that
baseline by at most ``MAX_DELTA_POINTS`` accuracy points; otherwise the generator has a surface
shortcut and test must be regenerated from a new seed. Each result names the features the
frozen probe relied on most (``ProbeEvaluation.top_features``, from
``Probe.feature_importances``), so a failing gate can be traced to a generator artifact or to
policy showing through a count (see ``probe.features``).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import sklearn
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict
from sklearn.ensemble import GradientBoostingClassifier

from identitybench.probe.features import (
    REQUEST_FEATURES,
    REVIEW_ITEM_FEATURES,
    FeatureRow,
    is_family_feature,
    request_features,
    review_item_features,
)
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Label, Split, Suite, Task, label_of
from identitybench.schemas.gold import RequestGold, ReviewGold

PROBE_TASKS: tuple[Task, ...] = (Task.access_request, Task.access_review)
"""Tasks with a decision label. JML plans have no single label, so the probe skips them."""

PROBE_SUITES: frozenset[Suite] = frozenset({Suite.requests, Suite.reviews})
"""The suites the probe trains and is scored on: base request and review cases only. Fixed, not
a parameter, so every caller freezes and refits the same probe (see the module docstring)."""

VALIDATION_FRACTION = 0.25
"""Share of a task's dev families held out for tuning (at least one family on each side)."""

MAX_DELTA_POINTS = 5
"""SPEC §9.4: the probe may beat the majority-class baseline by at most this many points."""

TOP_FEATURES = 5
"""How many of the probe's most important features ``evaluate_probe`` reports per task."""

FINGERPRINT_DECIMALS = 6
"""Thresholds and leaf values are rounded to this many decimals before fingerprinting: far finer
than any real change to a tree, and coarse enough that last-bit float noise between platforms
can't flip a digit and reject an identical model. Feature importances are rounded the same way,
so the published diagnostic is deterministic too."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GbParams(_Frozen):
    """Gradient boosting hyperparameters searched by ``train_probe``."""

    n_estimators: int
    max_depth: int
    learning_rate: float


PARAM_GRID: tuple[GbParams, ...] = tuple(
    GbParams(n_estimators=n, max_depth=depth, learning_rate=rate)
    for n in (50, 150)
    for depth in (2, 3)
    for rate in (0.05, 0.2)
)
"""The tuning grid, simplest first: validation ties go to the earlier (simpler) setting."""


class TaskProbeSpec(_Frozen):
    """Everything needed to refit one task's probe from the dev cases."""

    task: Task
    features: tuple[str, ...]
    """Vector order: the task's surface features, then, when the tuned model uses them, the
    family one-hots seen in training."""
    params: GbParams
    classes: tuple[Label, ...]
    train_families: tuple[str, ...]
    validation_families: tuple[str, ...]
    train_n: int
    validation_n: int
    validation_correct: int
    """Validation rows predicted correctly, scored with every family column 0 (as on test)."""
    train_digest: str
    """SHA-256 of the training matrix and labels; ``Probe.from_json`` checks it before refitting."""
    model_fingerprint: str
    """SHA-256 of the fitted trees; ``Probe.from_json`` checks the refit model against it."""


class ProbeSpec(_Frozen):
    """The frozen probe as JSON (no pickles)."""

    version: Literal[1] = 1
    seed: int
    sklearn_version: str
    """The scikit-learn release the probe was fitted under (informational)."""
    tasks: tuple[TaskProbeSpec, ...]


class ProbeEvaluation(_Frozen):
    """One task's probe result on a held-out set."""

    task: Task
    n: int
    correct: int
    accuracy: float
    majority_class: Label
    majority_correct: int
    majority_accuracy: float
    delta: float
    """``accuracy - majority_accuracy`` as a fraction (0.05 is five points)."""
    passed: bool
    """True iff ``delta`` is at most ``MAX_DELTA_POINTS`` points (compared exactly, in counts)."""
    top_features: tuple[tuple[str, float], ...] = ()
    """The probe's ``TOP_FEATURES`` most important features with their importance, highest
    first, leaving out features it never split on (``Probe.feature_importances``)."""


@dataclass(frozen=True, slots=True)
class Samples:
    """One task's probe samples: feature rows, gold labels and the family of each row."""

    rows: tuple[FeatureRow, ...]
    labels: tuple[Label, ...]
    groups: tuple[str, ...]

    def where(self, families: Iterable[str]) -> Samples:
        """The samples whose family is in ``families``, in their original order."""
        keep = frozenset(families)
        picked = [i for i, group in enumerate(self.groups) if group in keep]
        return Samples(
            tuple(self.rows[i] for i in picked),
            tuple(self.labels[i] for i in picked),
            tuple(self.groups[i] for i in picked),
        )


def task_samples(cases: Iterable[Case], task: Task) -> Samples:
    """Probe samples for ``task``: one per base request case, or one per base campaign item.

    Cases outside ``PROBE_SUITES`` are skipped, so training, refitting and evaluation see the
    same population whichever suites the caller read. Samples come in case ID order (items in
    campaign order), so the training matrix, and with it the frozen probe, doesn't depend on the
    order the dataset was read in. Labels come from gold here, never from the feature code, so
    ``probe.features`` can't read gold by construction.
    """
    rows: list[FeatureRow] = []
    labels: list[Label] = []
    groups: list[str] = []
    for case in sorted(cases, key=lambda c: c.case_id):
        if case.task is not task or case.suite not in PROBE_SUITES:
            continue
        if isinstance(case.gold, RequestGold):
            rows.append(request_features(case))
            labels.append(label_of(case.gold.decision))
            groups.append(case.family_id)
        elif isinstance(case.gold, ReviewGold):
            gold_items = {item.item_id: item for item in case.gold.items}
            items = case.view.campaign.items if case.view.campaign is not None else ()
            for item, row in zip(items, review_item_features(case), strict=True):
                gold_item = gold_items.get(item.item_id)
                if gold_item is None:
                    raise ValueError(f"case {case.case_id}: no gold for item {item.item_id}")
                rows.append(row)
                labels.append(label_of(gold_item.decision))
                groups.append(case.family_id)
        else:
            raise ValueError(f"case {case.case_id}: {task} has no probe label")
    return Samples(tuple(rows), tuple(labels), tuple(groups))


def _base_features(task: Task) -> tuple[str, ...]:
    return REQUEST_FEATURES if task is Task.access_request else REVIEW_ITEM_FEATURES


def _without_families(rows: Sequence[FeatureRow]) -> list[FeatureRow]:
    """Rows as test presents them to a probe trained on dev: every family one-hot unset (so 0
    in ``_matrix``), since a test family is never one the probe saw in training."""
    return [{k: v for k, v in row.items() if not is_family_feature(k)} for row in rows]


def _matrix(rows: Sequence[FeatureRow], features: Sequence[str]) -> NDArray[np.float64]:
    """Rows as a dense matrix in ``features`` order. Names a row lacks are 0, and names the
    list lacks are dropped: a family never seen in training carries no signal."""
    return np.array(
        [[row.get(name, 0.0) for name in features] for row in rows], dtype=np.float64
    ).reshape(len(rows), len(features))


def _targets(labels: Sequence[Label]) -> NDArray[np.str_]:
    return np.array([label.value for label in labels], dtype=np.str_)


def _labels(values: Any) -> list[Label]:
    """Labels from an estimator's string array output (``predict``, ``classes_``)."""
    return [Label(str(value)) for value in np.asarray(values).tolist()]


def _digest(features: Sequence[str], x: NDArray[np.float64], y: NDArray[np.str_]) -> str:
    payload = json.dumps(
        {"features": list(features), "x": x.tolist(), "y": y.tolist()}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _fingerprint(model: GradientBoostingClassifier) -> str:
    """SHA-256 of the fitted ensemble: each tree's split features, thresholds, children and
    leaf values. Given the training labels (pinned by the digest), these determine every
    prediction the model makes."""
    trees: list[dict[str, Any]] = []
    for estimator in np.asarray(model.estimators_).ravel().tolist():
        tree = estimator.tree_
        trees.append(
            {
                "feature": tree.feature.tolist(),
                "threshold": np.round(tree.threshold, FINGERPRINT_DECIMALS).tolist(),
                "left": tree.children_left.tolist(),
                "right": tree.children_right.tolist(),
                "value": np.round(tree.value.ravel(), FINGERPRINT_DECIMALS).tolist(),
            }
        )
    return hashlib.sha256(json.dumps(trees, separators=(",", ":")).encode()).hexdigest()


def _fit(
    params: GbParams, x: NDArray[np.float64], y: NDArray[np.str_], seed: int
) -> GradientBoostingClassifier:
    """Deterministic fit: same params, data and seed give the same model."""
    model = GradientBoostingClassifier(
        n_estimators=params.n_estimators,
        max_depth=params.max_depth,
        learning_rate=params.learning_rate,
        random_state=seed,
    )
    model.fit(x, y)
    return model


def _split_families(families: Sequence[str], task: Task, seed: int) -> frozenset[str]:
    """The validation families: a seeded, order-independent draw of ``VALIDATION_FRACTION``."""
    order = sorted(families)
    if len(order) < 2:
        raise ValueError(f"{task}: the probe needs dev cases from at least two families")
    # A string seed is hashed with SHA-512 by ``random``, so the draw ignores PYTHONHASHSEED.
    random.Random(f"identitybench-probe:{task.value}:{seed}").shuffle(order)
    n_validation = min(len(order) - 1, max(1, round(len(order) * VALIDATION_FRACTION)))
    return frozenset(order[:n_validation])


def _train_task(
    task: Task, samples: Samples, seed: int
) -> tuple[TaskProbeSpec, GradientBoostingClassifier]:
    validation_families = _split_families(sorted(set(samples.groups)), task, seed)
    train_families = sorted(set(samples.groups) - validation_families)
    train, validation = samples.where(train_families), samples.where(validation_families)
    classes = tuple(sorted(set(train.labels)))
    if len(classes) < 2:
        raise ValueError(f"{task}: the probe's training families hold a single label")
    y_train, y_validation = _targets(train.labels), _targets(validation.labels)
    validation_rows = _without_families(validation.rows)
    base = _base_features(task)
    families = sorted({name for row in train.rows for name in row if is_family_feature(name)})
    feature_sets = [base, (*base, *families)] if families else [base]

    # The winning grid model was fitted on the training part alone, which is exactly the
    # train-only refit the spec freezes, so it is kept rather than fitted a second time.
    best: tuple[int, tuple[str, ...], GbParams, GradientBoostingClassifier] | None = None
    for features in feature_sets:
        x_train, x_validation = _matrix(train.rows, features), _matrix(validation_rows, features)
        for params in PARAM_GRID:
            model = _fit(params, x_train, y_train, seed)
            correct = int(np.sum(model.predict(x_validation) == y_validation))
            if best is None or correct > best[0]:
                best = (correct, features, params, model)
    assert best is not None  # PARAM_GRID and feature_sets are non-empty
    correct, features, params, model = best
    spec = TaskProbeSpec(
        task=task,
        features=features,
        params=params,
        classes=classes,
        train_families=tuple(train_families),
        validation_families=tuple(sorted(validation_families)),
        train_n=len(train.rows),
        validation_n=len(validation.rows),
        validation_correct=correct,
        train_digest=_digest(features, _matrix(train.rows, features), y_train),
        model_fingerprint=_fingerprint(model),
    )
    return spec, model


class Probe:
    """A frozen shortcut probe: its JSON spec plus the fitted estimators it describes.

    Build one with ``train_probe`` or ``Probe.from_json``; nothing mutates it afterwards.
    """

    def __init__(
        self, spec: ProbeSpec, estimators: Mapping[Task, GradientBoostingClassifier]
    ) -> None:
        tasks = {task_spec.task for task_spec in spec.tasks}
        if len(tasks) != len(spec.tasks) or set(estimators) != tasks:
            raise ValueError("a probe needs exactly one estimator per task in its spec")
        self._spec = spec
        self._estimators = dict(estimators)
        self._json = spec.model_dump_json(indent=2)

    @property
    def spec(self) -> ProbeSpec:
        return self._spec

    @property
    def tasks(self) -> frozenset[Task]:
        return frozenset(self._estimators)

    def task_spec(self, task: Task) -> TaskProbeSpec:
        for task_spec in self._spec.tasks:
            if task_spec.task is task:
                return task_spec
        raise KeyError(f"the probe has no model for {task}")

    def feature_importances(self, task: Task) -> dict[str, float]:
        """What ``task``'s frozen model relied on: each feature's impurity-based importance, in
        vector order, rounded to ``FINGERPRINT_DECIMALS``.

        Importances sum to 1 (before rounding) over the features the trees split on, and are
        all 0 for a model that never split. They come from the fingerprinted trees, so they are
        as deterministic as the probe itself. They show what carried the training signal, not
        a causal effect on test: of two features that split the data the same way, the trees
        credit the one they happened to pick.
        """
        model = self._estimators.get(task)
        if model is None:
            raise KeyError(f"the probe has no model for {task}")
        importances = np.round(np.asarray(model.feature_importances_), FINGERPRINT_DECIMALS)
        return dict(zip(self.task_spec(task).features, importances.tolist(), strict=True))

    def top_features(self, task: Task, k: int = TOP_FEATURES) -> tuple[tuple[str, float], ...]:
        """``task``'s ``k`` most important features, highest first (ties in vector order),
        leaving out features with zero importance."""
        ranked = sorted(
            ((name, value) for name, value in self.feature_importances(task).items() if value),
            key=lambda pair: -pair[1],
        )
        return tuple(ranked[:k])

    def to_json(self) -> str:
        """The frozen parameters and feature lists (deterministic for a given spec)."""
        return self._json

    def digest(self) -> str:
        """SHA-256 of ``to_json()``: identifies this probe in manifests."""
        return hashlib.sha256(self._json.encode()).hexdigest()

    @classmethod
    def from_json(
        cls, params: str | Mapping[str, Any] | ProbeSpec, dev_cases: Sequence[Case]
    ) -> Probe:
        """Refit the probe a spec describes from the dev cases it was trained on.

        ``dev_cases`` may hold every dev suite: only base cases (``PROBE_SUITES``) are read.

        Raises ``ValueError`` if the dev cases don't reproduce the training matrix the spec
        recorded (different cases, families or feature code), or if the refit model's classes
        or trees differ from the spec's (a scikit-learn release that fits differently).
        """
        if isinstance(params, ProbeSpec):
            spec = params
        elif isinstance(params, str):
            spec = ProbeSpec.model_validate_json(params)
        else:
            spec = ProbeSpec.model_validate(params)
        estimators: dict[Task, GradientBoostingClassifier] = {}
        for task_spec in spec.tasks:
            train = task_samples(dev_cases, task_spec.task).where(task_spec.train_families)
            x, y = _matrix(train.rows, task_spec.features), _targets(train.labels)
            if _digest(task_spec.features, x, y) != task_spec.train_digest:
                raise ValueError(
                    f"{task_spec.task}: these dev cases don't reproduce the probe's training "
                    "data; refit it from the dev cases it was trained on"
                )
            model = _fit(task_spec.params, x, y, spec.seed)
            if tuple(_labels(model.classes_)) != task_spec.classes:
                raise ValueError(f"{task_spec.task}: refit classes differ from the spec")
            if _fingerprint(model) != task_spec.model_fingerprint:
                raise ValueError(
                    f"{task_spec.task}: the refit model differs from the frozen one (frozen "
                    f"under scikit-learn {spec.sklearn_version}, refit under "
                    f"{sklearn.__version__}); refit it under the release it was frozen with"
                )
            estimators[task_spec.task] = model
        return cls(spec, estimators)

    def predict_rows(self, task: Task, rows: Sequence[FeatureRow]) -> list[Label]:
        """The probe's label for each feature row of ``task``."""
        model = self._estimators.get(task)
        if model is None:
            raise KeyError(f"the probe has no model for {task}")
        if not rows:
            return []
        return _labels(model.predict(_matrix(rows, self.task_spec(task).features)))

    def predict_request(self, case: Case) -> Label:
        """The probe's label for an access request case."""
        return self.predict_rows(Task.access_request, [request_features(case)])[0]

    def predict_items(self, case: Case) -> list[Label]:
        """The probe's label for each campaign item, in campaign order."""
        return self.predict_rows(Task.access_review, review_item_features(case))


def train_probe(dev_cases: Sequence[Case], seed: int = 0) -> Probe:
    """Train and freeze the shortcut probe on dev (see the module docstring).

    Trains one model per task that has base dev cases (``PROBE_SUITES``; other suites are
    ignored). Raises ``ValueError`` if a case isn't from the dev split (the probe must never be
    tuned on test), if there are no base request or review cases, or if a task's dev cases
    come from fewer than two families or its training part holds a single label.
    """
    not_dev = sorted(case.case_id for case in dev_cases if case.split is not Split.dev)
    if not_dev:
        raise ValueError(f"train_probe takes dev cases only; got {', '.join(not_dev[:5])}")
    specs: list[TaskProbeSpec] = []
    estimators: dict[Task, GradientBoostingClassifier] = {}
    for task in PROBE_TASKS:
        samples = task_samples(dev_cases, task)
        if not samples.rows:
            continue
        spec, model = _train_task(task, samples, seed)
        specs.append(spec)
        estimators[task] = model
    if not specs:
        raise ValueError("train_probe needs base access request or access review dev cases")
    spec = ProbeSpec(seed=seed, sklearn_version=sklearn.__version__, tasks=tuple(specs))
    return Probe(spec, estimators)


def evaluate_probe(probe: Probe, test_cases: Sequence[Case]) -> dict[Task, ProbeEvaluation]:
    """Score the frozen probe against the majority-class baseline, per task.

    Only tasks the probe has a model for and the cases include are reported, scored on base
    cases (``PROBE_SUITES``) as in training; other suites are ignored. The majority
    class is the most common test label (ties go to the first label in A, N, E order).

    Raises ``ValueError`` for a dev case (mirroring ``train_probe``) or a case from a family
    that trained or tuned the probe: scored on those, the probe would report an inflated
    accuracy that reads like a gate result.
    """
    dev = sorted(case.case_id for case in test_cases if case.split is Split.dev)
    if dev:
        raise ValueError(f"evaluate_probe takes held-out cases, not dev; got {', '.join(dev[:5])}")
    results: dict[Task, ProbeEvaluation] = {}
    for task in PROBE_TASKS:
        if task not in probe.tasks:
            continue
        samples = task_samples(test_cases, task)
        if not samples.rows:
            continue
        spec = probe.task_spec(task)
        seen = sorted(set(samples.groups) & {*spec.train_families, *spec.validation_families})
        if seen:
            raise ValueError(
                f"{task}: families {', '.join(seen[:5])} trained or tuned the probe; evaluate "
                "it on held-out families"
            )
        predicted = probe.predict_rows(task, samples.rows)
        n = len(samples.labels)
        correct = sum(p is g for p, g in zip(predicted, samples.labels, strict=True))
        counts = {label: samples.labels.count(label) for label in (Label.A, Label.N, Label.E)}
        majority = max(counts, key=lambda label: counts[label])
        majority_correct = counts[majority]
        results[task] = ProbeEvaluation(
            task=task,
            n=n,
            correct=correct,
            accuracy=correct / n,
            majority_class=majority,
            majority_correct=majority_correct,
            majority_accuracy=majority_correct / n,
            delta=(correct - majority_correct) / n,
            passed=(correct - majority_correct) * 100 <= MAX_DELTA_POINTS * n,
            top_features=probe.top_features(task),
        )
    return results
