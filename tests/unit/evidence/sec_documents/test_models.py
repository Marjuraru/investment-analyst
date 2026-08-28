from datetime import UTC, date, datetime

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)


def _document() -> SecLogicalDocument:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("320193", "0000320193-25-000001"),
        filer_cik="320193",
        accession="0000320193-25-000001",
        form="10-K",
        filing_date=date(2025, 1, 31),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 31, 18, tzinfo=UTC),
        is_amendment=False,
    )
    return SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )


def test_document_and_revision_ids_are_deterministic_and_separate() -> None:
    document = _document()
    checksum = "a" * 64
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v1"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=SecDocumentRevision.expected_raw_record_id(
            document.filing.filing_id
        ),
        content_sha256=checksum,
        content_size_bytes=3,
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm",
    )

    assert document.document_id != revision.revision_id
    assert revision.revision_id != revision.raw_record_id
    assert revision.document.filing.filer_cik == "0000320193"


def test_revision_rejects_backdated_availability_and_invalid_primary_path() -> None:
    document = _document()
    with pytest.raises(ValueError, match="available_at"):
        SecDocumentRevision(
            revision_id=SecDocumentRevision.expected_id(
                document.document_id, "b" * 64, "sec-document-revision-v1"
            ),
            asset_id="equity:us:aapl",
            document=document,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(
                SecDocumentRevision.expected_id(
                    document.document_id, "b" * 64, "sec-document-revision-v1"
                )
            ),
            discovery_raw_record_id=document.filing.filing_id,
            content_sha256="b" * 64,
            content_size_bytes=1,
            available_at=datetime(2025, 1, 1, tzinfo=UTC),
            retrieved_at=datetime(2025, 1, 2, tzinfo=UTC),
            source_url="https://www.sec.gov/Archives/x",
        )
    with pytest.raises(ValueError, match="primary document name"):
        SecLogicalDocument(
            document_id=document.document_id,
            filing=document.filing,
            name="../annual.htm",
        )


def test_v2_revision_uses_filing_acceptance_independently_of_retrieval() -> None:
    document = _document()
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, "c" * 64, "sec-document-revision-v2"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=document.filing.filing_id,
        content_sha256="c" * 64,
        content_size_bytes=1,
        available_at=document.filing.accepted_at,
        retrieved_at=datetime(2025, 2, 2, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/x",
        revision_schema_version="sec-document-revision-v2",
    )

    assert revision.available_at != revision.retrieved_at

    with pytest.raises(ValueError, match="SEC filing acceptance"):
        SecDocumentRevision(**{**revision.model_dump(), "available_at": revision.retrieved_at})
