from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
    ActivityMetricRunSummary,
)
from investment_analyst.core.models.enums import DataQuality

_AT = datetime(2025, 1, 1, tzinfo=UTC)


def _candidate(**overrides: object) -> ActivityMetricCandidate:
    fields: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "metric_key": "cazatiburones.insider.holding_delta_ratio",
        "value": Decimal("0.25"),
        "unit": "ratio",
        "as_of": _AT,
        "available_at": _AT,
        "known_at": _AT,
        "parameters": {"family": "insider"},
        "input_observation_ids": (uuid4(), uuid4()),
        "algorithm_version": "cazatiburones-activity-metrics-v1",
        "quality": DataQuality.VALID,
    }
    fields.update(overrides)
    return ActivityMetricCandidate(**fields)


def test_candidate_accepts_valid_decimal() -> None:
    candidate = _candidate()
    assert candidate.value == Decimal("0.25")


def test_candidate_rejects_float_value() -> None:
    with pytest.raises(ValidationError):
        _candidate(value=0.25)


def test_candidate_rejects_non_finite_value() -> None:
    with pytest.raises(ValidationError):
        _candidate(value=Decimal("NaN"))


def test_candidate_rejects_duplicate_input_observation_ids() -> None:
    shared = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        _candidate(input_observation_ids=(shared, shared))


def test_candidate_rejects_available_at_after_known_at() -> None:
    with pytest.raises(ValidationError, match="known_at"):
        _candidate(available_at=datetime(2025, 1, 2, tzinfo=UTC), known_at=_AT)


def test_run_summary_validates_reconciled_counts() -> None:
    summary = ActivityMetricRunSummary(
        asset_id="equity:us:aapl",
        known_at=_AT,
        computed_at=_AT,
        values_examined=5,
        metrics_generated=3,
        metrics_created=2,
        metrics_reused=1,
        skipped_total=2,
        skipped_by_reason={"not_evaluable_no_precedent": 2},
    )
    assert summary.metrics_generated == 3


def test_run_summary_rejects_unreconciled_counts() -> None:
    with pytest.raises(ValidationError, match="metrics_generated"):
        ActivityMetricRunSummary(
            asset_id="equity:us:aapl",
            known_at=_AT,
            computed_at=_AT,
            values_examined=5,
            metrics_generated=3,
            metrics_created=1,
            metrics_reused=1,
            skipped_total=2,
            skipped_by_reason={"not_evaluable_no_precedent": 2},
        )
