from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths


def _revision(*, checksum: str, retrieved_at: datetime, discovery_id) -> SecDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="10-K",
        filing_date=date(2025, 1, 31),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v1"
    )
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery_id,
        content_sha256=checksum,
        content_size_bytes=4,
        available_at=retrieved_at,
        retrieved_at=retrieved_at,
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm",
    )


def _submissions(record_id, available_at: datetime) -> RawRecord:
    return RawRecord(
        record_id=record_id,
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="sec-edgar:aapl:submissions",
            retrieved_at=available_at,
        ),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload={"document": {"cik": "0000320193"}},
        schema_version="sec-edgar-submissions-v1",
    )


def _raw_path(storage: LocalStorage, record_id) -> Path:
    row = storage.store.connection.execute(
        "SELECT relative_path FROM raw_record_index WHERE record_id = ?", [str(record_id)]
    ).fetchone()
    assert row is not None
    return storage.paths.raw_dir / row[0]


def test_replay_uses_sql_pit_filter_before_future_corrupt_record(tmp_path: Path) -> None:
    first_time = datetime(2025, 2, 1, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, first_time - timedelta(seconds=1)))
        first_blob = storage.documents.put(b"one!")
        first = _revision(
            checksum=first_blob.sha256, retrieved_at=first_time, discovery_id=discovery_id
        )
        storage.raw_records.save(revision_to_raw_record(first))
        second_blob = storage.documents.put(b"two!")
        second = _revision(
            checksum=second_blob.sha256,
            retrieved_at=first_time + timedelta(days=1),
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(second))
        _raw_path(storage, second.raw_record_id).write_text("corrupt", encoding="utf-8")

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        replay = repository.replay(
            asset_id="equity:us:aapl",
            known_at=first_time,
            accession="0000320193-25-000001",
            include_content=True,
        )
        assert replay.state == "found"
        assert replay.revision == first
        assert replay.content == b"one!"

        with pytest.raises(StorageError, match="checksum mismatch"):
            repository.replay(
                asset_id="equity:us:aapl",
                known_at=first_time + timedelta(days=2),
                accession="0000320193-25-000001",
            )
