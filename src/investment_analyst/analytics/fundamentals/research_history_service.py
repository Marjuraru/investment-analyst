"""Deterministic historical analysis over point-in-time research metrics."""

from collections import defaultdict
from decimal import Context, Decimal, localcontext
from typing import Protocol

from investment_analyst.analytics.fundamentals.research_history_models import (
    HISTORY_ALGORITHM_VERSION,
    AaplFundamentalResearchHistoryCoverage,
    AaplFundamentalResearchHistoryResult,
    FundamentalResearchHistoryPoint,
    FundamentalResearchHistoryStatistics,
    FundamentalResearchMetricHistory,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
    FundamentalResearchMetricValue,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models import DataFrequency

_DAYS_PER_YEAR = Decimal("365.2425")


class _ResearchOperations(Protocol):
    def query(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchResult:
        """Return one exact point-in-time fundamental research result."""
        ...


class FundamentalResearchHistoryError(RuntimeError):
    """Raised when verified research metrics cannot produce historical output."""


class AaplFundamentalResearchHistoryService:
    """Calculate descriptive changes and dispersion without writes or scores."""

    def __init__(self, research: _ResearchOperations) -> None:
        self._research = research

    def query(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Return exact histories derived only from the embedded research result."""
        research = self._research.query(request)
        grouped: dict[str, list[FundamentalResearchMetricValue]] = defaultdict(list)
        for period in research.periods:
            for metric in period.metrics:
                grouped[metric.metric_key].append(metric)

        try:
            with localcontext(Context(prec=34)):
                series = tuple(
                    _history(metric_key, metrics, request.frequency)
                    for metric_key, metrics in sorted(grouped.items())
                )
        except (ArithmeticError, ValueError) as error:
            raise FundamentalResearchHistoryError(
                "verified research metrics could not produce historical statistics"
            ) from error

        return AaplFundamentalResearchHistoryResult(
            request=request,
            research=research,
            series=series,
            coverage=AaplFundamentalResearchHistoryCoverage(
                series_returned=len(series),
                points_returned=sum(len(item.points) for item in series),
                series_with_previous_comparison=sum(
                    item.statistics.latest_change_from_previous_available is not None
                    for item in series
                ),
                series_with_cagr=sum(
                    item.statistics.compound_annual_growth_rate is not None for item in series
                ),
            ),
        )


def _history(
    metric_key: str,
    metrics: list[FundamentalResearchMetricValue],
    frequency: DataFrequency,
) -> FundamentalResearchMetricHistory:
    definition = get_fundamental_research_metric_definition(metric_key)
    ordered = sorted(metrics, key=lambda item: item.period_end)
    points = tuple(
        FundamentalResearchHistoryPoint(
            period_end=metric.period_end,
            value=metric.value,
            available_at=metric.available_at,
            metric_algorithm_version=metric.algorithm_version,
            input_observation_ids=tuple(item.observation_id for item in metric.inputs),
        )
        for metric in ordered
    )
    values = tuple(point.value for point in points)
    first = points[0]
    latest = points[-1]
    previous = points[-2] if len(points) > 1 else None
    elapsed_days = (latest.period_end.date() - first.period_end.date()).days
    latest_delta = latest.value - previous.value if previous is not None else None
    horizon_delta = latest.value - first.value if previous is not None else None

    latest_change_rate: Decimal | None = None
    horizon_change_rate: Decimal | None = None
    compound_annual_growth_rate: Decimal | None = None
    if definition.unit == "USD" and previous is not None:
        if previous.value > 0:
            latest_change_rate = latest.value / previous.value - 1
        if first.value > 0:
            horizon_change_rate = latest.value / first.value - 1
        if (
            frequency is DataFrequency.ANNUAL
            and latest.value > 0
            and first.value > 0
            and elapsed_days > 0
        ):
            exponent = _DAYS_PER_YEAR / Decimal(elapsed_days)
            compound_annual_growth_rate = (latest.value / first.value) ** exponent - 1

    statistics = FundamentalResearchHistoryStatistics(
        point_count=len(points),
        first_period_end=first.period_end,
        previous_period_end=previous.period_end if previous is not None else None,
        latest_period_end=latest.period_end,
        elapsed_days=elapsed_days,
        first_value=first.value,
        latest_value=latest.value,
        minimum=min(values),
        maximum=max(values),
        arithmetic_mean=sum(values, Decimal(0)) / Decimal(len(values)),
        range=max(values) - min(values),
        latest_change_from_previous_available=latest_delta,
        latest_change_rate_from_previous_available=latest_change_rate,
        horizon_change=horizon_delta,
        horizon_change_rate=horizon_change_rate,
        compound_annual_growth_rate=compound_annual_growth_rate,
        algorithm_version=HISTORY_ALGORITHM_VERSION,
    )
    return FundamentalResearchMetricHistory(
        metric_key=metric_key,
        display_name_es=definition.display_name_es,
        unit=definition.unit,
        frequency=frequency,
        points=points,
        statistics=statistics,
    )


__all__ = [
    "AaplFundamentalResearchHistoryService",
    "FundamentalResearchHistoryError",
]
