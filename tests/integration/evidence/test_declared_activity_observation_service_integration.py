from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    statement_to_raw_record as beneficial_statement_to_raw_record,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    normalize_ownership_statement,
)
from investment_analyst.evidence.sec_declared_activity_observations.service import (
    DeclaredActivityObservationIdentityConflictError,
    DeclaredActivityObservationService,
    DeclaredActivityObservationServiceError,
)
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_ownership.models import (
    OwnershipEntry,
    OwnershipStatement,
    ReportingOwner,
)
from investment_analyst.evidence.sec_ownership.repository import (
    statement_to_raw_record as ownership_statement_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_ASSET_ID = "equity:us:aapl"
_ISSUER_CIK = "0000320193"
_OWNER_CIK = "0001214156"


def _filing(accession: str, accepted_at: datetime, form: str) -> SecFiling:
    return SecFiling(
        filing_id=SecFiling.expected_id(_ISSUER_CIK, accession),
        filer_cik=_ISSUER_CIK,
        accession=accession,
        form=form,
        filing_date=accepted_at.date(),
        report_date=accepted_at.date() - timedelta(days=1),
        accepted_at=accepted_at,
        is_amendment=False,
    )


def _revision(filing: SecFiling, name: str) -> SecDocumentRevision:
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
        filing=filing,
        name=name,
    )
    checksum = uuid4().hex + uuid4().hex[:32]
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, REVISION_SCHEMA_VERSION_V2
    )
    return SecDocumentRevision(
        revision_id=revision_id,
        asset_id=_ASSET_ID,
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=checksum,
        content_size_bytes=1,
        available_at=filing.accepted_at,
        retrieved_at=filing.accepted_at + timedelta(hours=1),
        source_url=f"https://www.sec.gov/Archives/{name}",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )


def _ownership_statement(*, accession: str, accepted_at: datetime) -> OwnershipStatement:
    filing = _filing(accession, accepted_at, "4")
    revision = _revision(filing, f"{accession}-form4.xml")
    statement_id = OwnershipStatement.expected_id(
        revision.revision_id, "sec-ownership-statement-v2"
    )
    owner = ReportingOwner(
        reporting_owner_id=ReportingOwner.expected_id(_OWNER_CIK),
        cik=_OWNER_CIK,
        name="COOK TIMOTHY D",
        is_officer=True,
        officer_title="Chief Executive Officer",
    )
    entry_id = OwnershipEntry.expected_id(statement_id, "non_derivative", "transaction", 0)
    entry = OwnershipEntry(
        entry_id=entry_id,
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik=_OWNER_CIK,
        security_title="Common Stock",
        transaction_date=accepted_at.date(),
        transaction_code="S",
        acquired_disposed="D",
        shares=Decimal("1000"),
        price_per_share=Decimal("225.50"),
        shares_owned_following=Decimal("500000"),
        ownership_nature="D",
    )
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=_ASSET_ID,
        document_revision=revision,
        form="4",
        period_of_report=filing.report_date,
        issuer_cik=_ISSUER_CIK,
        issuer_name="Apple Inc.",
        reporting_owners=(owner,),
        entries=(entry,),
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(hours=1),
        schema_version="sec-ownership-statement-v2",
    )


def _beneficial_statement(*, accession: str, accepted_at: datetime) -> BeneficialOwnershipStatement:
    filing = _filing(accession, accepted_at, "SC 13G")
    revision = _revision(filing, f"{accession}-sc13g.xml")
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=_ASSET_ID,
        document_revision=revision,
        form="SC 13G",
        subject_cik=_ISSUER_CIK,
        subject_name="Apple Inc.",
        reporting_person_cik="0000102909",
        reporting_person_name="Vanguard Group Inc",
        event_date=accepted_at.date(),
        shares_beneficially_owned=Decimal("1300000000"),
        percent_of_class=Decimal("8.1"),
        available_at=accepted_at,
        parsed_at=accepted_at + timedelta(hours=1),
    )


def _seed_ownership(storage: LocalStorage, statement: OwnershipStatement) -> None:
    storage.raw_records.save(revision_to_raw_record(statement.document_revision))
    storage.raw_records.save(ownership_statement_to_raw_record(statement))


def _seed_beneficial(storage: LocalStorage, statement: BeneficialOwnershipStatement) -> None:
    storage.raw_records.save(revision_to_raw_record(statement.document_revision))
    storage.raw_records.save(beneficial_statement_to_raw_record(statement))


_NOW = datetime(2026, 1, 15, tzinfo=UTC)


def test_service_requires_writable_storage(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass  # creates the DuckDB database so a read-only open below can succeed
    with (
        LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage,
        pytest.raises(StorageError, match="writable storage"),
    ):
        DeclaredActivityObservationService(storage).normalize(asset_id=_ASSET_ID, known_at=_NOW)


def test_service_rejects_a_naive_clock(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = DeclaredActivityObservationService(
            storage,
            clock=lambda: datetime(2026, 1, 15),  # noqa: DTZ001
        )
        with pytest.raises(DeclaredActivityObservationServiceError, match="timezone"):
            service.normalize(asset_id=_ASSET_ID, known_at=_NOW)


def test_first_run_creates_second_run_reuses_all(tmp_path: Path) -> None:
    ownership = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    beneficial = _beneficial_statement(accession="0000320193-25-000002", accepted_at=_NOW)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, ownership)
        _seed_beneficial(storage, beneficial)
        raw_count_before = storage.raw_records.count()

        service = DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=2)
        )
        first = service.normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(hours=1))
        assert first.observations_generated == 5
        assert first.observations_created == 5
        assert first.observations_reused == 0
        assert first.skipped_total == 0
        assert storage.observations.count() == 5

        service_again = DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(days=3)
        )
        second = service_again.normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(hours=1))
        assert second.observations_generated == 5
        assert second.observations_created == 0
        assert second.observations_reused == 5
        assert storage.observations.count() == 5
        assert storage.raw_records.count() == raw_count_before


def test_known_at_cutoff_excludes_not_yet_available_evidence(tmp_path: Path) -> None:
    early = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    late = _ownership_statement(
        accession="0000320193-25-000004", accepted_at=_NOW + timedelta(days=10)
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, early)
        _seed_ownership(storage, late)

        cut_before_late = DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(days=1)
        ).normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(days=1))
        assert cut_before_late.statements_examined == 1
        assert cut_before_late.observations_generated == 3

        cut_after_late = DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(days=20)
        ).normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(days=20))
        assert cut_after_late.statements_examined == 2
        assert cut_after_late.observations_generated == 6
        assert cut_after_late.observations_created == 3
        assert cut_after_late.observations_reused == 3


def test_amendment_coexists_with_original(tmp_path: Path) -> None:
    original = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    amendment = _ownership_statement(
        accession="0000320193-25-000005", accepted_at=_NOW + timedelta(days=2)
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, original)
        _seed_ownership(storage, amendment)
        summary = DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(days=3)
        ).normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(days=3))
        assert summary.statements_examined == 2
        assert summary.observations_generated == 6
        original_observations = storage.observations.list(
            asset_id=_ASSET_ID, source_id="sec-edgar:section16-ownership"
        )
        by_raw_record = {obs.raw_record_id for obs in original_observations}
        assert original.raw_record_id in by_raw_record
        assert amendment.raw_record_id in by_raw_record


def test_identity_conflict_raises_and_preserves_prior_observations(tmp_path: Path) -> None:
    statement = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    normalized_at = _NOW + timedelta(hours=2)
    precomputed = {
        obs.field_name: obs
        for obs in normalize_ownership_statement(
            statement, normalized_at=normalized_at
        ).observations
    }
    forged = precomputed["insider_shares_owned_following"].model_copy(
        update={"value": Decimal("999999")}
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, statement)
        storage.observations.save(forged)
        raw_count_before = storage.raw_records.count()

        service = DeclaredActivityObservationService(storage, clock=lambda: normalized_at)
        with pytest.raises(DeclaredActivityObservationIdentityConflictError):
            service.normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(hours=1))

        assert storage.raw_records.count() == raw_count_before
        assert storage.observations.count() == 3
        unchanged = storage.observations.get(forged.observation_id)
        assert unchanged.value == Decimal("999999")
        created_shares = storage.observations.get(
            precomputed["insider_transaction_shares"].observation_id
        )
        assert created_shares.value == Decimal("1000")


def test_writes_only_normalized_observations_table(tmp_path: Path) -> None:
    statement = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, statement)
        DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=1)
        ).normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(hours=1))
        assert storage.metric_results.count() == 0
        assert storage.diagnostics.count() == 0


def test_observation_is_reconstructable_to_its_raw_record(tmp_path: Path) -> None:
    statement = _ownership_statement(accession="0000320193-25-000001", accepted_at=_NOW)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_ownership(storage, statement)
        DeclaredActivityObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=1)
        ).normalize(asset_id=_ASSET_ID, known_at=_NOW + timedelta(hours=1))
        observations = storage.observations.list(asset_id=_ASSET_ID)
        assert observations
        for observation in observations:
            raw = storage.raw_records.get(observation.raw_record_id)
            assert raw.record_id == statement.raw_record_id
