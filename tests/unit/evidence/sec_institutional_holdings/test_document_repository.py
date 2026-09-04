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


def test_filer_documents_are_enumerable_without_known_revision_id(tmp_path: Path) -> None:
    cik_1 = "0001067983"
    cik_2 = "0001350694"

    def _create_rev(
        storage: LocalStorage, cik: str, accession: str, form: str, accepted_at: datetime
    ) -> SecFilerDocumentRevision:
        captured = accepted_at
        discovery = RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{cik}:submissions", retrieved_at=captured
            ),
            event_time=accepted_at,
            available_at=accepted_at,
            received_at=captured,
            payload={"document": {"cik": cik}},
            schema_version="sec-manager-submissions-snapshot-v1",
        )
        filing = SecFiling(
            filing_id=SecFiling.expected_id(cik, accession),
            filer_cik=cik,
            accession=accession,
            form=form,
            filing_date=accepted_at.date(),
            report_date=None,
            accepted_at=accepted_at,
            is_amendment=form.endswith("/A"),
        )
        document = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary_doc.xml"),
            filing=filing,
            name="primary_doc.xml",
        )
        storage.raw_records.save(discovery)
        receipt = storage.documents.put(f"<content {accession}/>".encode())
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
            source_url=f"https://www.sec.gov/Archives/{accession}/primary_doc.xml",
        )
        repository = SecFilerDocumentRepository(storage.raw_records, storage.documents)
        repository.save(revision)
        return revision

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        rev1 = _create_rev(
            storage, cik_1, "0000950123-25-000001", "13F-HR", datetime(2025, 2, 10, 18, tzinfo=UTC)
        )
        rev2 = _create_rev(
            storage,
            cik_1,
            "0000950123-25-000002",
            "13F-HR/A",
            datetime(2025, 2, 12, 18, tzinfo=UTC),
        )
        rev3 = _create_rev(
            storage, cik_2, "0000950123-25-000003", "13F-HR", datetime(2025, 2, 11, 18, tzinfo=UTC)
        )
        rev4_future = _create_rev(
            storage, cik_1, "0000950123-25-000004", "13F-HR", datetime(2025, 2, 20, 18, tzinfo=UTC)
        )

        repo = SecFilerDocumentRepository(storage.raw_records, storage.documents)

        results_cik1 = repo.list_revisions(
            available_to=datetime(2025, 2, 15, tzinfo=UTC), filer_cik=cik_1
        )
        assert len(results_cik1) == 2
        assert [r.revision_id for r in results_cik1] == [rev1.revision_id, rev2.revision_id]
        assert results_cik1[0].available_at < results_cik1[1].available_at

        results_unnorm = repo.list_revisions(
            available_to=datetime(2025, 2, 15, tzinfo=UTC), filer_cik="1067983"
        )
        assert results_unnorm == results_cik1

        results_amendment = repo.list_revisions(
            available_to=datetime(2025, 2, 15, tzinfo=UTC), filer_cik=cik_1, form="13F-HR/A"
        )
        assert len(results_amendment) == 1
        assert results_amendment[0].revision_id == rev2.revision_id

        results_acc = repo.list_revisions(
            available_to=datetime(2025, 2, 15, tzinfo=UTC), accession="0000950123-25-000003"
        )
        assert len(results_acc) == 1
        assert results_acc[0].revision_id == rev3.revision_id

        all_past = repo.list_revisions(
            available_to=datetime(2025, 2, 15, tzinfo=UTC), filer_cik=cik_1
        )
        assert rev4_future.revision_id not in {r.revision_id for r in all_past}


def test_filer_enumeration_is_additive_and_read_only(tmp_path: Path) -> None:
    captured = datetime(2025, 2, 15, tzinfo=UTC)
    cik = "0001067983"
    accession = "0000950123-25-000001"
    discovery = RawRecord(
        record_id=uuid4(),
        asset_id=None,
        source=SourceReference(
            source_id=f"sec-edgar:manager:{cik}:submissions", retrieved_at=captured
        ),
        event_time=captured,
        available_at=captured,
        received_at=captured,
        payload={"document": {"cik": cik}},
        schema_version="sec-manager-submissions-snapshot-v1",
    )
    filing = SecFiling(
        filing_id=SecFiling.expected_id(cik, accession),
        filer_cik=cik,
        accession=accession,
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
        repo = SecFilerDocumentRepository(storage.raw_records, storage.documents)
        repo.save(revision)

        raw_count_before = storage.raw_records.count()
        results = repo.list_revisions(available_to=datetime(2025, 2, 20, tzinfo=UTC), filer_cik=cik)
        raw_count_after = storage.raw_records.count()

        assert len(results) == 1
        assert results[0] == revision
        assert raw_count_before == raw_count_after
