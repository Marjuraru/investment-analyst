"""Offline tests for Coinbase candle parsing and request behavior."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investment_analyst.providers.crypto.coinbase_exchange import (
    CoinbaseExchangeClient,
    CoinbaseExchangeError,
)
from investment_analyst.providers.http import HttpResponse

FIXTURE_PATH = Path("tests/fixtures/coinbase/btc_usd_daily.json")
FIXED_NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


class FakeTransport:
    """Return queued offline bodies and record every request."""

    def __init__(self, bodies: list[bytes]) -> None:
        self._bodies = list(bodies)
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        body = self._bodies.pop(0) if self._bodies else b"[]"
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _client(transport: FakeTransport) -> CoinbaseExchangeClient:
    return CoinbaseExchangeClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: FIXED_NOW,
    )


def _fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def test_parses_decimal_orders_and_filters_exact_range() -> None:
    transport = FakeTransport([_fixture_bytes()])
    result = _client(transport).fetch_daily_candles(
        "BTC-USD",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert [candle.start.day for candle in result.candles] == [9, 10, 11]
    assert result.candles[0].close == Decimal("107050.75")
    assert isinstance(result.candles[0].close, Decimal)
    assert result.candles[0].raw_values == (
        "1783555200",
        "105900.25",
        "108000.00",
        "106400.50",
        "107050.75",
        "13200.000",
    )


def test_uses_public_headers_and_daily_granularity() -> None:
    transport = FakeTransport([b"[]"])
    _client(transport).fetch_daily_candles(
        "BTC-USD",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 7, 10, tzinfo=UTC),
    )

    url, headers, _ = transport.calls[0]
    assert "granularity=86400" in url
    assert headers == {
        "Accept": "application/json",
        "User-Agent": "investment-analyst/0.1.0",
    }
    assert all("auth" not in key.lower() for key in headers)


def test_deduplicates_identical_candles() -> None:
    rows = json.loads(_fixture_bytes())
    rows.append(rows[0])
    transport = FakeTransport([json.dumps(rows).encode()])

    result = _client(transport).fetch_daily_candles(
        "BTC-USD",
        datetime(2026, 7, 11, tzinfo=UTC),
        datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert len(result.candles) == 1


def test_rejects_conflicting_duplicate_timestamp() -> None:
    rows = json.loads(_fixture_bytes())
    revised = list(rows[0])
    revised[4] = 109600.75
    rows.append(revised)
    transport = FakeTransport([json.dumps(rows).encode()])

    with pytest.raises(CoinbaseExchangeError, match="conflicting candles"):
        _client(transport).fetch_daily_candles(
            "BTC-USD",
            datetime(2026, 7, 11, tzinfo=UTC),
            datetime(2026, 7, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[[1,2,3]]",
        b"[[true,1,2,1,1,1]]",
        b"[[1.5,1,2,1,1,1]]",
        b'[[1,"bad",2,1,1,1]]',
    ],
)
def test_rejects_malformed_responses(body: bytes) -> None:
    transport = FakeTransport([body])
    with pytest.raises(CoinbaseExchangeError):
        _client(transport).fetch_daily_candles(
            "BTC-USD",
            datetime(1970, 1, 1, tzinfo=UTC),
            datetime(1970, 1, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "row",
    [
        [1783555200, 109000, 108000, 108500, 108600, 1],
        [1783555200, 107000, 108000, 106500, 107500, 1],
        [1783555200, 107000, 108000, 107500, 108500, 1],
        [1783555200, 107000, 108000, 107500, 107800, -1],
    ],
)
def test_rejects_incoherent_ohlcv(row: list[object]) -> None:
    transport = FakeTransport([json.dumps([row]).encode()])
    with pytest.raises(CoinbaseExchangeError):
        _client(transport).fetch_daily_candles(
            "BTC-USD",
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 7, 10, tzinfo=UTC),
        )


def test_rejects_naive_or_invalid_ranges() -> None:
    client = _client(FakeTransport([b"[]"]))
    with pytest.raises(CoinbaseExchangeError, match="timezone"):
        client.fetch_daily_candles(
            "BTC-USD",
            datetime(2026, 7, 9),
            datetime(2026, 7, 10, tzinfo=UTC),
        )
    with pytest.raises(CoinbaseExchangeError, match="earlier"):
        client.fetch_daily_candles(
            "BTC-USD",
            datetime(2026, 7, 10, tzinfo=UTC),
            datetime(2026, 7, 10, tzinfo=UTC),
        )
    with pytest.raises(CoinbaseExchangeError, match="future"):
        client.fetch_daily_candles(
            "BTC-USD",
            datetime(2026, 7, 13, tzinfo=UTC),
            datetime(2026, 7, 14, tzinfo=UTC),
        )


def test_chunks_ranges_larger_than_three_hundred_days() -> None:
    transport = FakeTransport([b"[]", b"[]"])
    result = _client(transport).fetch_daily_candles(
        "BTC-USD",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert len(result.request_urls) == 2
    assert len(transport.calls) == 2
    assert all("granularity=86400" in call[0] for call in transport.calls)
