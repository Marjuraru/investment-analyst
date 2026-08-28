from datetime import UTC, date, datetime

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_ownership.models import (
    OwnershipQueryResult,
    OwnershipResolutionOutcome,
    OwnershipStatement,
)

_ACCEPTED_AT = datetime(2025, 1, 31, 18, 0, tzinfo=UTC)
_RETRIEVED_AT = datetime(2025, 2, 2, tzinfo=UTC)


def _filing() -> SecFiling:
    return SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="4",
        filing_date=date(2025, 1, 31),
        report_date=date(2025, 1, 30),
        accepted_at=_ACCEPTED_AT,
        is_amendment=False,
    )


def _revision(*, schema_version: str, available_at: datetime) -> SecDocumentRevision:
    filing = _filing()
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "form4.xml"),
        filing=filing,
        name="form4.xml",
    )
    revision_id = SecDocumentRevision.expected_id(document.document_id, "b" * 64, schema_version)
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=filing.filing_id,
        content_sha256="b" * 64,
        content_size_bytes=1,
        available_at=available_at,
        retrieved_at=_RETRIEVED_AT,
        source_url="https://www.sec.gov/Archives/form4.xml",
        revision_schema_version=schema_version,
    )


def _statement(*, schema_version: str, revision: SecDocumentRevision, parsed_at: datetime):
    statement_id = OwnershipStatement.expected_id(revision.revision_id, schema_version)
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form="4",
        period_of_report=date(2025, 1, 30),
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        reporting_owners=(),
        entries=(),
        available_at=revision.available_at,
        parsed_at=parsed_at,
        schema_version=schema_version,
    )


def _outcome(*, resolver_version: str, available_at: datetime) -> OwnershipResolutionOutcome:
    filing = _filing()
    outcome_id = OwnershipResolutionOutcome.expected_id(
        filing.accession, "form4.xml", "c" * 64, "accepted", resolver_version
    )
    return OwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=OwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=filing.filing_id,
        declared_locator="form4.xml",
        resource_name="form4.xml",
        resource_url="https://www.sec.gov/Archives/form4.xml",
        content_sha256="c" * 64,
        content_size_bytes=1,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256="d" * 64,
        available_at=available_at,
        retrieved_at=_RETRIEVED_AT,
        status="accepted",
        reason_code="ok",
        resolver_version=resolver_version,
    )


def test_v2_statement_transports_acceptance_and_is_disjoint_from_v1() -> None:
    v2_revision = _revision(schema_version="sec-document-revision-v2", available_at=_ACCEPTED_AT)
    v2_statement = _statement(
        schema_version="sec-ownership-statement-v2", revision=v2_revision, parsed_at=_RETRIEVED_AT
    )

    assert v2_statement.available_at == _ACCEPTED_AT
    assert v2_statement.available_at != v2_statement.parsed_at

    v1_revision = _revision(schema_version="sec-document-revision-v1", available_at=_RETRIEVED_AT)
    v1_statement = _statement(
        schema_version="sec-ownership-statement-v1", revision=v1_revision, parsed_at=_RETRIEVED_AT
    )
    assert v1_statement.statement_id != v2_statement.statement_id


def test_v2_statement_availability_cannot_be_parsed_at() -> None:
    v2_revision = _revision(schema_version="sec-document-revision-v2", available_at=_ACCEPTED_AT)
    valid = _statement(
        schema_version="sec-ownership-statement-v2",
        revision=v2_revision,
        parsed_at=_RETRIEVED_AT,
    )
    with pytest.raises(ValueError, match="statement availability must inherit document"):
        OwnershipStatement(**{**valid.model_dump(), "available_at": _RETRIEVED_AT})


def test_v1_outcome_still_requires_availability_equal_retrieval() -> None:
    with pytest.raises(ValueError, match="outcome availability must equal retrieval"):
        _outcome(resolver_version="sec-ownership-resolver-v1", available_at=_ACCEPTED_AT)


def test_v2_outcome_availability_equals_acceptance_and_coexists_with_v1_identity() -> None:
    v2_outcome = _outcome(resolver_version="sec-ownership-resolver-v2", available_at=_ACCEPTED_AT)
    assert v2_outcome.available_at == _ACCEPTED_AT
    assert v2_outcome.available_at != v2_outcome.retrieved_at

    v1_outcome = _outcome(resolver_version="sec-ownership-resolver-v1", available_at=_RETRIEVED_AT)
    assert v1_outcome.outcome_id != v2_outcome.outcome_id


def test_v2_outcome_availability_cannot_be_retrieved_at() -> None:
    with pytest.raises(
        ValueError, match="v2 outcome availability must equal SEC filing acceptance"
    ):
        _outcome(resolver_version="sec-ownership-resolver-v2", available_at=_RETRIEVED_AT)


def test_query_result_truncation_cannot_be_misreported() -> None:
    v2_revision = _revision(schema_version="sec-document-revision-v2", available_at=_ACCEPTED_AT)
    statement = _statement(
        schema_version="sec-ownership-statement-v2", revision=v2_revision, parsed_at=_RETRIEVED_AT
    )

    OwnershipQueryResult(
        statements=(statement,), total_matching=1, truncated=False, legacy_records_excluded=0
    )

    with pytest.raises(ValueError, match="truncated must match total_matching"):
        OwnershipQueryResult(
            statements=(statement,), total_matching=5, truncated=False, legacy_records_excluded=0
        )
    with pytest.raises(ValueError, match="total_matching cannot be lower than returned statements"):
        OwnershipQueryResult(
            statements=(statement,), total_matching=0, truncated=False, legacy_records_excluded=0
        )
