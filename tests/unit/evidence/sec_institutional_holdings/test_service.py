from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsQuery,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.service import (
    InstitutionalHoldingsService,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    parse_institutional_holdings,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>2024-12-31</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value></infoTable>
</informationTable>"""


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


def test_query_applies_known_at_period_and_truncation(tmp_path: Path) -> None:
    report, positions = parse_institutional_holdings(
        _COVER,
        _TABLE,
        cover_revision=_revision("primary_doc.xml", "a" * 64),
        information_table_revision=_revision("infotable.xml", "b" * 64),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        repository.save_report(report)
        for position in positions:
            repository.save_position(position)

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        service = InstitutionalHoldingsService(storage)
        before = service.query(
            InstitutionalHoldingsQuery(
                manager_cik="1067983",
                known_at=datetime(2025, 2, 14, 17, tzinfo=UTC),
            )
        )
        after = service.query(
            InstitutionalHoldingsQuery(
                manager_cik="1067983",
                known_at=datetime(2025, 2, 15, tzinfo=UTC),
                period_from=date(2024, 12, 31),
                period_to=date(2024, 12, 31),
            )
        )

    assert before.reports == ()
    assert after.reports == (report,)
    assert after.positions == positions
    assert after.total_matching == 1
    assert after.truncated is False
