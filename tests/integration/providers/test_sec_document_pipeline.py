import hashlib
from datetime import UTC, datetime
from email.message import Message
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.request import Request
from uuid import uuid4

import pytest

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.repository import SecDocumentRepository
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecDocumentClient,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentPipeline,
    SecDocumentPipelineError,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_TICKER,
    SecEdgarIdentity,
)
from investment_analyst.providers.http import (
    HttpRequestError,
    HttpRequestFailureKind,
    UrlLibHttpTransport,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.document_content import DocumentContentError


class _ArchivesResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/html"
        self._url = url
        self._body = body
        self._position = 0

    def __enter__(self) -> "_ArchivesResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        if size is None or size < 0:
            result = self._body[self._position :]
            self._position = len(self._body)
            return result
        result = self._body[self._position : self._position + size]
        self._position += len(result)
        return result

    def geturl(self) -> str:
        return self._url


class _Client:
    def __init__(
        self,
        *,
        fail_second: bool = False,
        body_suffix: str = "",
        retrieved_at: datetime | None = None,
    ) -> None:
        self._fail_second = fail_second
        self._body_suffix = body_suffix
        self._retrieved_at = retrieved_at
        self._calls = 0

    def fetch(self, document):
        self._calls += 1
        if self._fail_second and self._calls == 2:
            raise RuntimeError("second fetch failed")
        body = f"<html>{document.filing.accession}{self._body_suffix}</html>".encode()
        return SecPrimaryDocumentResponse(
            content=body,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            url=(
                "https://www.sec.gov/Archives/edgar/data/320193/"
                f"{document.filing.accession.replace('-', '')}/{document.name}"
            ),
            retrieved_at=self._retrieved_at or datetime(2025, 2, self._calls, tzinfo=UTC),
        )


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _submissions(
    *,
    accepted_at: str = "2025-01-31T18:00:00.000Z",
    retrieved_at: datetime = datetime(2025, 1, 31, tzinfo=UTC),
    primary_document: str = "annual.htm",
) -> RawRecord:
    retrieved = retrieved_at
    recent = {
        "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002"],
        "filingDate": ["2025-01-31", "2025-01-31"],
        "reportDate": ["2024-12-31", "2024-09-30"],
        "acceptanceDateTime": [accepted_at, "2025-01-31T17:00:00.000Z"],
        "form": ["10-K", "10-Q"],
        "primaryDocument": [primary_document, "quarterly.htm"],
    }
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=retrieved),
        event_time=retrieved,
        available_at=retrieved,
        received_at=retrieved,
        payload={
            "document_type": "submissions",
            "cik": APPLE_CIK,
            "entity_name": "Apple Inc.",
            "document": {
                "cik": APPLE_CIK,
                "name": "Apple Inc.",
                "tickers": ["AAPL"],
                "filings": {"recent": recent},
            },
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


def test_second_provider_failure_keeps_first_document_persisted(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        pipeline = SecDocumentPipeline(
            storage, _Client(fail_second=True), configuration=_configuration()
        )

        with pytest.raises(SecDocumentPipelineError) as raised:
            pipeline.run(SecDocumentImportRequest(forms=("10-K", "10-Q")))

        assert raised.value.reason_code == "sec_document_fetch_failed"
        assert isinstance(raised.value.__cause__, RuntimeError)

        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.observations.count() == 0
        assert storage.metric_results.count() == 0
        assert storage.diagnostics.count() == 0


def test_repeat_reuses_blob_and_revision(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        first = SecDocumentPipeline(storage, _Client(), configuration=_configuration()).run(
            SecDocumentImportRequest(forms=("10-K",))
        )
        second = SecDocumentPipeline(storage, _Client(), configuration=_configuration()).run(
            SecDocumentImportRequest(forms=("10-K",))
        )

        assert first.revisions_created == 1
        assert second.revisions_reused == 1
        assert first.revisions[0] == second.revisions[0]


def test_accepted_at_correction_appends_v3_and_reuse_skips_archive(tmp_path: Path) -> None:
    accession = "0000320193-25-000001"
    request = SecDocumentImportRequest(accessions=(accession,))
    first_submission = _submissions()
    metadata_observed = datetime(2025, 2, 4, tzinfo=UTC)
    corrected_submission = _submissions(
        accepted_at="2025-02-01T01:00:00.000Z",
        retrieved_at=metadata_observed,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(first_submission)
        first_client = _Client(retrieved_at=datetime(2025, 2, 1, tzinfo=UTC))
        first = SecDocumentPipeline(storage, first_client, configuration=_configuration()).run(
            request
        )
        prior = first.revisions[0]
        storage.raw_records.save(corrected_submission)

        correction_client = _Client(retrieved_at=datetime(2025, 2, 5, tzinfo=UTC))
        corrected = SecDocumentPipeline(
            storage, correction_client, configuration=_configuration()
        ).run(request)
        repeated = SecDocumentPipeline(storage, _Client(), configuration=_configuration()).run(
            request
        )
        replay_before = SecDocumentRepository(storage.raw_records, storage.documents).replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 4, 12, tzinfo=UTC),
            accession=accession,
        )
        replay_after = SecDocumentRepository(storage.raw_records, storage.documents).replay(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 6, tzinfo=UTC),
            accession=accession,
        )

        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.raw_records.count(schema_version="sec-document-revision-v3") == 1

    assert corrected.revisions_created == 1
    assert corrected.revisions_reused == 0
    assert corrected.blobs_created == 0
    assert corrected.blobs_reused == 1
    assert corrected.document_fetch_calls == 1
    assert correction_client._calls == 1
    assert corrected.revisions[0].prior_revision_id == prior.revision_id
    assert corrected.revisions[0].metadata_observed_at == metadata_observed
    assert corrected.revisions[0].available_at == datetime(2025, 2, 5, tzinfo=UTC)
    assert repeated.revisions_reused == 1
    assert repeated.document_fetch_calls == 0
    assert replay_before.revision == prior
    assert replay_after.revision == corrected.revisions[0]
    assert replay_after.revision.document.filing.accepted_at == datetime(2025, 2, 1, 1, tzinfo=UTC)


def test_accepted_at_correction_rejects_changed_remote_bytes_without_append(
    tmp_path: Path,
) -> None:
    accession = "0000320193-25-000001"
    request = SecDocumentImportRequest(accessions=(accession,))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        first = SecDocumentPipeline(
            storage,
            _Client(retrieved_at=datetime(2025, 2, 1, tzinfo=UTC)),
            configuration=_configuration(),
        ).run(request)
        storage.raw_records.save(
            _submissions(
                accepted_at="2025-02-01T01:00:00.000Z",
                retrieved_at=datetime(2025, 2, 4, tzinfo=UTC),
            )
        )

        with pytest.raises(SecDocumentPipelineError) as raised:
            SecDocumentPipeline(
                storage,
                _Client(
                    body_suffix=" changed",
                    retrieved_at=datetime(2025, 2, 5, tzinfo=UTC),
                ),
                configuration=_configuration(),
            ).run(request)

        assert raised.value.reason_code == "sec_document_revision_conflict"
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.raw_records.count(schema_version="sec-document-revision-v3") == 0
        assert storage.raw_records.get(first.revisions[0].raw_record_id).payload["revision"][
            "revision_id"
        ] == str(first.revisions[0].revision_id)


def test_metadata_change_beyond_accepted_at_fails_before_archive_fetch(tmp_path: Path) -> None:
    accession = "0000320193-25-000001"
    request = SecDocumentImportRequest(accessions=(accession,))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        first = SecDocumentPipeline(storage, _Client(), configuration=_configuration()).run(request)
        storage.raw_records.save(
            _submissions(
                accepted_at="2025-02-01T01:00:00.000Z",
                retrieved_at=datetime(2025, 2, 4, tzinfo=UTC),
                primary_document="renamed.htm",
            )
        )
        client = _Client()

        with pytest.raises(SecDocumentPipelineError) as raised:
            SecDocumentPipeline(storage, client, configuration=_configuration()).run(request)

        assert raised.value.reason_code == "sec_document_revision_conflict"
        assert client._calls == 0
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.raw_records.count(schema_version="sec-document-revision-v3") == 0
        assert first.revisions_created == 1


def test_transport_exhaustion_preserves_progress_and_retry_persists_one_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarterly_accession = "0000320193-25-000002"
    annual_accession = "0000320193-25-000001"
    quarterly_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000002/quarterly.htm"
    )
    annual_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm"
    quarterly_body = b"exact quarterly document"
    annual_body = b"exact annual document after transport retries"
    annual_attempts = 0
    recover_annual = False

    def fake_urlopen(request: Request, timeout: float) -> _ArchivesResponse:
        nonlocal annual_attempts
        if request.full_url == annual_url:
            annual_attempts += 1
            if not recover_annual or annual_attempts <= 2:
                raise RemoteDisconnected("upstream connection detail")
            return _ArchivesResponse(annual_url, annual_body)
        if request.full_url == quarterly_url:
            return _ArchivesResponse(quarterly_url, quarterly_body)
        raise AssertionError(f"unexpected offline SEC URL: {request.full_url}")

    monkeypatch.setattr("investment_analyst.providers.http.urlopen", fake_urlopen)
    client = SecDocumentClient(
        UrlLibHttpTransport(sleep=lambda _: None),
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        clock=lambda: datetime(2025, 2, 1, tzinfo=UTC),
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        pipeline = SecDocumentPipeline(storage, client, configuration=_configuration())
        request = SecDocumentImportRequest(forms=("10-K", "10-Q"))
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        known_at = datetime.max.replace(tzinfo=UTC)

        with pytest.raises(SecDocumentPipelineError) as raised:
            pipeline.run(request)

        assert raised.value.reason_code == "sec_document_fetch_failed"
        assert isinstance(raised.value.__cause__, HttpRequestError)
        assert raised.value.__cause__.failure_kind is HttpRequestFailureKind.TRANSPORT
        assert annual_attempts == 3
        assert repository.list_revisions(
            asset_id="equity:us:aapl",
            known_at=known_at,
            accession=quarterly_accession,
        )
        assert (
            repository.list_revisions(
                asset_id="equity:us:aapl",
                known_at=known_at,
                accession=annual_accession,
            )
            == []
        )
        revisions_after_failure = repository.list_revisions(
            asset_id="equity:us:aapl",
            known_at=known_at,
        )
        assert len(revisions_after_failure) == 1
        assert revisions_after_failure[0].document.filing.accession == quarterly_accession
        assert storage.documents.read(hashlib.sha256(quarterly_body).hexdigest()) == quarterly_body
        with pytest.raises(DocumentContentError, match="missing or not a regular file"):
            storage.documents.verify(hashlib.sha256(annual_body).hexdigest())

        annual_attempts = 0
        recover_annual = True
        recovered = pipeline.run(request)
        annual_revisions = repository.list_revisions(
            asset_id="equity:us:aapl",
            known_at=known_at,
            accession=annual_accession,
        )
        all_revisions = repository.list_revisions(
            asset_id="equity:us:aapl",
            known_at=known_at,
        )

        assert annual_attempts == 3
        assert recovered.revisions_created == 1
        assert recovered.revisions_reused == 1
        assert recovered.document_fetch_calls == 1
        assert len(annual_revisions) == 1
        assert annual_revisions[0].content_sha256 == hashlib.sha256(annual_body).hexdigest()
        assert len(all_revisions) == 2
        assert storage.documents.read(annual_revisions[0].content_sha256) == annual_body
