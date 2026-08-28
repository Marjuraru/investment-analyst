import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
    OWNERSHIP_SCHEMA_VERSION,
    OWNERSHIP_SCHEMA_VERSION_V2,
)
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
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
    def __init__(self, retrieved_at: datetime = datetime(2025, 2, 2, tzinfo=UTC)) -> None:
        self._retrieved_at = retrieved_at

    def resolve_ownership_document(self, document):
        timestamp = self._retrieved_at
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
        assert storage.raw_records.count(schema_version=OWNERSHIP_OUTCOME_SCHEMA_VERSION) == 0
        assert storage.raw_records.count(schema_version=OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2) == 2
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 1
        assert storage.raw_records.count(schema_version=OWNERSHIP_SCHEMA_VERSION) == 0
        assert storage.raw_records.count(schema_version=OWNERSHIP_SCHEMA_VERSION_V2) == 1


def test_pipeline_transports_availability_from_acceptance_not_parsing(tmp_path: Path) -> None:
    """The parser must transport available_at from the revision, never recompute it."""
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.raw_records.save(_submissions())
        pipeline = SecOwnershipPipeline(storage, _Client(), configuration=_configuration())

        (statement,) = pipeline.run(SecOwnershipImportRequest(forms=("4",)))

        accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
        assert statement.available_at == accepted_at
        assert statement.available_at == statement.document_revision.available_at
        assert statement.parsed_at != statement.available_at


def test_replay_across_two_import_environments_matches_by_known_at(tmp_path: Path) -> None:
    """Same corpus, two different local retrieval instants, identical known_at result."""
    known_at = datetime(2027, 1, 1, tzinfo=UTC)

    with LocalStorage(StoragePaths.from_root(tmp_path / "env-a")) as storage_a:
        storage_a.raw_records.save(_submissions())
        SecOwnershipPipeline(
            storage_a, _Client(datetime(2025, 2, 2, tzinfo=UTC)), configuration=_configuration()
        ).run(SecOwnershipImportRequest(forms=("4",)))
        statements_a = OwnershipRepository(storage_a.raw_records).list(
            asset_id="equity:us:aapl", known_at=known_at
        )

    with LocalStorage(StoragePaths.from_root(tmp_path / "env-b")) as storage_b:
        storage_b.raw_records.save(_submissions())
        SecOwnershipPipeline(
            storage_b, _Client(datetime(2026, 3, 10, tzinfo=UTC)), configuration=_configuration()
        ).run(SecOwnershipImportRequest(forms=("4",)))
        statements_b = OwnershipRepository(storage_b.raw_records).list(
            asset_id="equity:us:aapl", known_at=known_at
        )

    assert [item.statement_id for item in statements_a] == [
        item.statement_id for item in statements_b
    ]
    assert [item.available_at for item in statements_a] == [
        item.available_at for item in statements_b
    ]
    assert statements_a[0].parsed_at != statements_b[0].parsed_at
