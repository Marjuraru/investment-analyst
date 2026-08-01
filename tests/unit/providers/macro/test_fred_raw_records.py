"""Tests for FRED source identity and immutable raw-record conversion."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from investment_analyst.core.models import SourceType
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.macro.fred_alfred import (
    FredAlfredClient,
    FredApiKey,
)
from investment_analyst.providers.macro.fred_raw_records import (
    FRED_VINTAGE_SCHEMA,
    create_fred_source,
    fred_vintage_to_raw_record,
    stored_fred_vintage_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths
from investment_analyst.storage.serialization import canonical_json_text

FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_bytes()
API_KEY = "b" * 32
FIRST_RETRIEVAL = datetime(2020, 3, 1, 12, tzinfo=UTC)
LATER_RETRIEVAL = datetime(2020, 3, 2, 12, tzinfo=UTC)


class FixtureTransport:
    """Offline FRED transport."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        return HttpResponse(200, FIXTURE, {}, url)


def _record(retrieved_at: datetime):
    fetch = FredAlfredClient(
        FixtureTransport(),
        FredApiKey(API_KEY),
        clock=lambda: retrieved_at,
    ).fetch_vintage_snapshot(
        "GDP",
        vintage_date=date(2020, 1, 15),
        observation_start=date(2019, 1, 1),
        observation_end=date(2020, 1, 1),
    )
    return fred_vintage_to_raw_record(fetch)


def test_source_is_official_macro_and_series_specific() -> None:
    source = create_fred_source("GDP")

    assert source.source_id == "fred-alfred:series:gdp:vintage-observations:lin"
    assert source.source_type is SourceType.MACRO
    assert source.is_official is True
    assert "units=lin" in source.coverage_notes
    assert "next UTC day" in source.coverage_notes


def test_record_is_macro_only_deterministic_and_secret_free() -> None:
    first = _record(FIRST_RETRIEVAL)
    later = _record(LATER_RETRIEVAL)

    assert first.record_id == later.record_id
    assert first.asset_id is None
    assert first.event_time is None
    assert first.available_at == datetime(2020, 1, 16, tzinfo=UTC)
    assert first.received_at == FIRST_RETRIEVAL
    assert first.schema_version == FRED_VINTAGE_SCHEMA
    assert first.source.checksum_sha256 is not None
    assert first.source.raw_uri is not None
    assert "api_key" not in first.source.raw_uri
    assert API_KEY not in canonical_json_text(first)


def test_current_day_vintage_uses_actual_retrieval_time() -> None:
    retrieved_at = datetime(2020, 1, 15, 17, 30, tzinfo=UTC)
    record = _record(retrieved_at)

    assert record.available_at == retrieved_at


def test_conversion_rejects_a_secret_bearing_persistence_url() -> None:
    fetch = FredAlfredClient(
        FixtureTransport(),
        FredApiKey(API_KEY),
        clock=lambda: FIRST_RETRIEVAL,
    ).fetch_vintage_snapshot(
        "GDP",
        vintage_date=date(2020, 1, 15),
        observation_start=date(2019, 1, 1),
        observation_end=date(2020, 1, 1),
    )

    with pytest.raises(StorageError, match="unsafe"):
        fred_vintage_to_raw_record(
            replace(fetch, public_request_url=f"{fetch.public_request_url}&api_key={API_KEY}")
        )


def test_record_storage_round_trip_and_semantic_verification(tmp_path: Path) -> None:
    record = _record(FIRST_RETRIEVAL)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(record)
        stored = storage.raw_records.get(record.record_id)

    verified = stored_fred_vintage_from_raw_record(stored)
    assert verified.metadata.series_id == "GDP"
    assert verified.metadata.vintage_date == date(2020, 1, 15)
    assert len(verified.response.observations) == 2


def test_semantic_payload_tampering_is_detected() -> None:
    record = _record(FIRST_RETRIEVAL)
    assert isinstance(record.payload, dict)
    observations = record.payload["observations"]
    assert isinstance(observations, list)
    first = observations[0]
    assert isinstance(first, dict)
    altered_payload = {
        **record.payload,
        "observations": [{**first, "value": "999.125"}, observations[1]],
    }

    with pytest.raises(StorageError, match="payload checksum"):
        stored_fred_vintage_from_raw_record(record.model_copy(update={"payload": altered_payload}))
