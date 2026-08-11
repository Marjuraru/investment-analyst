"""Tests for strict historical market-statistics models."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.statistics_identity import metric_result_id
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.core.models import DataQuality


def _query() -> HistoricalBarQuery:
    return HistoricalBarQuery(
        asset_id="crypto:btc-usd",
        source_id="coinbase-exchange:btc-usd:daily-candles",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, 1, tzinfo=UTC),
        known_at=datetime(2026, 2, 2, tzinfo=UTC),
    )


def _calculation(**overrides: object) -> MetricCalculation:
    values: dict[str, object] = {
        "asset_id": "crypto:btc-usd",
        "source_id": "coinbase-exchange:btc-usd:daily-candles",
        "metric_key": "market.history.simple_return_1d",
        "value": Decimal("0.01"),
        "unit": "ratio",
        "as_of": datetime(2026, 1, 2, tzinfo=UTC),
        "available_at": datetime(2026, 1, 2, 1, tzinfo=UTC),
        "parameters": {"periods": 1},
        "input_observation_ids": (uuid4(), uuid4()),
        "algorithm_version": "test-v1",
        "quality": DataQuality.VALID,
    }
    values.update(overrides)
    return MetricCalculation.model_validate(values)


def test_request_defaults_and_sma_sorting() -> None:
    default = MarketStatisticsRequest(query=_query())
    sorted_request = MarketStatisticsRequest(query=_query(), sma_windows=(20, 1, 5))

    assert default.sma_windows == (5, 20)
    assert default.volatility_window == 20
    assert default.relative_volume_window == 20
    assert default.bollinger_window == 20
    assert default.bollinger_multiplier == Decimal("2")
    assert default.ema_windows == (20,)
    assert sorted_request.sma_windows == (1, 5, 20)


@pytest.mark.parametrize(
    "values",
    [(), (1, 1), (0,), (-1,), (True,), (10_001,)],
)
def test_invalid_sma_windows_are_rejected(values: tuple[object, ...]) -> None:
    with pytest.raises(ValidationError):
        MarketStatisticsRequest(query=_query(), sma_windows=values)


@pytest.mark.parametrize(
    "values",
    [(), (2, 2), (1,), (401,), (True,), ("20",), (20.0,)],
)
def test_invalid_ema_windows_are_rejected(values: tuple[object, ...]) -> None:
    with pytest.raises(ValidationError):
        MarketStatisticsRequest(query=_query(), ema_windows=values)


def test_ema_windows_are_sorted() -> None:
    request = MarketStatisticsRequest(query=_query(), ema_windows=(20, 2, 5))

    assert request.ema_windows == (2, 5, 20)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volatility_window", 1),
        ("volatility_window", True),
        ("volatility_window", 10_001),
        ("relative_volume_window", 0),
        ("relative_volume_window", False),
    ],
)
def test_invalid_scalar_windows_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        MarketStatisticsRequest.model_validate({"query": _query(), field: value})


@pytest.mark.parametrize(
    "value",
    [0.1, True, "2", Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("101")],
)
def test_invalid_bollinger_multiplier_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        MarketStatisticsRequest.model_validate({"query": _query(), "bollinger_multiplier": value})


def test_metric_calculation_preserves_decimal_and_serializes() -> None:
    calculation = _calculation()

    assert isinstance(calculation.value, Decimal)
    assert '"value":"0.01"' in calculation.model_dump_json()


@pytest.mark.parametrize("value", [0.1, True, Decimal("NaN"), Decimal("Infinity")])
def test_metric_calculation_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        _calculation(value=value)


def test_metric_calculation_rejects_duplicate_input_ids() -> None:
    identifier = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        _calculation(input_observation_ids=(identifier, identifier))


def test_metric_calculation_rejects_duplicate_derived_ids() -> None:
    identifier = uuid4()
    with pytest.raises(ValidationError, match="metric result IDs must be unique"):
        _calculation(input_metric_result_ids=(identifier, identifier))


def test_legacy_metric_identity_excludes_empty_derived_lineage() -> None:
    calculation = _calculation(
        input_observation_ids=(
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        ),
        algorithm_version="market-simple-return-1d-v1-decimal34",
    )

    identifier = metric_result_id(calculation, datetime(2026, 2, 2, tzinfo=UTC))

    assert str(identifier) == "c8efc255-3d61-5de2-a718-324fbf2ed57d"
