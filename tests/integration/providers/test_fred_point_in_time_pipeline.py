"""Offline integration tests for FRED vintage persistence and reconstruction."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.macro.fred_alfred import FredAlfredClient, FredApiKey
from investment_analyst.providers.macro.fred_pipeline import FredVintagePipeline
from investment_analyst.providers.macro.fred_point_in_time import (
    AmbiguousFredRevisionError,
    FredPointInTimeQuery,
    FredPointInTimeService,
)
from investment_analyst.providers.macro.fred_raw_records import fred_source_id
from investment_analyst.storage import LocalStorage, StoragePaths

FIRST_FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_bytes()
SECOND_FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-02-15.json").read_bytes()
API_KEY = "c" * 32
START = date(2019, 1, 1)
END = date(2020, 1, 1)


class FixtureTransport:
    """Offline transport for one complete vintage response."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        return HttpResponse(200, self.body, {}, url)


def _pipeline(storage: LocalStorage, body: bytes, retrieved_at: datetime):
    client = FredAlfredClient(
        FixtureTransport(body),
        FredApiKey(API_KEY),
        clock=lambda: retrieved_at,
    )
    return FredVintagePipeline(storage, client)


def _import_first(storage: LocalStorage):
    return _pipeline(
        storage,
        FIRST_FIXTURE,
        datetime(2020, 3, 1, 12, tzinfo=UTC),
    ).run(
        "GDP",
        vintage_date=date(2020, 1, 15),
        observation_start=START,
        observation_end=END,
    )


def _import_second(storage: LocalStorage, body: bytes = SECOND_FIXTURE):
    return _pipeline(
        storage,
        body,
        datetime(2020, 3, 1, 13, tzinfo=UTC),
    ).run(
        "GDP",
        vintage_date=date(2020, 2, 15),
        observation_start=START,
        observation_end=END,
    )


def _query(storage: LocalStorage, known_at: datetime):
    return FredPointInTimeService(storage).query(
        FredPointInTimeQuery(series_id="GDP", known_at=known_at)
    )


def test_pipeline_is_idempotent_isolated_and_point_in_time(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first = _import_first(storage)
        second = _import_second(storage)
        repeated = _import_first(storage)

        assert first.raw_records_created == 1
        assert first.raw_records_reused == 0
        assert first.observations_received == 2
        assert first.values_received == 1
        assert first.missing_values_received == 1
        assert second.raw_records_created == 1
        assert repeated.raw_records_created == 0
        assert repeated.raw_records_reused == 1
        assert len(storage.raw_records.list(source_id=fred_source_id("GDP"))) == 2
        assert storage.assets.list_all() == []
        assert storage.observations.list() == []
        assert storage.metric_definitions.list_all() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        before_first_availability = _query(storage, datetime(2020, 1, 15, 23, 59, tzinfo=UTC))
        first_vintage = _query(storage, datetime(2020, 1, 16, tzinfo=UTC))
        second_vintage = _query(storage, datetime(2020, 2, 16, tzinfo=UTC))

        assert before_first_availability.observations == ()
        assert [item.value for item in first_vintage.observations] == [
            Decimal("100.125"),
            None,
        ]
        assert [item.vintage_date for item in first_vintage.observations] == [
            date(2020, 1, 15),
            date(2020, 1, 15),
        ]
        assert [item.value for item in second_vintage.observations] == [
            Decimal("101.250"),
            Decimal("105.5"),
        ]
        assert second_vintage.revisions_superseded == 2
        assert second_vintage.traceability_verified is True


def test_query_range_is_inclusive(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _import_second(storage)
        result = FredPointInTimeService(storage).query(
            FredPointInTimeQuery(
                series_id="GDP",
                known_at=datetime(2020, 2, 16, tzinfo=UTC),
                observation_start=date(2020, 1, 1),
                observation_end=date(2020, 1, 1),
            )
        )

    assert len(result.observations) == 1
    assert result.observations[0].observation_date == date(2020, 1, 1)
    assert result.observations[0].value == Decimal("105.5")


def test_conflicting_same_vintage_revisions_fail_explicitly(tmp_path: Path) -> None:
    conflicting = SECOND_FIXTURE.replace(b'"101.250"', b'"999.250"')
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _import_second(storage)
        _import_second(storage, conflicting)

        with pytest.raises(AmbiguousFredRevisionError, match="conflicting GDP"):
            _query(storage, datetime(2020, 2, 16, tzinfo=UTC))
