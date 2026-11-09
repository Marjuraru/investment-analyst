"""HTTP integration coverage for the descriptive outcomes endpoint."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    RSI_INCREMENTAL_LOW_REVIEW_RULE,
    RSI_LOW_REVIEW_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.descriptive_outcomes import DescriptiveConditionOutcomeService
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.application.aapl_daily_runner import AaplDailyRunner
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage.repositories import (
    DuckDBMetricResultRepository,
    DuckDBObservationRepository,
)
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceService

_AAPL = "equity:us:aapl"
_SOURCE = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_PERIOD = datetime(2026, 10, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 10, 20, tzinfo=UTC)
_RULE = RSI_INCREMENTAL_LOW_REVIEW_RULE


def _close(
    day: int,
    value: str,
    *,
    available_at: datetime | None = None,
) -> tuple[RawRecord, NormalizedObservation]:
    observed = _PERIOD + timedelta(days=day)
    available = available_at or observed + timedelta(days=1, hours=1)
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"frontend-outcome-raw:{day}:{value}"),
        asset_id=_AAPL,
        source=SourceReference(
            source_id=_SOURCE,
            record_key=f"{observed.isoformat()}:{value}",
            retrieved_at=available,
        ),
        event_time=observed,
        available_at=available,
        received_at=available,
        payload={"close": value},
        schema_version="descriptive-outcome-frontend-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"frontend-outcome-close:{raw.record_id}"),
        raw_record_id=raw.record_id,
        asset_id=_AAPL,
        field_name="close",
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=observed,
        available_at=available,
        normalized_at=available + timedelta(minutes=1),
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="descriptive-outcome-frontend-v1",
    )
    return raw, observation


def _metric(value: str = "25", *, input_observation_id: UUID) -> MetricResult:
    parameters = {"source_id": _SOURCE, "window": 14}
    inputs = [input_observation_id]
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=_AAPL,
            metric_key="market.technical.rsi",
            input_observation_ids=inputs,
            algorithm_version="market-rsi-incremental-v2-decimal34",
            as_of=_PERIOD,
            available_at=_PERIOD + timedelta(days=1),
            unit="index",
            quality=DataQuality.PARTIAL,
            parameters=parameters,
        ),
        asset_id=_AAPL,
        metric_key="market.technical.rsi",
        value=Decimal(value),
        unit="index",
        as_of=_PERIOD,
        available_at=_PERIOD + timedelta(days=1),
        computed_at=_PERIOD + timedelta(days=1, hours=2),
        parameters=parameters,
        input_observation_ids=inputs,
        algorithm_version="market-rsi-incremental-v2-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _seed(root: Path) -> tuple[WorkspaceService, WorkspacePaths, ApplicationRuntime]:
    service = WorkspaceService(environ={}, home=root / "home")
    paths = service.initialize(root / "workspace", format_version=2).paths
    storage = service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        condition_raw, condition_observation = _close(
            0,
            "98",
            available_at=_PERIOD + timedelta(hours=1),
        )
        storage.raw_records.save(condition_raw)
        storage.observations.save(condition_observation)
        for day in range(1, 7):
            raw, observation = _close(
                day,
                "110" if day == 6 else str(99 + day),
            )
            storage.raw_records.save(raw)
            storage.observations.save(observation)
        storage.metric_results.save(
            _metric("25", input_observation_id=condition_observation.observation_id)
        )
    finally:
        storage.close()
    return service, paths, ApplicationRuntime.create_default(workspace_service=service)


def _application(
    service: WorkspaceService,
    paths: WorkspacePaths,
    runtime: ApplicationRuntime,
) -> AaplLocalWebApplication:
    application = InvestmentAnalystApplication(runtime)
    controller = AaplLocalController(
        AaplDailyRunner(application, runtime.workspace_service),
        application,
        workspace=paths.root,
        alpaca_credentials=AlpacaCredentials(api_key="unused", secret_key="unused"),
        sec_identity=SecEdgarIdentity("Investment Analyst descriptive-outcomes@example.invalid"),
    )
    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    return AaplLocalWebApplication(controller, None, analytical_rule_store=registry)


@contextmanager
def _server(
    application: AaplLocalWebApplication,
) -> Iterator[tuple[AaplLocalHttpServer, str]]:
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
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())
    with response:
        return response.status, json.loads(response.read()), dict(response.headers.items())


def _params(**overrides: str) -> str:
    values = {
        "asset_id": _AAPL,
        "rule_id": _RULE.rule_id,
        "expected_rule_fingerprint": _RULE.semantic_fingerprint(),
        "start_date": "2026-10-01",
        "end_date": "2026-10-01",
        "known_at": "2026-10-20T00:00:00Z",
    }
    values.update(overrides)
    return urlencode(values)


def test_server_serves_one_exact_report_and_reuses_warm_composite_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, paths, runtime = _seed(tmp_path)
    application = _application(service, paths, runtime)
    run_calls: list[object] = []
    original_run = DescriptiveConditionOutcomeService.run

    def tracked_run(self, request):
        run_calls.append(request)
        return original_run(self, request)

    monkeypatch.setattr(DescriptiveConditionOutcomeService, "run", tracked_run)
    with _server(application) as (_, root):
        first_status, first, first_headers = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params()}")
        )
        second_status, second, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params()}")
        )

    assert first_status == second_status == 200
    assert first == second
    assert len(run_calls) == 1
    assert first["schema_version"] == "descriptive-condition-outcomes-v1"
    assert first["request"]["start_date"] == "2026-10-01"
    assert first["request"]["end_date"] == "2026-10-01"
    assert first["observed_periods"] == 1
    assert first["matched_count"] == 1
    assert first["rows"][0]["outcome"]["status"] == "available"
    assert first["rows"][0]["outcome"]["return_5_intervals"] == "0.1"
    assert len(first["rows"][0]["outcome"]["closes"]) == 6
    assert len(first["groups"]) == 3
    assert first_headers["Content-Type"].startswith("application/json")
    assert len(json.dumps(first, ensure_ascii=False).encode("utf-8")) <= 262_144


def test_server_maps_invalid_fingerprint_budget_and_unsupported_targets(
    tmp_path: Path,
) -> None:
    service, paths, runtime = _seed(tmp_path)
    application = _application(service, paths, runtime)
    with _server(application) as (_, root):
        invalid_status, invalid, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params(start_date='2025-01-01')}")
        )
        assert invalid_status == 400
        assert invalid["error"]["code"] == "invalid_request"

        stale_status, stale, _ = _json_request(
            Request(
                f"{root}/api/v1/screening/outcomes?{_params(expected_rule_fingerprint='0' * 64)}"
            )
        )
        assert stale_status == 409
        assert stale["error"]["code"] == "screening_outcome_fingerprint_changed"

        legacy_status, legacy, _ = _json_request(
            Request(
                f"{root}/api/v1/screening/outcomes?{_params(rule_id=RSI_LOW_REVIEW_RULE.rule_id)}"
            )
        )
        assert legacy_status == 422
        assert legacy["error"]["code"] == "screening_outcome_unsupported"

        asset_status, asset, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params(asset_id='equity:us:unknown')}")
        )
        assert asset_status == 422
        assert asset["error"]["code"] == "screening_outcome_unsupported"

        extra_status, extra, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params(extra='x')}")
        )
        assert extra_status == 400
        assert extra["error"]["code"] == "invalid_request"


def test_server_returns_explicit_empty_report_without_an_error(
    tmp_path: Path,
) -> None:
    service, paths, runtime = _seed(tmp_path)
    application = _application(service, paths, runtime)
    with _server(application) as (_, root):
        empty_params = _params(
            start_date="2025-01-01",
            end_date="2025-01-01",
            known_at="2025-02-01T00:00:00Z",
        )
        status, payload, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{empty_params}")
        )
    assert status == 200
    assert payload["observed_periods"] == 0
    assert payload["rows"] == []
    assert payload["matched_count"] == 0
    assert payload["not_matched_count"] == 0
    assert payload["not_evaluable_count"] == 0
    assert all(group["denominator"] == 0 for group in payload["groups"])
    assert all(group["mean_return_5_intervals"] is None for group in payload["groups"])


def test_server_budget_and_read_only_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, paths, runtime = _seed(tmp_path)
    application = _application(service, paths, runtime)

    def forbidden(*args, **kwargs):
        raise AssertionError("the descriptive outcome endpoint must not write domain data")

    monkeypatch.setattr(DuckDBObservationRepository, "save", forbidden)
    monkeypatch.setattr(DuckDBObservationRepository, "save_many", forbidden)
    monkeypatch.setattr(DuckDBMetricResultRepository, "save", forbidden)
    monkeypatch.setattr(DuckDBMetricResultRepository, "save_many", forbidden)
    with _server(application) as (_, root):
        status, payload, _ = _json_request(Request(f"{root}/api/v1/screening/outcomes?{_params()}"))
    assert status == 200
    assert payload["matched_count"] == 1

    monkeypatch.setattr(
        "investment_analyst.alerts.descriptive_outcomes._MAX_OBSERVATIONS",
        1,
    )
    budget_application = _application(service, paths, runtime)
    with _server(budget_application) as (_, root):
        budget_status, budget, _ = _json_request(
            Request(f"{root}/api/v1/screening/outcomes?{_params(known_at='2026-10-21T00:00:00Z')}")
        )
    assert budget_status == 409
    assert budget["error"]["code"] == "screening_outcome_budget_exceeded"
