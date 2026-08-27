import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    OWNERSHIP_SCHEMA_VERSION,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
    SecResolvedOwnershipDocument,
)
from investment_analyst.providers.ownership.sec_ownership_pipeline import (
    SecOwnershipImportRequest,
    SecOwnershipPipeline,
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
    retrieved = datetime(2025, 2, 1, tzinfo=UTC)
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=retrieved),
        event_time=retrieved,
        available_at=retrieved,
        received_at=retrieved,
        payload={
            "document": {
                "cik": "0000320193",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000001"],
                        "filingDate": ["2025-01-31"],
                        "reportDate": ["2025-01-30"],
                        "acceptanceDateTime": ["2025-01-31T18:00:00.000Z"],
                        "form": ["4"],
                        "primaryDocument": ["xslF345X06/form4.xml"],
                    }
                },
            }
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


class _Client:
    def resolve_ownership_document(self, document):
        timestamp = datetime(2025, 2, 2, tzinfo=UTC)
        locator = b"<!DOCTYPE html><html/>"
        semantic = b"""<ownershipDocument>
<documentType>4</documentType><periodOfReport>2025-01-30</periodOfReport>
<issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Owner</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner><isOther>0</isOther></reportingOwnerRelationship></reportingOwner>
</ownershipDocument>"""
        url = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/"
        return SecResolvedOwnershipDocument(
            manifest=SecAccessionManifest(
                entries=("form4.xml", "xslF345X06/form4.xml"),
                sha256="a" * 64,
                size_bytes=10,
                url=f"{url}index.json",
                retrieved_at=timestamp,
            ),
            locator=SecPrimaryDocumentResponse(
                locator,
                hashlib.sha256(locator).hexdigest(),
                len(locator),
                f"{url}{document.name}",
                timestamp,
            ),
            semantic=SecPrimaryDocumentResponse(
                semantic,
                hashlib.sha256(semantic).hexdigest(),
                len(semantic),
                f"{url}form4.xml",
                timestamp,
            ),
        )


def test_pipeline_preserves_rejected_locator_then_parses_manifest_xml(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        pipeline = SecOwnershipPipeline(storage, _Client(), configuration=_configuration())

        first = pipeline.run(SecOwnershipImportRequest(forms=("4",)))
        second = pipeline.run(SecOwnershipImportRequest(forms=("4",)))

        assert len(first) == len(second) == 1
        assert storage.raw_records.count(schema_version=OWNERSHIP_OUTCOME_SCHEMA_VERSION) == 2
        assert storage.raw_records.count(schema_version="sec-document-revision-v1") == 1
        assert storage.raw_records.count(schema_version=OWNERSHIP_SCHEMA_VERSION) == 1
