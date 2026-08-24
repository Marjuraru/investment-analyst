from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecDocumentQuery,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_documents.service import SecDocumentCorpusService
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.storage import LocalStorage, StoragePaths


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def test_read_only_replay_returns_missing_then_local_content(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    retrieved = datetime(2025, 2, 1, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(paths) as writer:
        writer.raw_records.save(
            RawRecord(
                record_id=discovery_id,
                asset_id="equity:us:aapl",
                source=SourceReference(
                    source_id="sec-edgar:aapl:submissions",
                    retrieved_at=retrieved - timedelta(seconds=1),
                ),
                available_at=retrieved - timedelta(seconds=1),
                received_at=retrieved - timedelta(seconds=1),
                payload={"document": {"cik": "0000320193"}},
                schema_version="sec-edgar-submissions-snapshot-v1",
            )
        )
        filing = SecFiling(
            filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
            filer_cik="0000320193",
            accession="0000320193-25-000001",
            form="10-K",
            filing_date=date(2025, 1, 31),
            report_date=date(2024, 12, 31),
            accepted_at=retrieved,
            is_amendment=False,
        )
        document = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
            filing=filing,
            name="annual.htm",
        )
        receipt = writer.documents.put(b"body")
        revision_id = SecDocumentRevision.expected_id(
            document.document_id, receipt.sha256, "sec-document-revision-v1"
        )
        revision = SecDocumentRevision(
            revision_id=revision_id,
            asset_id="equity:us:aapl",
            document=document,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
            discovery_raw_record_id=discovery_id,
            content_sha256=receipt.sha256,
            content_size_bytes=receipt.size_bytes,
            available_at=retrieved,
            retrieved_at=retrieved,
            source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm",
        )
        writer.raw_records.save(revision_to_raw_record(revision))

    with LocalStorage(paths, read_only=True) as reader:
        service = SecDocumentCorpusService(reader, configuration=_configuration())
        missing = service.replay(
            SecDocumentQuery(asset_id="equity:us:aapl", known_at=retrieved - timedelta(seconds=1))
        )
        found = service.replay(
            SecDocumentQuery(asset_id="equity:us:aapl", known_at=retrieved, include_content=True)
        )

    assert missing.state == "missing"
    assert found.state == "found"
    assert found.content == b"body"
