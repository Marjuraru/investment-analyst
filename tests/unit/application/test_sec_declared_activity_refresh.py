"""Focused contracts for incremental SEC declared-activity refreshes."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.activity_metric_pipeline import (
    ActivityMetricPipeline,
)
from investment_analyst.application.sec_declared_activity_refresh import (
    MAX_ACCESSIONS_PER_FAMILY_PER_RUN,
    SecDeclaredActivityRefreshError,
    SecDeclaredActivityRefreshService,
)
from investment_analyst.application.sec_declared_activity_refresh_models import (
    SecDeclaredActivityRefreshRequest,
)
from investment_analyst.application.sec_submissions_refresh import (
    SecSubmissionsRefreshService,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipResolutionOutcome,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_declared_activity_observations.service import (
    DeclaredActivityObservationService,
)
from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_to_raw_record,
)
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_SOURCE_ID,
    OwnershipEntry,
    OwnershipResolutionOutcome,
    OwnershipStatement,
    ReportingOwner,
)
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_index import (
    beneficial_ownership_filings,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_pipeline import (
    SecBeneficialOwnershipImportRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_TICKER,
    SecDocumentType,
    SecEdgarDocument,
)
from investment_analyst.providers.fundamentals.sec_fact_models import SUBMISSIONS_SCHEMA_VERSION
from investment_analyst.providers.ownership.sec_ownership_index import ownership_filings
from investment_analyst.providers.ownership.sec_ownership_pipeline import (
    SecOwnershipImportRequest,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_INSIDER_FORMS = ("3", "3/A", "4", "4/A", "5", "5/A")
_BENEFICIAL_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")


@dataclass(frozen=True, slots=True)
class _Row:
    accession: str
    form: str
    accepted_at: datetime
    primary_document: str


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _row(*, accession: str, form: str, accepted_at: datetime, name: str = "primary.xml") -> _Row:
    return _Row(accession=accession, form=form, accepted_at=accepted_at, primary_document=name)


def _submissions_document(*, retrieved_at: datetime, rows: tuple[_Row, ...]) -> SecEdgarDocument:
    recent = {
        "accessionNumber": [row.accession for row in rows],
        "filingDate": [row.accepted_at.date().isoformat() for row in rows],
        "reportDate": [(row.accepted_at.date() - timedelta(days=1)).isoformat() for row in rows],
        "acceptanceDateTime": [row.accepted_at.strftime("%Y-%m-%dT%H:%M:%S.000Z") for row in rows],
        "form": [row.form for row in rows],
        "primaryDocument": [row.primary_document for row in rows],
    }
    body = {
        "cik": APPLE_CIK,
        "name": "Apple Inc.",
        "tickers": [APPLE_TICKER],
        "exchanges": ["NASDAQ"],
        "filings": {"recent": recent},
    }
    return SecEdgarDocument(
        document_type=SecDocumentType.SUBMISSIONS,
        cik=APPLE_CIK,
        entity_name="Apple Inc.",
        retrieved_at=retrieved_at,
        request_url=f"https://data.sec.gov/submissions/CIK{APPLE_CIK}.json",
        body=body,
        body_sha256=hashlib.sha256(repr(body).encode()).hexdigest(),
        content_length=1,
    )


class _SubmissionsClient:
    """Serve one mutable Submissions snapshot while counting every GET."""

    def __init__(self, document: SecEdgarDocument) -> None:
        self.document = document
        self.calls = 0

    def fetch_submissions(self) -> SecEdgarDocument:
        self.calls += 1
        return self.document


def _latest_submissions(storage: LocalStorage, configuration: SecAssetConfiguration):
    records = storage.raw_records.list(
        asset_id=configuration.asset_id,
        source_id=configuration.submissions_source_id,
        schema_version=SUBMISSIONS_SCHEMA_VERSION,
    )
    assert records
    return records[-1]


def _filing(configuration: SecAssetConfiguration, row: _Row, **overrides: object) -> SecFiling:
    payload: dict[str, object] = {
        "filing_id": SecFiling.expected_id(configuration.cik, row.accession),
        "filer_cik": configuration.cik,
        "accession": row.accession,
        "form": row.form,
        "filing_date": row.accepted_at.date(),
        "report_date": (row.accepted_at - timedelta(days=1)).date(),
        "accepted_at": row.accepted_at,
        "is_amendment": row.form.endswith("/A"),
    }
    payload.update(overrides)
    return SecFiling(**payload)


def _ensure_revision(
    storage: LocalStorage,
    *,
    filing: SecFiling,
    document_name: str,
    content: bytes,
) -> SecDocumentRevision:
    """Reuse one persisted revision, or create it exactly like the integrated importers."""
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, document_name),
        filing=filing,
        name=document_name,
    )
    documents = SecDocumentRepository(storage.raw_records, storage.documents)
    blob = storage.documents.put(content)
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, blob.sha256, "sec-document-revision-v2"
    )
    existing = documents.get_revision(revision_id)
    if existing is not None:
        return existing
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
        source_url="https://www.sec.gov/Archives/edgar/data/320193/document.xml",
        revision_schema_version="sec-document-revision-v2",
    )
    storage.raw_records.save(revision_to_raw_record(revision))
    return revision


def _ownership_statement(filing: SecFiling, revision: SecDocumentRevision) -> OwnershipStatement:
    statement_id = OwnershipStatement.expected_id(
        revision.revision_id, "sec-ownership-statement-v2"
    )
    owner = ReportingOwner(
        reporting_owner_id=ReportingOwner.expected_id("0000000001"),
        cik="0000000001",
        name="Reporting Owner",
        is_officer=True,
        officer_title="CFO",
    )
    entry = OwnershipEntry(
        entry_id=OwnershipEntry.expected_id(statement_id, "non_derivative", "transaction", 0),
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik=owner.cik,
        security_title="Common Stock",
        transaction_date=date(2025, 3, 3),
        transaction_code="P",
        acquired_disposed="A",
        shares=Decimal("10"),
        price_per_share=Decimal("100"),
        shares_owned_following=Decimal("110"),
        ownership_nature="D",
    )
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form=filing.form,
        period_of_report=date(2025, 3, 3),
        issuer_cik=APPLE_CIK,
        issuer_name="Apple Inc.",
        reporting_owners=(owner,),
        entries=(entry,),
        available_at=revision.available_at,
        parsed_at=revision.retrieved_at,
        schema_version="sec-ownership-statement-v2",
    )


def _ownership_outcome(
    revision: SecDocumentRevision, *, status: str, reason_code: str
) -> OwnershipResolutionOutcome:
    filing = revision.document.filing
    outcome_id = OwnershipResolutionOutcome.expected_id(
        filing.accession,
        revision.document.name,
        revision.content_sha256,
        status,
        "sec-ownership-outcome-v2",
    )
    return OwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=OwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=uuid4(),
        declared_locator=revision.document.name,
        resource_name=revision.document.name,
        resource_url=revision.source_url,
        content_sha256=revision.content_sha256,
        content_size_bytes=revision.content_size_bytes,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256="a" * 64,
        available_at=filing.accepted_at,
        retrieved_at=revision.retrieved_at,
        status=status,
        reason_code=reason_code,
        resolver_version="sec-ownership-resolver-v2",
        schema_version="sec-ownership-outcome-v2",
    )


def _beneficial_statement(
    filing: SecFiling, revision: SecDocumentRevision
) -> BeneficialOwnershipStatement:
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id="equity:us:aapl",
        document_revision=revision,
        form=filing.form,
        subject_cik=APPLE_CIK,
        subject_name="Apple Inc.",
        reporting_person_cik="0000000002",
        reporting_person_name="Institutional Holder",
        event_date=date(2025, 3, 3),
        shares_beneficially_owned=Decimal("1234"),
        percent_of_class=Decimal("5.5"),
        available_at=revision.available_at,
        parsed_at=revision.retrieved_at,
        schema_version="sec-beneficial-ownership-statement-v1",
    )


def _beneficial_outcome(
    revision: SecDocumentRevision, *, status: str, reason_code: str
) -> BeneficialOwnershipResolutionOutcome:
    filing = revision.document.filing
    outcome_id = BeneficialOwnershipResolutionOutcome.expected_id(
        filing.accession,
        revision.document.name,
        revision.content_sha256,
        status,
    )
    return BeneficialOwnershipResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=BeneficialOwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
        asset_id="equity:us:aapl",
        filing=filing,
        discovery_raw_record_id=uuid4(),
        declared_locator=revision.document.name,
        resource_name=revision.document.name,
        resource_url=revision.source_url,
        content_sha256=revision.content_sha256,
        content_size_bytes=revision.content_size_bytes,
        manifest_url="https://www.sec.gov/Archives/index.json",
        manifest_sha256="b" * 64,
        available_at=filing.accepted_at,
        retrieved_at=revision.retrieved_at,
        status=status,
        reason_code=reason_code,
    )


def _save_outcome_once(repository, outcome) -> None:
    """Persist one outcome like the integrated importers: reuse an existing identity."""
    if repository.get_outcome(outcome.outcome_id) is None:
        repository.save_outcome(outcome)


class _InsiderPipeline:
    """Deterministic stand-in for the integrated Section 16 importer."""

    def __init__(self, storage: LocalStorage, configuration: SecAssetConfiguration) -> None:
        self._storage = storage
        self._configuration = configuration
        self.behaviour: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls: list[tuple[str, ...]] = []

    def run(self, request: SecOwnershipImportRequest):
        self.calls.append(tuple(request.accessions))
        if self.error is not None:
            raise self.error
        record = _latest_submissions(self._storage, self._configuration)
        by_accession = {
            filing.accession: filing for filing in ownership_filings(record, self._configuration)
        }
        repository = OwnershipRepository(self._storage.raw_records)
        for accession in request.accessions:
            kind = self.behaviour.get(accession, "statement")
            if kind == "raise":
                raise StorageError("simulated accession failure")
            metadata = by_accession[accession]
            filing = _filing(
                self._configuration,
                _row(
                    accession=metadata.accession,
                    form=metadata.form,
                    accepted_at=metadata.accepted_at,
                    name=metadata.primary_document,
                ),
            )
            revision = _ensure_revision(
                self._storage,
                filing=filing,
                document_name=metadata.primary_document,
                content=f"ownership-{accession}".encode(),
            )
            kind = self.behaviour.get(accession, "statement")
            if kind == "skip":
                continue
            if kind == "rejected":
                _save_outcome_once(
                    repository,
                    _ownership_outcome(revision, status="rejected", reason_code="not_xml"),
                )
                continue
            _save_outcome_once(
                repository,
                _ownership_outcome(revision, status="accepted", reason_code="ownership_xml"),
            )
            if kind == "accepted":
                continue
            statement = _ownership_statement(filing, revision)
            if repository.get(statement.statement_id) is None:
                repository.save(statement)
        return ()


class _BeneficialPipeline:
    """Deterministic stand-in for the integrated Schedule 13D/13G importer."""

    def __init__(self, storage: LocalStorage, configuration: SecAssetConfiguration) -> None:
        self._storage = storage
        self._configuration = configuration
        self.behaviour: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls: list[tuple[str, ...]] = []

    def run(self, request: SecBeneficialOwnershipImportRequest):
        self.calls.append(tuple(request.accessions))
        if self.error is not None:
            raise self.error
        record = _latest_submissions(self._storage, self._configuration)
        by_accession = {
            filing.accession: filing
            for filing in beneficial_ownership_filings(record, self._configuration)
        }
        repository = BeneficialOwnershipRepository(self._storage.raw_records)
        for accession in request.accessions:
            kind = self.behaviour.get(accession, "statement")
            if kind == "raise":
                raise StorageError("simulated accession failure")
            metadata = by_accession[accession]
            filing = _filing(
                self._configuration,
                _row(
                    accession=metadata.accession,
                    form=metadata.form,
                    accepted_at=metadata.accepted_at,
                    name=metadata.primary_document,
                ),
            )
            revision = _ensure_revision(
                self._storage,
                filing=filing,
                document_name=metadata.primary_document,
                content=f"beneficial-{accession}".encode(),
            )
            kind = self.behaviour.get(accession, "statement")
            if kind == "skip":
                continue
            if kind == "rejected":
                _save_outcome_once(
                    repository,
                    _beneficial_outcome(
                        revision, status="rejected", reason_code="no_unique_top_level_xml"
                    ),
                )
                continue
            _save_outcome_once(
                repository,
                _beneficial_outcome(revision, status="accepted", reason_code="structured_sec_xml"),
            )
            if kind == "accepted":
                continue
            statement = _beneficial_statement(filing, revision)
            if repository.get(statement.statement_id) is None:
                repository.save(statement)
        return ()


def _service(
    storage: LocalStorage,
    client: _SubmissionsClient,
) -> tuple[SecDeclaredActivityRefreshService, _InsiderPipeline, _BeneficialPipeline]:
    configuration = _configuration()
    insider = _InsiderPipeline(storage, configuration)
    beneficial = _BeneficialPipeline(storage, configuration)
    service = SecDeclaredActivityRefreshService(
        storage,
        configuration=configuration,
        submissions_service=SecSubmissionsRefreshService(
            storage,
            configuration=configuration,
            issuer_client=client,
        ),
        ownership_pipeline=insider,
        beneficial_pipeline=beneficial,
        observation_service=DeclaredActivityObservationService(storage),
        metric_pipeline=ActivityMetricPipeline(storage),
    )
    return service, insider, beneficial


def _persisted_accessions(storage: LocalStorage, known_at: datetime) -> tuple[str, ...]:
    insider = OwnershipRepository(storage.raw_records).list(
        asset_id="equity:us:aapl", known_at=known_at
    )
    beneficial = BeneficialOwnershipRepository(storage.raw_records).list(
        asset_id="equity:us:aapl", known_at=known_at
    )
    return tuple(
        sorted(statement.document_revision.document.filing.accession for statement in insider)
    ) + tuple(
        sorted(statement.document_revision.document.filing.accession for statement in beneficial)
    )


def _request() -> SecDeclaredActivityRefreshRequest:
    return SecDeclaredActivityRefreshRequest(asset_id="equity:us:aapl")


def test_baseline_selects_one_recent_accession_per_exact_form_and_imports_both_families(
    tmp_path: Path,
) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=10),
            name="form4-old.xml",
        ),
        _row(
            accession="0000320193-25-000002",
            form="4",
            accepted_at=first_at - timedelta(days=1),
            name="form4.xml",
        ),
        _row(
            accession="0000320193-25-000003",
            form="5",
            accepted_at=first_at - timedelta(days=2),
            name="form5.xml",
        ),
        _row(
            accession="0000320193-25-000004",
            form="SC 13G",
            accepted_at=first_at - timedelta(days=30),
            name="filing-old.xml",
        ),
        _row(
            accession="0000320193-25-000005",
            form="SC 13G",
            accepted_at=first_at - timedelta(days=3),
            name="filing.xml",
        ),
        _row(
            accession="0000320193-25-000006",
            form="13F-HR",
            accepted_at=first_at - timedelta(days=2),
            name="institutional.xml",
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, beneficial = _service(storage, client)
        summary = service.run(_request())

        assert client.calls == 1
        assert summary.submissions_created == 1
        assert summary.submissions_reused == 0
        assert insider.calls == [("0000320193-25-000003", "0000320193-25-000002")]
        assert beneficial.calls == [("0000320193-25-000005",)]
        assert summary.insider.accessions_imported == (
            "0000320193-25-000002",
            "0000320193-25-000003",
        )
        assert summary.beneficial.accessions_imported == ("0000320193-25-000005",)
        assert summary.insider.forms_evaluated == _INSIDER_FORMS
        assert summary.beneficial.forms_evaluated == _BENEFICIAL_FORMS
        assert "13F-HR" not in summary.insider.forms_evaluated
        assert "13F-HR" not in summary.beneficial.forms_evaluated
        assert summary.insider.backlog_count == 0
        assert summary.beneficial.backlog_count == 0
        assert summary.coverage_complete is True
        assert summary.traceability_verified is True
        assert summary.observations_created > 0
        assert summary.metrics_created > 0
        assert _persisted_accessions(storage, first_at) == (
            "0000320193-25-000002",
            "0000320193-25-000003",
            "0000320193-25-000005",
        )
        assert {
            record.source.source_id
            for record in storage.raw_records.list(asset_id="equity:us:aapl")
        } == {
            "sec-edgar:aapl:submissions",
            OWNERSHIP_SOURCE_ID,
            BENEFICIAL_OWNERSHIP_SOURCE_ID,
            SEC_DOCUMENT_SOURCE_ID,
        }
        assert set(summary.model_dump()) == {
            "schema_version",
            "asset_id",
            "request",
            "policy_version",
            "submissions_source_id",
            "submissions_raw_record_id",
            "submissions_checked_at",
            "submissions_record_available_at",
            "submissions_created",
            "submissions_reused",
            "insider",
            "beneficial",
            "observations_created",
            "observations_reused",
            "observations_skipped",
            "metrics_created",
            "metrics_reused",
            "metrics_skipped",
            "backlog_count",
            "coverage_complete",
            "traceability_verified",
        }


def test_rerun_with_the_same_snapshot_makes_zero_archives_calls_and_reuses_ids(
    tmp_path: Path,
) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=1),
        ),
        _row(
            accession="0000320193-25-000002",
            form="SC 13G",
            accepted_at=first_at - timedelta(days=2),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, beneficial = _service(storage, client)
        first = service.run(_request())
        raw_records_before = len(storage.raw_records.list(asset_id="equity:us:aapl"))
        second = service.run(_request())

        assert client.calls == 2
        assert second.submissions_created == 0
        assert second.submissions_reused == 1
        assert second.submissions_checked_at == first.submissions_checked_at
        assert insider.calls == [("0000320193-25-000001",)]
        assert beneficial.calls == [("0000320193-25-000002",)]
        assert second.insider.accessions_selected == ()
        assert second.beneficial.accessions_selected == ()
        assert second.insider.statements_created == 0
        assert second.insider.statements_reused == 0
        assert second.coverage_complete is True
        assert second.submissions_raw_record_id == first.submissions_raw_record_id
        assert len(storage.raw_records.list(asset_id="equity:us:aapl")) == raw_records_before
        assert second.observations_created == 0
        assert second.observations_reused > 0
        assert second.metrics_created == 0


def test_new_accession_imports_only_the_delta_after_the_watermark(tmp_path: Path) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    first_rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=5),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=first_rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, _ = _service(storage, client)
        service.run(_request())
        insider.calls.clear()

        client.document = _submissions_document(
            retrieved_at=first_at + timedelta(days=4),
            rows=(
                *first_rows,
                _row(
                    accession="0000320193-25-000009",
                    form="4",
                    accepted_at=first_at + timedelta(days=3),
                ),
            ),
        )
        delta = service.run(_request())

        assert insider.calls == [("0000320193-25-000009",)]
        assert delta.insider.accessions_selected == ("0000320193-25-000009",)
        assert delta.insider.accessions_imported == ("0000320193-25-000009",)
        assert delta.insider.backlog_count == 0
        assert delta.coverage_complete is True


def test_family_limit_is_global_per_run_and_the_next_run_continues_without_gaps(
    tmp_path: Path,
) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    base = (
        _row(
            accession="0000320193-25-000000",
            form="4",
            accepted_at=first_at - timedelta(days=30),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=base))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, _ = _service(storage, client)
        service.run(_request())
        insider.calls.clear()

        delta_rows = tuple(
            _row(
                accession=f"0000320193-25-{index:06d}",
                form="4",
                accepted_at=first_at + timedelta(days=index),
            )
            for index in range(1, 31)
        )
        client.document = _submissions_document(
            retrieved_at=first_at + timedelta(days=31), rows=(*base, *delta_rows)
        )
        capped = service.run(_request())

        expected_first = tuple(f"0000320193-25-{index:06d}" for index in range(1, 26))
        assert MAX_ACCESSIONS_PER_FAMILY_PER_RUN == 25
        assert insider.calls == [expected_first]
        assert capped.insider.accessions_imported == expected_first
        assert capped.insider.backlog_count == 5
        assert capped.backlog_count == 5
        assert capped.coverage_complete is False
        assert capped.traceability_verified is True

        insider.calls.clear()
        resumed = service.run(_request())

        expected_second = tuple(f"0000320193-25-{index:06d}" for index in range(26, 31))
        assert insider.calls == [expected_second]
        assert resumed.insider.accessions_imported == expected_second
        assert resumed.insider.backlog_count == 0
        assert resumed.coverage_complete is True


def test_terminal_rejection_is_not_refetched_and_partial_state_resumes(tmp_path: Path) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=4),
        ),
        _row(
            accession="0000320193-25-000002",
            form="5",
            accepted_at=first_at - timedelta(days=3),
        ),
        _row(
            accession="0000320193-25-000003",
            form="SC 13G",
            accepted_at=first_at - timedelta(days=2),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, beneficial = _service(storage, client)
        insider.behaviour["0000320193-25-000001"] = "rejected"
        insider.behaviour["0000320193-25-000002"] = "accepted"
        beneficial.behaviour["0000320193-25-000003"] = "accepted"
        partial = service.run(_request())

        assert partial.insider.accessions_rejected == ("0000320193-25-000001",)
        assert partial.insider.accessions_incomplete == ("0000320193-25-000002",)
        assert partial.beneficial.accessions_incomplete == ("0000320193-25-000003",)
        assert partial.traceability_verified is False
        assert partial.coverage_complete is False

        insider.calls.clear()
        beneficial.calls.clear()
        insider.behaviour.clear()
        beneficial.behaviour.clear()
        resumed = service.run(_request())

        assert insider.calls == [("0000320193-25-000002",)]
        assert beneficial.calls == [("0000320193-25-000003",)]
        assert resumed.insider.accessions_rejected == ()
        assert resumed.insider.accessions_imported == ("0000320193-25-000002",)
        assert resumed.beneficial.accessions_imported == ("0000320193-25-000003",)
        assert resumed.traceability_verified is True
        assert resumed.coverage_complete is True


def test_family_failure_is_typed_and_preserves_previous_progress(tmp_path: Path) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=4),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, _ = _service(storage, client)
        service.run(_request())

        client.document = _submissions_document(
            retrieved_at=first_at + timedelta(days=1),
            rows=(
                *rows,
                _row(
                    accession="0000320193-25-000002",
                    form="4",
                    accepted_at=first_at + timedelta(days=1),
                ),
            ),
        )
        insider.error = StorageError("simulated storage failure")
        with pytest.raises(SecDeclaredActivityRefreshError) as error:
            service.run(_request())

        assert isinstance(error.value.__cause__, StorageError)
        assert "simulated storage failure" not in str(error.value)
        assert _persisted_accessions(storage, first_at) == ("0000320193-25-000001",)


def test_one_failing_accession_does_not_stall_the_rest_of_its_family(tmp_path: Path) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    rows = (
        _row(
            accession="0000320193-25-000001",
            form="4",
            accepted_at=first_at - timedelta(days=4),
        ),
        _row(
            accession="0000320193-25-000002",
            form="5",
            accepted_at=first_at - timedelta(days=3),
        ),
        _row(
            accession="0000320193-25-000003",
            form="SC 13G",
            accepted_at=first_at - timedelta(days=2),
        ),
    )
    client = _SubmissionsClient(_submissions_document(retrieved_at=first_at, rows=rows))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, beneficial = _service(storage, client)
        # The oldest selected accession fails hard and aborts the importer's own loop.
        insider.behaviour["0000320193-25-000001"] = "raise"
        summary = service.run(_request())

        assert insider.calls == [
            ("0000320193-25-000001", "0000320193-25-000002"),
            ("0000320193-25-000002",),
        ]
        assert summary.insider.accessions_incomplete == ("0000320193-25-000001",)
        assert summary.insider.accessions_imported == ("0000320193-25-000002",)
        assert summary.beneficial.accessions_imported == ("0000320193-25-000003",)
        assert summary.traceability_verified is False
        assert summary.coverage_complete is False
        assert "0000320193-25-000002" in _persisted_accessions(storage, first_at)


def test_refresh_rejects_a_foreign_asset_without_provider_work(tmp_path: Path) -> None:
    first_at = datetime(2025, 4, 1, tzinfo=UTC)
    client = _SubmissionsClient(
        _submissions_document(
            retrieved_at=first_at,
            rows=(
                _row(
                    accession="0000320193-25-000001",
                    form="4",
                    accepted_at=first_at - timedelta(days=1),
                ),
            ),
        )
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service, insider, beneficial = _service(storage, client)
        with pytest.raises(SecDeclaredActivityRefreshError, match="does not match SEC issuer"):
            service.run(SecDeclaredActivityRefreshRequest(asset_id="equity:us:tsm"))

        assert client.calls == 0
        assert insider.calls == []
        assert beneficial.calls == []
