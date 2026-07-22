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
)
from investment_analyst.application.aapl_bootstrap_models import AaplWorkspaceBootstrapRequest
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplOperationalHealth,
)
from investment_analyst.application.runtime import StorageLocationRequest
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
        self.trend_requests: list[AaplFundamentalTrendRequest] = []
        self.trend_locations: list[StorageLocationRequest] = []
        self.research_requests: list[AaplFundamentalResearchRequest] = []
        self.research_locations: list[StorageLocationRequest] = []
        self.research_history_requests: list[AaplFundamentalResearchRequest] = []
        self.research_history_locations: list[StorageLocationRequest] = []
        self.analysis_requests: list[AaplFundamentalResearchRequest] = []
        self.analysis_locations: list[StorageLocationRequest] = []

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

    def query_aapl_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        self.trend_requests.append(request)
        self.trend_locations.append(location)
        return cast(
            AaplFundamentalTrend,
            _JsonResult(
                {
                    "schema_version": "aapl-fundamental-trend-v1",
                    "frequency": request.frequency.value,
                    "period_limit": request.period_limit,
                    "periods": [],
                }
            ),
        )

    def query_aapl_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        self.research_requests.append(request)
        self.research_locations.append(location)
        return cast(
            AaplFundamentalResearchResult,
            _JsonResult(
                {
                    "schema_version": "aapl-fundamental-research-v2",
                    "frequency": request.frequency.value,
                    "period_limit": request.limit,
                    "periods": [],
                }
            ),
        )

    def query_aapl_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        self.research_history_requests.append(request)
        self.research_history_locations.append(location)
        return cast(
            AaplFundamentalResearchHistoryResult,
            _JsonResult(
                {
                    "schema_version": "aapl-fundamental-research-history-v2",
                    "request": {
                        "frequency": request.frequency.value,
                        "limit": request.limit,
                    },
                    "research": {"schema_version": "aapl-fundamental-research-v2"},
                    "series": [],
                }
            ),
        )

    def query_aapl_fundamental_analysis(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalAnalysisResult:
        self.analysis_requests.append(request)
        self.analysis_locations.append(location)
        return cast(
            AaplFundamentalAnalysisResult,
            _JsonResult(
                {
                    "schema_version": "aapl-fundamental-analysis-v1",
                    "request": {
                        "frequency": request.frequency.value,
                        "limit": request.limit,
                    },
                    "history": {
                        "schema_version": "aapl-fundamental-research-history-v2",
                        "research": {"schema_version": "aapl-fundamental-research-v2"},
                        "series": [],
                    },
                    "classification": {"status": "insufficient_evidence"},
                    "sections": [],
                }
            ),
        )


class _ExplodingApplication:
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
    assert 'id="chart-interval"' in html
    assert 'id="chart-zoom-reset"' in html
    assert "Historial completo" in html
    assert 'rel="icon"' in html
    assert '<option value="1w">1 semana</option>' in html
    assert "Consultar los datos visibles en una tabla" in html
    assert "Estadísticas técnicas" in html
    assert "Volatilidad 20" in html
    assert "Vol. relativo 20" in html
    assert "Evolución financiera" in html
    assert "Ingresos y resultado neto" in html
    assert "Ficha fundamental" in html
    assert "Métricas por área" in html
    assert "Fórmulas y evidencia exacta" in html
    assert "Tema claro" in html
    assert 'id="export-market-csv"' in html
    assert 'id="export-fundamental-csv"' in html
    assert 'id="export-fundamental-research-csv"' in html
    assert 'id="export-report-json"' in html
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
    assert "api(`/api/market-chart?${parameters.toString()}`)" in javascript
    assert 'class="period-selector"' not in html
    assert 'class="period-button' not in html
    assert "data-period=" not in html
    assert 'const MARKET_CHART_PERIOD = "max";' in javascript
    assert "period: MARKET_CHART_PERIOD" in javascript
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
    assert "chart.sma_windows[0] !== chartSettings.shortWindow" in javascript
    assert "short_sma_window: String(chartSettings.shortWindow)" in javascript
    assert "long_sma_window: String(chartSettings.longWindow)" in javascript
    assert "third_sma_window: String(chartSettings.thirdWindow)" in javascript
    assert 'priceScale: "linear"' in javascript
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
    assert trend_status == 200
    assert trend["schema_version"] == "aapl-fundamental-trend-v1"
    assert trend["frequency"] == "quarterly"
    assert trend["period_limit"] == 8
    assert application.trend_requests[0].known_at.isoformat() == "2026-07-16T15:46:09+00:00"
    assert application.trend_locations[0].workspace == workspace.resolve()
    assert research_status == 200
    assert research["schema_version"] == "aapl-fundamental-research-v2"
    assert research["frequency"] == "quarterly"
    assert research["period_limit"] == 8
    assert cached_research_status == 200
    assert cached_research == research
    assert len(application.research_requests) == 1
    assert application.research_requests[0].known_at.isoformat() == ("2026-07-16T15:46:09+00:00")
    assert application.research_locations[0].workspace == workspace.resolve()
    assert history_status == 200
    assert history["schema_version"] == "aapl-fundamental-research-history-v2"
    assert history["request"]["frequency"] == "annual"
    assert history["request"]["limit"] == 5
    assert cached_history_status == 200
    assert cached_history == history
    assert len(application.research_history_requests) == 1
    assert application.research_history_locations[0].workspace == workspace.resolve()
    assert analysis_status == 200
    assert analysis["schema_version"] == "aapl-fundamental-analysis-v1"
    assert analysis["request"]["frequency"] == "quarterly"
    assert analysis["request"]["limit"] == 8
    assert cached_analysis_status == 200
    assert cached_analysis == analysis
    assert len(application.analysis_requests) == 1
    assert application.analysis_locations[0].workspace == workspace.resolve()


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
    controller.fundamental_trend_request(trend_request)
    controller.fundamental_research_request(research_request)
    controller.fundamental_research_history_request(research_request)
    controller.fundamental_analysis_request(research_request)

    assert len(application.chart_requests) == 2
    assert len(application.trend_requests) == 2
    assert len(application.research_requests) == 2
    assert len(application.research_history_requests) == 2
    assert len(application.analysis_requests) == 2
    assert len(runner.requests) == 1


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
    assert trend_status == 400
    assert trend["error"]["code"] == "invalid_request"
    assert research_status == 400
    assert research["error"]["code"] == "invalid_request"
    assert history_status == 400
    assert history["error"]["code"] == "invalid_request"
    assert analysis_status == 400
    assert analysis["error"]["code"] == "invalid_request"
    assert runner.requests == []


def test_local_server_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AaplLocalHttpServer(
            ("0.0.0.0", 0),
            cast(AaplLocalWebApplication, _ExplodingApplication()),
        )
