from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.instrument_correspondence.service import (
    InstrumentCorrespondenceQuery,
    InstrumentCorrespondenceService,
)
from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
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


def _revision(
    name: str, digest: str, *, report_period: date | None = date(2024, 12, 31)
) -> SecFilerDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0001067983", "0000950123-25-000001"),
        filer_cik="0001067983",
        accession="0000950123-25-000001",
        form="13F-HR",
        filing_date=date(2025, 2, 14),
        report_date=report_period,
        accepted_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name), filing=filing, name=name
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


def _seed(root: Path, *, missing_report_period: bool = False) -> None:
    cover = (
        _COVER
        if not missing_report_period
        else _COVER.replace(b"<reportCalendarOrQuarter>2024-12-31</reportCalendarOrQuarter>", b"")
    )
    report, positions = parse_institutional_holdings(
        cover,
        _TABLE,
        cover_revision=_revision(
            "primary_doc.xml",
            "a" * 64,
            report_period=None if missing_report_period else date(2024, 12, 31),
        ),
        information_table_revision=_revision(
            "infotable.xml",
            "b" * 64,
            report_period=None if missing_report_period else date(2024, 12, 31),
        ),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    correspondence = InstrumentCorrespondence.declare(
        asset_id="equity:us:aapl",
        cusip="037833100",
        title_of_class="COM",
        effective_from=date(2020, 1, 1),
        effective_to=None,
        available_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        recorded_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        holdings.save_report(report)
        for position in positions:
            holdings.save_position(position)
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )


def test_projection_is_point_in_time_and_links_declared_evidence(tmp_path: Path) -> None:
    _seed(tmp_path)
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        service = InstrumentCorrespondenceService(storage)
        before = service.query(
            InstrumentCorrespondenceQuery(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                known_at=datetime(2025, 2, 14, 17, tzinfo=UTC),
            )
        )
        after = service.query(
            InstrumentCorrespondenceQuery(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                known_at=datetime(2025, 2, 15, tzinfo=UTC),
            )
        )

    assert before.reports == ()
    assert after.total_positions == 1
    assert len(after.linked_positions) == 1
    assert after.unlinked_positions == ()


def test_projection_keeps_missing_report_period_unlinked(tmp_path: Path) -> None:
    _seed(tmp_path, missing_report_period=True)
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        result = InstrumentCorrespondenceService(storage).query(
            InstrumentCorrespondenceQuery(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                known_at=datetime(2025, 2, 15, tzinfo=UTC),
            )
        )

    assert result.linked_positions == ()
    assert result.unlinked_positions[0].reason == "missing_report_period"
