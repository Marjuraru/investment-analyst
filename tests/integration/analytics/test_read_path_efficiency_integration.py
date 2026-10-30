"""Integration coverage for the reproducible evidence read-path benchmark."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    summarize_market_history_v2,
)
from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service
from investment_analyst.analytics.valuation import (
    CorporateValuationFilingQuery,
    CorporateValuationFilingService,
    CorporateValuationSelectedFilingRequest,
)
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.frontend.local_web import AaplLocalHttpServer
from investment_analyst.providers.fundamentals.sec_fact_models import get_sec_fact_definition
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.read_dependency_signature import (
    READ_DEPENDENCY_CODE_VERSION,
    ReadDependencyFamily,
    ReadDependencyKind,
    ReadDependencyQuery,
)
from investment_analyst.storage.workspace_v2_repositories import (
    WorkspaceV2ObservationRepository,
)
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 1, 5, 16, tzinfo=UTC)


def _bar(index: int, *, symbol: str = "AAPL") -> AlpacaStockBar:
    timestamp = _BASE + timedelta(days=index)
    close = Decimal("200") + Decimal(index * 2)
    return AlpacaStockBar(
        symbol=symbol,
        timestamp=timestamp,
        open=close - 1,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=Decimal("1000") + Decimal(index * 100),
        trade_count=Decimal("100") + Decimal(index),
        vwap=close,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(close - 1),
            "h": str(close + 2),
            "l": str(close - 2),
            "c": str(close),
            "v": str(Decimal("1000") + Decimal(index * 100)),
            "n": str(Decimal("100") + Decimal(index)),
            "vw": str(close),
        },
    )


def _stage_bar(staging, bar: AlpacaStockBar, *, retrieved_at: datetime) -> None:
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/test",
    )
    staging.save(raw)
    staging.save_observations(
        bar_to_observations(bar, raw, normalized_at=retrieved_at + timedelta(minutes=1))
    )


class _BenchmarkApplication:
    def overview(self) -> dict[str, object]:
        return {"status": "ready"}

    def compact_overview(self) -> dict[str, object]:
        return {"status": "ready", "workspace_id": "fixture"}

    def market_assets(self) -> dict[str, object]:
        return {"assets": []}

    def runtime_capabilities(self) -> dict[str, object]:
        return {"capabilities": []}

    def read_performance(self) -> dict[str, object]:
        return {
            "schema_version": "critical-read-performance-v1",
            "generation": "fixture",
            "uptime_seconds": 0.0,
            "requests": 0,
            "errors": 0,
            "families": {},
        }


def test_read_path_benchmark_uses_temporary_corpus_and_reduces_repeated_work() -> None:
    repository_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "src")

    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / "benchmark_evidence_read_path.py")],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["schema_version"] == "evidence-read-path-benchmark-v1"
    assert result["baseline"]["checksum_reads"] == 128
    assert result["optimized"]["checksum_reads"] == 32
    assert result["baseline"]["semantic_parses"] == 128
    assert result["optimized"]["semantic_parses"] == 32
    assert result["baseline"]["cache_invalidations"] == 10
    assert result["optimized"]["cache_invalidations"] == 7


def test_runtime_read_benchmark_reports_critical_telemetry_and_bounded_overhead() -> None:
    repository_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "src")
    server = AaplLocalHttpServer(
        ("127.0.0.1", 0),
        _BenchmarkApplication(),  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        completed = subprocess.run(
            [
                sys.executable,
                str(repository_root / "scripts" / "benchmark_runtime_reads.py"),
                "--base-url",
                base_url,
                "--samples",
                "5",
                "--include-critical",
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            check=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "runtime-read-benchmark-v1"
    assert result["results"][0]["name"] == "overview"
    assert "max_ms" in result["results"][0]
    critical = result["critical_read_performance"]
    assert critical["schema_version"] == "critical-read-performance-v1"
    overhead = result["telemetry_overhead"]
    assert overhead["pairs"] == 20
    assert overhead["passed"] is True


def test_v2_market_projection_matches_v1_and_hydrates_only_the_latest_bar(
    tmp_path: Path, monkeypatch
) -> None:
    initialized = WorkspaceService().initialize(tmp_path / "workspace", format_version=2)
    workspace = initialized.paths.root
    storage_paths = StoragePaths.from_workspace_root(
        workspace,
        format_version=2,
        workspace_id=initialized.manifest.workspace_id,
    )
    known_at = datetime(2026, 2, 1, tzinfo=UTC)
    query = HistoricalBarQuery(
        asset_id=ASSET_ID,
        source_id=ALPACA_SOURCE_ID,
        start=_BASE,
        end=_BASE + timedelta(days=6),
        known_at=known_at,
    )
    with LocalStorage(storage_paths) as storage:
        for index in range(5):
            _stage_bar(
                storage.store.raw_staging,
                _bar(index),
                retrieved_at=_BASE + timedelta(days=index, hours=2),
            )
        v1 = HistoricalMarketDataService(storage).query(query)
        v2_service = HistoricalMarketDataV2Service(storage.store.raw_staging)
        projection = v2_service.query(query)
        materialized = v2_service.materialize_many(projection.bars, query)
        v2_bars = tuple(sorted(materialized.values(), key=lambda item: item.timestamp))
        assert projection.candidate_versions == v1.coverage.candidate_versions
        assert projection.discarded_revisions == v1.coverage.discarded_revisions
        assert len(v2_bars) == v1.coverage.bar_count
        assert v2_bars[-1] == v1.bars[-1]
        assert projection.bars[0].timestamp == v1.coverage.earliest_timestamp
        assert projection.bars[-1].timestamp == v1.coverage.latest_timestamp
        summary = summarize_market_history_v2(storage.store.raw_staging, query)
        assert summary.bar_count == len(v2_bars)
        assert summary.candidate_versions == projection.candidate_versions
        assert summary.discarded_revisions == projection.discarded_revisions
        assert summary.earliest_timestamp == v1.coverage.earliest_timestamp
        assert summary.latest_timestamp == v1.coverage.latest_timestamp
        assert summary.latest_projection == projection.bars[-1]

        full_pages = {"count": 0}
        original_full_page = RawV2Staging.list_market_observation_page

        def counted_full_page(self, **kwargs):
            full_pages["count"] += 1
            return original_full_page(self, **kwargs)

        monkeypatch.setattr(RawV2Staging, "list_market_observation_page", counted_full_page)
        assert summarize_market_history_v2(storage.store.raw_staging, query) == summary
        assert full_pages["count"] == 1

        calls = {"blob_reads": 0, "projection_checks": 0}
        original_read_blob = storage.store.raw_staging._read_blob
        original_projection_check = storage.store.raw_staging._workspace_projection_table_exists

        def counted_read_blob(relative_path: str, checksum: str):
            calls["blob_reads"] += 1
            return original_read_blob(relative_path, checksum)

        def counted_projection_check() -> bool:
            calls["projection_checks"] += 1
            return original_projection_check()

        monkeypatch.setattr(storage.store.raw_staging, "_read_blob", counted_read_blob)
        monkeypatch.setattr(
            storage.store.raw_staging,
            "_workspace_projection_table_exists",
            counted_projection_check,
        )
        storage.store.raw_staging.get_many([item.raw_record_id for item in projection.bars[:3]])
        assert calls["projection_checks"] == 1
        assert calls["blob_reads"] == 3

    calls["blob_reads"] = 0
    original_class_read_blob = RawV2Staging._read_blob

    def counted_class_read_blob(self, relative_path: str, checksum: str):
        calls["blob_reads"] += 1
        return original_class_read_blob(self, relative_path, checksum)

    monkeypatch.setattr(RawV2Staging, "_read_blob", counted_class_read_blob)
    result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        UniverseCoverageRequest(
            known_at=known_at,
            market_start=date(2026, 1, 1),
            market_end=date(2026, 1, 31),
            fundamental_start=date(2020, 1, 1),
            fundamental_end=date(2026, 1, 31),
            asset_ids=(ASSET_ID,),
        ),
    )
    aapl = result.assets[0].market
    assert aapl.bar_count == len(v2_bars)
    assert aapl.candidate_versions == projection.candidate_versions
    assert aapl.discarded_revisions == projection.discarded_revisions
    assert aapl.latest_raw_record_id == v1.bars[-1].raw_record_id
    assert aapl.latest_observation_ids == tuple(
        sorted(v1.bars[-1].observation_ids.values(), key=str)
    )
    assert calls["blob_reads"] == 1
    parallel = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        UniverseCoverageRequest(
            known_at=known_at,
            market_start=date(2026, 1, 1),
            market_end=date(2026, 1, 31),
            fundamental_start=date(2020, 1, 1),
            fundamental_end=date(2026, 1, 31),
            asset_ids=("crypto:btc-usd", ASSET_ID),
        ),
    )
    assert parallel.assets[1].model_dump() == result.assets[0].model_dump()


_FILING_FACTS = (
    ("fundamental.revenue", "100000", "USD"),
    ("fundamental.net_income", "20000", "USD"),
    ("fundamental.stockholders_equity", "80000", "USD"),
    ("fundamental.shares_outstanding", "1000", "shares"),
)


def _seed_annual_facts(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    available_at: datetime,
    accession_number: str,
    period_end: date,
    values: tuple[tuple[str, str, str], ...] = _FILING_FACTS,
) -> None:
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    observations: list[NormalizedObservation] = []
    raw_records: list[RawRecord] = []
    for field_name, value, unit in values:
        definition = get_sec_fact_definition(field_name)
        raw_id = uuid4()
        key = {
            "accession_number": accession_number,
            "taxonomy": definition.taxonomy,
            "tag": definition.tag,
            "unit": unit,
            "period": period_end.isoformat(),
            "companyfacts_record_id": str(raw_id),
            "submissions_record_id": str(uuid4()),
            "form": "10-K",
            "fiscal_year": "2025",
            "fiscal_period": "FY",
        }
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw_id,
                asset_id=asset_id,
                field_name=field_name,
                value=Decimal(value),
                unit=unit,
                frequency=DataFrequency.ANNUAL,
                observed_at=period_at,
                period_end=period_at,
                available_at=available_at,
                normalized_at=available_at,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(key, separators=(",", ":"), sort_keys=True),
                    "retrieved_at": available_at,
                },
                quality=DataQuality.VALID,
                transformation_version="sec-companyfacts-normalizer-v2",
            )
        )
        raw_records.append(
            RawRecord(
                record_id=raw_id,
                asset_id=asset_id,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(
                        {"companyfacts_record_id": str(raw_id)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "retrieved_at": available_at,
                },
                event_time=period_at,
                available_at=available_at,
                received_at=available_at,
                payload={"fixture": field_name},
                schema_version="sec-companyfacts-snapshot-v1",
            )
        )
    storage.raw_records.save_many(raw_records)
    storage.observations.save_many(observations)


def _filing_service(
    storage: LocalStorage,
    catalog: AssetCatalogService,
) -> CorporateValuationFilingService:
    asset = catalog.get(ASSET_ID)
    resolver = ProviderAssetContextResolver(catalog)
    return CorporateValuationFilingService(
        storage,
        capabilities=analysis_capabilities_for(asset),
        market_source_id=resolve_alpaca_configuration(resolver, asset_id=ASSET_ID).source_id,
        fundamental_source_id=resolve_sec_configuration(
            resolver, asset_id=ASSET_ID
        ).companyfacts_source_id,
        price_currency=asset.quote_currency,
        security_unit_factor=asset.security_unit_factor,
        security_unit_basis=asset.security_unit_basis,
        security_unit_basis_version=asset.security_unit_basis_version,
        security_unit_market_adjustment=asset.security_unit_market_adjustment,
        security_unit_basis_revisions=asset.security_unit_basis_revisions,
    )


def test_filing_selection_read_path_is_bounded_and_reopen_stable(
    tmp_path: Path, monkeypatch
) -> None:
    initialized = WorkspaceService().initialize(tmp_path / "workspace", format_version=2)
    storage_paths = StoragePaths.from_workspace_root(
        initialized.paths.root,
        format_version=2,
        workspace_id=initialized.manifest.workspace_id,
    )
    catalog = AssetCatalogService.load_default()
    resolver = ProviderAssetContextResolver(catalog)
    sec_source = resolve_sec_configuration(resolver, asset_id=ASSET_ID).companyfacts_source_id
    market_source = resolve_alpaca_configuration(resolver, asset_id=ASSET_ID).source_id
    assert market_source == ALPACA_SOURCE_ID
    accepted_at = datetime(2026, 1, 30, 12, tzinfo=UTC)
    known_at = datetime(2026, 2, 1, tzinfo=UTC)
    query = CorporateValuationFilingQuery(
        asset_id=ASSET_ID,
        known_at=known_at,
        valuation_date=date(2026, 1, 31),
        start_date=date(2024, 1, 1),
        end_date=date(2026, 12, 31),
    )
    request = CorporateValuationSelectedFilingRequest(
        asset_id=ASSET_ID,
        known_at=known_at,
        valuation_date=date(2026, 1, 31),
        annual_period_end=date(2025, 9, 27),
        filing_accession_number="0000320193-25-000079",
        filing_accepted_at=accepted_at,
        filing_form="10-K",
        fiscal_year="2025",
        fiscal_period="FY",
    )
    hydrated: list[NormalizedObservation] = []
    original_list = WorkspaceV2ObservationRepository.list

    def counted_list(self, **filters):
        rows = original_list(self, **filters)
        hydrated.extend(rows)
        return rows

    monkeypatch.setattr(WorkspaceV2ObservationRepository, "list", counted_list)

    def seed_foreign_corpus(storage: LocalStorage, first: int, last: int) -> None:
        for index in range(first, last):
            _seed_annual_facts(
                storage,
                asset_id=f"equity:us:foreign-{index:03d}",
                source_id=sec_source,
                available_at=accepted_at,
                accession_number=f"0000000000-25-{index:06d}",
                period_end=date(2025, 9, 27),
            )
        _seed_annual_facts(
            storage,
            asset_id=ASSET_ID,
            source_id="sec-edgar:foreign:companyfacts",
            available_at=accepted_at,
            accession_number="0000000000-25-999999",
            period_end=date(2025, 9, 27),
        )

    def assert_calls_are_local(previous: int, *, expected: int) -> int:
        new_rows = hydrated[previous:]
        assert len(new_rows) == expected
        assert {row.asset_id for row in new_rows} == {ASSET_ID}
        assert {row.source.source_id for row in new_rows} == {sec_source}
        return len(hydrated)

    with LocalStorage(storage_paths) as storage:
        _stage_bar(
            storage.store.raw_staging,
            _bar(0),
            retrieved_at=_BASE + timedelta(hours=2),
        )
        _seed_annual_facts(
            storage,
            asset_id=ASSET_ID,
            source_id=sec_source,
            available_at=accepted_at,
            accession_number="0000320193-25-000079",
            period_end=date(2025, 9, 27),
        )
        seed_foreign_corpus(storage, 0, 10)
        service = _filing_service(storage, catalog)
        options = service.query_options(query)
        first = service.query(
            request,
            computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        )
        assert options.total == 1
        assert first.coverage.evaluated == 5
        baseline_ids = tuple(item.result_id for item in first.metrics)
        baseline_values = tuple(item.value for item in first.metrics)
        first_mark = assert_calls_are_local(0, expected=len(_FILING_FACTS) * 2)

        seed_foreign_corpus(storage, 10, 100)
        options_10x = service.query_options(query)
        second = service.query(
            request,
            computed_at=datetime(2026, 2, 1, 21, 5, tzinfo=UTC),
        )
        assert options_10x.total == 1
        assert tuple(item.result_id for item in second.metrics) == baseline_ids
        assert tuple(item.value for item in second.metrics) == baseline_values
        assert_calls_are_local(first_mark, expected=len(_FILING_FACTS) * 2)

    reopened = LocalStorage(storage_paths)
    with reopened:
        warmed = _filing_service(reopened, catalog).query(
            request,
            computed_at=datetime(2026, 2, 1, 21, 10, tzinfo=UTC),
        )
        assert tuple(item.result_id for item in warmed.metrics) == baseline_ids
        assert tuple(item.value for item in warmed.metrics) == baseline_values
        assert_calls_are_local(
            len(hydrated) - len(_FILING_FACTS),
            expected=len(_FILING_FACTS),
        )


def test_read_dependency_signature_binds_code_and_catalog_revisions(
    tmp_path: Path,
) -> None:
    initialized = WorkspaceService().initialize(tmp_path / "workspace", format_version=2)
    workspace = initialized.paths.root
    storage_paths = StoragePaths.from_workspace_root(
        workspace,
        format_version=2,
        workspace_id=initialized.manifest.workspace_id,
    )
    with LocalStorage(storage_paths) as storage:
        _stage_bar(
            storage.store.raw_staging,
            _bar(0),
            retrieved_at=_BASE + timedelta(hours=2),
        )
    query = ReadDependencyQuery(
        family=ReadDependencyFamily.MARKET_BARS,
        endpoint="/api/market-chart",
        asset_ids=(ASSET_ID,),
        source_ids=(ALPACA_SOURCE_ID,),
        start=_BASE,
        end=_BASE + timedelta(days=2),
        known_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    application = InvestmentAnalystApplication.create_default()
    location = StorageLocationRequest(workspace=workspace)

    signature = application.read_dependency_signature(query, location=location)
    composite = application.read_composite_dependency_signature((query,), location=location)

    catalog = AssetCatalogService.load_default()
    expected_code_version = f"{READ_DEPENDENCY_CODE_VERSION}:{catalog.semantic_digest}"
    assert signature.code_version == expected_code_version
    assert composite.code_version == expected_code_version
    assert any(
        dependency.kind is ReadDependencyKind.OBSERVATION for dependency in signature.dependencies
    )
    assert any(
        dependency.kind is ReadDependencyKind.CATALOG for dependency in signature.dependencies
    )
    assert not any(
        dependency.dependency_id == "equity:us:tsm" for dependency in signature.dependencies
    )
