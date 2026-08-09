"""Incremental catalog-scoped Coinbase daily ingestion and market analytics."""
# ruff: noqa: E501

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.btc_refresh import BtcMarketExecutionClock
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
    CryptoSpotDailyRefreshSummary,
)
from investment_analyst.application.crypto_spot_daily_planner import CryptoSpotDailyRefreshPlanner
from investment_analyst.providers.crypto.coinbase_pipeline import (
    CoinbaseHistoricalPipeline,
    CoinbaseImportSummary,
)
from investment_analyst.time_intervals import inclusive_utc_date_bounds

_OPERATIONAL_ANALYTICS_DAYS = 90


class CryptoSpotDailyRefreshError(RuntimeError):
    """Raised when a configured Coinbase daily refresh cannot be verified."""


class CryptoSpotDailyKnownAtTooEarlyError(CryptoSpotDailyRefreshError):
    """Raised when an explicit cut predates just-fetched Coinbase evidence."""


class CryptoSpotDailyRefreshPipeline:
    """Execute one configured daily Coinbase source without a BTC-specific fallback."""

    def __init__(
        self,
        *,
        asset_id: str,
        source_id: str,
        refresh_planner: CryptoSpotDailyRefreshPlanner,
        market_pipeline: CoinbaseHistoricalPipeline,
        statistics_pipeline: MarketStatisticsPipeline,
        diagnostic_pipeline: MarketDiagnosticPipeline,
        clock: Callable[[], datetime],
    ) -> None:
        self._asset_id = asset_id
        self._source_id = source_id
        self._refresh_planner = refresh_planner
        self._market_pipeline = market_pipeline
        self._statistics_pipeline = statistics_pipeline
        self._diagnostic_pipeline = diagnostic_pipeline
        self._clock = clock

    def run(self, request: CryptoSpotDailyRefreshRequest) -> CryptoSpotDailyRefreshSummary:
        if request.asset_id != self._asset_id:
            raise CryptoSpotDailyRefreshError(
                "Coinbase refresh request does not match its configured asset"
            )
        started_at = self._now()
        start, end = inclusive_utc_date_bounds(request.market_start, request.market_end)
        if end > started_at:
            raise CryptoSpotDailyRefreshError(
                "market_end must identify a completed UTC calendar day"
            )
        try:
            plan = self._refresh_planner.plan(
                requested_start=request.market_start,
                requested_end=request.market_end,
                refresh_mode=request.refresh_mode,
            )
        except (RuntimeError, ValueError) as error:
            raise CryptoSpotDailyRefreshError(
                f"Coinbase refresh planning failed: {error}"
            ) from error
        imports: list[CoinbaseImportSummary] = []
        for interval in plan.fetch_intervals:
            interval_start, interval_end = inclusive_utc_date_bounds(interval.start, interval.end)
            try:
                summary = self._market_pipeline.run(interval_start, interval_end)
            except (RuntimeError, ValueError) as error:
                raise CryptoSpotDailyRefreshError(
                    f"Coinbase market fetch failed: {error}"
                ) from error
            if (
                summary.asset_id != self._asset_id
                or summary.source_id != self._source_id
                or not summary.traceability_verified
            ):
                raise CryptoSpotDailyRefreshError(
                    "Coinbase market fetch returned an invalid context"
                )
            imports.append(summary)
        effective_known_at = self._known_at(request, plan.persisted_latest_available_at, imports)
        if (
            isinstance(self._clock, BtcMarketExecutionClock)
            and plan.persisted_latest_available_at is not None
            and plan.persisted_latest_available_at <= effective_known_at
        ):
            self._clock.observe(plan.persisted_latest_available_at)
        analytics_start = max(start, end - timedelta(days=_OPERATIONAL_ANALYTICS_DAYS))
        query = HistoricalBarQuery(
            asset_id=self._asset_id,
            source_id=self._source_id,
            start=analytics_start,
            end=end,
            known_at=effective_known_at,
        )
        try:
            statistics = self._statistics_pipeline.run(MarketStatisticsRequest(query=query))
            diagnostic = self._diagnostic_pipeline.run(MarketDiagnosticRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise CryptoSpotDailyRefreshError(
                f"Coinbase market analytics failed: {error}"
            ) from error
        if (
            (statistics.asset_id, statistics.source_id) != (self._asset_id, self._source_id)
            or (diagnostic.asset_id, diagnostic.source_id) != (self._asset_id, self._source_id)
            or not statistics.traceability_verified
            or not diagnostic.traceability_verified
        ):
            raise CryptoSpotDailyRefreshError(
                "Coinbase market analytics returned an invalid context"
            )
        missing = tuple(sorted({item for summary in imports for item in summary.missing_intervals}))
        return CryptoSpotDailyRefreshSummary(
            request=request,
            asset_id=self._asset_id,
            source_id=self._source_id,
            refresh_plan=plan,
            effective_known_at=effective_known_at,
            analytics_start=analytics_start,
            analytics_end=end,
            intervals_executed=len(imports),
            candles_received=sum(item.candles_received for item in imports),
            raw_records_created=sum(item.raw_records_created for item in imports),
            raw_records_reused=sum(item.raw_records_reused for item in imports),
            observations_created=sum(item.observations_created for item in imports),
            observations_reused=sum(item.observations_reused for item in imports),
            missing_intervals=missing,
            metric_results_created=statistics.results_created,
            metric_results_reused=statistics.results_reused,
            diagnostics_created=diagnostic.diagnostics_created,
            diagnostics_reused=diagnostic.diagnostics_reused,
            diagnostic_verdict=diagnostic.verdict,
            market_as_of=statistics.latest_as_of,
            traceability_verified=True,
        )

    def _known_at(
        self,
        request: CryptoSpotDailyRefreshRequest,
        persisted_available_at: datetime | None,
        imports: list[CoinbaseImportSummary],
    ) -> datetime:
        minimum = max((item.retrieved_at for item in imports), default=None)
        if request.requested_known_at is not None:
            if minimum is not None and request.requested_known_at < minimum:
                raise CryptoSpotDailyKnownAtTooEarlyError(
                    f"known_at {request.requested_known_at.isoformat()} predates newly fetched Coinbase evidence available at {minimum.isoformat()}"
                )
            return request.requested_known_at
        return minimum or persisted_available_at or self._now()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CryptoSpotDailyRefreshError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
