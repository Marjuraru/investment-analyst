from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import AssetClass
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipQuery,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_beneficial_ownership.service import BeneficialOwnershipService
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
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


def _statement() -> BeneficialOwnershipStatement:
    available_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="SC 13G",
        filing_date=date(2025, 1, 31),
        report_date=date(2025, 1, 30),
        accepted_at=available_at,
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "ownership.xml"),
        filing=filing,
        name="ownership.xml",
    )
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, "a" * 64, REVISION_SCHEMA_VERSION_V2
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256="a" * 64,
        content_size_bytes=12,
        available_at=available_at,
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/ownership.xml",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )
    statement_id = BeneficialOwnershipStatement.expected_id(revision_id)
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form="SC 13G",
        subject_cik="0000320193",
        subject_name="Apple Inc.",
        available_at=available_at,
        parsed_at=datetime(2025, 2, 1, tzinfo=UTC),
    )


def test_query_applies_known_at_and_reports_truncation(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        statement = _statement()
        BeneficialOwnershipRepository(storage.raw_records).save(statement)

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        service = BeneficialOwnershipService(storage, configuration=_configuration())
        before = service.query(
            BeneficialOwnershipQuery(
                asset_id="equity:us:aapl",
                known_at=datetime(2025, 1, 31, 17, tzinfo=UTC),
            )
        )
        after = service.query(
            BeneficialOwnershipQuery(
                asset_id="equity:us:aapl",
                known_at=datetime(2025, 2, 1, tzinfo=UTC),
            )
        )

    assert before.statements == ()
    assert after.total_matching == 1
    assert after.truncated is False
    assert after.statements == (statement,)
