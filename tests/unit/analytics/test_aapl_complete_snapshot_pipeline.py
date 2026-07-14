"""Unit tests for ordered Apple snapshot orchestration."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.aapl_snapshot_models import AaplCompleteSnapshotRequest
from investment_analyst.analytics.aapl_snapshot_pipeline import (
    AaplCompleteSnapshotPipeline,
    AaplSnapshotIncompleteError,
    AaplSnapshotStageError,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticSection,
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
    ConsolidatedTemporalContext,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRunSummary
from investment_analyst.analytics.market.diagnostic_rules import ALGORITHM_VERSION
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRunSummary
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
)
from investment_analyst.providers.market.alpaca_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import AlpacaImportSummary

KNOWN_AT = datetime(2026, 7, 14, tzinfo=UTC)
START = datetime(2026, 6, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)


class FakeStorage:
    """Minimal open-storage double required by the orchestrator."""

    def __init__(self, open_: bool = True) -> None:
        self.open_ = open_

    def require_open(self) -> None:
        if not self.open_:
            raise RuntimeError("storage closed")


class SpyPipeline:
    """Single-run pipeline double recording ordered calls."""

    def __init__(self, name: str, result: object, calls: list[str], error: Exception | None = None):
        self.name = name
        self.result = result
        self.calls = calls
        self.error = error
        self.arguments: list[tuple[object, ...]] = []

    def run(self, *arguments: object):
        self.calls.append(self.name)
        self.arguments.append(arguments)
        if self.error is not None:
            raise self.error
        return self.result


class SpyConsolidated:
    """Read-only consolidated service double."""

    def __init__(self, view: ConsolidatedDiagnosticView, calls: list[str]) -> None:
        self.view = view
        self.calls = calls
        self.requests: list[ConsolidatedDiagnosticRequest] = []

    def query(self, request: ConsolidatedDiagnosticRequest) -> ConsolidatedDiagnosticView:
        self.calls.append("consolidated_query")
        self.requests.append(request)
        return self.view


def _request(require_complete: bool = False) -> AaplCompleteSnapshotRequest:
    return AaplCompleteSnapshotRequest(
        known_at=KNOWN_AT,
        market_start=START.date(),
        market_end=END.date(),
        fundamental_frequency=DataFrequency.QUARTERLY,
        market_as_of=date(2026, 6, 30),
        fundamental_as_of=date(2026, 3, 31),
        require_complete=require_complete,
    )


def _diagnostic(mode: DiagnosticMode, algorithm: str, as_of: datetime) -> DiagnosticResult:
    return DiagnosticResult(
        asset_id=ASSET_ID,
        mode=mode,
        verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
        final_score=Decimal("0"),
        confidence=Decimal("0"),
        as_of=as_of,
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, tzinfo=UTC),
        components=[],
        evidence=[],
        algorithm_version=algorithm,
        summary="Descriptive diagnostic unavailable from the current local inputs.",
        quality=DataQuality.PARTIAL,
    )


def _view(status: ConsolidatedDiagnosticStatus) -> ConsolidatedDiagnosticView:
    request = ConsolidatedDiagnosticRequest(
        known_at=KNOWN_AT,
        fundamental_frequency=DataFrequency.QUARTERLY,
        market_as_of=date(2026, 6, 30),
        fundamental_as_of=date(2026, 3, 31),
    )
    market_available = status in {
        ConsolidatedDiagnosticStatus.COMPLETE,
        ConsolidatedDiagnosticStatus.PARTIAL,
    }
    fundamental_available = status is ConsolidatedDiagnosticStatus.COMPLETE
    market = ConsolidatedDiagnosticSection(
        status=(
            ConsolidatedSectionStatus.AVAILABLE
            if market_available
            else ConsolidatedSectionStatus.NOT_FOUND
        ),
        mode=DiagnosticMode.MARKET,
        diagnostic=(
            _diagnostic(DiagnosticMode.MARKET, ALGORITHM_VERSION, datetime(2026, 6, 30, tzinfo=UTC))
            if market_available
            else None
        ),
        not_found_reason=None if market_available else "market unavailable",
        candidates_examined=1 if market_available else 0,
        candidates_eligible=1 if market_available else 0,
    )
    fundamental = ConsolidatedDiagnosticSection(
        status=(
            ConsolidatedSectionStatus.AVAILABLE
            if fundamental_available
            else ConsolidatedSectionStatus.NOT_FOUND
        ),
        mode=DiagnosticMode.FUNDAMENTAL,
        diagnostic=(
            _diagnostic(
                DiagnosticMode.FUNDAMENTAL,
                FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
                datetime(2026, 3, 31, tzinfo=UTC),
            )
            if fundamental_available
            else None
        ),
        not_found_reason=None if fundamental_available else "fundamental unavailable",
        candidates_examined=1 if fundamental_available else 0,
        candidates_eligible=1 if fundamental_available else 0,
    )
    return ConsolidatedDiagnosticView(
        request=request,
        status=status,
        market=market,
        fundamental=fundamental,
        temporal_context=ConsolidatedTemporalContext(
            market_as_of=market.diagnostic.as_of if market.diagnostic else None,
            fundamental_as_of=(fundamental.diagnostic.as_of if fundamental.diagnostic else None),
            reference_gap_days=(
                91 if market.diagnostic is not None and fundamental.diagnostic is not None else None
            ),
            market_available_at=(market.diagnostic.available_at if market.diagnostic else None),
            fundamental_available_at=(
                fundamental.diagnostic.available_at if fundamental.diagnostic else None
            ),
        ),
        diagnostics_examined=int(market_available) + int(fundamental_available),
        metric_results_examined=0,
        ignored_algorithm_versions=0,
        traceability_verified=True,
    )


def _market_summary() -> AlpacaImportSummary:
    return AlpacaImportSummary(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        requested_start=START,
        requested_end=END,
        retrieved_at=datetime(2026, 7, 2, tzinfo=UTC),
        feed="iex",
        adjustment="all",
        request_count=1,
        bars_received=25,
        raw_records_created=25,
        raw_records_reused=0,
        observations_created=175,
        observations_reused=0,
        earliest_bar=START,
        latest_bar=datetime(2026, 6, 25, tzinfo=UTC),
        traceability_verified=True,
    )


def _statistics_summary() -> MarketStatisticsRunSummary:
    return MarketStatisticsRunSummary(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        requested_start=START,
        requested_end=END,
        known_at=KNOWN_AT,
        computed_at=datetime(2026, 7, 3, tzinfo=UTC),
        bar_count=25,
        definitions_upserted=4,
        results_generated=61,
        results_created=61,
        results_reused=0,
        result_counts={"market.history.simple_return_1d": 24},
        warmup_counts={"market.history.simple_return_1d": 1},
        zero_denominator_skips={"market.history.relative_volume": 0},
        earliest_as_of=datetime(2026, 6, 2, tzinfo=UTC),
        latest_as_of=datetime(2026, 6, 25, tzinfo=UTC),
        traceability_verified=True,
    )


def _diagnostic_summary() -> MarketDiagnosticRunSummary:
    return MarketDiagnosticRunSummary(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        requested_start=START,
        requested_end=END,
        known_at=KNOWN_AT,
        as_of=datetime(2026, 6, 25, tzinfo=UTC),
        computed_at=datetime(2026, 7, 3, tzinfo=UTC),
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("70"),
        confidence=Decimal("0.7"),
        quality=DataQuality.PARTIAL,
        selected_metric_result_ids=tuple(uuid4() for _ in range(5)),
        missing_requirements=(),
        diagnostics_generated=1,
        diagnostics_created=1,
        diagnostics_reused=0,
        traceability_verified=True,
    )


def _pipeline(status: ConsolidatedDiagnosticStatus, calls: list[str]):
    market = SpyPipeline("market_fetch", _market_summary(), calls)
    statistics = SpyPipeline("market_statistics", _statistics_summary(), calls)
    diagnostic = SpyPipeline("market_diagnostic", _diagnostic_summary(), calls)
    consolidated = SpyConsolidated(_view(status), calls)
    pipeline = AaplCompleteSnapshotPipeline(
        FakeStorage(),
        market,
        statistics,
        diagnostic,
        consolidated,
        clock=lambda: datetime(2026, 7, 14, 1, tzinfo=UTC),
    )
    return pipeline, market, statistics, diagnostic, consolidated


def test_pipeline_calls_each_existing_layer_once_in_order() -> None:
    calls: list[str] = []
    pipeline, market, statistics, diagnostic, consolidated = _pipeline(
        ConsolidatedDiagnosticStatus.COMPLETE,
        calls,
    )
    summary = pipeline.run(_request())

    assert calls == [
        "market_fetch",
        "market_statistics",
        "market_diagnostic",
        "consolidated_query",
    ]
    assert len(market.arguments) == len(statistics.arguments) == len(diagnostic.arguments) == 1
    assert len(consolidated.requests) == 1
    assert market.arguments[0] == (START, END)
    statistics_request = statistics.arguments[0][0]
    assert statistics_request.query.known_at == KNOWN_AT
    assert diagnostic.arguments[0][0].query == statistics_request.query
    assert consolidated.requests[0].fundamental_frequency is DataFrequency.QUARTERLY
    assert summary.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert summary.fundamental_refresh.status.value == "skipped"
    assert "combined_score" not in summary.to_json_dict()


@pytest.mark.parametrize(
    "status",
    [ConsolidatedDiagnosticStatus.PARTIAL, ConsolidatedDiagnosticStatus.UNAVAILABLE],
)
def test_non_complete_status_is_valid_unless_required(
    status: ConsolidatedDiagnosticStatus,
) -> None:
    pipeline, *_ = _pipeline(status, [])
    assert pipeline.run(_request()).overall_status is status

    required, *_ = _pipeline(status, [])
    with pytest.raises(AaplSnapshotIncompleteError) as captured:
        required.run(_request(require_complete=True))
    assert captured.value.summary.overall_status is status


def test_stage_failure_stops_later_stages_without_retry() -> None:
    calls: list[str] = []
    pipeline, market, statistics, diagnostic, consolidated = _pipeline(
        ConsolidatedDiagnosticStatus.COMPLETE,
        calls,
    )
    statistics.error = RuntimeError("statistics unavailable")

    with pytest.raises(AaplSnapshotStageError, match="market_statistics"):
        pipeline.run(_request())
    assert calls == ["market_fetch", "market_statistics"]
    assert len(market.arguments) == 1
    assert len(diagnostic.arguments) == 0
    assert len(consolidated.requests) == 0
