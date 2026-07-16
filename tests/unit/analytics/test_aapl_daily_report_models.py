"""Tests for strict Apple daily report presentation contracts."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.aapl_daily_report_models import AaplDailyMetric
from investment_analyst.core.models import DataQuality

RESULT_ID = UUID("00000000-0000-0000-0000-000000000010")
FIRST_OBSERVATION_ID = UUID("00000000-0000-0000-0000-000000000001")
SECOND_OBSERVATION_ID = UUID("00000000-0000-0000-0000-000000000002")


def _values() -> dict[str, object]:
    return {
        "result_id": RESULT_ID,
        "metric_key": "market.history.simple_return_1d",
        "display_name": "Daily Simple Return",
        "formula": "(close_t / close_previous_available_bar) - 1",
        "value": Decimal("0.025"),
        "unit": "ratio",
        "as_of": datetime(2026, 7, 10, tzinfo=UTC),
        "available_at": datetime(2026, 7, 11, tzinfo=UTC),
        "computed_at": datetime(2026, 7, 12, tzinfo=UTC),
        "parameters": {"periods": 1},
        "input_observation_ids": (FIRST_OBSERVATION_ID, SECOND_OBSERVATION_ID),
        "algorithm_version": "market-simple-return-1d-v1-decimal34",
        "quality": DataQuality.VALID,
    }


def test_metric_preserves_exact_decimal_and_json_traceability() -> None:
    metric = AaplDailyMetric(**_values())

    assert metric.value == Decimal("0.025")
    payload = metric.model_dump(mode="json")
    assert payload["value"] == "0.025"
    assert payload["parameters"] == {"periods": 1}
    assert payload["input_observation_ids"] == [
        str(FIRST_OBSERVATION_ID),
        str(SECOND_OBSERVATION_ID),
    ]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"value": 0.025}, "Decimal"),
        ({"value": Decimal("NaN")}, "finite"),
        (
            {"input_observation_ids": (SECOND_OBSERVATION_ID, FIRST_OBSERVATION_ID)},
            "deterministically ordered",
        ),
        ({"unexpected": "field"}, "Extra inputs are not permitted"),
    ],
)
def test_metric_rejects_unsafe_or_non_deterministic_values(
    updates: dict[str, object],
    message: str,
) -> None:
    values = _values()
    values.update(updates)

    with pytest.raises(ValidationError, match=message):
        AaplDailyMetric(**values)
