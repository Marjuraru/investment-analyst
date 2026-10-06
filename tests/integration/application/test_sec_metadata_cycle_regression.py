import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_document_corpus import SecDocumentCorpusApplication
from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentQuery,
    SecDocumentRevision,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import SecDocumentRepository
from investment_analyst.evidence.sec_documents.timeline_models import SecDocumentTimelineQuery
from investment_analyst.evidence.sec_documents.timeline_service import SecDocumentTimelineService
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentPipeline,
    SecDocumentPipelineError,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_ACCESSION = "0000320193-25-000001"
_SUBMISSIONS_SOURCE_ID = "sec-edgar:aapl:submissions"


class _FixtureDocumentClient:
    def __init__(self, retrieved_at: datetime, content: bytes) -> None:
        self.retrieved_at = retrieved_at
        self.content = content
        self.calls = 0

    def fetch(self, document: SecLogicalDocument) -> SecPrimaryDocumentResponse:
        self.calls += 1
        content = self.content
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=(
                "https://www.sec.gov/Archives/edgar/data/320193/"
                f"{_ACCESSION.replace('-', '')}/annual.htm"
            ),
            retrieved_at=self.retrieved_at,
        )


def _html(script: bytes = b"", *, filing_text: str = _ACCESSION) -> bytes:
    return f"<html><body>{filing_text}".encode() + script + b"</body></html>\n"


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id=_ASSET_ID,
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id=_SUBMISSIONS_SOURCE_ID,
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _submissions(accepted_at: str, received_at: datetime) -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=SourceReference(
            source_id=_SUBMISSIONS_SOURCE_ID,
            retrieved_at=received_at,
        ),
        event_time=received_at,
        available_at=received_at,
        received_at=received_at,
        payload={
            "document_type": "submissions",
            "cik": "0000320193",
            "entity_name": "Apple Inc.",
            "document": {
                "cik": "0000320193",
                "name": "Apple Inc.",
                "tickers": ["AAPL"],
                "filings": {
                    "recent": {
                        "accessionNumber": [_ACCESSION],
                        "filingDate": ["2025-01-31"],
                        "reportDate": ["2024-12-31"],
                        "acceptanceDateTime": [accepted_at],
                        "form": ["10-K"],
                        "primaryDocument": ["annual.htm"],
                    }
                },
            },
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


def test_metadata_correction_survives_cycle_and_application_pit_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace_service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_service.initialize(workspace, format_version=1)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    configuration = _configuration()
    first_archive_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
    correction_observed_at = datetime(2025, 2, 3, 12, tzinfo=UTC)
    correction_archive_at = datetime(2025, 2, 4, 12, tzinfo=UTC)

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        storage.raw_records.save(
            _submissions("2025-01-31T18:00:00.000Z", datetime(2025, 1, 31, tzinfo=UTC))
        )
        first = SecDocumentPipeline(
            storage,
            _FixtureDocumentClient(first_archive_at, _html()),
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        prior = first.revisions[0]
        assert isinstance(prior, SecDocumentRevision)

        storage.raw_records.save(
            _submissions(
                "2025-01-31T23:30:00.000Z",
                correction_observed_at,
            )
        )
        correction = SecDocumentPipeline(
            storage,
            _FixtureDocumentClient(correction_archive_at, _html()),
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        assert correction.revisions_created == 1
        assert isinstance(correction.revisions[0], SecDocumentMetadataRevision)
        corrected = correction.revisions[0]

    application = SecDocumentCorpusApplication(runtime)
    before_correction = application.replay(
        query=SecDocumentQuery(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 3, 23, 59, tzinfo=UTC),
            accession=_ACCESSION,
        ),
        location=StorageLocationRequest(workspace=workspace),
    )
    after_correction = application.replay(
        query=SecDocumentQuery(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 5, tzinfo=UTC),
            accession=_ACCESSION,
        ),
        location=StorageLocationRequest(workspace=workspace),
    )

    assert before_correction.revision == prior
    assert after_correction.revision == corrected
    assert corrected.prior_revision_id == prior.revision_id
    assert corrected.content_sha256 == prior.content_sha256
    assert corrected.metadata_observed_at == correction_observed_at
    assert corrected.available_at == correction_archive_at


def test_acquisition_revision_chain_is_pit_append_only_and_visible_in_timeline(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace_service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_service.initialize(workspace, format_version=1)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    configuration = _configuration()
    first_accepted_at = "2025-01-31T18:00:00.000Z"
    first_observed_at = datetime(2025, 1, 31, tzinfo=UTC)
    first_retrieved_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
    first_metadata_observed_at = datetime(2025, 2, 3, 12, tzinfo=UTC)
    first_metadata_retrieved_at = datetime(2025, 2, 4, 12, tzinfo=UTC)
    acquisition_observed_at = datetime(2025, 2, 5, 12, tzinfo=UTC)
    acquisition_retrieved_at = datetime(2025, 2, 6, 12, tzinfo=UTC)
    metadata_observed_at = datetime(2025, 2, 7, 12, tzinfo=UTC)
    metadata_retrieved_at = datetime(2025, 2, 8, 12, tzinfo=UTC)
    original_content = _html()
    terminal_script = b'<script type="text/javascript"  src="/rotated/path"></script>'
    acquired_content = _html(terminal_script)

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        storage.raw_records.save(_submissions(first_accepted_at, first_observed_at))
        first_client = _FixtureDocumentClient(first_retrieved_at, original_content)
        first = SecDocumentPipeline(
            storage,
            first_client,
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        prior = first.revisions[0]
        assert isinstance(prior, SecDocumentRevision)

        storage.raw_records.save(
            _submissions(
                "2025-01-31T23:30:00.000Z",
                first_metadata_observed_at,
            )
        )
        first_metadata = SecDocumentPipeline(
            storage,
            _FixtureDocumentClient(first_metadata_retrieved_at, original_content),
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        metadata_before_acquisition = first_metadata.revisions[0]
        assert isinstance(metadata_before_acquisition, SecDocumentMetadataRevision)
        assert metadata_before_acquisition.prior_revision_id == prior.revision_id

        storage.raw_records.save(_submissions("2025-02-01T00:30:00.000Z", acquisition_observed_at))
        acquisition_client = _FixtureDocumentClient(acquisition_retrieved_at, acquired_content)
        acquisition = SecDocumentPipeline(
            storage,
            acquisition_client,
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        acquired = acquisition.revisions[0]
        assert isinstance(acquired, SecDocumentAcquisitionRevision)
        assert acquired.prior_revision_id == metadata_before_acquisition.revision_id
        assert acquired.prior_content_sha256 == prior.content_sha256
        assert acquired.content_sha256 == hashlib.sha256(acquired_content).hexdigest()
        assert storage.documents.read(prior.content_sha256) == original_content
        assert storage.documents.read(acquired.content_sha256) == acquired_content
        assert acquisition_client.calls == 1

        storage.raw_records.save(_submissions("2025-02-01T01:30:00.000Z", metadata_observed_at))
        metadata_client = _FixtureDocumentClient(metadata_retrieved_at, acquired_content)
        metadata = SecDocumentPipeline(
            storage,
            metadata_client,
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        corrected = metadata.revisions[0]
        assert isinstance(corrected, SecDocumentMetadataRevision)
        assert corrected.prior_revision_id == acquired.revision_id
        assert corrected.content_sha256 == acquired.content_sha256

        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        history = repository.list_revisions(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 9, tzinfo=UTC),
            accession=_ACCESSION,
        )
        repository.verify_revision_history(history)
        before_first_metadata = repository.replay(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 3, 23, 59, tzinfo=UTC),
            accession=_ACCESSION,
            include_content=True,
        )
        before_acquisition = repository.replay(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 5, tzinfo=UTC),
            accession=_ACCESSION,
            include_content=True,
        )
        after_acquisition = repository.replay(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 7, tzinfo=UTC),
            accession=_ACCESSION,
            include_content=True,
        )
        after_metadata = repository.replay(
            asset_id=_ASSET_ID,
            known_at=datetime(2025, 2, 9, tzinfo=UTC),
            accession=_ACCESSION,
            include_content=True,
        )
    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        timeline = SecDocumentTimelineService(storage).query(
            SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 9, tzinfo=UTC),
                asset_ids=(_ASSET_ID,),
                accession=_ACCESSION,
            )
        )

    assert before_first_metadata.revision == prior
    assert before_first_metadata.content == original_content
    assert before_acquisition.revision == metadata_before_acquisition
    assert before_acquisition.content == original_content
    assert after_acquisition.revision == acquired
    assert after_acquisition.content == acquired_content
    assert after_metadata.revision == corrected
    assert {entry.revision_id for entry in timeline.entries} == {
        prior.revision_id,
        metadata_before_acquisition.revision_id,
        acquired.revision_id,
        corrected.revision_id,
    }
    assert len(history) == 4

    repeat_client = _FixtureDocumentClient(
        datetime(2025, 2, 10, tzinfo=UTC),
        acquired_content,
    )
    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        storage.raw_records.save(
            _submissions("2025-02-01T01:30:00.000Z", datetime(2025, 2, 10, tzinfo=UTC))
        )
        raw_count_before = storage.raw_records.count()
        repeated = SecDocumentPipeline(
            storage,
            repeat_client,
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        assert repeated.revisions_reused == 1
        assert repeated.revisions_created == 0
        assert repeated.document_fetch_calls == 0
        assert storage.raw_records.count() == raw_count_before
    assert repeat_client.calls == 0


def test_acquisition_pipeline_rejects_internal_html_change_without_append(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace_service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_service.initialize(workspace, format_version=1)
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    configuration = _configuration()
    original_content = _html()
    changed_content = _html(
        b'<script type="text/javascript"  src="/safe/path"></script>',
        filing_text=f"{_ACCESSION} with an added figure",
    )

    with runtime.open_storage(
        StorageLocationRequest(workspace=workspace),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        storage.raw_records.save(
            _submissions("2025-01-31T18:00:00.000Z", datetime(2025, 1, 31, tzinfo=UTC))
        )
        first = SecDocumentPipeline(
            storage,
            _FixtureDocumentClient(datetime(2025, 2, 1, tzinfo=UTC), original_content),
            configuration=configuration,
        ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        storage.raw_records.save(
            _submissions("2025-01-31T23:30:00.000Z", datetime(2025, 2, 3, tzinfo=UTC))
        )
        before = storage.raw_records.count()
        with pytest.raises(SecDocumentPipelineError, match="differs outside"):
            SecDocumentPipeline(
                storage,
                _FixtureDocumentClient(datetime(2025, 2, 4, tzinfo=UTC), changed_content),
                configuration=configuration,
            ).run(SecDocumentImportRequest(accessions=(_ACCESSION,)))
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        prior = first.revisions[0]
        assert isinstance(prior, SecDocumentRevision)
        assert storage.raw_records.count() == before
        assert repository.get_revision(prior.revision_id) == prior
