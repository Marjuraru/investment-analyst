"""Offline unit tests for the read-only Alpaca AAPL client."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args, get_type_hints
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import (
    AlpacaMarketFetchReceipt,
    alpaca_fetch_receipt_id,
)
from investment_analyst.providers.market.alpaca_stock import (
    ADJUSTMENT,
    FEED,
    AlpacaCredentials,
    AlpacaStockClient,
    AlpacaStockError,
)

FIXTURE_PATH = Path("tests/fixtures/alpaca/aapl_daily.json")
START = datetime(2026, 7, 7, tzinfo=UTC)
END = datetime(2026, 7, 10, tzinfo=UTC)
NOW = datetime(2026, 7, 12, tzinfo=UTC)


class QueueTransport:
    """Return queued Alpaca-shaped pages and record request metadata."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        return HttpResponse(status_code=200, body=self.bodies.pop(0), headers={}, url=url)


def _credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key=" test-key ", secret_key=" test-secret ")


def _fixture() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _client(transport: QueueTransport) -> AlpacaStockClient:
    return AlpacaStockClient(transport, _credentials(), clock=lambda: NOW)


def _document_with_bars(bars: list[dict[str, object]], token: str | None = None) -> bytes:
    return json.dumps({"bars": bars, "symbol": "AAPL", "next_page_token": token}).encode()


def _bar(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "t": "2026-07-08T04:00:00Z",
        "o": 205.55,
        "h": 208.10,
        "l": 204.90,
        "c": 207.35,
        "v": 36550125,
        "n": 161775,
        "vw": 206.74,
    }
    value.update(changes)
    return value


def test_credentials_are_trimmed_and_repr_is_redacted() -> None:
    credentials = _credentials()

    assert credentials.api_key == "test-key"
    assert credentials.secret_key == "test-secret"
    assert "test-key" not in repr(credentials)
    assert "test-secret" not in repr(credentials)
    assert "redacted" in repr(credentials)


@pytest.mark.parametrize(
    ("api_key", "secret_key"),
    [("", "secret"), ("key", ""), ("   ", "secret"), ("key", "   ")],
)
def test_credentials_reject_empty_values(api_key: str, secret_key: str) -> None:
    with pytest.raises(AlpacaStockError, match="must not be empty"):
        AlpacaCredentials(api_key=api_key, secret_key=secret_key)


def test_fetch_parses_decimal_filters_orders_and_uses_required_request_values() -> None:
    transport = QueueTransport([_fixture()])
    result = _client(transport).fetch_daily_bars("AAPL", START, END)

    assert len(result.bars) == 3
    assert [bar.timestamp for bar in result.bars] == sorted(bar.timestamp for bar in result.bars)
    assert all(isinstance(bar.close, Decimal) for bar in result.bars)
    assert all(len(bar.raw_values) == 8 for bar in result.bars)
    assert result.feed == FEED
    assert result.adjustment == ADJUSTMENT

    url, headers, timeout = transport.calls[0]
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "data.alpaca.markets"
    assert urlsplit(url).path == "/v2/stocks/AAPL/bars"
    assert query["timeframe"] == ["1Day"]
    assert query["feed"] == ["iex"]
    assert query["adjustment"] == ["all"]
    assert query["sort"] == ["asc"]
    assert query["limit"] == ["10000"]
    assert headers == {
        "APCA-API-KEY-ID": "test-key",
        "APCA-API-SECRET-KEY": "test-secret",
        "Accept": "application/json",
        "User-Agent": "investment-analyst/0.1.0",
    }
    assert "test-key" not in url
    assert "test-secret" not in url
    assert timeout > 0


def test_identical_duplicates_are_removed() -> None:
    bar = _bar()
    transport = QueueTransport([_document_with_bars([bar, dict(bar)])])

    result = _client(transport).fetch_daily_bars("AAPL", START, END)

    assert len(result.bars) == 1


def test_conflicting_duplicates_are_rejected() -> None:
    transport = QueueTransport([_document_with_bars([_bar(), _bar(c=207.50, h=208.50)])])

    with pytest.raises(AlpacaStockError, match="conflicting AAPL bars"):
        _client(transport).fetch_daily_bars("AAPL", START, END)


def test_pagination_processes_every_page() -> None:
    first = _document_with_bars([_bar(t="2026-07-07T04:00:00Z")], "page-2")
    second = _document_with_bars([_bar(t="2026-07-08T04:00:00Z")])
    transport = QueueTransport([first, second])

    result = _client(transport).fetch_daily_bars("AAPL", START, END)

    assert len(result.bars) == 2
    assert len(result.request_urls) == 2
    assert "page_token=page-2" in result.request_urls[1]


def test_pagination_cycle_is_rejected() -> None:
    first = _document_with_bars([], "page-2")
    second = _document_with_bars([], "page-2")
    transport = QueueTransport([first, second])

    with pytest.raises(AlpacaStockError, match="token cycle"):
        _client(transport).fetch_daily_bars("AAPL", START, END)


def test_empty_list_and_null_pages_produce_zero_bars() -> None:
    for body in (
        b'{"bars": [], "next_page_token": null}',
        b'{"bars": null, "next_page_token": null}',
    ):
        result = _client(QueueTransport([body])).fetch_daily_bars("AAPL", START, END)
        assert result.bars == ()
        assert len(result.request_urls) == 1


def test_receipt_model_is_strict_deterministic_and_contains_no_any() -> None:
    values = {
        "asset_id": "equity:us:aapl",
        "source_id": "alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        "feed": "iex",
        "adjustment": "all",
        "requested_start": START,
        "requested_end": END,
        "retrieved_at": NOW,
        "bar_count": 0,
        "page_count": 1,
        "traceability_verified": True,
    }
    receipt = AlpacaMarketFetchReceipt(**values)
    identifier = alpaca_fetch_receipt_id(
        asset_id=receipt.asset_id,
        source_id=receipt.source_id,
        feed=receipt.feed,
        adjustment=receipt.adjustment,
        requested_start=receipt.requested_start,
        requested_end=receipt.requested_end,
    )
    assert identifier == alpaca_fetch_receipt_id(
        asset_id=receipt.asset_id,
        source_id=receipt.source_id,
        feed=receipt.feed,
        adjustment=receipt.adjustment,
        requested_start=receipt.requested_start,
        requested_end=receipt.requested_end,
    )
    with pytest.raises(ValidationError):
        AlpacaMarketFetchReceipt(**{**values, "requested_end": START})
    with pytest.raises(ValidationError):
        AlpacaMarketFetchReceipt(**{**values, "traceability_verified": False})
    with pytest.raises(ValidationError):
        AlpacaMarketFetchReceipt(**{**values, "unexpected": True})

    def contains_any(annotation: object) -> bool:
        return annotation is Any or any(contains_any(item) for item in get_args(annotation))

    assert not any(contains_any(item) for item in get_type_hints(AlpacaMarketFetchReceipt).values())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"[]", "must be an object"),
        (b'{"bars": {}, "symbol": "AAPL", "next_page_token": null}', "must be a list"),
        (b'{"bars": null, "error": "provider failure"}', "provider error"),
        (_document_with_bars([{"t": "2026-07-08T04:00:00Z"}]), "missing required"),
        (_document_with_bars([_bar(o=True)]), "invalid JSON type"),
        (_document_with_bars([_bar(c="NaN")]), "must be finite"),
        (_document_with_bars([_bar(t="not-a-time")]), "timestamp is invalid"),
    ],
)
def test_malformed_responses_are_rejected(body: bytes, message: str) -> None:
    with pytest.raises(AlpacaStockError, match=message):
        _client(QueueTransport([body])).fetch_daily_bars("AAPL", START, END)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"l": 209.0, "h": 208.0}, "low must not exceed"),
        ({"v": -1}, "volume must not be negative"),
        ({"n": 1.5}, "trade_count"),
        ({"o": 210.0}, "open must be within"),
        ({"vw": 210.0}, "vwap must be within"),
    ],
)
def test_incoherent_bar_values_are_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(AlpacaStockError, match=message):
        _client(QueueTransport([_document_with_bars([_bar(**changes)])])).fetch_daily_bars(
            "AAPL", START, END
        )


def test_rejects_naive_and_invalid_ranges() -> None:
    client = _client(QueueTransport([_fixture()]))

    with pytest.raises(AlpacaStockError, match="timezone"):
        client.fetch_daily_bars("AAPL", datetime(2026, 7, 7), END)
    with pytest.raises(AlpacaStockError, match="earlier"):
        client.fetch_daily_bars("AAPL", END, END)
    with pytest.raises(AlpacaStockError, match="only AAPL"):
        client.fetch_daily_bars("MSFT", START, END)


def test_source_contains_no_trading_endpoint_urls() -> None:
    source = Path("src/investment_analyst/providers/market/alpaca_stock.py").read_text()

    assert "trading.alpaca.markets" not in source
    assert "/orders" not in source
    assert "/positions" not in source
    assert "/account" not in source
