"""Resumable orchestration of Apple IEX market analytics and consolidated diagnostics."""

from datetime import UTC, date, datetime
from decimal import Context, localcontext

from investment_analyst.analytics.aapl_snapshot_models import (
    AaplCompleteSnapshotRequest,
    AaplCompleteSnapshotSummary,
    AaplSnapshotStage,
    AaplSnapshotStageStatus,
    AaplSnapshotStageSummary,
    ConsolidatedQueryStageDetails,
    FundamentalRefreshStageDetails,
    MarketDiagnosticStageDetails,
    MarketFetchStageDetails,
    MarketStatisticsStageDetails,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
    ConsolidatedSectionStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import ALGORITHM_VERSION
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.core.models import DiagnosticMode
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import FEED
from investment_analyst.storage import LocalStorage


class AaplCompleteSnapshotPipelineError(RuntimeError):
    """Base error for Apple snapshot orchestration."""


class AaplSnapshotConfigurationError(AaplCompleteSnapshotPipelineError):
    """Raised when injected dependencies or fixed scope are inconsistent."""


class AaplSnapshotStageError(AaplCompleteSnapshotPipelineError):
    """Raised when one named stage fails."""

    def __init__(self, stage: AaplSnapshotStage, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage.value} failed: {cause}")


class AaplSnapshotIncompleteError(AaplCompleteSnapshotPipelineError):
    """Raised after a verified non-complete run when completeness is required."""

    def __init__(self, summary: AaplCompleteSnapshotSummary) -> None:
        self.summary = summary
        super().__init__(
            "consolidated result is not complete; completed stages remain persisted and "
            "the run can be resumed idempotently"
        )


class AaplSnapshotTraceabilityError(AaplCompleteSnapshotPipelineError):
    """Raised when stage outputs disagree with the fixed Apple IEX traceability contract."""


def _utc_clock(clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AaplSnapshotConfigurationError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _midnight(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _stage_status(generated: int, created: int, reused: int) -> AaplSnapshotStageStatus:
    if generated > 0 and created == 0 and reused == generated:
        return AaplSnapshotStageStatus.REUSED
    return AaplSnapshotStageStatus.COMPLETED


class AaplCompleteSnapshotPipeline:
    """Run each existing market layer once, skip fundamentals, and query consolidation."""

    def __init__(
        self,
        storage: LocalStorage,
        market_pipeline: AlpacaHistoricalPipeline,
        statistics_pipeline: MarketStatisticsPipeline,
        market_diagnostic_pipeline: MarketDiagnosticPipeline,
        consolidated_service: AaplConsolidatedDiagnosticService,
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._market_pipeline = market_pipeline
        self._statistics_pipeline = statistics_pipeline
        self._market_diagnostic_pipeline = market_diagnostic_pipeline
        self._consolidated_service = consolidated_service
        self._clock = clock

    def run(self, request: AaplCompleteSnapshotRequest) -> AaplCompleteSnapshotSummary:
        """Execute the five ordered stages exactly once and return a compact summary."""
        self._storage.require_open()
        if request.asset_id != ASSET_ID:
            raise AaplSnapshotConfigurationError("only Apple is supported")
        started_at = _utc_clock(self._clock)
        start = _midnight(request.market_start)
        end = _midnight(request.market_end)
        query = HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=start,
            end=end,
            known_at=request.known_at,
        )

        market_fetch = self._run_market_fetch(start, end)
        statistics = self._run_statistics(MarketStatisticsRequest(query=query))
        market_diagnostic = self._run_market_diagnostic(MarketDiagnosticRequest(query=query))
        fundamental_refresh = self._fundamental_skipped()
        consolidated = self._run_consolidated(request)
        completed_at = _utc_clock(self._clock)

        stages = (
            market_fetch,
            statistics,
            market_diagnostic,
            fundamental_refresh,
            consolidated,
        )
        summary = AaplCompleteSnapshotSummary(
            request=request,
            source="Alpaca Market Data",
            feed=FEED,
            market_fetch=market_fetch,
            market_statistics=statistics,
            market_diagnostic=market_diagnostic,
            fundamental_refresh=fundamental_refresh,
            consolidated=consolidated,
            overall_status=consolidated.details.view.status,
            started_at=started_at,
            completed_at=completed_at,
            stages_completed=sum(
                item.status is AaplSnapshotStageStatus.COMPLETED for item in stages
            ),
            stages_reused=sum(item.status is AaplSnapshotStageStatus.REUSED for item in stages),
            stages_skipped=sum(item.status is AaplSnapshotStageStatus.SKIPPED for item in stages),
            traceability_verified=True,
        )
        self._verify_summary(summary)
        if (
            request.require_complete
            and summary.overall_status is not ConsolidatedDiagnosticStatus.COMPLETE
        ):
            raise AaplSnapshotIncompleteError(summary)
        return summary

    def _run_market_fetch(self, start: datetime, end: datetime) -> AaplSnapshotStageSummary:
        stage = AaplSnapshotStage.MARKET_FETCH
        started = _utc_clock(self._clock)
        try:
            result = self._market_pipeline.run(start, end)
        except Exception as error:
            raise AaplSnapshotStageError(stage, error) from error
        completed = _utc_clock(self._clock)
        if result.asset_id != ASSET_ID or result.source_id != SOURCE_ID:
            raise AaplSnapshotTraceabilityError("market fetch returned another asset or source")
        if result.feed != FEED:
            raise AaplSnapshotTraceabilityError("market fetch did not use the fixed IEX feed")
        if not result.traceability_verified:
            raise AaplSnapshotTraceabilityError("market fetch traceability was not verified")
        created = result.raw_records_created + result.observations_created
        reused = result.raw_records_reused + result.observations_reused
        generated = created + reused
        return AaplSnapshotStageSummary(
            stage=stage,
            status=_stage_status(generated, created, reused),
            records_generated=generated,
            records_created=created,
            records_reused=reused,
            started_at=started,
            completed_at=completed,
            details=MarketFetchStageDetails(
                asset_id=result.asset_id,
                source_id=result.source_id,
                feed=result.feed,
                adjustment=result.adjustment,
                request_count=result.request_count,
                bars_received=result.bars_received,
                raw_records_created=result.raw_records_created,
                raw_records_reused=result.raw_records_reused,
                observations_created=result.observations_created,
                observations_reused=result.observations_reused,
                earliest_bar=result.earliest_bar,
                latest_bar=result.latest_bar,
            ),
            traceability_verified=True,
        )

    def _run_statistics(self, request: MarketStatisticsRequest) -> AaplSnapshotStageSummary:
        stage = AaplSnapshotStage.MARKET_STATISTICS
        started = _utc_clock(self._clock)
        try:
            result = self._statistics_pipeline.run(request)
        except Exception as error:
            raise AaplSnapshotStageError(stage, error) from error
        completed = _utc_clock(self._clock)
        if (
            result.asset_id != ASSET_ID
            or result.source_id != SOURCE_ID
            or result.known_at != request.query.known_at
        ):
            raise AaplSnapshotTraceabilityError("market statistics context is inconsistent")
        if not result.traceability_verified:
            raise AaplSnapshotTraceabilityError("market statistics traceability was not verified")
        return AaplSnapshotStageSummary(
            stage=stage,
            status=_stage_status(
                result.results_generated,
                result.results_created,
                result.results_reused,
            ),
            records_generated=result.results_generated,
            records_created=result.results_created,
            records_reused=result.results_reused,
            started_at=started,
            completed_at=completed,
            details=MarketStatisticsStageDetails(
                asset_id=result.asset_id,
                source_id=result.source_id,
                known_at=result.known_at,
                bar_count=result.bar_count,
                definitions_upserted=result.definitions_upserted,
                results_generated=result.results_generated,
                results_created=result.results_created,
                results_reused=result.results_reused,
                earliest_as_of=result.earliest_as_of,
                latest_as_of=result.latest_as_of,
            ),
            traceability_verified=True,
        )

    def _run_market_diagnostic(
        self,
        request: MarketDiagnosticRequest,
    ) -> AaplSnapshotStageSummary:
        stage = AaplSnapshotStage.MARKET_DIAGNOSTIC
        started = _utc_clock(self._clock)
        try:
            with localcontext(Context(prec=34)):
                result = self._market_diagnostic_pipeline.run(request)
        except Exception as error:
            raise AaplSnapshotStageError(stage, error) from error
        completed = _utc_clock(self._clock)
        if (
            result.asset_id != ASSET_ID
            or result.source_id != SOURCE_ID
            or result.known_at != request.query.known_at
        ):
            raise AaplSnapshotTraceabilityError("market diagnostic context is inconsistent")
        if not result.traceability_verified:
            raise AaplSnapshotTraceabilityError("market diagnostic traceability was not verified")
        return AaplSnapshotStageSummary(
            stage=stage,
            status=_stage_status(
                result.diagnostics_generated,
                result.diagnostics_created,
                result.diagnostics_reused,
            ),
            records_generated=result.diagnostics_generated,
            records_created=result.diagnostics_created,
            records_reused=result.diagnostics_reused,
            started_at=started,
            completed_at=completed,
            details=MarketDiagnosticStageDetails(
                asset_id=result.asset_id,
                source_id=result.source_id,
                known_at=result.known_at,
                as_of=result.as_of,
                verdict=result.verdict,
                diagnostics_generated=result.diagnostics_generated,
                diagnostics_created=result.diagnostics_created,
                diagnostics_reused=result.diagnostics_reused,
            ),
            traceability_verified=True,
        )

    def _fundamental_skipped(self) -> AaplSnapshotStageSummary:
        timestamp = _utc_clock(self._clock)
        return AaplSnapshotStageSummary(
            stage=AaplSnapshotStage.FUNDAMENTAL_REFRESH,
            status=AaplSnapshotStageStatus.SKIPPED,
            records_generated=0,
            records_created=0,
            records_reused=0,
            started_at=timestamp,
            completed_at=timestamp,
            details=FundamentalRefreshStageDetails(),
            traceability_verified=True,
        )

    def _run_consolidated(
        self,
        request: AaplCompleteSnapshotRequest,
    ) -> AaplSnapshotStageSummary:
        stage = AaplSnapshotStage.CONSOLIDATED_QUERY
        started = _utc_clock(self._clock)
        consolidated_request = ConsolidatedDiagnosticRequest(
            asset_id=request.asset_id,
            known_at=request.known_at,
            fundamental_frequency=request.fundamental_frequency,
            market_as_of=request.market_as_of,
            fundamental_as_of=request.fundamental_as_of,
        )
        try:
            view = self._consolidated_service.query(consolidated_request)
        except Exception as error:
            raise AaplSnapshotStageError(stage, error) from error
        completed = _utc_clock(self._clock)
        if not view.traceability_verified:
            raise AaplSnapshotTraceabilityError("consolidated traceability was not verified")
        return AaplSnapshotStageSummary(
            stage=stage,
            status=AaplSnapshotStageStatus.COMPLETED,
            records_generated=0,
            records_created=0,
            records_reused=0,
            started_at=started,
            completed_at=completed,
            details=ConsolidatedQueryStageDetails(view=view),
            traceability_verified=True,
        )

    @staticmethod
    def _verify_summary(summary: AaplCompleteSnapshotSummary) -> None:
        view = summary.consolidated.details.view
        if view.request.asset_id != ASSET_ID or view.request.known_at != summary.request.known_at:
            raise AaplSnapshotTraceabilityError("consolidated request does not match the run")
        market = view.market
        if market.status is ConsolidatedSectionStatus.AVAILABLE:
            diagnostic = market.diagnostic
            if diagnostic is None or diagnostic.asset_id != ASSET_ID:
                raise AaplSnapshotTraceabilityError("market diagnostic does not belong to Apple")
            if diagnostic.mode is not DiagnosticMode.MARKET:
                raise AaplSnapshotTraceabilityError("market section contains another mode")
            if diagnostic.algorithm_version != ALGORITHM_VERSION:
                raise AaplSnapshotTraceabilityError("market diagnostic version is not current")
        fundamental = view.fundamental
        if fundamental.status is ConsolidatedSectionStatus.AVAILABLE:
            diagnostic = fundamental.diagnostic
            if diagnostic is None or diagnostic.asset_id != ASSET_ID:
                raise AaplSnapshotTraceabilityError(
                    "fundamental diagnostic does not belong to Apple"
                )
            if diagnostic.mode is not DiagnosticMode.FUNDAMENTAL:
                raise AaplSnapshotTraceabilityError("fundamental section contains another mode")
            if diagnostic.algorithm_version != FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION:
                raise AaplSnapshotTraceabilityError("fundamental diagnostic version is not current")
        payload = summary.to_json_dict()
        forbidden = {
            "combined_score",
            "combined_verdict",
            "combined_confidence",
            "combined_quality",
            "recommendation",
            "ranking",
        }
        if forbidden.intersection(payload):
            raise AaplSnapshotTraceabilityError("snapshot introduced a combined diagnostic field")


__all__ = [
    "AaplCompleteSnapshotPipeline",
    "AaplCompleteSnapshotPipelineError",
    "AaplSnapshotConfigurationError",
    "AaplSnapshotIncompleteError",
    "AaplSnapshotStageError",
    "AaplSnapshotTraceabilityError",
]
