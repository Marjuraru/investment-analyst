"""Read-only composition of bounded SEC issuer fundamental trends."""

from typing import Protocol

from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendCoverage,
    AaplFundamentalTrendRequest,
    fundamental_trend_schema_version,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecFundamentalQueryError,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    aapl_sec_configuration,
)


class AaplFundamentalTrendQueryError(RuntimeError):
    """Raised when stored SEC evidence cannot produce a valid trend contract."""


class _FundamentalOperations(Protocol):
    def query(self, request: SecFundamentalQuery) -> SecFundamentalPointInTimeResult:
        """Return one verified point-in-time fundamental history."""
        ...


class SecIssuerFundamentalTrendService:
    """Adapt one configured SEC selector to a compact versioned UI contract."""

    def __init__(
        self,
        fundamentals: _FundamentalOperations,
        configuration: SecAssetConfiguration | None = None,
    ) -> None:
        self._fundamentals = fundamentals
        self._configuration = configuration or aapl_sec_configuration()

    def query(self, request: AaplFundamentalTrendRequest) -> AaplFundamentalTrend:
        """Return bounded issuer facts without providers, writes, or recomputation."""
        try:
            result = self._fundamentals.query(
                SecFundamentalQuery(
                    asset_id=self._configuration.asset_id,
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
            schema_version=fundamental_trend_schema_version(self._configuration.asset_id),
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.companyfacts_source_id,
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


AaplFundamentalTrendService = SecIssuerFundamentalTrendService


__all__ = [
    "AaplFundamentalTrendQueryError",
    "AaplFundamentalTrendService",
    "SecIssuerFundamentalTrendService",
]
