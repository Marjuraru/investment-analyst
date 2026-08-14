"""Stable application facade for local investment-analysis workflows."""

from collections.abc import Callable
from datetime import UTC, date, datetime
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
from investment_analyst.analytics.crypto.derivatives_engine import (
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesQueryResult,
)
from investment_analyst.analytics.crypto.derivatives_service import (
    CryptoDerivativesService,
)
from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendRequest,
)
from investment_analyst.analytics.fundamental_trend_service import (
    SecIssuerFundamentalTrendService,
)
from investment_analyst.analytics.fundamentals.analysis_models import (
    AaplFundamentalAnalysisResult,
    FundamentalAnalysisResult,
)
from investment_analyst.analytics.fundamentals.analysis_service import (
    SecIssuerFundamentalAnalysisService,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
    FundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_history_service import (
    SecIssuerFundamentalResearchHistoryService,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
    FundamentalResearchResult,
)
from investment_analyst.analytics.fundamentals.research_service import (
    SecIssuerFundamentalResearchService,
)
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartRequest,
    BtcMarketChart,
    BtcMarketChartRequest,
    CryptoSpotDailyMarketChart,
    CryptoSpotDailyMarketChartRequest,
    ListedMarketChart,
)
from investment_analyst.analytics.market.chart_service import (
    AaplMarketChartService,
    BtcMarketChartService,
    CryptoSpotDailyMarketChartService,
    ListedMarketChartService,
)
from investment_analyst.analytics.market.comparison_models import (
    MarketComparisonAsset,
    MarketComparisonRequest,
    MarketMultiAssetComparisonResult,
)
from investment_analyst.analytics.market.comparison_service import MarketComparisonService
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.analytics.valuation import (
    CorporateValuationPersistencePipeline,
    CorporateValuationRequest,
    CorporateValuationService,
    CorporateValuationSnapshot,
    ValuationPersistenceSummary,
)
from investment_analyst.application.aapl_bootstrap import AaplWorkspaceBootstrapPipeline
from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
    AaplWorkspaceBootstrapSummary,
)
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisFamily,
    analysis_capabilities_for,
)
from investment_analyst.application.btc_intraday import (
    query_btc_intraday_chart,
    refresh_btc_intraday,
)
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayChart,
    BtcIntradayChartRequest,
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.application.btc_refresh import (
    BtcMarketExecutionClock,
    BtcMarketRefreshPipeline,
)
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
)
from investment_analyst.application.btc_refresh_planner import BtcMarketRefreshPlanner
from investment_analyst.application.crypto_derivatives import (
    CryptoDerivativesRefreshService,
)
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesQueryRequest,
    CryptoDerivativesRefreshRequest,
    CryptoDerivativesRefreshSummary,
    public_date_bounds,
)
from investment_analyst.application.crypto_spot_daily import (
    CryptoSpotDailyRefreshPipeline,
)
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
    CryptoSpotDailyRefreshSummary,
)
from investment_analyst.application.crypto_spot_daily_planner import (
    CryptoSpotDailyRefreshPlanner,
)
from investment_analyst.application.listed_market_refresh import ListedMarketRefreshPipeline
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.market_universe import (
    MarketAssetUniverse,
    build_market_asset_universe,
)
from investment_analyst.application.peru_registry import (
    BvlRegistryRefreshRequest,
    BvlRegistryRefreshService,
    BvlRegistryRefreshSummary,
    BvlRegistryUniverse,
    BvlRegistryUniverseRequest,
    BvlRegistryUniverseService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalRefreshPipeline,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_deribit_configuration,
    resolve_sec_configuration,
)
from investment_analyst.core.models.base import ContractModel
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_pipeline import (
    CoinbaseHistoricalPipeline,
    CoinbaseIntradayPipeline,
)
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
    SecIssuerFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarClient, SecEdgarIdentity
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
    SecIssuerFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
    SecIssuerObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
    SecIssuerFundamentalsPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
    SecIssuerFundamentalPointInTimeService,
)
from investment_analyst.providers.http import (
    HttpFormTransport,
    HttpTransport,
    UrlLibHttpTransport,
)
from investment_analyst.providers.macro.fred_alfred import FredAlfredClient, FredApiKey
from investment_analyst.providers.macro.fred_catalog_refresh import (
    FredCatalogRefreshRequest,
    FredCatalogRefreshService,
    FredCatalogRefreshSummary,
)
from investment_analyst.providers.macro.fred_pipeline import (
    FredVintageImportSummary,
    FredVintagePipeline,
)
from investment_analyst.providers.macro.fred_point_in_time import (
    FredPointInTimeQuery,
    FredPointInTimeResult,
    FredPointInTimeService,
)
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.providers.peru.smv_open_data import SmvOpenDataClient
from investment_analyst.storage import LocalStorage
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInitialization

HttpTransportFactory = Callable[[], HttpTransport]
HttpFormTransportFactory = Callable[[], HttpFormTransport]


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
        form_transport_factory: HttpFormTransportFactory = UrlLibHttpTransport,
    ) -> None:
        self._runtime = runtime
        self._transport_factory = transport_factory
        self._form_transport_factory = form_transport_factory

    @classmethod
    def create_default(cls) -> "InvestmentAnalystApplication":
        """Create the production facade without opening storage or reading credentials."""
        return cls(ApplicationRuntime.create_default())

    def list_market_assets(self) -> MarketAssetUniverse:
        """Return the deterministic catalog-backed market watchlist without storage I/O."""
        return build_market_asset_universe(
            self._runtime.catalog,
            self._runtime.provider_resolver,
        )

    def refresh_fred_vintage(
        self,
        series_id: str,
        *,
        vintage_date: date,
        observation_start: date,
        observation_end: date,
        location: StorageLocationRequest,
        api_key: FredApiKey,
    ) -> FredVintageImportSummary:
        """Import one explicit official macro vintage without creating asset analytics."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            return FredVintagePipeline(
                storage,
                FredAlfredClient(self._transport_factory(), api_key),
            ).run(
                series_id,
                vintage_date=vintage_date,
                observation_start=observation_start,
                observation_end=observation_end,
            )

    def refresh_fred_catalog_series(
        self,
        request: FredCatalogRefreshRequest,
        *,
        location: StorageLocationRequest,
        api_key: FredApiKey,
    ) -> FredCatalogRefreshSummary:
        """Discover and import a bounded resumable batch for one configured series."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            return FredCatalogRefreshService(
                storage,
                FredAlfredClient(self._transport_factory(), api_key),
            ).run(request)

    def query_fred_point_in_time(
        self,
        request: FredPointInTimeQuery,
        *,
        location: StorageLocationRequest,
    ) -> FredPointInTimeResult:
        """Reconstruct one macro series locally at an explicit information cut."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return FredPointInTimeService(storage).query(request)

    def refresh_bvl_registry(
        self,
        request: BvlRegistryRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BvlRegistryRefreshSummary:
        """Refresh configured SMV identities through one resumable writer batch."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            return BvlRegistryRefreshService(
                storage,
                self._runtime.catalog,
                self._runtime.provider_resolver,
                SmvOpenDataClient(self._form_transport_factory()),
            ).run(request)

    def query_bvl_registry(
        self,
        request: BvlRegistryUniverseRequest,
        *,
        location: StorageLocationRequest,
    ) -> BvlRegistryUniverse:
        """Query configured BVL identities and local SMV evidence at one cut."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return BvlRegistryUniverseService(
                storage,
                self._runtime.catalog,
                self._runtime.provider_resolver,
            ).query(request)

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

    def query_market_comparison(
        self,
        request: MarketComparisonRequest,
        *,
        location: StorageLocationRequest,
    ) -> MarketMultiAssetComparisonResult:
        """Compare eligible catalog daily assets from one read-only local session."""
        visible_assets = {item.asset_id: item for item in self.list_market_assets().assets}
        resolved: dict[str, MarketComparisonAsset] = {}
        for asset_id in request.asset_ids:
            try:
                descriptor = visible_assets[asset_id]
            except KeyError as error:
                raise ValueError(
                    f"asset_id is not in the visible market universe: {asset_id}"
                ) from error
            if get_market_bar_schema(descriptor.source_id).frequency.value != "1d":
                raise ValueError("comparison requires a visible daily market asset")
            resolved[asset_id] = MarketComparisonAsset(
                asset_id=descriptor.asset_id,
                source_id=descriptor.source_id,
                quote_currency=descriptor.quote_currency,
            )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return MarketComparisonService(HistoricalMarketDataService(storage)).query(
                request,
                resolved,
            )

    def query_btc_market_chart(
        self,
        request: BtcMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketChart:
        """Return a bounded point-in-time Coinbase BTC-USD chart without writes."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return BtcMarketChartService(
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
            ).query(request)

    def query_crypto_spot_daily_market_chart(
        self,
        request: CryptoSpotDailyMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> CryptoSpotDailyMarketChart:
        """Return one read-only chart for an explicitly configured Coinbase daily asset."""
        configuration = resolve_coinbase_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode("read_only")
        ) as storage:
            return CryptoSpotDailyMarketChartService(
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
            ).query(
                request,
                source_id=configuration.source_id,
                volume_unit=configuration.base_unit,
            )

    def query_listed_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> ListedMarketChart:
        """Return a bounded point-in-time chart for one catalog-backed Alpaca asset."""
        configuration = resolve_alpaca_configuration(
            self._runtime.provider_resolver,
            asset_id=asset_id,
        )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return ListedMarketChartService(
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
            ).query(
                request,
                asset_id=configuration.asset_id,
                source_id=configuration.source_id,
            )

    def query_btc_intraday_chart(
        self,
        request: BtcIntradayChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayChart:
        """Return the latest bounded point-in-time BTC-USD intraday chart."""
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return query_btc_intraday_chart(storage, request)

    def refresh_btc_intraday(
        self,
        request: BtcIntradayRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayRefreshSummary:
        """Import one explicit 24-hour minute window without daily analytics."""
        configuration = resolve_coinbase_intraday_configuration(self._runtime.provider_resolver)
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            return refresh_btc_intraday(
                CoinbaseIntradayPipeline(
                    storage,
                    CoinbaseExchangeClient(self._transport_factory()),
                    configuration=configuration,
                ),
                request,
                now=datetime.now(UTC),
            )

    def refresh_btc_market(
        self,
        request: BtcMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketRefreshSummary:
        """Incrementally update Coinbase BTC-USD and persist independent market analytics."""
        configuration = resolve_coinbase_configuration(self._runtime.provider_resolver)
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            execution_clock = BtcMarketExecutionClock()
            history = HistoricalMarketDataService(storage)
            return BtcMarketRefreshPipeline(
                refresh_planner=BtcMarketRefreshPlanner(storage),
                market_pipeline=CoinbaseHistoricalPipeline(
                    storage,
                    CoinbaseExchangeClient(
                        self._transport_factory(),
                        clock=execution_clock,
                    ),
                    configuration=configuration,
                    clock=execution_clock,
                ),
                statistics_pipeline=MarketStatisticsPipeline(
                    storage,
                    history,
                    MarketStatisticsEngine(),
                    clock=execution_clock,
                ),
                diagnostic_pipeline=MarketDiagnosticPipeline(
                    storage,
                    MarketDiagnosticMetricSelector(storage),
                    MarketDiagnosticEngine(),
                    clock=execution_clock,
                ),
                clock=execution_clock,
            ).run(request)

    def refresh_crypto_spot_daily(
        self,
        request: CryptoSpotDailyRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> CryptoSpotDailyRefreshSummary:
        """Refresh one catalog-scoped Coinbase daily source without BTC fallback."""
        configuration = resolve_coinbase_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode("read_write")
        ) as storage:
            execution_clock = BtcMarketExecutionClock()
            history = HistoricalMarketDataService(storage)
            return CryptoSpotDailyRefreshPipeline(
                asset_id=configuration.asset_id,
                source_id=configuration.source_id,
                refresh_planner=CryptoSpotDailyRefreshPlanner(
                    storage,
                    asset_id=configuration.asset_id,
                    source_id=configuration.source_id,
                ),
                market_pipeline=CoinbaseHistoricalPipeline(
                    storage,
                    CoinbaseExchangeClient(self._transport_factory(), clock=execution_clock),
                    configuration=configuration,
                    clock=execution_clock,
                ),
                statistics_pipeline=MarketStatisticsPipeline(
                    storage,
                    history,
                    MarketStatisticsEngine(),
                    clock=execution_clock,
                ),
                diagnostic_pipeline=MarketDiagnosticPipeline(
                    storage,
                    MarketDiagnosticMetricSelector(storage),
                    MarketDiagnosticEngine(),
                    clock=execution_clock,
                ),
                clock=execution_clock,
            ).run(request)

    def list_crypto_derivatives_assets(self) -> tuple[str, ...]:
        """Return assets exposing the complete Deribit v1 capability set."""
        capabilities = (
            "derivatives.funding.hourly",
            "derivatives.perpetual.snapshot",
            "derivatives.volatility_index.daily",
        )
        eligible_ids = {
            asset.asset_id
            for asset in self._runtime.catalog.list_assets(capability=capabilities[0])
        }
        for capability in capabilities[1:]:
            eligible_ids.intersection_update(
                asset.asset_id for asset in self._runtime.catalog.list_assets(capability=capability)
            )
        configured = tuple(
            resolve_deribit_configuration(
                self._runtime.provider_resolver,
                asset_id=asset_id,
            )
            for asset_id in sorted(eligible_ids)
        )
        return tuple(sorted(item.asset_id for item in configured))

    def refresh_crypto_derivatives(
        self,
        request: CryptoDerivativesRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> CryptoDerivativesRefreshSummary:
        """Refresh one catalog-scoped Deribit derivatives evidence family."""
        configuration = resolve_deribit_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        read_write = WorkspaceAccessMode.READ_WRITE
        with self._runtime.open_storage(
            location,
            access_mode=read_write,
        ) as storage:
            execution_clock = BtcMarketExecutionClock()
            return CryptoDerivativesRefreshService(
                storage,
                DeribitClient(
                    self._transport_factory(),
                    clock=execution_clock,
                ),
                configuration=configuration,
                clock=execution_clock,
            ).run(request)

    def query_crypto_derivatives(
        self,
        request: CryptoDerivativesQueryRequest,
        *,
        location: StorageLocationRequest,
    ) -> CryptoDerivativesQueryResult:
        """Replay one derivatives information set without provider or storage writes."""
        configuration = resolve_deribit_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        family_configurations = tuple(
            resolve_deribit_configuration(
                self._runtime.provider_resolver,
                asset_id=asset_id,
            )
            for asset_id in self.list_crypto_derivatives_assets()
        )
        diagnostic_source_ids = tuple(
            sorted(
                source_id
                for item in family_configurations
                for source_id in (
                    item.funding_source_id,
                    item.dvol_source_id,
                    item.summary_source_id,
                )
            )
        )
        start, end = public_date_bounds(request.start_date, request.end_date)
        read_only = WorkspaceAccessMode.READ_ONLY
        with self._runtime.open_storage(
            location,
            access_mode=read_only,
        ) as storage:
            return CryptoDerivativesService(
                storage,
                CryptoDerivativesMetricEngine(),
            ).query(
                asset_id=configuration.asset_id,
                funding_source_id=configuration.funding_source_id,
                dvol_source_id=configuration.dvol_source_id,
                summary_source_id=configuration.summary_source_id,
                diagnostic_source_ids=diagnostic_source_ids,
                start=start,
                end=end,
                known_at=request.known_at,
            )

    def refresh_listed_market(
        self,
        request: ListedMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
        alpaca_credentials: AlpacaCredentials,
    ) -> ListedMarketRefreshSummary:
        """Incrementally update one catalog-backed Alpaca market asset."""
        configuration = resolve_alpaca_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        asset = self._runtime.catalog.get(request.asset_id)
        capabilities = analysis_capabilities_for(asset)
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            history = HistoricalMarketDataService(storage)
            valuation_service: CorporateValuationService | None = None
            valuation_pipeline: CorporateValuationPersistencePipeline | None = None
            if (
                capabilities.family is AssetAnalysisFamily.LISTED_COMPANY
                and capabilities.fundamental_data_configured
            ):
                fundamental = resolve_sec_configuration(
                    self._runtime.provider_resolver,
                    asset_id=request.asset_id,
                )
                valuation_service = CorporateValuationService(
                    storage,
                    capabilities=capabilities,
                    market_source_id=configuration.source_id,
                    fundamental_source_id=fundamental.companyfacts_source_id,
                    price_currency=asset.quote_currency,
                    security_unit_factor=asset.security_unit_factor,
                    security_unit_basis=asset.security_unit_basis,
                    security_unit_basis_version=asset.security_unit_basis_version,
                    security_unit_market_adjustment=asset.security_unit_market_adjustment,
                )
                valuation_pipeline = CorporateValuationPersistencePipeline(storage)
            return ListedMarketRefreshPipeline(
                configuration=configuration,
                refresh_planner=AaplMarketRefreshPlanner(
                    storage,
                    configuration=configuration,
                ),
                market_pipeline=AlpacaHistoricalPipeline(
                    storage,
                    AlpacaStockClient(self._transport_factory(), alpaca_credentials),
                    configuration=configuration,
                ),
                statistics_pipeline=MarketStatisticsPipeline(
                    storage,
                    history,
                    MarketStatisticsEngine(),
                ),
                diagnostic_pipeline=MarketDiagnosticPipeline(
                    storage,
                    MarketDiagnosticMetricSelector(storage),
                    MarketDiagnosticEngine(),
                ),
                valuation_service=valuation_service,
                valuation_pipeline=valuation_pipeline,
            ).run(request)

    def refresh_sec_fundamentals(
        self,
        request: SecIssuerFundamentalRefreshRequest,
        *,
        location: StorageLocationRequest,
        sec_identity: SecEdgarIdentity,
    ) -> SecIssuerFundamentalRefreshSummary:
        """Update one catalog-backed SEC issuer without market-provider coupling."""
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        market_configuration = resolve_alpaca_configuration(
            self._runtime.provider_resolver,
            asset_id=request.asset_id,
        )
        asset = self._runtime.catalog.get(request.asset_id)
        capabilities = analysis_capabilities_for(asset)
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            transport = self._transport_factory()
            client = SecEdgarClient(
                transport,
                sec_identity,
                cik=configuration.cik,
                ticker=configuration.ticker,
            )
            normalizer = SecCompanyFactsNormalizer(configuration)
            point_in_time = SecIssuerFundamentalPointInTimeService(
                storage,
                configuration,
            )
            return SecIssuerFundamentalRefreshPipeline(
                storage,
                configuration=configuration,
                fetch_pipeline=SecIssuerFundamentalsPipeline(
                    storage,
                    client,
                    configuration=configuration,
                ),
                observation_pipeline=SecIssuerObservationPipeline(
                    storage,
                    normalizer,
                    configuration=configuration,
                ),
                metric_pipeline=SecIssuerFundamentalMetricPipeline(
                    storage,
                    point_in_time,
                    SecFundamentalMetricEngine(configuration),
                    configuration=configuration,
                ),
                diagnostic_pipeline=SecIssuerFundamentalDiagnosticPipeline(
                    storage,
                    SecFundamentalDiagnosticSelector(storage, configuration),
                    SecFundamentalDiagnosticEngine(configuration),
                    configuration=configuration,
                ),
                valuation_service=CorporateValuationService(
                    storage,
                    capabilities=capabilities,
                    market_source_id=market_configuration.source_id,
                    fundamental_source_id=configuration.companyfacts_source_id,
                    price_currency=asset.quote_currency,
                    security_unit_factor=asset.security_unit_factor,
                    security_unit_basis=asset.security_unit_basis,
                    security_unit_basis_version=asset.security_unit_basis_version,
                    security_unit_market_adjustment=asset.security_unit_market_adjustment,
                ),
                valuation_pipeline=CorporateValuationPersistencePipeline(storage),
            ).run(request)

    def query_aapl_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Preserve the historical Apple fundamental-trend facade."""
        return self.query_sec_fundamental_trend(
            request,
            asset_id=APPLE_ASSET_ID,
            location=location,
        )

    def query_sec_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Return bounded point-in-time trends for one catalog-backed SEC issuer."""
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver,
            asset_id=asset_id,
        )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return SecIssuerFundamentalTrendService(
                SecIssuerFundamentalPointInTimeService(storage, configuration),
                configuration,
            ).query(request)

    def query_aapl_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        """Preserve the historical Apple research facade."""
        return self.query_sec_fundamental_research(
            request,
            asset_id=APPLE_ASSET_ID,
            location=location,
        )

    def query_sec_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> FundamentalResearchResult:
        """Calculate point-in-time research for one catalog-backed SEC issuer."""
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver,
            asset_id=asset_id,
        )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return SecIssuerFundamentalResearchService(
                storage,
                configuration,
            ).query(request)

    def query_aapl_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Preserve the historical Apple research-history facade."""
        return self.query_sec_fundamental_research_history(
            request,
            asset_id=APPLE_ASSET_ID,
            location=location,
        )

    def query_sec_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> FundamentalResearchHistoryResult:
        """Calculate historical research for one catalog-backed SEC issuer."""
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver,
            asset_id=asset_id,
        )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return SecIssuerFundamentalResearchHistoryService(
                SecIssuerFundamentalResearchService(storage, configuration)
            ).query(request)

    def query_aapl_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        """Preserve the historical Apple unified-analysis facade."""
        return self.query_sec_fundamental_analysis(
            request,
            asset_id=APPLE_ASSET_ID,
            location=location,
        )

    def query_sec_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> FundamentalAnalysisResult:
        """Organize one catalog-backed SEC issuer into analytical sections."""
        configuration = resolve_sec_configuration(
            self._runtime.provider_resolver,
            asset_id=asset_id,
        )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return SecIssuerFundamentalAnalysisService(
                SecIssuerFundamentalResearchHistoryService(
                    SecIssuerFundamentalResearchService(storage, configuration)
                )
            ).query(request)

    def query_corporate_valuation(
        self,
        request: CorporateValuationRequest,
        *,
        location: StorageLocationRequest,
    ) -> CorporateValuationSnapshot:
        """Reconstruct a read-only latest-annual valuation without provider calls."""
        asset = self._runtime.catalog.get(request.asset_id)
        capabilities = analysis_capabilities_for(asset)
        market_source_id: str | None = None
        fundamental_source_id: str | None = None
        if (
            capabilities.family is AssetAnalysisFamily.LISTED_COMPANY
            and capabilities.market_data_configured
        ):
            try:
                market_source_id = resolve_alpaca_configuration(
                    self._runtime.provider_resolver,
                    asset_id=request.asset_id,
                ).source_id
            except ValueError:
                market_source_id = None
        if (
            capabilities.family is AssetAnalysisFamily.LISTED_COMPANY
            and capabilities.fundamental_data_configured
        ):
            try:
                fundamental_source_id = resolve_sec_configuration(
                    self._runtime.provider_resolver,
                    asset_id=request.asset_id,
                ).companyfacts_source_id
            except ValueError:
                fundamental_source_id = None
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return CorporateValuationService(
                storage,
                capabilities=capabilities,
                market_source_id=market_source_id,
                fundamental_source_id=fundamental_source_id,
                price_currency=asset.quote_currency,
                security_unit_factor=asset.security_unit_factor,
                security_unit_basis=asset.security_unit_basis,
                security_unit_basis_version=asset.security_unit_basis_version,
                security_unit_market_adjustment=asset.security_unit_market_adjustment,
            ).query(request)

    def persist_corporate_valuation(
        self,
        request: CorporateValuationRequest,
        *,
        location: StorageLocationRequest,
    ) -> ValuationPersistenceSummary:
        """Materialize evaluated valuation results with the existing workspace writer."""
        asset = self._runtime.catalog.get(request.asset_id)
        market = resolve_alpaca_configuration(
            self._runtime.provider_resolver, asset_id=request.asset_id
        )
        fundamental = resolve_sec_configuration(
            self._runtime.provider_resolver, asset_id=request.asset_id
        )
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            snapshot = CorporateValuationService(
                storage,
                capabilities=analysis_capabilities_for(asset),
                market_source_id=market.source_id,
                fundamental_source_id=fundamental.companyfacts_source_id,
                price_currency=asset.quote_currency,
                security_unit_factor=asset.security_unit_factor,
                security_unit_basis=asset.security_unit_basis,
                security_unit_basis_version=asset.security_unit_basis_version,
                security_unit_market_adjustment=asset.security_unit_market_adjustment,
            ).query(request)
            return CorporateValuationPersistencePipeline(storage).persist(snapshot)

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
        asset = self._runtime.catalog.get(APPLE_ASSET_ID)
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
            valuation_service=CorporateValuationService(
                storage,
                capabilities=analysis_capabilities_for(asset),
                market_source_id=alpaca_configuration.source_id,
                fundamental_source_id=sec_configuration.companyfacts_source_id,
                price_currency=asset.quote_currency,
                security_unit_factor=asset.security_unit_factor,
                security_unit_basis=asset.security_unit_basis,
                security_unit_basis_version=asset.security_unit_basis_version,
                security_unit_market_adjustment=asset.security_unit_market_adjustment,
            ),
            valuation_pipeline=CorporateValuationPersistencePipeline(storage),
            consolidated_service=AaplConsolidatedDiagnosticService(storage),
        )
