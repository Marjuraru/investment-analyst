"""Offline integration tests for append-only Coinbase minute ingestion."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import SOURCE_ID
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseIntradayPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_PATH = Path("tests/fixtures/coinbase/btc_usd_minute.json")
START = datetime(2026, 7, 12, 11, 55, tzinfo=UTC)
END = datetime(2026, 7, 12, 11, 58, tzinfo=UTC)
FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 12, 12, 1, tzinfo=UTC)


class FixtureTransport:
    """Offline transport returning one Coinbase-shaped minute response."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(
            status_code=200,
            body=FIXTURE_PATH.read_bytes(),
            headers={},
            url=url,
        )


def _pipeline(
    storage: LocalStorage,
) -> tuple[CoinbaseIntradayPipeline, FixtureTransport]:
    transport = FixtureTransport()
    client = CoinbaseExchangeClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: FETCHED_AT,
    )
    return (
        CoinbaseIntradayPipeline(
            storage,
            client,
            clock=lambda: NORMALIZED_AT,
        ),
        transport,
    )


def test_intraday_pipeline_round_trip_and_idempotence(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_pipeline, transport = _pipeline(storage)
        first = first_pipeline.run(START, END)

        assert first.asset_id == ASSET_ID
        assert first.source_id == SOURCE_ID
        assert first.request_count == 1
        assert first.candles_received == 3
        assert first.raw_records_created == 3
        assert first.observations_created == 15
        assert first.missing_intervals == ()
        assert first.traceability_verified
        assert "granularity=60" in transport.calls[0]

        records = storage.raw_records.list(source_id=SOURCE_ID)
        observations = [
            item
            for item in storage.observations.list(
                asset_id=ASSET_ID,
                frequency=DataFrequency.MINUTE_1,
            )
            if item.source.source_id == SOURCE_ID
        ]
        first_record_ids = {record.record_id for record in records}
        first_observation_ids = {item.observation_id for item in observations}

        assert len(records) == 3
        assert len(observations) == 15
        assert {record.payload["granularity_seconds"] for record in records} == {60}
        assert all(
            observation.period_start is not None
            and observation.period_end == observation.period_start + timedelta(minutes=1)
            for observation in observations
        )

        second_pipeline, _ = _pipeline(storage)
        second = second_pipeline.run(START, END)

        assert second.raw_records_created == 0
        assert second.raw_records_reused == 3
        assert second.observations_created == 0
        assert second.observations_reused == 15
        assert {
            record.record_id for record in storage.raw_records.list(source_id=SOURCE_ID)
        } == first_record_ids
        assert {
            observation.observation_id
            for observation in storage.observations.list(
                asset_id=ASSET_ID,
                frequency=DataFrequency.MINUTE_1,
            )
            if observation.source.source_id == SOURCE_ID
        } == first_observation_ids
