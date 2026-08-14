"""Known-vector tests for Decimal-only market-comparison calculations."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.market.comparison_engine import (
    metrics,
    normalized_points,
    simple_returns,
)
from investment_analyst.analytics.market.comparison_models import MarketComparisonPoint


def _points(values: tuple[str, ...]) -> tuple[MarketComparisonPoint, ...]:
    start = date(2026, 1, 1)
    return tuple(
        MarketComparisonPoint(
            date=start + timedelta(days=index),
            close=Decimal(value),
            normalized_close=Decimal(value),
            close_observation_id=uuid4(),
            available_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        for index, value in enumerate(values)
    )


def test_normalization_and_metrics_use_the_shared_close_sample() -> None:
    benchmark = normalized_points(_points(tuple(str(100 + index) for index in range(21))))
    peer = normalized_points(_points(tuple("200" for _ in range(21))))

    result = metrics(peer, simple_returns(benchmark), is_benchmark=False)

    assert peer[0].normalized_close == Decimal("100")
    assert result.total_return == Decimal("0")
    assert result.maximum_drawdown == Decimal("0")
    assert result.daily_volatility == Decimal("0")
    assert result.correlation_to_benchmark is None
    assert result.beta_to_benchmark is not None
    assert result.correlation_status == "unavailable"
    assert result.missing_requirements == ("peer_returns_have_zero_variance",)


def test_zero_benchmark_variance_makes_correlation_and_beta_unavailable() -> None:
    benchmark = normalized_points(_points(tuple("100" for _ in range(21))))
    peer = normalized_points(_points(tuple(str(100 + index) for index in range(21))))

    result = metrics(peer, simple_returns(benchmark), is_benchmark=False)

    assert result.correlation_to_benchmark is None
    assert result.beta_to_benchmark is None
    assert result.beta_status == "unavailable"
    assert "benchmark_returns_have_zero_variance" in result.missing_requirements
