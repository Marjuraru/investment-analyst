import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import SecPrimaryDocumentResponse
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentPipeline,
)
from investment_analyst.providers.fundamentals.sec_edgar import APPLE_CIK, APPLE_TICKER
from investment_analyst.storage import LocalStorage, StoragePaths


class _Client:
    def __init__(self, *, fail_second: bool = False) -> None:
        self._fail_second = fail_second
        self._calls = 0

    def fetch(self, document):
        self._calls += 1
        if self._fail_second and self._calls == 2:
            raise RuntimeError("second fetch failed")
        body = f"<html>{document.filing.accession}</html>".encode()
        return SecPrimaryDocumentResponse(
            content=body,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            url=(
                "https://www.sec.gov/Archives/edgar/data/320193/"
                f"{document.filing.accession.replace('-', '')}/{document.name}"
            ),
            retrieved_at=datetime(2025, 2, self._calls, tzinfo=UTC),
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


def _submissions() -> RawRecord:
    retrieved = datetime(2025, 1, 31, tzinfo=UTC)
    recent = {
        "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002"],
        "filingDate": ["2025-01-31", "2025-01-31"],
        "reportDate": ["2024-12-31", "2024-09-30"],
        "acceptanceDateTime": ["2025-01-31T18:00:00.000Z", "2025-01-31T17:00:00.000Z"],
        "form": ["10-K", "10-Q"],
        "primaryDocument": ["annual.htm", "quarterly.htm"],
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

        with pytest.raises(RuntimeError, match="second fetch"):
            pipeline.run(SecDocumentImportRequest(forms=("10-K", "10-Q")))

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
