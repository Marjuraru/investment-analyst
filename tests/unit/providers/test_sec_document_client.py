from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from investment_analyst.evidence.sec_documents.models import SecFiling, SecLogicalDocument
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecDocumentClient,
    SecDocumentClientError,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse


class _Transport:
    def __init__(self, response_url: str | None = None, *, truncated: bool = False) -> None:
        self.response_url = response_url
        self.truncated = truncated
        self.calls: list[tuple[str, Mapping[str, str], int | None]] = []

    def get(self, url, *, headers, timeout_seconds, max_response_bytes=None):
        del timeout_seconds
        self.calls.append((url, dict(headers), max_response_bytes))
        return HttpResponse(
            status_code=200,
            body=b"<html>SEC</html>",
            headers={},
            url=self.response_url or url,
            body_truncated=self.truncated,
        )


def _document() -> SecLogicalDocument:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="10-K",
        filing_date=date(2025, 1, 31),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
        is_amendment=False,
    )
    return SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )


def test_client_uses_exact_official_archives_url_and_bounded_response() -> None:
    transport = _Transport()
    client = SecDocumentClient(
        transport,
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
    )

    result = client.fetch(_document())

    assert (
        result.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm"
    )
    assert result.size_bytes == len(b"<html>SEC</html>")
    assert transport.calls[0][2] == 50 * 1024 * 1024
    assert "User-Agent" in transport.calls[0][1]


def test_client_rejects_redirect_or_truncated_response() -> None:
    identity = SecEdgarIdentity("Investment Analyst tests@example.com")
    with pytest.raises(SecDocumentClientError, match="redirected"):
        SecDocumentClient(
            _Transport("https://example.invalid/document"),
            identity,
            clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
        ).fetch(_document())
    with pytest.raises(SecDocumentClientError, match="empty or exceeds"):
        SecDocumentClient(
            _Transport(truncated=True),
            identity,
            clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
        ).fetch(_document())


class _ManifestTransport:
    def __init__(self, bodies: tuple[bytes, ...]) -> None:
        self._bodies = iter(bodies)
        self.calls: list[str] = []

    def get(self, url, *, headers, timeout_seconds, max_response_bytes=None):
        del headers, timeout_seconds, max_response_bytes
        self.calls.append(url)
        return HttpResponse(status_code=200, body=next(self._bodies), headers={}, url=url)


def test_client_resolves_only_unique_top_level_xml_from_official_manifest() -> None:
    transport = _ManifestTransport(
        (
            b'{"directory":{"item":[{"name":"form4.xml"},{"name":"form4.xsd"}]}}',
            b"<!DOCTYPE html><html/>",
            b"<ownershipDocument/>",
        )
    )
    filing = _document().filing.model_copy(
        update={
            "form": "4",
            "is_amendment": False,
            "filing_id": SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        }
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "xslF345X06/form4.xml"),
        filing=filing,
        name="xslF345X06/form4.xml",
    )
    client = SecDocumentClient(
        transport,
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
    )

    result = client.resolve_ownership_document(document)

    assert result.manifest.entries == ("form4.xml", "form4.xsd")
    assert result.locator.content == b"<!DOCTYPE html><html/>"
    assert result.semantic.content == b"<ownershipDocument/>"
    assert transport.calls[0].endswith("/index.json")
    assert transport.calls[-1].endswith("/form4.xml")


@pytest.mark.parametrize(
    "manifest",
    (
        b'{"directory":{"item":[{"name":"xslF345X06/form4.xml"}]}}',
        b'{"directory":{"item":[{"name":"form4.xml"},{"name":"copy.xml"}]}}',
        b'{"directory":{"item":[{"name":"../form4.xml"}]}}',
    ),
)
def test_client_rejects_ambiguous_or_unsafe_manifest(manifest: bytes) -> None:
    document = _document()
    transport = _ManifestTransport((manifest,))
    client = SecDocumentClient(
        transport,
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
    )

    with pytest.raises(SecDocumentClientError):
        client.resolve_ownership_document(document)
