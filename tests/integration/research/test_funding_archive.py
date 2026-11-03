"""Bounded Deribit funding acquisition, sealing and offline reuse."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from investment_analyst.providers.crypto.deribit import MAX_FUNDING_INTERVAL, DeribitClient
from investment_analyst.providers.http import HttpResponse
from investment_analyst.research.funding_archive import (
    FundingArchiveConflictError,
    FundingArchiveError,
    FundingArchiveRequestError,
    FundingArchiveScopeError,
    _page_bytes,
    _sha256_bytes,
    acquire_funding_archive,
    build_funding_archive,
    read_funding_archive,
    resolve_funding_scope,
    seal_funding_archive,
    validate_funding_archive_request,
)
from investment_analyst.research.funding_models import (
    ARCHIVE_PAGES_FILE_NAME,
    MANIFEST_FILE_NAME,
    FundingArchivePage,
    FundingPoint,
    assumed_funding_available_instant,
    funding_archive_request,
    funding_point_id,
)

_START = date(2024, 1, 1)
_END = date(2024, 1, 3)
_RETRIEVED = datetime(2026, 11, 2, 0, 0, 30, tzinfo=UTC)
_INTEREST = "0.000001"
_INDEX = "100.500000000000000000000001"

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


def _rows_body(
    url: str,
    *,
    interest: str = _INTEREST,
    empty: bool = False,
    misaligned: bool = False,
    duplicate: bool = False,
) -> bytes:
    if empty:
        return json.dumps({"jsonrpc": "2.0", "result": []}).encode()
    query = parse_qs(urlparse(url).query)
    start_ms = int(query["start_timestamp"][0]) + 1
    end_ms = int(query["end_timestamp"][0])
    rows: list[dict[str, str]] = []
    timestamp = start_ms
    while timestamp < end_ms:
        rows.append(
            {
                "timestamp": str(timestamp),
                "index_price": _INDEX,
                "prev_index_price": "100",
                "interest_1h": interest,
                "interest_8h": "0.000008",
            }
        )
        if duplicate:
            rows.append(
                {
                    "timestamp": str(timestamp),
                    "index_price": _INDEX,
                    "prev_index_price": "100",
                    "interest_1h": "0.000002",
                    "interest_8h": "0.000008",
                }
            )
            break
        if misaligned:
            rows.append(
                {
                    "timestamp": str(timestamp + 1_800_000),
                    "index_price": _INDEX,
                    "prev_index_price": "100",
                    "interest_1h": interest,
                    "interest_8h": "0.000008",
                }
            )
            break
        timestamp += 3_600_000
    return json.dumps({"jsonrpc": "2.0", "result": rows}).encode()


def _handler(**options: object) -> Handler:
    def handle(url: str) -> tuple[int, bytes]:
        return 200, _rows_body(url, **options)

    return handle


def _client(
    transport: FakeTransport | None = None,
    handler: Handler | None = None,
) -> DeribitClient:
    if transport is None:
        assert handler is not None
        transport = FakeTransport(handler)
    return DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _RETRIEVED,
    )


def _acquire(
    client: DeribitClient,
    request,
    *,
    output_dir: Path,
):
    return acquire_funding_archive(
        client,
        request,
        output_dir=output_dir,
        clock=lambda: _RETRIEVED,
    )


def _request(start: date = _START, end: date = _END):
    return funding_archive_request(
        asset_id="crypto:btc-usd",
        decision_start_date=start,
        decision_end_date=end,
    )


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def test_three_day_request_seals_one_page_with_exact_derived_range(tmp_path: Path) -> None:
    transport = FakeTransport(_handler())
    receipt = _acquire(
        _client(transport),
        _request(),
        output_dir=tmp_path / "archives",
    )
    assert receipt.status == "sealed"
    assert receipt.decision_count == 3
    assert receipt.expected_points == 216
    assert receipt.observed_points == 216
    assert receipt.missing_points == 0
    assert receipt.page_count == 1
    assert len(transport.requests) == 1
    archive = read_funding_archive(Path(receipt.archive_dir))
    assert archive.manifest.method == "public/get_funding_rate_history"
    assert archive.manifest.strict_pit_verified is False
    assert archive.manifest.availability_policy == "historical-funding-end-plus-1h-assumed-v1"
    assert archive.manifest.request.requested_start == datetime(2023, 12, 25, 1, 0, tzinfo=UTC)
    assert archive.manifest.request.requested_end == datetime(2024, 1, 3, 1, 0, tzinfo=UTC)
    assert archive.points[0].timestamp == archive.manifest.request.requested_start
    assert archive.points[-1].timestamp == (
        archive.manifest.request.requested_end - timedelta(hours=1)
    )
    assert archive.points[0].assumed_available_at == assumed_funding_available_instant(
        archive.points[0].timestamp
    )
    assert set(archive.points[0].raw_payload) == {
        "timestamp",
        "index_price",
        "prev_index_price",
        "interest_1h",
        "interest_8h",
    }
    assert archive.points[0].raw_payload["interest_1h"] == _INTEREST


def test_full_derived_range_uses_the_bounded_sequential_page_count(tmp_path: Path) -> None:
    transport = FakeTransport(_handler())
    request = _request(date(2020, 1, 1), date(2025, 12, 25))
    receipt = _acquire(
        _client(transport),
        request,
        output_dir=tmp_path / "archives",
    )
    assert receipt.expected_points == 52_608
    assert receipt.observed_points == 52_608
    assert receipt.missing_points == 0
    assert receipt.page_count == 71
    assert receipt.page_count <= 72
    assert len(transport.requests) == receipt.page_count
    archive = read_funding_archive(Path(receipt.archive_dir))
    assert archive.points[0].timestamp == archive.manifest.request.requested_start
    assert archive.points[-1].timestamp == (
        archive.manifest.request.requested_end - timedelta(hours=1)
    )
    for index, page in enumerate(archive.manifest.pages):
        query = parse_qs(urlparse(page.request_url).query)
        assert int(query["start_timestamp"][0]) == _ms(page.requested_start) - 1
        assert int(query["end_timestamp"][0]) == _ms(page.requested_end)
        page_points = tuple(
            point for point in archive.points if point.page_index == page.page_index
        )
        assert page_points[0].timestamp == page.requested_start
        assert page_points[-1].timestamp == page.requested_end - timedelta(hours=1)
        if index < len(archive.manifest.pages) - 1:
            assert page.requested_end - page.requested_start == MAX_FUNDING_INTERVAL
        else:
            assert page.requested_end - page.requested_start <= MAX_FUNDING_INTERVAL
    for previous, current in zip(archive.manifest.pages, archive.manifest.pages[1:], strict=False):
        assert current.requested_start == previous.requested_end


def _legacy_point(timestamp: datetime) -> FundingPoint:
    raw = {
        "timestamp": str(_ms(timestamp)),
        "index_price": _INDEX,
        "prev_index_price": "100",
        "interest_1h": _INTEREST,
        "interest_8h": "0.000008",
    }
    return FundingPoint(
        point_id=funding_point_id(
            funding_source_id="deribit:btc-perpetual:funding-rate-history",
            timestamp=timestamp,
            index_price=Decimal(_INDEX),
            prev_index_price=Decimal("100"),
            interest_1h=Decimal(_INTEREST),
            interest_8h=Decimal("0.000008"),
            raw_payload=raw,
        ),
        page_index=0,
        timestamp=timestamp,
        index_price=Decimal(_INDEX),
        prev_index_price=Decimal("100"),
        interest_1h=Decimal(_INTEREST),
        interest_8h=Decimal("0.000008"),
        raw_payload=raw,
        assumed_available_at=assumed_funding_available_instant(timestamp),
    )


def test_legacy_31_day_archive_is_readable_and_reused_without_rewrite(tmp_path: Path) -> None:
    request = _request(date(2024, 1, 1), date(2024, 1, 25))
    assert request.requested_end - request.requested_start == timedelta(days=31)
    points = tuple(
        _legacy_point(request.requested_start + timedelta(hours=offset))
        for offset in range(request.expected_points)
    )
    payload = _page_bytes(points)
    page = FundingArchivePage(
        page_index=0,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        request_url="https://example.invalid/legacy-31-day-page",
        started_at=datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 2, 0, 1, 0, tzinfo=UTC),
        elapsed_milliseconds=1_000,
        retrieved_at=_RETRIEVED,
        point_count=len(points),
        points_sha256=_sha256_bytes(payload),
        points_bytes=len(payload),
    )
    scope = resolve_funding_scope("crypto:btc-usd")
    archive = build_funding_archive(
        asset=scope.asset,
        request=request,
        pages=(page,),
        points=points,
        retrieved_at=_RETRIEVED,
    )
    receipt = seal_funding_archive(archive, output_dir=tmp_path / "legacy")
    archive_dir = Path(receipt.archive_dir)
    pages_path = archive_dir / ARCHIVE_PAGES_FILE_NAME
    manifest_path = archive_dir / MANIFEST_FILE_NAME
    pages_before = pages_path.read_bytes()
    manifest_before = manifest_path.read_bytes()
    pages_mtime = pages_path.stat().st_mtime_ns
    manifest_mtime = manifest_path.stat().st_mtime_ns

    def forbidden(_url: str) -> tuple[int, bytes]:
        raise AssertionError("provider must not be called for a legacy sealed archive")

    reused = _acquire(
        _client(handler=forbidden),
        request,
        output_dir=tmp_path / "legacy",
    )
    assert reused.status == "reused"
    assert reused.archive_id == receipt.archive_id
    assert reused.page_count == 1
    assert pages_path.read_bytes() == pages_before
    assert manifest_path.read_bytes() == manifest_before
    assert pages_path.stat().st_mtime_ns == pages_mtime
    assert manifest_path.stat().st_mtime_ns == manifest_mtime
    assert read_funding_archive(archive_dir).points == points


def test_decimal_and_raw_text_are_preserved_exactly(tmp_path: Path) -> None:
    receipt = _acquire(
        _client(handler=_handler()),
        _request(),
        output_dir=tmp_path / "archives",
    )
    archive = read_funding_archive(Path(receipt.archive_dir))
    point = archive.points[0]
    assert point.index_price == Decimal(_INDEX)
    assert point.interest_1h == Decimal(_INTEREST)
    assert point.raw_payload["index_price"] == _INDEX
    assert point.raw_payload["interest_1h"] == _INTEREST
    assert point.timestamp == datetime(2023, 12, 25, 1, 0, tzinfo=UTC)


def test_empty_page_is_evidence_of_absence_not_zero(tmp_path: Path) -> None:
    receipt = _acquire(
        _client(handler=_handler(empty=True)),
        _request(),
        output_dir=tmp_path / "archives",
    )
    assert receipt.status == "sealed"
    assert receipt.observed_points == 0
    assert receipt.missing_points == 216
    archive = read_funding_archive(Path(receipt.archive_dir))
    assert archive.points == ()
    assert archive.manifest.counts.missing_points == 216


def test_partial_failure_leaves_no_seal(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(url: str) -> tuple[int, bytes]:
        calls["count"] += 1
        if calls["count"] == 2:
            return 503, b"{}"
        return 200, _rows_body(url)

    request = _request(date(2024, 1, 1), date(2024, 2, 10))
    with pytest.raises(FundingArchiveError, match="without a seal"):
        _acquire(
            _client(handler=handler),
            request,
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_conflicting_duplicate_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FundingArchiveError, match="without a seal"):
        _acquire(
            _client(handler=_handler(duplicate=True)),
            _request(),
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_misaligned_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _acquire(
            _client(handler=_handler(misaligned=True)),
            _request(),
            output_dir=tmp_path / "archives",
        )
    assert not (tmp_path / "archives").exists()


def test_offline_reuse_never_calls_the_provider(tmp_path: Path) -> None:
    first = _acquire(
        _client(handler=_handler()),
        _request(),
        output_dir=tmp_path / "archives",
    )
    assert first.status == "sealed"

    def forbidden(_url: str) -> tuple[int, bytes]:
        raise AssertionError("provider must not be called when a sealed archive matches")

    second = _acquire(
        _client(handler=forbidden),
        _request(),
        output_dir=tmp_path / "archives",
    )
    assert second.status == "reused"
    assert second.archive_id == first.archive_id
    assert second.semantic_sha256 == first.semantic_sha256
    assert second.pages_sha256 == first.pages_sha256


def test_reuse_ignores_bundle_siblings_and_rejects_broken_archive_claims(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    first = _acquire(
        _client(handler=_handler()),
        _request(),
        output_dir=root,
    )
    assert first.status == "sealed"
    bundle_sibling = root / "bundle-sibling"
    bundle_sibling.mkdir()
    (bundle_sibling / "funding_features.parquet").write_bytes(b"parquet")
    (bundle_sibling / "manifest.json").write_text("{}", encoding="utf-8")

    def forbidden(_url: str) -> tuple[int, bytes]:
        raise AssertionError("provider must not be called when a sealed archive matches")

    second = _acquire(
        _client(handler=forbidden),
        _request(),
        output_dir=root,
    )
    assert second.status == "reused"

    broken = root / "a-broken"
    broken.mkdir()
    (broken / ARCHIVE_PAGES_FILE_NAME).write_text("not-json\n", encoding="utf-8")
    with pytest.raises(FundingArchiveConflictError):
        _acquire(
            _client(handler=forbidden),
            _request(),
            output_dir=root,
        )


def test_corrupted_archive_fails_closed_without_repair(tmp_path: Path) -> None:
    receipt = _acquire(
        _client(handler=_handler()),
        _request(),
        output_dir=tmp_path / "artifacts",
    )
    archive_dir = Path(receipt.archive_dir)
    pages_path = archive_dir / ARCHIVE_PAGES_FILE_NAME
    original = pages_path.read_bytes()
    pages_path.write_bytes(original.replace(_INTEREST.encode(), b"0.000002", 1))
    with pytest.raises(FundingArchiveConflictError, match="differs from its manifest"):
        read_funding_archive(archive_dir)
    pages_path.write_bytes(original)
    assert read_funding_archive(archive_dir).archive_id == receipt.archive_id

    manifest_path = archive_dir / MANIFEST_FILE_NAME
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(manifest_bytes + b"\n")
    with pytest.raises(FundingArchiveConflictError, match="canonical encoding"):
        read_funding_archive(archive_dir)


def test_relative_output_symlink_and_foreign_destination_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(FundingArchiveRequestError, match="absolute"):
        _acquire(
            _client(handler=_handler()),
            _request(),
            output_dir=Path("relative-archives"),
        )
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(FundingArchiveRequestError, match="symbolic link"):
        _acquire(
            _client(handler=_handler()),
            _request(),
            output_dir=link / "archives",
        )
    foreign = tmp_path / "foreign"
    (foreign / "unrelated").mkdir(parents=True)
    with pytest.raises(FundingArchiveConflictError, match="unrelated"):
        _acquire(
            _client(handler=_handler()),
            _request(),
            output_dir=foreign,
        )


def test_scope_rejects_equity_and_request_asset_mismatch() -> None:
    with pytest.raises(FundingArchiveScopeError):
        resolve_funding_scope("equity:us:aapl")
    scope = resolve_funding_scope("crypto:btc-usd")
    eth_request = funding_archive_request(
        asset_id="crypto:eth-usd",
        decision_start_date=_START,
        decision_end_date=_END,
    )
    with pytest.raises(FundingArchiveRequestError, match="resolved funding scope"):
        validate_funding_archive_request(scope, eth_request)


def test_manifest_and_page_hash_tampering_fails_closed(tmp_path: Path) -> None:
    receipt = _acquire(
        _client(handler=_handler()),
        _request(),
        output_dir=tmp_path / "artifacts",
    )
    archive_dir = Path(receipt.archive_dir)
    manifest_path = archive_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["observed_points"] = 215
    manifest["counts"]["missing_points"] = 1
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(FundingArchiveConflictError):
        read_funding_archive(archive_dir)
