"""Contract tests for the bounded public Deribit client."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from investment_analyst.providers.crypto.deribit import (
    DeribitClient,
    DeribitError,
    DeribitFundingPoint,
)
from investment_analyst.providers.http import HttpRequestError, HttpResponse

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = datetime(2026, 8, 4, tzinfo=UTC)


class _Transport:
    def __init__(
        self,
        *bodies: bytes,
        truncated: bool = False,
        status_code: int = 200,
        response_url: str | None = None,
    ) -> None:
        self._bodies = list(bodies)
        self._truncated = truncated
        self._status_code = status_code
        self._response_url = response_url
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del timeout_seconds, max_response_bytes
        self.calls.append((url, headers))
        if not self._bodies:
            raise AssertionError("unexpected Deribit request")
        return HttpResponse(
            status_code=self._status_code,
            body=self._bodies.pop(0),
            headers={},
            url=self._response_url or url,
            body_truncated=self._truncated,
        )


def _rpc(result: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "result": result}).encode()


def _ms(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000


def _funding_row(timestamp: datetime) -> dict[str, object]:
    return {
        "timestamp": _ms(timestamp),
        "index_price": "100000.01",
        "prev_index_price": "99999.99",
        "interest_1h": "0.000001",
        "interest_8h": "0.000008",
    }


def _dvol_row(timestamp: datetime, close: str) -> list[object]:
    return [_ms(timestamp), "50", "60", "45", close]


def test_summary_maps_raw_last_and_keeps_funding_concepts_distinct() -> None:
    transport = _Transport((_FIXTURES / "btc_perpetual_summary.json").read_bytes())
    client = DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: datetime(2026, 8, 2, 1, tzinfo=UTC),
    )

    result = client.fetch_perpetual_summary("BTC-PERPETUAL")

    assert result.summary.last_price == Decimal("100005")
    assert str(result.summary.last_price) == result.summary.raw_payload["last"]
    assert str(result.summary.current_funding) == result.summary.raw_payload["current_funding"]
    assert str(result.summary.funding_8h) == result.summary.raw_payload["funding_8h"]
    assert "interest_1h" not in result.summary.raw_payload
    assert "interest_8h" not in result.summary.raw_payload
    assert transport.calls[0][1] == {
        "Accept": "application/json",
        "User-Agent": "investment-analyst/0.1.0",
    }


def test_funding_filters_exact_end_boundary_and_preserves_historical_fields() -> None:
    transport = _Transport(
        _rpc(
            [
                _funding_row(_START),
                _funding_row(_START + timedelta(hours=1)),
                _funding_row(_END),
            ]
        )
    )
    client = DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _END + timedelta(hours=1),
    )

    result = client.fetch_funding_history("BTC-PERPETUAL", _START, _END)

    assert tuple(point.timestamp for point in result.points) == (
        _START,
        _START + timedelta(hours=1),
    )
    assert str(result.points[0].interest_1h) == "0.000001"
    assert str(result.points[0].interest_8h) == "0.000008"
    query = parse_qs(urlsplit(result.request_urls[0]).query)
    assert int(query["start_timestamp"][0]) == _ms(_START) - 1
    assert int(query["end_timestamp"][0]) == _ms(_END)


def test_funding_rejects_duplicate_timestamp_and_chunks_at_31_days() -> None:
    duplicate = _funding_row(_START)
    with pytest.raises(DeribitError, match="duplicate funding timestamp"):
        DeribitClient(
            _Transport(_rpc([duplicate, duplicate])),
            sleep=lambda _: None,
            clock=lambda: _END,
        ).fetch_funding_history("BTC-PERPETUAL", _START, _END)

    transport = _Transport(_rpc([]), _rpc([]))
    DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _START + timedelta(days=33),
    ).fetch_funding_history(
        "BTC-PERPETUAL",
        _START,
        _START + timedelta(days=32),
    )
    queries = tuple(parse_qs(urlsplit(call[0]).query) for call in transport.calls)
    assert int(queries[0]["end_timestamp"][0]) == _ms(_START + timedelta(days=31))
    assert int(queries[1]["start_timestamp"][0]) == _ms(_START + timedelta(days=31)) - 1


def test_historical_fetch_rejects_interval_not_closed_at_retrieval() -> None:
    with pytest.raises(DeribitError, match="fully closed interval"):
        DeribitClient(
            _Transport(_rpc([])),
            sleep=lambda _: None,
            clock=lambda: _END - timedelta(microseconds=1),
        ).fetch_funding_history("BTC-PERPETUAL", _START, _END)


def test_dvol_uses_continuation_as_next_end_and_orders_pages() -> None:
    continuation = _ms(_START + timedelta(days=2))
    transport = _Transport(
        _rpc(
            {
                "data": [_dvol_row(_START + timedelta(days=2), "57")],
                "continuation": continuation,
            }
        ),
        _rpc(
            {
                "data": [
                    _dvol_row(_START + timedelta(days=1), "56"),
                    _dvol_row(_START, "55"),
                ],
                "continuation": None,
            }
        ),
    )
    client = DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _END + timedelta(hours=1),
    )

    result = client.fetch_dvol_daily("BTC", _START, _END)

    assert tuple(candle.start for candle in result.candles) == (
        _START,
        _START + timedelta(days=1),
        _START + timedelta(days=2),
    )
    second_query = parse_qs(urlsplit(result.request_urls[1]).query)
    assert int(second_query["end_timestamp"][0]) == continuation
    assert second_query["resolution"] == ["1D"]


@pytest.mark.parametrize(
    ("continuations", "message"),
    [
        ((_ms(_START + timedelta(days=2)),) * 2, "repeated or cycled"),
        ((_ms(_START + timedelta(days=2)), _ms(_START + timedelta(days=3))), "decrease"),
        ((_ms(_START),), "decrease"),
    ],
)
def test_dvol_fails_closed_on_repetition_cycle_or_wrong_direction(
    continuations: tuple[int, ...],
    message: str,
) -> None:
    bodies = tuple(
        _rpc({"data": [], "continuation": continuation}) for continuation in continuations
    )
    transport = _Transport(*bodies)
    client = DeribitClient(
        transport,
        sleep=lambda _: None,
        clock=lambda: _END + timedelta(hours=1),
    )

    with pytest.raises(DeribitError, match=message):
        client.fetch_dvol_daily("BTC", _START, _END)


def test_response_limits_json_rpc_errors_and_floats_fail_closed() -> None:
    truncated = _Transport(_rpc([]), truncated=True)
    with pytest.raises(DeribitError, match="maximum body size"):
        DeribitClient(truncated, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )

    oversized = _Transport(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(DeribitError, match="maximum body size"):
        DeribitClient(oversized, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )

    unexpected_envelope = _Transport(
        json.dumps({"jsonrpc": "2.0", "result": [], "unexpected": True}).encode()
    )
    with pytest.raises(DeribitError, match="unexpected shape"):
        DeribitClient(unexpected_envelope, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )

    rpc_error = _Transport(json.dumps({"jsonrpc": "2.0", "error": {"code": 10028}}).encode())
    with pytest.raises(DeribitError, match="JSON-RPC error"):
        DeribitClient(rpc_error, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )

    with pytest.raises(ValidationError, match="exact Decimal"):
        DeribitFundingPoint(
            instrument_name="BTC-PERPETUAL",
            timestamp=_START,
            index_price=100000.0,
            prev_index_price=99999.0,
            interest_1h=0.1,
            interest_8h=0.8,
            raw_payload=_funding_row(_START),
        )


def test_historical_shapes_row_limits_and_conflicts_fail_closed() -> None:
    funding_cases = (
        (_rpc({}), "funding result must be a list"),
        (_rpc([_funding_row(_START)] * 1_001), "funding response exceeds"),
        (_rpc([{"timestamp": _ms(_START)}]), "funding row has an unexpected shape"),
    )
    for body, message in funding_cases:
        with pytest.raises(DeribitError, match=message):
            DeribitClient(
                _Transport(body),
                sleep=lambda _: None,
                clock=lambda: _END,
            ).fetch_funding_history("BTC-PERPETUAL", _START, _END)

    dvol_cases = (
        (_rpc([]), "DVOL result must be an object"),
        (_rpc({"data": [], "continuation": None, "extra": True}), "unexpected shape"),
        (_rpc({"data": {}, "continuation": None}), "DVOL data must be a list"),
        (
            _rpc({"data": [_dvol_row(_START, "55")] * 1_001, "continuation": None}),
            "DVOL response exceeds",
        ),
        (
            _rpc(
                {
                    "data": [_dvol_row(_START, "55"), _dvol_row(_START, "56")],
                    "continuation": None,
                }
            ),
            "conflicting DVOL candles",
        ),
    )
    for body, message in dvol_cases:
        with pytest.raises(DeribitError, match=message):
            DeribitClient(
                _Transport(body),
                sleep=lambda _: None,
                clock=lambda: _END,
            ).fetch_dvol_daily("BTC", _START, _END)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (json.dumps([]).encode(), "response must be an object"),
        (json.dumps({"jsonrpc": "1.0", "result": []}).encode(), "version is unexpected"),
        (json.dumps({"jsonrpc": "2.0", "id": None, "result": []}).encode(), "unexpected ID"),
        (json.dumps({"jsonrpc": "2.0"}).encode(), "does not contain result"),
        (
            json.dumps({"jsonrpc": "2.0", "result": [float("nan")]}).encode(),
            "non-finite JSON number",
        ),
    ],
)
def test_json_rpc_envelope_contract_fails_closed(body: bytes, message: str) -> None:
    with pytest.raises(DeribitError, match=message):
        DeribitClient(
            _Transport(body),
            sleep=lambda _: None,
            clock=lambda: _END,
        ).fetch_funding_history("BTC-PERPETUAL", _START, _END)


def test_redirect_and_invalid_summary_values_fail_closed() -> None:
    redirected = _Transport(
        _rpc([]),
        response_url="https://example.invalid/public/get_funding_rate_history",
    )
    with pytest.raises(DeribitError, match="redirected"):
        DeribitClient(redirected, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )

    original = json.loads((_FIXTURES / "btc_perpetual_summary.json").read_text())
    cases = (
        ("open_interest", -1, "must not be negative"),
        ("ask_price", 99999, "must not be below"),
        ("mid_price", 0, "must be positive"),
        ("instrument_name", "ETH-PERPETUAL", "does not match"),
        ("quote_currency", "EUR", "currency identity"),
        ("creation_timestamp", _ms(_END + timedelta(hours=1)), "after local retrieval"),
    )
    for field, value, message in cases:
        changed = json.loads(json.dumps(original))
        changed["result"][0][field] = value
        with pytest.raises((DeribitError, ValidationError), match=message):
            DeribitClient(
                _Transport(json.dumps(changed).encode()),
                sleep=lambda _: None,
                clock=lambda: _END,
            ).fetch_perpetual_summary("BTC-PERPETUAL")

    missing = json.loads(json.dumps(original))
    del missing["result"][0]["base_currency"]
    with pytest.raises(DeribitError, match="missing identity fields"):
        DeribitClient(
            _Transport(json.dumps(missing).encode()),
            sleep=lambda _: None,
            clock=lambda: _END,
        ).fetch_perpetual_summary("BTC-PERPETUAL")


def test_nullable_summary_fields_remain_absent_instead_of_zero() -> None:
    document = json.loads((_FIXTURES / "btc_perpetual_summary.json").read_text())
    document["result"][0]["last"] = None
    document["result"][0]["current_funding"] = None

    summary = (
        DeribitClient(
            _Transport(json.dumps(document).encode()),
            sleep=lambda _: None,
            clock=lambda: _END,
        )
        .fetch_perpetual_summary("BTC-PERPETUAL")
        .summary
    )

    assert summary.last_price is None
    assert summary.current_funding is None


def test_http_and_exhausted_transport_failures_are_compact_and_classified() -> None:
    http_error = _Transport(_rpc([]), status_code=429)
    with pytest.raises(DeribitError, match="HTTP 429") as direct:
        DeribitClient(http_error, sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )
    assert direct.value.status_code == 429

    class _FailedTransport:
        def get(self, url: str, **kwargs: object) -> HttpResponse:
            del kwargs
            raise HttpRequestError(url, "simulated-secret", status_code=503)

    with pytest.raises(DeribitError, match="bounded transport retries") as exhausted:
        DeribitClient(_FailedTransport(), sleep=lambda _: None).fetch_funding_history(
            "BTC-PERPETUAL",
            _START,
            _END,
        )
    assert exhausted.value.status_code == 503
    assert "simulated-secret" not in str(exhausted.value)
