"""Socket-level tests for the loopback-only local web interface."""

import gzip
import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

import pytest

from investment_analyst.analytics.aapl_daily_report_models import AaplDailyDiagnosticReport
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrend,
    AaplFundamentalTrendRequest,
)
from investment_analyst.analytics.fundamentals.analysis_models import (
    AaplFundamentalAnalysisResult,
)
from investment_analyst.analytics.fundamentals.research_history_models import (
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
)
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartRequest,
    BtcMarketChart,
    BtcMarketChartRequest,
    ListedMarketChart,
)
from investment_analyst.analytics.market.intraday_models import IntradayInterval
from investment_analyst.application.aapl_bootstrap_models import AaplWorkspaceBootstrapRequest
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayChart,
    BtcIntradayChartRequest,
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshRequest,
    BtcMarketRefreshSummary,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.market_universe import MarketAssetUniverse
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobFailure,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertMonitor,
    OperationalAlertStateStore,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplOperationalHealth,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials


class _JsonResult:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_json_dict(self) -> dict[str, object]:
        return self._payload


class _FakeRunner:
    def __init__(self) -> None:
        self.requests: list[AaplWorkspaceBootstrapRequest] = []

    def run(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplDailyRunState:
        del workspace, alpaca_credentials, sec_identity
        self.requests.append(request)
        return cast(AaplDailyRunState, _JsonResult({"status": "succeeded", "run_id": "test"}))

    def inspect(self, *, workspace: Path | None) -> AaplOperationalHealth:
        del workspace
        payload: dict[str, object] = {
            "status": "ready",
            "workspace": {"status": "ready"},
            "latest_run": None,
            "issues": [],
        }
        return cast(AaplOperationalHealth, _JsonResult(payload))


class _FakeApplication:
    def __init__(self) -> None:
        self.requests: list[ConsolidatedDiagnosticRequest] = []
        self.locations: list[StorageLocationRequest] = []
        self.chart_requests: list[AaplMarketChartRequest] = []
        self.chart_locations: list[StorageLocationRequest] = []
        self.btc_chart_requests: list[BtcMarketChartRequest] = []
        self.btc_chart_locations: list[StorageLocationRequest] = []
        self.btc_intraday_chart_requests: list[BtcIntradayChartRequest] = []
        self.btc_intraday_chart_locations: list[StorageLocationRequest] = []
        self.btc_intraday_refresh_requests: list[BtcIntradayRefreshRequest] = []
        self.btc_intraday_refresh_locations: list[StorageLocationRequest] = []
        self.btc_refresh_requests: list[BtcMarketRefreshRequest] = []
        self.btc_refresh_locations: list[StorageLocationRequest] = []
        self.listed_chart_requests: list[tuple[str, AaplMarketChartRequest]] = []
        self.listed_chart_locations: list[StorageLocationRequest] = []
        self.listed_refresh_requests: list[ListedMarketRefreshRequest] = []
        self.listed_refresh_locations: list[StorageLocationRequest] = []
        self.trend_requests: list[AaplFundamentalTrendRequest] = []
        self.trend_asset_ids: list[str] = []
        self.trend_locations: list[StorageLocationRequest] = []
        self.research_requests: list[AaplFundamentalResearchRequest] = []
        self.research_asset_ids: list[str] = []
        self.research_locations: list[StorageLocationRequest] = []
        self.research_history_requests: list[AaplFundamentalResearchRequest] = []
        self.research_history_asset_ids: list[str] = []
        self.research_history_locations: list[StorageLocationRequest] = []
        self.analysis_requests: list[AaplFundamentalResearchRequest] = []
        self.analysis_asset_ids: list[str] = []
        self.analysis_locations: list[StorageLocationRequest] = []
        self.fundamental_refresh_requests: list[SecIssuerFundamentalRefreshRequest] = []
        self.fundamental_refresh_locations: list[StorageLocationRequest] = []
        self.fundamental_refresh_identities: list[SecEdgarIdentity] = []

    def list_market_assets(self) -> MarketAssetUniverse:
        return InvestmentAnalystApplication.create_default().list_market_assets()

    def query_aapl_diagnostics(
        self,
        request: ConsolidatedDiagnosticRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplDailyDiagnosticReport:
        self.requests.append(request)
        self.locations.append(location)
        return cast(
            AaplDailyDiagnosticReport,
            _JsonResult({"schema_version": "aapl-daily-diagnostic-report-v1"}),
        )

    def query_aapl_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplMarketChart:
        self.chart_requests.append(request)
        self.chart_locations.append(location)
        return cast(
            AaplMarketChart,
            _JsonResult(
                {
                    "schema_version": "aapl-market-chart-v5",
                    "period": request.period.value,
                    "interval": request.interval.value,
                    "points": [],
                }
            ),
        )

    def query_btc_market_chart(
        self,
        request: BtcMarketChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketChart:
        self.btc_chart_requests.append(request)
        self.btc_chart_locations.append(location)
        return cast(
            BtcMarketChart,
            _JsonResult(
                {
                    "schema_version": "btc-market-chart-v1",
                    "asset_id": "crypto:btc-usd",
                    "period": request.period.value,
                    "interval": request.interval.value,
                    "points": [],
                }
            ),
        )

    def query_listed_market_chart(
        self,
        request: AaplMarketChartRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> ListedMarketChart:
        self.listed_chart_requests.append((asset_id, request))
        self.listed_chart_locations.append(location)
        return cast(
            ListedMarketChart,
            _JsonResult(
                {
                    "schema_version": "listed-market-chart-v1",
                    "asset_id": asset_id,
                    "source_id": (
                        f"alpaca-market-data:iex:{asset_id.rsplit(':', 1)[-1]}:"
                        "daily-bars:adjustment-all"
                    ),
                    "period": request.period.value,
                    "interval": request.interval.value,
                    "points": [],
                }
            ),
        )

    def refresh_btc_market(
        self,
        request: BtcMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcMarketRefreshSummary:
        self.btc_refresh_requests.append(request)
        self.btc_refresh_locations.append(location)
        return cast(
            BtcMarketRefreshSummary,
            _JsonResult(
                {
                    "schema_version": "btc-market-refresh-v1",
                    "asset_id": "crypto:btc-usd",
                    "effective_known_at": "2026-07-16T15:47:00+00:00",
                    "refresh_plan": {"mode": "incremental"},
                    "candles_received": 1,
                    "metric_results_created": 7,
                    "traceability_verified": True,
                }
            ),
        )

    def refresh_listed_market(
        self,
        request: ListedMarketRefreshRequest,
        *,
        location: StorageLocationRequest,
        alpaca_credentials: AlpacaCredentials,
    ) -> ListedMarketRefreshSummary:
        del alpaca_credentials
        self.listed_refresh_requests.append(request)
        self.listed_refresh_locations.append(location)
        return cast(
            ListedMarketRefreshSummary,
            _JsonResult(
                {
                    "schema_version": "listed-market-refresh-v1",
                    "asset_id": request.asset_id,
                    "effective_known_at": "2026-07-16T15:47:00+00:00",
                    "refresh_plan": {"mode": "incremental"},
                    "bars_received": 1,
                    "metric_results_created": 7,
                    "traceability_verified": True,
                }
            ),
        )

    def query_btc_intraday_chart(
        self,
        request: BtcIntradayChartRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayChart:
        self.btc_intraday_chart_requests.append(request)
        self.btc_intraday_chart_locations.append(location)
        return cast(
            BtcIntradayChart,
            _JsonResult(
                {
                    "schema_version": "btc-intraday-chart-v1",
                    "asset_id": "crypto:btc-usd",
                    "source_id": "coinbase-exchange:btc-usd:minute-1-candles",
                    "known_at": request.known_at.isoformat(),
                    "start": request.query_start.isoformat(),
                    "end": request.query_end.isoformat(),
                    "lookback_hours": 24,
                    "interval": request.interval.value,
                    "bars": [],
                    "source_bar_count": 0,
                    "complete_interval_count": 0,
                    "incomplete_interval_count": 0,
                    "traceability_verified": True,
                }
            ),
        )

    def refresh_btc_intraday(
        self,
        request: BtcIntradayRefreshRequest,
        *,
        location: StorageLocationRequest,
    ) -> BtcIntradayRefreshSummary:
        self.btc_intraday_refresh_requests.append(request)
        self.btc_intraday_refresh_locations.append(location)
        return cast(
            BtcIntradayRefreshSummary,
            _JsonResult(
                {
                    "schema_version": "btc-intraday-refresh-v1",
                    "asset_id": "crypto:btc-usd",
                    "source_id": "coinbase-exchange:btc-usd:minute-1-candles",
                    "requested_start": "2026-07-15T15:46:00+00:00",
                    "requested_end": "2026-07-16T15:46:00+00:00",
                    "retrieved_at": "2026-07-16T15:47:00+00:00",
                    "request_count": 5,
                    "candles_received": 1_440,
                    "raw_records_created": 1_440,
                    "raw_records_reused": 0,
                    "observations_created": 7_200,
                    "observations_reused": 0,
                    "missing_intervals": [],
                    "earliest_candle": "2026-07-15T15:46:00+00:00",
                    "latest_candle": "2026-07-16T15:45:00+00:00",
                    "traceability_verified": True,
                }
            ),
        )

    def query_sec_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        self.trend_requests.append(request)
        self.trend_asset_ids.append(asset_id)
        self.trend_locations.append(location)
        return cast(
            AaplFundamentalTrend,
            _JsonResult(
                {
                    "schema_version": (
                        "aapl-fundamental-trend-v1"
                        if asset_id == "equity:us:aapl"
                        else "sec-fundamental-trend-v2"
                    ),
                    "asset_id": asset_id,
                    "frequency": request.frequency.value,
                    "period_limit": request.period_limit,
                    "periods": [],
                }
            ),
        )

    def query_sec_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        self.research_requests.append(request)
        self.research_asset_ids.append(asset_id)
        self.research_locations.append(location)
        return cast(
            AaplFundamentalResearchResult,
            _JsonResult(
                {
                    "schema_version": (
                        "aapl-fundamental-research-v2"
                        if asset_id == "equity:us:aapl"
                        else "sec-fundamental-research-v3"
                    ),
                    "asset_id": asset_id,
                    "frequency": request.frequency.value,
                    "period_limit": request.limit,
                    "periods": [],
                }
            ),
        )

    def query_sec_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        self.research_history_requests.append(request)
        self.research_history_asset_ids.append(asset_id)
        self.research_history_locations.append(location)
        return cast(
            AaplFundamentalResearchHistoryResult,
            _JsonResult(
                {
                    "schema_version": (
                        "aapl-fundamental-research-history-v2"
                        if asset_id == "equity:us:aapl"
                        else "sec-fundamental-research-history-v3"
                    ),
                    "asset_id": asset_id,
                    "request": {
                        "frequency": request.frequency.value,
                        "limit": request.limit,
                    },
                    "research": {
                        "schema_version": (
                            "aapl-fundamental-research-v2"
                            if asset_id == "equity:us:aapl"
                            else "sec-fundamental-research-v3"
                        )
                    },
                    "series": [],
                }
            ),
        )

    def query_sec_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        asset_id: str,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        self.analysis_requests.append(request)
        self.analysis_asset_ids.append(asset_id)
        self.analysis_locations.append(location)
        return cast(
            AaplFundamentalAnalysisResult,
            _JsonResult(
                {
                    "schema_version": (
                        "aapl-fundamental-analysis-v1"
                        if asset_id == "equity:us:aapl"
                        else "sec-fundamental-analysis-v2"
                    ),
                    "asset_id": asset_id,
                    "request": {
                        "frequency": request.frequency.value,
                        "limit": request.limit,
                    },
                    "history": {
                        "schema_version": (
                            "aapl-fundamental-research-history-v2"
                            if asset_id == "equity:us:aapl"
                            else "sec-fundamental-research-history-v3"
                        ),
                        "research": {
                            "schema_version": (
                                "aapl-fundamental-research-v2"
                                if asset_id == "equity:us:aapl"
                                else "sec-fundamental-research-v3"
                            )
                        },
                        "series": [],
                    },
                    "classification": {"status": "insufficient_evidence"},
                    "sections": [],
                }
            ),
        )

    def refresh_sec_fundamentals(
        self,
        request: SecIssuerFundamentalRefreshRequest,
        *,
        location: StorageLocationRequest,
        sec_identity: SecEdgarIdentity,
    ) -> SecIssuerFundamentalRefreshSummary:
        self.fundamental_refresh_requests.append(request)
        self.fundamental_refresh_locations.append(location)
        self.fundamental_refresh_identities.append(sec_identity)
        return cast(
            SecIssuerFundamentalRefreshSummary,
            _JsonResult(
                {
                    "schema_version": "sec-issuer-fundamental-refresh-v1",
                    "asset_id": request.asset_id,
                    "effective_known_at": (
                        request.requested_known_at.isoformat()
                        if request.requested_known_at is not None
                        else "2026-07-16T15:47:00+00:00"
                    ),
                    "metric_results_created": 3,
                    "diagnostics_created": 1,
                    "traceability_verified": True,
                }
            ),
        )


class _ExplodingApplication:
    def market_assets(self) -> dict[str, object]:
        raise RuntimeError("unexpected SECRET detail")

    def overview(self) -> dict[str, object]:
        raise RuntimeError("unexpected SECRET detail")

    def report(self, parameters: dict[str, tuple[str, ...]]) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def market_chart(self, parameters: dict[str, tuple[str, ...]]) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def fundamental_trend(self, parameters: dict[str, tuple[str, ...]]) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def fundamental_research(
        self,
        parameters: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def fundamental_research_history(
        self,
        parameters: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def fundamental_analysis(
        self,
        parameters: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        del parameters
        raise RuntimeError("unexpected SECRET detail")

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        del payload
        raise RuntimeError("unexpected SECRET detail")


@contextmanager
def _server(application: object) -> Iterator[tuple[AaplLocalHttpServer, str]]:
    server = AaplLocalHttpServer(("127.0.0.1", 0), cast(AaplLocalWebApplication, application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _json_request(request: Request) -> tuple[int, dict[str, object], dict[str, str]]:
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())
    with response:
        return response.status, json.loads(response.read()), dict(response.headers.items())


def test_local_server_serves_packaged_assets_with_security_headers() -> None:
    with _server(_ExplodingApplication()) as (_, root):
        with urlopen(f"{root}/", timeout=5) as response:
            body = response.read().decode("utf-8")
            headers = dict(response.headers.items())
        head = Request(f"{root}/", method="HEAD")
        with urlopen(head, timeout=5) as head_response:
            head_body = head_response.read()

        assert response.status == 200
        assert "Investment Analyst" in body
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"
        assert head_response.status == 200
        assert head_body == b""

        compressed = Request(f"{root}/assets/app.js", headers={"Accept-Encoding": "gzip"})
        with urlopen(compressed, timeout=5) as compressed_response:
            compressed_body = compressed_response.read()
            compressed_headers = dict(compressed_response.headers.items())

        assert compressed_headers["Content-Encoding"] == "gzip"
        assert compressed_headers["Vary"] == "Accept-Encoding"
        assert b'const LOCALE = "es-PE"' in gzip.decompress(compressed_body)


def test_local_assets_use_spanish_accessible_contextual_presentation() -> None:
    with _server(_ExplodingApplication()) as (_, root):
        with urlopen(f"{root}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{root}/assets/app.js", timeout=5) as response:
            javascript = response.read().decode("utf-8")
            javascript_content_type = response.headers["Content-Type"]
        with urlopen(f"{root}/assets/styles.css", timeout=5) as response:
            stylesheet = response.read().decode("utf-8")
            stylesheet_content_type = response.headers["Content-Type"]

    assert '<html lang="es" data-theme="dark">' in html
    assert "Saltar al contenido principal" in html
    assert "Análisis descriptivo." not in html
    assert "Precio, riesgo, actividad y tendencia" not in html
    assert "Ver contrato técnico JSON sin redondear" in html
    assert "Histórico de precio y volumen" in html
    assert "SMA 5" in html and "SMA 20" in html and "SMA 50" in html
    assert 'id="chart-settings"' in html
    assert 'id="sma-short-window"' in html
    assert 'id="sma-long-window"' in html
    assert 'id="sma-third-window"' in html
    assert 'id="sma-short-color"' in html
    assert 'id="sma-long-color"' in html
    assert 'id="sma-third-color"' in html
    assert 'id="chart-price-scale"' in html
    assert '<option value="logarithmic">Logarítmica</option>' in html
    assert 'data-chart-type="line"' in html
    assert 'data-chart-type="candlestick"' in html
    assert 'class="chart-type-button active" type="button" data-chart-type="candlestick"' in html
    assert 'id="chart-interval"' in html
    assert 'id="chart-zoom-reset"' not in html
    assert "Rueda: zoom · Arrastrar: desplazar · ← → recorrer" not in html
    assert 'id="snapshot-range-title"' in html
    assert "Último año" in html
    assert 'rel="icon"' in html
    assert '<option value="1w">1 semana · últimos 5 años</option>' in html
    assert '<option value="1mo">1 mes · historial completo</option>' in html
    assert "Consultar los datos visibles en una tabla" in html
    assert "Estadísticas técnicas" in html
    assert "Volatilidad 20" in html
    assert "Vol. relativo 20" in html
    assert "Evolución financiera" in html
    assert 'id="fundamental-chart-symbol">AAPL</span>' in html
    assert "Ingresos y resultado neto" in html
    assert "Ficha fundamental" in html
    assert "Métricas por área" in html
    assert "Fórmulas y evidencia exacta" in html
    assert "Tema claro" in html
    assert 'id="lima-clock"' in html
    assert 'id="new-york-clock"' in html
    assert 'id="nyse-session-status"' in html
    assert "09:30–16:00 ET" in html
    assert "no evalúa feriados ni cierres" in html
    assert 'class="market-clock-strip"' in html
    assert html.index('class="market-clock-strip"') < html.index('<main id="contenido"')
    assert "market-clock-panel" not in html
    assert 'id="export-market-csv"' in html
    assert 'id="export-fundamental-csv"' in html
    assert 'id="export-fundamental-research-csv"' in html
    assert 'id="export-report-json"' in html
    assert 'byId("fundamental-chart-symbol").textContent = presentation.symbol;' in javascript
    assert "Evolución fundamental de ${marketAssetPresentation().name}" in javascript
    assert 'byId("market-chart").setAttribute(' in javascript
    assert javascript_content_type == "text/javascript; charset=utf-8"
    assert stylesheet_content_type == "text/css; charset=utf-8"
    assert '"market.history.relative_volume"' in javascript
    assert 'kind: "multiple"' in javascript
    assert 'kind: "percentage"' in javascript
    assert 'style: "currency"' in javascript
    assert "formatScore(diagnostic.final_score)" in javascript
    assert "formatConfidence(diagnostic.confidence)" in javascript
    assert "JSON.stringify(report, null, 2)" in javascript
    assert "await queryReport();" in javascript
    assert 'intraday ? "/api/market-intraday" : "/api/market-chart"' in javascript
    assert "normalizeBtcIntradayChart(payload)" in javascript
    assert "market-intraday-refresh" in javascript
    assert "fundamental-refresh" in javascript
    assert "data-complete-analysis-only" in html
    assert '"5m", label: "5 min · últimas 24 h"' in javascript
    assert 'class="period-selector"' not in html
    assert 'class="period-button' not in html
    assert "data-period=" not in html
    assert 'auto: "1y"' in javascript
    assert '"1d": "1y"' in javascript
    assert '"1w": "5y"' in javascript
    assert '"1mo": "max"' in javascript
    assert 'parameters.set("period", requestedPeriod)' in javascript
    assert "chart.period !== requestedPeriod" in javascript
    assert "chart.coverage.earliest_selected_timestamp" in javascript
    assert "chart.coverage.latest_selected_timestamp" in javascript
    assert 'id="snapshot-range-cagr"' in html
    assert 'id="snapshot-range-drawdown"' in html
    assert 'id="chart-data-disclosure"' in html
    assert 'byId("chart-data-disclosure").addEventListener("toggle"' in javascript
    assert 'event.key === "ArrowLeft"' in javascript
    assert "function zoomMarketChart" in javascript
    assert "function visibleMarketChartPoints" in javascript
    assert 'addEventListener("wheel", handleMarketChartWheel, { passive: false })' in javascript
    assert "function panMarketChart" in javascript
    assert "host.onpointerdown" in javascript
    assert "host.setPointerCapture" in javascript
    assert "event.preventDefault()" in javascript
    assert "MINIMUM_CHART_VIEW_POINTS" in javascript
    assert 'const DEFAULT_TIME_ZONE = "America/Lima"' in javascript
    assert 'const NEW_YORK_TIME_ZONE = "America/New_York"' in javascript
    assert "function newYorkRegularSessionState" in javascript
    assert "const OVERVIEW_REFRESH_MS = 30_000" in javascript
    assert "const OVERVIEW_MAX_BACKOFF_MS = 5 * 60_000" in javascript
    assert "if (overviewRequestActive) return" in javascript
    assert "if (document.hidden) return" in javascript
    assert "() => refreshOverview({ manual: false })" in javascript
    assert 'document.addEventListener("visibilitychange", () => {' in javascript
    assert "refreshOverview({ manual: false })" in javascript
    assert "window.setTimeout(startMarketClocks, delay)" in javascript
    assert ".market-clock-strip" in stylesheet
    assert "window.requestAnimationFrame" in javascript
    assert 'event.key === "0"' in javascript
    assert "marketCsvRows(marketChartPayload, points)" in javascript
    assert 'document.querySelectorAll(".series-toggle")' in javascript
    assert '"market.history.rolling_daily_volatility"' in javascript
    assert "renderMarketSnapshot(chart, latest, latestPoint)" in javascript
    assert "chart.coverage.selected_sessions" in javascript
    assert "chart.coverage.displayed_points" in javascript
    assert '"aggregation_algorithm_version"' in javascript
    assert 'id="chart-point-period-label"' in html
    assert "renderFundamentalTrend" in javascript
    assert "api(`/api/fundamental-trend?${parameters.toString()}`)" in javascript
    assert "api(`/api/fundamental-analysis?${parameters.toString()}`)" in javascript
    assert "asset_id: selectedMarketAsset" in javascript
    assert "data-fundamental-only" in html
    assert "data-apple-only" not in html
    assert '"fundamental.research.free_cash_flow_margin"' in javascript
    assert '"fundamental.research.operating_cash_flow_to_net_income"' in javascript
    assert "function renderFundamentalResearch(payload)" in javascript
    assert "function renderCompanyProfile(classification)" in javascript
    assert "for (const section of payload.sections || [])" in javascript
    assert "classification.categories || []" in javascript
    assert "fundamental-lens" not in javascript
    assert ".fundamental-research-metric-change.increase" in stylesheet
    assert ".fundamental-research-metric-change.decrease" in stylesheet
    assert 'id="company-profile-title"' in html
    assert "Clasificación no determinada" in html
    assert "Métricas por área" in html
    assert "Último cierre" in html
    assert "Última sesión" not in html
    assert "Graham" not in html
    assert "Buffett" not in html
    assert "Lynch" not in html
    assert "compound_annual_growth_rate" in javascript
    assert "latest_change_from_previous_available" in javascript
    assert "window.localStorage.setItem(THEME_STORAGE_KEY, theme)" in javascript
    assert "window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY" in javascript
    assert 'id="market-asset-select"' in html
    assert 'api("/api/market-assets")' in javascript
    assert "marketAssetFromDescriptor(descriptor)" in javascript
    assert "descriptor.analysis.market_mode" in javascript
    assert "function isIntradayInterval" in javascript
    assert "const presentation = marketAssets[selectedMarketAsset];" in javascript
    assert 'selectedMarketAsset === "crypto:btc-usd"' not in javascript
    assert "MARKET_ASSETS" not in javascript
    assert "chart.sma_windows[0] !== chartSettings.shortWindow" in javascript
    assert 'parameters.set("short_sma_window", String(chartSettings.shortWindow))' in javascript
    assert 'parameters.set("long_sma_window", String(chartSettings.longWindow))' in javascript
    assert 'parameters.set("third_sma_window", String(chartSettings.thirdWindow))' in javascript
    assert 'priceScale: "linear"' in javascript
    assert 'chartType: "candlestick"' in javascript
    assert 'chartSettings.priceScale === "logarithmic" ? Math.log' in javascript
    assert "for (const price of prices)" in javascript
    assert "Number.POSITIVE_INFINITY" in javascript
    assert "renderMarketChart(marketChartPayload, { preserveViewport: true })" in javascript
    assert "function appendCandlesticks" in javascript
    assert 'chartSettings.chartType === "candlestick"' in javascript
    assert "interval: chartSettings.interval" in javascript
    assert "function exportMarketCsv()" in javascript
    assert "function exportFundamentalCsv()" in javascript
    assert "function exportFundamentalResearchCsv()" in javascript
    assert "function exportReportJson()" in javascript
    assert '"long_sma_input_observation_ids"' in javascript
    assert '"third_sma_input_observation_ids"' in javascript
    assert '"observation_id"' in javascript
    assert "new Blob([content]" in javascript
    assert "URL.revokeObjectURL(url)" in javascript
    assert "document.createElementNS(SVG_NAMESPACE, tag)" in javascript
    assert "maximumFractionDigits: 2" in javascript
    assert ".market-chart-card" in stylesheet
    assert ".chart-settings" in stylesheet
    assert "width: 184px;" in stylesheet
    assert ".chart-settings-panel select" in stylesheet
    assert ".candlestick-bodies.positive" in stylesheet
    assert ".candlestick-bodies.negative" in stylesheet
    assert ".candlestick-current-bodies" in stylesheet
    assert ".volume-bars path" in stylesheet
    assert ".sma-fifty-line" in stylesheet
    assert ".chart-host.is-panning" in stylesheet
    assert "· En curso" in javascript
    assert ".chart-inspector" in stylesheet
    assert ".market-workbench" in stylesheet
    assert ".market-snapshot" in stylesheet
    assert ':root[data-theme="dark"]' in stylesheet
    assert ".fundamental-workbench" in stylesheet
    assert ".fundamental-chart-svg" in stylesheet
    assert ".fundamental-research-grid" in stylesheet
    assert ".fundamental-research-audit" in stylesheet
    assert ".fundamental-research-metric-change" in stylesheet
    assert ".data-export-button" in stylesheet
    assert ":focus-visible" in stylesheet
    assert "min-height: 44px" in stylesheet
    assert "prefers-reduced-motion" in stylesheet
    assert "forced-colors: active" in stylesheet
    startup = javascript[javascript.index("async function initialize()") :]
    assert startup.index("await loadMarketAssets();") < startup.index("initializeChartSettings();")


def test_local_api_validates_and_delegates_run_report_and_overview(tmp_path: Path) -> None:
    runner = _FakeRunner()
    application = _FakeApplication()
    workspace = tmp_path / "workspace"
    controller = AaplLocalController(
        runner,
        application,
        workspace=workspace,
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    web = AaplLocalWebApplication(controller, None)

    with _server(web) as (_, root):
        overview_status, overview, _ = _json_request(Request(f"{root}/api/overview"))
        assets_status, assets, _ = _json_request(Request(f"{root}/api/market-assets"))
        payload = json.dumps(
            {
                "asset_id": "equity:us:aapl",
                "market_start": "2025-01-01",
                "market_end": "2026-07-15",
                "fundamental_frequency": "quarterly",
                "refresh_mode": "auto",
                "requested_known_at": None,
                "require_complete": True,
            }
        ).encode("utf-8")
        run_status, run, _ = _json_request(
            Request(
                f"{root}/api/run",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        parameters = urlencode(
            {
                "known_at": "2026-07-16T15:46:09Z",
                "fundamental_frequency": "quarterly",
            }
        )
        report_status, report, _ = _json_request(Request(f"{root}/api/report?{parameters}"))
        chart_parameters = urlencode(
            {
                "known_at": "2026-07-16T15:46:09Z",
                "period": "1y",
                "interval": "1w",
                "short_sma_window": "10",
                "long_sma_window": "50",
                "third_sma_window": "100",
            }
        )
        chart_status, chart, _ = _json_request(
            Request(f"{root}/api/market-chart?{chart_parameters}")
        )
        cached_chart_status, cached_chart, _ = _json_request(
            Request(f"{root}/api/market-chart?{chart_parameters}")
        )
        maximum_chart_status, maximum_chart, _ = _json_request(
            Request(
                f"{root}/api/market-chart?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'period': 'max'})}"
            )
        )
        btc_chart_parameters = urlencode(
            {
                "asset_id": "crypto:btc-usd",
                "known_at": "2026-07-16T15:46:09Z",
                "period": "max",
                "interval": "1d",
            }
        )
        btc_chart_status, btc_chart, _ = _json_request(
            Request(f"{root}/api/market-chart?{btc_chart_parameters}")
        )
        cached_btc_chart_status, cached_btc_chart, _ = _json_request(
            Request(f"{root}/api/market-chart?{btc_chart_parameters}")
        )
        listed_chart_parameters = urlencode(
            {
                "asset_id": "equity:us:bvn",
                "known_at": "2026-07-16T15:46:09Z",
                "period": "1y",
                "interval": "1d",
            }
        )
        listed_chart_status, listed_chart, _ = _json_request(
            Request(f"{root}/api/market-chart?{listed_chart_parameters}")
        )
        btc_intraday_parameters = urlencode(
            {
                "asset_id": "crypto:btc-usd",
                "known_at": "2026-07-16T15:46:09Z",
                "interval": "5m",
            }
        )
        btc_intraday_status, btc_intraday, _ = _json_request(
            Request(f"{root}/api/market-intraday?{btc_intraday_parameters}")
        )
        cached_btc_intraday_status, cached_btc_intraday, _ = _json_request(
            Request(f"{root}/api/market-intraday?{btc_intraday_parameters}")
        )
        btc_refresh_payload = json.dumps(
            {
                "asset_id": "crypto:btc-usd",
                "market_start": "2015-07-20",
                "market_end": "2026-07-15",
                "refresh_mode": "auto",
                "requested_known_at": None,
            }
        ).encode("utf-8")
        btc_refresh_status, btc_refresh, _ = _json_request(
            Request(
                f"{root}/api/market-refresh",
                data=btc_refresh_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        listed_refresh_payload = json.dumps(
            {
                "asset_id": "equity:us:bvn",
                "market_start": "2016-01-01",
                "market_end": "2026-07-15",
                "refresh_mode": "auto",
                "requested_known_at": None,
            }
        ).encode("utf-8")
        listed_refresh_status, listed_refresh, _ = _json_request(
            Request(
                f"{root}/api/market-refresh",
                data=listed_refresh_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        btc_intraday_refresh_payload = json.dumps(
            {
                "asset_id": "crypto:btc-usd",
                "hours": 24,
                "requested_end": "2026-07-16T15:46:00Z",
            }
        ).encode("utf-8")
        btc_intraday_refresh_status, btc_intraday_refresh, _ = _json_request(
            Request(
                f"{root}/api/market-intraday-refresh",
                data=btc_intraday_refresh_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        fundamental_refresh_payload = json.dumps(
            {
                "asset_id": "equity:us:aapl",
                "frequency": "quarterly",
                "requested_known_at": "2026-07-16T15:46:09Z",
            }
        ).encode("utf-8")
        fundamental_refresh_status, fundamental_refresh, _ = _json_request(
            Request(
                f"{root}/api/fundamental-refresh",
                data=fundamental_refresh_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        trend_status, trend, _ = _json_request(
            Request(
                f"{root}/api/fundamental-trend?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'quarterly'})}"
            )
        )
        research_status, research, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'quarterly'})}"
            )
        )
        cached_research_status, cached_research, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'quarterly'})}"
            )
        )
        history_status, history, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research-history?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'annual'})}"
            )
        )
        cached_history_status, cached_history, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research-history?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'annual'})}"
            )
        )
        analysis_status, analysis, _ = _json_request(
            Request(
                f"{root}/api/fundamental-analysis?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'quarterly'})}"
            )
        )
        cached_analysis_status, cached_analysis, _ = _json_request(
            Request(
                f"{root}/api/fundamental-analysis?"
                f"{urlencode({'known_at': '2026-07-16T15:46:09Z', 'frequency': 'quarterly'})}"
            )
        )

    assert overview_status == 200
    assert overview["operational"]["status"] == "ready"
    assert overview["scheduler"] == {"enabled": False}
    assert assets_status == 200
    assert assets["schema_version"] == "market-asset-universe-v3"
    assert assets["catalog_version"] == 1
    assert len(assets["assets"]) == 18
    assets_by_id = {item["asset_id"]: item for item in assets["assets"]}
    assert assets_by_id["equity:us:aapl"]["analysis"]["fundamental_mode"] == "corporate"
    assert assets_by_id["equity:us:amd"]["has_fundamentals"] is True
    assert assets_by_id["equity:us:amd"]["refresh_kind"] == "market_only"
    assert assets_by_id["equity:us:intc"]["has_fundamentals"] is True
    assert assets_by_id["equity:us:intc"]["refresh_kind"] == "market_only"
    for asset_id in ("equity:us:mstr", "equity:us:mu", "equity:us:pltr"):
        assert assets_by_id[asset_id]["has_fundamentals"] is True
        assert assets_by_id[asset_id]["refresh_kind"] == "market_only"
    for asset_id in (
        "equity:us:cde",
        "equity:us:hymc",
        "equity:us:mux",
        "equity:us:nem",
        "equity:us:scco",
    ):
        assert assets_by_id[asset_id]["has_fundamentals"] is True
        assert assets_by_id[asset_id]["refresh_kind"] == "market_only"
    for asset_id in ("equity:us:b", "equity:us:bvn", "equity:us:tsm"):
        assert assets_by_id[asset_id]["has_fundamentals"] is True
        assert assets_by_id[asset_id]["fundamental_frequencies"] == ["annual"]
    assert assets_by_id["etf:us:ibit"]["analysis"]["fundamental_mode"] == "investment_fund"
    assert assets_by_id["crypto:btc-usd"]["analysis"]["market_mode"] == "crypto_spot"
    assert run_status == 200 and run["status"] == "succeeded"
    assert runner.requests[0].market_start == date(2025, 1, 1)
    assert runner.requests[0].market_end == date(2026, 7, 15)
    assert report_status == 200
    assert report["schema_version"] == "aapl-daily-diagnostic-report-v1"
    assert application.requests[0].known_at.isoformat() == "2026-07-16T15:46:09+00:00"
    assert application.locations[0].workspace == workspace.resolve()
    assert chart_status == 200
    assert chart["schema_version"] == "aapl-market-chart-v5"
    assert chart["period"] == "1y"
    assert application.chart_requests[0].known_at.isoformat() == "2026-07-16T15:46:09+00:00"
    assert application.chart_requests[0].session_limit == 260
    assert application.chart_requests[0].interval.value == "1w"
    assert application.chart_requests[0].resolution.value == "weekly"
    assert application.chart_requests[0].short_sma_window == 10
    assert application.chart_requests[0].long_sma_window == 50
    assert application.chart_requests[0].third_sma_window == 100
    assert application.chart_locations[0].workspace == workspace.resolve()
    assert cached_chart_status == 200
    assert cached_chart == chart
    assert maximum_chart_status == 200
    assert maximum_chart["period"] == "max"
    assert application.chart_requests[1].interval.value == "auto"
    assert application.chart_requests[1].session_limit == 20_000
    assert len(application.chart_requests) == 2
    assert btc_chart_status == 200
    assert btc_chart["schema_version"] == "btc-market-chart-v1"
    assert btc_chart["asset_id"] == "crypto:btc-usd"
    assert cached_btc_chart_status == 200
    assert cached_btc_chart == btc_chart
    assert len(application.btc_chart_requests) == 1
    assert application.btc_chart_requests[0].interval.value == "1d"
    assert application.btc_chart_requests[0].session_limit == 20_000
    assert application.btc_chart_locations[0].workspace == workspace.resolve()
    assert listed_chart_status == 200
    assert listed_chart["schema_version"] == "listed-market-chart-v1"
    assert listed_chart["asset_id"] == "equity:us:bvn"
    assert application.listed_chart_requests[0][0] == "equity:us:bvn"
    assert application.listed_chart_locations[0].workspace == workspace.resolve()
    assert btc_intraday_status == 200
    assert btc_intraday["schema_version"] == "btc-intraday-chart-v1"
    assert btc_intraday["interval"] == "5m"
    assert cached_btc_intraday_status == 200
    assert cached_btc_intraday == btc_intraday
    assert len(application.btc_intraday_chart_requests) == 1
    assert application.btc_intraday_chart_requests[0].interval is IntradayInterval.MINUTE_5
    assert application.btc_intraday_chart_requests[0].query_start.isoformat() == (
        "2026-07-15T15:46:00+00:00"
    )
    assert application.btc_intraday_chart_locations[0].workspace == workspace.resolve()
    assert btc_refresh_status == 200
    assert btc_refresh["schema_version"] == "btc-market-refresh-v1"
    assert btc_refresh["traceability_verified"] is True
    assert application.btc_refresh_requests == [
        BtcMarketRefreshRequest(
            market_start=date(2015, 7, 20),
            market_end=date(2026, 7, 15),
        )
    ]
    assert application.btc_refresh_locations[0].workspace == workspace.resolve()
    assert listed_refresh_status == 200
    assert listed_refresh["schema_version"] == "listed-market-refresh-v1"
    assert application.listed_refresh_requests[0].asset_id == "equity:us:bvn"
    assert application.listed_refresh_locations[0].workspace == workspace.resolve()
    assert btc_intraday_refresh_status == 200
    assert btc_intraday_refresh["schema_version"] == "btc-intraday-refresh-v1"
    assert btc_intraday_refresh["traceability_verified"] is True
    assert application.btc_intraday_refresh_requests == [
        BtcIntradayRefreshRequest(
            requested_end=datetime(2026, 7, 16, 15, 46, tzinfo=UTC),
        )
    ]
    assert application.btc_intraday_refresh_locations[0].workspace == workspace.resolve()
    assert fundamental_refresh_status == 200
    assert fundamental_refresh["schema_version"] == "sec-issuer-fundamental-refresh-v1"
    assert fundamental_refresh["asset_id"] == "equity:us:aapl"
    assert application.fundamental_refresh_requests == [
        SecIssuerFundamentalRefreshRequest(
            asset_id="equity:us:aapl",
            frequency=DataFrequency.QUARTERLY,
            requested_known_at=datetime(2026, 7, 16, 15, 46, 9, tzinfo=UTC),
        )
    ]
    assert application.fundamental_refresh_locations[0].workspace == workspace.resolve()
    assert application.fundamental_refresh_identities == [
        SecEdgarIdentity("Investment Analyst tests@example.com")
    ]
    assert trend_status == 200
    assert trend["schema_version"] == "aapl-fundamental-trend-v1"
    assert trend["frequency"] == "quarterly"
    assert trend["period_limit"] == 8
    assert application.trend_asset_ids == ["equity:us:aapl"]
    assert application.trend_requests[0].known_at.isoformat() == "2026-07-16T15:46:09+00:00"
    assert application.trend_locations[0].workspace == workspace.resolve()
    assert research_status == 200
    assert research["schema_version"] == "aapl-fundamental-research-v2"
    assert research["frequency"] == "quarterly"
    assert research["period_limit"] == 8
    assert cached_research_status == 200
    assert cached_research == research
    assert len(application.research_requests) == 1
    assert application.research_asset_ids == ["equity:us:aapl"]
    assert application.research_requests[0].known_at.isoformat() == ("2026-07-16T15:46:09+00:00")
    assert application.research_locations[0].workspace == workspace.resolve()
    assert history_status == 200
    assert history["schema_version"] == "aapl-fundamental-research-history-v2"
    assert history["request"]["frequency"] == "annual"
    assert history["request"]["limit"] == 5
    assert cached_history_status == 200
    assert cached_history == history
    assert len(application.research_history_requests) == 1
    assert application.research_history_asset_ids == ["equity:us:aapl"]
    assert application.research_history_locations[0].workspace == workspace.resolve()
    assert analysis_status == 200
    assert analysis["schema_version"] == "aapl-fundamental-analysis-v1"
    assert analysis["request"]["frequency"] == "quarterly"
    assert analysis["request"]["limit"] == 8
    assert cached_analysis_status == 200
    assert cached_analysis == analysis
    assert len(application.analysis_requests) == 1
    assert application.analysis_asset_ids == ["equity:us:aapl"]
    assert application.analysis_locations[0].workspace == workspace.resolve()


def test_local_api_exposes_empty_persistent_alert_inbox(tmp_path: Path) -> None:
    application = _FakeApplication()
    controller = AaplLocalController(
        _FakeRunner(),
        application,
        workspace=tmp_path / "workspace",
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    alerts = OperationalAlertStateStore(tmp_path / "state" / "alerts.json")

    with _server(AaplLocalWebApplication(controller, None, alerts)) as (_, root):
        overview_status, overview, _ = _json_request(Request(f"{root}/api/overview"))
        inbox_status, inbox, _ = _json_request(Request(f"{root}/api/alerts?limit=10"))
        invalid_status, invalid, _ = _json_request(Request(f"{root}/api/alerts?limit=0"))

    assert overview_status == 200
    assert overview["alerts"]["enabled"] is True
    assert overview["alerts"]["silent_mode"] is True
    assert inbox_status == 200
    assert inbox["total"] == 0
    assert inbox["events"] == []
    assert invalid_status == 400
    assert invalid["error"]["code"] == "invalid_request"


def test_local_api_audits_alert_inbox_transitions(tmp_path: Path) -> None:
    application = _FakeApplication()
    controller = AaplLocalController(
        _FakeRunner(),
        application,
        workspace=tmp_path / "workspace",
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    alerts = OperationalAlertStateStore(tmp_path / "state" / "alerts.json")
    definition = ScheduledJobDefinition(
        job_id="test:failed",
        provider="test-provider",
        domain=ScheduledJobDomain.CATALOG,
        data_frequency="daily",
    )
    completed_at = datetime(2026, 7, 29, 12, 2, tzinfo=UTC)
    OperationalAlertMonitor(
        alerts,
        clock=lambda: completed_at,
    )(
        ScheduledJobAttempt(
            attempt_id=UUID("00000000-0000-4000-8000-000000000999"),
            definition=definition,
            local_date=date(2026, 7, 29),
            scheduled_for=definition.scheduled_for(date(2026, 7, 29)),
            attempt_number=1,
            status=ScheduledJobAttemptStatus.FAILED,
            started_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
            completed_at=completed_at,
            failure=ScheduledJobFailure(
                category="provider_unavailable",
                message="safe failure",
                retryable=True,
            ),
        )
    )
    alert_id = str(alerts.inbox().events[0].alert_id)

    with _server(AaplLocalWebApplication(controller, None, alerts)) as (_, root):
        transition_status, transition, _ = _json_request(
            Request(
                f"{root}/api/alerts/transition",
                data=json.dumps({"alert_id": alert_id, "status": "seen"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        inbox_status, inbox, _ = _json_request(Request(f"{root}/api/alerts?limit=10"))

    assert transition_status == 200
    assert transition["changed"] is True
    assert transition["event"]["status"] == "seen"
    assert inbox_status == 200
    assert inbox["events"][0]["status"] == "seen"
    assert alerts.status().new_count == 0
    assert len(alerts.load().transitions) == 1


def test_local_api_serves_and_refreshes_enabled_amd_fundamentals_independently(
    tmp_path: Path,
) -> None:
    application = _FakeApplication()
    workspace = tmp_path / "workspace"
    controller = AaplLocalController(
        _FakeRunner(),
        application,
        workspace=workspace,
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )

    with _server(AaplLocalWebApplication(controller, None)) as (_, root):
        query = urlencode(
            {
                "asset_id": "equity:us:amd",
                "known_at": "2026-07-16T15:46:09Z",
                "frequency": "annual",
            }
        )
        trend_status, trend, _ = _json_request(Request(f"{root}/api/fundamental-trend?{query}"))
        analysis_status, analysis, _ = _json_request(
            Request(f"{root}/api/fundamental-analysis?{query}")
        )
        refresh_status, refresh, _ = _json_request(
            Request(
                f"{root}/api/fundamental-refresh",
                data=json.dumps(
                    {
                        "asset_id": "equity:us:amd",
                        "frequency": "annual",
                        "requested_known_at": None,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    assert trend_status == 200
    assert trend["schema_version"] == "sec-fundamental-trend-v2"
    assert trend["asset_id"] == "equity:us:amd"
    assert analysis_status == 200
    assert analysis["schema_version"] == "sec-fundamental-analysis-v2"
    assert analysis["asset_id"] == "equity:us:amd"
    assert refresh_status == 200
    assert refresh["schema_version"] == "sec-issuer-fundamental-refresh-v1"
    assert refresh["asset_id"] == "equity:us:amd"
    assert application.trend_asset_ids == ["equity:us:amd"]
    assert application.analysis_asset_ids == ["equity:us:amd"]
    assert application.fundamental_refresh_requests[0].asset_id == "equity:us:amd"
    assert application.fundamental_refresh_locations[0].workspace == workspace.resolve()


def test_read_caches_are_bounded_to_data_before_the_next_run_attempt(tmp_path: Path) -> None:
    runner = _FakeRunner()
    application = _FakeApplication()
    controller = AaplLocalController(
        runner,
        application,
        workspace=tmp_path / "workspace",
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    chart_request = AaplMarketChartRequest(known_at=datetime(2026, 7, 16, tzinfo=UTC))
    btc_chart_request = BtcMarketChartRequest(known_at=datetime(2026, 7, 16, tzinfo=UTC))
    btc_intraday_chart_request = BtcIntradayChartRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        interval=IntradayInterval.MINUTE_5,
    )
    trend_request = AaplFundamentalTrendRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        frequency=DataFrequency.QUARTERLY,
        period_limit=8,
    )
    research_request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        frequency=DataFrequency.QUARTERLY,
        limit=8,
    )
    run_payload: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "market_start": "2025-01-01",
        "market_end": "2026-07-15",
        "fundamental_frequency": "quarterly",
        "refresh_mode": "auto",
        "requested_known_at": None,
        "require_complete": True,
    }

    controller.market_chart_request(chart_request)
    controller.market_chart_request(chart_request)
    controller.btc_market_chart_request(btc_chart_request)
    controller.btc_market_chart_request(btc_chart_request)
    controller.btc_intraday_chart_request(btc_intraday_chart_request)
    controller.btc_intraday_chart_request(btc_intraday_chart_request)
    controller.fundamental_trend_request(trend_request)
    controller.fundamental_trend_request(trend_request)
    controller.fundamental_research_request(research_request)
    controller.fundamental_research_request(research_request)
    controller.fundamental_research_history_request(research_request)
    controller.fundamental_research_history_request(research_request)
    controller.fundamental_analysis_request(research_request)
    controller.fundamental_analysis_request(research_request)
    controller.run_payload(run_payload)
    controller.market_chart_request(chart_request)
    controller.btc_market_chart_request(btc_chart_request)
    controller.btc_intraday_chart_request(btc_intraday_chart_request)
    controller.fundamental_trend_request(trend_request)
    controller.fundamental_research_request(research_request)
    controller.fundamental_research_history_request(research_request)
    controller.fundamental_analysis_request(research_request)

    assert len(application.chart_requests) == 2
    assert len(application.btc_chart_requests) == 2
    assert len(application.btc_intraday_chart_requests) == 2
    assert len(application.trend_requests) == 2
    assert len(application.research_requests) == 2
    assert len(application.research_history_requests) == 2
    assert len(application.analysis_requests) == 2
    assert len(runner.requests) == 1


def test_fundamental_read_caches_are_isolated_by_sec_asset(tmp_path: Path) -> None:
    application = _FakeApplication()
    controller = AaplLocalController(
        _FakeRunner(),
        application,
        workspace=tmp_path / "workspace",
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )
    trend_request = AaplFundamentalTrendRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        period_limit=5,
    )
    research_request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    for asset_id in ("equity:us:aapl", "equity:us:amd"):
        controller.fundamental_trend_request(trend_request, asset_id=asset_id)
        controller.fundamental_trend_request(trend_request, asset_id=asset_id)
        controller.fundamental_research_request(research_request, asset_id=asset_id)
        controller.fundamental_research_request(research_request, asset_id=asset_id)
        controller.fundamental_research_history_request(
            research_request,
            asset_id=asset_id,
        )
        controller.fundamental_research_history_request(
            research_request,
            asset_id=asset_id,
        )
        controller.fundamental_analysis_request(research_request, asset_id=asset_id)
        controller.fundamental_analysis_request(research_request, asset_id=asset_id)

    assert application.trend_asset_ids == ["equity:us:aapl", "equity:us:amd"]
    assert application.research_asset_ids == ["equity:us:aapl", "equity:us:amd"]
    assert application.research_history_asset_ids == ["equity:us:aapl", "equity:us:amd"]
    assert application.analysis_asset_ids == ["equity:us:aapl", "equity:us:amd"]


def test_local_api_rejects_cross_host_unsafe_content_and_invalid_payload() -> None:
    with _server(_ExplodingApplication()) as (server, root):
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.putrequest("GET", "/api/overview", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        invalid_host = connection.getresponse()
        invalid_host_payload = json.loads(invalid_host.read())
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(
            "POST",
            "/api/run",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "16385",
            },
        )
        oversized = connection.getresponse()
        oversized_payload = json.loads(oversized.read())
        connection.close()

        media_status, media, _ = _json_request(
            Request(
                f"{root}/api/run",
                data=b"{}",
                headers={"Content-Type": "text/plain"},
                method="POST",
            )
        )
        unexpected_status, unexpected, _ = _json_request(Request(f"{root}/api/overview"))

    assert invalid_host.status == 403
    assert invalid_host_payload["error"]["code"] == "invalid_host"
    assert oversized.status == 413
    assert oversized_payload["error"]["code"] == "request_too_large"
    assert media_status == 415
    assert media["error"]["code"] == "unsupported_media_type"
    assert unexpected_status == 500
    assert unexpected["error"]["message"] == "the local interface failed unexpectedly"
    assert "SECRET" not in json.dumps(unexpected)


def test_local_api_rejects_invalid_typed_run_without_calling_runner(tmp_path: Path) -> None:
    runner = _FakeRunner()
    controller = AaplLocalController(
        runner,
        _FakeApplication(),
        workspace=tmp_path / "workspace",
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity("Investment Analyst tests@example.com"),
    )

    with _server(AaplLocalWebApplication(controller, None)) as (_, root):
        status, payload, _ = _json_request(
            Request(
                f"{root}/api/run",
                data=b'{"market_start":"2025-01-01"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        duplicated_status, duplicated, _ = _json_request(
            Request(
                f"{root}/api/report?known_at=2026-07-16T15%3A46%3A09Z"
                "&known_at=2026-07-17T15%3A46%3A09Z"
                "&fundamental_frequency=quarterly"
            )
        )
        malformed_status, malformed, _ = _json_request(
            Request(
                f"{root}/api/run",
                data=b"{invalid",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        chart_status, chart, _ = _json_request(
            Request(
                f"{root}/api/market-chart?known_at=2026-07-16T15%3A46%3A09Z&period=6m&period=1y"
            )
        )
        chart_window_status, chart_window, _ = _json_request(
            Request(
                f"{root}/api/market-chart?known_at=2026-07-16T15%3A46%3A09Z"
                "&short_sma_window=50&long_sma_window=20"
            )
        )
        chart_interval_status, chart_interval, _ = _json_request(
            Request(f"{root}/api/market-chart?known_at=2026-07-16T15%3A46%3A09Z&interval=1h")
        )
        chart_asset_status, chart_asset, _ = _json_request(
            Request(
                f"{root}/api/market-chart?known_at=2026-07-16T15%3A46%3A09Z&asset_id=crypto:eth-usd"
            )
        )
        intraday_interval_status, intraday_interval, _ = _json_request(
            Request(f"{root}/api/market-intraday?known_at=2026-07-16T15%3A46%3A09Z&interval=1d")
        )
        intraday_asset_status, intraday_asset, _ = _json_request(
            Request(
                f"{root}/api/market-intraday?"
                "asset_id=equity:us:aapl&known_at=2026-07-16T15%3A46%3A09Z&interval=5m"
            )
        )
        intraday_refresh_status, intraday_refresh, _ = _json_request(
            Request(
                f"{root}/api/market-intraday-refresh",
                data=b'{"asset_id":"crypto:btc-usd","hours":25,"requested_end":null}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        trend_status, trend, _ = _json_request(
            Request(
                f"{root}/api/fundamental-trend?known_at=2026-07-16T15%3A46%3A09Z&frequency=monthly"
            )
        )
        research_status, research, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research?"
                "known_at=2026-07-16T15%3A46%3A09Z&frequency=monthly"
            )
        )
        history_status, history, _ = _json_request(
            Request(
                f"{root}/api/fundamental-research-history?"
                "known_at=2026-07-16T15%3A46%3A09Z&frequency=monthly"
            )
        )
        analysis_status, analysis, _ = _json_request(
            Request(
                f"{root}/api/fundamental-analysis?known_at=2026-07-16T15%3A46%3A09Z&frequency=monthly"
            )
        )
        cross_asset_status, cross_asset, _ = _json_request(
            Request(
                f"{root}/api/fundamental-analysis?"
                "asset_id=equity:us:b"
                "&known_at=2026-07-16T15%3A46%3A09Z"
                "&frequency=quarterly"
            )
        )
        fundamental_refresh_status, fundamental_refresh, _ = _json_request(
            Request(
                f"{root}/api/fundamental-refresh",
                data=(
                    b'{"asset_id":"equity:us:b","frequency":"quarterly","requested_known_at":null}'
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert duplicated_status == 400
    assert duplicated["error"]["code"] == "invalid_request"
    assert malformed_status == 400
    assert malformed["error"]["code"] == "invalid_json"
    assert chart_status == 400
    assert chart["error"]["code"] == "invalid_request"
    assert chart_window_status == 400
    assert chart_window["error"]["code"] == "invalid_request"
    assert chart_interval_status == 400
    assert chart_interval["error"]["code"] == "invalid_request"
    assert chart_asset_status == 400
    assert chart_asset["error"]["code"] == "invalid_request"
    assert intraday_interval_status == 400
    assert intraday_interval["error"]["code"] == "invalid_request"
    assert intraday_asset_status == 400
    assert intraday_asset["error"]["code"] == "invalid_request"
    assert intraday_refresh_status == 400
    assert intraday_refresh["error"]["code"] == "invalid_request"
    assert trend_status == 400
    assert trend["error"]["code"] == "invalid_request"
    assert research_status == 400
    assert research["error"]["code"] == "invalid_request"
    assert history_status == 400
    assert history["error"]["code"] == "invalid_request"
    assert analysis_status == 400
    assert analysis["error"]["code"] == "invalid_request"
    assert cross_asset_status == 400
    assert cross_asset["error"]["code"] == "invalid_request"
    assert fundamental_refresh_status == 400
    assert fundamental_refresh["error"]["code"] == "invalid_request"
    assert runner.requests == []


def test_local_server_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AaplLocalHttpServer(
            ("0.0.0.0", 0),
            cast(AaplLocalWebApplication, _ExplodingApplication()),
        )
