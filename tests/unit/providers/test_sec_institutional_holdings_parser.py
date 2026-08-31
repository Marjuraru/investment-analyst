from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    SecInstitutionalHoldingsParserError,
    classify_institutional_holdings_resource,
    parse_institutional_holdings,
)

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<formData><coverPage><form13FFileNumber>028-00001</form13FFileNumber></coverPage></formData>
<tableEntryTotal>1</tableEntryTotal>
<tableValueTotal>101</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


def _revision(name: str, digest: str) -> SecFilerDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0001067983", "0000950123-25-000001"),
        filer_cik="0001067983",
        accession="0000950123-25-000001",
        form="13F-HR",
        filing_date=date(2025, 2, 14),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
        filing=filing,
        name=name,
    )
    revision_id = SecFilerDocumentRevision.expected_id(document.document_id, digest)
    return SecFilerDocumentRevision(
        revision_id=revision_id,
        filer_cik=filing.filer_cik,
        document=document,
        raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=digest,
        content_size_bytes=12,
        available_at=filing.accepted_at,
        retrieved_at=datetime(2025, 2, 15, tzinfo=UTC),
        source_url=f"https://www.sec.gov/Archives/{name}",
    )


def test_parser_preserves_declared_fields_and_reports_total_mismatch() -> None:
    report, positions = parse_institutional_holdings(
        _COVER,
        _TABLE,
        cover_revision=_revision("primary_doc.xml", "a" * 64),
        information_table_revision=_revision("infotable.xml", "b" * 64),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )

    assert report.value_total_matches is False
    assert report.file_number == "028-00001"
    assert report.parsed_entry_total == 1
    assert positions[0].cusip == "037833100"
    assert positions[0].issuer_name == "APPLE INC"


def test_parser_rejects_forbidden_declaration_unexpected_root_and_bad_cusip() -> None:
    assert (
        classify_institutional_holdings_resource(
            b"<!DOCTYPE x><edgarSubmission/>", role="cover"
        ).reason_code
        == "forbidden_declaration"
    )
    assert (
        classify_institutional_holdings_resource(b"<wrong/>", role="information_table").reason_code
        == "unexpected_root"
    )
    with pytest.raises((SecInstitutionalHoldingsParserError, ValueError)):
        parse_institutional_holdings(
            _COVER,
            _TABLE.replace(b"037833100", b"BAD"),
            cover_revision=_revision("primary_doc.xml", "a" * 64),
            information_table_revision=_revision("infotable.xml", "b" * 64),
            parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
        )


def test_parser_selects_only_declarant_file_number_and_rejects_own_duplicates() -> None:
    other_managers = b"".join(
        b"<otherManager2><form13FFileNumber>028-OTHER</form13FFileNumber></otherManager2>"
        for _ in range(14)
    )
    cover = _COVER.replace(
        b"</edgarSubmission>",
        b"<summaryPage><otherManagers2Info>"
        + other_managers
        + b"</otherManagers2Info></summaryPage></edgarSubmission>",
    )
    report, _ = parse_institutional_holdings(
        cover,
        _TABLE,
        cover_revision=_revision("primary_doc.xml", "a" * 64),
        information_table_revision=_revision("infotable.xml", "b" * 64),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    assert report.file_number == "028-00001"

    duplicate = _COVER.replace(
        b"</coverPage>", b"<form13FFileNumber>028-00001</form13FFileNumber></coverPage>"
    )
    with pytest.raises(
        SecInstitutionalHoldingsParserError, match="ambiguous XML form13FFileNumber"
    ):
        parse_institutional_holdings(
            duplicate,
            _TABLE,
            cover_revision=_revision("primary_doc.xml", "a" * 64),
            information_table_revision=_revision("infotable.xml", "b" * 64),
            parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
        )


def test_parser_keeps_absent_or_blank_declarant_number_absent_despite_other_managers() -> None:
    without_own_number = _COVER.replace(
        b"<form13FFileNumber>028-00001</form13FFileNumber>", b""
    ).replace(
        b"</edgarSubmission>",
        b"<summaryPage><otherManagers2Info><otherManager2>"
        b"<form13FFileNumber>028-OTHER</form13FFileNumber>"
        b"</otherManager2></otherManagers2Info></summaryPage></edgarSubmission>",
    )
    report, _ = parse_institutional_holdings(
        without_own_number,
        _TABLE,
        cover_revision=_revision("primary_doc.xml", "a" * 64),
        information_table_revision=_revision("infotable.xml", "b" * 64),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    assert report.file_number is None
