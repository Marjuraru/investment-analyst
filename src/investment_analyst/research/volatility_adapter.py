"""Narrow, single-threaded Ridge adapter for the volatility study.

The adapter is the explicit Decimal-to-float64 boundary.  scikit-learn and
numpy are imported lazily so the sealed reader and CLI help stay usable in an
environment without the optional ML dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from investment_analyst.research.ml_models import MlEnvironment


class VolatilityAdapterError(RuntimeError):
    """A fit or prediction violated the preregistered numerical contract."""


class VolatilityAdapterUnavailableError(VolatilityAdapterError):
    """The optional numerical dependencies are not installed in this environment."""


def _require_dependencies() -> tuple[object, object, object]:
    try:
        import numpy
        import sklearn
        import threadpoolctl
    except ImportError as error:  # pragma: no cover - exercised through subprocess smoke
        raise VolatilityAdapterUnavailableError(
            "scikit-learn, numpy and threadpoolctl are required for training"
        ) from error
    return numpy, sklearn, threadpoolctl


def environment_versions() -> MlEnvironment:
    """Capture the effective versions that bound one run identity."""
    from investment_analyst.research.ml_adapter import environment_versions as ml_versions

    return ml_versions()


def _matrix(rows: Sequence[Sequence[Decimal]], *, label: str) -> np.ndarray:
    import numpy

    array = numpy.asarray(
        [[float(value) for value in row] for row in rows],
        dtype=numpy.float64,
    )
    if array.ndim != 2 or array.shape[1] != 3:
        raise VolatilityAdapterError(f"{label} must be a three-column float64 matrix")
    if array.shape[0] == 0:
        raise VolatilityAdapterError(f"{label} must not be empty")
    if not numpy.isfinite(array).all():
        raise VolatilityAdapterError(f"{label} must contain only finite values")
    return array


def _targets(values: Sequence[Decimal], *, expected_rows: int) -> np.ndarray:
    import numpy

    array = numpy.asarray([float(value) for value in values], dtype=numpy.float64)
    if array.shape != (expected_rows,):
        raise VolatilityAdapterError("training targets must match the feature rows")
    if not numpy.isfinite(array).all():
        raise VolatilityAdapterError("training targets must contain only finite values")
    return array


def fit_ridge(
    *,
    train_features: Sequence[Sequence[Decimal]],
    train_targets: Sequence[Decimal],
    evaluation_features: Sequence[Sequence[Decimal]],
) -> tuple[float, ...]:
    """Fit StandardScaler(train)+Ridge(alpha=1.0, svd) once and predict."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    _, _, threadpoolctl = _require_dependencies()
    train = _matrix(train_features, label="training features")
    evaluation = _matrix(evaluation_features, label="evaluation features")
    targets = _targets(train_targets, expected_rows=train.shape[0])
    pipeline = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "ridge",
                Ridge(
                    alpha=1.0,
                    fit_intercept=True,
                    solver="svd",
                ),
            ),
        ]
    )
    with threadpoolctl.threadpool_limits(limits=1):
        pipeline.fit(train, targets)
        predictions = pipeline.predict(evaluation)
    import numpy

    result = numpy.asarray(predictions, dtype=numpy.float64)
    if result.shape != (evaluation.shape[0],):
        raise VolatilityAdapterError("ridge predictions must cover the evaluation rows")
    if not numpy.isfinite(result).all():
        raise VolatilityAdapterError("ridge predictions must be finite")
    return tuple(float(value) for value in result)


__all__ = [
    "VolatilityAdapterError",
    "VolatilityAdapterUnavailableError",
    "environment_versions",
    "fit_ridge",
]
