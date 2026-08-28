from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_parser import (
    SecBeneficialOwnershipParserError,
    classify_beneficial_ownership_resource,
    parse_beneficial_ownership_statement,
)


def _revision() -> SecDocumentRevision:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="SC 13G",
        filing_date=date(2025, 1, 31),
        report_date=date(2025, 1, 30),
        accepted_at=accepted_at,
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
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256="a" * 64,
        content_size_bytes=10,
        available_at=accepted_at,
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/ownership.xml",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )


def _xml() -> bytes:
    return (
        b"<edgarSubmission><submissionType>SC 13G</submissionType>"
        b"<subjectCompany><cik>0000320193</cik><name>Apple Inc.</name></subjectCompany>"
        b"<reportingOwner><cik>0001234567</cik><name>Owner LLC</name></reportingOwner>"
        b"<eventDate>2025-01-30</eventDate>"
        b"<aggregateAmountBeneficiallyOwned>1000</aggregateAmountBeneficiallyOwned>"
        b"<percentOfClass>5.2</percentOfClass></edgarSubmission>"
    )


def test_parser_extracts_declared_values_without_instrument_normalization() -> None:
    statement = parse_beneficial_ownership_statement(
        _xml(),
        asset_id="equity:us:aapl",
        revision=_revision(),
        parsed_at=datetime(2025, 2, 1, tzinfo=UTC),
    )

    assert statement.subject_cik == "0000320193"
    assert statement.reporting_person_cik == "0001234567"
    assert statement.shares_beneficially_owned == Decimal("1000")
    assert statement.percent_of_class == Decimal("5.2")


@pytest.mark.parametrize("content", (b"<!DOCTYPE x><edgarSubmission/>", b"<other/>", b"text"))
def test_parser_rejects_forbidden_or_unexpected_representations(content: bytes) -> None:
    assert classify_beneficial_ownership_resource(content).status == "rejected"
    with pytest.raises(SecBeneficialOwnershipParserError):
        parse_beneficial_ownership_statement(
            content,
            asset_id="equity:us:aapl",
            revision=_revision(),
            parsed_at=datetime(2025, 2, 1, tzinfo=UTC),
        )
