"""Integration coverage for the reproducible evidence read-path benchmark."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    summarize_market_history_v2,
)
from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest
from investment_analyst.frontend.local_web import AaplLocalHttpServer
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging
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
    assert calls["blob_reads"] == 2
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
