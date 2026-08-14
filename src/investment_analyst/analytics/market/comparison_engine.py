"""Decimal-only calculations for a shared daily market-comparison sample."""

from decimal import Context, Decimal, localcontext

from investment_analyst.analytics.market.comparison_models import (
    MarketComparisonMetric,
    MarketComparisonPoint,
)

_DECIMAL34 = Context(prec=34)


def normalized_points(
    points: tuple[MarketComparisonPoint, ...],
) -> tuple[MarketComparisonPoint, ...]:
    """Normalize each close to a common base of 100 without float arithmetic."""
    first = points[0].close
    with localcontext(_DECIMAL34):
        return tuple(
            point.model_copy(update={"normalized_close": Decimal("100") * point.close / first})
            for point in points
        )


def metrics(
    points: tuple[MarketComparisonPoint, ...],
    benchmark_returns: tuple[Decimal, ...],
    *,
    is_benchmark: bool,
) -> MarketComparisonMetric:
    """Calculate requested descriptive metrics over one exact shared sample."""
    closes = tuple(point.close for point in points)
    returns = tuple(
        closes[index] / closes[index - 1] - Decimal("1") for index in range(1, len(closes))
    )
    with localcontext(_DECIMAL34):
        total_return = closes[-1] / closes[0] - Decimal("1")
        peak = closes[0]
        maximum_drawdown = Decimal("0")
        for close in closes[1:]:
            peak = max(peak, close)
            maximum_drawdown = min(maximum_drawdown, close / peak - Decimal("1"))
        volatility = _sample_standard_deviation(returns)
        if is_benchmark:
            return MarketComparisonMetric(
                total_return=total_return,
                maximum_drawdown=maximum_drawdown,
                daily_volatility=volatility,
                correlation_to_benchmark=None,
                beta_to_benchmark=None,
                correlation_status="not_applicable",
                beta_status="not_applicable",
            )
        covariance = _sample_covariance(returns, benchmark_returns)
        peer_deviation = _sample_standard_deviation(returns)
        benchmark_deviation = _sample_standard_deviation(benchmark_returns)
        benchmark_variance = _sample_variance(benchmark_returns)
        missing: list[str] = []
        correlation: Decimal | None = None
        beta: Decimal | None = None
        if peer_deviation == 0:
            missing.append("peer_returns_have_zero_variance")
        if benchmark_deviation == 0:
            missing.append("benchmark_returns_have_zero_variance")
        if not missing:
            correlation = covariance / (peer_deviation * benchmark_deviation)
        if benchmark_variance == 0:
            if "benchmark_returns_have_zero_variance" not in missing:
                missing.append("benchmark_returns_have_zero_variance")
        else:
            beta = covariance / benchmark_variance
        return MarketComparisonMetric(
            total_return=total_return,
            maximum_drawdown=maximum_drawdown,
            daily_volatility=volatility,
            correlation_to_benchmark=correlation,
            beta_to_benchmark=beta,
            correlation_status="available" if correlation is not None else "unavailable",
            beta_status="available" if beta is not None else "unavailable",
            missing_requirements=tuple(missing),
        )


def simple_returns(points: tuple[MarketComparisonPoint, ...]) -> tuple[Decimal, ...]:
    """Return simple close-to-close returns for a shared sequence of dates."""
    with localcontext(_DECIMAL34):
        return tuple(
            points[index].close / points[index - 1].close - Decimal("1")
            for index in range(1, len(points))
        )


def _sample_variance(values: tuple[Decimal, ...]) -> Decimal:
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    return sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values) - 1)


def _sample_standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
    return _sample_variance(values).sqrt()


def _sample_covariance(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal:
    left_mean = sum(left, Decimal("0")) / Decimal(len(left))
    right_mean = sum(right, Decimal("0")) / Decimal(len(right))
    return sum(
        ((left[index] - left_mean) * (right[index] - right_mean) for index in range(len(left))),
        Decimal("0"),
    ) / Decimal(len(left) - 1)
