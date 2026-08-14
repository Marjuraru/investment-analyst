"""Normalization, identity, units, and PIT tests for Deribit evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_normalizer import (
    dvol_to_observations,
    dvol_to_raw_record,
    funding_to_observations,
    funding_to_raw_record,
    summary_to_observations,
    summary_to_raw_record,
)
from investment_analyst.providers.http import HttpResponse

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_RECEIVED = datetime(2026, 8, 4, 2, tzinfo=UTC)


class _Transport:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        return HttpResponse(status_code=200, body=self._body, headers={}, url=url)


def _configuration():
    resolver = ProviderAssetContextResolver(AssetCatalogService.load_default())
    return resolve_deribit_configuration(resolver, asset_id="crypto:btc-usd")


def test_summary_raw_last_maps_explicitly_to_last_price_observation() -> None:
    client = DeribitClient(
        _Transport((_FIXTURES / "btc_perpetual_summary.json").read_bytes()),
        sleep=lambda _: None,
        clock=lambda: _RECEIVED,
    )
    fetch = client.fetch_perpetual_summary("BTC-PERPETUAL")
    configuration = _configuration()
    raw = summary_to_raw_record(
        fetch.summary,
        configuration=configuration,
        received_at=fetch.retrieved_at,
        request_url=fetch.request_url,
    )

    observations = summary_to_observations(
        fetch.summary,
        raw,
        configuration=configuration,
        normalized_at=_RECEIVED + timedelta(seconds=1),
    )
    by_field = {item.field_name: item for item in observations}

    assert raw.payload["last"] == "100005"
    assert "last_price" not in raw.payload
    assert by_field["last_price"].value == fetch.summary.last_price
    assert "last" not in by_field
    assert by_field["current_funding"].value == fetch.summary.current_funding
    assert by_field["funding_8h"].value == fetch.summary.funding_8h
    assert "funding_interest_1h" not in by_field
    assert "funding_interest_8h" not in by_field
    assert all(item.available_at == _RECEIVED for item in observations)
    assert all(item.raw_record_id == raw.record_id for item in observations)


def test_historical_funding_periods_remain_distinct_from_snapshot_fields() -> None:
    client = DeribitClient(
        _Transport((_FIXTURES / "btc_funding_history.json").read_bytes()),
        sleep=lambda _: None,
        clock=lambda: _RECEIVED,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    fetch = client.fetch_funding_history(
        "BTC-PERPETUAL",
        start,
        start + timedelta(days=1),
    )
    configuration = _configuration()
    point = fetch.points[0]
    raw = funding_to_raw_record(
        point,
        configuration=configuration,
        received_at=_RECEIVED,
        request_url=fetch.request_urls[0],
    )
    observations = funding_to_observations(
        point,
        raw,
        configuration=configuration,
        normalized_at=_RECEIVED,
    )
    by_field = {item.field_name: item for item in observations}

    assert set(by_field) == {
        "funding_interest_1h",
        "funding_interest_8h",
        "index_price",
        "prev_index_price",
    }
    assert by_field["funding_interest_1h"].period_start == point.timestamp - timedelta(hours=1)
    assert by_field["funding_interest_1h"].period_end == point.timestamp
    assert by_field["funding_interest_8h"].period_start == point.timestamp - timedelta(hours=8)
    assert by_field["funding_interest_8h"].period_end == point.timestamp
    assert by_field["funding_interest_1h"].unit == "ratio"
    assert by_field["index_price"].unit == "USD"


def test_raw_revision_identity_ignores_retrieval_but_observation_identity_is_exact() -> None:
    client = DeribitClient(
        _Transport((_FIXTURES / "btc_funding_history.json").read_bytes()),
        sleep=lambda _: None,
        clock=lambda: _RECEIVED,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    point = client.fetch_funding_history(
        "BTC-PERPETUAL",
        start,
        start + timedelta(days=1),
    ).points[0]
    configuration = _configuration()
    first = funding_to_raw_record(
        point,
        configuration=configuration,
        received_at=_RECEIVED,
        request_url="https://www.deribit.com/api/v2/public/get_funding_rate_history?a=1",
    )
    later = funding_to_raw_record(
        point,
        configuration=configuration,
        received_at=_RECEIVED + timedelta(days=1),
        request_url="https://www.deribit.com/api/v2/public/get_funding_rate_history?a=2",
    )
    observations = funding_to_observations(
        point,
        first,
        configuration=configuration,
        normalized_at=_RECEIVED,
    )

    assert first.record_id == later.record_id
    assert first.record_id == UUID("f98e75be-3184-5910-a688-e6fee047fc87")
    assert first.available_at == first.received_at == _RECEIVED
    assert later.available_at == later.received_at == _RECEIVED + timedelta(days=1)
    assert len({item.observation_id for item in observations}) == 4
    assert {item.field_name: item.observation_id for item in observations} == {
        "funding_interest_1h": UUID("c77f3875-2f6b-5241-b9c1-a087a5884ebd"),
        "funding_interest_8h": UUID("06c0a818-d869-50c9-8004-74be24910396"),
        "index_price": UUID("185228ab-a138-555f-bbda-ff8b155ca97d"),
        "prev_index_price": UUID("98406260-bbd4-5bd8-8d73-0c7aa458a520"),
    }


def test_dvol_daily_period_and_units_are_exact() -> None:
    client = DeribitClient(
        _Transport((_FIXTURES / "btc_dvol_daily.json").read_bytes()),
        sleep=lambda _: None,
        clock=lambda: _RECEIVED,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    fetch = client.fetch_dvol_daily("BTC", start, start + timedelta(days=2))
    candle = fetch.candles[0]
    configuration = _configuration()
    raw = dvol_to_raw_record(
        candle,
        configuration=configuration,
        received_at=_RECEIVED,
        request_url=fetch.request_urls[0],
    )
    observations = dvol_to_observations(
        candle,
        raw,
        configuration=configuration,
        normalized_at=_RECEIVED,
    )

    assert {item.field_name for item in observations} == {
        "dvol_open",
        "dvol_high",
        "dvol_low",
        "dvol_close",
    }
    assert all(item.unit == "dvol_index_points" for item in observations)
    assert all(item.period_start == candle.start for item in observations)
    assert all(item.period_end == candle.start + timedelta(days=1) for item in observations)
