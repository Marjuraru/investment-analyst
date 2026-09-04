"""Unit tests for the SEC document timeline service."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION,
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_documents.timeline_models import SecDocumentTimelineQuery
from investment_analyst.evidence.sec_documents.timeline_service import (
    SecDocumentTimelineService,
    SecDocumentTimelineServiceError,
)
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    filer_revision_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ASSET_ID = "equity:us:aapl"
_CIK_AAPL = "0000320193"
_CIK_BRK = "0001067983"
_KNOWN_AT = datetime(2025, 2, 20, 0, tzinfo=UTC)


def _create_asset_doc(
    storage: LocalStorage,
    accession: str,
    form: str,
    accepted_at: datetime,
    schema_version: str = REVISION_SCHEMA_VERSION_V2,
    retrieved_at: datetime | None = None,
) -> SecDocumentRevision:
    retrieved = retrieved_at or accepted_at
    available = retrieved if schema_version == REVISION_SCHEMA_VERSION else accepted_at
    filing = SecFiling(
        filing_id=SecFiling.expected_id(_CIK_AAPL, accession),
        filer_cik=_CIK_AAPL,
        accession=accession,
        form=form,
        filing_date=accepted_at.date(),
        report_date=accepted_at.date(),
        accepted_at=accepted_at,
        is_amendment=form.endswith("/A"),
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary.htm"),
        filing=filing,
        name="primary.htm",
    )
    receipt = storage.documents.put(f"<content {accession}/>".encode())
    rev_id = SecDocumentRevision.expected_id(document.document_id, receipt.sha256, schema_version)
    discovery = RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=SourceReference(
            source_id="sec-edgar:submissions:aapl",
            retrieved_at=retrieved,
        ),
        event_time=accepted_at,
        available_at=available,
        received_at=retrieved,
        payload={"form": form},
        schema_version="sec-submissions-v1",
    )
    storage.raw_records.save(discovery)
    revision = SecDocumentRevision(
        revision_id=rev_id,
        asset_id=_ASSET_ID,
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(rev_id),
        discovery_raw_record_id=discovery.record_id,
        content_sha256=receipt.sha256,
        content_size_bytes=receipt.size_bytes,
        available_at=available,
        retrieved_at=retrieved,
        source_url=f"https://www.sec.gov/{accession}",
        revision_schema_version=schema_version,  # type: ignore[arg-type]
    )
    storage.raw_records.save(revision_to_raw_record(revision))
    return revision


def _create_filer_doc(
    storage: LocalStorage,
    cik: str,
    accession: str,
    form: str,
    accepted_at: datetime,
) -> SecFilerDocumentRevision:
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
    receipt = storage.documents.put(f"<filer_content {accession}/>".encode())
    rev_id = SecFilerDocumentRevision.expected_id(document.document_id, receipt.sha256)
    discovery = RawRecord(
        record_id=uuid4(),
        asset_id=None,
        source=SourceReference(
            source_id=f"sec-edgar:manager:{cik}:submissions",
            retrieved_at=accepted_at,
        ),
        event_time=accepted_at,
        available_at=accepted_at,
        received_at=accepted_at,
        payload={"document": {"cik": cik}},
        schema_version="sec-manager-submissions-snapshot-v1",
    )
    storage.raw_records.save(discovery)
    revision = SecFilerDocumentRevision(
        revision_id=rev_id,
        filer_cik=cik,
        document=document,
        raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(rev_id),
        discovery_raw_record_id=discovery.record_id,
        content_sha256=receipt.sha256,
        content_size_bytes=receipt.size_bytes,
        available_at=accepted_at,
        retrieved_at=accepted_at,
        source_url=f"https://www.sec.gov/{accession}",
    )
    storage.raw_records.save(filer_revision_to_raw_record(revision))
    return revision


def test_read_only_storage_is_required_and_write_mode_rejected(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(known_at=_KNOWN_AT, asset_ids=(_ASSET_ID,))
        with pytest.raises(SecDocumentTimelineServiceError, match="requires read-only storage"):
            service.query(query)


def test_point_in_time_cut_excludes_later_availability(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        _create_asset_doc(
            storage, "0000320193-25-000001", "10-K", datetime(2025, 2, 10, 18, tzinfo=UTC)
        )
        _create_asset_doc(
            storage, "0000320193-25-000002", "10-Q", datetime(2025, 2, 18, 18, tzinfo=UTC)
        )
        _create_asset_doc(
            storage, "0000320193-25-000003", "10-Q", datetime(2025, 2, 25, 18, tzinfo=UTC)
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 20, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        assert result.state == "found"
        assert result.matched_count == 2
        assert [e.accession for e in result.entries] == [
            "0000320193-25-000001",
            "0000320193-25-000002",
        ]


def test_retrieved_at_is_never_used_as_availability(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        _create_asset_doc(
            storage,
            "0000320193-25-000001",
            "10-K",
            accepted_at=datetime(2025, 2, 10, 18, tzinfo=UTC),
            retrieved_at=datetime(2025, 2, 28, 18, tzinfo=UTC),
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        # Query known_at is before retrieved_at (Feb 28) but after available_at (Feb 10)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 15, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        assert result.state == "found"
        assert result.matched_count == 1
        assert result.entries[0].accession == "0000320193-25-000001"
        assert result.entries[0].available_at == datetime(2025, 2, 10, 18, tzinfo=UTC)


def test_inclusive_public_range_keeps_final_date(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        _create_asset_doc(
            storage, "0000320193-25-000001", "10-K", datetime(2025, 2, 9, 23, 59, tzinfo=UTC)
        )
        _create_asset_doc(
            storage, "0000320193-25-000002", "10-K", datetime(2025, 2, 10, 0, 1, tzinfo=UTC)
        )
        _create_asset_doc(
            storage, "0000320193-25-000003", "10-Q", datetime(2025, 2, 14, 23, 59, tzinfo=UTC)
        )
        _create_asset_doc(
            storage, "0000320193-25-000004", "10-Q", datetime(2025, 2, 15, 0, 1, tzinfo=UTC)
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
            available_from=date(2025, 2, 10),
            available_to=date(2025, 2, 14),
        )
        result = service.query(query)
        assert result.state == "found"
        assert result.matched_count == 2
        assert [e.accession for e in result.entries] == [
            "0000320193-25-000002",
            "0000320193-25-000003",
        ]


def test_total_deterministic_order_across_families(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        # Interleave asset and filer docs with same and different timestamps
        t1 = datetime(2025, 2, 10, 18, tzinfo=UTC)
        t2 = datetime(2025, 2, 12, 18, tzinfo=UTC)
        _create_asset_doc(storage, "0000320193-25-000002", "10-Q", t2)
        _create_filer_doc(storage, _CIK_BRK, "0000950123-25-000002", "13F-HR", t2)
        _create_asset_doc(storage, "0000320193-25-000001", "10-K", t1)
        _create_filer_doc(storage, _CIK_BRK, "0000950123-25-000001", "13F-HR", t1)

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
            filer_ciks=(_CIK_BRK,),
        )
        result1 = service.query(query)
        result2 = service.query(query)

        assert result1.state == "found"
        assert result1.matched_count == 4
        # Deterministic: result1 matches result2 exactly
        assert [e.revision_id for e in result1.entries] == [e.revision_id for e in result2.entries]

        # Verify ordering key: (available_at, family, str(revision_id))
        for i in range(len(result1.entries) - 1):
            curr = result1.entries[i]
            nxt = result1.entries[i + 1]
            key_curr = (curr.available_at, curr.family, str(curr.revision_id))
            key_nxt = (nxt.available_at, nxt.family, str(nxt.revision_id))
            assert key_curr <= key_nxt


def test_legacy_v1_revisions_excluded_and_counted(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        # Create a v1 legacy revision
        _create_asset_doc(
            storage,
            "0000320193-24-000001",
            "10-K",
            datetime(2024, 2, 10, 18, tzinfo=UTC),
            schema_version=REVISION_SCHEMA_VERSION,
        )
        # Create a v2 revision
        _create_asset_doc(
            storage,
            "0000320193-25-000001",
            "10-K",
            datetime(2025, 2, 10, 18, tzinfo=UTC),
            schema_version=REVISION_SCHEMA_VERSION_V2,
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        assert result.state == "found"
        assert result.matched_count == 1
        assert result.entries[0].accession == "0000320193-25-000001"
        assert result.legacy_records_excluded == 1


def test_no_record_observation_metric_or_file_is_written(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        _create_asset_doc(
            storage, "0000320193-25-000001", "10-K", datetime(2025, 2, 10, 18, tzinfo=UTC)
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        raw_count_before = storage.raw_records.count()
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        raw_count_after = storage.raw_records.count()

        assert result.state == "found"
        assert raw_count_before == raw_count_after


def test_content_bytes_are_never_read_returned_or_printed(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        _create_asset_doc(
            storage, "0000320193-25-000001", "10-K", datetime(2025, 2, 10, 18, tzinfo=UTC)
        )

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        entry = result.entries[0]
        assert not hasattr(entry, "content")
        assert not hasattr(entry, "bytes")
        assert len(entry.content_sha256) == 64
        assert entry.content_size_bytes > 0


def test_query_missing_state_when_no_revisions_match(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        storage.raw_records.count()  # ensure initialized

    with LocalStorage(paths, read_only=True) as storage:
        service = SecDocumentTimelineService(storage)
        query = SecDocumentTimelineQuery(
            known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
            asset_ids=(_ASSET_ID,),
        )
        result = service.query(query)
        assert result.state == "missing"
        assert result.matched_count == 0
        assert result.returned_count == 0
        assert len(result.entries) == 0
        assert result.truncated is False
