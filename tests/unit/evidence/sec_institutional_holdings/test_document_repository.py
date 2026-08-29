from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    verify_document_records,
)
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    SecFilerDocumentRepository,
    filer_revision_from_raw_record,
    filer_revision_to_raw_record,
    verify_filer_document_records,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def test_filer_document_round_trip_and_lineage_verification(tmp_path: Path) -> None:
    captured = datetime(2025, 2, 15, tzinfo=UTC)
    discovery = RawRecord(
        record_id=uuid4(),
        asset_id=None,
        source=SourceReference(
            source_id="sec-edgar:manager:0001067983:submissions", retrieved_at=captured
        ),
        event_time=captured,
        available_at=captured,
        received_at=captured,
        payload={"document": {"cik": "1067983"}},
        schema_version="sec-manager-submissions-snapshot-v1",
    )
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0001067983", "0000950123-25-000001"),
        filer_cik="0001067983",
        accession="0000950123-25-000001",
        form="13F-HR",
        filing_date=date(2025, 2, 14),
        report_date=None,
        accepted_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary_doc.xml"),
        filing=filing,
        name="primary_doc.xml",
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(discovery)
        receipt = storage.documents.put(b"<edgarSubmission/>")
        revision_id = SecFilerDocumentRevision.expected_id(document.document_id, receipt.sha256)
        revision = SecFilerDocumentRevision(
            revision_id=revision_id,
            filer_cik=filing.filer_cik,
            document=document,
            raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
            discovery_raw_record_id=discovery.record_id,
            content_sha256=receipt.sha256,
            content_size_bytes=receipt.size_bytes,
            available_at=filing.accepted_at,
            retrieved_at=captured,
            source_url="https://www.sec.gov/Archives/primary_doc.xml",
        )
        repository = SecFilerDocumentRepository(storage.raw_records, storage.documents)
        repository.save(revision)
        record = filer_revision_to_raw_record(revision)

        assert filer_revision_from_raw_record(record) == revision
        verify_filer_document_records((record,), repository)
        verify_document_records(
            (record,), SecDocumentRepository(storage.raw_records, storage.documents)
        )
