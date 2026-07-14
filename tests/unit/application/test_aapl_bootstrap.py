"""Unit tests for Apple workspace bootstrap orchestration and time safety."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticSection,
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
    ConsolidatedTemporalContext,
)
from investment_analyst.application.aapl_bootstrap import (
    AaplWorkspaceBootstrapPipeline,
    BootstrapIncompleteError,
    BootstrapKnownAtTooEarlyError,
    BootstrapStageError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplBootstrapStage,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.core.models import DataFrequency, DiagnosticMode
from investment_analyst.providers.fundamentals.sec_fact_models import (
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID

INGESTED_AT = datetime(2026, 7, 14, 10, tzinfo=UTC)
EFFECTIVE = datetime(2026, 7, 14, 11, tzinfo=UTC)
COMPUTED = datetime(2026, 7, 14, 12, tzinfo=UTC)


class ObservationRepositoryDouble:
    """Return one market and one fundamental observation without persistence."""

    def __init__(self) -> None:
        self.calls = 0

    def list(self, *, asset_id: str):
        self.calls += 1
        assert asset_id == "equity:us:aapl"
        return [
            SimpleNamespace(
                source=SimpleNamespace(source_id=SOURCE_ID),
                frequency=DataFrequency.DAY_1,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                available_at=INGESTED_AT,
                transformation_version="alpaca-iex-bar-normalizer-v1",
            ),
            SimpleNamespace(
                source=SimpleNamespace(source_id=COMPANYFACTS_SOURCE_ID),
                frequency=DataFrequency.QUARTERLY,
                observed_at=datetime(2026, 3, 31, tzinfo=UTC),
                available_at=datetime(2026, 4, 30, 21, tzinfo=UTC),
                transformation_version=TRANSFORMATION_VERSION,
            ),
        ]


class StorageDouble:
    """Minimal opened storage surface used by the orchestrator."""

    def __init__(self) -> None:
        self.observations = ObservationRepositoryDouble()
        self.require_open_calls = 0

    def require_open(self) -> None:
        self.require_open_calls += 1


class StageDouble:
    """One existing pipeline spy returning a request-aware summary."""

    def __init__(self, name: str, calls: list[str], result_factory) -> None:
        self.name = name
        self.calls = calls
        self.result_factory = result_factory
        self.arguments: list[object] = []

    def run(self, *arguments):
        self.calls.append(self.name)
        self.arguments.append(arguments[0] if len(arguments) == 1 else arguments)
        return self.result_factory(*arguments)


class ConsolidatedDouble:
    """Return one deterministic unavailable or complete consolidated view."""

    def __init__(self, calls: list[str], *, complete: bool = False) -> None:
        self.calls = calls
        self.complete = complete
        self.requests = []

    def query(self, request):
        self.calls.append("consolidated_query")
        self.requests.append(request)
        if self.complete:
            raise AssertionError("complete view is not required by these unit doubles")
        return ConsolidatedDiagnosticView(
            request=request,
            status=ConsolidatedDiagnosticStatus.UNAVAILABLE,
            market=ConsolidatedDiagnosticSection(
                status=ConsolidatedSectionStatus.NOT_FOUND,
                mode=DiagnosticMode.MARKET,
                not_found_reason="market diagnostic not found",
            ),
            fundamental=ConsolidatedDiagnosticSection(
                status=ConsolidatedSectionStatus.NOT_FOUND,
                mode=DiagnosticMode.FUNDAMENTAL,
                not_found_reason="fundamental diagnostic not found",
            ),
            temporal_context=ConsolidatedTemporalContext(),
            diagnostics_examined=0,
            metric_results_examined=0,
            ignored_algorithm_versions=0,
            traceability_verified=True,
        )


def _request(**updates: object) -> AaplWorkspaceBootstrapRequest:
    values: dict[str, object] = {
        "market_start": date(2026, 1, 1),
        "market_end": date(2026, 1, 26),
        "fundamental_frequency": DataFrequency.QUARTERLY,
        "requested_known_at": None,
        "require_complete": False,
    }
    values.update(updates)
    return AaplWorkspaceBootstrapRequest(**values)


def _pipeline(*, clock, consolidated=None):
    calls: list[str] = []
    storage = StorageDouble()
    sec_fetch = StageDouble(
        "sec_fetch",
        calls,
        lambda: SimpleNamespace(
            asset_id="equity:us:aapl",
            retrieved_at=INGESTED_AT,
            documents_received=2,
            raw_records_created=2,
            raw_records_reused=0,
            traceability_verified=True,
        ),
    )
    sec_normalization = StageDouble(
        "sec_normalization",
        calls,
        lambda: SimpleNamespace(
            asset_id="equity:us:aapl",
            normalized_at=INGESTED_AT,
            observations_generated=5,
            observations_created=5,
            observations_reused=0,
            traceability_verified=True,
        ),
    )
    market_fetch = StageDouble(
        "market_fetch",
        calls,
        lambda start, end: SimpleNamespace(
            asset_id="equity:us:aapl",
            source_id=SOURCE_ID,
            feed="iex",
            retrieved_at=INGESTED_AT,
            raw_records_created=25,
            raw_records_reused=0,
            observations_created=175,
            observations_reused=0,
            traceability_verified=True,
        ),
    )
    fundamental_metrics = StageDouble(
        "fundamental_metrics",
        calls,
        lambda request: SimpleNamespace(
            known_at=request.known_at,
            computed_at=COMPUTED,
            metrics_generated=3,
            metrics_created=3,
            metrics_reused=0,
        ),
    )
    fundamental_diagnostic = StageDouble(
        "fundamental_diagnostic",
        calls,
        lambda request: SimpleNamespace(
            known_at=request.known_at,
            computed_at=COMPUTED,
            diagnostics_generated=1,
            diagnostics_created=1,
            diagnostics_reused=0,
        ),
    )
    market_statistics = StageDouble(
        "market_statistics",
        calls,
        lambda request: SimpleNamespace(
            known_at=request.query.known_at,
            computed_at=COMPUTED,
            results_generated=20,
            results_created=20,
            results_reused=0,
        ),
    )
    market_diagnostic = StageDouble(
        "market_diagnostic",
        calls,
        lambda request: SimpleNamespace(
            known_at=request.query.known_at,
            computed_at=COMPUTED,
            diagnostics_generated=1,
            diagnostics_created=1,
            diagnostics_reused=0,
        ),
    )
    consolidated_service = consolidated or ConsolidatedDouble(calls)
    pipeline = AaplWorkspaceBootstrapPipeline(
        storage,
        workspace_id=uuid4(),
        sec_fetch_pipeline=sec_fetch,
        sec_observation_pipeline=sec_normalization,
        market_pipeline=market_fetch,
        fundamental_metric_pipeline=fundamental_metrics,
        fundamental_diagnostic_pipeline=fundamental_diagnostic,
        market_statistics_pipeline=market_statistics,
        market_diagnostic_pipeline=market_diagnostic,
        consolidated_service=consolidated_service,
        clock=clock,
    )
    return (
        pipeline,
        storage,
        calls,
        (
            fundamental_metrics,
            fundamental_diagnostic,
            market_statistics,
            market_diagnostic,
            consolidated_service,
        ),
    )


def test_automatic_known_at_is_captured_once_after_ingestion_and_propagated() -> None:
    clock_calls: list[str] = []

    def clock() -> datetime:
        clock_calls.append("clock")
        return EFFECTIVE

    pipeline, storage, calls, analytics = _pipeline(clock=clock)
    summary = pipeline.run(_request())

    assert clock_calls == ["clock"]
    assert calls == [
        "sec_fetch",
        "sec_normalization",
        "market_fetch",
        "fundamental_metrics",
        "fundamental_diagnostic",
        "market_statistics",
        "market_diagnostic",
        "consolidated_query",
    ]
    assert summary.effective_known_at == EFFECTIVE
    assert storage.observations.calls == 1
    assert analytics[0].arguments[0].known_at == EFFECTIVE
    assert analytics[1].arguments[0].known_at == EFFECTIVE
    assert analytics[2].arguments[0].query.known_at == EFFECTIVE
    assert analytics[3].arguments[0].query.known_at == EFFECTIVE
    assert analytics[4].requests[0].known_at == EFFECTIVE


def test_explicit_known_at_too_early_stops_before_metrics_or_diagnostics() -> None:
    pipeline, storage, calls, analytics = _pipeline(clock=lambda: EFFECTIVE)
    requested = INGESTED_AT.replace(hour=9)
    with pytest.raises(BootstrapKnownAtTooEarlyError) as captured:
        pipeline.run(_request(requested_known_at=requested))

    assert captured.value.stage is AaplBootstrapStage.MARKET_FETCH
    assert captured.value.minimum_known_at == INGESTED_AT
    assert calls == ["sec_fetch", "sec_normalization", "market_fetch"]
    assert storage.observations.calls == 1
    assert all(not item.arguments for item in analytics[:4])
    assert analytics[4].requests == []


def test_failures_stop_later_stages_and_require_complete_is_enforced() -> None:
    pipeline, _storage, calls, analytics = _pipeline(clock=lambda: EFFECTIVE)
    analytics[0].result_factory = lambda _request: (_ for _ in ()).throw(
        RuntimeError("metric failure")
    )
    with pytest.raises(BootstrapStageError) as captured:
        pipeline.run(_request())
    assert captured.value.stage is AaplBootstrapStage.FUNDAMENTAL_METRICS
    assert calls[-1] == "fundamental_metrics"
    assert not analytics[1].arguments

    pipeline, _storage, _calls, _analytics = _pipeline(clock=lambda: EFFECTIVE)
    with pytest.raises(BootstrapIncompleteError):
        pipeline.run(_request(require_complete=True))
