"""Tests for the bounded catalog-driven FRED refresh planner."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.macro.fred_alfred import FredAlfredClient, FredApiKey
from investment_analyst.providers.macro.fred_catalog import (
    FRED_SERIES_CATALOG,
    fred_catalog_entry,
)
from investment_analyst.providers.macro.fred_catalog_refresh import (
    FredCatalogRefreshRequest,
    FredCatalogRefreshService,
)
from investment_analyst.providers.macro.fred_raw_records import fred_source_id
from investment_analyst.storage import LocalStorage, StoragePaths

API_KEY = "f" * 32
RETRIEVED_AT = datetime(2020, 2, 1, 12, tzinfo=UTC)


class QueueTransport:
    """Return exact provider documents in request order."""

    def __init__(self, bodies: tuple[bytes, ...]) -> None:
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        self.calls.append(url)
        return HttpResponse(200, self.bodies.pop(0), {}, url)


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _observation_body() -> bytes:
    payload = json.loads(
        Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_text(encoding="utf-8")
    )
    payload["observation_start"] = "1947-01-01"
    payload["observation_end"] = "2020-01-15"
    return _body(payload)


def test_catalog_keeps_high_volume_daily_series_explicitly_deferred() -> None:
    automated = {item.series_id for item in FRED_SERIES_CATALOG.automated_entries()}

    assert {"CPIAUCSL", "FEDFUNDS", "GDPC1", "M2SL", "TOTALSL", "UNRATE"} <= automated
    assert {"DCOILWTICO", "DTWEXBGS", "T10Y2Y"}.isdisjoint(automated)
    with pytest.raises(ValueError, match="disabled"):
        FredCatalogRefreshRequest(
            series_id="DCOILWTICO",
            run_date=date(2020, 1, 15),
        )


def test_initial_latest_snapshot_then_incremental_check_is_idempotent(
    tmp_path: Path,
) -> None:
    transport = QueueTransport(
        (
            _body(
                {
                    "realtime_start": "1947-01-01",
                    "realtime_end": "2020-01-15",
                    "order_by": "vintage_date",
                    "sort_order": "desc",
                    "count": 2,
                    "offset": 0,
                    "limit": 1,
                    "vintage_dates": ["2020-01-15"],
                }
            ),
            _observation_body(),
            _body(
                {
                    "realtime_start": "1947-01-01",
                    "realtime_end": "2020-01-15",
                    "order_by": "vintage_date",
                    "sort_order": "desc",
                    "count": 2,
                    "offset": 0,
                    "limit": 1,
                    "vintage_dates": ["2020-01-15"],
                }
            ),
            _body(
                {
                    "realtime_start": "1947-01-01",
                    "realtime_end": "2020-01-31",
                    "order_by": "vintage_date",
                    "sort_order": "desc",
                    "count": 2,
                    "offset": 0,
                    "limit": 1,
                    "vintage_dates": ["2020-01-15"],
                }
            ),
        )
    )
    client = FredAlfredClient(
        transport,
        FredApiKey(API_KEY),
        clock=lambda: RETRIEVED_AT,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = FredCatalogRefreshService(storage, client)
        first = service.run(
            FredCatalogRefreshRequest(
                series_id="GDPC1",
                run_date=date(2020, 1, 15),
            )
        )
        same_day = service.run(
            FredCatalogRefreshRequest(
                series_id="GDPC1",
                run_date=date(2020, 1, 15),
            )
        )
        second = service.run(
            FredCatalogRefreshRequest(
                series_id="GDPC1",
                run_date=date(2020, 1, 31),
            )
        )
        records = storage.raw_records.list(source_id=fred_source_id("GDPC1"))

    assert fred_catalog_entry("GDPC1").data_frequency == "quarterly"
    assert first.bootstrap_latest_only is True
    assert first.selected_vintage_dates == (date(2020, 1, 15),)
    assert first.raw_records_created == 1
    assert first.historical_backfill_pending is True
    assert first.update_coverage_complete is True
    assert same_day.discovery_start == date(2020, 1, 15)
    assert same_day.discovery_end == date(2020, 1, 15)
    assert same_day.selected_vintage_dates == ()
    assert same_day.raw_records_created == 0
    assert same_day.update_coverage_complete is True
    assert second.bootstrap_latest_only is False
    assert second.discovery_start == date(2020, 1, 16)
    assert second.selected_vintage_dates == ()
    assert second.raw_records_created == 0
    assert second.update_coverage_complete is True
    assert len(records) == 1
    assert len(transport.calls) == 4
    second_query = parse_qs(urlsplit(transport.calls[-1]).query)
    assert second_query["realtime_start"] == ["1947-01-01"]
    assert second_query["sort_order"] == ["desc"]
    assert second_query["limit"] == ["1"]
    assert all(API_KEY not in (record.source.raw_uri or "") for record in records)
