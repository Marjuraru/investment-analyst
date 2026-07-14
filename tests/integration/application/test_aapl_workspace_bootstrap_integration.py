"""Offline end-to-end integration for the persistent Apple workspace bootstrap."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.diagnostic_pipeline import (
    MarketDiagnosticPipeline,
)
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
)
from investment_analyst.application.aapl_bootstrap import (
    AaplWorkspaceBootstrapPipeline,
    BootstrapKnownAtTooEarlyError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.core.models import DataFrequency, DiagnosticVerdict
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

FIXTURE_DIR = Path("tests/fixtures/sec")
SEC_RETRIEVED = datetime(2026, 7, 14, 10, tzinfo=UTC)
NORMALIZED = datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
MARKET_RETRIEVED = datetime(2026, 7, 14, 10, 2, tzinfo=UTC)
EFFECTIVE = datetime(2026, 7, 14, 10, 4, tzinfo=UTC)
COMPUTED = datetime(2026, 7, 14, 10, 5, tzinfo=UTC)


class FixtureTransport:
    """Route official SEC and Alpaca URLs to deterministic local fixture bytes."""

    def __init__(self, submissions: bytes, companyfacts: bytes, bars: bytes) -> None:
        self.submissions = submissions
        self.companyfacts = companyfacts
        self.bars = bars
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        assert timeout_seconds > 0
        if "/submissions/" in url:
            body = self.submissions
        elif "/companyfacts/" in url:
            body = self.companyfacts
        else:
            assert "data.alpaca.markets" in url
            assert headers["APCA-API-KEY-ID"] == "test-key"
            assert headers["APCA-API-SECRET-KEY"] == "test-secret"
            body = self.bars
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / name).read_text(encoding="utf-8"),
        parse_int=str,
        parse_float=str,
    )


def _sec_documents() -> tuple[bytes, bytes]:
    submissions = _load("aapl_submissions.json")
    recent = submissions["filings"]["recent"]
    recent["acceptanceDateTime"] = [
        "2026-01-30T21:00:00Z",
        "2026-04-30T21:00:00Z",
    ]
    recent["primaryDocument"] = [
        "aapl-20251231x10k.htm",
        "aapl-20260331x10q.htm",
    ]

    def duration(annual: bool, value: str) -> dict[str, str]:
        return {
            "start": "2025-01-01" if annual else "2026-01-01",
            "end": "2025-12-31" if annual else "2026-03-31",
            "val": value,
            "accn": "0000320193-26-000001" if annual else "0000320193-26-000002",
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-01-30" if annual else "2026-04-30",
        }

    def instant(annual: bool, value: str) -> dict[str, str]:
        result = duration(annual, value)
        del result["start"]
        return result

    companyfacts = _load("aapl_companyfacts.json")
    companyfacts["facts"] = {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [duration(True, "1000"), duration(False, "260")]}
            },
            "NetIncomeLoss": {"units": {"USD": [duration(True, "200"), duration(False, "52")]}},
            "Assets": {"units": {"USD": [instant(True, "5000"), instant(False, "5100")]}},
            "Liabilities": {"units": {"USD": [instant(True, "3000"), instant(False, "3050")]}},
            "StockholdersEquity": {
                "units": {"USD": [instant(True, "2000"), instant(False, "2050")]}
            },
        },
        "dei": companyfacts["facts"]["dei"],
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _bars() -> bytes:
    bars = []
    for offset in range(25):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)
        value = 100 + offset
        bars.append(
            {
                "t": timestamp.isoformat().replace("+00:00", "Z"),
                "o": value,
                "h": value + 3,
                "l": value - 1,
                "c": value + 2,
                "v": 1_000_000 + offset * 10_000,
                "n": 10_000 + offset,
                "vw": value + 1,
            }
        )
    return json.dumps(
        {"bars": bars, "symbol": "AAPL", "next_page_token": None},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _build_pipeline(storage, runtime, workspace_id, transport):
    sec_configuration = resolve_sec_configuration(runtime.provider_resolver)
    alpaca_configuration = resolve_alpaca_configuration(runtime.provider_resolver)
    sec_client = SecEdgarClient(
        transport,
        SecEdgarIdentity("Investment Analyst integration@example.com"),
        cik=sec_configuration.cik,
        ticker=sec_configuration.ticker,
        sleep=lambda _: None,
        clock=lambda: SEC_RETRIEVED,
    )
    alpaca_client = AlpacaStockClient(
        transport,
        AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        clock=lambda: MARKET_RETRIEVED,
    )
    point_in_time = SecAaplFundamentalPointInTimeService(storage)
    history = HistoricalMarketDataService(storage)
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
            clock=lambda: NORMALIZED,
        ),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            alpaca_client,
            configuration=alpaca_configuration,
            clock=lambda: MARKET_RETRIEVED,
        ),
        fundamental_metric_pipeline=SecAaplFundamentalMetricPipeline(
            storage,
            point_in_time,
            SecFundamentalMetricEngine(),
            clock=lambda: COMPUTED,
        ),
        fundamental_diagnostic_pipeline=SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
            clock=lambda: COMPUTED + timedelta(minutes=1),
        ),
        market_statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: COMPUTED + timedelta(minutes=2),
        ),
        market_diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=lambda: COMPUTED + timedelta(minutes=3),
        ),
        consolidated_service=AaplConsolidatedDiagnosticService(storage),
        clock=lambda: EFFECTIVE,
    )


def _counts(storage) -> tuple[int, int, int, int]:
    return (
        len(storage.raw_records.list()),
        len(storage.observations.list()),
        len(storage.metric_results.list()),
        len(storage.diagnostics.list()),
    )


def _request(*, known_at: datetime | None = None) -> AaplWorkspaceBootstrapRequest:
    return AaplWorkspaceBootstrapRequest(
        market_start=date(2026, 1, 1),
        market_end=date(2026, 1, 26),
        fundamental_frequency=DataFrequency.QUARTERLY,
        requested_known_at=known_at,
        require_complete=True,
    )


def test_bootstrap_is_complete_idempotent_and_visible_to_workspace_inspection(
    tmp_path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _bars())
    location = StorageLocationRequest(workspace=workspace_root)

    with runtime.open_storage(
        location,
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        first = pipeline.run(_request())
        counts_after_first = _counts(storage)
        identifiers_after_first = (
            tuple(item.record_id for item in storage.raw_records.list()),
            tuple(item.observation_id for item in storage.observations.list()),
            tuple(item.result_id for item in storage.metric_results.list()),
            tuple(item.diagnostic_id for item in storage.diagnostics.list()),
        )
        second = pipeline.run(_request())
        counts_after_second = _counts(storage)
        identifiers_after_second = (
            tuple(item.record_id for item in storage.raw_records.list()),
            tuple(item.observation_id for item in storage.observations.list()),
            tuple(item.result_id for item in storage.metric_results.list()),
            tuple(item.diagnostic_id for item in storage.diagnostics.list()),
        )

    assert first.workspace_id == initialization.manifest.workspace_id
    assert first.effective_known_at == EFFECTIVE
    assert first.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert first.consolidated.market.diagnostic is not None
    assert first.consolidated.fundamental.diagnostic is not None
    assert first.consolidated.market.diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA
    assert second.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert counts_after_first == counts_after_second
    assert identifiers_after_first == identifiers_after_second
    assert second.raw_records_created == 0
    assert second.observations_created == 0
    assert second.metric_results_created == 0
    assert second.diagnostics_created == 0
    assert len(transport.calls) == 6

    inspection = workspace_service.inspect(workspace_root)
    assert inspection.workspace_id == initialization.manifest.workspace_id
    assert inspection.raw_record_count == counts_after_second[0]
    assert inspection.observation_count == counts_after_second[1]
    assert inspection.metric_result_count == counts_after_second[2]
    assert inspection.diagnostic_result_count == counts_after_second[3]


def test_too_early_cut_preserves_ingestion_and_can_resume_without_insufficient_data(
    tmp_path,
) -> None:
    workspace_root = tmp_path / "early"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _bars())
    location = StorageLocationRequest(workspace=workspace_root)

    with runtime.open_storage(
        location,
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        with pytest.raises(BootstrapKnownAtTooEarlyError) as captured:
            pipeline.run(_request(known_at=MARKET_RETRIEVED - timedelta(minutes=1)))
        assert captured.value.minimum_known_at == MARKET_RETRIEVED
        assert len(storage.raw_records.list()) > 0
        assert len(storage.observations.list()) > 0
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        resumed = pipeline.run(_request(known_at=EFFECTIVE))
        assert resumed.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
        assert all(
            item.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA
            for item in storage.diagnostics.list()
        )
