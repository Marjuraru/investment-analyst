"""Hermetic operational regressions for bounded market memory and shutdown."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3] / "src"


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(_source_root()), current) if item
    )
    return environment


@pytest.mark.skipif(os.name != "posix", reason="operational signal regression requires POSIX")
def test_sequential_market_refresh_memory_is_bounded_with_foreign_large_corpus(
    tmp_path: Path,
) -> None:
    runner = r"""
import gc
import json
import resource
import sys
import time
import tracemalloc
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import MarketDiagnosticMetricSelector
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.listed_market_refresh import ListedMarketRefreshPipeline
from investment_analyst.application.listed_market_refresh_models import ListedMarketRefreshRequest
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.storage import LocalStorage, StoragePaths

ROOT = __import__("pathlib").Path(sys.argv[1])
CLOCK = datetime(2026, 7, 12, 12, tzinfo=UTC)
FOREIGN_ASSET = "equity:us:foreign-large"
FOREIGN_SOURCE = "foreign:large-corpus"
FOREIGN_SCHEMA = "foreign-large-v1"
FOREIGN_COUNT = 64
FOREIGN_PAYLOAD = "x" * (2 * 1024 * 1024)


class SyntheticTransport:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        bars = []
        for index in range(3):
            timestamp = datetime(2026, 7, 7 + index, 14, tzinfo=UTC)
            close = Decimal("100") + Decimal(index)
            bars.append(
                {
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": str(close - Decimal("1")),
                    "h": str(close + Decimal("2")),
                    "l": str(close - Decimal("2")),
                    "c": str(close),
                    "v": str(Decimal("1000") + Decimal(index * 100)),
                    "n": str(100 + index),
                    "vw": str(close),
                }
            )
        body = json.dumps(
            {"bars": bars, "symbol": self._symbol, "next_page_token": None},
            separators=(",", ":"),
        ).encode()
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def seed_foreign_corpus(storage: LocalStorage) -> None:
    source = SourceReference(
        source_id=FOREIGN_SOURCE,
        record_key="foreign-large",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for index in range(FOREIGN_COUNT):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        raw = RawRecord(
            record_id=uuid4(),
            asset_id=FOREIGN_ASSET,
            source=source.model_copy(update={"record_key": f"foreign:{index}"}),
            event_time=timestamp,
            available_at=timestamp,
            received_at=timestamp,
            payload={"large_document": FOREIGN_PAYLOAD, "index": index},
            schema_version=FOREIGN_SCHEMA,
        )
        storage.raw_records.save(raw)
        storage.observations.save(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw.record_id,
                asset_id=FOREIGN_ASSET,
                field_name="close",
                value=Decimal("1"),
                unit="USD",
                frequency=DataFrequency.DAY_1,
                observed_at=timestamp,
                available_at=timestamp,
                normalized_at=timestamp + timedelta(minutes=1),
                source=raw.source,
                quality=DataQuality.VALID,
                transformation_version=FOREIGN_SCHEMA,
            )
        )


def configuration(symbol: str) -> AlpacaAssetConfiguration:
    normalized = symbol.casefold()
    return AlpacaAssetConfiguration(
        asset_id=f"equity:us:{normalized}",
        symbol=symbol,
        feed="iex",
        adjustment="all",
        source_id=f"alpaca-market-data:iex:{normalized}:daily-bars:adjustment-all",
        name=f"Synthetic {symbol}",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def refresh_pipeline(storage: LocalStorage, resolved: AlpacaAssetConfiguration):
    history = HistoricalMarketDataService(storage)
    client = AlpacaStockClient(
        SyntheticTransport(resolved.symbol),
        AlpacaCredentials(api_key="synthetic-key", secret_key="synthetic-secret"),
        clock=lambda: CLOCK,
    )
    return ListedMarketRefreshPipeline(
        configuration=resolved,
        refresh_planner=AaplMarketRefreshPlanner(storage, configuration=resolved),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            client,
            configuration=resolved,
            clock=lambda: CLOCK,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: CLOCK,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=lambda: CLOCK,
        ),
        clock=lambda: CLOCK,
    )


def rss_bytes() -> int:
    scale = 1 if sys.platform == "darwin" else 1024
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale)


tracemalloc.start()
with LocalStorage(StoragePaths.from_root(ROOT)) as storage:
    seed_foreign_corpus(storage)
    gc.collect()
    baseline_rss = rss_bytes()
    baseline_heap, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    summaries = []
    for symbol in ("AAPL", "AMD"):
        started = time.perf_counter()
        resolved = configuration(symbol)
        request = ListedMarketRefreshRequest(
            asset_id=resolved.asset_id,
            market_start=date(2026, 7, 7),
            market_end=date(2026, 7, 9),
        )
        summary = refresh_pipeline(storage, resolved).run(request)
        summaries.append(
            {
                "asset_id": summary.asset_id,
                "bars_received": summary.bars_received,
                "raw_records_created": summary.raw_records_created,
                "observations_created": summary.observations_created,
                "metric_results_created": summary.metric_results_created,
                "diagnostics_created": summary.diagnostics_created,
                "traceability_verified": summary.traceability_verified,
                "duration_seconds": time.perf_counter() - started,
            }
        )
    peak_heap = tracemalloc.get_traced_memory()[1]
    foreign_raw_count = storage.raw_records.count(
        asset_id=FOREIGN_ASSET,
        source_id=FOREIGN_SOURCE,
        schema_version=FOREIGN_SCHEMA,
    )
    foreign_observation_count = storage.observations.count(asset_id=FOREIGN_ASSET)

print(
    json.dumps(
        {
            "memory_budget_bytes": 100 * 1024 * 1024,
            "peak_rss_delta_bytes": rss_bytes() - baseline_rss,
            "peak_heap_delta_bytes": peak_heap - baseline_heap,
            "foreign_raw_count": foreign_raw_count,
            "foreign_observation_count": foreign_observation_count,
            "summaries": summaries,
        },
        sort_keys=True,
    )
)
"""
    result = subprocess.run(
        [sys.executable, "-c", runner, str(tmp_path / "memory-workspace")],
        cwd=tmp_path,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["peak_rss_delta_bytes"] <= payload["memory_budget_bytes"]
    assert payload["peak_heap_delta_bytes"] <= payload["memory_budget_bytes"]
    assert payload["foreign_raw_count"] == 64
    assert payload["foreign_observation_count"] == 64
    assert [item["asset_id"] for item in payload["summaries"]] == [
        "equity:us:aapl",
        "equity:us:amd",
    ]
    assert all(item["traceability_verified"] for item in payload["summaries"])
    assert all(item["bars_received"] == 3 for item in payload["summaries"])


@pytest.mark.skipif(os.name != "posix", reason="operational signal regression requires POSIX")
def test_scheduler_subprocess_sigterm_closes_cooperatively_and_persists_interrupted(
    tmp_path: Path,
) -> None:
    runner = r"""
import json
import signal
import sys
import threading
import time
from datetime import UTC, datetime, time as local_time
from pathlib import Path

from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobDomain,
    ScheduledJobDefinition,
    ScheduledJobExecution,
)
from investment_analyst.core.operation_control import current_operation_control


def main() -> None:
    state_path = Path(sys.argv[1])
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = ScheduledJobDefinition(
        job_id="a-subprocess-cancellable",
        asset_id="equity:test:a",
        provider="synthetic",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=local_time(7),
        retry_backoff_seconds=60,
    )

    def run(invocation):
        del invocation
        control = current_operation_control()
        if control is None:
            raise RuntimeError("scheduler did not bind operation control")
        print("READY", flush=True)
        while True:
            control.raise_if_cancelled()
            time.sleep(0.01)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(state_path),
        clock=lambda: now,
    )
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop_event.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stop_event.set())
    scheduler.run_forever(stop_event, poll_seconds=0.01)
    state = scheduler._store.load()
    live_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread is not threading.main_thread()
    ]
    attempt = state.attempts[0]
    print(
        json.dumps(
            {
                "attempt_count": len(state.attempts),
                "status": attempt.status.value,
                "category": attempt.failure.category.value if attempt.failure else None,
                "live_non_main_threads": live_threads,
            },
            sort_keys=True,
        ),
        flush=True,
    )


main()
"""
    state_path = tmp_path / "scheduler-state.json"
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-c", runner, str(state_path)],
        cwd=tmp_path,
        env=_subprocess_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=60)
    elapsed = time.monotonic() - started

    assert process.returncode == 0, stderr
    payload = json.loads(stdout.strip().splitlines()[-1])
    assert elapsed < 60
    assert payload == {
        "attempt_count": 1,
        "category": "interrupted_job",
        "live_non_main_threads": [],
        "status": "failed",
    }
    assert "SIGKILL" not in stdout + stderr
