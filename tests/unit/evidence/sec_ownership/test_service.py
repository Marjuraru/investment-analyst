from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_ownership.models import OwnershipQuery, OwnershipStatement
from investment_analyst.evidence.sec_ownership.repository import statement_to_raw_record
from investment_analyst.evidence.sec_ownership.service import OwnershipService
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_ASSET_ID = "equity:us:aapl"


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id=_ASSET_ID,
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _submissions(record_id, available_at: datetime) -> RawRecord:
    return RawRecord(
        record_id=record_id,
        asset_id=_ASSET_ID,
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=available_at),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload={"document": {"cik": "0000320193"}},
        schema_version="sec-edgar-submissions-v1",
    )


def _statement(
    *, accession: str, accepted_at: datetime, discovery_id
) -> tuple[OwnershipStatement, SecDocumentRevision]:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", accession),
        filer_cik="0000320193",
        accession=accession,
        form="4",
        filing_date=accepted_at.date(),
        report_date=accepted_at.date() - timedelta(days=1),
        accepted_at=accepted_at,
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "form4.xml"),
        filing=filing,
        name="form4.xml",
    )
    checksum = uuid4().hex + uuid4().hex[:32]
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v2"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id=_ASSET_ID,
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery_id,
        content_sha256=checksum,
        content_size_bytes=1,
        available_at=accepted_at,
        retrieved_at=accepted_at + timedelta(days=1),
        source_url="https://www.sec.gov/Archives/form4.xml",
        revision_schema_version="sec-document-revision-v2",
    )
    statement_id = OwnershipStatement.expected_id(
        revision.revision_id, "sec-ownership-statement-v2"
    )
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=_ASSET_ID,
        document_revision=revision,
        form="4",
        period_of_report=filing.report_date,
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        reporting_owners=(),
        entries=(),
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(days=1),
        schema_version="sec-ownership-statement-v2",
    ), revision


def _seed(storage, *, count: int, known_at: datetime) -> list[OwnershipStatement]:
    discovery_id = uuid4()
    storage.raw_records.save(_submissions(discovery_id, known_at - timedelta(days=365)))
    statements = []
    for index in range(count):
        accepted_at = known_at - timedelta(days=count - index)
        statement, revision = _statement(
            accession=f"0000320193-25-{index:06d}",
            accepted_at=accepted_at,
            discovery_id=discovery_id,
        )
        storage.raw_records.save(revision_to_raw_record(revision))
        storage.raw_records.save(statement_to_raw_record(statement))
        statements.append(statement)
    return statements


def test_query_returns_most_recent_first_and_truncates_with_total(tmp_path: Path) -> None:
    known_at = datetime(2025, 6, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        statements = _seed(storage, count=3, known_at=known_at)

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        service = OwnershipService(storage, configuration=_configuration())
        result = service.query(OwnershipQuery(asset_id=_ASSET_ID, known_at=known_at, limit=2))

        assert [item.statement_id for item in result.statements] == [
            statements[2].statement_id,
            statements[1].statement_id,
        ]
        assert result.total_matching == 3
        assert result.truncated is True
        assert result.legacy_records_excluded == 0


def test_query_reports_complete_untruncated_result(tmp_path: Path) -> None:
    known_at = datetime(2025, 6, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed(storage, count=2, known_at=known_at)

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        service = OwnershipService(storage, configuration=_configuration())
        result = service.query(OwnershipQuery(asset_id=_ASSET_ID, known_at=known_at, limit=10))

        assert result.total_matching == 2
        assert len(result.statements) == 2
        assert result.truncated is False


def test_query_rejects_writable_storage(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = OwnershipService(storage, configuration=_configuration())
        with pytest.raises(StorageError, match="read-only storage"):
            service.query(
                OwnershipQuery(asset_id=_ASSET_ID, known_at=datetime(2025, 6, 1, tzinfo=UTC))
            )
