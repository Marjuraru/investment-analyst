from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
)


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


def test_report_and_position_are_deterministic_and_unlinked() -> None:
    cover = _revision("primary_doc.xml", "a" * 64)
    table = _revision("infotable.xml", "b" * 64)
    report_id = InstitutionalHoldingsReport.expected_id(cover.revision_id, table.revision_id)
    report = InstitutionalHoldingsReport(
        report_id=report_id,
        raw_record_id=InstitutionalHoldingsReport.expected_raw_record_id(report_id),
        manager_cik="0001067983",
        manager_name="Manager LLC",
        report_period=date(2024, 12, 31),
        cover_revision=cover,
        information_table_revision=table,
        declared_entry_total=1,
        declared_value_total=Decimal("100"),
        parsed_entry_total=1,
        parsed_value_total=Decimal("100"),
        position_values_complete=True,
        entry_total_matches=True,
        value_total_matches=True,
        available_at=cover.available_at,
        parsed_at=table.retrieved_at,
    )
    position_id = InstitutionalHoldingPosition.expected_id(report_id, 1)
    values = dict(
        position_id=position_id,
        raw_record_id=InstitutionalHoldingPosition.expected_raw_record_id(position_id),
        report_id=report_id,
        information_table_revision=table,
        row_number=1,
        issuer_name="APPLE INC",
        title_of_class="COM",
        cusip="037833100",
        value=Decimal("100"),
        quantity=Decimal("10"),
        available_at=table.available_at,
        parsed_at=table.retrieved_at,
    )

    assert report.value_total_matches is True
    assert InstitutionalHoldingPosition(**values).cusip == "037833100"
    with pytest.raises(ValueError):
        InstitutionalHoldingPosition(**(values | {"asset_id": "equity:us:aapl"}))
    with pytest.raises(ValueError):
        InstitutionalHoldingPosition(**(values | {"value": 100.0}))
    with pytest.raises(ValueError, match="CUSIP"):
        InstitutionalHoldingPosition(**(values | {"cusip": "BAD"}))
