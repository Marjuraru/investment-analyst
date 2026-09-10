from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipResolutionOutcome,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
    outcome_from_raw_record,
    outcome_to_raw_record,
    statement_to_raw_record,
)
from investment_analyst.evidence.sec_documents.models import (
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.storage import LocalStorage, StoragePaths


def _filing(*, accession: str, accepted_at: datetime, form: str = "SC 13D") -> SecFiling:
    return SecFiling(
        filing_id=SecFiling.expected_id("0000320193", accession),
        filer_cik="0000320193",
        accession=accession,
        form=form,
        filing_date=accepted_at.date(),
        report_date=accepted_at.date() - timedelta(days=1),
        accepted_at=accepted_at,
        is_amendment=form.endswith("/A"),
    )


def _outcome(
    *,
    filing: SecFiling,
    checksum: str,
    discovery_id,
    status: str = "accepted",
    reason_code: str = "structured_sec_xml",
) -> BeneficialOwnershipResolutionOutcome:
    outcome_id = BeneficialOwnershipResolutionOutcome.expected_id(
        filing.accession, "filing.htm", checksum, status
    )
    return BeneficialOwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=BeneficialOwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=discovery_id,
        declared_locator="filing.htm",
        resource_name="filing.htm",
        resource_url="https://www.sec.gov/Archives/filing.htm",
        content_sha256=checksum,
        content_size_bytes=12,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256="b" * 64,
        available_at=filing.accepted_at,
        retrieved_at=filing.accepted_at + timedelta(hours=1),
        status=status,
        reason_code=reason_code,
    )


def _revision(storage: LocalStorage, *, filing: SecFiling, content: bytes) -> SecDocumentRevision:
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "filing.xml"),
        filing=filing,
        name="filing.xml",
    )
    blob = storage.documents.put(content)
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, blob.sha256, "sec-document-revision-v2"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=blob.sha256,
        content_size_bytes=blob.size_bytes,
        available_at=filing.accepted_at,
        retrieved_at=filing.accepted_at + timedelta(hours=1),
        source_url="https://www.sec.gov/Archives/filing.xml",
        revision_schema_version="sec-document-revision-v2",
    )
    storage.raw_records.save(revision_to_raw_record(revision))
    return revision


def _statement(revision: SecDocumentRevision) -> BeneficialOwnershipStatement:
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form=revision.document.filing.form,
        subject_cik="0000320193",
        subject_name="Apple Inc.",
        reporting_person_cik="0000000002",
        reporting_person_name="Institutional Holder",
        event_date=date(2025, 1, 30),
        shares_beneficially_owned=None,
        percent_of_class=None,
        available_at=revision.available_at,
        parsed_at=revision.retrieved_at,
        schema_version="sec-beneficial-ownership-statement-v1",
    )


def test_outcome_raw_record_round_trips_exactly() -> None:
    accepted_at = datetime(2025, 1, 31, 18, tzinfo=UTC)
    filing = _filing(accession="0000320193-25-000001", accepted_at=accepted_at)
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


def test_typed_outcome_listing_distinguishes_terminal_and_partial_accessions(
    tmp_path: Path,
) -> None:
    known_at = datetime(2025, 2, 10, tzinfo=UTC)
    discovery_id = uuid4()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = BeneficialOwnershipRepository(storage.raw_records)

        accepted_filing = _filing(
            accession="0000320193-25-000001", accepted_at=known_at - timedelta(days=3)
        )
        accepted_revision = _revision(storage, filing=accepted_filing, content=b"accepted!")
        storage.raw_records.save(statement_to_raw_record(_statement(accepted_revision)))
        repository.save_outcome(
            _outcome(
                filing=accepted_filing,
                checksum=accepted_revision.content_sha256,
                discovery_id=discovery_id,
            )
        )

        rejected_filing = _filing(
            accession="0000320193-25-000002", accepted_at=known_at - timedelta(days=2)
        )
        repository.save_outcome(
            _outcome(
                filing=rejected_filing,
                checksum="c" * 64,
                discovery_id=discovery_id,
                status="rejected",
                reason_code="no_unique_top_level_xml",
            )
        )

        partial_filing = _filing(
            accession="0000320193-25-000003", accepted_at=known_at - timedelta(days=1)
        )
        repository.save_outcome(
            _outcome(
                filing=partial_filing,
                checksum="d" * 64,
                discovery_id=discovery_id,
            )
        )

        states = {
            state.accession: state
            for state in repository.list_accession_states(
                asset_id="equity:us:aapl", known_at=known_at
            )
        }

        assert states["0000320193-25-000001"].resolution == "accepted"
        assert states["0000320193-25-000001"].terminal is True
        assert states["0000320193-25-000002"].resolution == "rejected"
        assert states["0000320193-25-000002"].terminal is True
        assert states["0000320193-25-000003"].resolution == "partial"
        assert states["0000320193-25-000003"].terminal is False

        earlier = repository.list_accession_states(
            asset_id="equity:us:aapl",
            known_at=known_at - timedelta(days=2, seconds=1),
        )
        assert {state.accession for state in earlier} == {"0000320193-25-000001"}


def test_beneficial_source_identity_is_stable() -> None:
    assert BENEFICIAL_OWNERSHIP_SOURCE_ID == "sec-edgar:beneficial-ownership-13d-13g"
