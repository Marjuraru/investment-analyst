from datetime import UTC, datetime, timedelta
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
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
    OwnershipResolutionOutcome,
    OwnershipStatement,
)
from investment_analyst.evidence.sec_ownership.repository import (
    OwnershipRepository,
    outcome_from_raw_record,
    outcome_to_raw_record,
    statement_from_raw_record,
    statement_to_raw_record,
    verify_ownership_records,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths


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


def _filing(*, accession: str, accepted_at: datetime) -> SecFiling:
    return SecFiling(
        filing_id=SecFiling.expected_id("0000320193", accession),
        filer_cik="0000320193",
        accession=accession,
        form="4",
        filing_date=accepted_at.date(),
        report_date=accepted_at.date() - timedelta(days=1),
        accepted_at=accepted_at,
        is_amendment=False,
    )


def _revision(
    *, accession: str, accepted_at: datetime, retrieved_at: datetime, checksum: str, discovery_id
) -> SecDocumentRevision:
    filing = _filing(accession=accession, accepted_at=accepted_at)
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "form4.xml"),
        filing=filing,
        name="form4.xml",
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
        available_at=accepted_at,
        retrieved_at=retrieved_at,
        source_url="https://www.sec.gov/Archives/form4.xml",
        revision_schema_version="sec-document-revision-v2",
    )


def _statement(*, revision: SecDocumentRevision, schema_version: str, parsed_at: datetime):
    statement_id = OwnershipStatement.expected_id(revision.revision_id, schema_version)
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form="4",
        period_of_report=revision.document.filing.report_date,
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        reporting_owners=(),
        entries=(),
        available_at=revision.available_at,
        parsed_at=parsed_at,
        schema_version=schema_version,
    )


def _outcome(*, filing: SecFiling, resolver_version: str, checksum: str, discovery_id):
    is_v2 = resolver_version == "sec-ownership-resolver-v2"
    schema_version = (
        OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2 if is_v2 else OWNERSHIP_OUTCOME_SCHEMA_VERSION
    )
    available_at = filing.accepted_at if is_v2 else filing.accepted_at + timedelta(days=1)
    retrieved_at = filing.accepted_at + timedelta(days=1)
    outcome_id = OwnershipResolutionOutcome.expected_id(
        filing.accession, "form4.xml", checksum, "accepted", schema_version
    )
    return OwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=OwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=discovery_id,
        declared_locator="form4.xml",
        resource_name="form4.xml",
        resource_url="https://www.sec.gov/Archives/form4.xml",
        content_sha256=checksum,
        content_size_bytes=4,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256=checksum,
        available_at=available_at if is_v2 else retrieved_at,
        retrieved_at=retrieved_at,
        status="accepted",
        reason_code="ok",
        resolver_version=resolver_version,
        schema_version=schema_version,
    )


def test_statement_v1_and_v2_round_trip_and_reject_cross_schema_records(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        # Discovery (Submissions capture) happens after acceptance and before the document
        # download, the realistic PIT ordering that used to raise before the lineage fix.
        storage.raw_records.save(_submissions(discovery_id, accepted_at + timedelta(hours=6)))
        blob = storage.documents.put(b"one!")
        revision = _revision(
            accession="0000320193-25-000001",
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(days=2),
            checksum=blob.sha256,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))

        v2_statement = _statement(
            revision=revision,
            schema_version="sec-ownership-statement-v2",
            parsed_at=accepted_at + timedelta(days=2),
        )
        record = statement_to_raw_record(v2_statement)
        assert record.schema_version == "sec-ownership-statement-v2"
        assert statement_from_raw_record(record) == v2_statement

        with pytest.raises(StorageError, match="malformed"):
            statement_from_raw_record(
                RawRecord(**{**record.model_dump(), "schema_version": "sec-ownership-statement-v9"})
            )


def test_ownership_list_uses_pit_cut_on_acceptance_not_retrieval(tmp_path: Path) -> None:
    known_at = datetime(2025, 2, 1, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, known_at - timedelta(days=30)))

        visible_blob = storage.documents.put(b"visible!")
        visible_revision = _revision(
            accession="0000320193-25-000001",
            accepted_at=known_at - timedelta(days=1),
            retrieved_at=known_at + timedelta(days=10),
            checksum=visible_blob.sha256,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(visible_revision))
        visible_statement = _statement(
            revision=visible_revision,
            schema_version="sec-ownership-statement-v2",
            parsed_at=known_at + timedelta(days=10),
        )
        storage.raw_records.save(statement_to_raw_record(visible_statement))

        # Same retrieved_at as the visible revision: only accepted_at (available_at) differs,
        # proving visibility is driven by acceptance and not by retrieval recency.
        hidden_blob = storage.documents.put(b"hidden!!")
        hidden_revision = _revision(
            accession="0000320193-25-000002",
            accepted_at=known_at + timedelta(days=1),
            retrieved_at=known_at + timedelta(days=10),
            checksum=hidden_blob.sha256,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(hidden_revision))
        hidden_statement = _statement(
            revision=hidden_revision,
            schema_version="sec-ownership-statement-v2",
            parsed_at=known_at + timedelta(days=10),
        )
        storage.raw_records.save(statement_to_raw_record(hidden_statement))

        visible = OwnershipRepository(storage.raw_records).list(
            asset_id="equity:us:aapl", known_at=known_at
        )
        assert [item.statement_id for item in visible] == [visible_statement.statement_id]


def test_verify_ownership_records_checks_v2_and_fails_closed_on_corruption(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        # Discovery (Submissions capture) happens after acceptance and before the document
        # download, the realistic PIT ordering that used to raise before the lineage fix.
        storage.raw_records.save(_submissions(discovery_id, accepted_at + timedelta(hours=6)))
        blob = storage.documents.put(b"one!")
        revision = _revision(
            accession="0000320193-25-000001",
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(days=2),
            checksum=blob.sha256,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))
        statement = _statement(
            revision=revision,
            schema_version="sec-ownership-statement-v2",
            parsed_at=accepted_at + timedelta(days=2),
        )
        record = statement_to_raw_record(statement)
        storage.raw_records.save(record)

        document_repository = SecDocumentRepository(storage.raw_records, storage.documents)
        verify_ownership_records([record], document_repository, storage.documents)

        blob_path = (
            storage.paths.documents_dir
            / "sha256"
            / blob.sha256[:2]
            / blob.sha256[2:4]
            / blob.sha256
        )
        blob_path.write_bytes(b"nope")

        with pytest.raises(StorageError, match="checksum mismatch"):
            verify_ownership_records([record], document_repository, storage.documents)


def test_outcome_v1_and_v2_coexist_without_identity_conflict(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    discovery_id = uuid4()
    filing = _filing(accession="0000320193-25-000001", accepted_at=accepted_at)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, accepted_at - timedelta(days=1)))
        repository = OwnershipRepository(storage.raw_records)

        v1_outcome = _outcome(
            filing=filing,
            resolver_version="sec-ownership-resolver-v1",
            checksum="e" * 64,
            discovery_id=discovery_id,
        )
        v2_outcome = _outcome(
            filing=filing,
            resolver_version="sec-ownership-resolver-v2",
            checksum="e" * 64,
            discovery_id=discovery_id,
        )
        assert v1_outcome.outcome_id != v2_outcome.outcome_id

        repository.save_outcome(v1_outcome)
        repository.save_outcome(v2_outcome)

        assert repository.get_outcome(v1_outcome.outcome_id) == v1_outcome
        assert repository.get_outcome(v2_outcome.outcome_id) == v2_outcome
        assert outcome_from_raw_record(outcome_to_raw_record(v2_outcome)) == v2_outcome


def test_statement_decode_rejects_a_v1_payload_relabeled_as_v2(tmp_path: Path) -> None:
    """A genuinely v1 statement cannot be promoted to v2 by relabeling its outer RawRecord."""
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions(discovery_id, accepted_at + timedelta(hours=6)))
        blob = storage.documents.put(b"one!")
        revision = _revision(
            accession="0000320193-25-000001",
            accepted_at=accepted_at,
            retrieved_at=accepted_at + timedelta(days=2),
            checksum=blob.sha256,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))
        statement = _statement(
            revision=revision,
            schema_version="sec-ownership-statement-v2",
            parsed_at=accepted_at + timedelta(days=2),
        )
        record = statement_to_raw_record(statement)

        with pytest.raises(StorageError, match="schema conflicts"):
            statement_from_raw_record(
                RawRecord(**{**record.model_dump(), "schema_version": "sec-ownership-statement-v1"})
            )


def test_outcome_decode_rejects_a_relabeled_schema(tmp_path: Path) -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    discovery_id = uuid4()
    filing = _filing(accession="0000320193-25-000001", accepted_at=accepted_at)
    v2_outcome = _outcome(
        filing=filing,
        resolver_version="sec-ownership-resolver-v2",
        checksum="e" * 64,
        discovery_id=discovery_id,
    )
    record = outcome_to_raw_record(v2_outcome)

    with pytest.raises(StorageError, match="schema conflicts"):
        outcome_from_raw_record(
            RawRecord(**{**record.model_dump(), "schema_version": "sec-ownership-outcome-v1"})
        )
