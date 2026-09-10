"""Unit tests for official SEC Form 13F Data Sets provider and validator."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime

import pytest

from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
    Sec13FDataSetError,
    Sec13FDataSetLink,
    Sec13FDataSetsClient,
    parse_sec_dataset_token_date,
    parse_sec_date_value,
    validate_sec_13f_zip_archive,
)


class _DummyTransport(HttpTransport):
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.requested_urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.requested_urls.append(url)
        if url in self.responses:
            return self.responses[url]
        raise RuntimeError(f"Unexpected URL: {url}")


def _create_minimal_zip_bytes(
    members: dict[str, str] | None = None,
    *,
    corrupt: bool = False,
) -> bytes:
    if corrupt:
        return b"not a valid zip file"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        table_members = members or {
            "SUBMISSION.tsv": (
                "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
            ),
            "COVERPAGE.tsv": "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT\n",
            "INFOTABLE.tsv": "ACCESSION_NUMBER\tCUSIP\tVALUE\n",
        }
        for name, content in table_members.items():
            zf.writestr(name, content)
    return stream.getvalue()


def test_parse_sec_dataset_token_date() -> None:
    assert parse_sec_dataset_token_date("01mar2026") == date(2026, 3, 1)
    assert parse_sec_dataset_token_date("31dec2025") == date(2025, 12, 31)
    with pytest.raises(Sec13FDataSetError):
        parse_sec_dataset_token_date("invalid")
    with pytest.raises(Sec13FDataSetError):
        parse_sec_dataset_token_date("01foo2026")


def test_parse_sec_date_value() -> None:
    assert parse_sec_date_value("31-MAR-2026") == date(2026, 3, 31)
    assert parse_sec_date_value("2026-03-31") == date(2026, 3, 31)
    with pytest.raises(Sec13FDataSetError):
        parse_sec_date_value("")
    with pytest.raises(Sec13FDataSetError):
        parse_sec_date_value("invalid-date")


def test_discover_catalog_links_valid() -> None:
    html = """
    <html>
      <a href="/files/structureddata/data/form-13f-data-sets/01dec2025-28feb2026_form13f.zip">Q4</a>
      <a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">Q1</a>
      <a href="https://other.com/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">External</a>
    </html>
    """
    client = Sec13FDataSetsClient(SecEdgarIdentity("Analyst user@example.com"))
    links = client.discover_catalog_links(html)
    assert len(links) == 2
    # Sorted descending by period_end
    assert links[0].filename == "01mar2026-31may2026_form13f.zip"
    assert links[0].period_end == date(2026, 5, 31)
    assert links[1].filename == "01dec2025-28feb2026_form13f.zip"


def test_discover_catalog_links_duplicate_or_invalid_range() -> None:
    client = Sec13FDataSetsClient(SecEdgarIdentity("Analyst user@example.com"))

    # Inverted date range
    bad_range_html = (
        '<a href="/files/structureddata/data/form-13f-data-sets/'
        '31may2026-01mar2026_form13f.zip">bad</a>'
    )
    with pytest.raises(Sec13FDataSetError, match="Invalid period range"):
        client.discover_catalog_links(bad_range_html)

    # Duplicate link
    duplicate_html = """
    <a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">1</a>
    <a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">2</a>
    """
    with pytest.raises(Sec13FDataSetError, match="Duplicate"):
        client.discover_catalog_links(duplicate_html)


def test_fetch_catalog_page_safety() -> None:
    identity = SecEdgarIdentity("Analyst user@example.com")

    # Reject non-catalog URL
    client = Sec13FDataSetsClient(identity)
    with pytest.raises(Sec13FDataSetError, match="Only the official Form 13F catalog URL"):
        client.fetch_catalog_page("https://www.sec.gov/wrong")

    # Reject non-200
    transport_404 = _DummyTransport(
        {
            SEC_13F_DATA_SETS_CATALOG_URL: HttpResponse(
                404, b"Not found", {}, SEC_13F_DATA_SETS_CATALOG_URL
            )
        }
    )
    with pytest.raises(Sec13FDataSetError, match="failed with HTTP 404"):
        Sec13FDataSetsClient(identity, transport_404).fetch_catalog_page()

    # Reject redirect outside sec.gov
    transport_redirect = _DummyTransport(
        {
            SEC_13F_DATA_SETS_CATALOG_URL: HttpResponse(
                200, b"<html></html>", {}, "https://evil.com/catalog"
            )
        }
    )
    with pytest.raises(Sec13FDataSetError, match="redirected away"):
        Sec13FDataSetsClient(identity, transport_redirect).fetch_catalog_page()

    # Reject unexpected content-type
    transport_bad_ct = _DummyTransport(
        {
            SEC_13F_DATA_SETS_CATALOG_URL: HttpResponse(
                200,
                b'{"error":"json"}',
                {"Content-Type": "application/json"},
                SEC_13F_DATA_SETS_CATALOG_URL,
            )
        }
    )
    with pytest.raises(Sec13FDataSetError, match="Unexpected Content-Type"):
        Sec13FDataSetsClient(identity, transport_bad_ct).fetch_catalog_page()


def test_fetch_dataset_archive_safety() -> None:
    identity = SecEdgarIdentity("Analyst user@example.com")
    zip_bytes = _create_minimal_zip_bytes()
    zip_url = (
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
        "01mar2026-31may2026_form13f.zip"
    )
    link = Sec13FDataSetLink(
        url=zip_url,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        filename="01mar2026-31may2026_form13f.zip",
    )

    transport_ok = _DummyTransport(
        {link.url: HttpResponse(200, zip_bytes, {"Content-Type": "application/zip"}, link.url)}
    )
    client = Sec13FDataSetsClient(
        identity, transport_ok, clock=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    )
    download = client.fetch_dataset_archive(link)
    assert download.size_bytes == len(zip_bytes)
    assert download.period_start == date(2026, 3, 1)

    # Reject redirect away
    transport_redir = _DummyTransport(
        {
            link.url: HttpResponse(
                200, zip_bytes, {"Content-Type": "application/zip"}, "https://evil.com/zip"
            )
        }
    )
    with pytest.raises(Sec13FDataSetError, match="redirected away"):
        Sec13FDataSetsClient(identity, transport_redir).fetch_dataset_archive(link)


def test_validate_sec_13f_zip_archive_invariants() -> None:
    # 1. Valid zip passes
    valid_bytes = _create_minimal_zip_bytes()
    validate_sec_13f_zip_archive(valid_bytes)

    # 2. Corrupt / empty bytes fail
    with pytest.raises(Sec13FDataSetError, match="empty"):
        validate_sec_13f_zip_archive(b"")
    with pytest.raises(Sec13FDataSetError, match="not a valid ZIP"):
        validate_sec_13f_zip_archive(b"corrupt non zip")

    # 3. Missing required tables fails
    missing_table_zip = _create_minimal_zip_bytes(members={"SUBMISSION.tsv": "hdr\n"})
    with pytest.raises(Sec13FDataSetError, match="missing required member tables"):
        validate_sec_13f_zip_archive(missing_table_zip)

    # 4. Unknown/unauthorized table fails
    unauthorized_table_zip = _create_minimal_zip_bytes(
        members={
            "SUBMISSION.tsv": "h\n",
            "COVERPAGE.tsv": "h\n",
            "INFOTABLE.tsv": "h\n",
            "MALICIOUS.exe": "evil\n",
        }
    )
    with pytest.raises(Sec13FDataSetError, match="Unexpected member"):
        validate_sec_13f_zip_archive(unauthorized_table_zip)

    # 5. Path traversal fails
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("../traversal.tsv", "h\n")
        zf.writestr("SUBMISSION.tsv", "h\n")
        zf.writestr("COVERPAGE.tsv", "h\n")
        zf.writestr("INFOTABLE.tsv", "h\n")
    with pytest.raises(Sec13FDataSetError, match="Path traversal"):
        validate_sec_13f_zip_archive(stream.getvalue())

    # 6. More than 16 members fails
    stream_many = io.BytesIO()
    with zipfile.ZipFile(stream_many, "w") as zf:
        for i in range(17):
            zf.writestr(f"file_{i}.txt", "data")
    with pytest.raises(Sec13FDataSetError, match="exceeding limit of 16"):
        validate_sec_13f_zip_archive(stream_many.getvalue())
