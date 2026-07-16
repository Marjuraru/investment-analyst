"""Resumable Apple workspace bootstrap built only from existing domain pipelines."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
)
from investment_analyst.analytics.market.diagnostic_pipeline import (
    MarketDiagnosticPipeline,
)
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
)
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplBootstrapStage,
    AaplBootstrapStageDetails,
    AaplBootstrapStageStatus,
    AaplBootstrapStageSummary,
    AaplMarketRefreshPlan,
    AaplWorkspaceBootstrapRequest,
    AaplWorkspaceBootstrapSummary,
)
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.core.models import DataFrequency, NormalizedObservation
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
)
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import (
    AlpacaHistoricalPipeline,
    AlpacaImportSummary,
)
from investment_analyst.providers.market.alpaca_stock import FEED
from investment_analyst.storage import LocalStorage
from investment_analyst.time_intervals import inclusive_utc_date_bounds


class AaplWorkspaceBootstrapError(RuntimeError):
    """Base error for the complete Apple workspace bootstrap."""


class BootstrapConfigurationError(AaplWorkspaceBootstrapError):
    """Raised when bootstrap dependencies or fixed-scope configuration are invalid."""


class BootstrapStageError(AaplWorkspaceBootstrapError):
    """Raised when one named existing pipeline fails."""

    def __init__(self, stage: AaplBootstrapStage, cause: BaseException) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage.value} failed: {cause}")


class BootstrapKnownAtTooEarlyError(AaplWorkspaceBootstrapError):
    """Raised before analytics when the requested cut predates ingested availability."""

    def __init__(
        self,
        *,
        stage: AaplBootstrapStage,
        requested_known_at: datetime,
        minimum_known_at: datetime,
    ) -> None:
        self.stage = stage
        self.requested_known_at = requested_known_at
        self.minimum_known_at = minimum_known_at
        super().__init__(
            f"known_at {requested_known_at.isoformat()} is too early for {stage.value}; "
            f"minimum_known_at is {minimum_known_at.isoformat()}"
        )


class BootstrapIncompleteError(AaplWorkspaceBootstrapError):
    """Raised after consolidation when a complete result was explicitly required."""

    def __init__(self, summary: AaplWorkspaceBootstrapSummary) -> None:
        self.summary = summary
        super().__init__(
            "bootstrap result is not complete; persisted stages can be resumed idempotently"
        )


class BootstrapTraceabilityError(AaplWorkspaceBootstrapError):
    """Raised when an existing stage returns an inconsistent auditable context."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BootstrapConfigurationError(f"{name} must include timezone information")
    return value.astimezone(UTC)


def _stage_status(generated: int, created: int, reused: int) -> AaplBootstrapStageStatus:
    if created > 0:
        return AaplBootstrapStageStatus.COMPLETED
    if generated > 0 and reused == generated:
        return AaplBootstrapStageStatus.REUSED
    return AaplBootstrapStageStatus.COMPLETED


def _stage_summary(
    *,
    stage: AaplBootstrapStage,
    generated: int,
    created: int,
    reused: int,
    timestamp: datetime,
    details: AaplBootstrapStageDetails,
    status: AaplBootstrapStageStatus | None = None,
) -> AaplBootstrapStageSummary:
    normalized = _utc(timestamp, f"{stage.value} timestamp")
    return AaplBootstrapStageSummary(
        stage=stage,
        status=status or _stage_status(generated, created, reused),
        generated=generated,
        created=created,
        reused=reused,
        started_at=normalized,
        completed_at=normalized,
        details=details,
        traceability_verified=True,
    )


class AaplWorkspaceBootstrapPipeline:
    """Run the complete Apple ingestion and analysis chain once per stage."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        workspace_id: UUID,
        sec_fetch_pipeline: SecAaplFundamentalsPipeline,
        sec_observation_pipeline: SecAaplObservationPipeline,
        market_pipeline: AlpacaHistoricalPipeline,
        fundamental_metric_pipeline: SecAaplFundamentalMetricPipeline,
        fundamental_diagnostic_pipeline: SecAaplFundamentalDiagnosticPipeline,
        market_statistics_pipeline: MarketStatisticsPipeline,
        market_diagnostic_pipeline: MarketDiagnosticPipeline,
        consolidated_service: AaplConsolidatedDiagnosticService,
        market_refresh_planner: AaplMarketRefreshPlanner | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._workspace_id = workspace_id
        self._sec_fetch_pipeline = sec_fetch_pipeline
        self._sec_observation_pipeline = sec_observation_pipeline
        self._market_pipeline = market_pipeline
        self._fundamental_metric_pipeline = fundamental_metric_pipeline
        self._fundamental_diagnostic_pipeline = fundamental_diagnostic_pipeline
        self._market_statistics_pipeline = market_statistics_pipeline
        self._market_diagnostic_pipeline = market_diagnostic_pipeline
        self._consolidated_service = consolidated_service
        self._market_refresh_planner = market_refresh_planner or AaplMarketRefreshPlanner(storage)
        self._clock = clock

    def run(
        self,
        request: AaplWorkspaceBootstrapRequest,
    ) -> AaplWorkspaceBootstrapSummary:
        """Execute the fixed stage order without rollback or duplicate stage calls."""
        self._storage.require_open()
        start, end = inclusive_utc_date_bounds(
            request.market_start,
            request.market_end,
        )
        stages: list[AaplBootstrapStageSummary] = []

        sec_fetch = self._run_sec_fetch()
        stages.append(sec_fetch[0])
        sec_normalization = self._run_sec_normalization()
        stages.append(sec_normalization[0])
        refresh_plan, planning_stage = self._run_market_refresh_planning(
            request,
            timestamp=sec_normalization[1].normalized_at,
        )
        stages.append(planning_stage)
        market_fetch = self._run_market_fetch(
            refresh_plan,
            fallback_timestamp=planning_stage.completed_at,
        )
        stages.append(market_fetch[0])

        effective_known_at, known_at_stage = self._resolve_known_at(
            request,
            start=start,
            end=end,
        )
        stages.append(known_at_stage)

        fundamental_metrics = self._run_fundamental_metrics(
            request,
            effective_known_at,
        )
        stages.append(fundamental_metrics[0])
        fundamental_diagnostic = self._run_fundamental_diagnostic(
            request,
            effective_known_at,
        )
        stages.append(fundamental_diagnostic[0])

        market_query = HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=start,
            end=end,
            known_at=effective_known_at,
        )
        market_statistics = self._run_market_statistics(market_query)
        stages.append(market_statistics[0])
        market_diagnostic = self._run_market_diagnostic(market_query)
        stages.append(market_diagnostic[0])
        consolidated, consolidated_stage = self._run_consolidated(
            request,
            effective_known_at,
        )
        stages.append(consolidated_stage)

        summary = AaplWorkspaceBootstrapSummary(
            workspace_id=self._workspace_id,
            source="SEC EDGAR and Alpaca Market Data",
            feed=FEED,
            request=request,
            refresh_plan=refresh_plan,
            requested_known_at=request.requested_known_at,
            effective_known_at=effective_known_at,
            stages=tuple(stages),
            consolidated=consolidated,
            overall_status=consolidated.status,
            raw_records_created=(
                sec_fetch[1].raw_records_created
                + sum(
                    item.raw_records_created + item.coverage_receipts_created
                    for item in market_fetch[1]
                )
            ),
            raw_records_reused=(
                sec_fetch[1].raw_records_reused
                + sum(
                    item.raw_records_reused + item.coverage_receipts_reused
                    for item in market_fetch[1]
                )
            ),
            observations_created=(
                sec_normalization[1].observations_created
                + sum(item.observations_created for item in market_fetch[1])
            ),
            observations_reused=(
                sec_normalization[1].observations_reused
                + sum(item.observations_reused for item in market_fetch[1])
            ),
            metric_results_created=(
                fundamental_metrics[1].metrics_created + market_statistics[1].results_created
            ),
            metric_results_reused=(
                fundamental_metrics[1].metrics_reused + market_statistics[1].results_reused
            ),
            diagnostics_created=(
                fundamental_diagnostic[1].diagnostics_created
                + market_diagnostic[1].diagnostics_created
            ),
            diagnostics_reused=(
                fundamental_diagnostic[1].diagnostics_reused
                + market_diagnostic[1].diagnostics_reused
            ),
            traceability_verified=True,
        )
        self._verify_summary(summary)
        if (
            request.require_complete
            and summary.overall_status is not ConsolidatedDiagnosticStatus.COMPLETE
        ):
            raise BootstrapIncompleteError(summary)
        return summary

    def _run_sec_fetch(self):
        stage = AaplBootstrapStage.SEC_FETCH
        try:
            result = self._sec_fetch_pipeline.run()
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        if result.asset_id != ASSET_ID or not result.traceability_verified:
            raise BootstrapTraceabilityError("SEC fetch returned an invalid Apple context")
        return (
            _stage_summary(
                stage=stage,
                generated=result.documents_received,
                created=result.raw_records_created,
                reused=result.raw_records_reused,
                timestamp=result.retrieved_at,
                details=AaplBootstrapStageDetails(
                    source_id="sec-edgar:aapl:submissions+companyfacts",
                    message="Official SEC raw snapshots persisted or reused.",
                ),
            ),
            result,
        )

    def _run_sec_normalization(self):
        stage = AaplBootstrapStage.SEC_NORMALIZATION
        try:
            result = self._sec_observation_pipeline.run()
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        if result.asset_id != ASSET_ID or not result.traceability_verified:
            raise BootstrapTraceabilityError("SEC normalization returned an invalid Apple context")
        return (
            _stage_summary(
                stage=stage,
                generated=result.observations_generated,
                created=result.observations_created,
                reused=result.observations_reused,
                timestamp=result.normalized_at,
                details=AaplBootstrapStageDetails(
                    source_id=COMPANYFACTS_SOURCE_ID,
                    message="Five explicit SEC concepts normalized from local snapshots.",
                ),
            ),
            result,
        )

    def _run_market_refresh_planning(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        timestamp: datetime,
    ) -> tuple[AaplMarketRefreshPlan, AaplBootstrapStageSummary]:
        stage = AaplBootstrapStage.MARKET_REFRESH_PLANNING
        try:
            plan = self._market_refresh_planner.plan(
                requested_start=request.market_start,
                requested_end=request.market_end,
                refresh_mode=request.refresh_mode,
            )
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        if not plan.traceability_verified:
            raise BootstrapTraceabilityError("market refresh plan is not traceable")
        return plan, _stage_summary(
            stage=stage,
            generated=0,
            created=0,
            reused=0,
            timestamp=timestamp,
            details=AaplBootstrapStageDetails(
                source_id=SOURCE_ID,
                feed=FEED,
                message=plan.reason,
            ),
        )

    def _run_market_fetch(
        self,
        plan: AaplMarketRefreshPlan,
        *,
        fallback_timestamp: datetime,
    ) -> tuple[AaplBootstrapStageSummary, tuple[AlpacaImportSummary, ...]]:
        stage = AaplBootstrapStage.MARKET_FETCH
        if not plan.market_fetch_required:
            return (
                _stage_summary(
                    stage=stage,
                    status=AaplBootstrapStageStatus.SKIPPED,
                    generated=0,
                    created=0,
                    reused=0,
                    timestamp=fallback_timestamp,
                    details=AaplBootstrapStageDetails(
                        source_id=SOURCE_ID,
                        feed=FEED,
                        intervals_executed=0,
                        bars_processed=0,
                        coverage_receipts_created=0,
                        coverage_receipts_reused=0,
                        empty_intervals_completed=0,
                        message="Requested Apple IEX range is already covered; fetch skipped.",
                    ),
                ),
                (),
            )

        results: list[AlpacaImportSummary] = []
        for interval in plan.fetch_intervals:
            start, end = inclusive_utc_date_bounds(interval.start, interval.end)
            try:
                result = self._market_pipeline.run(start, end)
            except (RuntimeError, ValueError) as error:
                raise BootstrapStageError(stage, error) from error
            if (
                result.asset_id != ASSET_ID
                or result.source_id != SOURCE_ID
                or result.feed != FEED
                or not result.traceability_verified
            ):
                raise BootstrapTraceabilityError(
                    "market fetch returned an invalid Apple IEX context"
                )
            results.append(result)

        generated = sum(
            item.raw_records_created
            + item.raw_records_reused
            + item.coverage_receipts_created
            + item.coverage_receipts_reused
            + item.observations_created
            + item.observations_reused
            for item in results
        )
        created = sum(
            item.raw_records_created + item.coverage_receipts_created + item.observations_created
            for item in results
        )
        reused = sum(
            item.raw_records_reused + item.coverage_receipts_reused + item.observations_reused
            for item in results
        )
        empty_intervals = sum(item.empty_intervals_completed for item in results)
        return (
            _stage_summary(
                stage=stage,
                generated=generated,
                created=created,
                reused=reused,
                timestamp=max(item.retrieved_at for item in results),
                status=(AaplBootstrapStageStatus.COMPLETED if empty_intervals else None),
                details=AaplBootstrapStageDetails(
                    source_id=SOURCE_ID,
                    feed=FEED,
                    intervals_executed=len(results),
                    bars_processed=sum(item.bars_received for item in results),
                    coverage_receipts_created=sum(
                        item.coverage_receipts_created for item in results
                    ),
                    coverage_receipts_reused=sum(item.coverage_receipts_reused for item in results),
                    empty_intervals_completed=empty_intervals,
                    message=(f"{len(results)} planned Alpaca IEX interval(s) persisted or reused."),
                ),
            ),
            tuple(results),
        )

    def _resolve_known_at(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[datetime, AaplBootstrapStageSummary]:
        observations = tuple(self._storage.observations.list(asset_id=ASSET_ID))
        requirements = self._availability_requirements(
            observations,
            request=request,
            start=start,
            end=end,
        )
        if request.requested_known_at is None:
            effective = _utc(self._clock(), "bootstrap clock")
        else:
            effective = request.requested_known_at
        violated = [item for item in requirements if effective < item[1]]
        if violated:
            stage, minimum = max(violated, key=lambda item: item[1])
            raise BootstrapKnownAtTooEarlyError(
                stage=stage,
                requested_known_at=effective,
                minimum_known_at=minimum,
            )
        minimum = max((item[1] for item in requirements), default=None)
        return effective, _stage_summary(
            stage=AaplBootstrapStage.KNOWN_AT_RESOLUTION,
            generated=0,
            created=0,
            reused=0,
            timestamp=effective,
            details=AaplBootstrapStageDetails(
                minimum_known_at=minimum,
                effective_known_at=effective,
                message=(
                    "Automatic UTC cut captured after ingestion."
                    if request.requested_known_at is None
                    else "Explicit UTC cut preserved without adjustment."
                ),
            ),
        )

    @staticmethod
    def _availability_requirements(
        observations: tuple[NormalizedObservation, ...],
        *,
        request: AaplWorkspaceBootstrapRequest,
        start: datetime,
        end: datetime,
    ) -> tuple[tuple[AaplBootstrapStage, datetime], ...]:
        market = tuple(
            item.available_at
            for item in observations
            if item.source.source_id == SOURCE_ID
            and item.frequency is DataFrequency.DAY_1
            and item.observed_at is not None
            and start <= item.observed_at < end
        )
        fundamentals = tuple(
            item.available_at
            for item in observations
            if item.source.source_id == COMPANYFACTS_SOURCE_ID
            and item.frequency is request.fundamental_frequency
            and item.transformation_version == TRANSFORMATION_VERSION
        )
        requirements: list[tuple[AaplBootstrapStage, datetime]] = []
        if market:
            requirements.append((AaplBootstrapStage.MARKET_FETCH, min(market)))
        if fundamentals:
            requirements.append((AaplBootstrapStage.SEC_NORMALIZATION, min(fundamentals)))
        return tuple(requirements)

    def _run_fundamental_metrics(
        self,
        request: AaplWorkspaceBootstrapRequest,
        known_at: datetime,
    ):
        stage = AaplBootstrapStage.FUNDAMENTAL_METRICS
        metric_request = SecFundamentalMetricRequest(
            known_at=known_at,
            frequency=request.fundamental_frequency,
        )
        try:
            result = self._fundamental_metric_pipeline.run(metric_request)
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        self._verify_known_at(result.known_at, known_at, stage)
        return (
            _stage_summary(
                stage=stage,
                generated=result.metrics_generated,
                created=result.metrics_created,
                reused=result.metrics_reused,
                timestamp=result.computed_at,
                details=AaplBootstrapStageDetails(
                    source_id=COMPANYFACTS_SOURCE_ID,
                    effective_known_at=known_at,
                    message="Fundamental ratios computed from selected SEC observations.",
                ),
            ),
            result,
        )

    def _run_fundamental_diagnostic(
        self,
        request: AaplWorkspaceBootstrapRequest,
        known_at: datetime,
    ):
        stage = AaplBootstrapStage.FUNDAMENTAL_DIAGNOSTIC
        diagnostic_request = SecFundamentalDiagnosticRequest(
            known_at=known_at,
            frequency=request.fundamental_frequency,
        )
        try:
            result = self._fundamental_diagnostic_pipeline.run(diagnostic_request)
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        self._verify_known_at(result.known_at, known_at, stage)
        return (
            _stage_summary(
                stage=stage,
                generated=result.diagnostics_generated,
                created=result.diagnostics_created,
                reused=result.diagnostics_reused,
                timestamp=result.computed_at,
                details=AaplBootstrapStageDetails(
                    source_id=COMPANYFACTS_SOURCE_ID,
                    effective_known_at=known_at,
                    message="Fundamental diagnostic persisted from local metrics.",
                ),
            ),
            result,
        )

    def _run_market_statistics(self, query: HistoricalBarQuery):
        stage = AaplBootstrapStage.MARKET_STATISTICS
        try:
            result = self._market_statistics_pipeline.run(MarketStatisticsRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        self._verify_known_at(result.known_at, query.known_at, stage)
        return (
            _stage_summary(
                stage=stage,
                generated=result.results_generated,
                created=result.results_created,
                reused=result.results_reused,
                timestamp=result.computed_at,
                details=AaplBootstrapStageDetails(
                    source_id=SOURCE_ID,
                    feed=FEED,
                    effective_known_at=query.known_at,
                    message="Market statistics computed from local IEX observations.",
                ),
            ),
            result,
        )

    def _run_market_diagnostic(self, query: HistoricalBarQuery):
        stage = AaplBootstrapStage.MARKET_DIAGNOSTIC
        try:
            result = self._market_diagnostic_pipeline.run(MarketDiagnosticRequest(query=query))
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        self._verify_known_at(result.known_at, query.known_at, stage)
        return (
            _stage_summary(
                stage=stage,
                generated=result.diagnostics_generated,
                created=result.diagnostics_created,
                reused=result.diagnostics_reused,
                timestamp=result.computed_at,
                details=AaplBootstrapStageDetails(
                    source_id=SOURCE_ID,
                    feed=FEED,
                    effective_known_at=query.known_at,
                    message="Market diagnostic persisted from local statistics.",
                ),
            ),
            result,
        )

    def _run_consolidated(
        self,
        request: AaplWorkspaceBootstrapRequest,
        known_at: datetime,
    ) -> tuple[ConsolidatedDiagnosticView, AaplBootstrapStageSummary]:
        stage = AaplBootstrapStage.CONSOLIDATED_QUERY
        consolidated_request = ConsolidatedDiagnosticRequest(
            known_at=known_at,
            fundamental_frequency=request.fundamental_frequency,
        )
        try:
            view = self._consolidated_service.query(consolidated_request)
        except (RuntimeError, ValueError) as error:
            raise BootstrapStageError(stage, error) from error
        if not view.traceability_verified or view.request.known_at != known_at:
            raise BootstrapTraceabilityError(
                "consolidated query returned an inconsistent point-in-time context"
            )
        timestamp = self._consolidated_timestamp(view, known_at)
        return view, _stage_summary(
            stage=stage,
            status=AaplBootstrapStageStatus.COMPLETED,
            generated=0,
            created=0,
            reused=0,
            timestamp=timestamp,
            details=AaplBootstrapStageDetails(
                effective_known_at=known_at,
                consolidated_status=view.status,
                market_status=view.market.status,
                fundamental_status=view.fundamental.status,
                message="Independent market and fundamental diagnostics queried once.",
            ),
        )

    @staticmethod
    def _consolidated_timestamp(
        view: ConsolidatedDiagnosticView,
        fallback: datetime,
    ) -> datetime:
        values = [
            section.diagnostic.computed_at
            for section in (view.market, view.fundamental)
            if section.status is ConsolidatedSectionStatus.AVAILABLE
            and section.diagnostic is not None
        ]
        return max(values, default=fallback)

    @staticmethod
    def _verify_known_at(
        actual: datetime,
        expected: datetime,
        stage: AaplBootstrapStage,
    ) -> None:
        if actual != expected:
            raise BootstrapTraceabilityError(f"{stage.value} did not preserve effective_known_at")

    @staticmethod
    def _verify_summary(summary: AaplWorkspaceBootstrapSummary) -> None:
        if summary.feed != FEED or summary.asset_id != ASSET_ID:
            raise BootstrapTraceabilityError("bootstrap summary changed fixed scope")
        if (
            summary.consolidated.market.diagnostic is not None
            and summary.consolidated.market.diagnostic.asset_id != ASSET_ID
        ):
            raise BootstrapTraceabilityError("market diagnostic belongs to another asset")
        if (
            summary.consolidated.fundamental.diagnostic is not None
            and summary.consolidated.fundamental.diagnostic.asset_id != ASSET_ID
        ):
            raise BootstrapTraceabilityError("fundamental diagnostic belongs to another asset")
        serialized = summary.to_json_dict()
        if "combined_score" in serialized or "combined_verdict" in serialized:
            raise BootstrapTraceabilityError("bootstrap must not combine diagnostics")
