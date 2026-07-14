"""Tests for read-only SEC point-in-time revision selection."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    AmbiguousSecFundamentalRevisionError,
    MalformedSecFundamentalObservationError,
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.fundamentals.sec_query_models import SecFundamentalQuery


class _ObservationRepository:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self._observations = observations
        self.calls = 0

    def list(
        self,
        *,
        asset_id: str | None = None,
        available_from: datetime | None = None,
        available_to: datetime | None = None,
    ) -> list[NormalizedObservation]:
        self.calls += 1
        assert available_from is None
        assert available_to is None
        return [
            item for item in self._observations if asset_id is None or item.asset_id == asset_id
        ]


class _ForbiddenRawRepository:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"raw repository access is forbidden: {name}")


class _Storage:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self.observations = _ObservationRepository(observations)
        self.raw_records = _ForbiddenRawRepository()
        self.is_open = True

    def require_open(self) -> None:
        assert self.is_open


def _observation(
    *,
    field_name: str = "fundamental.revenue",
    value: str = "100",
    period_end: date = date(2025, 9, 27),
    available_at: datetime = datetime(2025, 10, 31, 20, tzinfo=UTC),
    normalized_at: datetime = datetime(2025, 11, 1, tzinfo=UTC),
    accession: str = "0000320193-25-000001",
    frequency: DataFrequency = DataFrequency.ANNUAL,
    source_id: str = COMPANYFACTS_SOURCE_ID,
    transformation_version: str = TRANSFORMATION_VERSION,
    quality: DataQuality = DataQuality.VALID,
    raw_record_id: UUID | None = None,
    record_key_updates: dict[str, object] | None = None,
) -> NormalizedObservation:
    raw_id = raw_record_id or uuid4()
    instant = field_name in {
        "fundamental.assets",
        "fundamental.liabilities",
        "fundamental.stockholders_equity",
    }
    duration_days = 89 if frequency is DataFrequency.QUARTERLY else 363
    period_start = (
        None
        if instant
        else datetime.combine(
            period_end - timedelta(days=duration_days),
            datetime.min.time(),
            tzinfo=UTC,
        )
    )
    effective_normalized_at = max(normalized_at, available_at)
    key: dict[str, object] = {
        "accession_number": accession,
        "taxonomy": "us-gaap",
        "tag": {
            "fundamental.revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "fundamental.net_income": "NetIncomeLoss",
            "fundamental.assets": "Assets",
            "fundamental.liabilities": "Liabilities",
            "fundamental.stockholders_equity": "StockholdersEquity",
        }.get(field_name, "Unknown"),
        "unit": "USD",
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_id),
        "submissions_record_id": str(uuid4()),
        "form": "10-K" if frequency is DataFrequency.ANNUAL else "10-Q",
        "fiscal_year": "2025",
        "fiscal_period": "FY" if frequency is DataFrequency.ANNUAL else "Q3",
    }
    if record_key_updates:
        key.update(record_key_updates)
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_id,
        asset_id=ASSET_ID,
        field_name=field_name,
        value=Decimal(value),
        unit="USD",
        frequency=frequency,
        observed_at=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        period_start=period_start,
        period_end=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        available_at=available_at,
        normalized_at=effective_normalized_at,
        source=SourceReference(
            source_id=source_id,
            record_key=json.dumps(key, separators=(",", ":"), sort_keys=True),
            retrieved_at=datetime(2025, 11, 1, tzinfo=UTC),
        ),
        quality=quality,
        transformation_version=transformation_version,
    )


def _query(
    known_at: datetime,
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> SecFundamentalQuery:
    return SecFundamentalQuery(
        known_at=known_at,
        frequency=frequency,
        start_period_end=start,
        end_period_end=end,
        limit=limit,
    )


def test_query_is_inclusive_and_selects_amendment_point_in_time() -> None:
    original = _observation(value="100")
    amendment_time = original.available_at + timedelta(days=10)
    amendment = _observation(
        value="105",
        available_at=amendment_time,
        accession="0000320193-25-000002",
    )
    storage = _Storage([original, amendment])
    service = SecAaplFundamentalPointInTimeService(storage)  # type: ignore[arg-type]

    before = service.query(_query(amendment_time - timedelta(seconds=1)))
    at = service.query(_query(amendment_time))
    after = service.query(_query(amendment_time + timedelta(days=1)))

    assert before.periods[0].facts[0].value == Decimal("100")
    assert at.periods[0].facts[0].value == Decimal("105")
    assert after.periods[0].facts[0].value == Decimal("105")
    assert at.periods[0].facts[0].superseded_count == 1
    assert storage.observations.calls == 3


def test_normalized_after_known_at_does_not_exclude_public_fact() -> None:
    observation = _observation(
        normalized_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    storage = _Storage([observation])

    result = SecAaplFundamentalPointInTimeService(storage).query(  # type: ignore[arg-type]
        _query(observation.available_at)
    )

    assert result.observations_selected == 1


def test_frequency_range_partial_period_and_limit() -> None:
    observations = [
        _observation(period_end=date(2024, 9, 28), value="80"),
        _observation(period_end=date(2025, 9, 27), value="100"),
        _observation(
            field_name="fundamental.assets",
            period_end=date(2025, 9, 27),
            value="200",
        ),
        _observation(
            period_end=date(2025, 6, 28),
            value="25",
            frequency=DataFrequency.QUARTERLY,
        ),
    ]
    storage = _Storage(observations)
    service = SecAaplFundamentalPointInTimeService(storage)  # type: ignore[arg-type]

    annual = service.query(
        _query(
            datetime(2026, 1, 1, tzinfo=UTC),
            start=date(2025, 1, 1),
            limit=1,
        )
    )
    quarterly = service.query(
        _query(datetime(2026, 1, 1, tzinfo=UTC), frequency=DataFrequency.QUARTERLY)
    )

    assert [period.period_end.date() for period in annual.periods] == [date(2025, 9, 27)]
    assert annual.periods[0].available_fields == (
        "fundamental.assets",
        "fundamental.revenue",
    )
    assert not annual.periods[0].is_complete
    assert quarterly.periods[0].facts[0].frequency is DataFrequency.QUARTERLY


def test_market_foreign_quality_and_transformation_observations_are_ignored() -> None:
    valid = _observation()
    wrong_source = _observation(source_id="simulated:daily-bars")
    wrong_quality = _observation(quality=DataQuality.PARTIAL)
    wrong_version = _observation(transformation_version="other-version")
    market = _observation(field_name="close", source_id="simulated:daily-bars")
    storage = _Storage([valid, wrong_source, wrong_quality, wrong_version, market])

    result = SecAaplFundamentalPointInTimeService(storage).query(  # type: ignore[arg-type]
        _query(datetime(2026, 1, 1, tzinfo=UTC))
    )

    assert result.observations_examined == 5
    assert result.observations_eligible == 1
    assert result.observations_selected == 1


def test_identical_duplicate_collapses_and_conflicting_tie_fails() -> None:
    original = _observation()
    identical = original.model_copy()
    storage = _Storage([original, identical])
    service = SecAaplFundamentalPointInTimeService(storage)  # type: ignore[arg-type]

    result = service.query(_query(datetime(2026, 1, 1, tzinfo=UTC)))
    assert result.observations_selected == 1

    conflict = _observation(value="101", available_at=original.available_at)
    conflict_storage = _Storage([original, conflict])
    with pytest.raises(AmbiguousSecFundamentalRevisionError):
        SecAaplFundamentalPointInTimeService(conflict_storage).query(  # type: ignore[arg-type]
            _query(datetime(2026, 1, 1, tzinfo=UTC))
        )


def test_malformed_record_keys_and_contradictions_are_rejected() -> None:
    malformed = _observation().model_copy(
        update={
            "source": SourceReference(
                source_id=COMPANYFACTS_SOURCE_ID,
                record_key="not-json",
                retrieved_at=datetime(2025, 11, 1, tzinfo=UTC),
            )
        }
    )
    storage = _Storage([malformed])
    with pytest.raises(MalformedSecFundamentalObservationError):
        SecAaplFundamentalPointInTimeService(storage).query(  # type: ignore[arg-type]
            _query(datetime(2026, 1, 1, tzinfo=UTC))
        )

    contradiction = _observation(record_key_updates={"period": "2024-01-01"})
    with pytest.raises(MalformedSecFundamentalObservationError):
        SecAaplFundamentalPointInTimeService(_Storage([contradiction])).query(  # type: ignore[arg-type]
            _query(datetime(2026, 1, 1, tzinfo=UTC))
        )


def test_service_reads_observations_once_and_never_accesses_raw_records() -> None:
    storage = _Storage([_observation()])

    result = SecAaplFundamentalPointInTimeService(storage).query(  # type: ignore[arg-type]
        _query(datetime(2026, 1, 1, tzinfo=UTC))
    )

    assert result.traceability_verified
    assert storage.observations.calls == 1
