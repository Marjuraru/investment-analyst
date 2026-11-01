"""Bounded Coinbase acquisition, canonical sealing and offline reuse of the archive."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.http import HttpResponse
from investment_analyst.research.historical_archive import (
    HistoricalArchiveConflictError,
    HistoricalArchiveError,
    HistoricalArchiveRequestError,
    HistoricalArchiveScopeError,
    acquire_historical_archive,
    read_historical_archive,
    resolve_historical_scope,
)
from investment_analyst.research.historical_models import (
    CANDLES_FILE_NAME,
    MANIFEST_FILE_NAME,
    HistoricalArchiveRequest,
    assumed_available_instant,
    bar_start_instant,
)

_START = date(2019, 1, 1)
_RETRIEVED = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
_PRICE = Decimal("101.25")

Handler = Callable[[str], tuple[int, bytes]]


class FakeTransport:
    """Contract-level transport that records each requested URL."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.requests.append(url)
        status_code, body = self._handler(url)
        return HttpResponse(
            status_code=status_code,
            body=body,
            headers={},
            url=url,
        )


def _candles_body(url: str, price_for: Callable[[date], Decimal | None]) -> bytes:
    query = parse_qs(urlparse(url).query)
    start = datetime.fromisoformat(query["start"][0]).date()
    end = datetime.fromisoformat(query["end"][0]).date()
    rows: list[str] = []
    day = start
    while day < end:
        price = price_for(day)
        if price is not None:
            timestamp = int(bar_start_instant(day).timestamp())
            rows.append(f'[{timestamp},{price},{price + 1},{price},{price},"3.25"]')
        day += timedelta(days=1)
    return ("[" + ",".join(rows) + "]").encode()


def _client(
    transport: FakeTransport | None = None, handler: Handler | None = None
) -> CoinbaseExchangeClient:
    if transport is None:
        assert handler is not None
        transport = FakeTransport(handler)
    return CoinbaseExchangeClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _RETRIEVED,
    )


def _always(price: Decimal = _PRICE) -> Callable[[date], Decimal | None]:
    return lambda _day: price


def test_inclusive_range_maps_to_half_open_utc_interval(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: (200, _candles_body(url, _always())))
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 3),
    )
    receipt = acquire_historical_archive(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    assert receipt.status == "sealed"
    assert receipt.requested_days == 3
    assert receipt.bar_count == 3
    assert receipt.absent_days == 0
    assert receipt.request_count == 1
    assert len(transport.requests) == 1
    query = parse_qs(urlparse(transport.requests[0]).query)
    assert datetime.fromisoformat(query["start"][0]) == datetime(2019, 1, 1, tzinfo=UTC)
    assert datetime.fromisoformat(query["end"][0]) == datetime(2019, 1, 4, tzinfo=UTC)
    archive = read_historical_archive(Path(receipt.archive_dir))
    assert archive.manifest.requested_end == datetime(2019, 1, 4, tzinfo=UTC)
    assert archive.manifest.retrieved_at == _RETRIEVED
    assert archive.manifest.strict_pit_verified is False
    assert archive.manifest.availability_basis.value == "historical_reconstruction_assumed"


def test_full_range_uses_bounded_sequential_requests(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: (200, _candles_body(url, _always())))
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2025, 12, 31),
    )
    receipt = acquire_historical_archive(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    assert receipt.requested_days == 2557
    assert receipt.request_count == 9
    assert receipt.request_count <= 12
    assert len(transport.requests) == 9


def test_decimal_raw_values_and_utc_are_preserved(tmp_path: Path) -> None:
    price = Decimal("101.123456789012345678901234567890")
    transport = FakeTransport(lambda url: (200, _candles_body(url, _always(price))))
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=_START,
    )
    receipt = acquire_historical_archive(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    archive = read_historical_archive(Path(receipt.archive_dir))
    candle = archive.candles[0]
    assert candle.close == price
    assert candle.low <= candle.open <= candle.high
    assert candle.bar_start == bar_start_instant(_START)
    assert candle.bar_end == bar_start_instant(_START) + timedelta(days=1)
    assert candle.assumed_available_at == assumed_available_instant(candle.bar_end)
    assert len(candle.raw_values) == 6
    assert candle.raw_values[0] == str(int(bar_start_instant(_START).timestamp()))


def test_valid_empty_interval_is_evidence_without_synthetic_prices(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: (200, b"[]"))
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 5),
    )
    receipt = acquire_historical_archive(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    assert receipt.status == "sealed"
    assert receipt.bar_count == 0
    assert receipt.absent_days == 5
    archive = read_historical_archive(Path(receipt.archive_dir))
    assert archive.candles == ()
    assert archive.manifest.counts.absent_days == 5


def test_internal_absence_is_conserved_not_filled(tmp_path: Path) -> None:
    missing = _START + timedelta(days=2)
    transport = FakeTransport(
        lambda url: (200, _candles_body(url, lambda day: None if day == missing else _PRICE))
    )
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 4),
    )
    receipt = acquire_historical_archive(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    assert receipt.requested_days == 4
    assert receipt.bar_count == 3
    assert receipt.absent_days == 1
    archive = read_historical_archive(Path(receipt.archive_dir))
    starts = tuple(candle.bar_start.date() for candle in archive.candles)
    assert missing not in starts
    assert all(candle.close == _PRICE for candle in archive.candles)


def test_partial_http_failure_leaves_no_seal(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(url: str) -> tuple[int, bytes]:
        calls["count"] += 1
        if calls["count"] == 2:
            return 503, b"{}"
        return 200, _candles_body(url, _always())

    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 12, 31),
    )
    with pytest.raises(HistoricalArchiveError, match="without a seal"):
        acquire_historical_archive(
            _client(handler=handler),
            request,
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_conflicting_duplicate_bar_is_rejected(tmp_path: Path) -> None:
    def handler(url: str) -> tuple[int, bytes]:
        timestamp = int(bar_start_instant(_START).timestamp())
        body = (f'[[{timestamp},100,101,100,100,"1"],[{timestamp},100,101,100,99,"1"]]').encode()
        return 200, body

    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=_START,
    )
    with pytest.raises(HistoricalArchiveError, match="without a seal"):
        acquire_historical_archive(
            _client(handler=handler),
            request,
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_offline_reuse_never_calls_the_provider(tmp_path: Path) -> None:
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 4),
    )
    first = acquire_historical_archive(
        _client(handler=lambda url: (200, _candles_body(url, _always()))),
        request,
        output_dir=tmp_path / "archives",
    )
    assert first.status == "sealed"

    def forbidden(_url: str) -> tuple[int, bytes]:
        raise AssertionError("provider must not be called when a sealed archive matches")

    second = acquire_historical_archive(
        _client(handler=forbidden),
        request,
        output_dir=tmp_path / "archives",
    )
    assert second.status == "reused"
    assert second.archive_id == first.archive_id
    assert second.semantic_sha256 == first.semantic_sha256
    assert second.candles_sha256 == first.candles_sha256


def test_reuse_ignores_dataset_siblings_and_rejects_broken_archive_claims(
    tmp_path: Path,
) -> None:
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 4),
    )
    root = tmp_path / "archives"
    first = acquire_historical_archive(
        _client(handler=lambda url: (200, _candles_body(url, _always()))),
        request,
        output_dir=root,
    )
    dataset_dir = root / "datasets" / "sibling"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "rows.parquet").write_bytes(b"parquet")
    (dataset_dir / "manifest.json").write_bytes(b"{}")
    (root / "notes").mkdir()

    def forbidden(_url: str) -> tuple[int, bytes]:
        raise AssertionError("provider must not be called when a sealed archive matches")

    second = acquire_historical_archive(
        _client(handler=forbidden),
        request,
        output_dir=root,
    )
    assert second.status == "reused"
    assert second.archive_id == first.archive_id

    broken = root / "broken-archive"
    broken.mkdir()
    (broken / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(HistoricalArchiveConflictError):
        acquire_historical_archive(
            _client(handler=forbidden),
            request,
            output_dir=root,
        )


def test_corrupted_archive_fails_closed_without_repair(tmp_path: Path) -> None:
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=date(2019, 1, 4),
    )
    receipt = acquire_historical_archive(
        _client(handler=lambda url: (200, _candles_body(url, _always()))),
        request,
        output_dir=tmp_path / "archives",
    )
    archive_dir = Path(receipt.archive_dir)
    candles_path = archive_dir / CANDLES_FILE_NAME
    original = candles_path.read_bytes()
    candles_path.write_bytes(original.replace(b"101.25", b"101.26", 1))
    with pytest.raises(HistoricalArchiveConflictError, match="differs from its manifest"):
        read_historical_archive(archive_dir)
    candles_path.write_bytes(original)
    assert read_historical_archive(archive_dir).archive_id == receipt.archive_id

    manifest_path = archive_dir / MANIFEST_FILE_NAME
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(manifest_bytes + b"\n")
    with pytest.raises(HistoricalArchiveConflictError, match="canonical encoding"):
        read_historical_archive(archive_dir)


def test_scope_rejects_equity_and_pre_history_ranges(tmp_path: Path) -> None:
    with pytest.raises(HistoricalArchiveScopeError):
        resolve_historical_scope("equity:us:aapl")
    request = HistoricalArchiveRequest(
        asset_id="crypto:sol-usd",
        start_date=_START,
        end_date=date(2019, 1, 5),
    )
    with pytest.raises(HistoricalArchiveRequestError, match="history starts"):
        acquire_historical_archive(
            _client(handler=lambda url: (200, b"[]")),
            request,
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_relative_output_and_future_range_are_rejected(tmp_path: Path) -> None:
    request = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=_START,
        end_date=_START,
    )
    with pytest.raises(HistoricalArchiveRequestError, match="absolute"):
        acquire_historical_archive(
            _client(handler=lambda url: (200, b"[]")),
            request,
            output_dir=Path("relative-archives"),
        )
    future = HistoricalArchiveRequest(
        asset_id="crypto:btc-usd",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3),
    )
    with pytest.raises(HistoricalArchiveRequestError, match="future"):
        acquire_historical_archive(
            _client(handler=lambda url: (200, b"[]")),
            future,
            output_dir=tmp_path / "archives",
        )
