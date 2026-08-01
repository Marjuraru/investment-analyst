"""Loopback-only HTTP adapter for the local investment-analysis application."""

import gzip
import json
import threading
from collections.abc import Mapping
from datetime import UTC, date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import ValidationError

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestError,
    AnalyticalBacktestRequest,
    AnalyticalBacktestService,
)
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleConfigurationUpdate,
    AnalyticalRuleRegistryConflictError,
    AnalyticalRuleRegistryStore,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.analytics.aapl_daily_report_models import AaplDailyDiagnosticReport
from investment_analyst.analytics.aapl_daily_report_service import AaplDailyReportError
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    ConsolidatedDiagnosticQueryError,
)
from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendRequest,
)
from investment_analyst.analytics.fundamental_trend_service import (
    AaplFundamentalTrendQueryError,
)
from investment_analyst.analytics.fundamentals.analysis_models import (
    AaplFundamentalAnalysisResult,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_history_service import (
    FundamentalResearchHistoryError,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
)
from investment_analyst.analytics.fundamentals.research_service import (
    FundamentalResearchError,
)
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartInterval,
    AaplMarketChartPeriod,
    AaplMarketChartRequest,
    BtcMarketChart,
    BtcMarketChartRequest,
    ListedMarketChart,
)
from investment_analyst.analytics.market.chart_service import (
    AaplMarketChartQueryError,
    BtcMarketChartQueryError,
)
from investment_analyst.application.aapl_bootstrap import BootstrapIncompleteError
from investment_analyst.application.aapl_bootstrap_models import AaplWorkspaceBootstrapRequest
from investment_analyst.application.aapl_daily_runner import (
    AaplDailyRunExecutionError,
    AaplDailyRunner,
)
from investment_analyst.application.aapl_scheduler import AaplDailyScheduler
from investment_analyst.application.analysis_capabilities import (
    FundamentalAnalysisMode,
    MarketAnalysisMode,
)
from investment_analyst.application.btc_intraday import (
    BtcIntradayChartQueryError,
    BtcIntradayRefreshError,
)
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayChart,
    BtcIntradayChartRequest,
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.application.btc_refresh import (
    BtcMarketKnownAtTooEarlyError,
    BtcMarketRefreshError,
)
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
)
from investment_analyst.application.capability_runtime import (
    CapabilityDrivenRuntimePlan,
    build_capability_runtime_plan,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.listed_market_refresh import (
    ListedMarketKnownAtTooEarlyError,
    ListedMarketRefreshError,
)
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationQueue,
    ManualOperationRequest,
    ManualOperationResult,
)
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    MarketAssetUniverse,
)
from investment_analyst.application.multi_asset_scheduler import MultiAssetScheduler
from investment_analyst.application.operational_alerts import (
    OperationalAlertEventStatus,
    OperationalAlertStateStore,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunRequestSnapshot,
    AaplDailyRunState,
    AaplOperationalHealth,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
    AaplOperationalStateError,
)
from investment_analyst.application.overview_snapshot import OperationalOverviewSnapshot
from investment_analyst.application.peru_registry import (
    BvlRegistryRefreshRequest,
    BvlRegistryRefreshSummary,
)
from investment_analyst.application.runtime import ApplicationRuntimeError, StorageLocationRequest
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalKnownAtTooEarlyError,
    SecIssuerFundamentalRefreshError,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.macro.fred_alfred import FredApiKey
from investment_analyst.providers.macro.fred_catalog_refresh import (
    FredCatalogRefreshRequest,
    FredCatalogRefreshSummary,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError

_MAX_REQUEST_BYTES = 16_384
_MAX_READ_CACHE_ENTRIES = 8
_MIN_GZIP_BYTES = 1_024
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_CSP = (
    "default-src 'self'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; "
    "script-src 'self'; style-src 'self'"
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _safe_nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _manual_operation_result(
    operation_kind: ManualOperationKind,
    response: dict[str, object],
) -> ManualOperationResult:
    """Reduce one typed public response without inventing unavailable evidence."""
    count_source = response.get("counts")
    counts = count_source if isinstance(count_source, dict) else response
    created = _operation_count(counts, suffix="_created", aggregate_key="created_count")
    reused = _operation_count(counts, suffix="_reused", aggregate_key="reused_count")
    known_at: datetime | None = None
    for key in ("effective_known_at", "known_at", "checked_at", "retrieved_at"):
        value = response.get(key)
        if not isinstance(value, str):
            continue
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if candidate.tzinfo is not None and candidate.utcoffset() is not None:
            known_at = candidate.astimezone(UTC)
            break
    schema_version = response.get("schema_version")
    return ManualOperationResult(
        result_schema_version=(
            schema_version if isinstance(schema_version, str) and schema_version else None
        ),
        effective_known_at=known_at,
        created_count=created,
        reused_count=reused,
        coverage_complete=_operation_coverage(operation_kind, response),
        traceability_verified=_optional_bool(response.get("traceability_verified")),
    )


def _operation_count(
    values: Mapping[object, object],
    *,
    suffix: str,
    aggregate_key: str,
) -> int | None:
    candidates = tuple(
        value
        for key, value in values.items()
        if isinstance(key, str) and (key == aggregate_key or key.endswith(suffix))
    )
    if not candidates or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in candidates
    ):
        return None
    return sum(cast(int, value) for value in candidates)


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _operation_coverage(
    operation_kind: ManualOperationKind,
    response: Mapping[str, object],
) -> bool | None:
    for key in ("coverage_complete", "update_coverage_complete"):
        evidence = _optional_bool(response.get(key))
        if evidence is not None:
            return evidence
    if operation_kind is ManualOperationKind.COMPLETE_REFRESH:
        status = response.get("overall_status")
        return status == "complete" if isinstance(status, str) else None
    if operation_kind in {
        ManualOperationKind.MARKET_DAILY,
        ManualOperationKind.MARKET_INTRADAY,
    }:
        missing = response.get("missing_intervals")
        if isinstance(missing, (list, tuple)):
            return not missing
    return None


class _RunnerOperations(Protocol):
    def run(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplDailyRunState:
        """Run one operational refresh."""
        ...

    def inspect(self, *, workspace: Path | None) -> AaplOperationalHealth:
        """Inspect operational health."""
        ...


class _ApplicationOperations(Protocol):
    def list_market_assets(self) -> MarketAssetUniverse:
        """Return the catalog-backed assets supported by the local market UI."""
        ...

    def query_aapl_diagnostics(
        self,
        request: ConsolidatedDiagnosticRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplDailyDiagnosticReport:
        """Query one persisted report."""
        ...

    def query_aapl_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplMarketChart:
        """Query one bounded persisted market chart."""
        ...

    def query_btc_market_chart(
        self,
        request: BtcMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketChart:
        """Query one bounded persisted Coinbase chart."""
        ...

    def query_listed_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> ListedMarketChart:
        """Query one bounded catalog-backed Alpaca chart."""
        ...

    def query_btc_intraday_chart(
        self,
        request: BtcIntradayChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayChart:
        """Query one bounded persisted Coinbase intraday chart."""
        ...

    def refresh_btc_intraday(
        self,
        request: BtcIntradayRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayRefreshSummary:
        """Import one bounded Coinbase one-minute window."""
        ...

    def refresh_btc_market(
        self,
        request: BtcMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketRefreshSummary:
        """Update Coinbase BTC-USD and persist independent market analytics."""
        ...

    def refresh_listed_market(
        self,
        request: ListedMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
        alpaca_credentials: AlpacaCredentials,
    ) -> ListedMarketRefreshSummary:
        """Update one Alpaca listed asset and persist independent analytics."""
        ...

    def query_aapl_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Query one bounded persisted SEC fundamental trend."""
        ...

    def query_sec_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Query one bounded catalog-backed SEC fundamental trend."""
        ...

    def query_aapl_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        """Calculate bounded point-in-time fundamental research metrics."""
        ...

    def query_sec_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        """Calculate bounded point-in-time research for one SEC issuer."""
        ...

    def query_aapl_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate bounded historical fundamental research statistics."""
        ...

    def query_sec_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate bounded historical research for one SEC issuer."""
        ...

    def query_aapl_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        """Organize exact research evidence into analytical sections."""
        ...

    def query_sec_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        """Organize one SEC issuer's evidence into analytical sections."""
        ...

    def refresh_sec_fundamentals(
        self,
        request: SecIssuerFundamentalRefreshRequest,
        *,
        location: StorageLocationRequest,
        sec_identity: SecEdgarIdentity,
    ) -> SecIssuerFundamentalRefreshSummary:
        """Refresh one catalog-backed SEC issuer independently from market."""
        ...

    def refresh_fred_catalog_series(
        self,
        request: FredCatalogRefreshRequest,
        *,
        location: StorageLocationRequest,
        api_key: FredApiKey,
    ) -> FredCatalogRefreshSummary:
        """Refresh one configured FRED series through bounded vintage discovery."""
        ...

    def refresh_bvl_registry(
        self,
        request: BvlRegistryRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BvlRegistryRefreshSummary:
        """Refresh the configured official SMV registry."""
        ...


class _WebOperations(Protocol):
    def market_assets(self) -> dict[str, object]:
        """Return the catalog-backed market watchlist."""
        ...

    def runtime_capabilities(self) -> dict[str, object]:
        """Return the catalog-derived provider/domain/frequency dispatch inventory."""
        ...

    def overview(self) -> dict[str, object]:
        """Return operational and scheduler state."""
        ...

    def compact_overview(self) -> dict[str, object]:
        """Return the bounded nonblocking operational snapshot."""
        ...

    def enqueue_operation(self, payload: dict[str, object]) -> dict[str, object]:
        """Persist one typed request and return immediately."""
        ...

    def operation_status(self, operation_id: UUID) -> dict[str, object] | None:
        """Return one durable queued-operation status."""
        ...

    def alerts(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return the bounded persistent local alert inbox."""
        ...

    def alert_transition(self, payload: dict[str, object]) -> dict[str, object]:
        """Apply one audited local alert-inbox transition."""
        ...

    def candidates(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return the bounded persistent analytical candidate inbox."""
        ...

    def candidate_transition(self, payload: dict[str, object]) -> dict[str, object]:
        """Apply one audited analytical-candidate transition."""
        ...

    def screening_rules(self) -> dict[str, object]:
        """Return the current versioned analytical rule configuration."""
        ...

    def screening_rule_update(self, payload: dict[str, object]) -> dict[str, object]:
        """Apply one optimistically locked analytical rule update."""
        ...

    def screening_backtest(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Replay one configured rule over persisted point-in-time evidence."""
        ...

    def report(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one point-in-time report."""
        ...

    def market_chart(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one bounded point-in-time market chart."""
        ...

    def market_intraday(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one bounded point-in-time intraday chart."""
        ...

    def fundamental_trend(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one bounded point-in-time SEC fundamental trend."""
        ...

    def fundamental_research(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Return bounded point-in-time fundamental research metrics."""
        ...

    def fundamental_research_history(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Return bounded historical fundamental research statistics."""
        ...

    def fundamental_analysis(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Return one unified fundamental analysis."""
        ...

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one manual refresh."""
        ...

    def market_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one manual market-only refresh."""
        ...

    def market_intraday_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one manual intraday market refresh."""
        ...

    def fundamental_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one manual SEC issuer refresh."""
        ...


class AaplLocalController:
    """Coordinate one writer while serving reads from independent locks and snapshots."""

    def __init__(
        self,
        runner: _RunnerOperations,
        application: _ApplicationOperations,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
        fred_api_key: FredApiKey | None = None,
    ) -> None:
        self._runner = runner
        self._application = application
        self._workspace = workspace
        self._alpaca_credentials = alpaca_credentials
        self._sec_identity = sec_identity
        self._fred_api_key = fred_api_key
        self._writer_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._market_chart_cache: dict[AaplMarketChartRequest, AaplMarketChart] = {}
        self._btc_market_chart_cache: dict[BtcMarketChartRequest, BtcMarketChart] = {}
        self._listed_market_chart_cache: dict[
            tuple[str, AaplMarketChartRequest], ListedMarketChart
        ] = {}
        self._btc_intraday_chart_cache: dict[BtcIntradayChartRequest, BtcIntradayChart] = {}
        self._fundamental_trend_cache: dict[
            tuple[str, AaplFundamentalTrendRequest], AaplFundamentalTrend
        ] = {}
        self._fundamental_research_cache: dict[
            tuple[str, AaplFundamentalResearchRequest], AaplFundamentalResearchResult
        ] = {}
        self._fundamental_research_history_cache: dict[
            tuple[str, AaplFundamentalResearchRequest], AaplFundamentalResearchHistoryResult
        ] = {}
        self._fundamental_analysis_cache: dict[
            tuple[str, AaplFundamentalResearchRequest], AaplFundamentalAnalysisResult
        ] = {}
        self._market_assets = self._application.list_market_assets()
        self._runtime_capabilities = build_capability_runtime_plan(self._market_assets)
        self._health_snapshot = self._runner.inspect(workspace=self._workspace)

    @classmethod
    def create_default(
        cls,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
        fred_api_key: FredApiKey | None = None,
    ) -> "AaplLocalController":
        """Compose one shared runtime for the facade and operational runner."""
        from investment_analyst.application.runtime import ApplicationRuntime

        runtime = ApplicationRuntime.create_default()
        application = InvestmentAnalystApplication(runtime)
        return cls(
            AaplDailyRunner(application, runtime.workspace_service),
            application,
            workspace=workspace,
            alpaca_credentials=alpaca_credentials,
            sec_identity=sec_identity,
            fred_api_key=fred_api_key,
        )

    def health(self) -> AaplOperationalHealth:
        """Return a complete immutable snapshot without waiting for a provider."""
        with self._state_lock:
            return self._health_snapshot

    def market_assets(self) -> MarketAssetUniverse:
        """Return the immutable market universe resolved at process startup."""
        return self._market_assets

    def runtime_capabilities(self) -> CapabilityDrivenRuntimePlan:
        """Return immutable capability routes resolved at process startup."""
        return self._runtime_capabilities

    def run_payload(self, payload: dict[str, object]) -> AaplDailyRunState:
        """Validate the stable request snapshot and execute it once."""
        snapshot = AaplDailyRunRequestSnapshot.model_validate(payload)
        return self.run_request(snapshot.to_request())

    def run_request(self, request: AaplWorkspaceBootstrapRequest) -> AaplDailyRunState:
        """Execute a typed manual or scheduled request through one shared mutex."""
        with self._writer_lock:
            try:
                return self._runner.run(
                    request,
                    workspace=self._workspace,
                    alpaca_credentials=self._alpaca_credentials,
                    sec_identity=self._sec_identity,
                )
            finally:
                self._clear_read_caches()
                self._refresh_health_snapshot()

    def report_request(
        self,
        request: ConsolidatedDiagnosticRequest,
    ) -> AaplDailyDiagnosticReport:
        """Query persisted evidence without providers or writes."""
        return self._application.query_aapl_diagnostics(
            request,
            location=StorageLocationRequest(workspace=self._workspace),
        )

    def market_chart_request(self, request: AaplMarketChartRequest) -> AaplMarketChart:
        """Query persisted market bars and indicators without providers or writes."""
        with self._cache_lock:
            cached = self._market_chart_cache.get(request)
            if cached is not None:
                return cached
        chart = self._application.query_aapl_market_chart(
            request,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._market_chart_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._market_chart_cache.pop(next(iter(self._market_chart_cache)))
            self._market_chart_cache[request] = chart
        return chart

    def btc_market_chart_request(self, request: BtcMarketChartRequest) -> BtcMarketChart:
        """Query persisted Coinbase bars and indicators without providers or writes."""
        with self._cache_lock:
            cached = self._btc_market_chart_cache.get(request)
            if cached is not None:
                return cached
        chart = self._application.query_btc_market_chart(
            request,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._btc_market_chart_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._btc_market_chart_cache.pop(next(iter(self._btc_market_chart_cache)))
            self._btc_market_chart_cache[request] = chart
        return chart

    def listed_market_chart_request(
        self,
        asset_id: str,
        request: AaplMarketChartRequest,
    ) -> ListedMarketChart:
        """Query one cached catalog-backed Alpaca chart without writes."""
        cache_key = (asset_id, request)
        with self._cache_lock:
            cached = self._listed_market_chart_cache.get(cache_key)
            if cached is not None:
                return cached
        chart = self._application.query_listed_market_chart(
            request,
            asset_id=asset_id,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._listed_market_chart_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._listed_market_chart_cache.pop(next(iter(self._listed_market_chart_cache)))
            self._listed_market_chart_cache[cache_key] = chart
        return chart

    def btc_intraday_chart_request(
        self,
        request: BtcIntradayChartRequest,
    ) -> BtcIntradayChart:
        """Query cached, persisted Coinbase one-minute evidence without writes."""
        with self._cache_lock:
            cached = self._btc_intraday_chart_cache.get(request)
            if cached is not None:
                return cached
        chart = self._application.query_btc_intraday_chart(
            request,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._btc_intraday_chart_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._btc_intraday_chart_cache.pop(next(iter(self._btc_intraday_chart_cache)))
            self._btc_intraday_chart_cache[request] = chart
        return chart

    def btc_market_refresh_request(
        self,
        request: BtcMarketRefreshRequest,
    ) -> BtcMarketRefreshSummary:
        """Execute one Coinbase-only refresh through the shared writer mutex."""
        with self._writer_lock:
            try:
                return self._application.refresh_btc_market(
                    request,
                    location=StorageLocationRequest(workspace=self._workspace),
                )
            finally:
                with self._cache_lock:
                    self._btc_market_chart_cache.clear()
                self._refresh_health_snapshot()

    def listed_market_refresh_request(
        self,
        request: ListedMarketRefreshRequest,
    ) -> ListedMarketRefreshSummary:
        """Execute one Alpaca market-only refresh through the writer mutex."""
        with self._writer_lock:
            try:
                return self._application.refresh_listed_market(
                    request,
                    location=StorageLocationRequest(workspace=self._workspace),
                    alpaca_credentials=self._alpaca_credentials,
                )
            finally:
                with self._cache_lock:
                    self._listed_market_chart_cache.clear()
                self._refresh_health_snapshot()

    def btc_intraday_refresh_request(
        self,
        request: BtcIntradayRefreshRequest,
    ) -> BtcIntradayRefreshSummary:
        """Execute one bounded Coinbase minute refresh through the writer mutex."""
        with self._writer_lock:
            try:
                return self._application.refresh_btc_intraday(
                    request,
                    location=StorageLocationRequest(workspace=self._workspace),
                )
            finally:
                with self._cache_lock:
                    self._btc_intraday_chart_cache.clear()
                self._refresh_health_snapshot()

    def sec_fundamental_refresh_request(
        self,
        request: SecIssuerFundamentalRefreshRequest,
    ) -> SecIssuerFundamentalRefreshSummary:
        """Execute one SEC-only issuer refresh through the shared writer mutex."""
        with self._writer_lock:
            try:
                return self._application.refresh_sec_fundamentals(
                    request,
                    location=StorageLocationRequest(workspace=self._workspace),
                    sec_identity=self._sec_identity,
                )
            finally:
                with self._cache_lock:
                    self._fundamental_trend_cache.clear()
                    self._fundamental_research_cache.clear()
                    self._fundamental_research_history_cache.clear()
                    self._fundamental_analysis_cache.clear()
                self._refresh_health_snapshot()

    def fred_catalog_refresh_request(
        self,
        request: FredCatalogRefreshRequest,
    ) -> FredCatalogRefreshSummary:
        """Refresh one FRED series through the shared writer mutex."""
        if self._fred_api_key is None:
            raise ValueError("FRED_API_KEY is required for automatic macro refresh")
        with self._writer_lock:
            return self._application.refresh_fred_catalog_series(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
                api_key=self._fred_api_key,
            )

    def bvl_registry_refresh_request(
        self,
        request: BvlRegistryRefreshRequest,
    ) -> BvlRegistryRefreshSummary:
        """Refresh official SMV registry evidence through the shared writer mutex."""
        with self._writer_lock:
            return self._application.refresh_bvl_registry(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )

    def fundamental_trend_request(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        asset_id: str = APPLE_ASSET_ID,
    ) -> AaplFundamentalTrend:
        """Query persisted SEC facts without providers, recomputation, or writes."""
        cache_key = (asset_id, request)
        with self._cache_lock:
            cached = self._fundamental_trend_cache.get(cache_key)
            if cached is not None:
                return cached
        trend = self._application.query_sec_fundamental_trend(
            request,
            asset_id=asset_id,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._fundamental_trend_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_trend_cache.pop(next(iter(self._fundamental_trend_cache)))
            self._fundamental_trend_cache[cache_key] = trend
        return trend

    def fundamental_research_request(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str = APPLE_ASSET_ID,
    ) -> AaplFundamentalResearchResult:
        """Calculate cached SEC research metrics without providers or writes."""
        cache_key = (asset_id, request)
        with self._cache_lock:
            cached = self._fundamental_research_cache.get(cache_key)
            if cached is not None:
                return cached
        research = self._application.query_sec_fundamental_research(
            request,
            asset_id=asset_id,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._fundamental_research_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_research_cache.pop(next(iter(self._fundamental_research_cache)))
            self._fundamental_research_cache[cache_key] = research
        return research

    def fundamental_research_history_request(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str = APPLE_ASSET_ID,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate cached historical research statistics without writes."""
        cache_key = (asset_id, request)
        with self._cache_lock:
            cached = self._fundamental_research_history_cache.get(cache_key)
            if cached is not None:
                return cached
        history = self._application.query_sec_fundamental_research_history(
            request,
            asset_id=asset_id,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._fundamental_research_history_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_research_history_cache.pop(
                    next(iter(self._fundamental_research_history_cache))
                )
            self._fundamental_research_history_cache[cache_key] = history
        return history

    def fundamental_analysis_request(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str = APPLE_ASSET_ID,
    ) -> AaplFundamentalAnalysisResult:
        """Return cached analytical sections without providers or writes."""
        cache_key = (asset_id, request)
        with self._cache_lock:
            cached = self._fundamental_analysis_cache.get(cache_key)
            if cached is not None:
                return cached
        analysis = self._application.query_sec_fundamental_analysis(
            request,
            asset_id=asset_id,
            location=StorageLocationRequest(workspace=self._workspace),
        )
        with self._cache_lock:
            if len(self._fundamental_analysis_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_analysis_cache.pop(next(iter(self._fundamental_analysis_cache)))
            self._fundamental_analysis_cache[cache_key] = analysis
        return analysis

    def _clear_read_caches(self) -> None:
        with self._cache_lock:
            self._market_chart_cache.clear()
            self._btc_market_chart_cache.clear()
            self._listed_market_chart_cache.clear()
            self._btc_intraday_chart_cache.clear()
            self._fundamental_trend_cache.clear()
            self._fundamental_research_cache.clear()
            self._fundamental_research_history_cache.clear()
            self._fundamental_analysis_cache.clear()

    def _refresh_health_snapshot(self) -> None:
        snapshot = self._runner.inspect(workspace=self._workspace)
        with self._state_lock:
            self._health_snapshot = snapshot


class AaplLocalWebApplication:
    """JSON-safe local UI operations over the controller and optional scheduler."""

    def __init__(
        self,
        controller: AaplLocalController,
        scheduler: AaplDailyScheduler | MultiAssetScheduler | None,
        alert_store: OperationalAlertStateStore | None = None,
        analytical_store: AnalyticalScreeningStateStore | None = None,
        analytical_rule_store: AnalyticalRuleRegistryStore | None = None,
        analytical_backtest: AnalyticalBacktestService | None = None,
        manual_operations: ManualOperationQueue | None = None,
    ) -> None:
        self._controller = controller
        self._scheduler = scheduler
        self._alert_store = alert_store
        self._analytical_store = analytical_store
        self._analytical_rule_store = analytical_rule_store
        self._analytical_backtest = analytical_backtest
        self._manual_operations = manual_operations

    def set_manual_operations(self, operations: ManualOperationQueue) -> None:
        """Attach the durable queue after its dispatcher can reference this adapter."""
        if self._manual_operations is not None:
            raise ValueError("manual operation queue is already configured")
        self._manual_operations = operations

    def overview(self) -> dict[str, object]:
        """Return state only; never initialize, fetch, calculate, or persist."""
        scheduler: dict[str, object] = {"enabled": False}
        if self._scheduler is not None:
            scheduler = self._scheduler.status().to_json_dict()
        alerts: dict[str, object] = {"enabled": False}
        if self._alert_store is not None:
            alerts = self._alert_store.status().to_json_dict()
        candidates: dict[str, object] = {"enabled": False}
        if self._analytical_store is not None:
            candidates = self._analytical_store.status().to_json_dict()
        return {
            "operational": self._controller.health().to_json_dict(),
            "scheduler": scheduler,
            "alerts": alerts,
            "candidates": candidates,
        }

    def compact_overview(self) -> dict[str, object]:
        """Return a bounded snapshot without retained histories or writer acquisition."""
        health = self._controller.health().to_json_dict()
        workspace = cast(dict[str, object], health["workspace"])
        latest_run = cast(dict[str, object] | None, health.get("latest_run"))
        scheduler_enabled = self._scheduler is not None
        scheduled_job_count = 0
        scheduled_running_count = 0
        scheduled_failed_count = 0
        if self._scheduler is not None:
            status = self._scheduler.status().to_json_dict()
            jobs = status.get("jobs", ())
            scheduled_job_count = len(jobs) if isinstance(jobs, (list, tuple)) else 0
            scheduled_running_count = _safe_nonnegative_int(status.get("running_count"))
            scheduled_failed_count = _safe_nonnegative_int(status.get("failed_count"))
        queue_snapshot = self._manual_operations.snapshot() if self._manual_operations else None
        return OperationalOverviewSnapshot.now(
            operational_status=str(health["status"]),
            workspace_status=str(workspace["status"]),
            workspace_id=workspace.get("workspace_id"),
            latest_run_id=latest_run.get("run_id") if latest_run else None,
            latest_run_status=str(latest_run["status"]) if latest_run else None,
            scheduler_enabled=scheduler_enabled,
            scheduled_job_count=scheduled_job_count,
            scheduled_running_count=scheduled_running_count,
            scheduled_failed_count=scheduled_failed_count,
            queued_operation_count=queue_snapshot.queued_count if queue_snapshot else 0,
            running_operation_count=queue_snapshot.running_count if queue_snapshot else 0,
            failed_operation_count=queue_snapshot.failed_count if queue_snapshot else 0,
            latest_operation_id=queue_snapshot.latest_operation_id if queue_snapshot else None,
            latest_operation_status=queue_snapshot.latest_status if queue_snapshot else None,
        ).to_json_dict()

    def enqueue_operation(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and durably enqueue one existing operation without provider work."""
        if self._manual_operations is None:
            raise ValueError("manual operation queue is not configured")
        request = ManualOperationRequest.model_validate(payload)
        return self._manual_operations.enqueue(request).to_json_dict()

    def operation_status(self, operation_id: UUID) -> dict[str, object] | None:
        """Return one durable operation status without waiting for its provider."""
        if self._manual_operations is None:
            raise ValueError("manual operation queue is not configured")
        operation = self._manual_operations.get(operation_id)
        return operation.to_json_dict() if operation else None

    def execute_manual_operation(self, request: ManualOperationRequest) -> ManualOperationResult:
        """Dispatch a queued request through the compatible typed synchronous facade."""
        if request.operation_kind is ManualOperationKind.COMPLETE_REFRESH:
            response = self.run(cast(dict[str, object], request.payload))
        elif request.operation_kind is ManualOperationKind.MARKET_DAILY:
            response = self.market_refresh(cast(dict[str, object], request.payload))
        elif request.operation_kind is ManualOperationKind.MARKET_INTRADAY:
            response = self.market_intraday_refresh(cast(dict[str, object], request.payload))
        else:
            response = self.fundamental_refresh(cast(dict[str, object], request.payload))
        return _manual_operation_result(request.operation_kind, response)

    def alerts(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return local persisted alerts without providers or analytical writes."""
        if self._alert_store is None:
            return {
                "schema_version": "operational-alert-inbox-v1",
                "silent_mode": True,
                "total": 0,
                "events": [],
            }
        allowed = {"limit"}
        if set(parameters) - allowed:
            raise ValueError("alert inbox query contains unsupported parameters")
        limit_value = _one_parameter(parameters, "limit", required=False)
        limit = _integer_parameter(limit_value, name="limit", default=50)
        return self._alert_store.inbox(limit=limit).to_json_dict()

    def alert_transition(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and persist one explicit local-inbox state change."""
        if self._alert_store is None:
            raise ValueError("operational alert monitor is not configured")
        if set(payload) != {"alert_id", "status"}:
            raise ValueError("alert transition requires only alert_id and status")
        alert_id_value = payload["alert_id"]
        status_value = payload["status"]
        if not isinstance(alert_id_value, str) or not isinstance(status_value, str):
            raise ValueError("alert_id and status must be strings")
        try:
            alert_id = UUID(alert_id_value)
            status = OperationalAlertEventStatus(status_value)
        except ValueError as error:
            raise ValueError("alert transition contains an invalid identifier or status") from error
        event, changed = self._alert_store.transition(
            alert_id,
            status,
            recorded_at=datetime.now(UTC),
        )
        return {
            "schema_version": "operational-alert-transition-response-v1",
            "changed": changed,
            "event": event.to_json_dict(),
        }

    def candidates(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return persisted analytical candidates without recalculation or providers."""
        if self._analytical_store is None:
            return {
                "schema_version": "analytical-candidate-inbox-v1",
                "silent_mode": True,
                "total": 0,
                "items": [],
            }
        if set(parameters) - {"limit"}:
            raise ValueError("candidate inbox query contains unsupported parameters")
        limit_value = _one_parameter(parameters, "limit", required=False)
        limit = _integer_parameter(limit_value, name="limit", default=50)
        return self._analytical_store.inbox(limit=limit).to_json_dict()

    def candidate_transition(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and persist one explicit analytical-candidate transition."""
        if self._analytical_store is None:
            raise ValueError("analytical screening monitor is not configured")
        if set(payload) != {"candidate_id", "status"}:
            raise ValueError("candidate transition requires only candidate_id and status")
        candidate_id_value = payload["candidate_id"]
        status_value = payload["status"]
        if not isinstance(candidate_id_value, str) or not isinstance(status_value, str):
            raise ValueError("candidate_id and status must be strings")
        try:
            candidate_id = UUID(candidate_id_value)
            status = AnalyticalCandidateStatus(status_value)
        except ValueError as error:
            raise ValueError(
                "candidate transition contains an invalid identifier or status"
            ) from error
        event, changed = self._analytical_store.transition(
            candidate_id,
            status,
            recorded_at=datetime.now(UTC),
        )
        return {
            "schema_version": "analytical-candidate-transition-response-v1",
            "changed": changed,
            "event": event.to_json_dict(),
        }

    def screening_rules(self) -> dict[str, object]:
        """Return defaults and append-only local rule revisions without providers."""
        if self._analytical_rule_store is None:
            return {
                "schema_version": "analytical-rule-registry-snapshot-v1",
                "configurations": [],
                "total_revisions": 0,
            }
        return self._analytical_rule_store.snapshot().to_json_dict()

    def screening_rule_update(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and persist one complete, optimistically locked rule revision."""
        if self._analytical_rule_store is None:
            raise ValueError("analytical rule registry is not configured")
        request = AnalyticalRuleConfigurationUpdate.model_validate(payload)
        return self._analytical_rule_store.update(
            request,
            recorded_at=datetime.now(UTC),
        ).to_json_dict()

    def screening_backtest(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Run a bounded read-only point-in-time replay for one rule and asset."""
        if self._analytical_backtest is None:
            raise ValueError("analytical backtest is not configured")
        if set(parameters) - {"rule_id", "asset_id", "max_cuts"}:
            raise ValueError("screening backtest query contains unsupported parameters")
        request = AnalyticalBacktestRequest(
            rule_id=_one_parameter(parameters, "rule_id", required=True),
            asset_id=_one_parameter(parameters, "asset_id", required=True),
            max_cuts=_integer_parameter(
                _one_parameter(parameters, "max_cuts", required=False),
                name="max_cuts",
                default=200,
            ),
        )
        return self._analytical_backtest.run(request).to_json_dict()

    def market_assets(self) -> dict[str, object]:
        """Return one immutable catalog-driven watchlist."""
        return self._controller.market_assets().to_json_dict()

    def runtime_capabilities(self) -> dict[str, object]:
        """Return the strict dispatch plan without storage or provider access."""
        return self._controller.runtime_capabilities().to_json_dict()

    def report(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Validate query parameters and return the versioned report contract."""
        allowed = {
            "known_at",
            "fundamental_frequency",
            "market_as_of",
            "fundamental_as_of",
        }
        if set(parameters) - allowed:
            raise ValueError("report query contains unsupported parameters")
        known_at = _one_parameter(parameters, "known_at", required=True)
        frequency = _one_parameter(parameters, "fundamental_frequency", required=True)
        market_as_of = _one_parameter(parameters, "market_as_of", required=False)
        fundamental_as_of = _one_parameter(parameters, "fundamental_as_of", required=False)
        request = ConsolidatedDiagnosticRequest(
            known_at=_aware_datetime(known_at),
            fundamental_frequency=_frequency(frequency),
            market_as_of=_optional_date(market_as_of),
            fundamental_as_of=_optional_date(fundamental_as_of),
        )
        return self._controller.report_request(request).to_json_dict()

    def market_chart(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Validate query parameters and return the versioned market-chart contract."""
        allowed = {
            "asset_id",
            "known_at",
            "period",
            "interval",
            "short_sma_window",
            "long_sma_window",
            "third_sma_window",
        }
        if set(parameters) - allowed:
            raise ValueError("market chart query contains unsupported parameters")
        asset_id = _one_parameter(parameters, "asset_id", required=False) or "equity:us:aapl"
        known_at = _one_parameter(parameters, "known_at", required=True)
        period = _one_parameter(parameters, "period", required=False)
        interval = _one_parameter(parameters, "interval", required=False)
        short_sma_window = _one_parameter(parameters, "short_sma_window", required=False)
        long_sma_window = _one_parameter(parameters, "long_sma_window", required=False)
        third_sma_window = _one_parameter(parameters, "third_sma_window", required=False)
        request_parameters: dict[str, object] = {
            "known_at": _aware_datetime(known_at),
            "period": period or AaplMarketChartPeriod.SIX_MONTHS,
            "interval": interval or AaplMarketChartInterval.AUTOMATIC,
            "short_sma_window": _integer_parameter(
                short_sma_window,
                name="short_sma_window",
                default=5,
            ),
            "long_sma_window": _integer_parameter(
                long_sma_window,
                name="long_sma_window",
                default=20,
            ),
        }
        if third_sma_window is not None:
            request_parameters["third_sma_window"] = _integer_parameter(
                third_sma_window,
                name="third_sma_window",
                default=50,
            )
        descriptor = self._market_asset(asset_id)
        if descriptor.refresh_kind == "complete_analysis":
            request = AaplMarketChartRequest.model_validate(request_parameters)
            return self._controller.market_chart_request(request).to_json_dict()
        if (
            descriptor.analysis.market_mode is MarketAnalysisMode.CRYPTO_SPOT
            and descriptor.provider == "coinbase"
        ):
            request = BtcMarketChartRequest.model_validate(request_parameters)
            return self._controller.btc_market_chart_request(request).to_json_dict()
        if (
            descriptor.analysis.market_mode is MarketAnalysisMode.LISTED_SECURITY
            and descriptor.provider == "alpaca"
        ):
            request = AaplMarketChartRequest.model_validate(request_parameters)
            return self._controller.listed_market_chart_request(
                asset_id,
                request,
            ).to_json_dict()
        raise ValueError("market chart asset_id is not supported")

    def market_intraday(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Validate and return the fixed 24-hour BTC-USD intraday chart."""
        allowed = {"asset_id", "known_at", "interval"}
        if set(parameters) - allowed:
            raise ValueError("intraday market query contains unsupported parameters")
        asset_id = _one_parameter(parameters, "asset_id", required=False) or "crypto:btc-usd"
        descriptor = self._market_asset(asset_id)
        if (
            descriptor.analysis.market_mode is not MarketAnalysisMode.CRYPTO_SPOT
            or not descriptor.supports_intraday
            or descriptor.provider != "coinbase"
        ):
            raise ValueError("intraday market asset_id is not supported")
        known_at = _one_parameter(parameters, "known_at", required=True)
        interval = _one_parameter(parameters, "interval", required=True)
        request = BtcIntradayChartRequest(
            known_at=_aware_datetime(known_at),
            interval=interval,
        )
        return self._controller.btc_intraday_chart_request(request).to_json_dict()

    def fundamental_trend(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Validate query parameters and return the versioned SEC trend contract."""
        allowed = {"asset_id", "known_at", "frequency"}
        if set(parameters) - allowed:
            raise ValueError("fundamental trend query contains unsupported parameters")
        descriptor = self._fundamental_asset(parameters)
        known_at = _one_parameter(parameters, "known_at", required=True)
        frequency = _frequency(_one_parameter(parameters, "frequency", required=True))
        self._require_fundamental_frequency(descriptor, frequency)
        period_limit = 5 if frequency is DataFrequency.ANNUAL else 8
        request = AaplFundamentalTrendRequest(
            known_at=_aware_datetime(known_at),
            frequency=frequency,
            period_limit=period_limit,
        )
        return self._controller.fundamental_trend_request(
            request,
            asset_id=descriptor.asset_id,
        ).to_json_dict()

    def fundamental_research(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Validate query parameters and return exact derived SEC metrics."""
        descriptor = self._fundamental_asset(parameters)
        request = _fundamental_research_request(parameters)
        self._require_fundamental_frequency(descriptor, request.frequency)
        return self._controller.fundamental_research_request(
            request,
            asset_id=descriptor.asset_id,
        ).to_json_dict()

    def fundamental_research_history(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Validate query parameters and return historical SEC statistics."""
        descriptor = self._fundamental_asset(parameters)
        request = _fundamental_research_request(parameters)
        self._require_fundamental_frequency(descriptor, request.frequency)
        return self._controller.fundamental_research_history_request(
            request,
            asset_id=descriptor.asset_id,
        ).to_json_dict()

    def fundamental_analysis(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Validate query parameters and return unified analytical sections."""
        descriptor = self._fundamental_asset(parameters)
        request = _fundamental_research_request(parameters)
        self._require_fundamental_frequency(descriptor, request.frequency)
        return self._controller.fundamental_analysis_request(
            request,
            asset_id=descriptor.asset_id,
        ).to_json_dict()

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one explicit request and return bounded operational state."""
        return self._controller.run_payload(payload).to_json_dict()

    def market_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and execute one explicit market-only refresh."""
        asset_id = payload.get("asset_id")
        descriptor = self._market_asset(asset_id)
        if (
            descriptor.analysis.market_mode is MarketAnalysisMode.CRYPTO_SPOT
            and descriptor.provider == "coinbase"
        ):
            request = BtcMarketRefreshRequest.model_validate(payload)
            return self._controller.btc_market_refresh_request(request).to_json_dict()
        if (
            descriptor.analysis.market_mode is MarketAnalysisMode.LISTED_SECURITY
            and descriptor.provider == "alpaca"
            and descriptor.refresh_kind == "market_only"
        ):
            request = ListedMarketRefreshRequest.model_validate(payload)
            return self._controller.listed_market_refresh_request(request).to_json_dict()
        raise ValueError("market refresh asset_id is not supported")

    def market_intraday_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and execute one explicit bounded Coinbase minute refresh."""
        request = BtcIntradayRefreshRequest.model_validate(payload)
        return self._controller.btc_intraday_refresh_request(request).to_json_dict()

    def fundamental_refresh(self, payload: dict[str, object]) -> dict[str, object]:
        """Validate and execute one explicit SEC-only issuer refresh."""
        descriptor = self._fundamental_descriptor(payload.get("asset_id"))
        request = SecIssuerFundamentalRefreshRequest.model_validate(payload)
        if request.asset_id != descriptor.asset_id:
            raise ValueError("fundamental refresh asset_id is inconsistent")
        self._require_fundamental_frequency(descriptor, request.frequency)
        return self._controller.sec_fundamental_refresh_request(request).to_json_dict()

    def _market_asset(self, asset_id: object) -> MarketAssetDescriptor:
        """Resolve one visible asset without maintaining a second allowlist."""
        if not isinstance(asset_id, str):
            raise ValueError("market asset_id must be a string")
        candidates = tuple(
            item for item in self._controller.market_assets().assets if item.asset_id == asset_id
        )
        if len(candidates) != 1:
            raise ValueError("market asset_id is not supported")
        return candidates[0]

    def _fundamental_asset(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> MarketAssetDescriptor:
        """Reject fundamental queries without a configured corporate issuer."""
        asset_id = _one_parameter(parameters, "asset_id", required=False) or APPLE_ASSET_ID
        return self._fundamental_descriptor(asset_id)

    def _fundamental_descriptor(self, asset_id: object) -> MarketAssetDescriptor:
        """Resolve one visible corporate issuer with an enabled SEC presentation."""
        descriptor = self._market_asset(asset_id)
        if (
            not descriptor.has_fundamentals
            or descriptor.analysis.fundamental_mode is not FundamentalAnalysisMode.CORPORATE
        ):
            raise ValueError("fundamental analysis is not available for asset_id")
        return descriptor

    @staticmethod
    def _require_fundamental_frequency(
        descriptor: MarketAssetDescriptor,
        frequency: DataFrequency,
    ) -> None:
        """Reject unsupported issuer frequencies before reads, providers, or writes."""
        if frequency not in descriptor.fundamental_frequencies:
            supported = ", ".join(item.value for item in descriptor.fundamental_frequencies)
            raise ValueError(
                f"fundamental frequency is not available for asset_id; supported: {supported}"
            )


class AaplLocalHttpServer(ThreadingHTTPServer):
    """Threaded server restricted to a loopback address and fixed application routes."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        application: _WebOperations,
    ) -> None:
        host, _ = address
        if host not in _ALLOWED_HOSTS:
            raise ValueError("local interface must bind to a loopback host")
        self.application = application
        static_root = files("investment_analyst.frontend").joinpath("static")
        self.assets = {
            route: (static_root.joinpath(name).read_bytes(), content_type)
            for route, (name, content_type) in _ASSETS.items()
        }
        super().__init__(address, AaplLocalRequestHandler)


class AaplLocalRequestHandler(BaseHTTPRequestHandler):
    """Serve fixed assets and a bounded same-origin JSON API."""

    protocol_version = "HTTP/1.1"
    server_version = "InvestmentAnalystLocal/0.1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_post()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "method not allowed")

    def log_message(self, format: str, *args: object) -> None:
        """Retain standard access logging without ever logging request bodies."""
        super().log_message(format, *args)

    def _dispatch(self, *, head_only: bool) -> None:
        try:
            self._require_loopback_host()
            parsed = urlsplit(self.path)
            server = cast(AaplLocalHttpServer, self.server)
            if parsed.path in server.assets:
                body, content_type = server.assets[parsed.path]
                self._send_bytes(HTTPStatus.OK, body, content_type, head_only=head_only)
                return
            if head_only:
                raise _HttpError(
                    HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "method not allowed"
                )
            if parsed.path == "/api/overview":
                self._send_json(HTTPStatus.OK, server.application.overview())
                return
            if parsed.path == "/api/v1/overview":
                if parsed.query:
                    raise ValueError("compact overview does not accept parameters")
                self._send_json(HTTPStatus.OK, server.application.compact_overview())
                return
            operation_prefix = "/api/v1/manual-operations/"
            if parsed.path.startswith(operation_prefix):
                if parsed.query:
                    raise ValueError("manual operation status does not accept parameters")
                raw_id = parsed.path.removeprefix(operation_prefix)
                if not raw_id or "/" in raw_id:
                    raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
                try:
                    operation_id = UUID(raw_id)
                except ValueError as error:
                    raise ValueError("manual operation_id is invalid") from error
                operation = server.application.operation_status(operation_id)
                if operation is None:
                    raise _HttpError(
                        HTTPStatus.NOT_FOUND,
                        "not_found",
                        "manual operation was not found",
                    )
                self._send_json(HTTPStatus.OK, operation)
                return
            if parsed.path == "/api/alerts":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.alerts(parameters))
                return
            if parsed.path == "/api/candidates":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.candidates(parameters),
                )
                return
            if parsed.path == "/api/screening-rules":
                if parsed.query:
                    raise ValueError("screening rules query does not accept parameters")
                self._send_json(
                    HTTPStatus.OK,
                    server.application.screening_rules(),
                )
                return
            if parsed.path == "/api/screening-backtest":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=3)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.screening_backtest(parameters),
                )
                return
            if parsed.path == "/api/market-assets":
                self._send_json(HTTPStatus.OK, server.application.market_assets())
                return
            if parsed.path == "/api/v1/capabilities":
                if parsed.query:
                    raise ValueError("runtime capabilities do not accept parameters")
                self._send_json(HTTPStatus.OK, server.application.runtime_capabilities())
                return
            if parsed.path == "/api/report":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.report(parameters))
                return
            if parsed.path == "/api/market-chart":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=9)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.market_chart(parameters))
                return
            if parsed.path == "/api/market-intraday":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.market_intraday(parameters),
                )
                return
            if parsed.path == "/api/fundamental-trend":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.fundamental_trend(parameters))
                return
            if parsed.path == "/api/fundamental-research":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.fundamental_research(parameters),
                )
                return
            if parsed.path == "/api/fundamental-research-history":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.fundamental_research_history(parameters),
                )
                return
            if parsed.path == "/api/fundamental-analysis":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(
                    HTTPStatus.OK,
                    server.application.fundamental_analysis(parameters),
                )
                return
            raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
        except Exception as error:  # noqa: BLE001
            self._send_mapped_error(error)

    def _dispatch_post(self) -> None:
        try:
            self._require_loopback_host()
            parsed = urlsplit(self.path)
            if parsed.query or parsed.path not in {
                "/api/run",
                "/api/market-refresh",
                "/api/market-intraday-refresh",
                "/api/fundamental-refresh",
                "/api/alerts/transition",
                "/api/candidates/transition",
                "/api/screening-rules/update",
                "/api/v1/manual-operations",
            }:
                raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            payload = self._read_json_object()
            server = cast(AaplLocalHttpServer, self.server)
            if parsed.path == "/api/v1/manual-operations":
                response = server.application.enqueue_operation(payload)
                self._send_json(HTTPStatus.ACCEPTED, response)
                return
            if parsed.path == "/api/run":
                response = server.application.run(payload)
            elif parsed.path == "/api/alerts/transition":
                response = server.application.alert_transition(payload)
            elif parsed.path == "/api/candidates/transition":
                response = server.application.candidate_transition(payload)
            elif parsed.path == "/api/screening-rules/update":
                response = server.application.screening_rule_update(payload)
            elif parsed.path == "/api/market-refresh":
                response = server.application.market_refresh(payload)
            elif parsed.path == "/api/fundamental-refresh":
                response = server.application.fundamental_refresh(payload)
            else:
                response = server.application.market_intraday_refresh(payload)
            self._send_json(HTTPStatus.OK, response)
        except Exception as error:  # noqa: BLE001
            self._send_mapped_error(error)

    def _read_json_object(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
        if content_type != "application/json":
            raise _HttpError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _HttpError(
                HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required"
            )
        try:
            length = int(raw_length)
        except ValueError as error:
            raise _HttpError(
                HTTPStatus.BAD_REQUEST, "invalid_length", "Content-Length is invalid"
            ) from error
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise _HttpError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "JSON request body is empty or too large",
            )
        try:
            value = json.loads(
                self.rfile.read(length).decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise _HttpError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON"
            ) from error
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be a JSON object"
            )
        return cast(dict[str, object], value)

    def _require_loopback_host(self) -> None:
        host_header = self.headers.get("Host")
        if host_header is None:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid_host", "Host header is required")
        try:
            hostname = urlsplit(f"//{host_header}").hostname
        except ValueError as error:
            raise _HttpError(
                HTTPStatus.BAD_REQUEST, "invalid_host", "Host header is invalid"
            ) from error
        if hostname is None or hostname.casefold() not in _ALLOWED_HOSTS:
            raise _HttpError(HTTPStatus.FORBIDDEN, "invalid_host", "Host must identify loopback")

    def _send_mapped_error(self, error: Exception) -> None:
        if isinstance(error, _HttpError):
            self._send_error(error.status, error.code, error.message)
        elif isinstance(error, ValidationError):
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", "request validation failed")
        elif isinstance(error, AaplDailyRunAlreadyRunningError):
            self._send_error(HTTPStatus.CONFLICT, "run_active", str(error)[:500])
        elif isinstance(error, AnalyticalRuleRegistryConflictError):
            self._send_error(
                HTTPStatus.CONFLICT,
                "rule_conflict",
                str(error)[:500],
            )
        elif isinstance(error, AaplDailyRunExecutionError):
            status = (
                HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(error.cause, BootstrapIncompleteError)
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._send_error(status, error.failure.category, error.failure.message)
        elif isinstance(
            error,
            (
                BtcMarketKnownAtTooEarlyError,
                ListedMarketKnownAtTooEarlyError,
                SecIssuerFundamentalKnownAtTooEarlyError,
            ),
        ):
            self._send_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "known_at_too_early",
                str(error)[:500],
            )
        elif isinstance(error, (ListedMarketRefreshError, BtcMarketRefreshError)):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "market_refresh_failed",
                str(error)[:500],
            )
        elif isinstance(error, SecIssuerFundamentalRefreshError):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "fundamental_refresh_failed",
                str(error)[:500],
            )
        elif isinstance(error, BtcIntradayRefreshError):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "market_intraday_refresh_failed",
                str(error)[:500],
            )
        elif isinstance(
            error,
            (
                AaplDailyReportError,
                AaplFundamentalTrendQueryError,
                AaplMarketChartQueryError,
                BtcIntradayChartQueryError,
                BtcMarketChartQueryError,
                ConsolidatedDiagnosticQueryError,
                FundamentalResearchError,
                FundamentalResearchHistoryError,
            ),
        ):
            self._send_error(HTTPStatus.UNPROCESSABLE_ENTITY, "query_failed", str(error)[:500])
        elif isinstance(error, AnalyticalBacktestError):
            self._send_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "backtest_unavailable",
                str(error)[:500],
            )
        elif isinstance(error, ValueError):
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error)[:500])
        elif isinstance(
            error,
            (
                AaplOperationalStateError,
                ApplicationRuntimeError,
                StorageError,
                WorkspaceError,
            ),
        ):
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "operational_error", str(error)[:500])
        else:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unexpected_error",
                "the local interface failed unexpectedly",
            )

    def _send_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", head_only=False)

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        head_only: bool,
    ) -> None:
        accepts_gzip = {
            item.partition(";")[0].strip().casefold()
            for item in self.headers.get("Accept-Encoding", "").split(",")
        }
        compressible = content_type.startswith(("application/json", "text/"))
        encoded_body = body
        if compressible and len(body) >= _MIN_GZIP_BYTES and "gzip" in accepts_gzip:
            encoded_body = gzip.compress(body, compresslevel=5, mtime=0)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded_body)))
        if encoded_body is not body:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if not head_only:
            self.wfile.write(encoded_body)


class _HttpError(RuntimeError):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _one_parameter(
    parameters: Mapping[str, tuple[str, ...]],
    name: str,
    *,
    required: bool,
) -> str | None:
    values = parameters.get(name)
    if values is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if len(values) != 1 or not values[0].strip():
        raise ValueError(f"{name} must contain one non-empty value")
    return values[0]


def _aware_datetime(value: str | None) -> datetime:
    if value is None:
        raise ValueError("known_at is required")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("known_at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("known_at must include timezone information")
    return parsed.astimezone(UTC)


def _integer_parameter(value: str | None, *, name: str, default: int) -> int:
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _frequency(value: str | None) -> DataFrequency:
    if value is None:
        raise ValueError("fundamental_frequency is required")
    mapping = {"annual": DataFrequency.ANNUAL, "quarterly": DataFrequency.QUARTERLY}
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise ValueError("fundamental_frequency must be annual or quarterly") from error


def _fundamental_research_request(
    parameters: Mapping[str, tuple[str, ...]],
) -> AaplFundamentalResearchRequest:
    allowed = {"asset_id", "known_at", "frequency"}
    if set(parameters) - allowed:
        raise ValueError("fundamental research query contains unsupported parameters")
    known_at = _one_parameter(parameters, "known_at", required=True)
    frequency = _frequency(_one_parameter(parameters, "frequency", required=True))
    period_limit = 5 if frequency is DataFrequency.ANNUAL else 8
    return AaplFundamentalResearchRequest(
        known_at=_aware_datetime(known_at),
        frequency=frequency,
        limit=period_limit,
    )


def _optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("requested as-of dates must use YYYY-MM-DD") from error
