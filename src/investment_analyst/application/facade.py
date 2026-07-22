"""Stable application facade for Apple bootstrap and diagnostic queries."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from pydantic import ConfigDict, model_validator

from investment_analyst.analytics.aapl_daily_report_models import AaplDailyDiagnosticReport
from investment_analyst.analytics.aapl_daily_report_service import AaplDailyReportService
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendRequest,
)
from investment_analyst.analytics.fundamental_trend_service import AaplFundamentalTrendService
from investment_analyst.analytics.fundamentals.analysis_models import (
    AaplFundamentalAnalysisResult,
)
from investment_analyst.analytics.fundamentals.analysis_service import (
    AaplFundamentalAnalysisService,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_history_service import (
    AaplFundamentalResearchHistoryService,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
)
from investment_analyst.analytics.fundamentals.research_service import (
    AaplFundamentalResearchService,
)
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartRequest,
)
from investment_analyst.analytics.market.chart_service import AaplMarketChartService
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.aapl_bootstrap import AaplWorkspaceBootstrapPipeline
from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
    AaplWorkspaceBootstrapSummary,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.core.models.base import ContractModel
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarClient, SecEdgarIdentity
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import SecAaplFundamentalsPipeline
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.http import HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.storage import LocalStorage
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInitialization

HttpTransportFactory = Callable[[], HttpTransport]


class AaplApplicationBootstrapResult(ContractModel):
    """Workspace initialization and bootstrap summary returned as one typed result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    initialization: WorkspaceInitialization
    summary: AaplWorkspaceBootstrapSummary

    @model_validator(mode="after")
    def validate_workspace_identity(self) -> "AaplApplicationBootstrapResult":
        """Keep the initialized workspace and persisted bootstrap identity aligned."""
        if self.initialization.manifest.workspace_id != self.summary.workspace_id:
            raise ValueError("bootstrap summary workspace_id must match the initialized workspace")
        return self


class InvestmentAnalystApplication:
    """Stable entry point that keeps provider composition outside the analytical core."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        transport_factory: HttpTransportFactory = UrlLibHttpTransport,
    ) -> None:
        self._runtime = runtime
        self._transport_factory = transport_factory

    @classmethod
    def create_default(cls) -> "InvestmentAnalystApplication":
        """Create the production facade without opening storage or reading credentials."""
        return cls(ApplicationRuntime.create_default())

    def bootstrap_aapl_workspace(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplApplicationBootstrapResult:
        """Initialize one workspace and run the complete resumable Apple pipeline."""
        initialization = self._runtime.workspace_service.initialize(workspace)
        location = StorageLocationRequest(workspace=initialization.paths.root)
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = self._build_aapl_bootstrap_pipeline(
                storage,
                workspace_id=initialization.manifest.workspace_id,
                alpaca_credentials=alpaca_credentials,
                sec_identity=sec_identity,
            ).run(request)
        return AaplApplicationBootstrapResult(
            initialization=initialization,
            summary=summary,
        )

    def query_aapl_diagnostics(
        self,
        request: ConsolidatedDiagnosticRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplDailyDiagnosticReport:
        """Return one versioned report from persisted data without writes or providers."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplDailyReportService(storage).query(request)

    def query_aapl_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplMarketChart:
        """Return a bounded point-in-time market chart without writes or providers."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplMarketChartService(
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
            ).query(request)

    def query_aapl_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Return bounded point-in-time SEC trends without writes or providers."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplFundamentalTrendService(SecAaplFundamentalPointInTimeService(storage)).query(
                request
            )

    def query_aapl_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        """Calculate point-in-time research metrics without writes or providers."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplFundamentalResearchService(storage).query(request)

    def query_aapl_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate historical research statistics without writes or providers."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplFundamentalResearchHistoryService(
                AaplFundamentalResearchService(storage)
            ).query(request)

    def query_aapl_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        """Organize exact history into non-overlapping analytical sections."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return AaplFundamentalAnalysisService(
                AaplFundamentalResearchHistoryService(AaplFundamentalResearchService(storage))
            ).query(request)

    def _build_aapl_bootstrap_pipeline(
        self,
        storage: LocalStorage,
        *,
        workspace_id: UUID,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplWorkspaceBootstrapPipeline:
        alpaca_configuration = resolve_alpaca_configuration(self._runtime.provider_resolver)
        sec_configuration = resolve_sec_configuration(self._runtime.provider_resolver)
        transport = self._transport_factory()
        sec_client = SecEdgarClient(
            transport,
            sec_identity,
            cik=sec_configuration.cik,
            ticker=sec_configuration.ticker,
        )
        point_in_time = SecAaplFundamentalPointInTimeService(storage)
        history = HistoricalMarketDataService(storage)
        return AaplWorkspaceBootstrapPipeline(
            storage,
            workspace_id=workspace_id,
            sec_fetch_pipeline=SecAaplFundamentalsPipeline(
                storage,
                sec_client,
                configuration=sec_configuration,
            ),
            sec_observation_pipeline=SecAaplObservationPipeline(
                storage,
                SecCompanyFactsNormalizer(),
            ),
            market_pipeline=AlpacaHistoricalPipeline(
                storage,
                AlpacaStockClient(transport, alpaca_credentials),
                configuration=alpaca_configuration,
            ),
            fundamental_metric_pipeline=SecAaplFundamentalMetricPipeline(
                storage,
                point_in_time,
                SecFundamentalMetricEngine(),
            ),
            fundamental_diagnostic_pipeline=SecAaplFundamentalDiagnosticPipeline(
                storage,
                SecFundamentalDiagnosticSelector(storage),
                SecFundamentalDiagnosticEngine(),
            ),
            market_statistics_pipeline=MarketStatisticsPipeline(
                storage,
                history,
                MarketStatisticsEngine(),
            ),
            market_diagnostic_pipeline=MarketDiagnosticPipeline(
                storage,
                MarketDiagnosticMetricSelector(storage),
                MarketDiagnosticEngine(),
            ),
            consolidated_service=AaplConsolidatedDiagnosticService(storage),
        )
