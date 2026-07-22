"""Incremental Coinbase BTC-USD ingestion and independent market analytics."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshPlan,
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
)
from investment_analyst.application.btc_refresh_planner import BtcMarketRefreshPlanner
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import (
    CoinbaseHistoricalPipeline,
    CoinbaseImportSummary,
)
from investment_analyst.time_intervals import inclusive_utc_date_bounds

_OPERATIONAL_ANALYTICS_DAYS = 90


class BtcMarketRefreshError(RuntimeError):
    """Raised when one Coinbase refresh cannot produce a verified market outcome."""


class BtcMarketKnownAtTooEarlyError(BtcMarketRefreshError):
    """Raised after ingestion when an explicit cut predates newly available evidence."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BtcMarketRefreshPipeline:
    """Plan, ingest, calculate, and diagnose BTC-USD without fundamental data."""

    def __init__(
        self,
        *,
        refresh_planner: BtcMarketRefreshPlanner,
        market_pipeline: CoinbaseHistoricalPipeline,
        statistics_pipeline: MarketStatisticsPipeline,
        diagnostic_pipeline: MarketDiagnosticPipeline,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._refresh_planner = refresh_planner
        self._market_pipeline = market_pipeline
        self._statistics_pipeline = statistics_pipeline
        self._diagnostic_pipeline = diagnostic_pipeline
        self._clock = clock

    def run(self, request: BtcMarketRefreshRequest) -> BtcMarketRefreshSummary:
        """Execute one append-only refresh over inclusive completed UTC dates."""
        started_at = self._normalized_now()
        start, end = inclusive_utc_date_bounds(request.market_start, request.market_end)
        if end > started_at:
            raise BtcMarketRefreshError("market_end must identify a completed UTC calendar day")

        try:
            plan = self._refresh_planner.plan(
                requested_start=request.market_start,
                requested_end=request.market_end,
                refresh_mode=request.refresh_mode,
            )
        except (RuntimeError, ValueError) as error:
            raise BtcMarketRefreshError(f"Coinbase refresh planning failed: {error}") from error

        imports: list[CoinbaseImportSummary] = []
        for interval in plan.fetch_intervals:
            interval_start, interval_end = inclusive_utc_date_bounds(interval.start, interval.end)
            try:
                summary = self._market_pipeline.run(interval_start, interval_end)
            except (RuntimeError, ValueError) as error:
                raise BtcMarketRefreshError(f"Coinbase market fetch failed: {error}") from error
            if (
                summary.asset_id != ASSET_ID
                or summary.source_id != SOURCE_ID
                or not summary.traceability_verified
            ):
                raise BtcMarketRefreshError("Coinbase market fetch returned an invalid context")
            imports.append(summary)

        effective_known_at = self._resolve_known_at(request, plan, imports)
        analytics_start = max(start, end - timedelta(days=_OPERATIONAL_ANALYTICS_DAYS))
        query = HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=analytics_start,
            end=end,
            known_at=effective_known_at,
        )
        try:
            statistics = self._statistics_pipeline.run(MarketStatisticsRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise BtcMarketRefreshError(f"BTC-USD market statistics failed: {error}") from error
        try:
            diagnostic = self._diagnostic_pipeline.run(MarketDiagnosticRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise BtcMarketRefreshError(f"BTC-USD market diagnostic failed: {error}") from error

        self._verify_analytics(
            effective_known_at=effective_known_at,
            statistics_asset_id=statistics.asset_id,
            statistics_source_id=statistics.source_id,
            statistics_traceability=statistics.traceability_verified,
            diagnostic_asset_id=diagnostic.asset_id,
            diagnostic_source_id=diagnostic.source_id,
            diagnostic_traceability=diagnostic.traceability_verified,
        )
        missing = tuple(sorted({item for summary in imports for item in summary.missing_intervals}))
        return BtcMarketRefreshSummary(
            request=request,
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

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise BtcMarketRefreshError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _resolve_known_at(
        self,
        request: BtcMarketRefreshRequest,
        plan: BtcMarketRefreshPlan,
        imports: list[CoinbaseImportSummary],
    ) -> datetime:
        minimum = max((item.retrieved_at for item in imports), default=None)
        if request.requested_known_at is not None:
            if minimum is not None and request.requested_known_at < minimum:
                raise BtcMarketKnownAtTooEarlyError(
                    f"known_at {request.requested_known_at.isoformat()} predates newly fetched "
                    f"Coinbase evidence available at {minimum.isoformat()}"
                )
            return request.requested_known_at
        if minimum is not None:
            return minimum
        if plan.persisted_latest_available_at is not None:
            return plan.persisted_latest_available_at
        return self._normalized_now()

    @staticmethod
    def _verify_analytics(
        *,
        effective_known_at: datetime,
        statistics_asset_id: str,
        statistics_source_id: str,
        statistics_traceability: bool,
        diagnostic_asset_id: str,
        diagnostic_source_id: str,
        diagnostic_traceability: bool,
    ) -> None:
        expected = (ASSET_ID, SOURCE_ID)
        if (statistics_asset_id, statistics_source_id) != expected:
            raise BtcMarketRefreshError("market statistics returned a different BTC-USD context")
        if (diagnostic_asset_id, diagnostic_source_id) != expected:
            raise BtcMarketRefreshError("market diagnostic returned a different BTC-USD context")
        if not statistics_traceability or not diagnostic_traceability:
            raise BtcMarketRefreshError("BTC-USD analytical traceability was not verified")
        if effective_known_at.tzinfo is not UTC:
            raise BtcMarketRefreshError("effective_known_at must be normalized to UTC")
