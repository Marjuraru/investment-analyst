"""Unit tests for the official FRED/ALFRED vintage client."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investment_analyst.providers.http import HttpRequestError, HttpResponse
from investment_analyst.providers.macro.fred_alfred import (
    MAX_RESPONSE_BYTES,
    FredAlfredClient,
    FredAlfredError,
    FredApiKey,
)

FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_bytes()
API_KEY = "a" * 32
RETRIEVED_AT = datetime(2020, 3, 1, 12, tzinfo=UTC)


class FixtureTransport:
    """Capture the secret-bearing wire URL while returning offline bytes."""

    def __init__(self, body: bytes = FIXTURE) -> None:
        self.body = body
        self.calls: list[str] = []
        self.max_response_bytes: list[int | None] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        self.max_response_bytes.append(max_response_bytes)
        return HttpResponse(200, self.body, {}, url)


class FailingTransport:
    """Simulate a transport error whose original context contains the secret."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        raise HttpRequestError(url, "failure", status_code=503)


class QueueTransport:
    """Return exact queued JSON pages while capturing pagination URLs."""

    def __init__(self, payloads: tuple[dict[str, object], ...]) -> None:
        self.payloads = list(payloads)
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
        return HttpResponse(200, json.dumps(self.payloads.pop(0)).encode(), {}, url)


def _fetch(transport: FixtureTransport):
    return FredAlfredClient(
        transport,
        FredApiKey(API_KEY),
        clock=lambda: RETRIEVED_AT,
    ).fetch_vintage_snapshot(
        "GDP",
        vintage_date=date(2020, 1, 15),
        observation_start=date(2019, 1, 1),
        observation_end=date(2020, 1, 1),
    )


def test_fetch_preserves_decimals_missing_values_and_safe_metadata() -> None:
    transport = FixtureTransport()

    fetch = _fetch(transport)

    assert fetch.series_id == "GDP"
    assert fetch.response.observations[0].value == Decimal("100.125")
    assert fetch.response.observations[1].value is None
    assert fetch.response.observations[1].raw_value == "."
    assert fetch.retrieved_at == RETRIEVED_AT
    assert fetch.body_sha256
    assert API_KEY in transport.calls[0]
    assert "api_key" not in fetch.public_request_url
    assert API_KEY not in fetch.public_request_url
    assert transport.max_response_bytes == [MAX_RESPONSE_BYTES]
    assert API_KEY not in json.dumps(fetch.response_payload, sort_keys=True)


def test_api_key_is_strict_and_redacted() -> None:
    credential = FredApiKey(API_KEY)

    assert repr(credential) == "FredApiKey(value='[REDACTED]')"
    assert API_KEY not in repr(credential)
    with pytest.raises(FredAlfredError):
        FredApiKey("invalid")


def test_transport_failure_does_not_reveal_api_key() -> None:
    client = FredAlfredClient(
        FailingTransport(),
        FredApiKey(API_KEY),
        clock=lambda: RETRIEVED_AT,
    )

    with pytest.raises(FredAlfredError) as captured:
        client.fetch_vintage_snapshot(
            "GDP",
            vintage_date=date(2020, 1, 15),
            observation_start=date(2019, 1, 1),
            observation_end=date(2020, 1, 1),
        )

    assert str(captured.value) == "FRED/ALFRED request failed (HTTP 503)"
    assert API_KEY not in str(captured.value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"count": 1},
        {"units": "pc1"},
        {"output_type": 2},
        {"limit": 1},
        {"observations": []},
    ],
)
def test_rejects_incomplete_or_transformed_provider_contract(
    mutation: dict[str, object],
) -> None:
    payload = json.loads(FIXTURE)
    payload.update(mutation)
    transport = FixtureTransport(json.dumps(payload).encode())

    with pytest.raises((FredAlfredError, ValueError)):
        _fetch(transport)


def test_rejects_float_financial_value_and_out_of_scope_dates() -> None:
    payload = json.loads(FIXTURE)
    payload["observations"][0]["value"] = 100.125
    transport = FixtureTransport(json.dumps(payload).encode())
    with pytest.raises(ValueError):
        _fetch(transport)

    payload = json.loads(FIXTURE)
    payload["observations"][0]["date"] = "2018-01-01"
    transport = FixtureTransport(json.dumps(payload).encode())
    with pytest.raises(FredAlfredError, match="outside"):
        _fetch(transport)


def test_vintage_dates_are_paginated_bounded_and_secret_safe() -> None:
    transport = QueueTransport(
        (
            {
                "realtime_start": "2020-01-01",
                "realtime_end": "2020-01-31",
                "order_by": "vintage_date",
                "sort_order": "asc",
                "count": 3,
                "offset": 0,
                "limit": 3,
                "vintage_dates": ["2020-01-03", "2020-01-10"],
            },
            {
                "realtime_start": "2020-01-01",
                "realtime_end": "2020-01-31",
                "order_by": "vintage_date",
                "sort_order": "asc",
                "count": 3,
                "offset": 2,
                "limit": 1,
                "vintage_dates": ["2020-01-17"],
            },
        )
    )
    client = FredAlfredClient(
        transport,
        FredApiKey(API_KEY),
        clock=lambda: RETRIEVED_AT,
    )

    result = client.fetch_vintage_dates(
        "GDP",
        realtime_start=date(2020, 1, 1),
        realtime_end=date(2020, 1, 31),
        max_dates=3,
    )

    assert result.vintage_dates == (
        date(2020, 1, 3),
        date(2020, 1, 10),
        date(2020, 1, 17),
    )
    assert result.total_count == 3
    assert result.complete is True
    assert "offset=2" in transport.calls[1]
    assert all(API_KEY in call for call in transport.calls)
    assert all(API_KEY not in url for url in result.public_request_urls)


def test_vintage_discovery_reports_an_explicit_resume_boundary() -> None:
    transport = QueueTransport(
        (
            {
                "realtime_start": "2020-01-01",
                "realtime_end": "2020-01-31",
                "order_by": "vintage_date",
                "sort_order": "asc",
                "count": 3,
                "offset": 0,
                "limit": 2,
                "vintage_dates": ["2020-01-03", "2020-01-10"],
            },
        )
    )
    client = FredAlfredClient(
        transport,
        FredApiKey(API_KEY),
        clock=lambda: RETRIEVED_AT,
    )

    result = client.fetch_vintage_dates(
        "GDP",
        realtime_start=date(2020, 1, 1),
        realtime_end=date(2020, 1, 31),
        max_dates=2,
    )

    assert result.total_count == 3
    assert result.vintage_dates[-1] == date(2020, 1, 10)
    assert result.complete is False
