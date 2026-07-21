"""Read-only composition of bounded Apple SEC fundamental trends."""

from typing import Protocol

from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendCoverage,
    AaplFundamentalTrendRequest,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecFundamentalQueryError,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
)


class AaplFundamentalTrendQueryError(RuntimeError):
    """Raised when stored SEC evidence cannot produce a valid trend contract."""


class _FundamentalOperations(Protocol):
    def query(self, request: SecFundamentalQuery) -> SecFundamentalPointInTimeResult:
        """Return one verified point-in-time fundamental history."""
        ...


class AaplFundamentalTrendService:
    """Adapt the existing SEC selector to a compact versioned UI contract."""

    def __init__(self, fundamentals: _FundamentalOperations) -> None:
        self._fundamentals = fundamentals

    def query(self, request: AaplFundamentalTrendRequest) -> AaplFundamentalTrend:
        """Return bounded Apple facts without providers, writes, or recomputation."""
        try:
            result = self._fundamentals.query(
                SecFundamentalQuery(
                    known_at=request.known_at,
                    frequency=request.frequency,
                    limit=request.period_limit,
                )
            )
        except SecFundamentalQueryError as error:
            raise AaplFundamentalTrendQueryError(
                "stored SEC fundamentals could not be presented"
            ) from error
        return AaplFundamentalTrend(
            request=request,
            periods=result.periods,
            coverage=AaplFundamentalTrendCoverage(
                observations_examined=result.observations_examined,
                observations_eligible=result.observations_eligible,
                observations_selected=result.observations_selected,
                observations_superseded=result.observations_superseded,
                periods_returned=result.periods_returned,
                earliest_period_end=result.earliest_period_end,
                latest_period_end=result.latest_period_end,
                latest_period_complete=result.latest_period_complete,
            ),
        )


__all__ = ["AaplFundamentalTrendQueryError", "AaplFundamentalTrendService"]
