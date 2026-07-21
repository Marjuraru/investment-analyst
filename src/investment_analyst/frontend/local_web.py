"""Loopback-only HTTP adapter for the local Apple analysis application."""

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

from pydantic import ValidationError

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
    AaplMarketChartPeriod,
    AaplMarketChartRequest,
)
from investment_analyst.analytics.market.chart_service import AaplMarketChartQueryError
from investment_analyst.application.aapl_bootstrap import BootstrapIncompleteError
from investment_analyst.application.aapl_bootstrap_models import AaplWorkspaceBootstrapRequest
from investment_analyst.application.aapl_daily_runner import (
    AaplDailyRunExecutionError,
    AaplDailyRunner,
)
from investment_analyst.application.aapl_scheduler import AaplDailyScheduler
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.operational_models import (
    AaplDailyRunRequestSnapshot,
    AaplDailyRunState,
    AaplOperationalHealth,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
    AaplOperationalStateError,
)
from investment_analyst.application.runtime import ApplicationRuntimeError, StorageLocationRequest
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
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

    def query_aapl_fundamental_trend(
        self,
        request: AaplFundamentalTrendRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalTrend:
        """Query one bounded persisted SEC fundamental trend."""
        ...

    def query_aapl_fundamental_research(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchResult:
        """Calculate bounded point-in-time fundamental research metrics."""
        ...

    def query_aapl_fundamental_research_history(
        self,
        request: AaplFundamentalResearchRequest,
        *,
        location: StorageLocationRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate bounded historical fundamental research statistics."""
        ...


class _WebOperations(Protocol):
    def overview(self) -> dict[str, object]:
        """Return operational and scheduler state."""
        ...

    def report(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one point-in-time report."""
        ...

    def market_chart(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Return one bounded point-in-time market chart."""
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

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one manual refresh."""
        ...


class AaplLocalController:
    """Serialize in-process reads and writes over existing application boundaries."""

    def __init__(
        self,
        runner: _RunnerOperations,
        application: _ApplicationOperations,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> None:
        self._runner = runner
        self._application = application
        self._workspace = workspace
        self._alpaca_credentials = alpaca_credentials
        self._sec_identity = sec_identity
        self._operation_lock = threading.RLock()
        self._market_chart_cache: dict[AaplMarketChartRequest, AaplMarketChart] = {}
        self._fundamental_trend_cache: dict[AaplFundamentalTrendRequest, AaplFundamentalTrend] = {}
        self._fundamental_research_cache: dict[
            AaplFundamentalResearchRequest, AaplFundamentalResearchResult
        ] = {}
        self._fundamental_research_history_cache: dict[
            AaplFundamentalResearchRequest, AaplFundamentalResearchHistoryResult
        ] = {}

    @classmethod
    def create_default(
        cls,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
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
        )

    def health(self) -> AaplOperationalHealth:
        """Inspect workspace and latest run while no in-process write is active."""
        with self._operation_lock:
            return self._runner.inspect(workspace=self._workspace)

    def run_payload(self, payload: dict[str, object]) -> AaplDailyRunState:
        """Validate the stable request snapshot and execute it once."""
        snapshot = AaplDailyRunRequestSnapshot.model_validate(payload)
        return self.run_request(snapshot.to_request())

    def run_request(self, request: AaplWorkspaceBootstrapRequest) -> AaplDailyRunState:
        """Execute a typed manual or scheduled request through one shared mutex."""
        with self._operation_lock:
            try:
                return self._runner.run(
                    request,
                    workspace=self._workspace,
                    alpaca_credentials=self._alpaca_credentials,
                    sec_identity=self._sec_identity,
                )
            finally:
                self._market_chart_cache.clear()
                self._fundamental_trend_cache.clear()
                self._fundamental_research_cache.clear()
                self._fundamental_research_history_cache.clear()

    def report_request(
        self,
        request: ConsolidatedDiagnosticRequest,
    ) -> AaplDailyDiagnosticReport:
        """Query persisted evidence without providers or writes."""
        with self._operation_lock:
            return self._application.query_aapl_diagnostics(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )

    def market_chart_request(self, request: AaplMarketChartRequest) -> AaplMarketChart:
        """Query persisted market bars and indicators without providers or writes."""
        with self._operation_lock:
            cached = self._market_chart_cache.get(request)
            if cached is not None:
                return cached
            chart = self._application.query_aapl_market_chart(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )
            if len(self._market_chart_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._market_chart_cache.pop(next(iter(self._market_chart_cache)))
            self._market_chart_cache[request] = chart
            return chart

    def fundamental_trend_request(
        self,
        request: AaplFundamentalTrendRequest,
    ) -> AaplFundamentalTrend:
        """Query persisted SEC facts without providers, recomputation, or writes."""
        with self._operation_lock:
            cached = self._fundamental_trend_cache.get(request)
            if cached is not None:
                return cached
            trend = self._application.query_aapl_fundamental_trend(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )
            if len(self._fundamental_trend_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_trend_cache.pop(next(iter(self._fundamental_trend_cache)))
            self._fundamental_trend_cache[request] = trend
            return trend

    def fundamental_research_request(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchResult:
        """Calculate cached SEC research metrics without providers or writes."""
        with self._operation_lock:
            cached = self._fundamental_research_cache.get(request)
            if cached is not None:
                return cached
            research = self._application.query_aapl_fundamental_research(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )
            if len(self._fundamental_research_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_research_cache.pop(next(iter(self._fundamental_research_cache)))
            self._fundamental_research_cache[request] = research
            return research

    def fundamental_research_history_request(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchHistoryResult:
        """Calculate cached historical research statistics without writes."""
        with self._operation_lock:
            cached = self._fundamental_research_history_cache.get(request)
            if cached is not None:
                return cached
            history = self._application.query_aapl_fundamental_research_history(
                request,
                location=StorageLocationRequest(workspace=self._workspace),
            )
            if len(self._fundamental_research_history_cache) >= _MAX_READ_CACHE_ENTRIES:
                self._fundamental_research_history_cache.pop(
                    next(iter(self._fundamental_research_history_cache))
                )
            self._fundamental_research_history_cache[request] = history
            return history


class AaplLocalWebApplication:
    """JSON-safe local UI operations over the controller and optional scheduler."""

    def __init__(
        self,
        controller: AaplLocalController,
        scheduler: AaplDailyScheduler | None,
    ) -> None:
        self._controller = controller
        self._scheduler = scheduler

    def overview(self) -> dict[str, object]:
        """Return state only; never initialize, fetch, calculate, or persist."""
        scheduler: dict[str, object] = {"enabled": False}
        if self._scheduler is not None:
            scheduler = self._scheduler.status().to_json_dict()
        return {
            "operational": self._controller.health().to_json_dict(),
            "scheduler": scheduler,
        }

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
        allowed = {"known_at", "period"}
        if set(parameters) - allowed:
            raise ValueError("market chart query contains unsupported parameters")
        known_at = _one_parameter(parameters, "known_at", required=True)
        period = _one_parameter(parameters, "period", required=False)
        request = AaplMarketChartRequest(
            known_at=_aware_datetime(known_at),
            period=period or AaplMarketChartPeriod.SIX_MONTHS,
        )
        return self._controller.market_chart_request(request).to_json_dict()

    def fundamental_trend(self, parameters: Mapping[str, tuple[str, ...]]) -> dict[str, object]:
        """Validate query parameters and return the versioned SEC trend contract."""
        allowed = {"known_at", "frequency"}
        if set(parameters) - allowed:
            raise ValueError("fundamental trend query contains unsupported parameters")
        known_at = _one_parameter(parameters, "known_at", required=True)
        frequency = _frequency(_one_parameter(parameters, "frequency", required=True))
        period_limit = 5 if frequency is DataFrequency.ANNUAL else 8
        request = AaplFundamentalTrendRequest(
            known_at=_aware_datetime(known_at),
            frequency=frequency,
            period_limit=period_limit,
        )
        return self._controller.fundamental_trend_request(request).to_json_dict()

    def fundamental_research(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Validate query parameters and return exact derived SEC metrics."""
        request = _fundamental_research_request(parameters)
        return self._controller.fundamental_research_request(request).to_json_dict()

    def fundamental_research_history(
        self,
        parameters: Mapping[str, tuple[str, ...]],
    ) -> dict[str, object]:
        """Validate query parameters and return historical SEC statistics."""
        request = _fundamental_research_request(parameters)
        return self._controller.fundamental_research_history_request(request).to_json_dict()

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute one explicit request and return bounded operational state."""
        return self._controller.run_payload(payload).to_json_dict()


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
            if parsed.path == "/api/report":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.report(parameters))
                return
            if parsed.path == "/api/market-chart":
                raw = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                parameters = {key: tuple(values) for key, values in raw.items()}
                self._send_json(HTTPStatus.OK, server.application.market_chart(parameters))
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
            raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
        except Exception as error:  # noqa: BLE001
            self._send_mapped_error(error)

    def _dispatch_post(self) -> None:
        try:
            self._require_loopback_host()
            parsed = urlsplit(self.path)
            if parsed.path != "/api/run" or parsed.query:
                raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            payload = self._read_json_object()
            server = cast(AaplLocalHttpServer, self.server)
            self._send_json(HTTPStatus.OK, server.application.run(payload))
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
                AaplDailyReportError,
                AaplFundamentalTrendQueryError,
                AaplMarketChartQueryError,
                ConsolidatedDiagnosticQueryError,
                FundamentalResearchError,
                FundamentalResearchHistoryError,
            ),
        ):
            self._send_error(HTTPStatus.UNPROCESSABLE_ENTITY, "query_failed", str(error)[:500])
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
    allowed = {"known_at", "frequency"}
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
