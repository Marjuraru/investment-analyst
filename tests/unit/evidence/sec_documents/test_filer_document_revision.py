from datetime import UTC, date, datetime

import pytest

from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)


def _values() -> dict[str, object]:
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
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary_doc.xml"),
        filing=filing,
        name="primary_doc.xml",
    )
    revision_id = SecFilerDocumentRevision.expected_id(document.document_id, "a" * 64)
    return {
        "revision_id": revision_id,
        "filer_cik": "0001067983",
        "document": document,
        "raw_record_id": SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        "discovery_raw_record_id": SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        "content_sha256": "a" * 64,
        "content_size_bytes": 12,
        "available_at": filing.accepted_at,
        "retrieved_at": datetime(2025, 2, 15, tzinfo=UTC),
        "source_url": "https://www.sec.gov/Archives/primary_doc.xml",
    }


def test_filer_revision_is_disjoint_and_forbids_asset_id() -> None:
    values = _values()
    revision = SecFilerDocumentRevision(**values)
    asset_revision_id = SecDocumentRevision.expected_id(
        revision.document.document_id, revision.content_sha256, REVISION_SCHEMA_VERSION_V2
    )

    assert revision.revision_id != asset_revision_id
    with pytest.raises(ValueError):
        SecFilerDocumentRevision(**(values | {"asset_id": "equity:us:aapl"}))


def test_filer_revision_requires_matching_cik_and_acceptance_time() -> None:
    values = _values()

    with pytest.raises(ValueError, match="CIK conflicts"):
        SecFilerDocumentRevision(**(values | {"filer_cik": "0000320193"}))
    with pytest.raises(ValueError, match="availability"):
        SecFilerDocumentRevision(**(values | {"available_at": datetime(2025, 2, 15, tzinfo=UTC)}))
