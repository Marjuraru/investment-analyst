"""Offline integration tests for AAPL Alpaca history through LocalStorage."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import (
    ALPACA_FETCH_RECEIPT_SCHEMA,
    AlpacaHistoricalPipeline,
    alpaca_fetch_receipt_from_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
    AlpacaStockError,
)
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_PATH = Path("tests/fixtures/alpaca/aapl_daily.json")
START = datetime(2026, 7, 7, tzinfo=UTC)
END = datetime(2026, 7, 10, tzinfo=UTC)
FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 12, 12, 1, tzinfo=UTC)


class FixtureTransport:
    """Offline transport returning queued Alpaca pages."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        return HttpResponse(status_code=200, body=self.bodies.pop(0), headers={}, url=url)


def _fixture() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _pipeline(
    storage: LocalStorage,
    bodies: list[bytes] | None = None,
) -> tuple[AlpacaHistoricalPipeline, FixtureTransport]:
    transport = FixtureTransport(bodies or [_fixture()])
    client = AlpacaStockClient(
        transport,
        AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        clock=lambda: FETCHED_AT,
    )
    return (
        AlpacaHistoricalPipeline(storage, client, clock=lambda: NORMALIZED_AT),
        transport,
    )


def test_complete_round_trip_idempotence_and_data_separation(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_pipeline, transport = _pipeline(storage)
        first = first_pipeline.run(START, END)

        assert first.asset_id == ASSET_ID
        assert first.source_id == SOURCE_ID
        assert first.bars_received == 3
        assert first.raw_records_created == 3
        assert first.raw_records_reused == 0
        assert first.observations_created == 21
        assert first.observations_reused == 0
        assert first.traceability_verified is True
        assert len(transport.calls) == 1

        records = storage.raw_records.list(source_id=SOURCE_ID)
        observations = storage.observations.list(asset_id=ASSET_ID)
        assert storage.assets.get(ASSET_ID).symbol == "AAPL"
        assert storage.sources.get(SOURCE_ID).is_official is True
        assert len(records) == 4
        assert len(observations) == 21
        bar_records = [
            record for record in records if record.schema_version != ALPACA_FETCH_RECEIPT_SCHEMA
        ]
        receipt_records = [
            record for record in records if record.schema_version == ALPACA_FETCH_RECEIPT_SCHEMA
        ]
        assert {record.record_id for record in bar_records} == {
            observation.raw_record_id for observation in observations
        }
        assert len(receipt_records) == 1
        assert all(storage.raw_records.get(record.record_id) == record for record in records)
        assert all(
            storage.observations.get(observation.observation_id) == observation
            for observation in observations
        )
        assert all(record.asset_id == ASSET_ID for record in records)
        assert all(observation.asset_id == ASSET_ID for observation in observations)
        assert all(observation.quality.value == "partial" for observation in observations)
        assert all(
            observation.period_start is None and observation.period_end is None
            for observation in observations
        )
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        raw_ids = {record.record_id for record in records}
        observation_ids = {item.observation_id for item in observations}
        second_pipeline, _ = _pipeline(storage)
        second = second_pipeline.run(START, END)

        assert second.raw_records_created == 0
        assert second.raw_records_reused == 3
        assert second.observations_created == 0
        assert second.observations_reused == 21
        assert {
            record.record_id for record in storage.raw_records.list(source_id=SOURCE_ID)
        } == raw_ids
        assert {
            item.observation_id for item in storage.observations.list(asset_id=ASSET_ID)
        } == observation_ids
        assert json.loads(json.dumps(second.to_json_dict()))["bars_received"] == 3
        assert first.earliest_bar < first.latest_bar

        for record in bar_records:
            assert record.event_time is not None
            assert record.event_time.utcoffset() == timedelta(0)
            assert record.available_at.utcoffset() == timedelta(0)
        for observation in observations:
            assert observation.observed_at is not None
            assert observation.observed_at.utcoffset() == timedelta(0)
            assert observation.available_at <= observation.normalized_at


def test_pagination_and_revised_bar_create_new_version(tmp_path: Path) -> None:
    fixture = json.loads(_fixture())
    first_page = json.dumps(
        {
            "bars": fixture["bars"][:2],
            "symbol": "AAPL",
            "next_page_token": "page-2",
        }
    ).encode()
    second_page = json.dumps(
        {
            "bars": fixture["bars"][2:],
            "symbol": "AAPL",
            "next_page_token": None,
        }
    ).encode()

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, transport = _pipeline(storage, [first_page, second_page])
        first = pipeline.run(START, END)
        assert first.request_count == 2
        assert first.bars_received == 3
        assert first.coverage_receipts_created == 1
        assert len(transport.calls) == 2
        receipt_record = next(
            record
            for record in storage.raw_records.list(source_id=SOURCE_ID)
            if record.schema_version == ALPACA_FETCH_RECEIPT_SCHEMA
        )
        receipt = alpaca_fetch_receipt_from_raw_record(receipt_record)
        assert receipt is not None
        assert receipt.page_count == 2

        revised = json.loads(_fixture())
        revised["bars"][2]["c"] = 207.45
        revised["bars"][2]["h"] = 208.20
        revised_pipeline, _ = _pipeline(storage, [json.dumps(revised).encode()])
        second = revised_pipeline.run(START, END)

        assert second.raw_records_created == 1
        assert second.raw_records_reused == 2
        assert second.observations_created == 7
        assert second.observations_reused == 14
        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 5
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 28
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []


def test_empty_interval_persists_one_deterministic_receipt_and_no_observations(
    tmp_path: Path,
) -> None:
    null_page = b'{"bars": null, "next_page_token": null}'
    empty_page = b'{"bars": [], "next_page_token": null}'
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_pipeline, _ = _pipeline(storage, [null_page])
        first = first_pipeline.run(START, END)
        receipt_records = [
            record
            for record in storage.raw_records.list(source_id=SOURCE_ID)
            if record.schema_version == ALPACA_FETCH_RECEIPT_SCHEMA
        ]
        assert first.bars_received == 0
        assert first.raw_records_created == 0
        assert first.coverage_receipts_created == 1
        assert first.empty_intervals_completed == 1
        assert storage.observations.list(asset_id=ASSET_ID) == []
        assert len(receipt_records) == 1
        receipt = alpaca_fetch_receipt_from_raw_record(receipt_records[0])
        assert receipt is not None
        assert receipt.requested_start == START
        assert receipt.requested_end == END
        assert receipt.interval_semantics == "half-open-utc"
        first_id = receipt_records[0].record_id

        second_pipeline, _ = _pipeline(storage, [empty_page])
        second = second_pipeline.run(START, END)
        current = [
            record
            for record in storage.raw_records.list(source_id=SOURCE_ID)
            if record.schema_version == ALPACA_FETCH_RECEIPT_SCHEMA
        ]
        assert second.raw_records_created == 0
        assert second.raw_records_reused == 0
        assert second.coverage_receipts_reused == 1
        assert len(current) == 1
        assert current[0].record_id == first_id
        assert storage.observations.list(asset_id=ASSET_ID) == []


def test_failed_second_page_does_not_persist_complete_receipt(tmp_path: Path) -> None:
    fixture = json.loads(_fixture())
    first_page = json.dumps(
        {
            "bars": fixture["bars"][:1],
            "symbol": "AAPL",
            "next_page_token": "page-2",
        }
    ).encode()
    malformed_second = b'{"bars": {}, "symbol": "AAPL", "next_page_token": null}'

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, _ = _pipeline(storage, [first_page, malformed_second])
        with pytest.raises(AlpacaStockError, match="must be a list"):
            pipeline.run(START, END)
        assert all(
            record.schema_version != ALPACA_FETCH_RECEIPT_SCHEMA
            for record in storage.raw_records.list(source_id=SOURCE_ID)
        )


def test_script_runs_offline_and_never_prints_credentials(tmp_path: Path) -> None:
    fixture_path = FIXTURE_PATH.resolve()
    launcher = "\n".join(
        (
            "import runpy, sys",
            "from pathlib import Path",
            "from investment_analyst.providers.http import HttpResponse",
            "import investment_analyst.providers.http as http_module",
            "class FakeTransport:",
            "    def __init__(self, *args, **kwargs): pass",
            "    def get(self, url, *, headers, timeout_seconds):",
            f"        body = Path({str(fixture_path)!r}).read_bytes()",
            "        return HttpResponse(status_code=200, body=body, headers={}, url=url)",
            "http_module.UrlLibHttpTransport = FakeTransport",
            "sys.argv = ['fetch_alpaca_history.py', *sys.argv[1:]]",
            "runpy.run_path('scripts/fetch_alpaca_history.py', run_name='__main__')",
        )
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ALPACA_API_KEY": "subprocess-key",
            "ALPACA_API_SECRET": "subprocess-secret",
            "PYTHONPATH": os.pathsep.join(
                [str(Path("src").resolve()), environment.get("PYTHONPATH", "")]
            ),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            "--root",
            str(tmp_path / "storage"),
            "--start",
            "2026-07-07",
            "--end",
            "2026-07-10",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["summary"]["bars_received"] == 3
    assert "partial Alpaca IEX" in output["notice"]
    assert "subprocess-key" not in result.stdout + result.stderr
    assert "subprocess-secret" not in result.stdout + result.stderr


def test_script_fails_cleanly_without_credentials(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("ALPACA_API_KEY", None)
    environment.pop("ALPACA_API_SECRET", None)
    environment["PYTHONPATH"] = str(Path("src").resolve())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/fetch_alpaca_history.py",
            "--root",
            str(tmp_path / "storage"),
            "--start",
            "2026-07-07",
            "--end",
            "2026-07-10",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "ALPACA_API_KEY and ALPACA_API_SECRET are required" in result.stderr
