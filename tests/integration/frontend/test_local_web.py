"""Socket-level tests for the loopback-only local web interface."""

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
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
from investment_analyst.application.aapl_bootstrap_models import AaplWorkspaceBootstrapRequest
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplOperationalHealth,
)
from investment_analyst.application.runtime import StorageLocationRequest
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


class _ExplodingApplication:
    def overview(self) -> dict[str, object]:
        raise RuntimeError("unexpected SECRET detail")

    def report(self, parameters: dict[str, tuple[str, ...]]) -> dict[str, object]:
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

    assert '<html lang="es">' in html
    assert "Saltar al contenido principal" in html
    assert "No se genera una recomendación" in html
    assert "Ver contrato técnico JSON sin redondear" in html
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

    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert duplicated_status == 400
    assert duplicated["error"]["code"] == "invalid_request"
    assert malformed_status == 400
    assert malformed["error"]["code"] == "invalid_json"
    assert runner.requests == []


def test_local_server_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AaplLocalHttpServer(
            ("0.0.0.0", 0),
            cast(AaplLocalWebApplication, _ExplodingApplication()),
        )
