"""Offline integration tests for Coinbase history through LocalStorage."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_PATH = Path("tests/fixtures/coinbase/btc_usd_daily.json")
START = datetime(2026, 7, 9, tzinfo=UTC)
END = datetime(2026, 7, 12, tzinfo=UTC)
FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 12, 12, 1, tzinfo=UTC)


class FixtureTransport:
    """Offline transport that returns a supplied Coinbase-shaped document."""

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
        return HttpResponse(status_code=200, body=self.body, headers={}, url=url)


def _pipeline(
    storage: LocalStorage, body: bytes
) -> tuple[CoinbaseHistoricalPipeline, FixtureTransport]:
    transport = FixtureTransport(body)
    client = CoinbaseExchangeClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: FETCHED_AT,
    )
    return (
        CoinbaseHistoricalPipeline(
            storage,
            client,
            clock=lambda: NORMALIZED_AT,
        ),
        transport,
    )


def _fixture() -> bytes:
    return FIXTURE_PATH.read_bytes()


def test_complete_pipeline_round_trip_and_idempotence(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        first_pipeline, first_transport = _pipeline(storage, _fixture())
        first = first_pipeline.run(START, END)

        assert first.asset_id == ASSET_ID
        assert first.source_id == SOURCE_ID
        assert first.candles_received == 3
        assert first.raw_records_created == 3
        assert first.raw_records_reused == 0
        assert first.observations_created == 15
        assert first.observations_reused == 0
        assert first.missing_intervals == ()
        assert first.traceability_verified is True
        assert len(first_transport.calls) == 1

        asset = storage.assets.get(ASSET_ID)
        source = storage.sources.get(SOURCE_ID)
        records = storage.raw_records.list(source_id=SOURCE_ID)
        observations = storage.observations.list(asset_id=ASSET_ID)

        assert asset.symbol == "BTC"
        assert source.is_official is True
        assert len(records) == 3
        assert len(observations) == 15
        assert {record.record_id for record in records} == {
            observation.raw_record_id for observation in observations
        }
        assert all(storage.raw_records.get(record.record_id) == record for record in records)
        assert all(
            storage.observations.get(observation.observation_id) == observation
            for observation in observations
        )
        assert all(record.asset_id == ASSET_ID for record in records)
        assert all(observation.asset_id == ASSET_ID for observation in observations)
        assert all(observation.field_name != "trade_count" for observation in observations)
        assert storage.metric_definitions.list_all() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        first_raw_ids = {record.record_id for record in records}
        first_observation_ids = {item.observation_id for item in observations}
        second_pipeline, _ = _pipeline(storage, _fixture())
        second = second_pipeline.run(START, END)
        second_records = storage.raw_records.list(source_id=SOURCE_ID)
        second_observations = storage.observations.list(asset_id=ASSET_ID)

        assert second.raw_records_created == 0
        assert second.raw_records_reused == 3
        assert second.observations_created == 0
        assert second.observations_reused == 15
        assert {record.record_id for record in second_records} == first_raw_ids
        assert {item.observation_id for item in second_observations} == first_observation_ids
        assert json.loads(json.dumps(second.to_json_dict()))["candles_received"] == 3

        for record in second_records:
            assert record.event_time is not None
            assert record.event_time.utcoffset() == timedelta(0)
            assert record.available_at.utcoffset() == timedelta(0)
        for observation in second_observations:
            assert observation.observed_at is not None
            assert observation.period_start is not None
            assert observation.period_end is not None
            assert observation.observed_at.utcoffset() == timedelta(0)
            assert observation.period_end - observation.period_start == timedelta(days=1)


def test_reports_missing_intervals_without_filling_them(tmp_path: Path) -> None:
    rows = json.loads(_fixture())
    rows = [row for row in rows if row[0] != 1783641600]
    body = json.dumps(rows).encode()

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline, _ = _pipeline(storage, body)
        summary = pipeline.run(START, END)

        assert summary.candles_received == 2
        assert summary.raw_records_created == 2
        assert summary.observations_created == 10
        assert summary.missing_intervals == (datetime(2026, 7, 10, tzinfo=UTC),)
        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 2
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 10


def test_revised_candle_creates_new_historical_version(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        original_pipeline, _ = _pipeline(storage, _fixture())
        original_pipeline.run(START, END)

        revised_body = _fixture().replace(
            b"[1783641600, 106500.00, 109200.50, 107100.00, 108750.25, 14110.500]",
            b"[1783641600, 106500.00, 109500.50, 107100.00, 108900.25, 14110.500]",
        )
        revised_pipeline, _ = _pipeline(storage, revised_body)
        summary = revised_pipeline.run(START, END)

        assert summary.raw_records_created == 1
        assert summary.raw_records_reused == 2
        assert summary.observations_created == 5
        assert summary.observations_reused == 10
        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 4
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 20
