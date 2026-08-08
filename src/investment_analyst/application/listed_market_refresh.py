"""Incremental Alpaca ingestion and independent analytics for listed assets."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.analytics.valuation import (
    CorporateValuationPersistencePipeline,
    CorporateValuationRequest,
    CorporateValuationService,
    ValuationPersistenceSummary,
)
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.market.alpaca_pipeline import (
    AlpacaHistoricalPipeline,
    AlpacaImportSummary,
)
from investment_analyst.time_intervals import inclusive_utc_date_bounds

_OPERATIONAL_ANALYTICS_DAYS = 90


class ListedMarketRefreshError(RuntimeError):
    """Raised when one Alpaca refresh cannot produce a verified market outcome."""


class ListedMarketKnownAtTooEarlyError(ListedMarketRefreshError):
    """Raised when an explicit cut predates newly fetched provider evidence."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ListedMarketRefreshPipeline:
    """Plan, ingest, calculate, and diagnose one catalog-backed Alpaca asset."""

    def __init__(
        self,
        *,
        configuration: AlpacaAssetConfiguration,
        refresh_planner: AaplMarketRefreshPlanner,
        market_pipeline: AlpacaHistoricalPipeline,
        statistics_pipeline: MarketStatisticsPipeline,
        diagnostic_pipeline: MarketDiagnosticPipeline,
        valuation_service: CorporateValuationService | None = None,
        valuation_pipeline: CorporateValuationPersistencePipeline | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._configuration = configuration
        self._refresh_planner = refresh_planner
        self._market_pipeline = market_pipeline
        self._statistics_pipeline = statistics_pipeline
        self._diagnostic_pipeline = diagnostic_pipeline
        if (valuation_service is None) != (valuation_pipeline is None):
            raise ValueError("valuation service and pipeline must be configured together")
        self._valuation_service = valuation_service
        self._valuation_pipeline = valuation_pipeline
        self._clock = clock

    def run(self, request: ListedMarketRefreshRequest) -> ListedMarketRefreshSummary:
        """Execute one append-only refresh over inclusive completed UTC dates."""
        if request.asset_id != self._configuration.asset_id:
            raise ListedMarketRefreshError("request asset does not match Alpaca configuration")
        started_at = self._normalized_now()
        start, end = inclusive_utc_date_bounds(request.market_start, request.market_end)
        if end > started_at:
            raise ListedMarketRefreshError("market_end must identify a completed UTC calendar day")
        try:
            plan = self._refresh_planner.plan(
                requested_start=request.market_start,
                requested_end=request.market_end,
                refresh_mode=request.refresh_mode,
            )
        except (RuntimeError, ValueError) as error:
            raise ListedMarketRefreshError(f"Alpaca refresh planning failed: {error}") from error

        imports: list[AlpacaImportSummary] = []
        for interval in plan.fetch_intervals:
            interval_start, interval_end = inclusive_utc_date_bounds(
                interval.start,
                interval.end,
            )
            try:
                summary = self._market_pipeline.run(interval_start, interval_end)
            except (RuntimeError, ValueError) as error:
                raise ListedMarketRefreshError(f"Alpaca market fetch failed: {error}") from error
            if (
                summary.asset_id != self._configuration.asset_id
                or summary.source_id != self._configuration.source_id
                or not summary.traceability_verified
            ):
                raise ListedMarketRefreshError("Alpaca market fetch returned an invalid context")
            imports.append(summary)

        effective_known_at = self._resolve_known_at(request, imports)
        analytics_start = max(start, end - timedelta(days=_OPERATIONAL_ANALYTICS_DAYS))
        query = HistoricalBarQuery(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.source_id,
            start=analytics_start,
            end=end,
            known_at=effective_known_at,
        )
        try:
            statistics = self._statistics_pipeline.run(MarketStatisticsRequest(query=query))
            diagnostic = self._diagnostic_pipeline.run(MarketDiagnosticRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise ListedMarketRefreshError(f"listed market analytics failed: {error}") from error
        self._verify_analytics(
            statistics.asset_id,
            statistics.source_id,
            statistics.traceability_verified,
            diagnostic.asset_id,
            diagnostic.source_id,
            diagnostic.traceability_verified,
        )
        valuation = self._run_valuation(request, effective_known_at)
        return ListedMarketRefreshSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.source_id,
            request=request,
            refresh_plan=plan,
            effective_known_at=effective_known_at,
            analytics_start=analytics_start,
            analytics_end=end,
            intervals_executed=len(imports),
            bars_received=sum(item.bars_received for item in imports),
            raw_records_created=sum(item.raw_records_created for item in imports),
            raw_records_reused=sum(item.raw_records_reused for item in imports),
            observations_created=sum(item.observations_created for item in imports),
            observations_reused=sum(item.observations_reused for item in imports),
            coverage_receipts_created=sum(item.coverage_receipts_created for item in imports),
            coverage_receipts_reused=sum(item.coverage_receipts_reused for item in imports),
            metric_results_created=(statistics.results_created + valuation.metric_results_created),
            metric_results_reused=(statistics.results_reused + valuation.metric_results_reused),
            valuation_metric_results_created=valuation.metric_results_created,
            valuation_metric_results_reused=valuation.metric_results_reused,
            valuation_metrics_not_evaluable=valuation.metrics_not_evaluable,
            diagnostics_created=diagnostic.diagnostics_created,
            diagnostics_reused=diagnostic.diagnostics_reused,
            diagnostic_verdict=diagnostic.verdict,
            market_as_of=statistics.latest_as_of,
            traceability_verified=True,
        )

    def _run_valuation(
        self,
        request: ListedMarketRefreshRequest,
        effective_known_at: datetime,
    ) -> ValuationPersistenceSummary:
        if self._valuation_service is None or self._valuation_pipeline is None:
            return ValuationPersistenceSummary(
                definitions_created=0,
                definitions_reused=0,
                metric_results_created=0,
                metric_results_reused=0,
                metrics_not_evaluable=0,
                metrics_not_applicable=0,
            )
        try:
            snapshot = self._valuation_service.query(
                CorporateValuationRequest(
                    asset_id=request.asset_id,
                    known_at=effective_known_at,
                    valuation_date=request.market_end,
                )
            )
            return self._valuation_pipeline.persist(snapshot)
        except (RuntimeError, ValueError) as error:
            raise ListedMarketRefreshError(
                f"corporate valuation persistence failed: {error}"
            ) from error

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ListedMarketRefreshError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _resolve_known_at(
        self,
        request: ListedMarketRefreshRequest,
        imports: list[AlpacaImportSummary],
    ) -> datetime:
        minimum = max((item.retrieved_at for item in imports), default=None)
        if request.requested_known_at is not None:
            if minimum is not None and request.requested_known_at < minimum:
                raise ListedMarketKnownAtTooEarlyError(
                    f"known_at {request.requested_known_at.isoformat()} predates newly fetched "
                    f"Alpaca evidence available at {minimum.isoformat()}"
                )
            return request.requested_known_at
        return minimum or self._refresh_planner.latest_available_at() or self._normalized_now()

    def _verify_analytics(
        self,
        statistics_asset_id: str,
        statistics_source_id: str,
        statistics_traceability: bool,
        diagnostic_asset_id: str,
        diagnostic_source_id: str,
        diagnostic_traceability: bool,
    ) -> None:
        expected = (self._configuration.asset_id, self._configuration.source_id)
        if (statistics_asset_id, statistics_source_id) != expected:
            raise ListedMarketRefreshError("statistics returned a different listed-asset context")
        if (diagnostic_asset_id, diagnostic_source_id) != expected:
            raise ListedMarketRefreshError("diagnostic returned a different listed-asset context")
        if not statistics_traceability or not diagnostic_traceability:
            raise ListedMarketRefreshError("listed-market analytical traceability was not verified")


__all__ = [
    "ListedMarketKnownAtTooEarlyError",
    "ListedMarketRefreshError",
    "ListedMarketRefreshPipeline",
]
