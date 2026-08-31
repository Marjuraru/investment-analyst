from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_semantics_parser import (
    SecInstitutionalSemanticsParserError,
    parse_institutional_semantics,
)

_COVER = b"""<edgarSubmission schemaVersion="1.9"><submissionType>13F-HR</submissionType>
<filingManager><name>Manager LLC</name></filingManager><reportType>13F HOLDINGS REPORT</reportType>
<tableEntryTotal>2</tableEntryTotal><tableValueTotal>100</tableValueTotal>
<otherManagers2Info><otherManager2><sequenceNumber>1</sequenceNumber><name>Other LLC</name>
<cik>0000000123</cik></otherManager2></otherManagers2Info></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<putCall>PUT</putCall><investmentDiscretion>SOLE</investmentDiscretion><otherManager>1</otherManager>
<votingAuthority><Sole>10</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>50</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt>
<sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable></informationTable>"""


def _revision(name: str, content: bytes) -> SecFilerDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("1067983", "0000950123-25-000001"),
        filer_cik="1067983",
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
    digest = sha256(content).hexdigest()
    revision_id = SecFilerDocumentRevision.expected_id(document.document_id, digest)
    return SecFilerDocumentRevision(
        revision_id=revision_id,
        filer_cik=filing.filer_cik,
        document=document,
        raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=digest,
        content_size_bytes=len(content),
        available_at=filing.accepted_at,
        retrieved_at=datetime(2025, 2, 15, tzinfo=UTC),
        source_url=f"https://www.sec.gov/Archives/{name}",
    )


def test_parser_keeps_duplicate_rows_dimensions_and_unresolved_monetary_scale() -> None:
    item = parse_institutional_semantics(
        _COVER,
        _TABLE,
        parent_report_id=uuid4(),
        cover_revision=_revision("primary_doc.xml", _COVER),
        information_table_revision=_revision("infotable.xml", _TABLE),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )

    assert len(item.rows) == 2
    assert item.rows[0].cusip == item.rows[1].cusip
    assert item.rows[0].put_call == "PUT"
    assert item.rows[1].quantity_type == "PRN"
    assert item.rows[0].other_manager_sequence_references == ("1",)
    assert item.value_unit == "sec_13f_as_reported"
    assert item.monetary_scale_status == "unresolved"


def test_parser_rejects_unsafe_xml() -> None:
    with pytest.raises(SecInstitutionalSemanticsParserError):
        parse_institutional_semantics(
            b"<!DOCTYPE report><edgarSubmission/>",
            _TABLE,
            parent_report_id=uuid4(),
            cover_revision=_revision("primary_doc.xml", _COVER),
            information_table_revision=_revision("infotable.xml", _TABLE),
            parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
        )
