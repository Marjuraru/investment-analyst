import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    BENEFICIAL_OWNERSHIP_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_pipeline import (
    SecBeneficialOwnershipImportRequest,
    SecBeneficialOwnershipPipeline,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
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


def _submissions() -> RawRecord:
    captured = datetime(2025, 2, 1, tzinfo=UTC)
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=captured),
        event_time=captured,
        available_at=captured,
        received_at=captured,
        payload={
            "document": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000001"],
                        "filingDate": ["2025-01-31"],
                        "reportDate": ["2025-01-30"],
                        "acceptanceDateTime": ["2025-01-31T18:00:00.000Z"],
                        "form": ["SC 13G"],
                        "primaryDocument": ["filing.htm"],
                    }
                }
            }
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


class _Client:
    def __init__(self, *, top_level_xml: bool = True) -> None:
        self._top_level_xml = top_level_xml
        self._retrieved_at = datetime(2025, 2, 2, tzinfo=UTC)

    def fetch_manifest(self, document):
        del document
        entries = ("ownership.xml", "filing.htm") if self._top_level_xml else ("filing.htm",)
        return SecAccessionManifest(
            entries=entries,
            sha256="a" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self._retrieved_at,
        )

    def fetch(self, document):
        content = (
            b"""<edgarSubmission><submissionType>SC 13G</submissionType>
<subjectCompany><cik>0000320193</cik><name>Apple Inc.</name></subjectCompany>
<reportingOwner><cik>0001234567</cik><name>Owner LLC</name></reportingOwner>
<aggregateAmountBeneficiallyOwned>1000</aggregateAmountBeneficiallyOwned>
<percentOfClass>5.2</percentOfClass></edgarSubmission>"""
            if document.name == "ownership.xml"
            else b"<html>declared locator</html>"
        )
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self._retrieved_at,
        )


def test_pipeline_persists_rejected_locator_and_accepted_xml_idempotently(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        pipeline = SecBeneficialOwnershipPipeline(
            storage, _Client(), configuration=_configuration()
        )
        request = SecBeneficialOwnershipImportRequest(forms=("SC 13G",))

        first = pipeline.run(request)
        second = pipeline.run(request)

        assert [item.statement_id for item in first] == [item.statement_id for item in second]
        assert (
            storage.raw_records.count(schema_version=BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION)
            == 2
        )
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.raw_records.count(schema_version=BENEFICIAL_OWNERSHIP_SCHEMA_VERSION) == 1
        statement = BeneficialOwnershipRepository(storage.raw_records).list(
            asset_id="equity:us:aapl", known_at=datetime(2025, 2, 2, tzinfo=UTC)
        )[0]
        assert statement.available_at == datetime(2025, 1, 31, 18, tzinfo=UTC)


def test_pipeline_records_rejected_outcome_when_manifest_has_no_xml(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        result = SecBeneficialOwnershipPipeline(
            storage, _Client(top_level_xml=False), configuration=_configuration()
        ).run(SecBeneficialOwnershipImportRequest(forms=("SC 13G",)))

        assert result == ()
        assert (
            storage.raw_records.count(schema_version=BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION)
            == 1
        )
        assert storage.raw_records.count(schema_version=BENEFICIAL_OWNERSHIP_SCHEMA_VERSION) == 0
