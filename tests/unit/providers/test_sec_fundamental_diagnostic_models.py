"""Tests for strict Apple fundamental diagnostic models."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticInput,
    SecFundamentalDiagnosticMetric,
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticSelection,
)


def _metric(**overrides: object) -> SecFundamentalDiagnosticMetric:
    first = uuid4()
    second = uuid4()
    values: dict[str, object] = {
        "result_id": uuid4(),
        "metric_name": "fundamental.net_margin",
        "value": Decimal("0.20"),
        "unit": "ratio",
        "frequency": DataFrequency.ANNUAL,
        "period_start": None,
        "period_end": datetime(2025, 9, 27, tzinfo=UTC),
        "available_at": datetime(2025, 10, 31, tzinfo=UTC),
        "computed_at": datetime(2025, 11, 1, tzinfo=UTC),
        "formula": "net_income / revenue",
        "algorithm_version": "sec-fundamental-net-margin-v1-decimal34",
        "input_observation_ids": (first, second),
        "input_roles": (
            SecFundamentalDiagnosticInput(
                role="current_net_income",
                observation_id=first,
            ),
            SecFundamentalDiagnosticInput(
                role="current_revenue",
                observation_id=second,
            ),
        ),
        "quality": DataQuality.VALID,
    }
    values.update(overrides)
    return SecFundamentalDiagnosticMetric.model_validate(values)


def test_request_normalizes_offset_and_accepts_exact_period() -> None:
    request = SecFundamentalDiagnosticRequest(
        known_at=datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=-5))),
        frequency=DataFrequency.ANNUAL,
        as_of_period_end=date(2025, 9, 27),
    )

    assert request.known_at == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert request.as_of_period_end == date(2025, 9, 27)


@pytest.mark.parametrize(
    "values",
    [
        {"known_at": datetime(2026, 1, 1), "frequency": DataFrequency.ANNUAL},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.DAY_1,
        },
        {
            "asset_id": "equity:us:msft",
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
            "as_of_period_end": date(2026, 1, 2),
        },
    ],
)
def test_request_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecFundamentalDiagnosticRequest.model_validate(values)


def test_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        SecFundamentalDiagnosticRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            unexpected="value",
        )


@pytest.mark.parametrize("value", [True, 0.2, "NaN", "Infinity", "-Infinity"])
def test_metric_rejects_bool_float_and_non_finite_values(value: object) -> None:
    with pytest.raises(ValidationError):
        _metric(value=value)


def test_metric_requires_deterministic_input_order() -> None:
    metric = _metric()

    with pytest.raises(ValidationError, match="ordered by role"):
        _metric(input_roles=tuple(reversed(metric.input_roles)))


def test_selection_serialization_is_deterministic() -> None:
    request = SecFundamentalDiagnosticRequest(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )
    metric = _metric()
    selection = SecFundamentalDiagnosticSelection(
        request=request,
        target_period_end=metric.period_end,
        selected_metrics=(metric,),
        missing_metric_names=(
            "fundamental.liabilities_to_assets",
            "fundamental.liabilities_to_equity",
            "fundamental.revenue_yoy_growth",
            "fundamental.net_income_yoy_change_rate",
        ),
        metrics_examined=1,
        metrics_eligible=1,
        revisions_superseded=0,
        traceability_verified=True,
    )

    serialized = selection.to_json_dict()

    assert serialized["target_period_end"] == "2025-09-27T00:00:00+00:00"
    assert serialized["selected_metrics"][0]["value"] == "0.20"
    assert selection.metric_result_ids() == (metric.result_id,)
