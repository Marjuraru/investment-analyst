from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.evidence.sec_declared_activity_observations.models import (
    DeclaredActivityObservationRunSummary,
)

_KNOWN_AT = datetime(2026, 1, 1, tzinfo=UTC)
_NORMALIZED_AT = datetime(2026, 1, 2, tzinfo=UTC)


def _summary(**overrides: object) -> DeclaredActivityObservationRunSummary:
    payload: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "known_at": _KNOWN_AT,
        "normalized_at": _NORMALIZED_AT,
        "statements_examined": 2,
        "values_examined": 5,
        "observations_generated": 3,
        "observations_created": 2,
        "observations_reused": 1,
        "skipped_total": 2,
        "skipped_by_reason": {"missing_value": 1, "missing_date": 1},
    }
    payload.update(overrides)
    return DeclaredActivityObservationRunSummary(**payload)


def test_consistent_summary_validates() -> None:
    summary = _summary()
    assert summary.observations_generated == 3
    assert summary.skipped_total == 2


def test_summary_rejects_created_plus_reused_mismatch() -> None:
    with pytest.raises(ValueError, match="observations_created plus observations_reused"):
        _summary(observations_created=1)


def test_summary_rejects_negative_skipped_by_reason_count() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _summary(skipped_by_reason={"missing_value": -1, "missing_date": 3})


def test_summary_rejects_skipped_by_reason_not_summing_to_total() -> None:
    with pytest.raises(ValueError, match="skipped_by_reason must sum"):
        _summary(skipped_total=3)


def test_summary_rejects_values_examined_mismatch() -> None:
    with pytest.raises(ValueError, match="values_examined must equal"):
        _summary(values_examined=99)


def test_summary_is_frozen_and_forbids_extra_fields() -> None:
    summary = _summary()
    with pytest.raises(ValidationError):
        summary.observations_generated = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DeclaredActivityObservationRunSummary(**{**_summary().model_dump(), "extra": 1})
