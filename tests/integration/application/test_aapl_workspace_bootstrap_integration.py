"""Offline end-to-end integration for the persistent Apple workspace bootstrap."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    BootstrapStageError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplBootstrapStage,
    AaplBootstrapStageStatus,
    AaplMarketRefreshMode,
    AaplRefreshMode,
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
from investment_analyst.providers.market.alpaca_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import (
    ALPACA_FETCH_RECEIPT_SCHEMA,
    AlpacaHistoricalPipeline,
)
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
    """Route official URLs to interval-aware deterministic local fixture bytes."""

    def __init__(
        self,
        submissions: bytes,
        companyfacts: bytes,
        bars: bytes,
        *,
        empty_as_null: bool = False,
    ) -> None:
        self.submissions = submissions
        self.companyfacts = companyfacts
        self.bar_document = json.loads(bars)
        self.empty_as_null = empty_as_null
        self.calls: list[str] = []
        self.sec_calls: list[str] = []
        self.alpaca_calls: list[str] = []
        self.fail_alpaca_call: int | None = None

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
            self.sec_calls.append(url)
            body = self.submissions
        elif "/companyfacts/" in url:
            self.sec_calls.append(url)
            body = self.companyfacts
        else:
            self.alpaca_calls.append(url)
            if self.fail_alpaca_call == len(self.alpaca_calls):
                raise RuntimeError("controlled Alpaca interval failure")
            assert "data.alpaca.markets" in url
            assert "/v2/stocks/AAPL/bars" in url
            assert "/v2/orders" not in url
            assert headers["APCA-API-KEY-ID"] == "test-key"
            assert headers["APCA-API-SECRET-KEY"] == "test-secret"
            query = parse_qs(urlsplit(url).query)
            assert query["feed"] == ["iex"]
            assert query["adjustment"] == ["all"]
            assert query["timeframe"] == ["1Day"]
            start = datetime.fromisoformat(query["start"][0])
            end = datetime.fromisoformat(query["end"][0])
            selected = [
                bar
                for bar in self.bar_document["bars"]
                if start <= datetime.fromisoformat(bar["t"].replace("Z", "+00:00")) < end
            ]
            body = json.dumps(
                {
                    "bars": None if self.empty_as_null and not selected else selected,
                    "symbol": self.bar_document["symbol"],
                    "next_page_token": None,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        return HttpResponse(status_code=200, body=body, headers={}, url=url)

    def alpaca_intervals(self) -> tuple[tuple[datetime, datetime], ...]:
        """Return the exact half-open intervals requested from Alpaca."""
        intervals = []
        for url in self.alpaca_calls:
            query = parse_qs(urlsplit(url).query)
            intervals.append(
                (
                    datetime.fromisoformat(query["start"][0]),
                    datetime.fromisoformat(query["end"][0]),
                )
            )
        return tuple(intervals)


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
    first = datetime(2025, 12, 28, 5, tzinfo=UTC)
    for offset in range(38):
        timestamp = first + timedelta(days=offset)
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


def _real_case_bars() -> bytes:
    timestamps = [
        datetime(2025, 1, 2, 5, tzinfo=UTC) + timedelta(days=offset) for offset in range(24)
    ]
    timestamps.append(datetime(2026, 7, 13, 4, tzinfo=UTC))
    bars = []
    for offset, timestamp in enumerate(timestamps):
        value = 150 + offset
        bars.append(
            {
                "t": timestamp.isoformat().replace("+00:00", "Z"),
                "o": value,
                "h": value + 3,
                "l": value - 1,
                "c": value + 2,
                "v": 2_000_000 + offset * 10_000,
                "n": 20_000 + offset,
                "vw": value + 1,
            }
        )
    return json.dumps(
        {"bars": bars, "symbol": "AAPL", "next_page_token": None},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _build_pipeline(
    storage,
    runtime,
    workspace_id,
    transport,
    *,
    execution_offset: timedelta = timedelta(),
):
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
            clock=lambda: COMPUTED + execution_offset,
        ),
        fundamental_diagnostic_pipeline=SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
            clock=lambda: COMPUTED + execution_offset + timedelta(minutes=1),
        ),
        market_statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: COMPUTED + execution_offset + timedelta(minutes=2),
        ),
        market_diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=lambda: COMPUTED + execution_offset + timedelta(minutes=3),
        ),
        consolidated_service=AaplConsolidatedDiagnosticService(storage),
        clock=lambda: EFFECTIVE + execution_offset,
    )


def _counts(storage) -> tuple[int, int, int, int]:
    return (
        len(storage.raw_records.list()),
        len(storage.observations.list()),
        len(storage.metric_results.list()),
        len(storage.diagnostics.list()),
    )


def _request(
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 26),
    known_at: datetime | None = None,
    refresh_mode: AaplRefreshMode = AaplRefreshMode.AUTO,
) -> AaplWorkspaceBootstrapRequest:
    return AaplWorkspaceBootstrapRequest(
        market_start=start,
        market_end=end,
        fundamental_frequency=DataFrequency.QUARTERLY,
        refresh_mode=refresh_mode,
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
        market_timestamps = {
            item.observed_at
            for item in storage.observations.list(asset_id=ASSET_ID)
            if item.source.source_id == SOURCE_ID and item.observed_at is not None
        }
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
    assert first.consolidated.market.diagnostic.as_of == datetime(2026, 1, 26, 5, tzinfo=UTC)
    assert first.consolidated.fundamental.diagnostic is not None
    assert first.consolidated.market.diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA
    assert first.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
    assert second.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert second.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    assert counts_after_first == counts_after_second
    assert identifiers_after_first == identifiers_after_second
    assert second.raw_records_created == 0
    assert second.observations_created == 0
    assert second.metric_results_created == 0
    assert second.diagnostics_created == 0
    assert max(market_timestamps) == datetime(2026, 1, 26, 5, tzinfo=UTC)
    assert datetime(2026, 1, 27, tzinfo=UTC) not in market_timestamps
    assert datetime(2026, 1, 27, 5, tzinfo=UTC) not in market_timestamps
    market_stage = next(
        item for item in second.stages if item.stage is AaplBootstrapStage.MARKET_FETCH
    )
    assert market_stage.status is AaplBootstrapStageStatus.SKIPPED
    assert market_stage.generated == market_stage.created == market_stage.reused == 0
    assert len(transport.sec_calls) == 4
    assert len(transport.alpaca_calls) == 1
    assert len(transport.calls) == 5

    inspection = workspace_service.inspect(workspace_root)
    assert inspection.workspace_id == initialization.manifest.workspace_id
    assert inspection.raw_record_count == counts_after_second[0]
    assert inspection.observation_count == counts_after_second[1]
    assert inspection.metric_result_count == counts_after_second[2]
    assert inspection.diagnostic_result_count == counts_after_second[3]


def test_two_automatic_bootstraps_with_distinct_clocks_keep_stable_representative(
    tmp_path,
) -> None:
    workspace_root = tmp_path / "semantic-revisions"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _real_case_bars())
    request = _request(
        start=date(2025, 1, 1),
        end=date(2026, 7, 13),
    )

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        first = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        ).run(request)
        metric_ids_after_first = {
            item.result_id for item in storage.metric_results.list(asset_id=ASSET_ID)
        }
        diagnostic_ids_after_first = {
            item.diagnostic_id for item in storage.diagnostics.list(asset_id=ASSET_ID)
        }
        counts_after_first = _counts(storage)
        alpaca_calls_after_first = len(transport.alpaca_calls)

        second = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
            execution_offset=timedelta(days=1),
        ).run(request)
        metric_ids_after_second = {
            item.result_id for item in storage.metric_results.list(asset_id=ASSET_ID)
        }
        diagnostic_ids_after_second = {
            item.diagnostic_id for item in storage.diagnostics.list(asset_id=ASSET_ID)
        }
        counts_after_second = _counts(storage)

    assert first.effective_known_at == EFFECTIVE
    assert second.effective_known_at == EFFECTIVE + timedelta(days=1)
    assert first.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert second.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert first.consolidated.market.diagnostic is not None
    assert second.consolidated.market.diagnostic == first.consolidated.market.diagnostic
    assert first.consolidated.market.diagnostic.as_of == datetime(2026, 7, 13, 4, tzinfo=UTC)
    assert second.consolidated.market.revisions_superseded == 1
    assert second.consolidated.fundamental.revisions_superseded == 0
    assert second.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    second_market_stage = next(
        item for item in second.stages if item.stage is AaplBootstrapStage.MARKET_FETCH
    )
    assert second_market_stage.status is AaplBootstrapStageStatus.SKIPPED
    assert len(transport.alpaca_calls) == alpaca_calls_after_first == 1
    assert len(transport.sec_calls) == 4
    assert second.metric_results_created > 0
    assert second.diagnostics_created > 0
    assert counts_after_second[:2] == counts_after_first[:2]
    assert counts_after_second[2] > counts_after_first[2]
    assert counts_after_second[3] > counts_after_first[3]
    assert metric_ids_after_first < metric_ids_after_second
    assert diagnostic_ids_after_first < diagnostic_ids_after_second
    assert second.traceability_verified is True
    assert second.consolidated.traceability_verified is True


def test_empty_real_prefix_receipt_makes_next_run_already_current(tmp_path) -> None:
    workspace_root = tmp_path / "empty-prefix"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(
        submissions,
        companyfacts,
        _real_case_bars(),
        empty_as_null=True,
    )

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        setup = pipeline.run(
            _request(
                start=date(2025, 1, 2),
                end=date(2026, 7, 13),
            )
        )
        assert setup.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
        transport.alpaca_calls.clear()
        sec_before = len(transport.sec_calls)
        observations_before = len(storage.observations.list(asset_id=ASSET_ID))
        market_records_before = len(storage.raw_records.list(source_id=SOURCE_ID))

        first = pipeline.run(
            _request(
                start=date(2025, 1, 1),
                end=date(2026, 7, 13),
            )
        )
        first_market_stage = next(
            item for item in first.stages if item.stage is AaplBootstrapStage.MARKET_FETCH
        )
        receipt_count = sum(
            record.schema_version == ALPACA_FETCH_RECEIPT_SCHEMA
            for record in storage.raw_records.list(source_id=SOURCE_ID)
        )
        observations_after_first = len(storage.observations.list(asset_id=ASSET_ID))
        market_records_after_first = len(storage.raw_records.list(source_id=SOURCE_ID))
        assert transport.alpaca_intervals() == (
            (
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            ),
        )
        assert first.refresh_plan.mode is AaplMarketRefreshMode.BACKFILL
        assert first.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
        assert first_market_stage.status is AaplBootstrapStageStatus.COMPLETED
        assert first_market_stage.details.intervals_executed == 1
        assert first_market_stage.details.bars_processed == 0
        assert first_market_stage.details.coverage_receipts_created == 1
        assert first_market_stage.details.empty_intervals_completed == 1
        assert first.consolidated.market.diagnostic is not None
        assert first.consolidated.market.diagnostic.as_of == datetime(2026, 7, 13, 4, tzinfo=UTC)
        assert observations_after_first == observations_before
        assert market_records_after_first == market_records_before + 1
        assert receipt_count == 2
        assert len(transport.sec_calls) == sec_before + 2

        transport.alpaca_calls.clear()
        second = pipeline.run(
            _request(
                start=date(2025, 1, 1),
                end=date(2026, 7, 13),
            )
        )
        second_market_stage = next(
            item for item in second.stages if item.stage is AaplBootstrapStage.MARKET_FETCH
        )
        assert second.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
        assert transport.alpaca_calls == []
        assert second_market_stage.status is AaplBootstrapStageStatus.SKIPPED
        assert second.raw_records_created == 0
        assert second.observations_created == 0
        assert second.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
        assert second.consolidated.market.diagnostic is not None
        assert second.consolidated.market.diagnostic.as_of == datetime(2026, 7, 13, 4, tzinfo=UTC)


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


def test_incremental_suffix_and_full_refresh_use_exact_half_open_ranges(tmp_path) -> None:
    workspace_root = tmp_path / "incremental"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _bars())

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        initial = pipeline.run(_request())
        counts_initial = _counts(storage)
        extended = pipeline.run(_request(end=date(2026, 1, 28)))
        counts_extended = _counts(storage)
        full = pipeline.run(
            _request(
                end=date(2026, 1, 28),
                refresh_mode=AaplRefreshMode.FULL,
            )
        )
        counts_full = _counts(storage)

    assert initial.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
    assert extended.refresh_plan.mode is AaplMarketRefreshMode.INCREMENTAL
    assert extended.consolidated.market.diagnostic is not None
    assert extended.consolidated.market.diagnostic.as_of == datetime(2026, 1, 28, 5, tzinfo=UTC)
    assert counts_extended[0] == counts_initial[0] + 3
    assert counts_extended[1] == counts_initial[1] + 14
    assert full.refresh_plan.mode is AaplMarketRefreshMode.FULL
    assert full.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert counts_full[0] == counts_extended[0] + 1
    assert counts_full[1:] == counts_extended[1:]
    assert transport.alpaca_intervals() == (
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 27, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 27, tzinfo=UTC),
            datetime(2026, 1, 29, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 29, tzinfo=UTC),
        ),
    )
    assert len(transport.sec_calls) == 6


def test_backfill_only_requests_prefix(tmp_path) -> None:
    workspace_root = tmp_path / "backfill"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _bars())

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        pipeline.run(_request(start=date(2026, 1, 5), end=date(2026, 1, 28)))
        backfill = pipeline.run(_request(start=date(2026, 1, 1), end=date(2026, 1, 28)))

    assert backfill.refresh_plan.mode is AaplMarketRefreshMode.BACKFILL
    assert backfill.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert transport.alpaca_intervals()[-1] == (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 5, tzinfo=UTC),
    )


def test_second_interval_failure_persists_prefix_and_resume_fetches_only_suffix(
    tmp_path,
) -> None:
    workspace_root = tmp_path / "resume"
    workspace_service = WorkspaceService(environ={}, home=tmp_path)
    initialization = workspace_service.initialize(workspace_root)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    submissions, companyfacts = _sec_documents()
    transport = FixtureTransport(submissions, companyfacts, _bars())

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        pipeline = _build_pipeline(
            storage,
            runtime,
            initialization.manifest.workspace_id,
            transport,
        )
        pipeline.run(_request(start=date(2026, 1, 5), end=date(2026, 1, 26)))
        transport.fail_alpaca_call = len(transport.alpaca_calls) + 2
        with pytest.raises(BootstrapStageError) as captured:
            pipeline.run(_request(start=date(2026, 1, 1), end=date(2026, 1, 28)))
        assert captured.value.stage is AaplBootstrapStage.MARKET_FETCH
        persisted_dates = {
            item.observed_at.date()
            for item in storage.observations.list(asset_id=ASSET_ID)
            if item.source.source_id == SOURCE_ID and item.observed_at is not None
        }
        assert date(2026, 1, 1) in persisted_dates
        assert date(2026, 1, 27) not in persisted_dates

        transport.fail_alpaca_call = None
        resumed = pipeline.run(_request(start=date(2026, 1, 1), end=date(2026, 1, 28)))

    attempted = transport.alpaca_intervals()
    assert attempted[-3:-1] == (
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 27, tzinfo=UTC),
            datetime(2026, 1, 29, tzinfo=UTC),
        ),
    )
    assert attempted[-1] == (
        datetime(2026, 1, 27, tzinfo=UTC),
        datetime(2026, 1, 29, tzinfo=UTC),
    )
    assert resumed.refresh_plan.mode is AaplMarketRefreshMode.INCREMENTAL
    assert resumed.overall_status is ConsolidatedDiagnosticStatus.COMPLETE
    assert resumed.traceability_verified is True
