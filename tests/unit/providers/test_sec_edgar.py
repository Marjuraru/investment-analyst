"""Unit tests for the fixed Apple SEC EDGAR client."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_ENTITY_NAME,
    SecDocumentType,
    SecEdgarClient,
    SecEdgarError,
    SecEdgarIdentity,
)
from investment_analyst.providers.http import HttpResponse

SUBMISSIONS = Path("tests/fixtures/sec/aapl_submissions.json").read_bytes()
COMPANY_FACTS = Path("tests/fixtures/sec/aapl_companyfacts.json").read_bytes()
RETRIEVED_AT = datetime(2026, 7, 13, 18, tzinfo=UTC)


class QueueTransport:
    """Offline transport returning queued response bodies."""

    def __init__(
        self,
        bodies: list[bytes],
        *,
        statuses: list[int] | None = None,
        response_urls: list[str] | None = None,
    ) -> None:
        self.bodies = list(bodies)
        self.statuses = list(statuses or [200] * len(bodies))
        self.response_urls = list(response_urls or [])
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        response_url = self.response_urls.pop(0) if self.response_urls else url
        return HttpResponse(
            status_code=self.statuses.pop(0),
            body=self.bodies.pop(0),
            headers={},
            url=response_url,
        )


def _client(
    transport: QueueTransport,
    *,
    sleeps: list[float] | None = None,
    clock=lambda: RETRIEVED_AT,
) -> SecEdgarClient:
    pauses = sleeps if sleeps is not None else []
    return SecEdgarClient(
        transport,
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        sleep=pauses.append,
        clock=clock,
    )


def _replace_json(body: bytes, **changes: object) -> bytes:
    document = json.loads(body)
    document.update(changes)
    return json.dumps(document).encode()


def test_identity_validation_and_redacted_repr() -> None:
    identity = SecEdgarIdentity("  Investment Analyst contact@example.com  ")

    assert identity.user_agent == "Investment Analyst contact@example.com"
    assert "contact@example.com" not in repr(identity)
    with pytest.raises(SecEdgarError, match="empty"):
        SecEdgarIdentity("   ")
    with pytest.raises(SecEdgarError, match="contact email"):
        SecEdgarIdentity("Investment Analyst")
    with pytest.raises(SecEdgarError, match="descriptive"):
        SecEdgarIdentity("contact@example.com")


def test_client_uses_exact_headers_two_requests_and_pause() -> None:
    transport = QueueTransport([SUBMISSIONS, COMPANY_FACTS])
    sleeps: list[float] = []

    result = _client(transport, sleeps=sleeps).fetch_aapl_issuer_documents()

    assert result.cik == APPLE_CIK
    assert result.entity_name == APPLE_ENTITY_NAME
    assert len(result.documents) == 2
    assert [document.document_type for document in result.documents] == [
        SecDocumentType.SUBMISSIONS,
        SecDocumentType.COMPANY_FACTS,
    ]
    assert len(transport.calls) == 2
    assert sleeps == [0.5]
    for url, headers, timeout in transport.calls:
        assert url.startswith("https://data.sec.gov/")
        assert headers == {
            "Accept": "application/json",
            "User-Agent": "Investment Analyst tests@example.com",
        }
        assert "Authorization" not in headers
        assert "tests@example.com" not in url
        assert timeout == 30.0


def test_client_preserves_numbers_as_strings_without_float() -> None:
    result = _client(QueueTransport([SUBMISSIONS, COMPANY_FACTS])).fetch_aapl_issuer_documents()
    submissions, company_facts = result.documents

    assert submissions.body["cik"] == "320193"
    facts = company_facts.body["facts"]
    assert isinstance(facts, dict)
    gaap = facts["us-gaap"]
    assert isinstance(gaap, dict)
    concept = gaap["SyntheticRevenue"]
    assert isinstance(concept, dict)
    units = concept["units"]
    assert isinstance(units, dict)
    rows = units["USD"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    assert first["val"] == "1000.25"
    assert first["fy"] == "2025"


def test_numeric_and_string_cik_are_normalized() -> None:
    numeric = _client(QueueTransport([SUBMISSIONS, COMPANY_FACTS])).fetch_aapl_issuer_documents()
    string_submissions = _replace_json(SUBMISSIONS, cik="320193")
    string_result = _client(
        QueueTransport([string_submissions, COMPANY_FACTS])
    ).fetch_aapl_issuer_documents()

    assert numeric.documents[0].cik == APPLE_CIK
    assert string_result.documents[0].cik == APPLE_CIK


def test_checksums_content_lengths_and_utc_timestamp_are_stable() -> None:
    offset = timezone(timedelta(hours=-5))
    clock_value = datetime(2026, 7, 13, 13, tzinfo=offset)
    result = _client(
        QueueTransport([SUBMISSIONS, COMPANY_FACTS]),
        clock=lambda: clock_value,
    ).fetch_aapl_issuer_documents()

    assert result.retrieved_at == RETRIEVED_AT
    assert result.retrieved_at.tzinfo is UTC
    assert result.documents[0].body_sha256 == hashlib.sha256(SUBMISSIONS).hexdigest()
    assert result.documents[1].body_sha256 == hashlib.sha256(COMPANY_FACTS).hexdigest()
    assert result.documents[0].content_length == len(SUBMISSIONS)
    assert result.documents[1].content_length == len(COMPANY_FACTS)


@pytest.mark.parametrize(
    ("submissions", "company_facts", "message"),
    [
        (_replace_json(SUBMISSIONS, cik="1"), COMPANY_FACTS, "CIK"),
        (_replace_json(SUBMISSIONS, name="Other Corp."), COMPANY_FACTS, "entity"),
        (_replace_json(SUBMISSIONS, tickers=["OTHER"]), COMPANY_FACTS, "AAPL"),
        (
            json.dumps(
                {"cik": 320193, "name": "Apple Inc.", "tickers": ["AAPL"], "exchanges": ["Nasdaq"]}
            ).encode(),
            COMPANY_FACTS,
            "required fields",
        ),
        (SUBMISSIONS, _replace_json(COMPANY_FACTS, facts=[]), "facts"),
    ],
)
def test_client_rejects_invalid_issuer_documents(
    submissions: bytes,
    company_facts: bytes,
    message: str,
) -> None:
    with pytest.raises(SecEdgarError, match=message):
        _client(QueueTransport([submissions, company_facts])).fetch_aapl_issuer_documents()


def test_client_rejects_non_object_non_finite_and_boolean_json() -> None:
    with pytest.raises(SecEdgarError, match="JSON object"):
        _client(QueueTransport([b"[]", COMPANY_FACTS])).fetch_aapl_issuer_documents()
    with pytest.raises(SecEdgarError, match="non-standard"):
        _client(
            QueueTransport([SUBMISSIONS, COMPANY_FACTS.replace(b"1000.25", b"NaN")])
        ).fetch_aapl_issuer_documents()
    boolean_body = COMPANY_FACTS.replace(
        (
            b'"description": "Synthetic SEC company-facts fixture for offline '
            b'tests; values are not real."'
        ),
        b'"description": true',
    )
    with pytest.raises(SecEdgarError, match="objects, arrays, strings, and null"):
        _client(QueueTransport([SUBMISSIONS, boolean_body])).fetch_aapl_issuer_documents()


def test_client_rejects_status_redirect_large_response_and_naive_clock(monkeypatch) -> None:
    with pytest.raises(SecEdgarError, match="HTTP 404"):
        _client(
            QueueTransport([SUBMISSIONS, COMPANY_FACTS], statuses=[404, 200])
        ).fetch_aapl_issuer_documents()
    with pytest.raises(SecEdgarError, match="redirected"):
        _client(
            QueueTransport(
                [SUBMISSIONS, COMPANY_FACTS],
                response_urls=["https://example.com/a", "https://data.sec.gov/b"],
            )
        ).fetch_aapl_issuer_documents()

    import investment_analyst.providers.fundamentals.sec_edgar as module

    monkeypatch.setattr(module, "_MAX_RESPONSE_BYTES", 10)
    with pytest.raises(SecEdgarError, match="safety limit"):
        _client(QueueTransport([SUBMISSIONS, COMPANY_FACTS])).fetch_aapl_issuer_documents()
    monkeypatch.setattr(module, "_MAX_RESPONSE_BYTES", 50 * 1024 * 1024)
    with pytest.raises(SecEdgarError, match="timezone"):
        _client(
            QueueTransport([SUBMISSIONS, COMPANY_FACTS]),
            clock=lambda: datetime(2026, 7, 13),
        ).fetch_aapl_issuer_documents()


def test_client_rejects_non_official_or_insecure_base_urls() -> None:
    transport = QueueTransport([SUBMISSIONS, COMPANY_FACTS])
    identity = SecEdgarIdentity("Investment Analyst tests@example.com")

    for base_url in (
        "http://data.sec.gov",
        "https://example.com",
        "https://data.sec.gov/other",
    ):
        with pytest.raises(SecEdgarError, match="base_url"):
            SecEdgarClient(transport, identity, base_url=base_url)
