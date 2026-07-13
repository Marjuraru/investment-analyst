"""Unit tests for point-in-time historical bar reconstruction."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import (
    ALPACA_SOURCE_ID,
    COINBASE_SOURCE_ID,
    SIMULATED_SOURCE_ID,
    get_market_bar_schema,
)
from investment_analyst.analytics.market.history_service import (
    AmbiguousRevisionError,
    ConflictingObservationError,
    HistoricalMarketDataService,
    IncompleteMarketBarError,
    TraceabilityError,
    UnsupportedMarketSourceError,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_VALUES = {
    "open": Decimal("100"),
    "high": Decimal("110"),
    "low": Decimal("90"),
    "close": Decimal("105"),
    "volume": Decimal("1000"),
    "trade_count": Decimal("200"),
    "vwap": Decimal("102"),
}


def _query(
    *,
    asset_id: str = "crypto:btc-usd",
    source_id: str = COINBASE_SOURCE_ID,
    start: datetime = datetime(2026, 7, 1, tzinfo=UTC),
    end: datetime = datetime(2026, 7, 5, tzinfo=UTC),
    known_at: datetime = datetime(2026, 7, 10, tzinfo=UTC),
) -> HistoricalBarQuery:
    return HistoricalBarQuery(
        asset_id=asset_id,
        source_id=source_id,
        start=start,
        end=end,
        known_at=known_at,
    )


def _store_version(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    timestamp: datetime,
    available_at: datetime,
    record_id: UUID | None = None,
    omit: str | None = None,
    field_overrides: dict[str, dict[str, object]] | None = None,
    payload_tag: str = "v1",
) -> tuple[RawRecord, tuple[NormalizedObservation, ...]]:
    schema = get_market_bar_schema(source_id)
    identifier = record_id or uuid4()
    reference = SourceReference(
        source_id=source_id,
        record_key=f"{asset_id}:{timestamp.isoformat()}:{payload_tag}",
        retrieved_at=available_at,
    )
    raw = RawRecord(
        record_id=identifier,
        asset_id=asset_id,
        source=reference,
        event_time=timestamp,
        available_at=available_at,
        received_at=available_at,
        payload={"version": payload_tag},
        schema_version="test-bar-v1",
    )
    storage.raw_records.save(raw)
    observations: list[NormalizedObservation] = []
    for field_name in schema.required_fields:
        if field_name == omit:
            continue
        values: dict[str, object] = {
            "observation_id": uuid4(),
            "raw_record_id": identifier,
            "asset_id": asset_id,
            "field_name": field_name,
            "value": _VALUES[field_name],
            "unit": schema.units[field_name],
            "frequency": DataFrequency.DAY_1,
            "observed_at": timestamp,
            "available_at": available_at,
            "normalized_at": available_at + timedelta(minutes=1),
            "source": reference,
            "quality": schema.expected_quality,
            "transformation_version": "test-normalizer-v1",
        }
        if field_overrides and field_name in field_overrides:
            values.update(field_overrides[field_name])
        observation = NormalizedObservation.model_validate(values)
        storage.observations.save(observation)
        observations.append(observation)
    return raw, tuple(observations)


def test_empty_query_and_unsupported_source(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = HistoricalMarketDataService(storage)
        result = service.query(_query())
        assert result.bars == ()
        assert result.coverage.bar_count == 0
        with pytest.raises(UnsupportedMarketSourceError):
            service.query(_query(source_id="unknown:source"))


@pytest.mark.parametrize(
    ("asset_id", "source_id", "expected_fields", "expected_quality"),
    [
        ("crypto:btc-usd", COINBASE_SOURCE_ID, 5, DataQuality.VALID),
        ("equity:us:aapl", ALPACA_SOURCE_ID, 7, DataQuality.PARTIAL),
        ("crypto:btc-usd", SIMULATED_SOURCE_ID, 6, DataQuality.VALID),
    ],
)
def test_complete_supported_bars(
    tmp_path,
    asset_id: str,
    source_id: str,
    expected_fields: int,
    expected_quality: DataQuality,
) -> None:
    timestamp = datetime(2026, 7, 2, tzinfo=UTC)
    available = datetime(2026, 7, 3, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        raw, _ = _store_version(
            storage,
            asset_id=asset_id,
            source_id=source_id,
            timestamp=timestamp,
            available_at=available,
        )
        result = HistoricalMarketDataService(storage).query(
            _query(asset_id=asset_id, source_id=source_id)
        )

    assert len(result.bars) == 1
    assert result.bars[0].raw_record_id == raw.record_id
    assert len(result.bars[0].observation_ids) == expected_fields
    assert result.bars[0].quality is expected_quality
    assert result.traceability_verified


def test_asset_source_and_end_exclusive_filters(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=datetime(2026, 7, 2, tzinfo=UTC),
            available_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
        _store_version(
            storage,
            asset_id="equity:us:aapl",
            source_id=ALPACA_SOURCE_ID,
            timestamp=datetime(2026, 7, 2, tzinfo=UTC),
            available_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
        _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=datetime(2026, 7, 5, tzinfo=UTC),
            available_at=datetime(2026, 7, 6, tzinfo=UTC),
        )
        result = HistoricalMarketDataService(storage).query(_query())

    assert [bar.timestamp for bar in result.bars] == [datetime(2026, 7, 2, tzinfo=UTC)]
    assert all(bar.source_id == COINBASE_SOURCE_ID for bar in result.bars)


@pytest.mark.parametrize(
    ("override", "error_type"),
    [
        ({"open": {"unit": "EUR"}}, ConflictingObservationError),
        ({"open": {"quality": DataQuality.PARTIAL}}, ConflictingObservationError),
        (
            {"open": {"observed_at": datetime(2026, 7, 3, tzinfo=UTC)}},
            TraceabilityError,
        ),
    ],
)
def test_rejects_inconsistent_observations(tmp_path, override, error_type) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=datetime(2026, 7, 2, tzinfo=UTC),
            available_at=datetime(2026, 7, 3, tzinfo=UTC),
            field_overrides=override,
        )
        with pytest.raises(error_type):
            HistoricalMarketDataService(storage).query(_query())


def test_rejects_duplicate_unknown_and_incomplete_fields(tmp_path) -> None:
    timestamp = datetime(2026, 7, 2, tzinfo=UTC)
    available = datetime(2026, 7, 3, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path / "duplicate")) as storage:
        raw, observations = _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=available,
        )
        duplicate = observations[0].model_copy(update={"observation_id": uuid4()})
        storage.observations.save(duplicate)
        with pytest.raises(ConflictingObservationError, match="duplicate"):
            HistoricalMarketDataService(storage).query(_query())

    with LocalStorage(StoragePaths.from_root(tmp_path / "unknown")) as storage:
        raw, _ = _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=available,
        )
        storage.observations.save(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw.record_id,
                asset_id="crypto:btc-usd",
                field_name="bid",
                value=Decimal("99"),
                unit="USD",
                frequency=DataFrequency.DAY_1,
                observed_at=timestamp,
                available_at=available,
                normalized_at=available + timedelta(minutes=1),
                source=raw.source,
                quality=DataQuality.VALID,
                transformation_version="test-normalizer-v1",
            )
        )
        with pytest.raises(ConflictingObservationError, match="unsupported field"):
            HistoricalMarketDataService(storage).query(_query())

    with LocalStorage(StoragePaths.from_root(tmp_path / "incomplete")) as storage:
        _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=available,
            omit="close",
        )
        with pytest.raises(IncompleteMarketBarError, match="close"):
            HistoricalMarketDataService(storage).query(_query())


def test_raw_record_traceability_is_verified(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        raw, observations = _store_version(
            storage,
            asset_id="wrong:asset",
            source_id=COINBASE_SOURCE_ID,
            timestamp=datetime(2026, 7, 2, tzinfo=UTC),
            available_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
        for observation in observations:
            replacement = observation.model_copy(
                update={"observation_id": uuid4(), "asset_id": "crypto:btc-usd"}
            )
            storage.observations.save(replacement)
        with pytest.raises(TraceabilityError, match="asset_id"):
            HistoricalMarketDataService(storage).query(_query())


def test_point_in_time_selects_latest_eligible_revision(tmp_path) -> None:
    timestamp = datetime(2026, 7, 2, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        old_raw, _ = _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=datetime(2026, 7, 3, tzinfo=UTC),
            payload_tag="old",
        )
        new_raw, _ = _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=datetime(2026, 7, 4, tzinfo=UTC),
            payload_tag="new",
        )
        future_raw, _ = _store_version(
            storage,
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            timestamp=timestamp,
            available_at=datetime(2026, 7, 8, tzinfo=UTC),
            payload_tag="future",
        )
        service = HistoricalMarketDataService(storage)
        early = service.query(_query(known_at=datetime(2026, 7, 3, 12, tzinfo=UTC)))
        later = service.query(_query(known_at=datetime(2026, 7, 5, tzinfo=UTC)))

    assert early.bars[0].raw_record_id == old_raw.record_id
    assert early.coverage.candidate_versions == 1
    assert later.bars[0].raw_record_id == new_raw.record_id
    assert later.bars[0].raw_record_id != future_raw.record_id
    assert later.coverage.discarded_revisions == 1


def test_equal_available_revision_is_ambiguous(tmp_path) -> None:
    timestamp = datetime(2026, 7, 2, tzinfo=UTC)
    available = datetime(2026, 7, 3, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        for tag in ("one", "two"):
            _store_version(
                storage,
                asset_id="crypto:btc-usd",
                source_id=COINBASE_SOURCE_ID,
                timestamp=timestamp,
                available_at=available,
                payload_tag=tag,
            )
        with pytest.raises(AmbiguousRevisionError):
            HistoricalMarketDataService(storage).query(_query())


def test_result_order_is_deterministic(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        for day in (4, 2, 3):
            _store_version(
                storage,
                asset_id="crypto:btc-usd",
                source_id=COINBASE_SOURCE_ID,
                timestamp=datetime(2026, 7, day, tzinfo=UTC),
                available_at=datetime(2026, 7, day, 1, tzinfo=UTC),
            )
        result = HistoricalMarketDataService(storage).query(_query())

    assert [bar.timestamp.day for bar in result.bars] == [2, 3, 4]
