"""Offline integration of the complete Apple market snapshot orchestrator."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.aapl_snapshot_models import AaplCompleteSnapshotRequest
from investment_analyst.analytics.aapl_snapshot_pipeline import AaplCompleteSnapshotPipeline
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths

START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 1, 26)
FETCHED_AT = datetime(2026, 1, 27, 12, tzinfo=UTC)
KNOWN_AT = datetime(2026, 1, 29, 12, tzinfo=UTC)
COMPUTED_AT = datetime(2026, 1, 28, 12, tzinfo=UTC)


class FixtureTransport:
    """One-page offline Alpaca Market Data transport."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        assert headers["APCA-API-KEY-ID"] == "test-key"
        assert headers["APCA-API-SECRET-KEY"] == "test-secret"
        assert timeout_seconds > 0
        return HttpResponse(status_code=200, body=self.body, headers={}, url=url)


def _bars_body() -> bytes:
    bars = []
    for offset in range(25):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)
        open_value = 100 + offset
        bars.append(
            {
                "t": timestamp.isoformat().replace("+00:00", "Z"),
                "o": open_value,
                "h": open_value + 3,
                "l": open_value - 1,
                "c": open_value + 2,
                "v": 1_000_000 + offset * 10_000,
                "n": 10_000 + offset,
                "vw": open_value + 1,
            }
        )
    return json.dumps({"bars": bars, "symbol": "AAPL", "next_page_token": None}).encode()


def _persist_fundamental_diagnostic(storage: LocalStorage) -> DiagnosticResult:
    definition = next(
        item
        for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
        if item.metric_name == "fundamental.net_margin"
    )
    metric = MetricResult(
        asset_id="equity:us:aapl",
        metric_key=definition.metric_name,
        value=Decimal("0.20"),
        unit="ratio",
        as_of=datetime(2025, 12, 31, tzinfo=UTC),
        available_at=datetime(2026, 1, 20, tzinfo=UTC),
        computed_at=datetime(2026, 1, 21, tzinfo=UTC),
        parameters={"frequency": "quarterly"},
        input_observation_ids=[uuid4()],
        algorithm_version=definition.algorithm_version,
        quality=DataQuality.VALID,
    )
    storage.metric_results.save(metric)
    diagnostic = DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.FUNDAMENTAL,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("70"),
        confidence=Decimal("0.8"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=metric.computed_at,
        components=[
            DiagnosticComponent(
                component_key="fundamental_test",
                score=Decimal("70"),
                weight=Decimal("1"),
                weighted_contribution=Decimal("70"),
                metric_result_ids=[metric.result_id],
                explanation="Independent local fundamental component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.4"),
                reason="Independent local fundamental evidence.",
            )
        ],
        algorithm_version=FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
        summary="Descriptive local fundamental diagnostic for Apple.",
        quality=DataQuality.VALID,
    )
    storage.diagnostics.save(diagnostic)
    return diagnostic


def _pipeline(storage: LocalStorage, transport: FixtureTransport) -> AaplCompleteSnapshotPipeline:
    client = AlpacaStockClient(
        transport,
        AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        clock=lambda: FETCHED_AT,
    )
    market = AlpacaHistoricalPipeline(
        storage,
        client,
        clock=lambda: FETCHED_AT + timedelta(minutes=1),
    )
    history = HistoricalMarketDataService(storage)
    statistics = MarketStatisticsPipeline(
        storage,
        history,
        MarketStatisticsEngine(),
        clock=lambda: COMPUTED_AT,
    )
    diagnostic = MarketDiagnosticPipeline(
        storage,
        MarketDiagnosticMetricSelector(storage),
        MarketDiagnosticEngine(),
        clock=lambda: COMPUTED_AT + timedelta(minutes=1),
    )
    return AaplCompleteSnapshotPipeline(
        storage,
        market,
        statistics,
        diagnostic,
        AaplConsolidatedDiagnosticService(storage),
        clock=lambda: COMPUTED_AT + timedelta(minutes=2),
    )


def _counts(storage: LocalStorage) -> tuple[int, int, int, int]:
    return (
        len(storage.raw_records.list()),
        len(storage.observations.list()),
        len(storage.metric_results.list()),
        len(storage.diagnostics.list()),
    )


def test_complete_snapshot_is_idempotent_and_keeps_modes_separate(tmp_path) -> None:
    request = AaplCompleteSnapshotRequest(
        known_at=KNOWN_AT,
        market_start=START_DATE,
        market_end=END_DATE,
        fundamental_frequency=DataFrequency.QUARTERLY,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        fundamental = _persist_fundamental_diagnostic(storage)
        transport = FixtureTransport(_bars_body())
        first = _pipeline(storage, transport).run(request)
        counts_after_first = _counts(storage)
        fundamental_after_first = storage.diagnostics.get(fundamental.diagnostic_id)

        second = _pipeline(storage, transport).run(request)
        counts_after_second = _counts(storage)

    assert len(transport.calls) == 2
    assert first.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert first.consolidated.details.view.market.diagnostic is not None
    assert first.consolidated.details.view.fundamental.diagnostic is not None
    assert first.market_fetch.records_created == 200
    assert second.market_fetch.status.value == "reused"
    assert second.market_statistics.status.value == "reused"
    assert second.market_diagnostic.status.value == "reused"
    assert counts_after_first == counts_after_second
    assert fundamental_after_first == fundamental
    payload = first.to_json_dict()
    assert payload["feed"] == "iex"
    assert payload["consolidated"]["status"] == "complete"
    assert "combined_score" not in json.dumps(payload)
    assert first.traceability_verified is True
