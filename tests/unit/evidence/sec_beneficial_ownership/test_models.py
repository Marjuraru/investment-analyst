from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipResolutionOutcome,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)


def _revision() -> SecDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="SC 13G",
        filing_date=date(2025, 1, 31),
        report_date=date(2025, 1, 30),
        accepted_at=datetime(2025, 1, 31, 18, tzinfo=UTC),
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "ownership.xml"),
        filing=filing,
        name="ownership.xml",
    )
    digest = "a" * 64
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, digest, REVISION_SCHEMA_VERSION_V2
    )
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        content_sha256=digest,
        content_size_bytes=10,
        available_at=filing.accepted_at,
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/ownership.xml",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )


def test_statement_is_deterministic_and_rejects_float() -> None:
    revision = _revision()
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    values = dict(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form="SC 13G",
        subject_cik="0000320193",
        subject_name="Apple Inc.",
        shares_beneficially_owned=Decimal("123"),
        percent_of_class=Decimal("5.1"),
        available_at=revision.available_at,
        parsed_at=datetime(2025, 2, 1, tzinfo=UTC),
    )

    statement = BeneficialOwnershipStatement(**values)

    assert statement.statement_id == statement_id
    with pytest.raises(ValueError):
        BeneficialOwnershipStatement(**(values | {"percent_of_class": 5.1}))


def test_outcome_requires_filing_acceptance_as_available_at() -> None:
    revision = _revision()
    filing = revision.document.filing
    outcome_id = BeneficialOwnershipResolutionOutcome.expected_id(
        filing.accession, "ownership.xml", "b" * 64, "accepted"
    )
    with pytest.raises(ValueError, match="availability"):
        BeneficialOwnershipResolutionOutcome(
            outcome_id=outcome_id,
            raw_record_id=BeneficialOwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
            asset_id="equity:us:aapl",
            filing=filing,
            discovery_raw_record_id=revision.discovery_raw_record_id,
            declared_locator="ownership.xml",
            resource_name="ownership.xml",
            resource_url="https://www.sec.gov/Archives/ownership.xml",
            content_sha256="b" * 64,
            content_size_bytes=10,
            manifest_url="https://www.sec.gov/Archives/index.json",
            manifest_sha256="c" * 64,
            available_at=datetime(2025, 2, 1, tzinfo=UTC),
            retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
            status="accepted",
            reason_code="structured_sec_xml",
        )


def test_filing_requires_explicit_report_date_except_for_beneficial_ownership() -> None:
    common = dict(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000002"),
        filer_cik="0000320193",
        accession="0000320193-25-000002",
        filing_date=date(2025, 1, 31),
        accepted_at=datetime(2025, 1, 31, tzinfo=UTC),
        is_amendment=False,
    )

    beneficial = SecFiling(**(common | {"form": "SC 13G", "report_date": None}))

    assert beneficial.report_date is None
    with pytest.raises(ValueError, match="report_date is required"):
        SecFiling(**(common | {"form": "4", "report_date": None}))
    with pytest.raises(ValueError):
        SecFiling(**(common | {"form": "SC 13G"}))
