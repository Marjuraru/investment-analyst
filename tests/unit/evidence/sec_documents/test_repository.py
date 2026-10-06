import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
    create_sec_terminal_script_difference,
    sec_document_metadata_sha256,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_from_raw_record,
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
        accepted_at=retrieved_at,
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v2"
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
        revision_schema_version="sec-document-revision-v2",
    )


def _submissions(
    record_id, available_at: datetime, *, asset_id: str = "equity:us:aapl"
) -> RawRecord:
    return RawRecord(
        record_id=record_id,
        asset_id=asset_id,
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


def _v2_revision(
    *,
    checksum: str,
    accepted_at: datetime,
    retrieved_at: datetime,
    discovery_id,
    content_size_bytes: int = 4,
) -> SecDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="10-K",
        filing_date=accepted_at.date(),
        report_date=date(2024, 12, 31),
        accepted_at=accepted_at,
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v2"
    )
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery_id,
        content_sha256=checksum,
        content_size_bytes=content_size_bytes,
        available_at=accepted_at,
        retrieved_at=retrieved_at,
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm",
        revision_schema_version="sec-document-revision-v2",
    )


def _acquisition_revision(
    *,
    prior: SecDocumentRevision | SecDocumentMetadataRevision | SecDocumentAcquisitionRevision,
    prior_content: bytes,
    current_content: bytes,
    observed_at: datetime,
    retrieved_at: datetime,
    discovery_id,
) -> SecDocumentAcquisitionRevision:
    document = prior.document
    metadata_sha256 = sec_document_metadata_sha256(document)
    current_checksum = hashlib.sha256(current_content).hexdigest()
    proof = create_sec_terminal_script_difference(
        prior_content,
        current_content,
        document_name=document.name,
    )
    revision_id = SecDocumentAcquisitionRevision.expected_id(
        document.document_id,
        current_checksum,
        metadata_sha256,
        prior.revision_id,
        prior.content_sha256,
    )
    return SecDocumentAcquisitionRevision(
        revision_id=revision_id,
        asset_id=prior.asset_id,
        document=document,
        prior_revision_id=prior.revision_id,
        raw_record_id=SecDocumentAcquisitionRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery_id,
        content_sha256=current_checksum,
        content_size_bytes=len(current_content),
        prior_content_sha256=prior.content_sha256,
        metadata_sha256=metadata_sha256,
        metadata_observed_at=observed_at,
        available_at=max(
            document.filing.accepted_at, observed_at, retrieved_at, prior.available_at
        ),
        retrieved_at=retrieved_at,
        source_url=prior.source_url,
        terminal_script_difference=proof,
    )


def _metadata_revision(
    *,
    prior: SecDocumentRevision | SecDocumentMetadataRevision,
    accepted_at: datetime,
    metadata_observed_at: datetime,
    retrieved_at: datetime,
    discovery_id,
) -> SecDocumentMetadataRevision:
    filing = prior.document.filing.model_copy(update={"accepted_at": accepted_at})
    document = prior.document.model_copy(update={"filing": filing})
    metadata_sha256 = sec_document_metadata_sha256(document)
    revision_id = SecDocumentMetadataRevision.expected_id(
        document.document_id,
        prior.content_sha256,
        metadata_sha256,
        prior.revision_id,
    )
    return SecDocumentMetadataRevision(
        revision_id=revision_id,
        asset_id=prior.asset_id,
        document=document,
        prior_revision_id=prior.revision_id,
        raw_record_id=SecDocumentMetadataRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery_id,
        content_sha256=prior.content_sha256,
        content_size_bytes=prior.content_size_bytes,
        metadata_sha256=metadata_sha256,
        metadata_observed_at=metadata_observed_at,
        available_at=max(accepted_at, metadata_observed_at, retrieved_at),
        retrieved_at=retrieved_at,
        source_url=prior.source_url,
    )


def _raw_path(storage: LocalStorage, record_id) -> Path:
    row = storage.store.connection.execute(
        "SELECT relative_path FROM raw_record_index WHERE record_id = ?", [str(record_id)]
    ).fetchone()
    assert row is not None
    return storage.paths.raw_dir / row[0]


def test_v2_document_lineage_rejects_a_discovery_from_another_asset(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 2, 1, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(
            _submissions(
                discovery_id,
                accepted_at + timedelta(days=1),
                asset_id="equity:us:msft",
            )
        )
        blob = storage.documents.put(b"one!")
        revision = _v2_revision(
            checksum=blob.sha256,
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(days=2),
            discovery_id=discovery_id,
        )

        with pytest.raises(StorageError, match="lineage asset does not match"):
            SecDocumentRepository(storage.raw_records, storage.documents).verify_revision(revision)


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


def test_replay_excludes_v1_legacy_records_and_reports_their_count(tmp_path: Path) -> None:
    known_at = datetime(2025, 3, 1, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, known_at - timedelta(days=60)))

        legacy_retrieved_at = known_at - timedelta(days=30)
        storage.raw_records.save(
            RawRecord(
                asset_id="equity:us:aapl",
                source=SourceReference(
                    source_id=SEC_DOCUMENT_SOURCE_ID,
                    retrieved_at=legacy_retrieved_at,
                ),
                event_time=legacy_retrieved_at,
                available_at=legacy_retrieved_at,
                received_at=legacy_retrieved_at,
                payload={"kind": "sec_document_revision", "revision": {}},
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION,
            )
        )

        repository = SecDocumentRepository(storage.raw_records, storage.documents)

        missing = repository.replay(asset_id="equity:us:aapl", known_at=known_at)
        assert missing.state == "missing"
        assert missing.legacy_records_excluded == 1

        blob = storage.documents.put(b"nine!")
        current = _revision(
            checksum=blob.sha256,
            retrieved_at=known_at - timedelta(days=1),
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(current))

        found = repository.replay(asset_id="equity:us:aapl", known_at=known_at)
        assert found.state == "found"
        assert found.revision == current
        assert found.legacy_records_excluded == 1


def test_v2_lineage_survives_discovery_captured_years_after_historical_acceptance(
    tmp_path: Path,
) -> None:
    """Real imports: EDGAR acceptance is old, the Submissions snapshot is captured at import
    time, and the document is downloaded right after. Availability (acceptance) must not be
    compared against the discovery timestamp; only acquisition causality is checked."""
    accepted_at = datetime(2020, 3, 1, tzinfo=UTC)
    discovered_at = datetime(2026, 8, 1, tzinfo=UTC)
    retrieved_at = datetime(2026, 8, 1, minute=5, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, discovered_at))
        blob = storage.documents.put(b"one!")
        revision = _v2_revision(
            checksum=blob.sha256,
            accepted_at=accepted_at,
            retrieved_at=retrieved_at,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        repository.verify_revision(revision)


def test_v2_lineage_rejects_discovery_received_after_document_retrieval(tmp_path: Path) -> None:
    """Acquisition causality still fails closed: discovering the filing after the document was
    already downloaded is not a valid lineage, even under v2 availability semantics."""
    accepted_at = datetime(2020, 3, 1, tzinfo=UTC)
    retrieved_at = datetime(2026, 8, 1, tzinfo=UTC)
    discovered_at = datetime(2026, 8, 2, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, discovered_at))
        blob = storage.documents.put(b"one!")
        revision = _v2_revision(
            checksum=blob.sha256,
            accepted_at=accepted_at,
            retrieved_at=retrieved_at,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        with pytest.raises(StorageError, match="received after the revision was retrieved"):
            repository.verify_revision(revision)


def test_metadata_revision_is_append_only_point_in_time_and_restores_shared_blob(
    tmp_path: Path,
) -> None:
    accepted_before = datetime(2025, 1, 31, 23, tzinfo=UTC)
    accepted_after = datetime(2025, 2, 1, 1, tzinfo=UTC)
    prior_observed = datetime(2025, 2, 1, tzinfo=UTC)
    prior_retrieved = datetime(2025, 2, 1, 1, tzinfo=UTC)
    metadata_observed = datetime(2025, 2, 2, tzinfo=UTC)
    metadata_retrieved = datetime(2025, 2, 3, tzinfo=UTC)
    prior_discovery_id = uuid4()
    metadata_discovery_id = uuid4()

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(prior_discovery_id, prior_observed))
        receipt = storage.documents.put(b"one!")
        prior = _v2_revision(
            checksum=receipt.sha256,
            accepted_at=accepted_before,
            retrieved_at=prior_retrieved,
            discovery_id=prior_discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(prior))
        storage.raw_records.save(_submissions(metadata_discovery_id, metadata_observed))
        corrected = _metadata_revision(
            prior=prior,
            accepted_at=accepted_after,
            metadata_observed_at=metadata_observed,
            retrieved_at=metadata_retrieved,
            discovery_id=metadata_discovery_id,
        )
        raw_record = revision_to_raw_record(corrected)
        storage.raw_records.save(raw_record)

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        before_correction = repository.replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 2, 12, tzinfo=UTC),
            accession="0000320193-25-000001",
            include_content=True,
        )
        after_correction = repository.replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 4, tzinfo=UTC),
            accession="0000320193-25-000001",
            include_content=True,
        )
        repository.verify_revision(corrected)
        decoded = revision_from_raw_record(raw_record)

    assert before_correction.revision == prior
    assert before_correction.content == b"one!"
    assert after_correction.revision == corrected
    assert after_correction.revision.document.filing.accepted_at == accepted_after
    assert after_correction.content == b"one!"
    assert decoded == corrected
    assert raw_record.schema_version == "sec-document-revision-v3"
    assert raw_record.payload["kind"] == "sec_document_metadata_revision"
    assert receipt.created


def test_metadata_revision_rejects_missing_prior_and_metadata_tampering(tmp_path: Path) -> None:
    observed_at = datetime(2025, 2, 2, tzinfo=UTC)
    retrieved_at = datetime(2025, 2, 3, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, observed_at))
        receipt = storage.documents.put(b"one!")
        prior = _v2_revision(
            checksum=receipt.sha256,
            accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
            retrieved_at=observed_at,
            discovery_id=discovery_id,
        )
        missing_prior = _metadata_revision(
            prior=prior,
            accepted_at=datetime(2025, 2, 1, tzinfo=UTC),
            metadata_observed_at=observed_at,
            retrieved_at=retrieved_at,
            discovery_id=discovery_id,
        )
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        storage.raw_records.save(revision_to_raw_record(missing_prior))

        with pytest.raises(StorageError, match="prior is missing"):
            repository.verify_revision(missing_prior)

        tampered_payload = dict(revision_to_raw_record(missing_prior).payload)
        tampered_revision = dict(tampered_payload["revision"])
        tampered_revision["metadata_sha256"] = "0" * 64
        tampered_payload["revision"] = tampered_revision
        tampered_record = revision_to_raw_record(missing_prior).model_copy(
            update={"payload": tampered_payload}
        )
        with pytest.raises(StorageError, match="revision is malformed"):
            revision_from_raw_record(tampered_record)


def test_v3_rejects_changed_content_hash_even_with_valid_revision_identity(tmp_path: Path) -> None:
    prior_discovery_id = uuid4()
    current_discovery_id = uuid4()
    observed_at = datetime(2025, 2, 2, tzinfo=UTC)
    accepted_at = datetime(2025, 2, 1, tzinfo=UTC)
    prior_content = b"prior"
    changed_content = b"other"

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(prior_discovery_id, observed_at))
        storage.raw_records.save(_submissions(current_discovery_id, observed_at))
        prior_blob = storage.documents.put(prior_content)
        changed_blob = storage.documents.put(changed_content)
        prior = _v2_revision(
            checksum=prior_blob.sha256,
            accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
            retrieved_at=observed_at,
            discovery_id=prior_discovery_id,
            content_size_bytes=len(prior_content),
        )
        current_document = prior.document.model_copy(
            update={"filing": prior.document.filing.model_copy(update={"accepted_at": accepted_at})}
        )
        metadata_sha256 = sec_document_metadata_sha256(current_document)
        revision_id = SecDocumentMetadataRevision.expected_id(
            current_document.document_id,
            changed_blob.sha256,
            metadata_sha256,
            prior.revision_id,
        )
        changed_v3 = SecDocumentMetadataRevision(
            revision_id=revision_id,
            asset_id=prior.asset_id,
            document=current_document,
            prior_revision_id=prior.revision_id,
            raw_record_id=SecDocumentMetadataRevision.expected_raw_record_id(revision_id),
            discovery_raw_record_id=current_discovery_id,
            content_sha256=changed_blob.sha256,
            content_size_bytes=len(changed_content),
            metadata_sha256=metadata_sha256,
            metadata_observed_at=observed_at,
            available_at=observed_at + timedelta(days=1),
            retrieved_at=observed_at + timedelta(days=1),
            source_url=prior.source_url,
        )
        storage.raw_records.save(revision_to_raw_record(prior))
        storage.raw_records.save(revision_to_raw_record(changed_v3))
        repository = SecDocumentRepository(storage.raw_records, storage.documents)

        with pytest.raises(StorageError, match="metadata revision prior conflicts"):
            repository.verify_revision(changed_v3)


def test_acquisition_revision_preserves_full_blobs_and_recomputes_proof(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    prior_discovery_id = uuid4()
    current_discovery_id = uuid4()
    observed_at = datetime(2025, 2, 2, tzinfo=UTC)
    retrieved_at = datetime(2025, 2, 3, tzinfo=UTC)
    prior_content = b"<html><body>filing</body></html>\n"
    script = b'<script type="text/javascript"  src="/rotated/path"></script>'
    current_content = prior_content.replace(b"</body>", script + b"</body>")

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(prior_discovery_id, accepted_at))
        prior_blob = storage.documents.put(prior_content)
        prior = _v2_revision(
            checksum=prior_blob.sha256,
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(hours=1),
            discovery_id=prior_discovery_id,
            content_size_bytes=len(prior_content),
        )
        storage.raw_records.save(revision_to_raw_record(prior))
        storage.raw_records.save(_submissions(current_discovery_id, observed_at))
        current_blob = storage.documents.put(current_content)
        acquired = _acquisition_revision(
            prior=prior,
            prior_content=prior_content,
            current_content=current_content,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            discovery_id=current_discovery_id,
        )
        acquired_raw = revision_to_raw_record(acquired)
        storage.raw_records.save(acquired_raw)

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        repository.verify_revision(acquired)
        history = repository.list_revisions(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 4, tzinfo=UTC),
            accession="0000320193-25-000001",
        )
        repository.verify_revision_history(history)
        before = repository.replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 2, tzinfo=UTC),
            accession="0000320193-25-000001",
            include_content=True,
        )
        after = repository.replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 4, tzinfo=UTC),
            accession="0000320193-25-000001",
            include_content=True,
        )
        tampered_payload = dict(acquired_raw.payload)
        tampered_revision = dict(tampered_payload["revision"])
        tampered_proof = dict(tampered_revision["terminal_script_difference"])
        tampered_proof["core_sha256"] = "0" * 64
        tampered_revision["terminal_script_difference"] = tampered_proof
        tampered_payload["revision"] = tampered_revision
        tampered = revision_from_raw_record(
            acquired_raw.model_copy(update={"payload": tampered_payload})
        )
        assert storage.documents.read(prior.content_sha256) == prior_content
        assert storage.documents.read(acquired.content_sha256) == current_content

        with pytest.raises(StorageError, match="terminal script proof is invalid"):
            repository.verify_revision(tampered)

    assert current_blob.sha256 == acquired.content_sha256
    assert current_blob.size_bytes == len(current_content)
    assert before.revision == prior
    assert before.content == prior_content
    assert after.revision == acquired
    assert after.content == current_content


def test_acquisition_history_rejects_divergent_forks(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    prior_discovery_id = uuid4()
    acquisition_discovery_id = uuid4()
    observed_at = datetime(2025, 2, 2, tzinfo=UTC)
    retrieved_at = datetime(2025, 2, 3, tzinfo=UTC)
    prior_content = b"<html><body>filing</body></html>\n"
    first_content = prior_content.replace(
        b"</body>",
        b'<script type="text/javascript"  src="/path/one"></script></body>',
    )
    second_content = prior_content.replace(
        b"</body>",
        b'<script type="text/javascript"  src="/path/two"></script></body>',
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(prior_discovery_id, accepted_at))
        prior_blob = storage.documents.put(prior_content)
        prior = _v2_revision(
            checksum=prior_blob.sha256,
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(hours=1),
            discovery_id=prior_discovery_id,
            content_size_bytes=len(prior_content),
        )
        storage.raw_records.save(revision_to_raw_record(prior))
        storage.raw_records.save(_submissions(acquisition_discovery_id, observed_at))
        first_blob = storage.documents.put(first_content)
        second_blob = storage.documents.put(second_content)
        first = _acquisition_revision(
            prior=prior,
            prior_content=prior_content,
            current_content=first_content,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            discovery_id=acquisition_discovery_id,
        )
        second = _acquisition_revision(
            prior=prior,
            prior_content=prior_content,
            current_content=second_content,
            observed_at=observed_at,
            retrieved_at=retrieved_at + timedelta(minutes=1),
            discovery_id=acquisition_discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(first))
        storage.raw_records.save(revision_to_raw_record(second))

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        with pytest.raises(StorageError, match="divergent forks"):
            repository.verify_revision_history([prior, first, second])

    assert first_blob.sha256 == first.content_sha256
    assert second_blob.sha256 == second.content_sha256
