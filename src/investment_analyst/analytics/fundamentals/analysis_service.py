"""Unified analytical sections over exact fundamental research history."""

from typing import Protocol

from investment_analyst.analytics.fundamentals.analysis_models import (
    FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS,
    AaplFundamentalAnalysisResult,
    FundamentalAnalysisCoverage,
    FundamentalAnalysisSectionView,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
)


class _HistoryOperations(Protocol):
    def query(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Return exact point-in-time fundamental histories."""
        ...


class AaplFundamentalAnalysisService:
    """Organize each metric once without thresholds, scores, or recommendations."""

    def __init__(self, history: _HistoryOperations) -> None:
        self._history = history

    def query(self, request: AaplFundamentalResearchRequest) -> AaplFundamentalAnalysisResult:
        """Return versioned analytical sections over one embedded history result."""
        history = self._history.query(request)
        latest_period = history.research.periods[-1] if history.research.periods else None
        latest_metric_keys = (
            {metric.metric_key for metric in latest_period.metrics}
            if latest_period is not None
            else set()
        )
        histories = {item.metric_key: item for item in history.series}

        sections: list[FundamentalAnalysisSectionView] = []
        for definition in FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS:
            expected = tuple(item.metric_key for item in definition.metric_references)
            available = tuple(key for key in expected if key in latest_metric_keys)
            missing = tuple(key for key in expected if key not in latest_metric_keys)
            historical = tuple(key for key in expected if key in histories)
            sections.append(
                FundamentalAnalysisSectionView(
                    definition=definition,
                    latest_period_end=(
                        latest_period.period_end if latest_period is not None else None
                    ),
                    available_metric_keys=available,
                    missing_metric_keys=missing,
                    historical_metric_keys=historical,
                    coverage=FundamentalAnalysisCoverage(
                        expected_metrics=len(expected),
                        latest_period_metrics=len(available),
                        historical_series=len(historical),
                        series_with_previous_comparison=sum(
                            histories[key].statistics.latest_change_from_previous_available
                            is not None
                            for key in historical
                        ),
                    ),
                )
            )

        return AaplFundamentalAnalysisResult(
            request=request,
            history=history,
            sections=tuple(sections),
            coverage=FundamentalAnalysisCoverage(
                expected_metrics=sum(item.coverage.expected_metrics for item in sections),
                latest_period_metrics=sum(item.coverage.latest_period_metrics for item in sections),
                historical_series=sum(item.coverage.historical_series for item in sections),
                series_with_previous_comparison=sum(
                    item.coverage.series_with_previous_comparison for item in sections
                ),
            ),
        )


__all__ = ["AaplFundamentalAnalysisService"]
