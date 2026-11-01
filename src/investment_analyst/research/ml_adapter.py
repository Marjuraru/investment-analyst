"""Narrow, single-threaded scikit-learn adapter for the research study.

scikit-learn is imported lazily so that the sealed artifact reader and the CLI
help remain usable in an environment without the optional ML dependencies.
"""

from __future__ import annotations

import platform
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from investment_analyst.research.ml_models import (
    MlEnvironment,
    MlHistogramParameters,
    MlLogisticParameters,
    MlModelFamily,
)


class MlAdapterError(RuntimeError):
    """A fit or prediction violated the preregistered contract."""


class MlAdapterUnavailableError(MlAdapterError):
    """The optional ML dependencies are not installed in this environment."""


@dataclass(frozen=True, slots=True)
class MlFitOutcome:
    """Probabilities of the positive class plus compact fit diagnostics."""

    probabilities: tuple[float, ...]
    diagnostics: tuple[str, ...]


def _require_dependencies() -> tuple[object, object, object, object, object]:
    try:
        import duckdb
        import numpy
        import scipy
        import sklearn
        import threadpoolctl
    except ImportError as error:  # pragma: no cover - exercised through subprocess smoke
        raise MlAdapterUnavailableError(
            "scikit-learn, numpy, scipy and threadpoolctl are required for training"
        ) from error
    return numpy, sklearn, scipy, threadpoolctl, duckdb


def environment_versions() -> MlEnvironment:
    """Capture the effective versions that bound one run identity."""
    numpy, sklearn, scipy, threadpoolctl, duckdb = _require_dependencies()
    return MlEnvironment(
        python_version=platform.python_version(),
        scikit_learn_version=str(sklearn.__version__),
        numpy_version=str(numpy.__version__),
        scipy_version=str(scipy.__version__),
        threadpoolctl_version=str(threadpoolctl.__version__),
        duckdb_version=str(duckdb.__version__),
    )


def _matrix(rows: Sequence[Sequence[float]], *, expected_width: int, label: str) -> np.ndarray:
    import numpy

    array = numpy.asarray(rows, dtype=numpy.float64)
    if array.ndim != 2 or array.shape[1] != expected_width:
        raise MlAdapterError(f"{label} must be a two-dimensional float64 matrix")
    if array.shape[0] == 0:
        raise MlAdapterError(f"{label} must not be empty")
    if not numpy.isfinite(array).all():
        raise MlAdapterError(f"{label} must contain only finite values")
    return array


def _labels(labels: Sequence[int], *, expected_rows: int) -> np.ndarray:
    import numpy

    array = numpy.asarray(labels, dtype=numpy.int64)
    if array.shape != (expected_rows,):
        raise MlAdapterError("training labels must match the feature rows")
    if not numpy.isin(array, (0, 1)).all():
        raise MlAdapterError("training labels must be binary")
    if array.min() == array.max():
        raise MlAdapterError("training requires both classes")
    return array


def _probabilities(model: object, matrix: np.ndarray) -> tuple[float, ...]:
    import numpy

    probabilities = numpy.asarray(model.predict_proba(matrix), dtype=numpy.float64)
    if probabilities.ndim != 2 or probabilities.shape[1] != 2:
        raise MlAdapterError("predict_proba must return two class columns")
    positive = probabilities[:, 1]
    if not numpy.isfinite(positive).all():
        raise MlAdapterError("predicted probabilities must be finite")
    if ((positive < 0) | (positive > 1)).any():
        raise MlAdapterError("predicted probabilities must stay inside [0, 1]")
    return tuple(float(value) for value in positive)


def _logistic(
    *,
    train_features: Sequence[Sequence[float]],
    train_labels: Sequence[int],
    evaluation_features: Sequence[Sequence[float]],
    parameters: MlLogisticParameters,
) -> MlFitOutcome:
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    train = _matrix(train_features, expected_width=3, label="training features")
    evaluation = _matrix(evaluation_features, expected_width=3, label="evaluation features")
    labels = _labels(train_labels, expected_rows=train.shape[0])
    pipeline = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(
                    C=float(parameters.C),
                    l1_ratio=float(parameters.l1_ratio),
                    solver=parameters.solver,
                    fit_intercept=parameters.fit_intercept,
                    max_iter=parameters.max_iter,
                    tol=float(parameters.tol),
                    class_weight=parameters.class_weight,
                    random_state=parameters.random_state,
                ),
            ),
        ]
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        pipeline.fit(train, labels)
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise MlAdapterError("logistic regression did not converge within max_iter")
    classifier = pipeline.named_steps["classifier"]
    iterations = int(max(classifier.n_iter_))
    if iterations >= parameters.max_iter:
        raise MlAdapterError("logistic regression exhausted its preregistered iterations")
    scaler = pipeline.named_steps["scaler"]
    diagnostics = (
        f"logistic_iterations={iterations}",
        "logistic_coefficients=" + ",".join(repr(float(value)) for value in classifier.coef_[0]),
        "logistic_intercept=" + repr(float(classifier.intercept_[0])),
        "scaler_mean=" + ",".join(repr(float(value)) for value in scaler.mean_),
        "scaler_scale=" + ",".join(repr(float(value)) for value in scaler.scale_),
    )
    return MlFitOutcome(_probabilities(pipeline, evaluation), diagnostics)


def _histogram(
    *,
    train_features: Sequence[Sequence[float]],
    train_labels: Sequence[int],
    evaluation_features: Sequence[Sequence[float]],
    parameters: MlHistogramParameters,
) -> MlFitOutcome:
    from sklearn.ensemble import HistGradientBoostingClassifier

    train = _matrix(train_features, expected_width=3, label="training features")
    evaluation = _matrix(evaluation_features, expected_width=3, label="evaluation features")
    labels = _labels(train_labels, expected_rows=train.shape[0])
    classifier = HistGradientBoostingClassifier(
        loss=parameters.loss,
        learning_rate=float(parameters.learning_rate),
        max_iter=parameters.max_iter,
        max_leaf_nodes=parameters.max_leaf_nodes,
        max_depth=parameters.max_depth,
        min_samples_leaf=parameters.min_samples_leaf,
        l2_regularization=float(parameters.l2_regularization),
        max_bins=parameters.max_bins,
        max_features=float(parameters.max_features),
        categorical_features=parameters.categorical_features,
        early_stopping=parameters.early_stopping,
        warm_start=parameters.warm_start,
        class_weight=parameters.class_weight,
        random_state=parameters.random_state,
    )
    classifier.fit(train, labels)
    diagnostics = (
        "histogram_iterations=" + str(int(classifier.n_iter_)),
        "histogram_feature_importances_omitted=reproducible-from-protocol-inputs",
    )
    return MlFitOutcome(_probabilities(classifier, evaluation), diagnostics)


def fit_and_predict(
    *,
    model: MlModelFamily,
    train_features: Sequence[Sequence[float]],
    train_labels: Sequence[int],
    evaluation_features: Sequence[Sequence[float]],
    logistic: MlLogisticParameters,
    histogram: MlHistogramParameters,
) -> MlFitOutcome:
    """Fit one preregistered model with BLAS and OpenMP pinned to one thread."""
    _, _, _, threadpoolctl, _ = _require_dependencies()
    if model is MlModelFamily.LOGISTIC_REGRESSION:
        fit = _logistic
        parameters = logistic
    elif model is MlModelFamily.HIST_GRADIENT_BOOSTING:
        fit = _histogram
        parameters = histogram
    else:  # pragma: no cover - closed enum
        raise MlAdapterError("unsupported model family")
    with threadpoolctl.threadpool_limits(limits=1):
        return fit(
            train_features=train_features,
            train_labels=train_labels,
            evaluation_features=evaluation_features,
            parameters=parameters,
        )


__all__ = [
    "MlAdapterError",
    "MlAdapterUnavailableError",
    "MlFitOutcome",
    "environment_versions",
    "fit_and_predict",
]
