from datetime import UTC, date, datetime
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipResolutionOutcome,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    outcome_from_raw_record,
    outcome_to_raw_record,
)
from investment_analyst.evidence.sec_documents.models import SecFiling


def test_outcome_raw_record_round_trips_exactly() -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
        filer_cik="0000320193",
        accession="0000320193-25-000001",
        form="SC 13D",
        filing_date=date(2025, 1, 31),
        report_date=date(2025, 1, 30),
        accepted_at=accepted_at,
        is_amendment=False,
    )
    outcome_id = BeneficialOwnershipResolutionOutcome.expected_id(
        filing.accession, "filing.htm", "a" * 64, "rejected"
    )
    outcome = BeneficialOwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=BeneficialOwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=uuid4(),
        declared_locator="filing.htm",
        resource_name="filing.htm",
        resource_url="https://www.sec.gov/Archives/filing.htm",
        content_sha256="a" * 64,
        content_size_bytes=12,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256="b" * 64,
        available_at=accepted_at,
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        status="rejected",
        reason_code="not_xml",
    )

    assert outcome_from_raw_record(outcome_to_raw_record(outcome)) == outcome


def test_outcome_decoder_rejects_other_source() -> None:
    accepted_at = datetime(2025, 1, 31, tzinfo=UTC)
    record = RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="other", retrieved_at=accepted_at),
        event_time=accepted_at,
        available_at=accepted_at,
        received_at=accepted_at,
        payload={},
        schema_version="sec-beneficial-ownership-outcome-v1",
    )

    try:
        outcome_from_raw_record(record)
    except Exception as error:
        assert "malformed" in str(error)
    else:
        raise AssertionError("invalid source must not decode")
